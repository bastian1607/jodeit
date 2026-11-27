"""AB-015: read-only device profile Buderus / EMS-ESP / HMC310 (HA source and REST source)."""
import csv
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import devices  # noqa: E402
import model  # noqa: E402
import server  # noqa: E402

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "emsesp_thermostat_hmc310.json").read_text())
PROFILE = "buderus-emsesp-hmc310"
# Entity ids confirmed in Bastian's Home Assistant on 2026-11-27 (all 30 present).
VERIFIED_HA_IDS = {
    "binary_sensor.boiler_dhw_charging", "binary_sensor.boiler_heatingactive", "binary_sensor.boiler_heatingpump",
    "number.thermostat_dhw_disinfecttime", "number.thermostat_hc1_baseflowtemp", "number.thermostat_hc1_designtemp",
    "number.thermostat_hc1_hpminflowtemp", "number.thermostat_hc1_maxflowtemp", "number.thermostat_hc1_offsettemp",
    "number.thermostat_hc1_roominfluence", "number.thermostat_hc1_seltemp", "number.thermostat_hc1_solarinfl",
    "number.thermostat_minexttemp", "select.thermostat_building", "select.thermostat_dhw_disinfectday",
    "select.thermostat_hc1_controlmode", "select.thermostat_hc1_heatingtype", "select.thermostat_hc1_mode",
    "select.thermostat_hc1_reducemode", "sensor.boiler_curflowtemp", "sensor.boiler_hpactivity", "sensor.boiler_hptc0",
    "sensor.boiler_hptc1", "sensor.boiler_outdoortemp", "sensor.boiler_rettemp", "sensor.thermostat_dampedoutdoortemp",
    "sensor.thermostat_hc1_targetflowtemp", "sensor.thermostat_lastcode", "switch.thermostat_damping",
    "switch.thermostat_dhw_disinfecting"}
BOILER = {"outdoortemp": -4.3, "curflowtemp": 45, "rettemp": 42.8, "hptc1": 50.4, "hptc0": 42.8,
          "heatingactive": "on", "heatingpump": "on", "hpactivity": "heating", "dhw": {"charging": "off"}}


def fake_fetch(boiler=True):
    def fetch(host, device):
        if device == "thermostat":
            return FIXTURE
        if boiler:
            return BOILER
        raise OSError("boiler not reachable")
    return fetch


class ProfileTests(unittest.TestCase):
    def test_ha_ids_match_real_installation(self):
        self.assertEqual(set(devices.entities({"profile": PROFILE, "source": "ha"})), VERIFIED_HA_IDS)

    def test_rest_values_match_ha_values_of_test_plant(self):
        states, errors = devices.read_rest({"profile": PROFILE, "source": "emsesp_rest", "emsesp_host": "192.168.178.69"},
                                           "2026-11-27T08:30:00+00:00", fake_fetch())
        self.assertEqual(errors, {})
        v = {sid.split(".", 1)[1]: s["state"] for sid, s in states.items()}
        self.assertEqual(v["thermostat_hc1_designtemp"], "54")
        self.assertEqual(v["thermostat_hc1_offsettemp"], "-2")
        self.assertEqual(v["thermostat_hc1_baseflowtemp"], "25")
        self.assertEqual(v["thermostat_hc1_maxflowtemp"], "52")
        self.assertEqual(v["thermostat_minexttemp"], "-10")
        self.assertEqual(v["thermostat_hc1_targetflowtemp"], "47")
        self.assertEqual(v["thermostat_dampedoutdoortemp"], "-5.6")
        self.assertEqual(v["thermostat_hc1_controlmode"], "weather compensated")
        self.assertEqual(v["thermostat_dhw_disinfectday"], "mo")
        self.assertEqual(v["boiler_dhw_charging"], "off")

    def test_boiler_failure_only_affects_boiler_fields(self):
        states, errors = devices.read_rest({"profile": PROFILE, "source": "emsesp_rest", "emsesp_host": "192.168.178.69"},
                                           "x", fake_fetch(boiler=False))
        self.assertEqual(errors, {"boiler": "OSError"})
        self.assertEqual(states["emsesp.boiler_curflowtemp"]["state"], "unavailable")
        self.assertEqual(states["emsesp.thermostat_hc1_designtemp"]["state"], "54")

    def test_measurement_temperatures_are_stale_checked_settings_are_not(self):
        temps = devices.temperature_entities({"profile": PROFILE, "source": "ha"})
        self.assertIn("sensor.boiler_curflowtemp", temps)
        self.assertNotIn("number.thermostat_hc1_designtemp", temps)
        self.assertNotIn("sensor.thermostat_hc1_targetflowtemp", temps)  # published on change only

    def test_host_validation(self):
        for ok, expect in (("192.168.178.69", "192.168.178.69"), ("http://192.168.178.69/", "192.168.178.69"),
                           ("10.0.0.5:8080", "10.0.0.5:8080"), ("ems-esp", "ems-esp"), ("EMS-ESP.local", "ems-esp.local")):
            self.assertEqual(devices.normalise_host(ok), expect)
        for bad in ("8.8.8.8", "example.com", "127.0.0.1", "192.168.1.1/../x", "::1", "192.168.1.1:99999", "", "a b"):
            with self.assertRaises(ValueError, msg=bad):
                devices.normalise_host(bad)


