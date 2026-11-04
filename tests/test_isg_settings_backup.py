"""Offline tests on ISG pages captured from a WPMsystem (ISG 1.6.6.0, session token redacted)."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import isg_settings_backup as backup  # noqa: E402


def page(name: str) -> str:
    return (HERE / name).read_text(encoding="latin-1")


class ParseTests(unittest.TestCase):
    def test_versions(self):
        versions = backup.parse_versions(page("page_2_7.html"))
        self.assertEqual(versions["software 449"], "19.0.5")
        self.assertEqual(versions["software 503"], "5.0.0")
        self.assertEqual(versions["software 450"], "4.0.4")
        self.assertEqual(versions["FET 5 software 501"], "4.0.5")
        self.assertEqual(versions["ISG"], "1.6.6.0")
        self.assertEqual(versions["HP-ID"], "271")
        self.assertNotIn("PARAMETERSET", versions)

    def test_versions_refuse_a_page_without_them(self):
        with self.assertRaises(ValueError):
            backup.parse_versions(page("page_4_2_2.html"))

    def test_settings_numbers(self):
        fields = backup.parse_settings(page("page_4_2_2.html"))
        self.assertEqual(fields["val10986"], {"label": "COMFORT TEMPERATURE", "value": "16,0", "kind": "number"})
        self.assertEqual(fields["val10991"]["value"], "0,40")

    def test_settings_options(self):
        fields = backup.parse_settings(page("page_4_3_1.html"))
        self.assertEqual(fields["val375"], {"label": "DHW MODE", "value": "1", "kind": "option"})

    def test_time_program(self):
        days = backup.parse_time_program(page("page_3_0_0.html"))
        self.assertEqual(len(days), 7)
        self.assertEqual(days["Monday"], {"ids": ["val130", "val131", "val132"], "windows": [[40, 80]], "off": 128})

    def test_time_program_fields_fill_unused_slots(self):
        entry = {"ids": ["val130", "val131", "val132"], "windows": [[40, 80]], "off": 128}
        self.assertEqual(backup.time_program_fields(entry), [
            {"name": "val130", "value": "40;80"},
            {"name": "val131", "value": "128;128"},
            {"name": "val132", "value": "128;128"},
        ])


class VersionCheckTests(unittest.TestCase):
    saved = {"versions": {"ISG": "1.6.6.0", "software 449": "19.0.5"}}

    def run_check(self, current, allow=False):
        with mock.patch.object(backup, "fetch", return_value=("", None)), \
                mock.patch.object(backup, "parse_versions", return_value=current):
            return backup.check_versions(self.saved, allow)

    def test_match(self):
        self.assertTrue(self.run_check({"ISG": "1.6.6.0", "software 449": "19.0.5"}))

    def test_isg_update_refuses(self):
        self.assertFalse(self.run_check({"ISG": "1.6.7.0", "software 449": "19.0.5"}))

    def test_wpm_update_refuses(self):
        self.assertFalse(self.run_check({"ISG": "1.6.6.0", "software 449": "19.1.0"}))

    def test_new_module_refuses(self):
        self.assertFalse(self.run_check({"ISG": "1.6.6.0", "software 449": "19.0.5", "software 503": "5.0.0"}))

    def test_override(self):
        self.assertTrue(self.run_check({"ISG": "1.6.7.0", "software 449": "19.0.5"}, allow=True))

    def test_backup_without_versions_needs_override(self):
        with mock.patch.object(backup, "fetch", return_value=("", None)), \
                mock.patch.object(backup, "parse_versions", return_value={"ISG": "1.6.6.0"}):
            self.assertFalse(backup.check_versions({}, False))
            self.assertTrue(backup.check_versions({}, True))

    def test_restore_writes_nothing_on_mismatch(self):
        with mock.patch.object(backup, "check_versions", return_value=False), \
                mock.patch.object(backup, "post") as post, \
                mock.patch.object(backup, "differences") as differences:
            self.assertEqual(backup.restore({"holding": {}}, True, [], False), 3)
        post.assert_not_called()
        differences.assert_not_called()


class BackupGuardTests(unittest.TestCase):
    def test_no_backup_in_standby(self):
        data = {"holding": {"1501": 1}, "taken": "x"}
        with mock.patch.object(backup, "take", return_value=data), \
                mock.patch.object(backup, "BACKUP_FILE") as file:
            self.assertEqual(backup.backup(), 2)
        file.write_text.assert_not_called()


class PagesFileTests(unittest.TestCase):
    def test_pages_list_has_the_heating_circuits(self):
        listed = backup.pages()
        for p in ("?s=4,2,0", "?s=4,2,1", "?s=4,2,2", "?s=4,2,3", "?s=4,2,4", "?s=4,3,1"):
            self.assertIn(p, listed)
        self.assertNotIn("?s=3,0,0", listed)  # time programs are handled separately
        json.dumps(listed)


if __name__ == "__main__":
    unittest.main()
