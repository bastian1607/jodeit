"""AB-008: schema v2, v1 compatibility, start-up migration with backup, API."""
import copy
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import model  # noqa: E402
import server  # noqa: E402
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

RECENT = (_dt.now(_tz.utc) - _td(days=1)).replace(microsecond=0).isoformat()


def v1_config():
    """Realistic synthetic v0.1.2 configuration (no customer data)."""
    return {
        "rooms": [
            {"id": "wohnen", "name": "Wohnen", "target": 21, "area": "32.5", "heat_load": "1450",
             "temperature_sensors": ["sensor.wohnen_t", "sensor.kueche_t"], "window_sensors": ["binary_sensor.terrasse"],
             "radiators": [
                 {"name": "Fenster", "entity": "number.hk_fenster", "valve_model": "Eclipse DN15", "power_w": "1200"},
                 {"name": "Wand", "entity": "", "valve_model": "", "power_w": None},
             ]},
            {"id": "kueche", "name": "Küche", "target": 20, "temperature_sensors": ["sensor.kueche2_t"],
             "window_sensors": [], "radiators": []},
            {"id": "bad", "name": "Bad", "target": 24, "temperature_sensors": ["sensor.bad_t"],
             "window_sensors": [], "radiators": [{"name": "Handtuch", "entity": "climate.bad", "valve_model": "", "power_w": 500}]},
        ],
        "zones": [{"id": "z1", "name": "Wohnküche", "room_ids": ["wohnen", "kueche"]}],
        "heating": {"manufacturer": "Buderus", "model": "WLW186i", "circuit_name": "HK1", "outdoor": "sensor.aussen",
                    "flow": "sensor.vl", "return": "sensor.rl", "compressor": "binary_sensor.verdichter", "dhw": "sensor.ww"},
    }


def adopted(value):
    return {"status": "adopted", "value_w": value, "method": "DIN EN 12831-1 / DIN/TS 12831-1",
            "reference": "Heizlastberechnung Software X", "author": "Fachplaner", "date": "2026-10-01",
            "legacy_value_w": None}


