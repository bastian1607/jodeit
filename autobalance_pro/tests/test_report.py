"""AB-020 AC-06 / AC-020-11: measuring report per job and coverage since job start."""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402
from test_ab014 import Plant, config, local  # noqa: E402

START = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = server.DB
        server.DB = os.path.join(self.tmp.name, "r.db")
        server.setup()
        with sqlite3.connect(server.DB) as c:
            c.execute("DELETE FROM config_history")
        cfg = config()
        cfg["building"]["entities"]["outdoor"] = "sensor.out"
        cfg["circuits"][0]["entities"]["flow"] = "sensor.vl"
        with patch.object(server, "utcnow", return_value=START - timedelta(hours=1)):
            server.save_v2(cfg)
        with patch.object(server, "utcnow", return_value=START):
            self.job = server.start_session({"name": "Pilot & Anlage \"1\"", "confirmed": True})

    def tearDown(self):
        server.DB = self.old_db
        self.tmp.cleanup()

    def store(self, plant):
        with sqlite3.connect(server.DB) as c:
            for t in plant.ticks:
                for eid in ("sensor.out", "sensor.vl", "sensor.wz", "binary_sensor.f_wz"):
                    x = plant.values[t][eid]
                    c.execute("INSERT INTO samples(at,entity_id,state,unit,valid,quality,normalized_value) VALUES(?,?,?,?,?,?,?)",
                              (t.isoformat(), eid, x["s"], "", 1, "ok", x["v"]))

    def test_ac06_insufficient_after_seven_days_with_outage(self):
        p = Plant(days=7)
        p.drop(local(2, 0), local(6, 12))                   # long outage: only one usable night remains
        self.store(p)
        with patch.object(server, "utcnow", return_value=p.end):
            server.stop_session(self.job["id"])
        html = server.session_report(self.job["id"], now=p.end + timedelta(hours=1))
        self.assertIn("Messung unzureichend", html)
        self.assertIn("Messdauer verlängern oder Anlage manuell prüfen", html)
        self.assertIn("Messlücken über 15 Minuten", html)
        self.assertIn("Dieser Zeitraum wurde nicht ersetzt", html)
        self.assertIn("Nicht überwachte Räume:</b> Bad", html)
        self.assertIn("Pilot &amp; Anlage &quot;1&quot;", html)          # escaped
        self.assertIn("beendet", html)
        for forbidden in ("Offset auf", "Empfehlung: ", "alle Räume im Zielbereich"):
            self.assertNotIn(forbidden, html)

    def test_running_job_with_level_result_is_provisional(self):
        p = Plant(days=4)
        self.store(p)
        html = server.session_report(self.job["id"], now=p.end)
        self.assertIn("Niveau im Messbereich prüfbar", html)
        self.assertIn("läuft noch – Ergebnis vorläufig", html)
        self.assertIn("Steigung und Frostverhalten nicht nachgewiesen", html)
        self.assertIn("Keine Messlücken über 15 Minuten", html)

    def test_job_without_data_reports_whole_window_as_gap(self):
        end = START + timedelta(hours=3)
        html = server.session_report(self.job["id"], now=end)
        self.assertIn("Keine Messdaten im Messauftrag", html)
        self.assertIn("(180 min) – andauernd", html)

    def test_endpoints_and_csv_limited_to_job(self):
        p = Plant(days=2)
        self.store(p)
        with sqlite3.connect(server.DB) as c:          # sample before the job must not be exported
            c.execute("INSERT INTO samples(at,entity_id,state,unit,valid,quality,normalized_value) VALUES(?,?,?,?,?,?,?)",
                      ((START - timedelta(days=1)).isoformat(), "sensor.wz", "19", "°C", 1, "ok", 19))
        with patch.object(server, "utcnow", return_value=p.end):
            server.stop_session(self.job["id"])
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.API)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{httpd.server_port}/api/v2/sessions/{self.job['id']}"
            with urlopen(base + "/report") as r:
                self.assertEqual(r.headers["Content-Type"], "text/html; charset=utf-8")
                self.assertIn("Messbericht", r.read().decode())
            with urlopen(base + "/export.csv") as r:
                text = r.read().decode("utf-8-sig")
            self.assertNotIn((START - timedelta(days=1)).isoformat(), text)
            self.assertIn(START.isoformat(), text)
            with self.assertRaises(Exception):
                urlopen(f"http://127.0.0.1:{httpd.server_port}/api/v2/sessions/ma_x/report")
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
