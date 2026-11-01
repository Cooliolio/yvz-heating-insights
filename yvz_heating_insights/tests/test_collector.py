import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import requests  # noqa: E402
import collector as c  # noqa: E402

COLS = [("temp", "sensor.t", None), ("pump", "binary_sensor.p", None),
        ("mode", "input_select.m", None), ("wtemp", "weather.w", "temperature")]
STATES = {
    "sensor.t": {"entity_id": "sensor.t", "state": "20.5", "attributes": {}},
    "binary_sensor.p": {"entity_id": "binary_sensor.p", "state": "on", "attributes": {}},
    "input_select.m": {"entity_id": "input_select.m", "state": "Comfort, day", "attributes": {}},
    "weather.w": {"entity_id": "weather.w", "state": "cloudy", "attributes": {"temperature": 7.0}},
}


def ts(*a):
    return datetime(*a, tzinfo=timezone.utc)


def epoch(*a):
    return ts(*a).timestamp()


class ConvertTests(unittest.TestCase):
    def test_numbers_as_is(self):
        self.assertEqual(c.convert_value("sensor.t", None, STATES["sensor.t"]), "20.5")
        self.assertEqual(c.convert_value("sensor.t", None, {"state": "0"}), "0")

    def test_on_off(self):
        self.assertEqual(c.convert_value("switch.x", None, {"state": "on"}), "1")
        self.assertEqual(c.convert_value("switch.x", None, {"state": "off"}), "0")
        self.assertEqual(c.convert_value("binary_sensor.x", None, {"state": "off"}), "0")

    def test_missing_is_empty_never_zero(self):
        for s in ("unavailable", "unknown", "None", "none", ""):
            self.assertEqual(c.convert_value("sensor.t", None, {"state": s}), "")
        self.assertEqual(c.convert_value("switch.x", None, {"state": "unavailable"}), "")
        self.assertEqual(c.convert_value("sensor.t", None, None), "")

    def test_text(self):
        self.assertEqual(c.convert_value("input_select.m", None, STATES["input_select.m"]), "Comfort, day")

    def test_attributes(self):
        self.assertEqual(c.convert_value("weather.w", "temperature", STATES["weather.w"]), "7.0")
        self.assertEqual(c.convert_value("weather.w", "wind_speed", STATES["weather.w"]), "")
        self.assertEqual(c.convert_value("climate.h", "temperature", {"state": "heat", "attributes": {"temperature": None}}), "")

    def test_row_and_quoting(self):
        row = c.build_row(COLS, STATES, ts(2026, 1, 5, 10, 5))
        self.assertEqual(row, ["2026-01-05T10:05Z", "20.5", "1", "Comfort, day", "7.0"])
        self.assertEqual(c.format_line(row), '2026-01-05T10:05Z,20.5,1,"Comfort, day",7.0\n')

    def test_missing_entity(self):
        row = c.build_row(COLS, {}, ts(2026, 1, 5, 10, 5))
        self.assertEqual(row[1:], ["", "", "", ""])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self._tmp.name, "data")
        self.store = c.Store(self.dir)
        self.store.ensure_dir()
        self.header = c.header_for(COLS)

    def schema(self):
        with open(os.path.join(self.dir, "schema_version")) as f:
            return f.read().strip()

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, when, header=None, cols=COLS):
        header = header or self.header
        return self.store.append(when, header, c.format_line(c.build_row(cols, STATES, when)))

    def lines(self, name):
        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
            return f.read().splitlines()

    def test_header_created_once(self):
        self.write(ts(2026, 1, 5, 10, 0))
        self.write(ts(2026, 1, 5, 10, 5))
        lines = self.lines("2026-01.csv")
        self.assertEqual(lines[0], "ts_utc,temp,pump,mode,wtemp")
        self.assertEqual(len(lines), 3)
        self.assertEqual(self.schema(), "1")

    def test_month_rollover_utc(self):
        self.write(ts(2026, 1, 31, 23, 55))
        self.write(ts(2026, 2, 1, 0, 0))
        self.assertEqual(len(self.lines("2026-01.csv")), 2)
        self.assertEqual(len(self.lines("2026-02.csv")), 2)
        self.assertTrue(self.lines("2026-02.csv")[1].startswith("2026-02-01T00:00Z"))

    def test_schema_change_rotates_to_v2_then_v3(self):
        self.write(ts(2026, 1, 5, 10, 0))
        before = self.lines("2026-01.csv")
        cols2 = COLS + [("extra", "sensor.e", None)]
        h2 = c.header_for(cols2)
        self.write(ts(2026, 1, 5, 10, 5), h2, cols2)
        self.write(ts(2026, 1, 5, 10, 10), h2, cols2)
        self.assertEqual(self.lines("2026-01.csv"), before)  # old file untouched
        self.assertEqual(len(self.lines("2026-01_v2.csv")), 3)
        self.assertEqual(self.schema(), "2")
        cols3 = cols2[:-1]
        self.write(ts(2026, 1, 5, 10, 15), c.header_for(cols3), cols3)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "2026-01_v3.csv")))

    def test_schema_change_across_month_boundary(self):
        self.write(ts(2026, 1, 31, 23, 55))
        cols2 = COLS[:2]
        self.write(ts(2026, 2, 1, 0, 0), c.header_for(cols2), cols2)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "2026-02_v2.csv")))
        self.write(ts(2026, 3, 1, 0, 0), c.header_for(cols2), cols2)  # same schema, new month
        self.assertTrue(os.path.exists(os.path.join(self.dir, "2026-03_v2.csv")))

    def test_partial_last_line_does_not_glue(self):
        p = self.write(ts(2026, 1, 5, 10, 0))
        with open(p, "ab") as f:
            f.write(b"2026-01-05T10:05Z,20")  # simulated crash mid-write
        self.write(ts(2026, 1, 5, 10, 10))
        self.assertTrue(self.lines("2026-01.csv")[-1].startswith("2026-01-05T10:10Z"))