class ReadOnlyTests(unittest.TestCase):
    def test_no_write_capability(self):
        self.assertFalse(devices.WRITE_ENABLED)
        ids = {f["object_id"] for f in devices.PROFILES[PROFILE]["fields"]}
        self.assertNotIn("thermostat_hc1_remotetemp", ids)
        self.assertNotIn("thermostat_hc1_control", ids)
        self.assertIn("thermostat_hc1_remotetemp", devices.FORBIDDEN_WRITE)
        for name in ("devices.py", "server.py"):
            source = (Path(__file__).resolve().parents[1] / "app" / name).read_text()
            for marker in ("/api/services", "publish(", 'method="POST"', "method='POST'"):
                self.assertNotIn(marker, source, f"{marker} in {name}")

    def test_fetch_uses_get_without_body(self):
        calls = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n): return json.dumps(FIXTURE).encode()

        def opener(request, timeout):
            calls.append((request.get_method(), request.full_url, request.data, timeout))
            return Response()
        data = devices.fetch_device("192.168.178.69", "thermostat", opener=opener)
        self.assertEqual(data["hc1"]["designtemp"], 54)
        self.assertEqual(calls, [("GET", "http://192.168.178.69/api/thermostat", None, 10)])
        with self.assertRaises(ValueError):
            devices.fetch_device("192.168.178.69", "system/restart", opener=opener)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = server.DB
        server.DB = str(Path(self.tmp.name) / "hs.db")
        server.setup()

    def tearDown(self):
        server.DB = self.old_db
        self.tmp.cleanup()

    def config(self, source="emsesp_rest", host="192.168.178.69"):
        cfg = model.empty()
        cfg["rooms"].append({"id": "wz", "name": "Wohnzimmer", "target": 21, "area": None, "default_circuit_id": "hk1",
                             "temperature_sensors": ["sensor.wz"], "window_sensors": ["binary_sensor.fenster"],
                             "heat_load": model.empty_heat_load()})
        cfg["building"]["heat_source"] = {"profile": PROFILE, "source": source, "emsesp_host": host}
        return cfg

    def ha_states(self, with_profile=False):
        now = server.utcnow().isoformat()
        out = [{"entity_id": "sensor.wz", "state": "21.4", "unit": "°C", "source_at": now},
               {"entity_id": "binary_sensor.fenster", "state": "off", "unit": "", "source_at": now}]
        if with_profile:
            out += [{"entity_id": "number.thermostat_hc1_designtemp", "state": "54", "unit": "°C", "source_at": now},
                    {"entity_id": "sensor.boiler_curflowtemp", "state": "45", "unit": "°C", "source_at": now}]
        return out

    def test_validation(self):
        self.assertEqual(model.validate(self.config())["building"]["heat_source"]["emsesp_host"], "192.168.178.69")
        with self.assertRaises(ValueError):
            model.validate(self.config(host=""))
        with self.assertRaises(ValueError):
            model.validate(self.config(host="1.1.1.1"))
        old = model.empty()
        del old["building"]["heat_source"]   # configuration saved by v0.2.1
        model.validate(old)

    def test_rest_sampling_and_snapshot(self):
        server.save_v2(self.config())
        with patch.object(server, "get_entities", return_value=self.ha_states()), \
             patch.object(devices, "fetch_device", side_effect=fake_fetch()):
            server.sample()
        snap = server.heat_source_snapshot()
        by = {f["key"]: f for f in snap["fields"]}
        self.assertEqual(by["curve_design"]["value"], "54")
        self.assertEqual(by["flow"]["value"], 45.0)          # measurement, normalised °C
        self.assertTrue(by["flow"]["valid"])
        self.assertEqual(snap["rooms"][0]["temperature"], 21.4)
        self.assertFalse(snap["rooms"][0]["window_open"])
        self.assertTrue(snap["read_only"])
        report = server.data_quality(24)
        self.assertIn("emsesp.thermostat_hc1_designtemp", [e["entity_id"] for e in report["entities"]])

    def test_rest_down_does_not_stop_room_measurement(self):
        server.save_v2(self.config())
        def down(host, device):
            raise OSError("no route")
        with patch.object(server, "get_entities", return_value=self.ha_states()), \
             patch.object(devices, "fetch_device", side_effect=down):
            server.sample()
        obs = {o["entity_id"]: o for o in server.latest_observations()}
        self.assertTrue(obs["sensor.wz"]["valid"])
        self.assertFalse(obs["emsesp.boiler_curflowtemp"]["valid"])
        self.assertEqual(server.heat_source_snapshot()["source_errors"], {"boiler": "OSError", "thermostat": "OSError"})

    def test_ha_source_sampling_and_csv(self):
        server.save_v2(self.config(source="ha", host=""))
        with patch.object(server, "get_entities", return_value=self.ha_states(with_profile=True)):
            server.sample()
        by = {f["key"]: f for f in server.heat_source_snapshot()["fields"]}
        self.assertEqual(by["curve_design"]["value"], "54")
        self.assertIsNone(by["curve_base"]["value"])            # not delivered -> no invented value
        rows = list(csv.reader(io.StringIO(server.export_csv()), delimiter=";"))
        self.assertEqual(rows[0][:3], ["zeit_utc", "entity_id", "bezeichnung"])
        labels = {r[1]: r[2] for r in rows[1:]}
        self.assertEqual(labels["sensor.wz"], "Raum Wohnzimmer Temperatur")
        self.assertEqual(labels["number.thermostat_hc1_designtemp"], "Heizkurve Auslegungstemperatur")

    def test_probe_endpoint_and_profiles(self):
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.API)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_port}"
        try:
            profiles = json.load(urlopen(base + "/api/v2/heat-source/profiles"))
            self.assertFalse(profiles["write_enabled"])
            self.assertEqual(profiles["profiles"][0]["key"], PROFILE)
            body = json.dumps({"profile": PROFILE, "source": "emsesp_rest", "emsesp_host": "192.168.178.69"}).encode()
            with patch.object(devices, "fetch_device", side_effect=fake_fetch()):
                result = json.load(urlopen(Request(base + "/api/v2/heat-source/test", body, {"Content-Type": "application/json"})))
            self.assertEqual(result["found"], result["total"])
            with server.connect() as c:   # probe stores nothing
                self.assertEqual(c.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 0)
            csv_resp = urlopen(base + "/api/v2/export.csv")
            self.assertIn("text/csv", csv_resp.headers["Content-Type"])
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()


