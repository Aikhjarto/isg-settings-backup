#!/usr/bin/env python3
"""Read and write settings of a Stiebel Eltron ISG / WPM through the ISG web interface.

Commands:
  get PAGE [valNNNN ...]                 show the settings fields of a page (all, or the ones listed)
  set PAGE valNNNN=VALUE [...]           write fields (checked against the page's range and options)
  program PAGE                           show a time program (one line per day)
  set-program PAGE DAY|all HH:MM-HH:MM[,HH:MM-HH:MM]   set the windows of one day or all days
  versions                               software versions the ISG reports
  crawl                                  rebuild pages.txt (page ids, names, field ids) from the ISG menu

Writes (set, set-program) refuse to run when the software versions the ISG reports differ from those
pages.txt was made with: after a firmware update, field ids and meanings can change. Check the pages,
run `crawl`, or pass --allow-version-change.

Page ids and field ids: pages.txt, e.g. ?s=4,2,1 = SETTINGS > HEATING > HEATING CIRCUIT 2.
Environment: ISG_HOST (default servicewelt.private.lan).

The write path is what the web form does (POST save.php). It is not documented by Stiebel Eltron.
The ISG web interface has no login by default: use this only inside your own network.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import socket
import struct
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HOST = os.environ.get("ISG_HOST", "servicewelt.private.lan")
PAGES_FILE = Path(__file__).with_name("pages.txt")
TIMEOUT = 60
VERSION_PAGE = "?s=2,7"
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class VersionMismatch(RuntimeError):
    """The device runs other software versions than the reference."""


# --- device access -------------------------------------------------------------------------------

def fetch(page: str) -> tuple[str, urllib.request.OpenerDirector]:
    """Return the page HTML and an opener that keeps its session cookie."""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    return opener.open(f"http://{HOST}/{page}", timeout=TIMEOUT).read().decode("latin-1"), opener


def post(page: str, data: list[dict[str, str]]) -> dict:
    """Save fields like the web form: POST save.php data=[{"name": "valNNNN", "value": "x.y"}]."""
    _, opener = fetch(page)
    body = urllib.parse.urlencode({"data": json.dumps(data)}).encode()
    return json.loads(opener.open(f"http://{HOST}/save.php", data=body, timeout=TIMEOUT).read().decode("latin-1"))


def modbus_read(function: int, register: int, count: int = 1) -> list[int]:
    """Read Modbus TCP registers; function 3 = holding, 4 = input; register 1-based (manual numbering)."""
    with socket.create_connection((HOST, 502), timeout=10) as s:
        s.sendall(struct.pack(">HHHBBHH", 1, 0, 6, 1, function, register - 1, count))
        head = b""
        while len(head) < 9:
            head += s.recv(9 - len(head))
        if head[7] & 0x80:
            raise OSError(f"Modbus exception {head[8]} at {function}:{register}")
        body = b""
        while len(body) < head[8]:
            body += s.recv(head[8] - len(body))
    return list(struct.unpack(f">{count}h", body))


# --- parsing (pure) ------------------------------------------------------------------------------

def page_text(html: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text).replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text)


def parse_versions(html: str) -> dict[str, str]:
    """Software versions from INFO > DIAGNOSIS > SYSTEM (?s=2,7), e.g. {"software 449": "19.0.5", "ISG": "1.6.6.0"}."""
    text = page_text(html)
    versions: dict[str, str] = {}
    # Several FET room units share software 501, so their unit number is part of the key.
    for unit, number, major, minor, revision in re.findall(
            r"(?:(FET \d+) )?SOFTWARE (\d+) Major version (\d+) Minor version (\d+) Revision (\d+)", text):
        versions[f"{unit} software {number}" if unit else f"software {number}"] = f"{major}.{minor}.{revision}"
    for key in ("HP-TYPE", "HP-ID"):
        if match := re.search(rf"{key} (\d+)", text):
            versions[key] = match.group(1)
    if match := re.search(r"ISG Major version (\d+) Minor version (\d+) Revision (\d+) Build (\d+)", text):
        versions["ISG"] = ".".join(match.groups())
    if "ISG" not in versions or not any(key.startswith("software ") for key in versions):
        raise ValueError("no software versions found on the system page")
    return versions


def parse_parameter_set(html: str) -> str | None:
    """PARAMETERSET of the heat pump: informational only (read 1 and 0 within an hour)."""
    match = re.search(r"PARAMETERSET (\d+)", page_text(html))
    return match.group(1) if match else None


def parse_settings(html: str) -> dict[str, dict]:
    """Every settings field of a page.

    {"valNNNN": {"label", "value", "kind": number|option, "min", "max", "options": {value: label}}}.
    Numbers keep the page's decimal comma ("22,0"); options hold the selected value.
    """
    fields = {}
    for vid, block in re.findall(r'id="calval(\d+)"(.*?)(?=id="calval|\Z)', html, re.S):
        title = re.search(r'<h3 class="title">(.*?)</h3>', block, re.S)
        value = re.findall(r"jsvalues\['\d+'\]\['val'\]='([^']*)'", block)
        option = re.findall(r'<input[^>]*type="radio"[^>]*value="([^"]*)"[^>]*checked', block)
        if not value and not option:
            continue
        field = {"label": re.sub(r"\s+", " ", title.group(1)).strip() if title else "",
                 "value": (value or option)[0], "kind": "number" if value else "option"}
        for key in ("min", "max"):
            if match := re.search(rf"valSettings\['val{vid}'\]\['{key}'\] = '([^']*)'", block):
                field[key] = match.group(1)
        options = re.findall(r'<input[^>]*type="radio"[^>]*alt="([^"]*)"[^>]*value="([^"]*)"', block)
        if options:
            field["options"] = {val: label for label, val in options}
        fields[f"val{vid}"] = field
    return fields


def parse_time_program(html: str) -> dict[str, dict]:
    """Per day: field ids of the slots, the windows in quarter hours (0-96) and the off value."""
    days = {}
    for day, values, ids, off in re.findall(
            r'<h2 class="curdef"[^>]*>(\w+)</h2>.*?values:\s*\[([^\]]*)\].*?ids:\s*\[([^\]]*)\].*?defaultValue:\s*(\d+)',
            html, re.S):
        quarters = [int(x) for x in values.split(",") if x.strip()]
        days[day] = {"ids": re.findall(r'"(val\d+)"', ids),
                     "windows": [quarters[i:i + 2] for i in range(0, len(quarters) - 1, 2)], "off": int(off)}
    return days


def time_program_fields(entry: dict) -> list[dict[str, str]]:
    """The form fields that set one day's windows; unused slots get the off value."""
    if len(entry["windows"]) > len(entry["ids"]):
        raise ValueError(f"at most {len(entry['ids'])} windows per day")
    slots = [f"{start};{end}" for start, end in entry["windows"]]
    slots += [f"{entry['off']};{entry['off']}"] * (len(entry["ids"]) - len(slots))
    return [{"name": vid, "value": slots[n]} for n, vid in enumerate(entry["ids"])]


