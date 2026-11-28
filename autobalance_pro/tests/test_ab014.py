"""AB-014: measuring-job assessment against the pilot rule set (synthetic data, AC-01 … AC-12)."""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import assessment  # noqa: E402
import report  # noqa: E402
import model  # noqa: E402
import server  # noqa: E402

TZ = ZoneInfo("Europe/Berlin")
ROLES = {"outdoor_damped": "sensor.damped", "outdoor": "sensor.out", "flow": "sensor.vl", "flow_target": "sensor.vls",
         "return": None, "heating_active": "binary_sensor.heat", "dhw": "binary_sensor.ww",
         "hp_activity": "sensor.act", "params": ["number.offset", "number.design"]}
START = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)


def config(rooms=(("wz", "Wohnzimmer", ["sensor.wz"], ["binary_sensor.f_wz"], 21),
                  ("bad", "Bad", [], [], 23))):
    cfg = model.empty()
    for rid, name, sensors, windows, target in rooms:
        cfg["rooms"].append({"id": rid, "name": name, "target": target, "area": None, "default_circuit_id": "hk1",
                             "heat_load": model.empty_heat_load(), "design_indoor_c": None, "design_indoor_reference": "",
                             "temperature_sensors": list(sensors), "window_sensors": list(windows)})
    return cfg


def p_end(r):
    return datetime.fromisoformat(r["window"]["end"])


class Plant:
    """Synthetic 5-minute data. ``outdoor(t)`` gives the damped outdoor temperature."""

    def __init__(self, days=7, outdoor=lambda t: 2.0, start=START):
        self.start, self.end = start, start + timedelta(days=days)
        self.ticks, self.values = [], {}
        t = start
        while t < self.end:
            self.ticks.append(t)
            o = outdoor(t)
            self.values[t] = {"sensor.damped": cell(o), "sensor.out": cell(o + 0.5), "sensor.vl": cell(40), "sensor.vls": cell(41),
                              "binary_sensor.heat": cell(None, "on"), "binary_sensor.ww": cell(None, "off"),
                              "sensor.act": cell(None, "heating"), "number.offset": cell(-2, "-2"), "number.design": cell(54, "54"),
                              "sensor.wz": cell(20.8), "binary_sensor.f_wz": cell(None, "off")}
            t += timedelta(minutes=5)
        self.history = [(start, config())]

    def set(self, entity, a, b, v=None, s=None, ok=True):
        for t in self.ticks:
            if a <= t < b:
                self.values[t][entity] = {"v": v, "s": s if s is not None else ("" if v is None else str(v)), "ok": ok}

    def drop(self, a, b):
        self.ticks = [t for t in self.ticks if not a <= t < b]

    def run(self, roles=ROLES, end=None):
        return assessment.assess(self.start, end or self.end, self.ticks, self.values, self.history, roles, TZ, 7)


def cell(v, s=None):
    return {"v": v, "s": s if s is not None else str(v), "ok": True}


def local(day, hour, minute=0):
    return datetime(2026, 12, day, hour, minute, tzinfo=TZ).astimezone(timezone.utc)


def by_night(values):
    """outdoor per local night: dict {first-day: temp}."""
    def f(t):
        lt = t.astimezone(TZ)
        day = lt.day if lt.hour >= 12 else lt.day - 1
        return values.get(day, 5.0)
    return f


