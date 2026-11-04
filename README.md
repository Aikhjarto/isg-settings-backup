# isg-settings-backup

Backs up and restores the settings of a Stiebel Eltron heat pump controller (WPMsystem) through its ISG web interface. The ISG web pages expose more than Modbus does, for example maximum flow temperatures, room influence, summer mode and the time programs.

It exists because a power cut on 2026-11-03 reset the WPM's own settings to factory defaults: standby mode, comfort/eco/curve, maximum flow, room influence, time programs and hot water settings. The WPE extension module (HK4/HK5) kept its settings.

## Usage

```
isg_settings_backup.py backup     # store everything in ~/isg-backup/isg-settings.json (git commit when it changed)
isg_settings_backup.py diff       # compare the device with the backup, read only
isg_settings_backup.py restore    # dry run: list what would be written
isg_settings_backup.py restore --apply [?s=4,2,1 ...]   # write (all pages, or only those listed)
isg_settings_backup.py versions   # software versions the ISG reports
isg_settings_backup.py crawl      # rebuild pages.txt from the ISG menu
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

## Tests

`python3 -I -m unittest discover -s tests` runs offline on captured pages (session token redacted).
