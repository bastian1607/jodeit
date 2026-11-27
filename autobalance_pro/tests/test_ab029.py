"""AB-029: take over everything the read device profile delivers into empty configuration fields."""
import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import devices  # noqa: E402
import model  # noqa: E402
import server  # noqa: E402

PROFILE = "buderus-emsesp-hmc310"
# Values as read from Bastian's plant (EMS-ESP 3.8.6, HMC310) on 27.11.2026, 16:15.
PLANT = {
    "sensor.boiler_outdoortemp": "-0.8", "sensor.thermostat_dampedoutdoortemp": "-0.6",
    "sensor.thermostat_hc1_targetflowtemp": "42", "sensor.boiler_curflowtemp": "42.0", "sensor.boiler_rettemp": "41.2",
    "sensor.boiler_hptc1": "47.1", "sensor.boiler_hptc0": "41.2", "binary_sensor.boiler_heatingactive": "on",
    "binary_sensor.boiler_heatingpump": "on", "binary_sensor.boiler_dhw_charging": "off", "sensor.boiler_hpactivity": "heating",
    "number.thermostat_hc1_designtemp": "54", "number.thermostat_hc1_baseflowtemp": "25",
    "number.thermostat_hc1_offsettemp": "-2.0", "number.thermostat_hc1_maxflowtemp": "52",
    "number.thermostat_minexttemp": "-10.0", "number.thermostat_hc1_seltemp": "21.5",
    "number.thermostat_hc1_hpminflowtemp": "18", "select.thermostat_hc1_controlmode": "weather compensated",
    "select.thermostat_hc1_heatingtype": "radiator", "select.thermostat_hc1_mode": "manual",
    "number.thermostat_hc1_roominfluence": "0", "number.thermostat_hc1_solarinfl": "0", "select.thermostat_building": "medium",
    "switch.thermostat_damping": "on", "select.thermostat_hc1_reducemode": "reduce", "switch.thermostat_dhw_disinfecting": "on",
    "select.thermostat_dhw_disinfectday": "mo", "number.thermostat_dhw_disinfecttime": "720",
    "sensor.thermostat_lastcode": "A01(5600) 25.11.2026 14:09-25.11.2026 14:12",
}
UNITS = {k: "°C" for k in PLANT if k.startswith("sensor.boiler_") and k != "sensor.boiler_hpactivity"}
UNITS.update({"sensor.thermostat_dampedoutdoortemp": "°C", "sensor.thermostat_hc1_targetflowtemp": "°C"})


class AutofillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = server.DB
        server.DB = str(Path(self.tmp.name) / "ab029.db")
        server.setup()
        self.plant = dict(PLANT)

    def tearDown(self):
        server.DB = self.old_db
        self.tmp.cleanup()

    def states(self):
        now = server.utcnow().isoformat()
        return [{"entity_id": k, "state": v, "unit": UNITS.get(k, ""), "source_at": now, "source_at_kind": "reported"}
                for k, v in self.plant.items()]

    def config(self, source="ha"):
        cfg = model.empty()
        cfg["building"]["heat_source"] = {"profile": PROFILE, "source": source,
                                          "emsesp_host": "192.168.178.69" if source == "emsesp_rest" else ""}
        return cfg

    def run_autofill(self):
        with patch.object(server, "get_entities", side_effect=self.states):
            server.sample()
            return server.autofill_from_source()

    def test_fills_every_available_field_of_the_real_plant(self):
        server.save_v2(self.config())
        report = self.run_autofill()
        cfg = server.load_v2()
        b, c = cfg["building"], cfg["circuits"][0]
        self.assertEqual(b["entities"], {"outdoor": "sensor.boiler_outdoortemp", "compressor": "",
                                         "dhw": "binary_sensor.boiler_dhw_charging"})
        self.assertEqual(c["entities"], {"flow": "sensor.boiler_curflowtemp", "flow_target": "sensor.thermostat_hc1_targetflowtemp",
                                         "return": "sensor.boiler_rettemp", "pump": "binary_sensor.boiler_heatingpump"})
        self.assertEqual(b["heat_pump"]["manufacturer"], "Buderus")
        self.assertEqual((b["heating_system"], c["emitter_type"]), ("radiators", "radiators"))
        params = c["heating_curve"]["manufacturer_params"]
        self.assertEqual(params["Heizkurve Auslegungstemperatur"], 54)
        self.assertEqual(params["Heizkurve Fußpunkt"], 25)
        self.assertEqual(params["Heizkurve Parallelverschiebung"], -2)
        self.assertEqual(params["Regelungsart"], "weather compensated")
        self.assertEqual(len(params), len(devices.CURVE_PARAMS))
        self.assertIn("automatisch ausgelesen am", c["heating_curve"]["reference"])
        self.assertEqual(c["heating_curve"]["points"], [])               # no invented curve points
        self.assertEqual(b["design_outdoor_c"], -10.0)
        self.assertIn("Mit der Heizlastberechnung abgleichen", b["design_outdoor_reference"])
        self.assertIsNone(c["design_flow_c"])                           # controller curve is no hydraulic design value
        self.assertEqual(report["kept"], [])
        self.assertIn("Heizkreis Vorlauf (Entität)", report["filled"])
        with server.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM config_history WHERE kind='autofill'").fetchone()[0], 1)
        self.assertIsNotNone(self.run_autofill())                      # idempotent: nothing new
        with server.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM config_history WHERE kind='autofill'").fetchone()[0], 1)

    def test_user_values_are_never_overwritten(self):
        cfg = self.config()
        cfg["building"]["heat_pump"]["manufacturer"] = "Bosch"
        cfg["circuits"][0]["entities"]["flow"] = "sensor.eigener_vorlauf"
        cfg["building"]["design_outdoor_c"] = -12.0
        cfg["building"]["design_outdoor_reference"] = "Heizlastberechnung 2025"
        server.save_v2(cfg)
        report = self.run_autofill()
        cfg = server.load_v2()
        self.assertEqual(cfg["building"]["heat_pump"]["manufacturer"], "Bosch")
        self.assertEqual(cfg["circuits"][0]["entities"]["flow"], "sensor.eigener_vorlauf")
        self.assertEqual(cfg["building"]["design_outdoor_c"], -12.0)
        self.assertIn("Hersteller Wärmepumpe", report["kept"])
        self.assertIn("Heizkreis Vorlauf (Entität)", report["kept"])
        self.assertEqual(cfg["circuits"][0]["entities"]["return"], "sensor.boiler_rettemp")   # empty field still filled

    def test_changed_device_setting_refreshes_auto_value_only(self):
        server.save_v2(self.config())
        self.run_autofill()
        self.plant["number.thermostat_hc1_offsettemp"] = "-1.0"          # installer changed the curve at the controller
        report = self.run_autofill()
        params = server.load_v2()["circuits"][0]["heating_curve"]["manufacturer_params"]
        self.assertEqual(params["Heizkurve Parallelverschiebung"], -1)
        self.assertIn("Heizkurve laut Regler", report["refreshed"])
        cfg = server.load_v2()                                          # user edits the min. outdoor temperature
        cfg["building"]["design_outdoor_c"] = -14.0
        server.save_v2(cfg)
        self.plant["number.thermostat_minexttemp"] = "-11.0"
        self.run_autofill()
        self.assertEqual(server.load_v2()["building"]["design_outdoor_c"], -14.0)

    def test_missing_or_unavailable_values_are_reported_not_invented(self):
        self.plant["select.thermostat_hc1_heatingtype"] = "unavailable"
        del self.plant["sensor.boiler_rettemp"]
        server.save_v2(self.config())
        report = self.run_autofill()
        cfg = server.load_v2()
        self.assertEqual(cfg["circuits"][0]["entities"]["return"], "")
        self.assertIsNone(cfg["circuits"][0]["emitter_type"])
        self.assertIn("Heizkreis Rücklauf (Entität)", report["missing"])
        self.assertIn("Heizflächenart Heizkreis", report["missing"])

    def test_unknown_heating_type_is_not_mapped(self):
        self.plant["select.thermostat_hc1_heatingtype"] = "convector"
        server.save_v2(self.config())
        self.run_autofill()
        self.assertIsNone(server.load_v2()["circuits"][0]["emitter_type"])

    def test_rest_source_fills_values_but_no_ha_entities(self):
        server.save_v2(self.config("emsesp_rest"))
        values = {k: {"entity_id": "emsesp.x", "value": v} for k, v in
                  (("curve_design", "54"), ("min_ext", "-10"), ("heating_type", "radiator"), ("flow", "42"))}
        cfg, report = devices.autofill(server.load_v2(), values, "27.11.2026")
        self.assertEqual(cfg["circuits"][0]["entities"]["flow"], "")
        self.assertEqual(cfg["circuits"][0]["heating_curve"]["manufacturer_params"], {"Heizkurve Auslegungstemperatur": 54,
                                                                                      "Minimale Außentemperatur (Auslegung)": -10})
        model.validate(cfg)

    def test_no_profile_does_nothing(self):
        server.save_v2(model.empty())
        with patch.object(server, "get_entities", side_effect=self.states):
            self.assertIsNone(server.autofill_from_source())
        self.assertEqual(server.load_v2()["circuits"][0]["entities"]["flow"], "")

    def test_no_write_to_device(self):
        source = Path(server.__file__).read_text() + Path(devices.__file__).read_text()
        for word in ("/api/services", "call_service", "mqtt.publish"):
            self.assertNotIn(word, source)
        self.assertFalse(devices.WRITE_ENABLED)


if __name__ == "__main__":
    unittest.main()
