#!/usr/bin/env python3
"""Back up and restore the settings of a Stiebel Eltron ISG / WPM through the ISG web interface.

Commands:
  backup                      read everything and store it in DATA_DIR/isg-settings.json (git commit per change)
  diff [FILE]                 compare the device with the backup (read only)
  restore [FILE] [--apply] [?s=4,2,1 ...] [--allow-version-change]
                              without --apply only print what would be written

Restore refuses when the software versions the ISG reports (ISG, WPM, WPE, heat pump, FES, FET) differ
from those stored with the backup: after a firmware update, field ids and meanings can change.
--allow-version-change overrides that after checking the pages by hand.

Reading and writing single settings: isg_web.py.
Environment: ISG_HOST (default servicewelt.private.lan), ISG_BACKUP_DIR (default ~/isg-backup).
The ISG web is slow (~10 s per page); a full backup takes several minutes.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python -I (cron) does not add the script directory
import isg_web  # noqa: E402
from isg_web import fetch, modbus_read, parse_settings, parse_time_program, post, time_program_fields  # noqa: E402

DATA_DIR = Path(os.environ.get("ISG_BACKUP_DIR", Path.home() / "isg-backup"))
BACKUP_FILE = DATA_DIR / "isg-settings.json"
FORMAT = 2

# Never written back: clock, summer time switch dates, holiday dates.
NEVER_RESTORE = {"?s=4,0", "?s=4,0,0", "?s=4,0,1", "?s=4,1", "?s=4,1,0", "?s=4,1,1", "?s=3,13", "?s=3,13,0", "?s=3,13,1"}
TIME_PROGRAMS = ["?s=3,0,0", "?s=3,0,1", "?s=3,0,2", "?s=3,0,3", "?s=3,0,4", "?s=3,1,0", "?s=3,1,1", "?s=3,1,2",
                 "?s=3,1,3", "?s=3,2", "?s=3,4", "?s=3,5", "?s=3,6", "?s=3,7"]
# Modbus holding registers (1-based) stored for reference: operating mode + HK1-3/DHW parameters,
# SG Ready, SG Ready load temperatures.
HOLDING = [(1501, 20), (4001, 3), (4272, 7)]
PROGRAM_MODE = 2


def take() -> dict:
    system_page = fetch(isg_web.VERSION_PAGE)[0]
    data = {"format": FORMAT, "host": isg_web.HOST,
            "taken": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "versions": isg_web.parse_versions(system_page), "parameter_set": isg_web.parse_parameter_set(system_page),
            "settings": {}, "time_programs": {}, "holding": {}}
    for page in isg_web.settings_pages():
        fields = parse_settings(fetch(page)[0])
        data["settings"][page] = {vid: {k: f[k] for k in ("label", "value", "kind")} for vid, f in fields.items()}
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
    try:
        isg_web.require_versions(saved.get("versions"), allow_change, "the backup")
    except isg_web.VersionMismatch as err:
        print(f"{err}\nrestore refused: check the pages by hand, then use --allow-version-change", file=sys.stderr)
        return False
    print("software versions checked against the backup" + (" (override given)" if allow_change else ""))
    return True


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
