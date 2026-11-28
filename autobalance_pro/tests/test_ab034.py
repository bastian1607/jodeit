"""AB-034: night rule pilot v2 (ChatGPT Auftrag 20, limits confirmed by Bastian 28.11.2026), AC20-01 … 09.

Undisturbed blocks instead of one contiguous 4-hour window; defrost after-run 30 min with flow recovery check
(else 60 min); DHW after-run stays 60 min.
"""
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import assessment  # noqa: E402
from test_ab014 import Plant, config, local  # noqa: E402

DAY = "2026-12-01"


def night(r, date=DAY):
    return next(n for n in r["nights"] if n["date"] == date)


def defrost(p, start, minutes=6, drop_to=28.6, recover_min=15, base=40.0):
    """Defrost phase like Bastian's plant on 28.11.: flow drops, then recovers linearly within ``recover_min``."""
    end = start + timedelta(minutes=minutes)
    p.set("sensor.act", start, end, s="defrost")
    p.set("binary_sensor.heat", start, end, s="off")
    for t in p.ticks:
        if start <= t < end:
            p.values[t]["sensor.vl"] = {"v": drop_to, "s": str(drop_to), "ok": True}
        elif end <= t < end + timedelta(minutes=recover_min):
            share = (t - end) / timedelta(minutes=recover_min)
            v = round(drop_to + (base - drop_to) * share, 1)
            p.values[t]["sensor.vl"] = {"v": v, "s": str(v), "ok": True}