class ApiTests(unittest.TestCase):
    def test_api_failure_skips_row(self):
        with tempfile.TemporaryDirectory() as d:
            store = c.Store(os.path.join(d, "hd"))
            store.ensure_dir()
            for exc in (requests.ConnectionError("down"), requests.Timeout("slow")):
                with mock.patch.object(c.requests, "get", side_effect=exc):
                    self.assertIsNone(c.collect_once(COLS, store, epoch(2026, 1, 5, 10, 0), "http://x", "t"))
            resp = mock.Mock()
            resp.raise_for_status.side_effect = requests.HTTPError("401")
            with mock.patch.object(c.requests, "get", return_value=resp):
                self.assertIsNone(c.collect_once(COLS, store, epoch(2026, 1, 5, 10, 0), "http://x", "t"))
            self.assertEqual(os.listdir(store.dir), [])  # nothing written, no crash

    def test_success_writes_row(self):
        with tempfile.TemporaryDirectory() as d:
            store = c.Store(os.path.join(d, "hd"))
            store.ensure_dir()
            resp = mock.Mock()
            resp.json.return_value = list(STATES.values())
            with mock.patch.object(c.requests, "get", return_value=resp) as g:
                line = c.collect_once(COLS, store, epoch(2026, 1, 5, 10, 0), "http://x/api", "tok")
            self.assertEqual(g.call_args.kwargs["headers"], {"Authorization": "Bearer tok"})
            self.assertEqual(g.call_args.args[0], "http://x/api/states")
            self.assertEqual(line, '2026-01-05T10:00Z,20.5,1,"Comfort, day",7.0\n')


class TickTests(unittest.TestCase):
    def test_alignment(self):
        self.assertEqual(c.next_tick(epoch(2026, 1, 5, 10, 0, 0)), epoch(2026, 1, 5, 10, 5))
        self.assertEqual(c.next_tick(epoch(2026, 1, 5, 10, 2, 31)), epoch(2026, 1, 5, 10, 5))
        self.assertEqual(c.next_tick(epoch(2026, 1, 5, 10, 4, 59)), epoch(2026, 1, 5, 10, 5))
        self.assertEqual(c.next_tick(epoch(2026, 1, 31, 23, 58)), epoch(2026, 2, 1, 0, 0))

    def test_truncated_to_minute_utc(self):
        self.assertEqual(c.tick_datetime(epoch(2026, 1, 5, 10, 5) + 0.4), ts(2026, 1, 5, 10, 5))


HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "options.fake.json")


def valid(cols=None):
    cols = cols or [("a", "sensor.fake_a"), ("b", "weather.fake_w|temperature")]
    return {"columns": [{"name": n, "entity": e} for n, e in cols]}


class OptionsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.data = os.path.join(self.dir, "data")

    def tearDown(self):
        self._tmp.cleanup()

    def options_file(self, content):
        path = os.path.join(self.dir, "options.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content if isinstance(content, str) else json.dumps(content))
        return path

    def run_main(self, path, *extra):
        return c.main(["--options", path, "--data-dir", self.data, "--once",
                       "--api-url", "http://127.0.0.1:9/api", *extra])

    def test_valid_list_order_and_attribute_preserved(self):
        path = self.options_file(valid([("z", "sensor.fake_a"), ("a", "weather.fake_w|temperature"),
                                        ("m", "switch.fake_relay")]))
        self.assertEqual(c.load_columns(path), [
            ("z", "sensor.fake_a", None), ("a", "weather.fake_w", "temperature"),
            ("m", "switch.fake_relay", None)])

    def test_shipped_fixture_loads(self):
        cols = c.load_columns(FIXTURE)
        self.assertEqual(cols[0], ("temp_a", "sensor.fake_a", None))
        self.assertIn(("weather_temp", "weather.fake_w", "temperature"), cols)

    def test_invalid_options_exit_nonzero_and_write_nothing(self):
        dup = valid([("a", "sensor.fake_a"), ("a", "sensor.fake_b")])
        cases = {
            "missing file": os.path.join(self.dir, "nope.json"),
            "invalid json": self.options_file("{not json"),
            "not an object": self.options_file("[]"),
            "no columns key": self.options_file({}),
            "empty list": self.options_file({"columns": []}),
            "duplicate": self.options_file(dup),
            "bad name caps": self.options_file(valid([("Temp", "sensor.fake_a")])),
            "bad name digit": self.options_file(valid([("1a", "sensor.fake_a")])),
            "bad name space": self.options_file(valid([("a b", "sensor.fake_a")])),
            "bad entity": self.options_file(valid([("a", "fake_a")])),
            "bad entity attr": self.options_file(valid([("a", "sensor.fake_a|")])),
            "entity not str": self.options_file({"columns": [{"name": "a", "entity": 5}]}),
            "reserved": self.options_file(valid([("ts_utc", "sensor.fake_a")])),
        }
        for label, path in cases.items():
            with self.subTest(label), self.assertLogs("collector", "ERROR"):
                self.assertEqual(self.run_main(path), 1)
                self.assertFalse(os.path.exists(self.data))

    def test_exit_code_is_nonzero_for_real_process(self):
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "..", "app", "collector.py"),
             "--options", os.path.join(self.dir, "nope.json"), "--data-dir", self.data],
            capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("cannot read options file", proc.stderr)
        self.assertFalse(os.path.exists(self.data))

    def test_startup_log_has_no_entity_ids(self):
        path = self.options_file(valid())
        with self.assertLogs("collector", "INFO") as cm:
            with mock.patch.object(c.requests, "get", side_effect=requests.ConnectionError("down")):
                self.run_main(path)
        text = "\n".join(cm.output)
        self.assertIn("3 columns", text)  # ts_utc + 2
        self.assertIn(self.data, text)
        for needle in ("fake_a", "fake_w", "temperature", "sensor."):
            self.assertNotIn(needle, text)

    def test_error_messages_do_not_leak_entity_ids(self):
        path = self.options_file(valid([("a", "not-an-entity-xyz")]))
        with self.assertLogs("collector", "ERROR") as cm:
            self.run_main(path)
        self.assertNotIn("not-an-entity-xyz", "\n".join(cm.output))

    def test_options_change_rotates_csv_and_keeps_old_file(self):
        resp = mock.Mock()
        resp.json.return_value = list(STATES.values())
        first = self.options_file(valid([("temp", "sensor.t"), ("pump", "binary_sensor.p")]))
        with mock.patch.object(c.requests, "get", return_value=resp):
            self.assertEqual(self.run_main(first), 0)
            old = [n for n in os.listdir(self.data) if n.endswith(".csv")]
            self.assertEqual(len(old), 1)
            with open(os.path.join(self.data, old[0]), encoding="utf-8") as f:
                before = f.read()
            second = self.options_file(valid([("temp", "sensor.t"), ("pump", "binary_sensor.p"),
                                              ("mode", "input_select.m")]))
            self.assertEqual(self.run_main(second), 0)
        files = sorted(n for n in os.listdir(self.data) if n.endswith(".csv"))
        self.assertEqual(len(files), 2)
        self.assertRegex(files[1], r"_v2\.csv$")
        with open(os.path.join(self.data, old[0]), encoding="utf-8") as f:
            self.assertEqual(f.read(), before)
        with open(os.path.join(self.data, files[1]), encoding="utf-8") as f:
            self.assertEqual(f.readline().strip(), "ts_utc,temp,pump,mode")
        with open(os.path.join(self.data, "schema_version")) as f:
            self.assertEqual(f.read().strip(), "2")


if __name__ == "__main__":
    unittest.main()
