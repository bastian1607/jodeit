"""AB-020: measurement gaps, coverage and monitoring endpoint (AC-020-01 … 03, 05, 07, 08, 09)."""
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import coverage  # noqa: E402
import model  # noqa: E402
import server  # noqa: E402

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


def ticks(start, end, step_min=5):
    out, t = [], start
    while t <= end:
        out.append(t)
        t += timedelta(minutes=step_min)
    return out


class PureTests(unittest.TestCase):
    def test_status_thresholds(self):
        self.assertEqual(coverage.status_for_age(None), "no_data")
        self.assertEqual(coverage.status_for_age(300), "ok")
        self.assertEqual(coverage.status_for_age(700), "delayed")
        self.assertEqual(coverage.status_for_age(20 * 60), "warning")
        self.assertEqual(coverage.status_for_age(70 * 60), "critical")

    def test_20_minute_gap_is_found_and_not_ongoing(self):  # AC-020-01
        t = ticks(NOW - timedelta(hours=2), NOW - timedelta(minutes=60)) + ticks(NOW - timedelta(minutes=40), NOW)
        gaps = coverage.find_gaps(t, NOW, NOW - timedelta(hours=24))
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["minutes"], 20)
        self.assertFalse(gaps[0]["ongoing"])

    def test_regular_ticks_and_short_jitter_are_no_gap(self):
        t = ticks(NOW - timedelta(hours=3), NOW) + [NOW - timedelta(minutes=7)]
        self.assertEqual(coverage.find_gaps(sorted(t), NOW, NOW - timedelta(hours=24)), [])

    def test_70_minute_outage_is_critical_and_ongoing(self):  # AC-020-02
        t = ticks(NOW - timedelta(hours=3), NOW - timedelta(minutes=70))
        report = coverage.analyse([x.isoformat() for x in t], [], (NOW - timedelta(minutes=1)).isoformat(), NOW, 24)
        self.assertEqual(report["status"], "critical")
        self.assertTrue(report["gaps"][-1]["ongoing"])
        self.assertFalse(report["loop_stalled"])

    def test_gap_spanning_window_start_is_clipped_and_counted(self):
        t = [NOW - timedelta(hours=30)] + ticks(NOW - timedelta(hours=2), NOW)
        report = coverage.analyse([x.isoformat() for x in t], [], NOW.isoformat(), NOW, 24)
        self.assertEqual(report["gaps"][0]["start"], (NOW - timedelta(hours=24)).isoformat())
        self.assertLess(report["coverage"], 0.1)  # 2 of 24 hours measured

    def test_late_sensor_has_no_retroactive_gap(self):  # AC-020-07
        late = NOW - timedelta(hours=1)
        rows = [("sensor.early", (NOW - timedelta(hours=24)).isoformat(), 289, 289),
                ("sensor.late", late.isoformat(), 13, 13)]
        t = [x.isoformat() for x in ticks(NOW - timedelta(hours=24), NOW)]
        report = coverage.analyse(t, rows, NOW.isoformat(), NOW, 24)
        cov = {e["entity_id"]: e["coverage"] for e in report["entities"]}
        self.assertEqual(cov["sensor.late"], 1.0)
        self.assertEqual(cov["sensor.early"], 1.0)

    def test_no_interpolation_fields(self):  # AC-020-05: report only lists gaps, never values
        report = coverage.analyse([], [], None, NOW, 24)
        self.assertEqual(report["status"], "no_data")
        self.assertTrue(report["loop_stalled"])
        self.assertNotIn("values", json.dumps(report))


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = server.DB
        server.DB = str(Path(self.tmp.name) / "dq.db")
        server.setup()
        cfg = model.empty()
        cfg["rooms"].append({"id": "wz", "name": "Wohnzimmer", "target": 21, "area": None, "default_circuit_id": "hk1",
                             "temperature_sensors": ["sensor.wz", "sensor.wz2"], "window_sensors": [],
                             "heat_load": model.empty_heat_load()})
        server.save_v2(cfg)

    def tearDown(self):
        server.DB = self.old_db
        self.tmp.cleanup()

    def insert(self, entity, times, valid=1):
        with server.connect() as c:
            c.executemany("INSERT INTO samples(at,entity_id,state,unit,valid,quality) VALUES(?,?,?,?,?,?)",
                          [(t.isoformat(), entity, "21", "°C", valid, "ok" if valid else "unavailable") for t in times])
            c.execute("INSERT INTO acquisition(id,last_attempt,last_success,connected,error) VALUES(1,?,?,1,NULL) "
                      "ON CONFLICT(id) DO UPDATE SET last_attempt=excluded.last_attempt", (times[-1].isoformat(), times[-1].isoformat()))

    def test_gap_survives_restart(self):  # AC-020-01 / 08
        t = ticks(NOW - timedelta(hours=3), NOW - timedelta(hours=2)) + ticks(NOW - timedelta(minutes=30), NOW)
        self.insert("sensor.wz", t)
        first = server.data_quality(24, NOW)
        server.setup()  # simulated restart: schema setup again, data untouched
        second = server.data_quality(24, NOW)
        self.assertEqual(first["gaps"], second["gaps"])
        self.assertEqual(len(second["gaps"]), 1)
        self.assertEqual(second["gaps"][0]["minutes"], 90)
        self.assertEqual(server.load_v2()["rooms"][0]["name"], "Wohnzimmer")

    def test_failed_sensor_does_not_hide_other(self):  # AC-020-03
        t = ticks(NOW - timedelta(hours=1), NOW)
        self.insert("sensor.wz", t)
        self.insert("sensor.wz2", t, valid=0)
        ent = {e["entity_id"]: e for e in server.data_quality(24, NOW)["entities"]}
        self.assertEqual(ent["sensor.wz"]["valid_coverage"], 1.0)
        self.assertEqual(ent["sensor.wz2"]["valid_coverage"], 0.0)
        self.assertEqual(ent["sensor.wz2"]["coverage"], 1.0)

    def test_unassigned_entities_are_ignored_and_missing_listed(self):
        self.insert("sensor.alt", ticks(NOW - timedelta(hours=1), NOW))
        report = server.data_quality(24, NOW)
        self.assertNotIn("sensor.alt", [e["entity_id"] for e in report["entities"]])
        self.assertEqual(report["entities_without_samples"], ["sensor.wz", "sensor.wz2"])

    def test_endpoints(self):
        self.insert("sensor.wz", ticks(datetime.now(timezone.utc) - timedelta(hours=1), datetime.now(timezone.utc)))
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.API)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_port}"
        try:
            dq = json.load(urlopen(base + "/api/v2/data-quality?hours=24"))
            self.assertEqual(dq["status"], "ok")
            health = json.load(urlopen(base + "/health"))
            self.assertEqual(health["acquisition"]["status"], "ok")
            self.assertTrue(health["read_only"])
            self.assertEqual(urlopen(base + "/health/acquisition").status, 200)
            with self.assertRaises(HTTPError) as err:
                urlopen(base + "/api/v2/data-quality?hours=0")
            self.assertEqual(err.exception.code, 400)
            with server.connect() as c:  # own loop stalled for 2 h
                c.execute("UPDATE acquisition SET last_attempt=?", ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),))
            with self.assertRaises(HTTPError) as err:
                urlopen(base + "/health/acquisition")
            self.assertEqual(err.exception.code, 503)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_start_event_logged_and_no_writes_to_ha(self):  # AC-020-09
        server.log_event("start", "test")
        with patch.object(server.urllib.request, "urlopen") as net:
            report = server.data_quality(24, NOW)
            server.acquisition_health()
        net.assert_not_called()
        with server.connect() as c:
            self.assertEqual(c.execute("SELECT kind FROM app_events").fetchall(), [("start",)])


if __name__ == "__main__":
    unittest.main()
