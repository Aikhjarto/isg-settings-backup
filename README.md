# isg-settings-backup

Reads, writes, backs up and restores the settings of a Stiebel Eltron heat pump controller (WPMsystem) through its ISG web interface. The ISG web pages expose more than Modbus does, for example maximum flow temperatures, room influence, summer mode and the time programs.

- `isg_web.py`: read and write single settings and time programs (module and command line).
- `isg_settings_backup.py`: back up all settings and restore them.
- `pages.txt`: the ISG's pages with their field ids, as found on the installation below.

It exists because a power cut on 2026-11-03 reset the WPM's own settings to factory defaults: standby mode, comfort/eco/curve, maximum flow, room influence, time programs and hot water settings. The WPE extension module (HK4/HK5) kept its settings.

## Reading and writing settings

```
isg_web.py get ?s=4,2,1                         # all fields of SETTINGS > HEATING > HEATING CIRCUIT 2
isg_web.py get ?s=4,2,1 val10984                # one field, with its allowed range
isg_web.py set ?s=4,2,1 val10984=30             # room influence HC2 = 30 %
isg_web.py program ?s=3,0,0                     # heating program HC1
isg_web.py set-program ?s=3,0,0 all 10:00-20:00
isg_web.py set-program ?s=3,2 saturday 07:00-09:00,17:00-20:00
isg_web.py versions                             # software versions the ISG reports
isg_web.py crawl                                # rebuild pages.txt
```

Before writing, `isg_web.py` checks:

- **Versions:** the device must run the software versions `pages.txt` was made with (its first line). After a firmware update, field ids and meanings can change. Check the pages and run `crawl`, or pass `--allow-version-change`.
- **Field:** the field id must exist on that page.
- **Value:** a number must be within the range the page declares. An option must be one of the page's choices.
- **Time windows:** 15-minute steps, at most three windows per day, in order and not overlapping.

It writes the way the web form does: `POST save.php` with `data=[{"name": "valNNNN", "value": "x.y"}]`. This path is **not documented by Stiebel Eltron** and can change with any ISG update. The ISG web interface has **no login** by default, so use these tools only inside your own network.

Page ids: `?s=4,2,0` to `?s=4,2,4` are the heating circuits 1 to 5, `?s=4,3,x` hot water, `?s=3,0,0` to `?s=3,0,4` the heating programs, `?s=3,2` the hot water program. `pages.txt` lists all pages with their fields.

## Backup and restore

```
isg_settings_backup.py backup     # store everything in ~/isg-backup/isg-settings.json (git commit when it changed)
isg_settings_backup.py diff       # compare the device with the backup, read only
isg_settings_backup.py restore    # dry run: list what would be written
isg_settings_backup.py restore --apply [?s=4,2,1 ...]   # write (all pages, or only those listed)
```

Environment: `ISG_HOST` (default `servicewelt.private.lan`), `ISG_BACKUP_DIR` (default `~/isg-backup`).
The ISG web interface is slow (about 10 s per page), so a full backup or diff takes several minutes.

## What is stored

- Every field on the settings pages listed in `pages.txt` (`<div id="calvalNNNN">`): numbers as shown (`22,0`) and selected options.
- The time programs (heating HC1-5, cooling, DHW, circulation, pool, silent programs): per day the windows in quarter hours (0-96).
- Modbus holding registers 1501-1520, 4001-4003 and 4272-4278 for reference. Restore does not write them; the operating mode is only reported.
- The software versions from INFO > DIAGNOSIS > SYSTEM (`?s=2,7`), and the heat pump parameter set for information.

## Safety

- **Version check:** restore compares the stored versions of the ISG, WPM (449), WPE (503), heat pump (450, HP-TYPE, HP-ID), FES (502) and FET room units (501) with the device. If any of them differs, or a module appeared or disappeared, nothing is written. After a firmware update, field ids and meanings may have changed. Check the pages by hand first; then `--allow-version-change` overrides the check. A backup taken before the version check existed also needs this flag.
- **No backup of a reset WPM:** `backup` refuses to store anything when the WPM is not in program mode (holding 1501 != 2). A WPM that lost its settings comes back in standby, so its defaults cannot overwrite the good copy.
- **Never restored:** the clock, the summer time dates and the holiday program.
- Restore writes only the fields that differ, the way the web form does (`POST save.php`, `data=[{"name": "valNNNN", "value": "x.y"}]`).

## After a power cut

1. `isg_settings_backup.py diff` shows what was lost.
2. `isg_settings_backup.py restore` lists what would be written. Check the list.
3. `isg_settings_backup.py restore --apply` writes it.
4. Set the operating mode back to program if it is reported as different.

## Tested on

| Component | Version |
|---|---|
| Heat pump | WPL-A 13.2 Plus HK 400, software 450: 4.0.4 |
| Controller | WPMsystem, identification 449, software 19.0.5 |
| Extension module | WPE (heating circuits 4 and 5), software 503: 5.0.0 |
| Room units | FET, software 501: 4.0.5 |
| Gateway | ISG WEB 1.6.6.0 |

## Tests

`python3 -I -m unittest discover -s tests` runs offline on captured pages (session token redacted).
