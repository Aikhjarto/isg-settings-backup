#!/usr/bin/env python3
"""Back up and restore the settings of a Stiebel Eltron ISG / WPM through the ISG web interface.

Commands:
  backup                      read everything and store it in DATA_DIR/isg-settings.json (git commit per change)
  diff [FILE]                 compare the device with the backup (read only)
  restore [FILE] [--apply] [--pages ?s=4,2,1 ...] [--allow-version-change]
                              without --apply only print what would be written
  versions                    print the software versions the ISG reports
  crawl                       rebuild the page list (pages.txt) from the ISG menu

Restore refuses when the software versions the ISG reports (ISG, WPM, WPE, heat pump, FES, FET) differ
from those stored with the backup: after a firmware update, field ids and meanings can change.
--allow-version-change overrides that after checking the pages by hand.

Options (environment): ISG_HOST (default servicewelt.private.lan), ISG_BACKUP_DIR (default ~/isg-backup).
The ISG web is slow (~10 s per page); a full backup takes several minutes.
"""
from __future__ import annotations

import datetime
import http.cookiejar
import json
import os
import re
import socket
import struct
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HOST = os.environ.get("ISG_HOST", "servicewelt.private.lan")
DATA_DIR = Path(os.environ.get("ISG_BACKUP_DIR", Path.home() / "isg-backup"))
BACKUP_FILE = DATA_DIR / "isg-settings.json"
PAGES_FILE = Path(__file__).with_name("pages.txt")
TIMEOUT = 60
FORMAT = 2

# Never written back: clock, summer time switch dates, holiday dates.
NEVER_RESTORE = {"?s=4,0", "?s=4,0,0", "?s=4,0,1", "?s=4,1", "?s=4,1,0", "?s=4,1,1", "?s=3,13", "?s=3,13,0", "?s=3,13,1"}
TIME_PROGRAMS = ["?s=3,0,0", "?s=3,0,1", "?s=3,0,2", "?s=3,0,3", "?s=3,0,4", "?s=3,1,0", "?s=3,1,1", "?s=3,1,2",
                 "?s=3,1,3", "?s=3,2", "?s=3,4", "?s=3,5", "?s=3,6", "?s=3,7"]
VERSION_PAGE = "?s=2,7"
# Modbus holding registers (1-based) stored for reference: operating mode + HK1-3/DHW parameters,
# SG Ready, SG Ready load temperatures.
HOLDING = [(1501, 20), (4001, 3), (4272, 7)]
PROGRAM_MODE = 2


# --- device access -------------------------------------------------------------------------------

def modbus_read(function: int, register: int, count: int = 1) -> list[int]:
    """Read registers; function 3 = holding, 4 = input; register 1-based (manual numbering)."""
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


def fetch(page: str) -> tuple[str, urllib.request.OpenerDirector]:
    """Return the page HTML and an opener that keeps its session cookie."""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    return opener.open(f"http://{HOST}/{page}", timeout=TIMEOUT).read().decode("latin-1"), opener


def post(page: str, data: list[dict[str, str]]) -> dict:
    """Save fields like the web form: POST save.php data=[{"name": "valNNNN", "value": "x.y"}]."""
    _, opener = fetch(page)
    body = urllib.parse.urlencode({"data": json.dumps(data)}).encode()
    return json.loads(opener.open(f"http://{HOST}/save.php", data=body, timeout=TIMEOUT).read().decode("latin-1"))


# --- parsing (pure, tested) ----------------------------------------------------------------------

def page_text(html: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text).replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text)


def parse_versions(html: str) -> dict[str, str]:
    """Software versions from INFO > DIAGNOSIS > SYSTEM (?s=2,7), e.g. {"SOFTWARE 449": "19.0.5", "ISG": "1.6.6.0"}."""
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
    """PARAMETERSET of the heat pump: stored for information, not compared (read 1 and 0 within an hour)."""
    match = re.search(r"PARAMETERSET (\d+)", page_text(html))
    return match.group(1) if match else None


def parse_settings(html: str) -> dict[str, dict[str, str]]:
    """Every settings field: {"valNNNN": {"label", "value", "kind": number|option}}."""
    fields = {}
    for vid, block in re.findall(r'id="calval(\d+)"(.*?)(?=id="calval|\Z)', html, re.S):
        title = re.search(r'<h3 class="title">(.*?)</h3>', block, re.S)
        value = re.findall(r"jsvalues\['\d+'\]\['val'\]='([^']*)'", block)
        option = re.findall(r'<input[^>]*type="radio"[^>]*value="([^"]*)"[^>]*checked', block)
        if not value and not option:
            continue
        fields[f"val{vid}"] = {"label": re.sub(r"\s+", " ", title.group(1)).strip() if title else "",
                               "value": (value or option)[0], "kind": "number" if value else "option"}
    return fields


def parse_time_program(html: str) -> dict[str, dict]:
    """Per day: field ids of the three slots, the windows in quarter hours (0-96) and the off value."""
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
    slots = [f"{start};{end}" for start, end in entry["windows"]]
    slots += [f"{entry['off']};{entry['off']}"] * (len(entry["ids"]) - len(slots))
    return [{"name": vid, "value": slots[n]} for n, vid in enumerate(entry["ids"])]


def version_mismatch(saved: dict[str, str], current: dict[str, str]) -> list[str]:
    """Human-readable differences between two version maps; empty when they match."""
    return [f"{key}: backup {saved.get(key, '-')}, device {current.get(key, '-')}"
            for key in sorted(set(saved) | set(current)) if saved.get(key) != current.get(key)]


# --- commands ------------------------------------------------------------------------------------