class NightRuleTests(unittest.TestCase):
    def test_ac20_01_real_night_27_28_11_is_suitable(self):
        p = Plant(days=2)
        for h, m in ((0, 14), (2, 21), (4, 22)):
            defrost(p, local(2, h, m))
        p.set("binary_sensor.ww", local(2, 5, 37), local(2, 6, 30), s="on")
        n = night(p.run())
        self.assertTrue(n["valid"])
        self.assertTrue(n["heating_verified"])
        self.assertGreaterEqual(n["counted_hours"], 4.0)
        self.assertGreaterEqual(sum(1 for b in n["blocks"] if b["counted"]), 2)
        self.assertEqual((n["defrost_events"], n["defrost_unrecovered"]), (3, 0))
        self.assertIn("dhw", n["disturbances"])

    def test_ac20_01_missing_room_sensor_gives_no_suitable_night(self):
        p = Plant(days=2)
        defrost(p, local(2, 0, 14))
        p.set("sensor.wz", local(1, 21), local(2, 7), v=None, s="unavailable", ok=False)
        self.assertFalse(night(p.run())["valid"])

    def test_ac20_02_many_short_islands_do_not_count(self):
        p = Plant(days=2)
        t = local(1, 22)
        while t < local(2, 6):                       # every third poll without heating -> 10-min islands
            p.set("binary_sensor.heat", t, t + timedelta(minutes=5), s="off")
            t += timedelta(minutes=15)
        n = night(p.run())
        self.assertGreater(n["ok_hours"], 4.0)        # enough ok polls in total …
        self.assertFalse(n["valid"])                  # … but only islands shorter than 30 min

    def test_ac20_03_one_undisturbed_four_hour_block_is_suitable(self):
        p = Plant(days=2)
        p.set("binary_sensor.heat", local(2, 2), local(2, 7), s="off")
        n = night(p.run())
        self.assertTrue(n["valid"])
        self.assertEqual(n["valid_window"], [local(1, 22).isoformat(), local(2, 2).isoformat()])

    def test_ac20_04_after_run_30_min_only_with_recovered_flow(self):
        ok = Plant(days=2)
        defrost(ok, local(2, 0, 0), minutes=10)
        bad = Plant(days=2)
        defrost(bad, local(2, 0, 0), minutes=10, recover_min=24 * 60)    # flow does not come back
        th = assessment.THRESHOLDS
        (a1, b1, rec1), = assessment._defrost_intervals(
            [t for t in ok.ticks if ok.values[t]["sensor.act"]["s"] == "defrost"], ok.ticks, ok.values, "sensor.vl", th)
        (a2, b2, rec2), = assessment._defrost_intervals(
            [t for t in bad.ticks if bad.values[t]["sensor.act"]["s"] == "defrost"], bad.ticks, bad.values, "sensor.vl", th)
        self.assertEqual(a1, local(2, 0, 0) - timedelta(minutes=5))       # uncertain poll before excluded
        self.assertEqual((b1, rec1), (local(2, 0, 10) + timedelta(minutes=30), True))
        self.assertEqual((b2, rec2), (local(2, 0, 10) + timedelta(minutes=60), False))
        self.assertEqual(night(bad.run())["defrost_unrecovered"], 1)

    def test_ac20_04_without_flow_value_60_min(self):
        p = Plant(days=2)
        defrost(p, local(2, 0, 0), minutes=10)
        ticks = [t for t in p.ticks if p.values[t]["sensor.act"]["s"] == "defrost"]
        (_, b, rec), = assessment._defrost_intervals(ticks, p.ticks, p.values, None, assessment.THRESHOLDS)
        self.assertEqual((b, rec), (local(2, 0, 10) + timedelta(minutes=60), False))

    def test_ac20_05_defrost_lasts_through_unavailable_status(self):
        p = Plant(days=2)
        defrost(p, local(2, 0, 0), minutes=10)
        p.set("sensor.act", local(2, 0, 10), local(2, 0, 40), s="unavailable", ok=False)
        r = p.run()
        self.assertEqual(r["events"]["defrost_ticks"], 8)                 # 2 defrost + 6 unknown polls
        self.assertEqual(r["defrost"]["events"], 1)

    def test_ac20_05_unknown_status_is_not_undisturbed(self):
        p = Plant(days=2)
        p.set("sensor.act", local(1, 20), local(2, 8), s="unavailable", ok=False)
        p.set("binary_sensor.heat", local(1, 20), local(2, 8), s="unavailable", ok=False)
        n = night(p.run())
        self.assertFalse(n["heating_verified"])
        self.assertFalse(n["disturbances_verified"])

    def test_ac20_06_dhw_at_end_keeps_earlier_blocks(self):
        p = Plant(days=2)
        defrost(p, local(2, 1, 0))
        p.set("binary_sensor.ww", local(2, 5, 37), local(2, 7), s="on")
        n = night(p.run())
        self.assertTrue(n["valid"])
        last = max(b["end"] for b in n["blocks"] if b["counted"])
        self.assertLessEqual(last, local(2, 5, 40).isoformat())          # last ok poll 05:35 (slot to 05:40)

    def test_ac20_07_no_hours_added_up_across_a_real_change(self):
        p = Plant(days=2)
        p.set("number.offset", local(2, 2), p.end, v=-1, s="-1")
        n = night(p.run())
        self.assertFalse(n["valid"])                    # 22–02 (old setting) + 04:05–06 (new) must not be summed
        p2 = Plant(days=2)
        p2.set("number.offset", local(2, 2), local(2, 2, 20), s="unavailable", ok=False)
        self.assertTrue(night(p2.run())["valid"])       # AB-033: a dropout is no change

    def test_ac20_09_room_trend_never_bridges_a_disturbance(self):
        p = Plant(days=2)
        defrost(p, local(2, 1, 0))
        p.set("sensor.wz", local(2, 1, 0), local(2, 7), v=21.8, s="21.8")   # level step during the defrost gap
        r = p.run()
        wz = next(x for x in r["rooms"] if x["id"] == "wz")
        self.assertEqual(wz["trend_k_per_h"], 0.0)       # flat inside each block
        self.assertTrue(wz["stationary"])

    def test_rule_version_in_result(self):
        r = Plant(days=2).run()
        self.assertEqual(r["rule_version"], assessment.RULE_VERSION)
        self.assertEqual(r["thresholds"]["defrost_after_min"], 30)
        self.assertEqual(r["thresholds"]["dhw_after_min"], 60)


if __name__ == "__main__":
    unittest.main()