class ConversionTests(unittest.TestCase):
    def test_from_v1_preserves_data_and_marks_unknown_origin(self):
        cfg = model.from_v1(v1_config())
        self.assertEqual(cfg["schema_version"], 2)
        self.assertEqual([r["id"] for r in cfg["rooms"]], ["wohnen", "kueche", "bad"])
        wohnen = cfg["rooms"][0]
        self.assertEqual(wohnen["heat_load"]["status"], "missing")
        self.assertIsNone(wohnen["heat_load"]["value_w"])
        self.assertEqual(wohnen["heat_load"]["legacy_value_w"], 1450)
        self.assertEqual(wohnen["area"], 32.5)
        self.assertEqual(len(cfg["emitters"]), 3)
        first = cfg["emitters"][0]
        self.assertEqual((first["room_id"], first["circuit_id"], first["actuator_entity"]), ("wohnen", "hk1", "number.hk_fenster"))
        self.assertEqual(first["rated_output"]["value_w"], 1200)
        self.assertIsNone(first["rated_output"]["flow_c"])
        self.assertEqual(first["rated_output"]["source"], "unknown")
        self.assertEqual(first["valve"]["label"], "Eclipse DN15")
        self.assertEqual(cfg["circuits"][0]["name"], "HK1")
        self.assertEqual(cfg["circuits"][0]["entities"]["flow"], "sensor.vl")
        self.assertEqual(cfg["building"]["entities"]["compressor"], "binary_sensor.verdichter")
        model.validate(cfg)

    def test_v1_projection_roundtrip(self):
        original = v1_config()
        projected = model.to_v1(model.from_v1(original))
        self.assertEqual(projected["heating"], original["heating"])
        self.assertEqual(projected["zones"], original["zones"])
        for before, after in zip(original["rooms"], projected["rooms"]):
            self.assertEqual(after["temperature_sensors"], before["temperature_sensors"])
            self.assertEqual([r["name"] for r in after["radiators"]], [r["name"] for r in before["radiators"]])
            self.assertTrue(all(r["id"].startswith("hf_") for r in after["radiators"]))
        server.validate(projected)  # projection is valid for the legacy validator

    def test_lenient_conversion_of_broken_legacy_data(self):
        cfg = model.from_v1({"rooms": [None, {"id": "x", "radiators": "kaputt", "target": "abc"}], "zones": "x", "heating": None})
        self.assertEqual(len(cfg["rooms"]), 1)
        self.assertEqual(cfg["rooms"][0]["target"], 21)
        self.assertEqual(cfg["emitters"], [])

    def enriched(self):
        cfg = model.from_v1(v1_config())
        cfg["circuits"].append(dict(model.empty_circuit("hk2", "FBH"), emitter_type="underfloor"))
        cfg["rooms"][0]["heat_load"] = adopted(1500)
        e = cfg["emitters"][0]
        e["circuit_id"] = "hk2"
        e["rated_output"].update(flow_c=75, return_c=65, room_c=20, source="manufacturer", reference="Katalog")
        e["valve"].update(manufacturer="IMI Heimeier", series="Eclipse")
        cfg["emitters"][0]["load_share_percent"] = 70
        cfg["emitters"][1]["load_share_percent"] = 30
        return model.validate(cfg)

    def test_legacy_ui_save_keeps_v2_fields(self):
        cfg = self.enriched()
        merged = model.merge_v1(cfg, model.to_v1(cfg))
        self.assertEqual(merged, cfg)

    def test_legacy_ui_changes_drop_documented_status(self):
        cfg = self.enriched()
        v1 = model.to_v1(cfg)
        v1["rooms"][0]["heat_load"] = 1600
        v1["rooms"][0]["radiators"][0]["power_w"] = "1300"
        merged = model.merge_v1(cfg, v1)
        self.assertEqual(merged["rooms"][0]["heat_load"]["status"], "missing")
        self.assertEqual(merged["rooms"][0]["heat_load"]["legacy_value_w"], 1600)
        emitter = merged["emitters"][0]
        self.assertEqual(emitter["rated_output"]["value_w"], 1300)
        self.assertIsNone(emitter["rated_output"]["flow_c"])
        self.assertEqual(emitter["circuit_id"], "hk2")          # hydraulic assignment survives
        self.assertEqual(emitter["valve"]["series"], "Eclipse")
        model.validate(merged)

    def test_removing_middle_radiator_keeps_identity(self):
        cfg = model.from_v1(v1_config())
        cfg["emitters"][1]["valve"]["series"] = "V-exact II"
        v1 = model.to_v1(cfg)
        removed_id = v1["rooms"][0]["radiators"][0]["id"]
        del v1["rooms"][0]["radiators"][0]
        merged = model.merge_v1(cfg, v1)
        ids = [e["id"] for e in merged["emitters"]]
        self.assertNotIn(removed_id, ids)
        wand = next(e for e in merged["emitters"] if e["name"] == "Wand")
        self.assertEqual(wand["valve"]["series"], "V-exact II")

    def test_match_without_id_uses_unique_name_and_entity(self):
        cfg = model.from_v1(v1_config())
        cfg["emitters"][0]["valve"]["series"] = "Eclipse"
        v1 = model.to_v1(cfg)
        for r in v1["rooms"][0]["radiators"]:
            r.pop("id")
        merged = model.merge_v1(cfg, v1)
        self.assertEqual(merged["emitters"][0]["id"], cfg["emitters"][0]["id"])
        self.assertEqual(merged["emitters"][0]["valve"]["series"], "Eclipse")

    def test_new_room_from_legacy_ui_uses_default_circuit(self):
        cfg = model.from_v1(v1_config())
        v1 = model.to_v1(cfg)
        v1["rooms"].append({"id": "neu", "name": "Neu", "target": 20, "temperature_sensors": ["sensor.neu"],
                            "window_sensors": [], "radiators": [{"name": "HK", "entity": "", "valve_model": "", "power_w": None}]})
        merged = model.merge_v1(cfg, v1)
        self.assertEqual(merged["rooms"][-1]["default_circuit_id"], "hk1")
        self.assertEqual(merged["emitters"][-1]["circuit_id"], "hk1")


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = model.from_v1(v1_config())

    def assertInvalid(self, mutate, fragment=None):
        cfg = copy.deepcopy(self.cfg)
        mutate(cfg)
        with self.assertRaises(model.Invalid) as caught:
            model.validate(cfg)
        if fragment:
            self.assertIn(fragment, str(caught.exception))

    def test_adopted_heat_load_requires_documentation(self):
        for key in ("method", "reference"):
            def mutate(c, key=key):
                c["rooms"][0]["heat_load"] = adopted(1500)
                c["rooms"][0]["heat_load"][key] = " "
            self.assertInvalid(mutate, "fehlt")
        self.assertInvalid(lambda c: c["rooms"][0].update(heat_load=dict(adopted(1500), date="01.10.2026")), "Datum")
        self.assertInvalid(lambda c: c["rooms"][0].update(heat_load=dict(adopted(None))), "Raumheizlast")

    def test_no_heat_load_value_without_adoption(self):
        self.assertInvalid(lambda c: c["rooms"][0]["heat_load"].update(value_w=1200), "dokumentierter")

    def test_no_estimated_status(self):
        self.assertInvalid(lambda c: c["rooms"][0]["heat_load"].update(status="estimated"), "Heizlaststatus")

    def test_references(self):
        self.assertInvalid(lambda c: c["emitters"][0].update(circuit_id="hk9"), "Heizkreis")
        self.assertInvalid(lambda c: c["emitters"][0].update(room_id="nirgends"), "Raum")
        self.assertInvalid(lambda c: c["rooms"][0].update(default_circuit_id="hk9"), "Heizkreis")
        self.assertInvalid(lambda c: c["circuits"].append(model.empty_circuit()), "doppelt")
        self.assertInvalid(lambda c: c["emitters"].append(copy.deepcopy(c["emitters"][0])), "doppelt")

    def test_rated_output_reference_temperatures(self):
        self.assertInvalid(lambda c: c["emitters"][0]["rated_output"].update(flow_c=75), "vollständig")
        self.assertInvalid(lambda c: c["emitters"][0]["rated_output"].update(flow_c=60, return_c=65, room_c=20), "Vorlauf > Rücklauf")
        cfg = copy.deepcopy(self.cfg)
        cfg["emitters"][0]["rated_output"].update(flow_c=75, return_c=65, room_c=20)
        model.validate(cfg)

    def test_load_shares(self):
        self.assertInvalid(lambda c: c["emitters"][0].update(load_share_percent=60), "Summe 100")
        def partial_sum(c):
            c["emitters"][0]["load_share_percent"] = 60
            c["emitters"][1]["load_share_percent"] = 30
        self.assertInvalid(partial_sum, "Summe 100")
        cfg = copy.deepcopy(self.cfg)
        cfg["emitters"][0]["load_share_percent"] = 60
        cfg["emitters"][1]["load_share_percent"] = 40
        model.validate(cfg)

    def test_circuit_design_and_curve(self):
        self.assertInvalid(lambda c: c["circuits"][0].update(design_flow_c=45), "gemeinsam")
        self.assertInvalid(lambda c: c["circuits"][0].update(design_flow_c=40, design_return_c=45), "über dem Rücklauf")
        self.assertInvalid(lambda c: c["circuits"][0]["heating_curve"].update(points=[{"outdoor_c": -10, "flow_c": 45}]), "Stützpunkte")
        self.assertInvalid(lambda c: c["circuits"][0]["heating_curve"].update(points=[{"outdoor_c": -10, "flow_c": 45}, {"outdoor_c": -10, "flow_c": 35}]), "doppelt")
        cfg = copy.deepcopy(self.cfg)
        cfg["circuits"][0].update(design_flow_c=50, design_return_c=40)
        cfg["circuits"][0]["heating_curve"].update(points=[{"outdoor_c": -12, "flow_c": 50}, {"outdoor_c": 20, "flow_c": 25}],
                                                   manufacturer_params={"Fußpunkt": 25, "Endpunkt": 50})
        model.validate(cfg)

    def test_min_flow_needs_source_and_entities_are_read_only_domains(self):
        self.assertInvalid(lambda c: c["building"]["heat_pump"].update(min_flow_l_h=900), "Quelle")
        self.assertInvalid(lambda c: c["emitters"][0].update(actuator_entity="switch.ventil"), "Stellantrieb")
        self.assertInvalid(lambda c: c["circuits"][0]["entities"].update(pump="switch.pumpe"), "Heizkreispumpe")
        self.assertInvalid(lambda c: c["building"].update(heating_system="fernwaerme"), "Heizsystem")

    def test_calculation_blockers(self):
        blockers = model.calculation_blockers(self.cfg)
        self.assertIn("Heizlast fehlt", blockers["wohnen"])
        self.assertIn("Keine Heizfläche erfasst", blockers["kueche"])
        self.assertIn("Heizlastanteile der Heizflächen fehlen", blockers["wohnen"])
        self.assertIn("Heizkörperleistung ohne Referenztemperaturen", blockers["bad"])
        self.assertIn("Auslegungstemperaturen des Heizkreises fehlen", blockers["bad"])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = server.DB
        server.DB = os.path.join(self.tmp.name, "autobalance.db")

    def tearDown(self):
        server.DB = self.old_db
        self.tmp.cleanup()

    def v012_database(self, cfg):
        """Database as written by v0.1.2 (config row + samples + acquisition), WAL mode."""
        with sqlite3.connect(server.DB) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("CREATE TABLE config (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
            c.execute("CREATE TABLE samples (id INTEGER PRIMARY KEY, at TEXT, entity_id TEXT, state TEXT, unit TEXT, valid INTEGER, source_at TEXT, quality TEXT, normalized_value REAL, normalized_unit TEXT)")
            c.execute("INSERT INTO config VALUES(1,?)", (json.dumps(cfg),))
            c.executemany("INSERT INTO samples(at,entity_id,state,unit,valid) VALUES(?,?,?,?,?)",
                          [(RECENT, "sensor.wohnen_t", "20.5", "°C", 1)] * 50)   # recent: AB-026 purges loose data > 30 days

    def backups(self):
        folder = Path(server.DB).parent / "backups"
        return sorted(folder.glob("*.db")) if folder.exists() else []

    def test_fresh_install_has_empty_v2_and_no_backup(self):
        server.setup()
        self.assertEqual(server.load_v2(), model.empty())
        self.assertEqual(self.backups(), [])

    def test_migration_from_v012_with_backup_and_idempotence(self):
        original = v1_config()
        self.v012_database(original)
        server.setup()
        cfg = server.load_v2()
        self.assertEqual(cfg["schema_version"], 2)
        self.assertEqual(len(cfg["emitters"]), 3)
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as c:
            self.assertEqual(json.loads(c.execute("SELECT value FROM config").fetchone()[0]), original)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 50)
        with server.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 50)  # samples untouched
            migration = c.execute("SELECT version,kind,backup_path FROM schema_migrations").fetchall()
        self.assertEqual(migration, [(2, "v1_to_v2", str(backups[0]))])
        emitter_ids = [e["id"] for e in cfg["emitters"]]
        server.setup()
        server.setup()
        self.assertEqual([e["id"] for e in server.load_v2()["emitters"]], emitter_ids)
        self.assertEqual(len(self.backups()), 1)
        with server.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 1)

    def test_legacy_row_is_readable_for_downgrade_and_merged_back(self):
        self.v012_database(v1_config())
        server.setup()
        cfg = server.load_v2()
        cfg["rooms"][2]["heat_load"] = adopted(800)
        server.save_v2(cfg)
        with server.connect() as c:
            legacy = json.loads(c.execute("SELECT value FROM config WHERE id=1").fetchone()[0])
        server.validate(legacy)                      # v0.1.2 can still read and save it
        self.assertEqual(legacy["rooms"][2]["heat_load"], 800)
        legacy["rooms"][1]["name"] = "Küche (geändert unter v0.1.2)"
        with server.connect() as c:                  # simulate edit while downgraded
            c.execute("UPDATE config SET value=? WHERE id=1", (json.dumps(legacy),))
        server.setup()
        merged = server.load_v2()
        self.assertEqual(merged["rooms"][1]["name"], "Küche (geändert unter v0.1.2)")
        self.assertEqual(merged["rooms"][2]["heat_load"]["status"], "adopted")  # v2-only data survived
        self.assertEqual(len(self.backups()), 2)

    def test_failed_migration_leaves_database_unchanged(self):
        original = v1_config()
        self.v012_database(original)
        real_write = server._write_config

        def broken(c, cfg):
            real_write(c, cfg)
            raise sqlite3.OperationalError("disk I/O error (simulated)")
        with patch.object(server, "_write_config", side_effect=broken):
            with self.assertRaises(sqlite3.OperationalError):
                server.setup()
        with server.connect() as c:
            self.assertIsNone(c.execute("SELECT value FROM config_v2").fetchone())
            self.assertEqual(json.loads(c.execute("SELECT value FROM config").fetchone()[0]), original)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 0)
        server.setup()                               # next start succeeds
        self.assertEqual(server.load_v2()["schema_version"], 2)

    def test_unknown_future_schema_is_refused(self):
        server.setup()
        with server.connect() as c:
            c.execute("UPDATE config_v2 SET schema_version=3")
        with self.assertRaises(RuntimeError):
            server.setup()

    def test_acquisition_reads_new_circuit_entities(self):
        server.setup()
        cfg = model.from_v1(v1_config())
        cfg["circuits"][0]["entities"].update(flow_target="sensor.vl_soll", pump="binary_sensor.pumpe")
        server.save_v2(cfg)
        self.assertIn("sensor.vl_soll", server.wanted(server.load_v2()))
        self.assertIn("binary_sensor.pumpe", server.wanted(server.load_v2()))
        self.assertIn("sensor.vl_soll", server.temperature_ids(server.load_v2()))


