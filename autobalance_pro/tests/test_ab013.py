"""AB-013: rooms without sensors, design indoor temperature, monitoring status, measuring jobs."""
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
import model  # noqa: E402
import monitoring  # noqa: E402
import server  # noqa: E402
from test_model_v2 import adopted, v1_config  # noqa: E402


def obs(entity, value=None, quality="ok", valid=True, state=None, stale=False):
    return {"entity_id": entity, "normalized_value": value, "quality": quality, "valid": valid,
            "state": state if state is not None else ("" if value is None else str(value)), "stale": stale}


def room(rid="bad", sensors=(), windows=(), target=23):
    return {"id": rid, "name": rid.capitalize(), "target": target, "area": None, "default_circuit_id": "hk1",
            "heat_load": model.empty_heat_load(), "design_indoor_c": None, "design_indoor_reference": "",
            "temperature_sensors": list(sensors), "window_sensors": list(windows)}


class ModelTests(unittest.TestCase):
    def test_ac01_room_without_sensor_is_valid(self):
        cfg = model.empty()
        cfg["rooms"].append(room())
        model.validate(cfg)

    def test_config_before_ab013_without_design_fields_is_valid(self):
        cfg = model.from_v1(v1_config())
        for r in cfg["rooms"]:
            r.pop("design_indoor_c"); r.pop("design_indoor_reference")
        model.validate(cfg)

    def test_design_indoor_needs_reference_and_range(self):
        cfg = model.empty()
        cfg["rooms"].append(room())
        cfg["rooms"][0]["design_indoor_c"] = 24
        with self.assertRaises(model.Invalid):
            model.validate(cfg)
        cfg["rooms"][0]["design_indoor_reference"] = "HottCAD 07.07.2026"
        model.validate(cfg)
        cfg["rooms"][0]["design_indoor_c"] = 40
        with self.assertRaises(model.Invalid):
            model.validate(cfg)

    def test_ac017_design_kept_when_legacy_ui_saves(self):
        cfg = model.from_v1(v1_config())
        cfg["rooms"][0].update(design_indoor_c=20, design_indoor_reference="Plan")
        merged = model.merge_v1(cfg, model.to_v1(cfg))
        self.assertEqual(merged["rooms"][0]["design_indoor_c"], 20)

    def test_ac017_comfort_target_change_does_not_touch_design(self):
        cfg = model.from_v1(v1_config())
        cfg["rooms"][0].update(design_indoor_c=20, design_indoor_reference="Plan")
        v1 = model.to_v1(cfg)
        v1["rooms"][0]["target"] = 22.5
        merged = model.merge_v1(cfg, v1)
        self.assertEqual((merged["rooms"][0]["target"], merged["rooms"][0]["design_indoor_c"]), (22.5, 20))


