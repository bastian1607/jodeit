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

    def run_autofill(self, now=None):
        with patch.object(server, "get_entities", side_effect=self.states):
            if now:
                with patch.object(server, "utcnow", return_value=now):
                    server.sample()
                    return server.autofill_from_source(now)
            server.sample()
            return server.autofill_from_source()

    def status(self):
        with patch.object(server, "get_entities", side_effect=self.states):
            return {f["path"]: f for f in server.autofill_report()["fields"]}

    def save_ui(self, cfg):
        """Save like the form: through the source-switch check."""
        return server.save_v2(server.prepare_source_switch(json.loads(json.dumps(cfg))))

    # ------------------------------------------------------------------ AC29-01..03
    def test_ac01_02_03_fills_available_fields_but_no_design_values(self):
        server.save_v2(self.config())
        changed = self.run_autofill()
        cfg = server.load_v2()
        b, c = cfg["building"], cfg["circuits"][0]
        self.assertEqual(b["entities"], {"outdoor": "sensor.boiler_outdoortemp", "compressor": "",          # AC29-03
                                         "dhw": "binary_sensor.boiler_dhw_charging"})
        self.assertEqual(c["entities"], {"flow": "sensor.boiler_curflowtemp", "flow_target": "sensor.thermostat_hc1_targetflowtemp",
                                         "return": "sensor.boiler_rettemp", "pump": "binary_sensor.boiler_heatingpump"})
        self.assertEqual(b["heat_pump"]["manufacturer"], "Buderus")
        self.assertEqual((b["heating_system"], c["emitter_type"]), ("radiators", "radiators"))
        params = c["heating_curve"]["manufacturer_params"]
        self.assertEqual((params["Heizkurve Auslegungstemperatur"], params["Heizkurve Parallelverschiebung"]), (54, -2))
        self.assertEqual(params["Minimale Außentemperatur (Auslegung)"], -10)
        self.assertEqual(params["Regelungsart"], "weather compensated")
        self.assertIsNone(b["design_outdoor_c"])                                     # AC29-01: never from minexttemp
        self.assertEqual(b["design_outdoor_reference"], "")
        self.assertEqual(c["heating_curve"]["points"], [])                           # AC29-02
        self.assertIsNone(c["design_flow_c"])
        self.assertIsNone(b["heat_pump"]["nominal_power_kw"])
        self.assertIsNone(b["heat_pump"]["min_flow_l_h"])
        self.assertIn("Heizkreis Vorlauf (Entität)", changed)
        rec = b["heat_source"]["autofilled"]["circuit.entities.flow"]
        self.assertEqual((rec["profile"], rec["source"], rec["circuit_id"]), (PROFILE, "ha", "hk1"))
        with server.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM config_history WHERE kind='autofill'").fetchone()[0], 1)
        self.assertEqual(self.run_autofill(), [])                                    # idempotent
        with server.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM config_history WHERE kind='autofill'").fetchone()[0], 1)

    def test_ac01_confirmed_norm_temperature_untouched(self):
        cfg = self.config()
        cfg["building"]["design_outdoor_c"] = -12.0
        cfg["building"]["design_outdoor_reference"] = "Heizlastberechnung 2025"
        server.save_v2(cfg)
        self.run_autofill()
        self.assertEqual(server.load_v2()["building"]["design_outdoor_c"], -12.0)

    def test_ac03_unknown_heating_type_stays_unknown(self):
        self.plant["select.thermostat_hc1_heatingtype"] = "convector"
        server.save_v2(self.config())
        self.run_autofill()
        self.assertIsNone(server.load_v2()["circuits"][0]["emitter_type"])
        self.assertEqual(self.status()["circuit.emitter_type"]["status"], "unmapped")

    # ------------------------------------------------------------------ AC29-04, 08, 11
    def test_ac04_manual_change_kept_other_auto_values_follow_device(self):
        t0 = server.utcnow()
        server.save_v2(self.config())
        self.run_autofill(t0)
        cfg = server.load_v2()
        cfg["circuits"][0]["entities"]["flow"] = "sensor.eigener_vorlauf"            # user changes an auto value
        self.save_ui(cfg)
        self.plant["number.thermostat_hc1_offsettemp"] = "-1.5"                       # installer changes the controller
        t1 = t0 + timedelta(days=2)
        changed = self.run_autofill(t1)
        cfg = server.load_v2()
        c = cfg["circuits"][0]
        self.assertEqual(c["entities"]["flow"], "sensor.eigener_vorlauf")
        self.assertEqual(c["heating_curve"]["manufacturer_params"]["Heizkurve Parallelverschiebung"], -1.5)
        self.assertEqual(set(changed), {"Heizkurve laut Regler", "Quelle Heizkurve"})
        self.assertIn(server.local_date(t1), c["heating_curve"]["reference"])          # source date follows the change
        rec = cfg["building"]["heat_source"]["autofilled"][devices.PARAMS_PATH]
        self.assertEqual(rec["changed_at"], t1.isoformat())
        self.assertEqual(rec["read_at"], t0.isoformat())
        st = self.status()
        self.assertEqual(st["circuit.entities.flow"]["status"], "manual")
        self.assertEqual(st["circuit.entities.flow"]["text"], "Manuell hinterlegter Wert bleibt bestehen")
        self.assertEqual(st[devices.PARAMS_PATH]["status"], "auto")

    def test_ac08_11_curve_change_keeps_acquisition_and_running_job(self):
        cfg = self.config()
        cfg["rooms"].append({"id": "wz", "name": "Wohnzimmer", "target": 21, "area": None, "default_circuit_id": "hk1",
                             "temperature_sensors": ["sensor.boiler_hptc1"], "window_sensors": [],
                             "heat_load": model.empty_heat_load()})
        server.save_v2(cfg)
        self.run_autofill()
        job = server.start_session({"name": "Test", "confirmed": True})
        self.plant["number.thermostat_hc1_offsettemp"] = "-1.0"
        self.run_autofill()                                     # sample, then autofill with a curve change only
        with server.connect() as conn:
            acq = conn.execute("SELECT last_success FROM acquisition").fetchall()
            self.assertEqual(len(acq), 1)                                                  # not reset: same entities
            self.assertIsNotNone(acq[0][0])
            self.assertEqual(conn.execute("SELECT kind FROM config_history ORDER BY id DESC LIMIT 1").fetchone()[0], "autofill")
            snap = conn.execute("SELECT config_start FROM sessions WHERE id=?", (job["id"],)).fetchone()[0]
        self.assertIn('"Heizkurve Parallelverschiebung": -2', snap)                    # start snapshot not rewritten
        self.assertEqual(server.load_v2()["circuits"][0]["heating_curve"]["manufacturer_params"]["Heizkurve Parallelverschiebung"], -1)
        hist = server.session_history(job["id"])
        self.assertEqual([cfg["circuits"][0]["heating_curve"]["manufacturer_params"]["Heizkurve Parallelverschiebung"]
                          for _, cfg in hist][-1], -1)                                   # change stays time-bound in the job

    # ------------------------------------------------------------------ AC29-05
    def test_ac05_switch_to_rest_removes_old_auto_entities_keeps_manual(self):
        server.save_v2(self.config())
        self.run_autofill()
        cfg = server.load_v2()
        cfg["building"]["heat_pump"]["manufacturer"] = "Buderus (Typenschild)"        # manual
        self.save_ui(cfg)
        cfg = server.load_v2()
        cfg["building"]["heat_source"].update(source="emsesp_rest", emsesp_host="192.168.178.69")
        with self.assertRaises(ValueError) as e:
            self.save_ui(cfg)
        self.assertIn("bestätigen", str(e.exception))
        self.assertEqual(server.load_v2()["building"]["heat_source"]["source"], "ha")  # nothing saved
        cfg["building"]["heat_source"]["switch_confirmed"] = True
        self.save_ui(cfg)
        cfg = server.load_v2()
        self.assertEqual(cfg["circuits"][0]["entities"], {"flow": "", "flow_target": "", "return": "", "pump": ""})
        self.assertEqual(cfg["building"]["entities"]["outdoor"], "")
        self.assertEqual(cfg["building"]["heat_pump"]["manufacturer"], "Buderus (Typenschild)")
        self.assertNotIn("switch_confirmed", cfg["building"]["heat_source"])
        self.assertEqual(cfg["building"]["heat_source"]["autofilled"], {})
        values = {"curve_design": {"entity_id": "emsesp.x", "value": "54"}}
        devices.autofill(cfg, values, "2026-11-27T12:00:00+00:00", "27.11.2026")
        self.assertEqual(cfg["circuits"][0]["entities"]["flow"], "")                   # REST: no HA entity
        rec = cfg["building"]["heat_source"]["autofilled"][devices.PARAMS_PATH]
        self.assertEqual(rec["source"], "emsesp_rest")
        st = {f["path"]: f for f in devices.autofill_status(cfg, values)}
        self.assertEqual(st["circuit.entities.flow"]["status"], "rest_no_entity")
        self.assertEqual(st["building.heat_pump.manufacturer"]["status"], "unclear")   # differs, no record

    def test_ac05_switch_a_b_a(self):
        server.save_v2(self.config())
        self.run_autofill()
        for src in ("emsesp_rest", "ha"):
            cfg = server.load_v2()
            cfg["building"]["heat_source"].update(source=src, emsesp_host="192.168.178.69" if src != "ha" else "",
                                                  switch_confirmed=True)
            self.save_ui(cfg)
            if src == "ha":
                self.run_autofill()
        cfg = server.load_v2()
        self.assertEqual(cfg["circuits"][0]["entities"]["flow"], "sensor.boiler_curflowtemp")
        self.assertEqual(cfg["building"]["heat_source"]["autofilled"]["circuit.entities.flow"]["source"], "ha")
        self.assertEqual(self.status()["circuit.entities.flow"]["status"], "auto")

    def test_ac05_stale_records_never_refreshed_without_switch(self):
        server.save_v2(self.config())
        self.run_autofill()
        cfg = server.load_v2()
        for rec in cfg["building"]["heat_source"]["autofilled"].values():   # records of another source (e.g. edited elsewhere)
            rec["source"] = "emsesp_rest"
        server.save_v2(cfg)
        self.plant["number.thermostat_hc1_offsettemp"] = "-1.0"
        self.run_autofill()
        cfg = server.load_v2()
        self.assertEqual(cfg["circuits"][0]["heating_curve"]["manufacturer_params"]["Heizkurve Parallelverschiebung"], -2)
        self.assertEqual(self.status()["circuit.entities.flow"]["status"], "stale_source")

    # ------------------------------------------------------------------ AC29-06
    def test_ac06_multi_circuit_only_bound_circuit(self):
        cfg = self.config()
        fbh = model.empty_circuit("hk2", "Fußbodenheizung")
        cfg["circuits"] = [fbh] + cfg["circuits"]                                       # HK1 not first
        server.save_v2(cfg)
        self.run_autofill()
        cfg = server.load_v2()
        for c in cfg["circuits"]:
            self.assertEqual(c["entities"]["flow"], "")                                 # not bound -> nothing
        self.assertIsNone(cfg["building"]["heating_system"])
        st = self.status()
        self.assertEqual(st["circuit.entities.flow"]["status"], "no_circuit")
        self.assertEqual(st["building.heating_system"]["status"], "multi_circuit")
        self.assertEqual(cfg["building"]["entities"]["outdoor"], "sensor.boiler_outdoortemp")   # building values still fine
        cfg["building"]["heat_source"].update(circuit_id="hk1", switch_confirmed=True)
        self.save_ui(cfg)
        self.run_autofill()
        cfg = server.load_v2()
        hk = {c["id"]: c for c in cfg["circuits"]}
        self.assertEqual(hk["hk1"]["entities"]["flow"], "sensor.boiler_curflowtemp")
        self.assertEqual(hk["hk1"]["emitter_type"], "radiators")
        self.assertEqual(hk["hk2"]["entities"]["flow"], "")
        self.assertIsNone(hk["hk2"]["emitter_type"])
        self.assertIsNone(cfg["building"]["heating_system"])                            # not derived from one circuit

    # ------------------------------------------------------------------ AC29-07, 09, 12
    def test_ac07_status_persistent_after_second_poll_and_restart(self):
        server.save_v2(self.config())
        self.run_autofill()
        self.run_autofill()
        server.AUTOFILL_STATUS.update(at=None, changed=[])                               # restart: RAM state gone
        st = self.status()
        self.assertEqual(st["circuit.entities.flow"]["status"], "auto")
        self.assertIsNotNone(st["circuit.entities.flow"]["changed_at"])
        self.plant["sensor.boiler_rettemp"] = "unavailable"
        cfg = server.load_v2()
        cfg["circuits"][0]["entities"]["return"] = ""
        server.save_v2(cfg)
        with patch.object(server, "get_entities", side_effect=self.states):
            server.sample()
        st = self.status()
        self.assertEqual(st["circuit.entities.return"]["status"], "unavailable")
        self.assertEqual(st["circuit.entities.return"]["text"], "Derzeit nicht erreichbar / kein gültiger Messwert")
        self.assertEqual(st["building.heat_pump.manufacturer"]["status"], "auto")

    def test_ac12_unclear_origin_vs_matching_value(self):
        cfg = self.config()
        cfg["circuits"][0]["entities"]["flow"] = "sensor.irgendwas"                   # existing value, no record
        cfg["circuits"][0]["entities"]["return"] = "sensor.boiler_rettemp"             # same as device
        server.save_v2(cfg)
        self.run_autofill()
        st = self.status()
        self.assertEqual(st["circuit.entities.flow"]["status"], "unclear")
        self.assertEqual(st["circuit.entities.flow"]["text"], "Herkunft ungeklärt / Abgleich erforderlich")
        self.assertEqual(st["circuit.entities.return"]["status"], "auto")
        self.assertEqual(server.load_v2()["circuits"][0]["entities"]["flow"], "sensor.irgendwas")

    def test_ac09_no_profile_and_no_write_path(self):
        server.save_v2(model.empty())
        with patch.object(server, "get_entities", side_effect=self.states):
            self.assertIsNone(server.autofill_from_source())
        source = Path(server.__file__).read_text() + Path(devices.__file__).read_text()
        for word in ("/api/services", "call_service", "mqtt.publish"):
            self.assertNotIn(word, source)
        self.assertFalse(devices.WRITE_ENABLED)


if __name__ == "__main__":
    unittest.main()
