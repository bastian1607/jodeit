"""AB-009 browser acceptance tests (AC-01 … AC-14) against a local API with synthetic HA entities.

Run: AUTO_BALANCE_UI_TESTS=1 python -m unittest tests.test_ui_v2
Screenshots for the PR: AUTO_BALANCE_SCREENSHOTS=<dir>.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import model  # noqa: E402
import server  # noqa: E402

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

STAMP = datetime.now(timezone.utc).isoformat()


def ent(entity_id, name, state="21", unit="°C", device_class="temperature"):
    return dict(entity_id=entity_id, name=name, state=state, unit=unit, device_class=device_class,
                source_at=STAMP, last_reported=STAMP, last_updated=STAMP)


ENTITIES = [
    ent("sensor.wohnzimmer_t", "Wohnzimmer Temperatur"),
    ent("sensor.wohnzimmer_t2", "Wohnzimmer Sofa"),
    ent("sensor.kueche_t", "Küche Temperatur"),
    ent("sensor.bad_t", "Bad Temperatur"),
    ent("sensor.aussen", "Außentemperatur", "3"),
    ent("sensor.vl", "Vorlauf Ist", "42"),
    ent("sensor.vl_soll", "Vorlauf Soll", "43"),
    ent("sensor.rl", "Rücklauf", "36"),
    ent("binary_sensor.fenster_wz", "Fenster Wohnzimmer", "off", "", "window"),
    ent("binary_sensor.tuer_terrasse", "Terrassentür", "off", "", "door"),
    ent("binary_sensor.pumpe", "Heizkreispumpe", "on", "", ""),
    ent("number.ventil_wz", "Ventil Wohnzimmer", "40", "%", ""),
]


@unittest.skipUnless(os.environ.get("AUTO_BALANCE_UI_TESTS") == "1" and sync_playwright,
                     "Optional: set AUTO_BALANCE_UI_TESTS=1 and install playwright/Chromium")
class V2Browser(unittest.TestCase):
    viewport = {"width": 1280, "height": 900}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = server.DB
        server.DB = str(Path(self.tmp.name) / "ui.db")
        server.setup()
        self.entities = [dict(e) for e in ENTITIES]
        self.ha = patch.object(server, "get_entities", side_effect=lambda: self.entities)
        self.ha.start()
        self.http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.API)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.http.server_port}/"
        self.pw = sync_playwright().start()
        exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or shutil.which("chromium")
        self.browser = self.pw.chromium.launch(headless=True, executable_path=exe, args=["--no-sandbox"])
        self.page = self.browser.new_page(viewport=self.viewport)
        self.errors, self.requests = [], []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("request", lambda r: self.requests.append((r.method, r.url.replace(self.url, "/"))))

    def tearDown(self):
        self.browser.close()
        self.pw.stop()
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.ha.stop()
        server.DB = self.old_db
        self.tmp.cleanup()
        self.assertEqual(self.errors, [], "Browser JavaScript errors")
        # AC-14: the UI only talks to the local AutoBalance API – no HA service calls.
        for method, path in self.requests:
            self.assertTrue(path.startswith("/api/") or path in ("/", "/jodeit-logo.png"), path)
            if method == "POST":
                self.assertIn(path, ("/api/v2/config", "/api/v2/load-share-suggestion", "/api/v2/heat-source/test", "/api/v2/sessions/start", "/api/v2/sessions/stop", "/api/v2/sessions/archive", "/api/v2/sessions/delete", "/api/v2/reset"), path)

    # ------------------------------------------------------------- helpers
    def v2(self):
        with urlopen(self.url + "api/v2/config") as r:
            return json.load(r)

    def put_v2(self, cfg):
        req = Request(self.url + "api/v2/config", json.dumps(cfg).encode(), {"Content-Type": "application/json"})
        with urlopen(req) as r:
            self.assertEqual(r.status, 200)

    def seed(self, rooms=("Wohnzimmer", "Küche", "Bad")):
        cfg = model.empty()
        sensors = {"Wohnzimmer": "sensor.wohnzimmer_t", "Küche": "sensor.kueche_t", "Bad": "sensor.bad_t"}
        for name in rooms:
            room_id = name.lower().replace("ü", "ue")
            cfg["rooms"].append({"id": room_id, "name": name, "target": 21, "area": None, "default_circuit_id": "hk1",
                                 "heat_load": model.empty_heat_load(), "temperature_sensors": [sensors[name]], "window_sensors": []})
        self.put_v2(cfg)
        return cfg

    def open(self):
        self.page.goto(self.url)
        expect(self.page.locator("#notice")).to_contain_text("Entitäten geladen")

    def tab(self, name):
        self.page.locator("#tabs").get_by_role("button", name=name).click()

    def edit_room(self, name):
        self.tab("1 Räume")
        self.page.locator("#room-list .item", has_text=name).get_by_role("button", name="Bearbeiten").click()
        expect(self.page.locator("#room-editor")).to_be_visible()

    def pick(self, container, query):
        picker = self.page.locator(container)
        details = picker.locator("details.entity-picker")
        if not details.evaluate("d => d.open"):
            picker.locator("summary").click()
        picker.get_by_role("searchbox").fill(query)
        picker.locator(".entity-results").get_by_role("checkbox").first.check()

    def save_room(self, ok=True):
        self.page.get_by_role("button", name="Raum speichern").click()
        if ok:
            expect(self.page.locator("#notice")).to_contain_text("Konfiguration gespeichert")
            expect(self.page.locator("#room-editor")).to_be_hidden()

    def open_section(self, section_id):
        if not self.page.locator("#" + section_id).evaluate("d => d.open"):
            self.page.locator("#" + section_id + " > summary").click()

    def card(self, index):
        """Emitter card by position, opened (cards are re-rendered collapsed when one is added)."""
        card = self.page.locator("details.emitter").nth(index)
        if not card.evaluate("d => d.open"):
            card.locator("summary").first.click()
        return card

    def add_emitter(self):
        self.open_section("sec-emitters")
        self.page.get_by_role("button", name="+ Heizfläche").click()
        return self.card(self.page.locator("details.emitter").count() - 1)

    def shot(self, name):
        folder = os.environ.get("AUTO_BALANCE_SCREENSHOTS")
        if folder:
            Path(folder).mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(Path(folder) / f"{name}.png"), full_page=True)


class AcceptanceTests(V2Browser):
    def test_ac01_ac02_rooms_first_and_entity_selection(self):
        self.open()
        tabs = self.page.locator("#tabs button").all_inner_texts()
        self.assertEqual(tabs[1:], ["1 Räume", "2 Thermische Zonen", "3 Heizungsanlage & Heizkreise", "4 Heizflächen", "5 Prüfen & Messung"])
        self.tab("1 Räume")
        expect(self.page.locator("#room-list")).to_contain_text("Keine Räume angelegt")
        self.page.get_by_role("button", name="+ Raum anlegen").click()
        self.page.fill("#r-name", "Wohnzimmer")
        self.page.fill("#r-target", "21,5")                       # decimal comma accepted
        self.pick("#r-temperature", "wohnzimmer_t2")
        self.pick("#r-temperature", "Wohnzimmer Temperatur")
        self.pick("#r-windows", "Fenster")
        self.pick("#r-windows", "Terrasse")
        self.save_room()
        room = self.v2()["rooms"][0]
        self.assertEqual(room["target"], 21.5)
        self.assertEqual(set(room["temperature_sensors"]), {"sensor.wohnzimmer_t", "sensor.wohnzimmer_t2"})
        self.assertEqual(room["heat_load"]["status"], "missing")    # AC-01: no heat load needed
        self.assertEqual(self.v2()["emitters"], [])
        expect(self.page.locator("#room-list")).to_contain_text("Fühler zugeordnet")
        # remove one window contact selectively, reload, check
        self.page.reload()
        self.edit_room("Wohnzimmer")
        self.page.locator("#r-windows").get_by_role("button", name="Terrassentür entfernen").click()
        self.save_room()
        self.assertEqual(self.v2()["rooms"][0]["window_sensors"], ["binary_sensor.fenster_wz"])
        # HA entity temporarily unavailable: assignment is kept, nothing lost on save
        self.entities = [e for e in self.entities if e["entity_id"] != "sensor.wohnzimmer_t2"]
        self.page.reload()
        self.edit_room("Wohnzimmer")
        expect(self.page.locator("#r-temperature")).to_contain_text("nicht verfügbar")
        self.save_room()
        self.assertIn("sensor.wohnzimmer_t2", self.v2()["rooms"][0]["temperature_sensors"])

    def test_ac01_validation_messages(self):
        self.open()
        self.tab("1 Räume")
        self.page.get_by_role("button", name="+ Raum anlegen").click()
        self.page.fill("#r-target", "23")
        self.save_room(ok=False)
        expect(self.page.locator("#notice")).to_contain_text("Bitte einen Raumnamen eingeben")
        self.page.locator("#sec-heat-load summary").click()
        self.page.fill("#r-design", "20")
        self.save_room(ok=False)
        expect(self.page.locator("#notice")).to_contain_text("Quelle der Auslegungs-Innentemperatur")
        self.assertEqual(self.v2()["rooms"], [])

    def test_ab013_room_without_sensor_and_coverage(self):
        """AC-013-01/04/06: Bad ohne Fühler speicherbar, nicht überwacht, Abdeckung sichtbar."""
        self.seed(rooms=("Wohnzimmer",))
        self.open()
        self.tab("1 Räume")
        self.page.get_by_role("button", name="+ Raum anlegen").click()
        self.page.fill("#r-name", "Bad")
        self.page.fill("#r-target", "23")
        self.save_room()
        room = self.v2()["rooms"][1]
        self.assertEqual((room["name"], room["target"], room["temperature_sensors"], room["design_indoor_c"]), ("Bad", 23, [], None))
        item = self.page.locator("#room-list .item", has_text="Bad")
        expect(item).to_contain_text("Nicht überwacht")
        expect(item).to_contain_text("Höheres Komfortziel")
        expect(self.page.locator("#coverage-line")).to_contain_text("1 von 2 Räumen mit Temperaturfühler")
        expect(self.page.locator("#coverage-line")).to_contain_text("nicht überwacht: Bad")
        self.page.reload()
        self.tab("1 Räume")
        expect(self.page.locator("#room-list")).to_contain_text("Bad")
        # AB-024: Messung mit Freitext beginnen und stoppen, danach erneut druckbar
        self.tab("5 Prüfen & Messung")
        form = self.page.locator("#session-form")
        expect(form).to_contain_text("Gemessen werden 1 von 2 Räumen")
        expect(form).to_contain_text("nicht überwacht: Bad")
        # AC6-05: sensor IDs with freshness and an explicit check before the job can start
        review = self.page.locator("#session-sensor-review")
        expect(review).to_contain_text("Wohnzimmer: sensor.")
        expect(review).to_contain_text("Nicht überwacht: Bad")
        expect(self.page.locator("#session-start")).to_be_disabled()
        self.page.check("#session-confirm")
        expect(review).to_contain_text("Ich habe Sensorzuordnung, Aufstellorte und nicht überwachte Räume geprüft")
        self.page.click("#session-start")
        expect(self.page.locator("#notice")).to_contain_text("Bitte eine Bezeichnung eingeben")
        self.page.fill("#session-name", "Müller · Angebot 2026-123 <Test>")
        self.page.fill("#session-note", "Schlüssel beim Nachbarn")
        self.page.click("#session-start")
        active = self.page.locator("#session-active")
        expect(active).to_contain_text("Müller · Angebot 2026-123 <Test>")
        expect(active).to_contain_text("Tag 1 von 7")
        expect(active).to_contain_text("Notiz: Schlüssel beim Nachbarn")
        self.tab("Übersicht")
        expect(self.page.locator("#measurement-quick")).to_contain_text("Messung läuft: Müller")
        dialogs = []
        self.page.once("dialog", lambda d: (dialogs.append(d.message), d.accept()))
        self.page.click("#quick-stop")
        expect(self.page.locator("#quick-start")).to_be_visible()
        self.assertIn("Die gesonderte Ablage in der Kundenakte muss bestätigt werden.", dialogs[0])
        self.page.click("#quick-start")
        expect(self.page.locator("#session-form")).to_be_visible()
        history = self.page.locator("#session-history")
        expect(history).to_contain_text("Müller · Angebot 2026-123 <Test>")
        with self.page.expect_popup() as popup:
            history.locator(".job-report").first.click()
        expect(popup.value.locator("body")).to_contain_text("Notiz: Schlüssel beim Nachbarn")
        expect(popup.value.locator("body")).to_contain_text("Müller · Angebot 2026-123 <Test>")
        popup.value.close()
        self.shot("ab013-raeume-messauftrag")

    def test_ac03_zones(self):
        self.seed()
        cfg = self.v2()
        self.open()
        self.tab("2 Thermische Zonen")
        self.page.fill("#z-name", "Wohnküche")
        self.page.locator("#z-rooms label", has_text="Wohnzimmer").locator("input").check()
        self.page.locator("#z-rooms label", has_text="Küche").locator("input").check()
        self.page.get_by_role("button", name="Zone erstellen").click()
        expect(self.page.locator("#notice")).to_contain_text("Konfiguration gespeichert")
        expect(self.page.locator("#zone-list")).to_contain_text("Wohnzimmer + Küche")
        expect(self.page.locator("#zone-list .item", has_text="Bad")).to_contain_text("Geschlossene Einzelzone")
        expect(self.page.locator("#z-rooms label", has_text="Wohnzimmer").locator("input")).to_be_disabled()
        after = self.v2()
        self.assertEqual(after["zones"][0]["room_ids"], ["wohnzimmer", "kueche"])
        self.assertEqual([r["default_circuit_id"] for r in after["rooms"]], [r["default_circuit_id"] for r in cfg["rooms"]])

    def test_ac04_ac10_circuits_and_heating_curve(self):
        self.seed()
        self.open()
        self.tab("3 Heizungsanlage & Heizkreise")
        self.page.select_option("#b-system", "mixed")
        self.page.fill("#b-outdoor", "-12")
        self.page.get_by_role("button", name="+ Heizkreis").click()
        cards = self.page.locator("details.circuit")
        first, second = cards.nth(0), cards.nth(1)
        if not first.evaluate("d => d.open"):
            first.locator("summary").first.click()
        first.locator(".c-flow").fill("55")
        first.locator(".c-return").fill("45")
        first.locator(".c-e-flow").get_by_role("combobox").select_option("sensor.vl")
        first.locator(".c-e-flow_target").get_by_role("combobox").select_option("sensor.vl_soll")
        first.locator(".c-e-pump").get_by_role("combobox").select_option("binary_sensor.pumpe")
        second.locator(".c-name").fill("FBH Bad")
        second.locator(".c-flow").fill("35")                         # only one design temperature
        self.page.get_by_role("button", name="Anlage speichern").click()
        expect(self.page.locator("#notice")).to_contain_text("gemeinsam angeben")
        second.locator(".c-return").fill("28")
        # heating curve: a single point is rejected
        first.locator("summary", has_text="Heizkurve erfassen").click()
        first.get_by_role("button", name="+ Stützpunkt").click()
        rows = first.locator(".c-points .pair-row")
        rows.nth(0).locator(".p-a").fill("-12")
        rows.nth(0).locator(".p-b").fill("50")
        self.page.get_by_role("button", name="Anlage speichern").click()
        expect(self.page.locator("#notice")).to_contain_text("mindestens zwei Punkte")
        first.get_by_role("button", name="+ Stützpunkt").click()
        rows.nth(1).locator(".p-a").fill("-12")
        rows.nth(1).locator(".p-b").fill("30")
        self.page.get_by_role("button", name="Anlage speichern").click()
        expect(self.page.locator("#notice")).to_contain_text("bereits als Heizkurvenpunkt")
        rows.nth(1).locator(".p-a").fill("20")
        first.get_by_role("button", name="+ Parameter").click()
        params = first.locator(".c-params .pair-row")
        params.nth(0).locator(".p-a").fill("Fußpunkt")
        params.nth(0).locator(".p-b").fill("25")
        first.locator(".c-curve-ref").fill("Inbetriebnahmeprotokoll")
        self.page.get_by_role("button", name="Anlage speichern").click()
        expect(self.page.locator("#notice")).to_contain_text("Konfiguration gespeichert")
        cfg = self.v2()
        hk1, hk2 = cfg["circuits"]
        self.assertEqual((hk1["design_flow_c"], hk1["design_return_c"]), (55, 45))
        self.assertEqual((hk2["name"], hk2["design_flow_c"], hk2["design_return_c"]), ("FBH Bad", 35, 28))
        self.assertEqual(hk1["entities"], {"flow": "sensor.vl", "flow_target": "sensor.vl_soll", "return": "", "pump": "binary_sensor.pumpe"})
        self.assertEqual(hk1["heating_curve"]["points"], [{"outdoor_c": -12, "flow_c": 50}, {"outdoor_c": 20, "flow_c": 30}])
        self.assertEqual(hk1["heating_curve"]["manufacturer_params"], {"Fußpunkt": 25})
        self.assertEqual(cfg["building"]["heating_system"], "mixed")
        self.assertEqual(cfg["building"]["design_outdoor_c"], -12)
        # reload: values editable again; zero points can be saved
        self.page.reload()
        self.tab("3 Heizungsanlage & Heizkreise")
        first = self.page.locator("details.circuit").first
        first.locator("summary").first.click()
        expect(first.locator(".c-flow")).to_have_value("55")
        first.locator("summary", has_text="Heizkurve erfassen").click()
        for _ in range(2):
            first.locator(".c-points .pair-row").first.get_by_role("button", name="Entfernen").click()
        self.page.get_by_role("button", name="Anlage speichern").click()
        expect(self.page.locator("#notice")).to_contain_text("Konfiguration gespeichert")
        self.assertEqual(self.v2()["circuits"][0]["heating_curve"]["points"], [])

    def test_circuit_in_use_cannot_be_deleted(self):
        cfg = self.seed()
        cfg["circuits"].append(model.empty_circuit("hk2", "FBH"))
        e = model._emitter_from_v1({"name": "FBH Bad"}, "bad", "hk2")
        cfg["emitters"].append(e)
        self.put_v2(cfg)
        self.open()
        self.tab("3 Heizungsanlage & Heizkreise")
        card = self.page.locator("details.circuit").nth(1)
        card.locator("summary").first.click()
        card.get_by_role("button", name="Heizkreis löschen").click()
        expect(self.page.locator("#notice")).to_contain_text("versorgt noch 1 Heizfläche")
        self.assertEqual(len(self.v2()["circuits"]), 2)

    def test_ac05_ac07_ac09_emitters(self):
        cfg = self.seed()
        cfg["circuits"].append(model.empty_circuit("hk2", "FBH"))
        self.put_v2(cfg)
        self.open()
        self.edit_room("Wohnzimmer")
        a = self.add_emitter()
        self.assertEqual(a.locator(".e-circuit").input_value(), "hk1")      # pre-filled from room default
        a.locator(".e-name").fill("Fenster")
        a.locator(".e-type").select_option("panel")
        a.locator(".e-power").fill("1200")
        a.locator(".e-flow").fill("75")                                       # incomplete reference
        a.locator(".e-valve").select_option("imi-heimeier-eclipse")
        a.locator(".e-v-variant").fill("DN15 Eck")
        a.locator(".e-v-setting").fill("Stufe 5")
        a.locator(".rad-entity").get_by_role("combobox").select_option("number.ventil_wz")
        b = self.add_emitter()
        b.locator(".e-name").fill("FBH-Kreis")
        b.locator(".e-type").select_option("underfloor")
        expect(b.locator(".e-ufh")).to_be_visible()
        b.locator(".e-circuit").select_option("hk2")
        b.locator(".e-valve").select_option("other")
        b.locator(".e-v-manufacturer").fill("Oventrop")
        b.locator(".e-v-series").fill("Verteiler")
        self.save_room(ok=False)
        expect(self.page.locator("#notice")).to_contain_text("alle drei Referenztemperaturen")
        a = self.card(0)
        a.locator(".e-return").fill("65")
        a.locator(".e-room").fill("20")
        a.locator(".e-source").select_option("manufacturer")
        self.save_room()
        cfg = self.v2()
        fenster, fbh = cfg["emitters"]
        self.assertEqual((fenster["circuit_id"], fbh["circuit_id"]), ("hk1", "hk2"))
        self.assertEqual(fenster["rated_output"], {"value_w": 1200, "flow_c": 75, "return_c": 65, "room_c": 20, "source": "manufacturer", "reference": ""})
        self.assertEqual((fenster["valve"]["manufacturer"], fenster["valve"]["series"], fenster["valve"]["variant"]), ("IMI Heimeier", "Eclipse", "DN15 Eck"))
        self.assertEqual(fenster["actuator_entity"], "number.ventil_wz")
        self.assertEqual((fbh["valve"]["manufacturer"], fbh["valve"]["series"]), ("Oventrop", "Verteiler"))
        self.assertEqual(set(fenster["valve"]), {"label", "manufacturer", "series", "variant", "dimension", "current_setting"})  # no derived curve
        # changing one emitter's circuit does not touch the other; "unknown" valve clears catalogue data
        self.edit_room("Wohnzimmer")
        self.open_section("sec-emitters")
        card = self.card(0)
        card.locator(".e-circuit").select_option("hk2")
        card.locator(".e-valve").select_option("unknown")
        self.save_room()
        fenster2, fbh2 = self.v2()["emitters"]
        self.assertEqual(fenster2["circuit_id"], "hk2")
        self.assertEqual((fenster2["valve"]["manufacturer"], fenster2["valve"]["series"]), ("", ""))
        self.assertEqual(fenster2["valve"]["variant"], "DN15 Eck")               # free text kept
        self.assertEqual(fbh2, fbh)

    def test_ac07_legacy_power_is_marked_unverified(self):
        req = Request(self.url + "api/config", json.dumps({"rooms": [{"id": "wz", "name": "Wohnzimmer", "target": 21, "heat_load": 1400,
              "temperature_sensors": ["sensor.wohnzimmer_t"], "window_sensors": [],
              "radiators": [{"name": "Alt", "entity": "", "valve_model": "", "power_w": 900}]}], "zones": [],
              "heating": {"manufacturer": "", "model": "", "circuit_name": "HK1", "outdoor": "", "flow": "", "return": "", "compressor": "", "dhw": ""}}).encode(),
              {"Content-Type": "application/json"})
        urlopen(req).close()
        self.open()
        self.edit_room("Wohnzimmer")
        self.open_section("sec-emitters")
        expect(self.card(0)).to_contain_text("Bezugstemperaturen unbekannt")
        self.open_section("sec-heat-load")
        expect(self.page.locator("#hl-legacy")).to_contain_text("Alter, nicht belegter Wert: 1400 W")
        expect(self.page.locator("input[name=hl-status][value=missing]")).to_be_checked()

    def test_ac06_heat_load_only_adopted_or_missing(self):
        self.seed()
        self.open()
        self.edit_room("Wohnzimmer")
        self.open_section("sec-heat-load")
        expect(self.page.locator("#sec-heat-load .seg label")).to_have_count(2)
        self.assertNotIn("schätz", self.page.content().lower())
        self.page.locator("input[name=hl-status][value=adopted]").check()
        self.page.fill("#hl-value", "1450")
        self.save_room(ok=False)
        expect(self.page.locator("#notice")).to_contain_text("Wert, Berechnungsverfahren, Dokumentquelle und Datum gemeinsam")
        self.page.fill("#hl-method", "DIN EN 12831-1")
        self.page.fill("#hl-reference", "Heizlastberechnung Muster, S. 4")
        self.page.fill("#hl-date", "2026-10-01")
        self.save_room()
        heat_load = self.v2()["rooms"][0]["heat_load"]
        self.assertEqual((heat_load["status"], heat_load["value_w"], heat_load["date"], heat_load["author"]), ("adopted", 1450, "2026-10-01", ""))
        # switching back to "Fehlt" removes the value
        self.edit_room("Wohnzimmer")
        self.open_section("sec-heat-load")
        self.page.locator("input[name=hl-status][value=missing]").check()
        self.save_room()
        heat_load = self.v2()["rooms"][0]["heat_load"]
        self.assertEqual((heat_load["status"], heat_load["value_w"]), ("missing", None))

    def complete_two_emitters(self, exponent="1,3"):
        cfg = self.seed()
        cfg["circuits"][0].update(design_flow_c=55, design_return_c=45)
        cfg["rooms"][0].update(design_indoor_c=20, design_indoor_reference="Heizlastberechnung Test")
        self.put_v2(cfg)
        self.open()
        self.edit_room("Wohnzimmer")
        for name, power in (("Fenster", "1200"), ("Wand", "600")):
            card = self.add_emitter()
            card.locator(".e-name").fill(name)
            card.locator(".e-power").fill(power)
            card.locator(".e-flow").fill("75")
            card.locator(".e-return").fill("65")
            card.locator(".e-room").fill("20")
            if exponent:
                card.locator("summary", has_text="Weitere Fachangaben").click()
                card.locator(".e-exponent").fill(exponent)
        return self

    def test_ac08_shares_live_sum_rejection_and_suggestion(self):
        self.complete_two_emitters(exponent=None)
        self.card(0).locator(".e-share").fill("60")
        self.card(1).locator(".e-share").fill("30")
        expect(self.page.locator("#share-sum")).to_have_text("zusammen 90 %")
        expect(self.page.locator("#share-sum")).to_have_class("share-sum bad")
        self.save_room(ok=False)
        expect(self.page.locator("#notice")).to_contain_text("zusammen 90 %")
        self.assertEqual(self.v2()["emitters"], [])
        # without exponent the data is not comparable -> no proposal, manual input still possible
        self.page.get_by_role("button", name="Anteile vorschlagen").click()
        expect(self.page.locator("#share-hint")).to_contain_text("Kein Vorschlag")
        expect(self.card(0).locator(".e-share")).to_have_value("60")
        self.card(1).locator(".e-share").fill("40")
        expect(self.page.locator("#share-sum")).to_have_text("zusammen 100 %")
        self.save_room()
        self.assertEqual([e["load_share_percent"] for e in self.v2()["emitters"]], [60, 40])

    def test_ac08_suggestion_with_comparable_data(self):
        self.complete_two_emitters()
        self.page.get_by_role("button", name="Anteile vorschlagen").click()
        expect(self.page.locator("#share-hint")).to_contain_text("Vorschlag eingetragen")
        expect(self.card(0).locator(".e-share")).to_have_value("66,7")
        expect(self.card(1).locator(".e-share")).to_have_value("33,3")
        self.assertEqual(self.v2()["emitters"], [])          # not saved automatically
        self.save_room()
        self.assertEqual([e["load_share_percent"] for e in self.v2()["emitters"]], [66.7, 33.3])

    def test_ac11_readiness_navigation(self):
        self.seed(rooms=("Wohnzimmer",))
        self.open()
        self.tab("5 Prüfen & Messung")
        expect(self.page.locator("#measure-list")).to_contain_text("Fühler zugeordnet")
        box = self.page.locator("#readiness-list .item", has_text="Wohnzimmer")
        expect(box).to_contain_text("Angaben für Abgleich fehlen")
        box.get_by_role("button", name="→ Heizlast fehlt").click()
        expect(self.page.locator("#room-editor")).to_be_visible()
        self.assertTrue(self.page.locator("#sec-heat-load").evaluate("d => d.open"))
        self.page.get_by_role("button", name="Abbrechen").click()
        self.tab("5 Prüfen & Messung")
        self.page.locator("#readiness-list .item", has_text="Anlage").get_by_role("button", name="→ Heizsystem der Anlage fehlt").click()
        expect(self.page.locator("#plant")).to_be_visible()
        # complete room -> wording is data completeness only
        cfg = self.v2()
        cfg["building"].update(heating_system="radiators", design_outdoor_c=-12)
        cfg["circuits"][0].update(design_flow_c=55, design_return_c=45)
        cfg["rooms"][0].update(design_indoor_c=20, design_indoor_reference="Berechnung")
        cfg["rooms"][0]["heat_load"] = {"status": "adopted", "value_w": 1400, "method": "DIN EN 12831-1", "reference": "Berechnung",
                                        "author": "", "date": "2026-10-01", "legacy_value_w": None}
        e = model._emitter_from_v1({"name": "HK", "power_w": 1500}, "wohnzimmer", "hk1")
        e["rated_output"].update(flow_c=75, return_c=65, room_c=20)
        cfg["emitters"] = [e]
        self.put_v2(cfg)
        self.page.reload()
        self.tab("5 Prüfen & Messung")
        expect(self.page.locator("#readiness-list .item", has_text="Wohnzimmer")).to_contain_text("Auslegungsdaten vollständig – Berechnung noch nicht verfügbar")
        content = self.page.content()
        for forbidden in ("Hydraulisch optimiert", "Einstellempfehlung bereit"):
            self.assertNotIn(forbidden, content)

    def test_ac13_navigation_does_not_save_and_unsaved_changes_are_protected(self):
        self.seed()
        before = self.v2()
        self.open()
        for name in ("1 Räume", "2 Thermische Zonen", "3 Heizungsanlage & Heizkreise", "4 Heizflächen", "5 Prüfen & Messung", "Übersicht"):
            self.tab(name)
        self.assertFalse([r for r in self.requests if r[0] == "POST"])
        self.edit_room("Wohnzimmer")
        self.page.fill("#r-name", "Wohnzimmer neu")
        self.page.once("dialog", lambda d: d.dismiss())       # keep editing
        self.tab("2 Thermische Zonen")
        expect(self.page.locator("#room-editor")).to_be_visible()
        expect(self.page.locator("#r-name")).to_have_value("Wohnzimmer neu")
        # a failing save keeps every entry and shows no success message
        with patch.object(server, "save_v2", side_effect=ValueError("Speicher simuliert defekt")):
            self.page.get_by_role("button", name="Raum speichern").click()
            expect(self.page.locator("#notice")).to_contain_text("Nicht gespeichert: Speicher simuliert defekt")
        expect(self.page.locator("#r-name")).to_have_value("Wohnzimmer neu")
        self.assertEqual(self.v2(), before)
        self.save_room()
        self.assertEqual(self.v2()["rooms"][0]["name"], "Wohnzimmer neu")

    def test_ac14_no_active_test_controls(self):
        self.seed()
        self.open()
        for name in ("1 Räume", "3 Heizungsanlage & Heizkreise", "5 Prüfen & Messung"):
            self.tab(name)
        buttons = " ".join(self.page.locator("button").all_inner_texts()).lower()
        for forbidden in ("stellversuch", "ventil fahren", "öffnen auf", "heizkurve übertragen", "wärmepumpe aus"):
            self.assertNotIn(forbidden, buttons)
        expect(self.page.locator("header .tag")).to_have_text("Nur Messung · keine Steuerbefehle")


    def test_ab021_insecure_http_context_without_random_uuid(self):
        """AB-021: Home Assistant via http://<LAN-IP> has no crypto.randomUUID. Room, emitter and zone must still work."""
        self.page.add_init_script("delete Crypto.prototype.randomUUID;")
        self.seed(rooms=("Wohnzimmer", "Küche"))
        self.open()
        self.assertFalse(self.page.evaluate("typeof crypto.randomUUID === 'function'"))
        self.tab("1 Räume")
        self.page.get_by_role("button", name="+ Raum anlegen").click()
        expect(self.page.locator("#room-editor")).to_be_visible()
        expect(self.page.locator("#room-heading")).to_have_text("Raum anlegen")
        self.page.locator("#r-name").fill("Bad")
        self.pick("#r-temperature", "Bad Temperatur")
        self.open_section("sec-emitters")
        self.page.get_by_role("button", name="+ Heizfläche").click()
        expect(self.page.locator("#r-radiators > details")).to_have_count(1)
        self.page.get_by_role("button", name="Raum speichern").click()
        expect(self.page.locator("#notice")).to_contain_text("gespeichert")
        cfg = self.v2()
        bad = next(r for r in cfg["rooms"] if r["name"] == "Bad")
        self.assertRegex(bad["id"], r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        self.assertRegex(cfg["emitters"][-1]["id"], r"^hf_[0-9a-f]{12}$")
        self.tab("2 Thermische Zonen")
        self.page.locator("#z-name").fill("Wohnen und Kochen")
        self.page.locator("#z-rooms label", has_text="Wohnzimmer").locator("input").check()
        self.page.locator("#z-rooms label", has_text="Küche").locator("input").check()
        self.page.get_by_role("button", name="Zone erstellen").click()
        expect(self.page.locator("#notice")).to_contain_text("gespeichert")
        self.assertEqual(len(self.v2()["zones"]), 1)

    def test_ab021_no_secure_random_source_shows_message(self):
        self.page.add_init_script("delete Crypto.prototype.randomUUID; delete Crypto.prototype.getRandomValues;")
        self.open()
        self.tab("1 Räume")
        self.page.get_by_role("button", name="+ Raum anlegen").click()
        expect(self.page.locator("#notice")).to_contain_text("keine sichere Zufallsquelle")
        expect(self.page.locator("#room-editor")).to_be_hidden()

    def test_ab014_assessment_of_running_job(self):
        """AB-014: Stufe, Nächte und fehlende Aussagegrundlage sichtbar; keine Parameterempfehlung."""
        import sqlite3
        from datetime import timedelta
        start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(days=4)
        with sqlite3.connect(server.DB) as c:      # fresh-install baseline carries "now"; this test travels back in time
            c.execute("DELETE FROM config_history")
        with patch.object(server, "utcnow", return_value=start - timedelta(hours=1)):   # configured before the job
            cfg = self.seed(rooms=("Wohnzimmer", "Bad"))
            cfg["rooms"][1]["temperature_sensors"] = []
            cfg["building"]["entities"]["outdoor"] = "sensor.aussen"
            cfg["circuits"][0]["entities"]["flow"] = "sensor.vl"
            self.put_v2(cfg)
        with patch.object(server, "utcnow", return_value=start):
            job = server.start_session({"name": "Pilot Anlage 1", "confirmed": True})
        with sqlite3.connect(server.DB) as c:
            t = start
            while t < datetime.now(timezone.utc):
                for eid, v in (("sensor.aussen", 2.0), ("sensor.vl", 40.0), ("sensor.wohnzimmer_t", 20.7)):
                    c.execute("INSERT INTO samples(at,entity_id,state,unit,valid,quality,normalized_value,session_id) VALUES(?,?,?,?,?,?,?,?)",
                              (t.isoformat(), eid, str(v), "°C", 1, "ok", v, job["id"]))
                t += timedelta(minutes=5)
        self.open()
        self.tab("5 Prüfen & Messung")
        stage = self.page.locator("#assessment-stage")
        expect(stage).to_contain_text("Heizbetrieb nicht nachgewiesen")     # HA-only config (AC6-02)
        expect(stage).to_contain_text("vorläufig")
        expect(self.page.locator("#assessment-basis")).to_contain_text("Abtauung nicht erkennbar")
        expect(self.page.locator("#assessment-nights")).to_contain_text("technisch auswertbar · Heizbetrieb nicht nachgewiesen")
        expect(self.page.locator("#assessment")).to_contain_text("Bad nicht überwacht")
        content = self.page.content()
        for forbidden in ("Offset auf", "Heizkurve anheben", "Empfehlung übernehmen"):
            self.assertNotIn(forbidden, content)
        self.shot("ab014-auswertung")
        # AC-06: report opens from step 5; AC-020-11: dashboard shows coverage since job start
        with self.page.expect_popup() as popup:
            self.page.click("#report-link")
        report = popup.value
        expect(report.locator("#report-result")).to_contain_text("Heizbetrieb nicht nachgewiesen")
        expect(report.locator("body")).to_contain_text("Nicht überwachte Räume: Bad")
        if os.environ.get("AUTO_BALANCE_SCREENSHOTS"):
            report.screenshot(path=str(Path(os.environ["AUTO_BALANCE_SCREENSHOTS"]) / "ab020-messbericht.png"), full_page=True)
        report.close()
        self.tab("Übersicht")
        expect(self.page.locator("#stats")).to_contain_text("Abdeckung seit Messbeginn")
        expect(self.page.locator("#stats")).to_contain_text("Ziel: 7 Tage")
        self.assertNotIn("Abdeckung 7 Tage", self.page.locator("#stats").inner_text())


class MobileTests(V2Browser):
    viewport = {"width": 390, "height": 844}

    def assertNoHorizontalScroll(self):
        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),
                        self.page.evaluate("document.documentElement.scrollWidth"))

    def test_ac12_mobile_editing_without_horizontal_scroll(self):
        cfg = self.seed()
        cfg["circuits"][0]["heating_curve"]["points"] = [{"outdoor_c": -12, "flow_c": 50}, {"outdoor_c": 20, "flow_c": 28}]
        self.put_v2(cfg)
        self.open()
        self.edit_room("Wohnzimmer")
        for section in ("sec-heat-load", "sec-emitters"):
            self.open_section(section)
        self.page.locator("input[name=hl-status][value=adopted]").check()
        card = self.add_emitter()
        card.locator(".e-power").fill("800")
        self.pick("#r-windows", "Fenster")
        self.assertNoHorizontalScroll()
        self.shot("ab009-mobile-raum")
        self.page.once("dialog", lambda d: d.accept())
        self.tab("3 Heizungsanlage & Heizkreise")
        circuit = self.page.locator("details.circuit").first
        circuit.locator("summary").first.click()
        circuit.locator("summary", has_text="Heizkurve erfassen").click()
        self.assertNoHorizontalScroll()
        self.shot("ab009-mobile-heizkreis")
        self.page.once("dialog", lambda d: d.accept())
        self.page.locator("#tabs").get_by_role("button", name="5 Prüfen & Messung").click()
        expect(self.page.locator("#readiness-list")).to_contain_text("Heizlast fehlt")
        self.assertNoHorizontalScroll()
        self.shot("ab009-mobile-pruefen")

    def test_ac12_tablet_and_desktop_layouts(self):
        self.seed()
        self.open()
        for width, height in ((820, 1180), (1440, 900)):
            self.page.set_viewport_size({"width": width, "height": height})
            self.edit_room("Wohnzimmer")
            self.open_section("sec-heat-load")
            self.assertNoHorizontalScroll()
            self.shot(f"ab009-raum-{width}")
            self.page.get_by_role("button", name="Abbrechen").click()

    def test_ab020_gap_and_outage_are_visible(self):
        """AB-020 AC-01/02: historical gap is listed; a 70-minute outage turns the banner red."""
        from datetime import timedelta
        self.seed(rooms=("Wohnzimmer",))
        now = datetime.now(timezone.utc)
        times = [now - timedelta(hours=4) + timedelta(minutes=5 * i) for i in range(13)]       # 4 h … 3 h ago
        times += [now - timedelta(hours=2) + timedelta(minutes=5 * i) for i in range(11)]      # 2 h … 70 min ago
        with server.connect() as c:
            c.executemany("INSERT INTO samples(at,entity_id,state,unit,valid,quality,source_at,normalized_value,normalized_unit) VALUES(?,?,?,?,?,?,?,?,?)",
                          [(t.isoformat(), "sensor.wohnzimmer_t", "21", "°C", 1, "ok", t.isoformat(), 21, "°C") for t in times])
        self.open()
        expect(self.page.locator("#acquisition-status")).to_contain_text("seit über 60 Minuten unterbrochen")
        self.tab("5 Prüfen & Messung")
        gaps = self.page.locator("#dq-gaps .dq-gap")
        expect(gaps).to_have_count(2)
        expect(gaps.first).to_contain_text("keine Messung")
        expect(gaps.nth(1)).to_contain_text("nicht erfasst (60 min)")
        expect(self.page.locator("#dq-entities")).to_contain_text("sensor.wohnzimmer_t")
        self.shot("ab020-messluecke")

    def test_ab015_heat_source_profile_read_only(self):
        """AB-015: choose profile, probe read-only, save, see heat pump next to rooms."""
        self.seed(rooms=("Wohnzimmer",))
        self.entities += [ent("number.thermostat_hc1_designtemp", "hc1 Design temperature", "54", "°C", ""),
                          ent("number.thermostat_hc1_offsettemp", "hc1 Offset temperature", "-2.0", "°C", ""),
                          ent("sensor.thermostat_hc1_targetflowtemp", "hc1 Target flow", "47", "°C", ""),
                          ent("sensor.boiler_curflowtemp", "Vorlauf", "45", "°C", ""),
                          ent("sensor.boiler_outdoortemp", "Außen", "-4.3", "°C", "")]
        self.open()
        self.tab("3 Heizungsanlage & Heizkreise")
        self.page.locator("#hs-profile").select_option("buderus-emsesp-hmc310")
        expect(self.page.locator("#hs-host-wrap")).to_be_hidden()
        self.page.locator("#hs-source").select_option("emsesp_rest")
        expect(self.page.locator("#hs-host-wrap")).to_be_visible()
        self.page.locator("#hs-source").select_option("ha")
        self.page.get_by_role("button", name="Verbindung prüfen (nur lesen)").click()
        expect(self.page.locator("#hs-summary")).to_contain_text("5 von 30 Werten gefunden")
        self.page.get_by_role("button", name="Anlage speichern").click()
        expect(self.page.locator("#notice")).to_contain_text("gespeichert")
        self.assertEqual(self.v2()["building"]["heat_source"]["profile"], "buderus-emsesp-hmc310")
        server.sample()
        self.tab("Übersicht")
        self.page.reload()
        expect(self.page.locator("#hs-values")).to_contain_text("54 °C")
        expect(self.page.locator("#hs-values")).to_contain_text("-4,3 °C")
        expect(self.page.locator("#hs-rooms")).to_contain_text("Wohnzimmer")
        self.shot("ab015-waermepumpe-raeume")


    def test_ab022_profile_detected_and_adopted_with_one_click(self):
        """AB-022: all profile entities exist in HA -> proposal; nothing saved before the click."""
        import devices
        self.seed(rooms=("Wohnzimmer",))
        for sid, f in devices.entities({"profile": "buderus-emsesp-hmc310", "source": "ha"}).items():
            self.entities.append(ent(sid, f["label"], "1", f["unit"], ""))
        self.open()
        self.tab("3 Heizungsanlage & Heizkreise")
        box = self.page.locator("#hs-detect-box")
        expect(box).to_contain_text("Erkannt: Buderus")
        expect(box).to_contain_text("30 von 30")
        self.assertEqual(self.v2()["building"]["heat_source"]["profile"], "")   # proposal only
        self.page.click("#hs-adopt")
        expect(self.page.locator("#notice")).to_contain_text("gespeichert")
        hs = self.v2()["building"]["heat_source"]
        self.assertEqual((hs["profile"], hs["source"]), ("buderus-emsesp-hmc310", "ha"))
        self.page.reload()
        self.tab("3 Heizungsanlage & Heizkreise")
        expect(self.page.locator("#hs-detect-box")).to_have_count(0)        # configured -> no proposal

    def test_ab022_partial_match_is_hint_only(self):
        self.seed(rooms=("Wohnzimmer",))
        self.entities += [ent("number.thermostat_hc1_designtemp", "hc1 Design temperature", "54", "°C", "")]
        self.open()
        self.tab("3 Heizungsanlage & Heizkreise")
        expect(self.page.locator("#hs-detect-box")).to_contain_text("Teilweise erkannt")
        expect(self.page.locator("#hs-adopt")).to_have_count(0)


    def test_ab026_retention_archive_delete_and_reset(self):
        """AB-026: Löschtermin und Hinweis sichtbar, Archivierung, Löschen mit Bestätigung, Zurücksetzen."""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        self.seed(rooms=("Wohnzimmer",))
        with patch.object(server, "utcnow", return_value=now - timedelta(days=25)):
            job = server.start_session({"name": "J-2026-123 Müller", "confirmed": True})
        with patch.object(server, "utcnow", return_value=now - timedelta(days=24)):
            server.stop_session(job["id"])
        self.open()
        expect(self.page.locator(".retention-notice")).to_contain_text("J-2026-123 Müller")
        expect(self.page.locator(".retention-notice")).to_contain_text("automatisch vom Gerät gelöscht")
        self.tab("5 Prüfen & Messung")
        row = self.page.locator("#session-history .job-row")
        expect(row).to_contain_text("Automatische Löschung am")
        expect(row).to_contain_text("Noch nicht als archiviert bestätigt")
        self.page.once("dialog", lambda d: d.accept())
        row.locator(".job-archive").click()
        expect(row).to_contain_text("In Kundenakte übernommen")
        self.shot("ab026-loeschung")
        self.page.once("dialog", lambda d: d.accept("falsch"))
        row.locator(".job-delete").click()
        expect(self.page.locator("#notice")).to_contain_text("Bezeichnung der Messung genau eingeben")
        self.page.once("dialog", lambda d: d.accept("J-2026-123 Müller"))
        row.locator(".job-delete").click()
        expect(self.page.locator("#session-history")).to_contain_text("Noch keine abgeschlossene Messung")
        self.page.locator("#reset-box summary").click()
        self.page.once("dialog", lambda d: d.accept("NEUER KUNDE"))
        self.page.click("#reset-start")
        expect(self.page.locator("#notice")).to_contain_text("Gerät zurückgesetzt")
        self.assertEqual(self.v2()["rooms"], [])


if __name__ == "__main__":
    unittest.main()
