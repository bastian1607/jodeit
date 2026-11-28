"""AB-032: derived °C values of the device profile (damped outdoor temperature, flow target) as numbers.

Found on Bastian's plant (28.11.2026): sensor.thermostat_dampedoutdoortemp was stored without a numeric value,
so no night was technically evaluable and the report's measured points stayed empty.
"""
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import devices  # noqa: E402
import model  # noqa: E402
import server  # noqa: E402

PROFILE = "buderus-emsesp-hmc310"
DAMPED = "sensor.thermostat_dampedoutdoortemp"
TARGET = "sensor.thermostat_hc1_targetflowtemp"
NOW = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)


class QualityTests(unittest.TestCase):
    def test_derived_value_is_numeric_without_age_check(self):
        e = {"entity_id": DAMPED, "state": "-0.1", "unit": "°C", "source_at": None}
        self.assertEqual(server.quality(e, False, NOW, True), ("ok", -0.1, "°C"))
        old = dict(e, source_at=(NOW - timedelta(days=3)).isoformat())
        self.assertEqual(server.quality(old, False, NOW, True)[0], "ok")            # reports on change only
        self.assertEqual(server.quality(dict(e, state="abc"), False, NOW, True)[0], "invalid_number")
        self.assertEqual(server.quality(dict(e, unit="%"), False, NOW, True)[0], "invalid_unit")
        self.assertEqual(server.quality(dict(e, state="unavailable"), False, NOW, True)[0], "unavailable")
        self.assertEqual(server.quality(e, False, NOW), ("ok", None, None))        # old call unchanged

    def test_temperature_still_needs_timestamp(self):
        e = {"entity_id": "sensor.wz", "state": "21", "unit": "°C", "source_at": None}
        self.assertEqual(server.quality(e, True, NOW, True)[0], "missing_timestamp")

    def test_legacy_number(self):
        self.assertEqual(server.legacy_number("-0.1", "°C"), -0.1)
        self.assertAlmostEqual(server.legacy_number("50", "°F"), 10.0)
        for state, unit in (("x", "°C"), ("nan", "°C"), ("1", ""), ("200", "°C"), (None, "°C")):
            self.assertIsNone(server.legacy_number(state, unit))

    def test_profile_numeric_set(self):
        ids = devices.numeric_temperature_entities({"profile": PROFILE, "source": "ha"})
        self.assertIn(DAMPED, ids)
        self.assertIn(TARGET, ids)
        self.assertIn("sensor.boiler_outdoortemp", ids)
        self.assertNotIn("number.thermostat_hc1_designtemp", ids)                    # settings stay as reported
        self.assertNotIn(DAMPED, devices.temperature_entities({"profile": PROFILE, "source": "ha"}))


class StorageAndJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = server.DB
        server.DB = str(Path(self.tmp.name) / "ab032.db")
        server.setup()
        cfg = model.empty()
        cfg["rooms"].append({"id": "wz", "name": "Wohnzimmer", "target": 21, "area": None, "default_circuit_id": "hk1",
                             "temperature_sensors": ["sensor.wz"], "window_sensors": [],
                             "heat_load": model.empty_heat_load()})
        cfg["building"]["heat_source"] = {"profile": PROFILE, "source": "ha", "emsesp_host": ""}
        server.save_v2(cfg)

    def tearDown(self):
        server.DB = self.old
        self.tmp.cleanup()

    def test_sampling_stores_damped_outdoor_as_number(self):
        now = server.utcnow().isoformat()
        states = [{"entity_id": "sensor.wz", "state": "21.4", "unit": "°C", "source_at": now},
                  {"entity_id": DAMPED, "state": "-0.1", "unit": "°C", "source_at": None},
                  {"entity_id": TARGET, "state": "42", "unit": "°C", "source_at": None},
                  {"entity_id": "number.thermostat_hc1_designtemp", "state": "54", "unit": "°C", "source_at": now}]
        with patch.object(server, "get_entities", return_value=states):
            server.sample()
        with sqlite3.connect(server.DB) as c:
            rows = {e: (v, q, n) for e, v, q, n in c.execute(
                "SELECT entity_id,valid,quality,normalized_value FROM samples")}
        self.assertEqual(rows[DAMPED], (1, "ok", -0.1))
        self.assertEqual(rows[TARGET], (1, "ok", 42.0))
        self.assertIsNone(rows["number.thermostat_hc1_designtemp"][2])              # setting unchanged
        by = {f["key"]: f for f in server.heat_source_snapshot()["fields"]}
        self.assertEqual(by["curve_design"]["value"], "54")

    def legacy_job(self):
        """Job recorded by v0.6.2: damped outdoor and flow target only as raw state."""
        with patch.object(server, "utcnow", return_value=NOW):
            job = server.start_session({"name": "Alt", "confirmed": True})
        with sqlite3.connect(server.DB) as c:
            for i, (out, flow) in enumerate((("-2.0", "46"), ("0.5", "43"), ("x", "40"))):
                at = (NOW + timedelta(minutes=5 * (i + 1))).isoformat()
                for eid, st in ((DAMPED, out), (TARGET, flow)):
                    c.execute("INSERT INTO samples(at,entity_id,state,unit,valid,quality,normalized_value,session_id)"
                              " VALUES(?,?,?,?,?,?,?,?)", (at, eid, st, "°C", 1, "ok", None, job["id"]))
        return job

    def test_legacy_samples_are_read_for_curve_points(self):
        job = self.legacy_job()
        points, damped, meta = server.curve_points(job["id"], server.load_v2())
        self.assertTrue(damped)
        self.assertEqual(points, [(-2.0, 46.0), (0.5, 43.0)])                       # "x" is not invented
        self.assertEqual(meta["n"], 2)
        with sqlite3.connect(server.DB) as c:                                         # stored data unchanged
            self.assertEqual(c.execute("SELECT COUNT(*) FROM samples WHERE normalized_value IS NOT NULL").fetchone()[0], 0)

    def test_legacy_samples_count_for_assessment(self):
        job = self.legacy_job()
        captured = {}
        real = server.assessment.assess

        def spy(start, end, ticks, values, *a, **k):
            captured.update(values=values)
            return real(start, end, ticks, values, *a, **k)
        with patch.object(server.assessment, "assess", side_effect=spy):
            server.session_assessment(job["id"], NOW + timedelta(hours=1))
        damped = [cells[DAMPED]["v"] for _, cells in sorted(captured["values"].items()) if DAMPED in cells]
        self.assertEqual(damped, [-2.0, 0.5, None])


if __name__ == "__main__":
    unittest.main()