class AssessmentTests(unittest.TestCase):
    def test_ac01_full_coverage_but_narrow_band_no_slope(self):
        r = Plant(outdoor=lambda t: 2.0 + (t.hour % 2)).run()          # 2 K span only
        self.assertEqual(r["stage"], "level")
        self.assertGreaterEqual(r["valid_nights"], 6)
        self.assertTrue(any("Temperaturspreizung" in x for x in r["reasons"]))
        self.assertEqual(r["coverage"][0]["share"], 1.0)

    def test_ac02_three_nights_level_only(self):
        p = Plant(days=3.5)
        r = p.run()
        self.assertEqual(r["valid_nights"], 3)
        self.assertEqual(r["stage"], "level")

    def test_ac03_two_groups_eight_kelvin_slope(self):
        p = Plant(outdoor=by_night({1: -6, 2: -5, 3: 3, 4: 4, 5: 4, 6: -6, 7: 3}))
        r = p.run()
        self.assertEqual(r["stage"], "level_and_slope")
        self.assertIn("nur Technikerprüfung", r["stage_text"])
        # 7.9 K is not enough
        r2 = Plant(outdoor=by_night({1: -5, 2: -5, 3: 2.9, 4: 2.9, 5: 2.9, 6: -5, 7: 2.9})).run()
        self.assertEqual(r2["stage"], "level")

    def test_ac04_window_marks_room_and_followup_history_kept(self):
        p = Plant(days=2)
        p.set("binary_sensor.f_wz", local(1, 22), local(2, 5), s="on")
        r = p.run()
        first = next(n for n in r["nights"] if n["date"] == "2026-12-01")
        self.assertFalse(first["valid"])
        self.assertIn("window", first["disturbances"])
        self.assertEqual(r["events"]["window_rooms"], ["wz"])
        self.assertEqual(p.values[local(1, 23)]["sensor.wz"]["v"], 20.8)        # nothing deleted

    def test_ac04_window_plus_90_minutes(self):
        p = Plant(days=2)
        p.set("binary_sensor.f_wz", local(1, 22), local(1, 22, 30), s="on")   # open 22:00–22:30, +90 -> 00:00
        night = next(n for n in p.run()["nights"] if n["date"] == "2026-12-01")
        self.assertTrue(night["valid"])
        self.assertEqual(night["valid_window"][0], local(2, 0).isoformat())   # closed 22:30 + 90 min

    def test_ac05_dhw_defrost_with_after_run(self):
        p = Plant(days=2)
        for h in (22, 0, 2, 4):
            day = 1 if h == 22 else 2
            p.set("sensor.act", local(day, h), local(day, h, 10), s="defrost")
        r = p.run()
        night = next(n for n in r["nights"] if n["date"] == "2026-12-01")
        # AB-034: four 10-min defrosts with recovered flow leave four ~70-min blocks -> night counts
        self.assertTrue(night["valid"])
        self.assertIn("defrost", night["disturbances"])
        self.assertEqual((night["defrost_events"], night["defrost_unrecovered"]), (4, 0))
        hourly = Plant(days=2)                          # defrost every hour: only ~20-min islands remain
        for i in range(8):
            t = local(1, 22) + timedelta(hours=i)
            hourly.set("sensor.act", t, t + timedelta(minutes=10), s="defrost")
        self.assertFalse(next(n for n in hourly.run()["nights"] if n["date"] == "2026-12-01")["valid"])
        p2 = Plant(days=2)
        p2.set("binary_sensor.ww", local(1, 23), local(2, 1), s="on")
        n2 = next(n for n in p2.run()["nights"] if n["date"] == "2026-12-01")
        self.assertIn("dhw", n2["disturbances"])
        self.assertTrue(n2["valid"])                    # 02:00–06:00 remains

    def test_ac06_parameter_change_starts_new_segment(self):
        p = Plant(days=6)
        p.set("number.offset", local(5, 12), p.end, v=-1.5, s="-1.5")
        r = p.run()
        self.assertEqual(r["segments"], 2)
        self.assertEqual(r["changes"][0]["detail"], ["number.offset"])
        self.assertEqual(r["valid_nights"], 2)          # only nights after the change count
        self.assertEqual(r["stage"], "insufficient")

    def test_ac06_target_change_from_history(self):
        p = Plant(days=6)
        changed = config()
        changed["rooms"][0]["target"] = 22
        p.history.append((local(3, 12), changed))
        r = p.run()
        self.assertEqual([c["kind"] for c in r["changes"]], ["target"])
        self.assertEqual(r["segments"], 2)

    def test_ac07_missing_basis_is_reported(self):
        roles = dict(ROLES, outdoor_damped=None, dhw=None, hp_activity=None)
        p = Plant(days=4)
        p.history = [(START, config(rooms=(("wz", "Wohnzimmer", ["sensor.wz"], [], 21),)))]
        r = p.run(roles=roles)
        text = " ".join(r["missing_basis"])
        for fragment in ("gedämpfte", "Warmwasser", "Abtauung", "kein Fensterkontakt"):
            self.assertIn(fragment, text)

    def test_ac08_period_over_but_one_night(self):
        p = Plant()
        p.set("binary_sensor.heat", p.start, local(7, 12), s="off")
        r = p.run()
        self.assertEqual(r["valid_nights"], 1)
        self.assertEqual(r["stage"], "insufficient")
        self.assertIn("Messung unzureichend", " ".join(assessment.report_text(r)))

    def test_ac09_sensor_added_on_day_three(self):
        p = Plant(days=6)
        added = config(rooms=(("wz", "Wohnzimmer", ["sensor.wz"], ["binary_sensor.f_wz"], 21),
                              ("bad", "Bad", ["sensor.bad"], [], 23)))
        p.history.append((local(3, 12), added))
        p.set("sensor.bad", local(3, 12), p.end, v=22.6)
        r = p.run()
        bad = next(c for c in r["coverage"] if c["entity_id"] == "sensor.bad")
        self.assertEqual(bad["share"], 1.0)
        self.assertEqual(bad["since"], local(3, 12).isoformat())

    def test_removed_sensor_counts_only_while_assigned(self):
        p = Plant(days=6)
        removed = config(rooms=(("wz", "Wohnzimmer", [], ["binary_sensor.f_wz"], 21),))
        p.history.append((local(3, 12), removed))
        p.set("sensor.wz", local(3, 12), p.end, v=None, s="unavailable", ok=False)
        wz = next(c for c in p.run()["coverage"] if c["entity_id"] == "sensor.wz")
        self.assertEqual((wz["share"], wz["until"]), (1.0, local(3, 12).isoformat()))

    def test_ac10_raw_and_damped_outdoor_difference_visible(self):
        r = Plant(days=4).run()
        self.assertTrue(r["outdoor_band"]["damped"])
        self.assertEqual(r["outdoor_band"]["raw_minus_damped_median_k"], 0.5)

    def test_ac11_report_contents(self):
        r = Plant(outdoor=lambda t: 2.0).run()
        wz = next(x for x in r["rooms"] if x["id"] == "wz")
        self.assertEqual(wz["deviation_median_k"], -0.2)
        self.assertTrue(wz["stationary"])
        bad = next(x for x in r["rooms"] if x["id"] == "bad")
        self.assertFalse(bad["monitored"])
        self.assertIsNone(bad["deviation_median_k"])
        self.assertIn("Steigung und Frostverhalten nicht nachgewiesen", " ".join(assessment.report_text(r)))

    def test_coverage_below_85_percent_blocks(self):
        p = Plant()
        for d in range(1, 8):
            p.drop(local(d, 12), local(d, 16))           # 4 h per day missing -> 83 %
        r = p.run()
        self.assertEqual(r["stage"], "insufficient")
        self.assertTrue(any("Technische Abdeckung" in x for x in r["reasons"]))

    def test_long_outage_listed_and_no_data(self):
        p = Plant()
        p.drop(local(3, 0), local(3, 9))
        r = p.run()
        self.assertEqual(len(r["long_outages"]), 1)
        self.assertTrue(any("nicht ersetzt" in x for x in assessment.report_text(r)))
        empty = Plant(days=1)
        empty.ticks = []
        self.assertEqual(empty.run()["stage"], "no_data")

    def test_ac6_02_without_heating_signal_no_level_statement(self):
        roles = dict(ROLES, heating_active=None, hp_activity=None)
        r = Plant().run(roles=roles)
        self.assertEqual(r["stage"], "unverified")
        self.assertIn("Heizbetrieb nur in 0 durch gültige Statuswerte", " ".join(assessment.report_text(r)))
        self.assertIn("Kein Betriebsstatus der Wärmepumpe zugeordnet", " ".join(r["reasons"]))
        roles = dict(ROLES, hp_activity=None, dhw=None)       # heating known, disturbances not checked
        r = Plant(outdoor=lambda t: 2.0).run(roles=roles)
        self.assertEqual(r["stage"], "level")
        self.assertEqual(r["stage_text"], assessment.STAGE_TEXT["level"])      # notice is separate, not appended
        self.assertEqual(r["disturbance_notice"], "WW-/Abtauphasen nicht zuverlässig erfasst. Messergebnis fachlich eingeschränkt.")
        self.assertTrue(r["restricted"])
        self.assertEqual(r["restricted_label"], "Vorläufig · fachlich eingeschränkt")
        r["texts"] = assessment.report_text(r)
        html = report.render({"name": "T", "started_at": r["window"]["start"], "ended_at": None, "note": ""}, r, [], [], [], {},
                             timezone.utc, "UTC", "t", p_end(r))
        self.assertIn("Vorläufig · fachlich eingeschränkt: " + assessment.STAGE_TEXT["level"], html)
        self.assertIn('class="result warn" id="report-result"', html)

    def test_review7_e_status_assigned_but_unavailable_all_nights(self):
        p = Plant()
        p.set("binary_sensor.heat", p.start, p.end, s="unavailable", ok=False)
        p.set("sensor.act", p.start, p.end, s="unavailable", ok=False)
        r = p.run()
        self.assertEqual(r["stage"], "unverified")
        self.assertGreaterEqual(r["technical_nights"], 6)
        self.assertEqual(r["valid_nights"], 0)
        self.assertIn("Nur 0 von", " ".join(r["reasons"]))

    def test_heating_status_missing_in_part_of_the_nights(self):
        p = Plant(outdoor=by_night({1: 4, 2: 4, 3: -6, 4: -5, 5: 4, 6: 4, 7: 4}))
        p.set("binary_sensor.heat", local(3, 12), local(5, 12), s="unknown", ok=False)
        p.set("sensor.act", local(3, 12), local(5, 12), s="unknown", ok=False)
        r = p.run()
        self.assertEqual(r["technical_nights"], 7)
        self.assertEqual(r["valid_nights"], 5)           # nights 3 and 4 not counted
        self.assertFalse(next(n for n in r["nights"] if n["date"] == "2026-12-03")["heating_verified"])
        self.assertEqual(r["stage"], "level")            # cold nights unverified -> must not create a slope

    def test_ac6_03_slope_examples(self):
        ok = Plant(outdoor=by_night({1: -6, 2: -5, 3: 3, 4: 4, 5: 4, 6: 4, 7: 4})).run()
        self.assertEqual(ok["stage"], "level_and_slope")
        no = Plant(outdoor=by_night({1: -6, 2: -5, 3: 2, 4: 4, 5: 2, 6: 2, 7: 2})).run()
        self.assertEqual(no["stage"], "level")

    def test_running_job_is_provisional(self):
        p = Plant(days=4)
        r = assessment.assess(p.start, p.end, p.ticks, p.values, p.history, ROLES, TZ, planned_days=7)
        self.assertFalse(r["window"]["complete"])
        self.assertTrue(any("vorläufig" in x for x in r["reasons"]))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = server.DB
        server.DB = os.path.join(self.tmp.name, "a.db")
        server.setup()

    def tearDown(self):
        server.DB = self.old_db
        self.tmp.cleanup()

    def test_ac12_end_to_end_from_database_without_any_write(self):
        cfg = config()
        cfg["building"]["entities"]["outdoor"] = "sensor.out"
        cfg["circuits"][0]["entities"]["flow"] = "sensor.vl"
        server.save_v2(cfg)
        with patch.object(server, "utcnow", return_value=START):
            s = server.start_session({"name": "Pilot", "confirmed": True})
        p = Plant(days=4)
        with sqlite3.connect(server.DB) as c:
            for t in p.ticks:
                row = p.values[t]
                for eid in ("sensor.out", "sensor.vl", "sensor.wz", "binary_sensor.f_wz"):
                    x = row[eid]
                    c.execute("INSERT INTO samples(at,entity_id,state,unit,valid,quality,normalized_value,session_id) VALUES(?,?,?,?,?,?,?,?)",
                              (t.isoformat(), eid, x["s"], "", 1, "ok", x["v"], s["id"]))
        with patch("urllib.request.urlopen", side_effect=AssertionError("no network")):
            r = server.session_assessment(s["id"], now=p.end)
        self.assertEqual(r["roles"]["outdoor"], "sensor.out")
        self.assertIsNone(r["roles"]["outdoor_damped"])
        self.assertEqual(r["technical_nights"], 4)
        self.assertEqual(r["valid_nights"], 0)
        # AC6-02: HA assignment without heating-mode signal -> descriptive only, no level statement
        self.assertEqual(r["stage"], "unverified")
        self.assertIn("Heizbetrieb nicht nachgewiesen", r["stage_text"])
        self.assertTrue(r["read_only"])
        self.assertIsNone(server.session_assessment("ma_unknown"))


if __name__ == "__main__":
    unittest.main()