def pages() -> list[str]:
    return [line.split("\t")[0] for line in PAGES_FILE.read_text().splitlines() if line.startswith("?s=") and "[val" in line]


def take() -> dict:
    system_page = fetch(VERSION_PAGE)[0]
    data = {"format": FORMAT, "host": HOST, "taken": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "versions": parse_versions(system_page), "parameter_set": parse_parameter_set(system_page),
            "settings": {}, "time_programs": {}, "holding": {}}
    for page in pages():
        data["settings"][page] = parse_settings(fetch(page)[0])
    for page in TIME_PROGRAMS:
        data["time_programs"][page] = parse_time_program(fetch(page)[0])
    for start, count in HOLDING:
        for offset, raw in enumerate(modbus_read(3, start, count)):
            data["holding"][str(start + offset)] = raw
    return data


def backup() -> int:
    data = take()
    mode = data["holding"].get("1501")
    if mode != PROGRAM_MODE:
        # A WPM that lost its settings comes back in standby with defaults; never overwrite the good copy with that.
        print(f"operating mode (holding 1501) is {mode}, not {PROGRAM_MODE} = program: backup not stored", file=sys.stderr)
        return 2
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stable = {k: v for k, v in data.items() if k != "taken"}
    old = json.loads(BACKUP_FILE.read_text()) if BACKUP_FILE.exists() else {}
    if {k: v for k, v in old.items() if k != "taken"} == stable:
        print("unchanged since", old.get("taken"))
        return 0
    BACKUP_FILE.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    if not (DATA_DIR / ".git").is_dir():
        subprocess.run(["git", "init", "-q", str(DATA_DIR)], check=True)
    subprocess.run(["git", "-C", str(DATA_DIR), "add", BACKUP_FILE.name], check=True)
    subprocess.run(["git", "-C", str(DATA_DIR), "commit", "-q", "-m", f"ISG settings {data['taken']}"], check=True)
    print("saved", BACKUP_FILE, data["taken"])
    return 0


def differences(saved: dict, only_pages: list[str] | None = None):
    """Yield (page, kind, field, saved entry, current value) for every difference."""
    for page, fields in saved["settings"].items():
        if only_pages and page not in only_pages:
            continue
        now = parse_settings(fetch(page)[0])
        for vid, entry in fields.items():
            if now.get(vid, {}).get("value") != entry["value"]:
                yield page, "setting", vid, entry, now.get(vid, {}).get("value")
    for page, days in saved["time_programs"].items():
        if only_pages and page not in only_pages:
            continue
        now = parse_time_program(fetch(page)[0])
        for day, entry in days.items():
            if now.get(day, {}).get("windows") != entry["windows"]:
                yield page, "time", day, entry, now.get(day, {}).get("windows")


def check_versions(saved: dict, allow_change: bool) -> bool:
    current = parse_versions(fetch(VERSION_PAGE)[0])
    if "versions" not in saved:
        print("backup has no version information (taken before the version check existed)", file=sys.stderr)
        return allow_change
    mismatch = version_mismatch(saved["versions"], current)
    if not mismatch:
        print("software versions match the backup")
        return True
    print("software versions differ from the backup:", *mismatch, sep="\n  ", file=sys.stderr)
    if allow_change:
        print("--allow-version-change given: continuing", file=sys.stderr)
        return True
    print("restore refused: check the pages by hand, then use --allow-version-change", file=sys.stderr)
    return False


def restore(saved: dict, apply: bool, only_pages: list[str], allow_version_change: bool) -> int:
    if not check_versions(saved, allow_version_change):
        return 3
    writes: dict[str, list[dict[str, str]]] = {}
    for page, kind, field, entry, current in differences(saved, only_pages):
        if page in NEVER_RESTORE:
            continue
        if kind == "setting":
            print(f"{page} {field} {entry['label']}: device {current} -> backup {entry['value']}")
            writes.setdefault(page, []).append({"name": field, "value": entry["value"].replace(",", ".")})
        else:
            print(f"{page} {field}: device {current} -> backup {entry['windows']}")
            writes.setdefault(page, []).extend(time_program_fields(entry))
    if not writes:
        print("nothing to restore")
    elif not apply:
        print("dry run: add --apply to write")
    else:
        for page, data in writes.items():
            print(page, post(page, data))
    mode = modbus_read(3, 1501)[0]
    if mode != saved["holding"].get("1501"):
        print(f"operating mode (holding 1501) is {mode}, backup {saved['holding'].get('1501')}: "
              "set it in the ISG or Home Assistant (not restored automatically)")
    return 0


def crawl() -> int:
    """Walk the ISG menu and write pages.txt: page id, name, field labels with ids."""
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
    PAGES_FILE.write_text("\n".join(lines) + "\n")
    print(f"{len(lines)} pages -> {PAGES_FILE}")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    command, rest = argv[0], argv[1:]
    files = [a for a in rest if not a.startswith("--") and not a.startswith("?s=")]
    only_pages = [a for a in rest if a.startswith("?s=")]
    saved_file = Path(files[0]) if files else BACKUP_FILE
    if command == "backup":
        return backup()
    if command == "versions":
        print(json.dumps(parse_versions(fetch(VERSION_PAGE)[0]), indent=1))
        return 0
    if command == "crawl":
        return crawl()
    saved = json.loads(saved_file.read_text())
    if command == "diff":
        for page, kind, field, entry, current in differences(saved, only_pages):
            print(page, field, entry.get("label", ""), "device", current, "backup", entry.get("value", entry.get("windows")))
        return 0
    if command == "restore":
        return restore(saved, "--apply" in rest, only_pages, "--allow-version-change" in rest)
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