def parse_windows(text: str) -> list[list[int]]:
    """"10:00-20:00,22:00-24:00" -> [[40, 80], [88, 96]] (quarter hours)."""
    windows = []
    for part in text.split(","):
        match = re.fullmatch(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", part.strip())
        if not match:
            raise ValueError(f"window {part!r} is not HH:MM-HH:MM")
        h1, m1, h2, m2 = map(int, match.groups())
        if m1 % 15 or m2 % 15:
            raise ValueError(f"window {part!r}: the ISG uses 15-minute steps")
        start, end = h1 * 4 + m1 // 15, h2 * 4 + m2 // 15
        if not 0 <= start < end <= 96:
            raise ValueError(f"window {part!r} is not within 00:00-24:00")
        windows.append([start, end])
    for (_, end), (start, _) in zip(windows, windows[1:]):
        if start < end:
            raise ValueError("windows must not overlap and must be in order")
    return windows


def format_windows(windows: list[list[int]]) -> str:
    return ", ".join(f"{s // 4:02d}:{s % 4 * 15:02d}-{e // 4:02d}:{e % 4 * 15:02d}" for s, e in windows) or "off"


def check_value(field: dict, value: str) -> str:
    """Validate a value against the field's range or options; return it in the form save.php takes."""
    if field["kind"] == "option":
        if value not in field.get("options", {}):
            raise ValueError(f"{field['label']}: {value!r} is not one of {field.get('options')}")
        return value
    number = float(value.replace(",", "."))
    low, high = (float(field[k].replace(",", ".")) if k in field else None for k in ("min", "max"))
    if (low is not None and number < low) or (high is not None and number > high):
        raise ValueError(f"{field['label']}: {number} is outside {field.get('min')}..{field.get('max')}")
    return value.replace(",", ".")


def version_mismatch(reference: dict[str, str], current: dict[str, str]) -> list[str]:
    """Human-readable differences between two version maps; empty when they match."""
    return [f"{key}: reference {reference.get(key, '-')}, device {current.get(key, '-')}"
            for key in sorted(set(reference) | set(current)) if reference.get(key) != current.get(key)]


# --- page list -----------------------------------------------------------------------------------

def pages_versions() -> dict[str, str] | None:
    """The versions pages.txt was made with (header line "# versions {...}")."""
    for line in PAGES_FILE.read_text().splitlines():
        if line.startswith("# versions "):
            return json.loads(line[len("# versions "):])
    return None


def settings_pages() -> list[str]:
    return [line.split("\t")[0] for line in PAGES_FILE.read_text().splitlines()
            if line.startswith("?s=") and "[val" in line]


def crawl() -> list[str]:
    """Walk the ISG menu; return the lines of pages.txt (page id, name, field labels with ids)."""
    def links(html):
        return [(m.group(1), re.sub(r"<[^>]+>|\s+", " ", m.group(2)).strip())
                for m in re.finditer(r'href="[^"]*(\?s=[0-9,]+)"[^>]*>(.*?)</a>', html, re.S)]
    seen, names, queue, lines = set(), {}, ["?s=1,0"], []
    while queue:
        page = queue.pop(0)
        if page in seen or page == "?s=0":
            continue
        seen.add(page)
        html = fetch(page)[0]
        for link, name in links(html):
            names.setdefault(link, name)
            if link not in seen and link.count(",") <= 2:
                queue.append(link)
        fields = " | ".join(f"{f['label']} [{vid}]" for vid, f in parse_settings(html).items())
        lines.append(f"{page}\t{names.get(page, '?')}\t{fields}")
    lines.sort(key=lambda line: [int(x) for x in line.split("\t")[0][3:].split(",")])
    return lines


# --- guarded writes ------------------------------------------------------------------------------

def device_versions() -> dict[str, str]:
    return parse_versions(fetch(VERSION_PAGE)[0])


def require_versions(reference: dict[str, str] | None, allow_change: bool, what: str = "pages.txt") -> None:
    """Raise VersionMismatch unless the device runs the reference versions (or allow_change)."""
    if reference is None:
        if not allow_change:
            raise VersionMismatch(f"{what} has no version information")
        return
    mismatch = version_mismatch(reference, device_versions())
    if mismatch and not allow_change:
        raise VersionMismatch("software versions differ from " + what + ":\n  " + "\n  ".join(mismatch))


def write_settings(page: str, values: dict[str, str], allow_version_change: bool = False) -> dict:
    """Write fields of a settings page after checking versions, field ids, ranges and options."""
    require_versions(pages_versions(), allow_version_change)
    fields = parse_settings(fetch(page)[0])
    data = []
    for vid, value in values.items():
        if vid not in fields:
            raise KeyError(f"{vid} is not a settings field of {page}")
        data.append({"name": vid, "value": check_value(fields[vid], str(value))})
    return post(page, data)


def write_time_program(page: str, day: str, windows: list[list[int]], allow_version_change: bool = False) -> dict:
    """Set the windows of one day ("Monday".."Sunday") or of all days ("all")."""
    require_versions(pages_versions(), allow_version_change)
    program = parse_time_program(fetch(page)[0])
    if len(program) != 7:
        raise RuntimeError(f"{page} is not a time program page")
    days = DAYS if day.lower() == "all" else [day.capitalize()]
    data = []
    for name in days:
        if name not in program:
            raise KeyError(f"no day {name!r} on {page}")
        data += time_program_fields({**program[name], "windows": windows})
    return post(page, data)


# --- command line --------------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    allow = "--allow-version-change" in argv
    args = [a for a in argv if a != "--allow-version-change"]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    command, rest = args[0], args[1:]
    try:
        if command == "get":
            fields = parse_settings(fetch(rest[0])[0])
            for vid, field in fields.items():
                if len(rest) == 1 or vid in rest[1:]:
                    extra = (f" ({field['min']}..{field['max']})" if "min" in field
                             else f" {field.get('options', '')}")
                    print(f"{vid}\t{field['label']}\t{field['value']}{extra}")
        elif command == "set":
            values = dict(item.split("=", 1) for item in rest[1:])
            print(write_settings(rest[0], values, allow))
            for vid, field in parse_settings(fetch(rest[0])[0]).items():
                if vid in values:
                    print(f"{vid}\t{field['label']}\tnow {field['value']}")
        elif command == "program":
            for day, entry in parse_time_program(fetch(rest[0])[0]).items():
                print(f"{day}\t{format_windows(entry['windows'])}")
        elif command == "set-program":
            print(write_time_program(rest[0], rest[1], parse_windows(rest[2]), allow))
            for day, entry in parse_time_program(fetch(rest[0])[0]).items():
                print(f"{day}\t{format_windows(entry['windows'])}")
        elif command == "versions":
            print(json.dumps(device_versions(), indent=1))
        elif command == "crawl":
            versions = device_versions()
            lines = crawl()
            PAGES_FILE.write_text(f"# versions {json.dumps(versions, sort_keys=True)}\n" + "\n".join(lines) + "\n")
            print(f"{len(lines)} pages -> {PAGES_FILE}")
        else:
            print(__doc__, file=sys.stderr)
            return 1
    except (VersionMismatch, ValueError, KeyError) as err:
        print(f"refused: {err}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
