"""Offline tests on ISG pages captured from a WPMsystem (ISG 1.6.6.0, session token redacted)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import isg_settings_backup as backup  # noqa: E402
import isg_web  # noqa: E402

VERSIONS = {"ISG": "1.6.6.0", "software 449": "19.0.5"}


def page(name: str) -> str:
    return (HERE / name).read_text(encoding="latin-1")


class ParseTests(unittest.TestCase):
    def test_versions(self):
        versions = isg_web.parse_versions(page("page_2_7.html"))
        self.assertEqual(versions["software 449"], "19.0.5")
        self.assertEqual(versions["software 503"], "5.0.0")
        self.assertEqual(versions["software 450"], "4.0.4")
        self.assertEqual(versions["FET 5 software 501"], "4.0.5")
        self.assertEqual(versions["ISG"], "1.6.6.0")
        self.assertEqual(versions["HP-ID"], "271")
        self.assertNotIn("PARAMETERSET", versions)
        self.assertEqual(isg_web.parse_parameter_set(page("page_2_7.html")), "0")

    def test_versions_refuse_a_page_without_them(self):
        with self.assertRaises(ValueError):
            isg_web.parse_versions(page("page_4_2_2.html"))

    def test_settings_numbers_with_range(self):
        fields = isg_web.parse_settings(page("page_4_2_2.html"))
        self.assertEqual(fields["val10986"], {"label": "COMFORT TEMPERATURE", "value": "16,0", "kind": "number",
                                              "min": "5", "max": "30"})
        self.assertEqual(fields["val10991"]["value"], "0,40")
        self.assertEqual((fields["val10989"]["min"], fields["val10989"]["max"]), ("20", "90"))

    def test_settings_options(self):
        field = isg_web.parse_settings(page("page_4_3_1.html"))["val375"]
        self.assertEqual((field["label"], field["value"], field["kind"]), ("DHW MODE", "1", "option"))
        self.assertEqual(field["options"], {"0": "PRIORITY OPERATION", "1": "PARALLEL OPERATION", "2": "PARTIAL PRIORITY"})

    def test_time_program(self):
        days = isg_web.parse_time_program(page("page_3_0_0.html"))
        self.assertEqual(len(days), 7)
        self.assertEqual(days["Monday"], {"ids": ["val130", "val131", "val132"], "windows": [[40, 80]], "off": 128})

    def test_time_program_fields_fill_unused_slots(self):
        entry = {"ids": ["val130", "val131", "val132"], "windows": [[40, 80]], "off": 128}
        self.assertEqual(isg_web.time_program_fields(entry), [
            {"name": "val130", "value": "40;80"},
            {"name": "val131", "value": "128;128"},
            {"name": "val132", "value": "128;128"},
        ])

    def test_time_program_fields_refuse_too_many_windows(self):
        with self.assertRaises(ValueError):
            isg_web.time_program_fields({"ids": ["a"], "windows": [[0, 4], [8, 12]], "off": 128})


class WindowTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(isg_web.parse_windows("10:00-20:00"), [[40, 80]])
        self.assertEqual(isg_web.parse_windows("00:00-06:15, 22:00-24:00"), [[0, 25], [88, 96]])

    def test_refuse(self):
        for text in ("10:10-12:00", "20:00-10:00", "10:00-25:00", "08:00-12:00,11:00-13:00", "8-12"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                isg_web.parse_windows(text)

    def test_format(self):
        self.assertEqual(isg_web.format_windows([[40, 80], [88, 96]]), "10:00-20:00, 22:00-24:00")
        self.assertEqual(isg_web.format_windows([]), "off")


class CheckValueTests(unittest.TestCase):
    fields = isg_web.parse_settings(page("page_4_2_2.html"))
    options = isg_web.parse_settings(page("page_4_3_1.html"))

    def test_number_in_range(self):
        self.assertEqual(isg_web.check_value(self.fields["val10986"], "21,5"), "21.5")

    def test_number_out_of_range(self):
        for value in ("4", "31"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                isg_web.check_value(self.fields["val10986"], value)

    def test_option(self):
        self.assertEqual(isg_web.check_value(self.options["val375"], "0"), "0")
        with self.assertRaises(ValueError):
            isg_web.check_value(self.options["val375"], "3")


class GuardedWriteTests(unittest.TestCase):
    def patched(self, device_versions, reference=VERSIONS, html="page_4_2_2.html"):
        return mock.patch.multiple(
            isg_web,
            device_versions=mock.Mock(return_value=device_versions),
            pages_versions=mock.Mock(return_value=reference),
            fetch=mock.Mock(return_value=(page(html), None)),
            post=mock.Mock(return_value={"success": True}),
        )

    def test_write_after_checks(self):
        with self.patched(VERSIONS):
            isg_web.write_settings("?s=4,2,2", {"val10992": "30"})
            isg_web.post.assert_called_once_with("?s=4,2,2", [{"name": "val10992", "value": "30"}])

    def test_version_change_refuses_write(self):
        with self.patched({**VERSIONS, "ISG": "1.6.7.0"}):
            with self.assertRaises(isg_web.VersionMismatch):
                isg_web.write_settings("?s=4,2,2", {"val10992": "30"})
            isg_web.post.assert_not_called()

    def test_version_change_override(self):
        with self.patched({**VERSIONS, "ISG": "1.6.7.0"}):
            isg_web.write_settings("?s=4,2,2", {"val10992": "30"}, allow_version_change=True)
            isg_web.post.assert_called_once()

    def test_unknown_field_refused(self):
        with self.patched(VERSIONS):
            with self.assertRaises(KeyError):
                isg_web.write_settings("?s=4,2,2", {"val99999": "30"})
            isg_web.post.assert_not_called()

    def test_out_of_range_refused(self):
        with self.patched(VERSIONS):
            with self.assertRaises(ValueError):
                isg_web.write_settings("?s=4,2,2", {"val10989": "95"})
            isg_web.post.assert_not_called()

    def test_time_program_all_days(self):
        with self.patched(VERSIONS, html="page_3_0_0.html"):
            isg_web.write_time_program("?s=3,0,0", "all", [[40, 80]])
            data = isg_web.post.call_args.args[1]
        self.assertEqual(len(data), 21)
        self.assertEqual(data[0], {"name": "val130", "value": "40;80"})
        self.assertEqual(data[1], {"name": "val131", "value": "128;128"})

    def test_time_program_one_day(self):
        with self.patched(VERSIONS, html="page_3_0_0.html"):
            isg_web.write_time_program("?s=3,0,0", "tuesday", [[0, 96]])
            data = isg_web.post.call_args.args[1]
        self.assertEqual([d["name"] for d in data], ["val133", "val134", "val135"])
        self.assertEqual(data[0]["value"], "0;96")


class RestoreVersionTests(unittest.TestCase):
    saved = {"versions": VERSIONS}

    def run_check(self, current, allow=False, saved=None):
        with mock.patch.object(isg_web, "device_versions", return_value=current):
            return backup.check_versions(self.saved if saved is None else saved, allow)

    def test_match(self):
        self.assertTrue(self.run_check(dict(VERSIONS)))

    def test_isg_update_refuses(self):
        self.assertFalse(self.run_check({**VERSIONS, "ISG": "1.6.7.0"}))

    def test_wpm_update_refuses(self):
        self.assertFalse(self.run_check({**VERSIONS, "software 449": "19.1.0"}))

    def test_new_module_refuses(self):
        self.assertFalse(self.run_check({**VERSIONS, "software 503": "5.0.0"}))

    def test_override(self):
        self.assertTrue(self.run_check({**VERSIONS, "ISG": "1.6.7.0"}, allow=True))

    def test_backup_without_versions_needs_override(self):
        self.assertFalse(self.run_check(dict(VERSIONS), saved={}))
        self.assertTrue(self.run_check(dict(VERSIONS), allow=True, saved={}))

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


class TakeTests(unittest.TestCase):
    def test_clock_is_not_backed_up(self):
        fetched = []

        def fake_fetch(page_id):
            fetched.append(page_id)
            return (page("page_2_7.html") if page_id == isg_web.VERSION_PAGE else "", None)

        with mock.patch.object(backup, "fetch", side_effect=fake_fetch), \
                mock.patch.object(backup, "modbus_read", side_effect=lambda f, r, n: [0] * n):
            data = backup.take()
        self.assertFalse(backup.NOT_BACKED_UP & set(fetched))
        self.assertFalse(backup.NOT_BACKED_UP & set(data["settings"]))
        self.assertIn("?s=4,2,1", data["settings"])


class PagesFileTests(unittest.TestCase):
    def test_pages_list_has_the_heating_circuits(self):
        listed = isg_web.settings_pages()
        for p in ("?s=4,2,0", "?s=4,2,1", "?s=4,2,2", "?s=4,2,3", "?s=4,2,4", "?s=4,3,1"):
            self.assertIn(p, listed)
        self.assertNotIn("?s=3,0,0", listed)  # time programs are handled separately

    def test_pages_file_records_its_versions(self):
        self.assertEqual(isg_web.pages_versions()["ISG"], "1.6.6.0")


if __name__ == "__main__":
    unittest.main()