class ApiTests(unittest.TestCase):
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

    def get(self, path):
        with urlopen(self.base + path) as response:
            return json.load(response)

    def post(self, path, payload):
        request = Request(self.base + path, json.dumps(payload).encode(), {"Content-Type": "application/json"})
        with urlopen(request) as response:
            return json.load(response)

    def test_v2_roundtrip_and_legacy_endpoint(self):
        self.assertEqual(self.get("/health")["schema_version"], 2)
        cfg = model.from_v1(v1_config())
        cfg["rooms"][0]["heat_load"] = adopted(1500)
        self.assertTrue(self.post("/api/v2/config", cfg)["saved"])
        self.assertEqual(self.get("/api/v2/config"), cfg)
        legacy = self.get("/api/config")
        self.assertEqual(legacy["rooms"][0]["heat_load"], 1500)
        self.assertTrue(self.post("/api/config", legacy)["saved"])   # legacy UI save
        self.assertEqual(self.get("/api/v2/config"), cfg)              # nothing lost
        readiness = self.get("/api/v2/readiness")["calculation_blockers"]
        self.assertNotIn("Heizlast fehlt", readiness["wohnen"])
        self.assertIn("Heizlast fehlt", readiness["bad"])

    def test_invalid_v2_is_rejected_and_not_stored(self):
        cfg = model.from_v1(v1_config())
        cfg["rooms"][0]["heat_load"]["value_w"] = 1500   # number without documented adoption
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/v2/config", cfg)
        self.assertEqual(caught.exception.code, 400)
        self.assertIn("dokumentierter", json.load(caught.exception)["error"])
        caught.exception.close()
        self.assertEqual(self.get("/api/v2/config"), model.empty())
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/v2/config", ["kein", "objekt"])
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()


if __name__ == "__main__":
    unittest.main()
