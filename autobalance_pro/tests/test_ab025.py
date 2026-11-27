"""AB-025: measurement data, device profile and report belong to exactly one measuring job."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import devices  # noqa: E402
import model  # noqa: E402
import server  # noqa: E402

T0 = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
PROFILE = {"profile": "buderus-emsesp-hmc310", "source": "ha", "emsesp_host": ""}


def customer(room_name, sensor, with_profile=True):
    cfg = model.empty()
    cfg["rooms"].append({"id": "r_" + sensor.split(".")[1], "name": room_name, "target": 21, "area": None, "default_circuit_id": "hk1",
                         "heat_load": model.empty_heat_load(), "design_indoor_c": None, "design_indoor_reference": "",
                         "temperature_sensors": [sensor], "window_sensors": []})
    if with_profile:
        cfg["building"]["heat_source"] = dict(PROFILE)
    return cfg


def offset_id():
    return next(sid for sid, f in devices.entities(PROFILE).items() if f["key"] == "curve_offset")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = server.DB
        server.DB = os.path.join(self.tmp.name, "a.db")
        server.setup()
        with sqlite3.connect(server.DB) as c:
            c.execute("DELETE FROM config_history")

    def tearDown(self):
        server.DB = self.old_db
        self.tmp.cleanup()

    def at(self, t):
        return patch.object(server, "utcnow", return_value=t)

    def put(self, rows, job=None):
        with sqlite3.connect(server.DB) as c:
            c.executemany("INSERT INTO samples(at,entity_id,state,unit,valid,quality,normalized_value,session_id) VALUES(?,?,?,?,?,?,?,?)",
                          [(t.isoformat(), eid, str(v), "", 1, "ok", v, job) for t, eid, v in rows])

    def job(self, name, cfg, start, hours=24, offset=None):
        with self.at(start - timedelta(minutes=30)):
            server.save_v2(cfg)
        with self.at(start):
            s = server.start_session({"name": name, "confirmed": True})
        rows = [(start + timedelta(minutes=5 * i), cfg["rooms"][0]["temperature_sensors"][0], 20.5) for i in range(hours * 12)]
        if offset is not None:
            rows.append((start + timedelta(minutes=10), offset_id(), offset))
        self.put(rows, s["id"])
        return s


class IsolationTests(Base):
    def test_ac6_06_second_customer_never_sees_first_customers_values(self):
        a = self.job("Kunde A", customer("Wohnzimmer", "sensor.a"), T0, offset=-2.0)
        with self.at(T0 + timedelta(hours=24)):
            server.stop_session(a["id"])
        report_a = server.session_report(a["id"])
        self.assertIn("-2.0", report_a)
        # Customer B: other rooms, same profile, but no offset recorded during B's job
        b = self.job("Kunde B", customer("Küche", "sensor.b"), T0 + timedelta(days=3))
        report_b = server.session_report(b["id"], now=T0 + timedelta(days=4))
        self.assertNotIn("-2.0", report_b)
        self.assertIn("im Auftrag nicht gemessen", report_b)
        self.assertNotIn("Wohnzimmer", report_b)
        self.assertNotIn("Kunde A", report_b)
        # A's report is frozen: byte-identical after B exists and the configuration changed
        self.assertEqual(server.session_report(a["id"]), report_a)
        self.assertNotIn("Küche", report_a)
        csv_b = server.session_csv(b["id"])
        self.assertNotIn("sensor.a", csv_b)
        self.assertIn("sensor.b", csv_b)

    def test_samples_outside_the_job_are_ignored(self):
        cfg = customer("Wohnzimmer", "sensor.a", with_profile=False)
        self.put([(T0 - timedelta(hours=1), "sensor.a", 30.0)])        # passive, before the job
        s = self.job("Kunde A", cfg, T0, hours=2)
        r = server.session_assessment(s["id"], now=T0 + timedelta(hours=2))
        self.assertNotIn("30", server.session_csv(s["id"]).split("sensor.a;")[1][:5])
        self.assertEqual(r["window"]["start"], T0.isoformat())
        self.assertGreater(r["coverage"][0]["share"], 0.9)

    def test_ac6_05_start_snapshot_survives_configuration_change(self):
        s = self.job("Kunde A", customer("Wohnzimmer", "sensor.a"), T0, hours=1)
        with self.at(T0 + timedelta(minutes=30)):
            server.save_v2(customer("Bad", "sensor.x"))              # someone edits during the job
        history = server.session_history(s["id"], (T0 + timedelta(hours=1)).isoformat())
        self.assertEqual(history[0][1]["rooms"][0]["name"], "Wohnzimmer")
        self.assertEqual(history[-1][1]["rooms"][0]["name"], "Bad")
        with self.at(T0 + timedelta(hours=1)):
            server.stop_session(s["id"])
        with self.at(T0 + timedelta(days=2)):
            server.save_v2(customer("Garage", "sensor.y"))           # next customer
        frozen = server.session_history(s["id"])
        self.assertEqual([h[1]["rooms"][0]["name"] for h in frozen], ["Wohnzimmer", "Bad"])

    def test_ac6_09_stop_freezes_report_and_assessment(self):
        s = self.job("Kunde A", customer("Wohnzimmer", "sensor.a", with_profile=False), T0, hours=3)
        with self.at(T0 + timedelta(hours=3)):
            server.stop_session(s["id"])
        stored = server.get_session(s["id"])
        self.assertTrue(stored["finalized"])
        html = server.session_report(s["id"])
        self.assertIn("unverändert gespeichert", html)
        self.put([(T0 + timedelta(hours=2), "sensor.a", 99.0)], s["id"])  # late write must not alter the result
        self.assertEqual(server.session_report(s["id"]), html)
        self.assertEqual(server.session_assessment(s["id"])["window"]["end"], (T0 + timedelta(hours=3)).isoformat())

    def test_acquisition_tags_samples_with_active_job(self):
        cfg = customer("Wohnzimmer", "sensor.a", with_profile=False)
        ent = [{"entity_id": "sensor.a", "state": "21", "unit": "°C", "source_at": T0.isoformat()}]
        with self.at(T0 - timedelta(hours=1)):
            server.save_v2(cfg)
        with patch.object(server, "get_entities", return_value=ent):
            with self.at(T0 - timedelta(minutes=5)):
                server.sample()
            with self.at(T0):
                s = server.start_session({"name": "Kunde A", "confirmed": True})
                server.sample()
            with self.at(T0 + timedelta(minutes=5)):
                server.stop_session(s["id"])
                server.sample()
        with sqlite3.connect(server.DB) as c:
            tags = [r[0] for r in c.execute("SELECT session_id FROM samples ORDER BY at")]
        self.assertEqual(tags, [None, s["id"], None])


class MigrationTests(Base):
    def test_v040_jobs_are_tagged_and_finalized_on_start(self):
        with self.at(T0 - timedelta(hours=1)):
            server.save_v2(customer("Wohnzimmer", "sensor.a", with_profile=False))
        with sqlite3.connect(server.DB) as c:      # state as written by v0.4.0
            c.execute("INSERT INTO sessions(id,name,started_at,ended_at,planned_days,rooms,note) VALUES(?,?,?,?,?,?,?)",
                      ("ma_old", "Alt", T0.isoformat(), (T0 + timedelta(hours=2)).isoformat(), 7, "[]", ""))
            c.execute("INSERT INTO sessions(id,name,started_at,ended_at,planned_days,rooms,note) VALUES(?,?,?,?,?,?,?)",
                      ("ma_run", "Läuft", (T0 + timedelta(hours=5)).isoformat(), None, 7, "[]", ""))
        self.put([(T0 + timedelta(hours=1), "sensor.a", 20.0), (T0 + timedelta(hours=3), "sensor.a", 20.0),
                  (T0 + timedelta(hours=6), "sensor.a", 20.0)])
        with self.at(T0 + timedelta(hours=7)):
            server.setup()
            server.setup()                          # idempotent
        with sqlite3.connect(server.DB) as c:
            tags = [r[0] for r in c.execute("SELECT session_id FROM samples ORDER BY at")]
            snap = c.execute("SELECT config_start FROM sessions WHERE id='ma_run'").fetchone()[0]
        self.assertEqual(tags, ["ma_old", None, "ma_run"])
        self.assertTrue(server.get_session("ma_old")["finalized"])
        self.assertFalse(server.get_session("ma_run")["finalized"])
        self.assertEqual(json.loads(snap)["rooms"][0]["name"], "Wohnzimmer")


if __name__ == "__main__":
    unittest.main()
