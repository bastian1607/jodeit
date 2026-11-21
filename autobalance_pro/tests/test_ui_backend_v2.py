"""AB-009 backend: readiness codes, load-share suggestion, valve catalogue, API endpoints."""
import copy
import json
import math
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import model  # noqa: E402
import server  # noqa: E402
from test_model_v2 import adopted, v1_config  # noqa: E402


def two_radiator_room():
    cfg = model.from_v1(v1_config())
    cfg["circuits"][0].update(design_flow_c=55, design_return_c=45)
    a, b = cfg["emitters"][0], cfg["emitters"][1]          # both in "wohnen"
    a["rated_output"].update(value_w=1200, flow_c=75, return_c=65, room_c=20)
    b["rated_output"].update(value_w=600, flow_c=75, return_c=65, room_c=20)
    a["exponent"] = b["exponent"] = 1.3
    return cfg


class SuggestionTests(unittest.TestCase):
    def test_single_emitter_is_100_percent(self):
        cfg = model.from_v1(v1_config())
        shares, reason = model.suggest_load_shares(cfg, "bad")
        self.assertIsNone(reason)
        self.assertEqual(list(shares.values()), [100.0])

    def test_equal_reference_gives_power_ratio_and_sums_to_100(self):
        shares, reason = model.suggest_load_shares(two_radiator_room(), "wohnen")
        self.assertIsNone(reason)
        self.assertEqual(sorted(shares.values()), [33.3, 66.7])
        self.assertAlmostEqual(sum(shares.values()), 100.0, places=6)

    def test_different_reference_temperatures_are_normalised(self):
        cfg = two_radiator_room()
        # Same catalogue value at 55/45/20 instead of 75/65/20 delivers more at design conditions.
        cfg["emitters"][1]["rated_output"].update(value_w=1200, flow_c=55, return_c=45, room_c=20)
        shares, _ = model.suggest_load_shares(cfg, "wohnen")
        lm = lambda f, r, i: (f - r) / math.log((f - i) / (r - i))
        q_a = 1200 * (lm(55, 45, 21) / lm(75, 65, 20)) ** 1.3
        q_b = 1200 * (lm(55, 45, 21) / lm(55, 45, 20)) ** 1.3
        self.assertAlmostEqual(shares[cfg["emitters"][0]["id"]], round(100 * q_a / (q_a + q_b), 1), places=6)
        self.assertGreater(shares[cfg["emitters"][1]["id"]], 50)

    def test_no_suggestion_without_comparable_data(self):
        for mutate, fragment in (
            (lambda c: c["emitters"][1]["rated_output"].update(flow_c=None, return_c=None, room_c=None), "Referenztemperaturen"),
            (lambda c: c["emitters"][1].update(exponent=None), "exponent"),
            (lambda c: c["circuits"][0].update(design_flow_c=None, design_return_c=None), "Auslegungstemperaturen"),
            (lambda c: c["emitters"][1].update(type="underfloor"), "FBH"),
            (lambda c: c["circuits"][0].update(design_flow_c=30, design_return_c=21), "Raumtemperatur"),
        ):
            cfg = two_radiator_room()
            mutate(cfg)
            shares, reason = model.suggest_load_shares(cfg, "wohnen")
            self.assertIsNone(shares)
            self.assertIn(fragment, reason)