class DetectTests(unittest.TestCase):
    """AB-022: profile proposal from existing HA entities (read-only)."""

    def states(self, keys=None, drop=()):
        out = {}
        for sid, f in devices.entities({"profile": PROFILE, "source": "ha"}).items():
            if (keys is None or f["key"] in keys) and f["key"] not in drop:
                out[sid] = {"entity_id": sid, "state": "1"}
        return out

    def test_full_match_is_proposed(self):
        r = devices.detect(self.states())
        self.assertEqual(r["candidates"][0]["profile"], PROFILE)
        self.assertTrue(r["candidates"][0]["proposed"])
        self.assertEqual((r["candidates"][0]["found"], r["candidates"][0]["total"]), (30, 30))

    def test_some_missing_non_core_fields_still_proposed(self):
        r = devices.detect(self.states(drop=("hp_flow_tc1", "hp_return_tc0", "last_code")))
        self.assertTrue(r["candidates"][0]["proposed"])

    def test_missing_core_field_or_partial_match_is_not_proposed(self):
        r = devices.detect(self.states(drop=("outdoor_damped",)))
        self.assertFalse(r["candidates"][0]["proposed"])
        self.assertEqual(r["candidates"][0]["core_missing"], ["outdoor_damped"])
        r = devices.detect(self.states(keys=("outdoor", "flow", "curve_design", "curve_offset", "flow_target", "outdoor_damped")))
        self.assertFalse(r["candidates"][0]["proposed"])     # 6 of 30

    def test_nothing_found_no_candidates(self):
        self.assertEqual(devices.detect({})["candidates"], [])