class StatusTests(unittest.TestCase):
    def test_ac04_no_sensor_no_temperature(self):
        s = monitoring.room_status(room(), {})
        self.assertEqual((s["status"], s["temperature"], s["deviation_k"], s["monitored"]), ("not_monitored", None, None, False))

    def test_ac05_states_are_distinguished(self):
        r = room(sensors=["sensor.a"])
        self.assertEqual(monitoring.room_status(r, {})["status"], "waiting")
        self.assertEqual(monitoring.room_status(r, {"sensor.a": obs("sensor.a", quality="unavailable", valid=False)})["status"], "sensor_unavailable")
        self.assertEqual(monitoring.room_status(r, {"sensor.a": obs("sensor.a", 99, quality="implausible", valid=False)})["status"], "check_value")
        stale = monitoring.room_status(r, {"sensor.a": obs("sensor.a", 21, quality="ok", valid=False, stale=True)})
        self.assertEqual((stale["status"], stale["temperature"]), ("stale", None))
        ok = monitoring.room_status(r, {"sensor.a": obs("sensor.a", 22.5)})
        self.assertEqual((ok["status"], ok["temperature"], ok["deviation_k"]), ("monitored", 22.5, -0.5))

    def test_disagreeing_sensors_are_not_averaged(self):
        r = room(sensors=["sensor.a", "sensor.b"])
        s = monitoring.room_status(r, {"sensor.a": obs("sensor.a", 20.0), "sensor.b": obs("sensor.b", 22.0)})
        self.assertEqual((s["status"], s["temperature"]), ("disagree", None))
        self.assertEqual([x["value"] for x in s["sensors"]], [20.0, 22.0])
        close = monitoring.room_status(r, {"sensor.a": obs("sensor.a", 21.0), "sensor.b": obs("sensor.b", 21.6)})
        self.assertEqual(close["temperature"], 21.3)

    def test_ac07_critical_room_only_from_valid_measurements(self):
        statuses = [monitoring.room_status(room("a", ["sensor.a"], target=21), {"sensor.a": obs("sensor.a", 20.0)}),
                    monitoring.room_status(room("b", ["sensor.b"], target=21), {"sensor.b": obs("sensor.b", quality="unavailable", valid=False)}),
                    monitoring.room_status(room("c", target=25), {})]
        self.assertEqual(monitoring.critical_room(statuses)["id"], "a")
        self.assertIsNone(monitoring.critical_room(statuses[1:]))

    def test_ac06_coverage_names_unmonitored_rooms_and_partial_zones(self):
        cfg = model.from_v1(v1_config())
        cfg["rooms"].append(room("schlafen", target=19))
        cfg["rooms"][1]["temperature_sensors"] = []          # Küche in shared zone without sensor
        statuses = [monitoring.room_status(r, {}) for r in cfg["rooms"]]
        summary = monitoring.coverage_summary(cfg, statuses)
        self.assertEqual(summary["rooms"], 4)
        self.assertEqual(summary["rooms_with_sensor"], 2)
        self.assertEqual(summary["rooms_without_sensor"], ["Küche", "Schlafen"])
        self.assertEqual((summary["zones"], summary["zones_with_sensor"]), (3, 2))
        self.assertEqual(summary["zones_partial"], ["Wohnküche"])

    def test_ac014_hints_are_reasons_from_documented_data_only(self):
        cfg = model.from_v1(v1_config())
        hints = monitoring.reference_hints(cfg)
        self.assertTrue(any("Komfortziel" in h for h in hints["bad"]))
        self.assertEqual(hints["wohnen"], [])
        # Emitter output below documented heat load at the design point -> hint.
        bad = next(r for r in cfg["rooms"] if r["id"] == "bad")
        bad.update(heat_load=adopted(900), design_indoor_c=24, design_indoor_reference="Plan")
        cfg["circuits"][0].update(design_flow_c=45, design_return_c=38)
        e = next(e for e in cfg["emitters"] if e["room_id"] == "bad")
        e["rated_output"].update(value_w=500, flow_c=75, return_c=65, room_c=20)
        e["exponent"] = 1.3
        self.assertTrue(any("unter dokumentierter Heizlast" in h for h in monitoring.reference_hints(cfg)["bad"]))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = server.DB
        server.DB = os.path.join(self.tmp.name, "autobalance.db")
        server.setup()

    def tearDown(self):
        server.DB = self.old_db
        self.tmp.cleanup()

    def cfg(self):
        cfg = model.from_v1(v1_config())
        cfg["rooms"].append(room("schlafen", target=19))
        return cfg

    def test_ac01_ac11_saved_without_sensor_and_history_kept(self):
        cfg = self.cfg()
        server.save_v2(cfg)
        cfg["rooms"][0]["temperature_sensors"] = []
        server.save_v2(cfg)
        self.assertEqual(server.load_v2()["rooms"][0]["temperature_sensors"], [])
        history = server.config_history()
        self.assertEqual([h[1]["rooms"][0]["temperature_sensors"] for h in history[1:]],
                         [["sensor.wohnen_t", "sensor.kueche_t"], []])
        server.setup()      # restart keeps everything
        self.assertEqual(server.load_v2()["rooms"][-1]["name"], "Schlafen")

    def test_ac08_target_changes_are_logged_design_unchanged(self):
        cfg = self.cfg()
        server.save_v2(cfg)
        cfg["rooms"][0]["target"] = 22
        server.save_v2(cfg)
        changes = server.target_changes()
        self.assertEqual(len(changes), 1)
        self.assertEqual((changes[0]["room_id"], changes[0]["target_from"], changes[0]["target_to"]), ("wohnen", 21, 22))
        self.assertIsNone(changes[0]["design_to"])

    def test_baseline_for_existing_installations_starts_at_first_sample(self):
        tmp = tempfile.TemporaryDirectory()
        server.DB = os.path.join(tmp.name, "old.db")
        try:
            with sqlite3.connect(server.DB) as c:
                c.execute("CREATE TABLE samples (id INTEGER PRIMARY KEY, at TEXT, entity_id TEXT, state TEXT, unit TEXT, valid INTEGER)")
                c.execute("INSERT INTO samples(at,entity_id,state,unit,valid) VALUES('2026-11-20T10:00:00+00:00','sensor.x','20','°C',1)")
            server.setup()
            server.setup()
            history = server.config_history()
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0][0], "2026-11-20T10:00:00+00:00")
        finally:
            server.DB = os.path.join(self.tmp.name, "autobalance.db")
            tmp.cleanup()

    def test_sessions_start_stop_and_rules(self):
        with self.assertRaises(ValueError):          # no room with sensor yet
            server.start_session({"name": "Test", "confirmed": True})
        server.save_v2(self.cfg())
        with self.assertRaises(ValueError):
            server.start_session({"name": "Test"})   # selection not confirmed
        with self.assertRaises(ValueError):
            server.start_session({"name": "", "confirmed": True})
        with self.assertRaises(ValueError):
            server.start_session({"name": "x", "planned_days": 0, "confirmed": True})
        s = server.start_session({"name": "Pilot Musterhaus", "confirmed": True})
        self.assertEqual(s["planned_days"], 7)
        self.assertEqual([r["name"] for r in s["rooms"] if not r["sensors"]], ["Schlafen"])
        with self.assertRaises(ValueError):
            server.start_session({"name": "Zweiter", "confirmed": True})
        self.assertEqual(server.list_sessions()["active"]["id"], s["id"])
        stopped = server.stop_session(s["id"])
        self.assertIsNotNone(stopped["ended_at"])
        self.assertIsNone(server.list_sessions()["active"])
        with self.assertRaises(ValueError):
            server.stop_session(s["id"])
        # A sensor change after the job does not rewrite the job's room snapshot (AC-013-11).
        cfg = server.load_v2(); cfg["rooms"][0]["temperature_sensors"] = []; server.save_v2(cfg)
        self.assertEqual(server.get_session(s["id"])["rooms"][0]["sensors"], ["sensor.wohnen_t", "sensor.kueche_t"])

    def test_rooms_status_endpoint_data(self):
        server.save_v2(self.cfg())
        result = server.rooms_status()
        self.assertEqual(result["coverage"]["rooms_without_sensor"], ["Schlafen"])
        self.assertEqual(next(r for r in result["rooms"] if r["id"] == "schlafen")["status"], "not_monitored")

    def test_ac13_no_write_path(self):
        server.save_v2(self.cfg())
        with patch("urllib.request.urlopen", side_effect=AssertionError("no network")):
            s = server.start_session({"name": "Pilot", "confirmed": True})
            server.stop_session(s["id"])
            server.rooms_status()


if __name__ == "__main__":
    unittest.main()