class ReadinessTests(unittest.TestCase):
    def test_codes_and_targets(self):
        cfg = model.from_v1(v1_config())
        result = model.readiness(cfg)
        self.assertEqual({b["code"] for b in result["building"]}, {"heating_system_missing", "design_outdoor_missing"})
        wohnen = result["rooms"]["wohnen"]
        self.assertTrue(wohnen["measurement_ready"])
        codes = [b["code"] for b in wohnen["blockers"]]
        self.assertIn("heat_load_missing", codes)
        self.assertIn("shares_missing", codes)
        self.assertIn("circuit_design_missing", codes)
        out = next(b for b in wohnen["blockers"] if b["code"] == "output_reference_missing")
        self.assertEqual(out["emitter_id"], cfg["emitters"][0]["id"])
        self.assertEqual(result["rooms"]["kueche"]["blockers"][-1]["code"], "no_emitter")

    def test_complete_room_has_no_blockers_but_still_no_calculation(self):
        cfg = two_radiator_room()
        cfg["rooms"][0]["heat_load"] = adopted(1500)
        cfg["emitters"][0]["load_share_percent"] = 66.7
        cfg["emitters"][1]["load_share_percent"] = 33.3
        model.validate(cfg)
        result = model.readiness(cfg)
        self.assertEqual(result["rooms"]["wohnen"]["blockers"], [])
        self.assertIn("nicht implementiert", result["note"])

    def test_underfloor_is_flagged(self):
        cfg = model.from_v1(v1_config())
        cfg["emitters"][2]["type"] = "underfloor"
        codes = [b["code"] for b in model.readiness(cfg)["rooms"]["bad"]["blockers"]]
        self.assertIn("underfloor_not_supported", codes)

    def test_structure_validation_skips_only_share_rule(self):
        cfg = two_radiator_room()
        cfg["emitters"][0]["load_share_percent"] = 50
        with self.assertRaises(model.Invalid):
            model.validate(cfg)
        model.validate_structure(cfg)
        cfg["emitters"][0]["circuit_id"] = "unbekannt"
        with self.assertRaises(model.Invalid):
            model.validate_structure(cfg)

    def test_catalogue_has_no_unverified_product_data(self):
        for entry in model.VALVE_CATALOG:
            self.assertEqual(set(entry), {"key", "manufacturer", "series"})


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = server.DB
        server.DB = os.path.join(self.tmp.name, "api.db")
        server.setup()
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.API)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.http.server_port}"

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        server.DB = self.old_db
        self.tmp.cleanup()

    def call(self, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        with urlopen(Request(self.base + path, data, {"Content-Type": "application/json"})) as response:
            return json.load(response)

    def test_suggestion_endpoint_uses_unsaved_state_and_stores_nothing(self):
        cfg = two_radiator_room()
        cfg["emitters"][0]["load_share_percent"] = 10      # incomplete shares are fine here
        result = self.call("/api/v2/load-share-suggestion", {"config": cfg, "room_id": "wohnen"})
        self.assertIsNone(result["reason"])
        self.assertAlmostEqual(sum(result["shares"].values()), 100.0)
        self.assertEqual(self.call("/api/v2/config"), model.empty())
        with self.assertRaises(HTTPError) as caught:
            self.call("/api/v2/load-share-suggestion", {"config": {"schema_version": 1}, "room_id": "x"})
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()

    def test_readiness_and_catalog_endpoints(self):
        cfg = model.from_v1(v1_config())
        self.call("/api/v2/config", cfg)
        readiness = self.call("/api/v2/readiness")
        self.assertIn("rooms", readiness)
        self.assertIn("calculation_blockers", readiness)         # AB-008 format kept
        self.assertIn("Heizlast fehlt", readiness["calculation_blockers"]["wohnen"])
        self.assertEqual(self.call("/api/v2/catalog")["valves"][0]["series"], "Eclipse")

    def test_v2_save_then_legacy_save_keeps_everything(self):
        cfg = two_radiator_room()
        cfg["rooms"][0]["heat_load"] = adopted(1500)
        cfg["circuits"].append(model.empty_circuit("hk2", "FBH"))
        cfg["emitters"][1]["circuit_id"] = "hk2"
        cfg["emitters"][0]["valve"].update(manufacturer="IMI Heimeier", series="Eclipse", variant="DN15 Eck", current_setting="80 l/h")
        self.call("/api/v2/config", cfg)
        legacy = self.call("/api/config")
        self.call("/api/config", legacy)
        self.assertEqual(self.call("/api/v2/config"), cfg)


if __name__ == "__main__":
    unittest.main()
