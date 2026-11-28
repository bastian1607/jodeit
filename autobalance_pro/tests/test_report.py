"""AB-020 AC-06 / AC-020-11: measuring report per job and coverage since job start."""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import model  # noqa: E402
import report  # noqa: E402
import server  # noqa: E402
from test_ab014 import Plant, config, local  # noqa: E402

START = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = server.DB
        server.DB = os.path.join(self.tmp.name, "r.db")
        server.setup()
        with sqlite3.connect(server.DB) as c:
            c.execute("DELETE FROM config_history")
        cfg = config()
        cfg["building"]["entities"]["outdoor"] = "sensor.out"
        cfg["circuits"][0]["entities"]["flow"] = "sensor.vl"
        with patch.object(server, "utcnow", return_value=START - timedelta(hours=1)):
            server.save_v2(cfg)
        with patch.object(server, "utcnow", return_value=START):
            self.job = server.start_session({"name": "Pilot & Anlage \"1\"", "confirmed": True})

    def tearDown(self):
        server.DB = self.old_db
        self.tmp.cleanup()

    def store(self, plant):
        with sqlite3.connect(server.DB) as c:
            for t in plant.ticks:
                for eid in ("sensor.out", "sensor.vl", "sensor.wz", "binary_sensor.f_wz"):
                    x = plant.values[t][eid]
                    c.execute("INSERT INTO samples(at,entity_id,state,unit,valid,quality,normalized_value,session_id) VALUES(?,?,?,?,?,?,?,?)",
                              (t.isoformat(), eid, x["s"], "", 1, "ok", x["v"], self.job["id"]))

    def test_ac06_insufficient_after_seven_days_with_outage(self):
        p = Plant(days=7)
        p.drop(local(2, 0), local(6, 12))                   # long outage: only one usable night remains
        self.store(p)
        with patch.object(server, "utcnow", return_value=p.end):
            server.stop_session(self.job["id"])
        html = server.session_report(self.job["id"], now=p.end + timedelta(hours=1))
        self.assertIn("Messung unzureichend", html)
        self.assertIn("Messdauer verlängern oder Anlage manuell prüfen", html)
        self.assertIn("Messlücken über 15 Minuten", html)
        self.assertIn("Dieser Zeitraum wurde nicht ersetzt", html)
        self.assertIn("Nicht überwachte Räume:</b> Bad", html)
        self.assertIn("Pilot &amp; Anlage &quot;1&quot;", html)          # escaped
        self.assertIn("Abgeschlossen", html)
        for forbidden in ("Offset auf", "Empfehlung: ", "alle Räume im Zielbereich"):
            self.assertNotIn(forbidden, html)

    def test_running_job_with_level_result_is_provisional(self):
        p = Plant(days=4)
        self.store(p)
        html = server.session_report(self.job["id"], now=p.end)
        self.assertIn("Heizbetrieb nicht nachgewiesen", html)      # HA-only config, AC6-02
        self.assertIn("Laufende Messung · keine abschließende Bewertung", html)
        self.assertIn("Vorläufiger Bericht einer laufenden Messung", html)
        self.assertIn("Keine Messlücken über 15 Minuten", html)

    def test_job_without_data_reports_whole_window_as_gap(self):
        end = START + timedelta(hours=3)
        html = server.session_report(self.job["id"], now=end)
        self.assertIn("Keine Messdaten in dieser Messung", html)
        self.assertIn("(180 min) – andauernd", html)

    def test_endpoints_and_csv_limited_to_job(self):
        p = Plant(days=2)
        self.store(p)
        with sqlite3.connect(server.DB) as c:          # sample before the job must not be exported
            c.execute("INSERT INTO samples(at,entity_id,state,unit,valid,quality,normalized_value) VALUES(?,?,?,?,?,?,?)",
                      ((START - timedelta(days=1)).isoformat(), "sensor.wz", "19", "°C", 1, "ok", 19))
        with patch.object(server, "utcnow", return_value=p.end):
            server.stop_session(self.job["id"])
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.API)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{httpd.server_port}/api/v2/sessions/{self.job['id']}"
            with urlopen(base + "/report") as r:
                self.assertEqual(r.headers["Content-Type"], "text/html; charset=utf-8")
                self.assertIn("Ergebnisbericht Heizungsoptimierung", r.read().decode())
            with urlopen(base + "/export.csv") as r:
                text = r.read().decode("utf-8-sig")
            self.assertNotIn((START - timedelta(days=1)).isoformat(), text)
            self.assertIn(START.isoformat(), text)
            with self.assertRaises(Exception):
                urlopen(f"http://127.0.0.1:{httpd.server_port}/api/v2/sessions/ma_x/report")
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()


class LayoutV2Tests(ReportTests):
    """AB-030 v2 (AC-030-09 … 18): customer page + appendix, only real data, nothing invented."""
    test_ac06_insufficient_after_seven_days_with_outage = None
    test_running_job_with_level_result_is_provisional = None
    test_job_without_data_reports_whole_window_as_gap = None
    test_endpoints_and_csv_limited_to_job = None


    def html(self, days=5, stop=True, change=None):
        p = Plant(days=days)
        if change:                                       # configuration change inside the job window
            with patch.object(server, "utcnow", return_value=p.start + timedelta(hours=1)):
                cfg = server.load_v2()
                change(cfg)
                server.save_v2(cfg)
        self.store(p)
        if stop:
            with patch.object(server, "utcnow", return_value=p.end):
                server.stop_session(self.job["id"])
        return server.session_report(self.job["id"], now=p.end + timedelta(hours=1)), p

    def test_ac09_logo_title_and_offline(self):
        html, _ = self.html()
        self.assertIn('<img src="data:image/png;base64,', html)                      # original logo, embedded
        logo = (Path(server.__file__).parent / "static" / "jodeit-logo.png").read_bytes()
        import base64
        self.assertIn(base64.b64encode(logo).decode()[:200], html)
        self.assertIn("AutoBalance Pro · Ergebnisbericht Heizungsoptimierung", html)
        self.assertIn("Heizkurvenprüfung und Heizkörperempfehlungen", html)
        for external in ("http://", "https://", "@import", "fonts.googleapis"):
            self.assertNotIn(external, html)                                       # AC-030-07: no CDN/fonts
        self.assertIn("@page{size:A4 portrait", html)
        self.assertIn('class="appendix"', html)                                   # page break before appendix
        self.assertIn("thead{display:table-header-group}", html)

    def test_ac10_17_intervention_and_no_invented_recommendation(self):
        html, _ = self.html()
        self.assertIn("Was hat AutoBalance tatsächlich verändert?", html)
        self.assertIn("Nur gemessen, nicht gesteuert", html)
        self.assertIn("AutoBalance hat die Wärmepumpe nicht verändert.", html)
        self.assertIn("In dieser Version noch keine berechnete Einstellungsempfehlung", html)
        self.assertIn("Keine Änderung durch AutoBalance; eine manuelle Umsetzung wurde nicht dokumentiert", html)
        self.assertIn("Konkrete Einstellvorgaben werden derzeit nicht automatisch", html)
        for forbidden in ("AutoBalance hat eingestellt", "optimiert", "Voreinstellung: ", "l/h empfohlen",
                          "vorgeschlagene", "Noch nicht ableitbar"):
            self.assertNotIn(forbidden, html)

    def test_ac11_stage5_not_claimed(self):
        html, _ = self.html()
        self.assertIn("Thermostatköpfe auf Stufe 5“: <b>nicht bestätigt</b>", html)
        self.assertIn("Technische Auswertung, noch nicht durch Techniker abgenommen.", html)

    def test_ac12_13_two_radiators_in_one_room_without_data(self):
        def add(cfg):
            room = next(r for r in cfg["rooms"] if r["name"] == "Wohnzimmer")
            for name in ("Fenster", "Innenwand"):
                cfg["emitters"].append(model._emitter_from_v1({"name": name, "power_w": None}, room["id"], "hk1"))
        html, _ = self.html(change=add)
        section = html[html.index("Heizkörper je Raum"):]
        section = section[:section.index("</table>")]
        rows = [line for line in section.split("<tr>")[2:] if "Wohnzimmer" in line]
        self.assertEqual(len(rows), 2)                                              # one row per radiator
        for row in rows:
            self.assertIn("Nicht belastbar berechenbar", row)
            self.assertIn("AB-031-Berechnung noch nicht freigegeben", row)
            self.assertIn("erforderliche Anlagendaten fehlen", row)
            self.assertIn(">fehlt<", row)                                           # no heat load, no output

    def test_ac14_15_curve_only_measured_points_and_no_zero_degree_value(self):
        html, _ = self.html()
        self.assertIn("Vorlauf-Soll bei 0 °C <b>gedämpfter</b> Außentemperatur (Jodeit-Kennwert)", html)
        self.assertIn("Nicht zuverlässig bestimmbar", html)
        self.assertIn("Regler liefert keinen bestätigten Vergleichswert", html)
        self.assertIn("Noch keine fachlich freigegebene Empfehlung", html)
        self.assertNotIn("<path", html)                                             # no drawn/fitted curve
        self.assertIn(report.CURVE_TITLE, html)
        self.assertIn("Ungefilterte, technisch gültige 5-Minuten-Messpaare", html)
        self.assertIn("Warmwasser-, Abtau- und Nicht-Heizphasen", html)
        self.assertIn("Daraus ist keine Einstellungsempfehlung ableitbar", html)

    def test_ac14_undamped_fallback_never_presented_as_damped_kpi(self):
        pts = [(-3.0, 45.0), (0.2, 42.0), (4.0, 39.0)]
        meta = {"n": 3, "from": "2026-12-01T03:00:00+00:00", "to": "2026-12-03T03:00:00+00:00",
                "target": "sensor.vl_soll", "outdoor": "sensor.out"}
        with patch.object(server, "curve_points", return_value=(pts, False, meta)):
            html, _ = self.html()
        self.assertIn("Außentemperatur ungedämpft in °C", html)
        self.assertIn("Diagramm nutzt ungedämpfte Außentemperatur; für den gedämpften 0-°C-Kennwert liegen noch keine "
                      "geeigneten Werte vor.", html)
        self.assertIn("3 Messpaare", html)
        k0 = html[html.index('id="report-k0"'):]
        k0 = k0[:k0.index("</div></div>")]
        for value in ("42", "42,0", "45", "39"):                                   # no measured value turned into a KPI
            self.assertNotIn(f">{value}", k0)
        self.assertIn(report.NOT_DETERMINABLE, k0)

    def test_ac14_damped_points_with_dhw_still_labelled_unfiltered(self):
        meta = {"n": 4000, "from": None, "to": None, "target": "sensor.vl_soll", "outdoor": "sensor.dampedout"}
        pts = [(x / 100, 40.0) for x in range(-500, 500, 5)]
        with patch.object(server, "curve_points", return_value=(pts, True, meta)):
            html, _ = self.html()
        self.assertIn("4000 Messpaare, davon 200 dargestellt", html)
        self.assertIn("Warmwasser-, Abtau- und Nicht-Heizphasen", html)
        self.assertNotIn("Diagramm nutzt ungedämpfte", html)
        self.assertNotIn("<path", html)

    def test_p1_customer_page_shows_at_most_three_rooms(self):
        def add(cfg):
            base = next(r for r in cfg["rooms"] if r["name"] == "Bad")
            for i in range(4):
                cfg["rooms"].append(dict(base, id=f"room_extra{i}", name=f"Zusatzraum {i}"))
        html, _ = self.html(change=add)
        page1 = html[html.index('id="report-page1"'):html.index('id="report-appendix"')]
        rooms = page1[page1.index('id="report-rooms"'):]
        rooms = rooms[:rooms.index("</table>")]
        self.assertEqual(rooms.count("<tr>") - 1, 3)
        self.assertIn("3 weitere Räume und alle Heizkörper im Anhang", page1)
        self.assertIn('id="report-signoff"', page1)                                 # sign-off belongs to page 1

    def test_stopped_early_job_is_not_called_running(self):
        html, _ = self.html(days=5)                                               # planned 7 days, stopped after 5
        self.assertNotIn("Messung läuft noch", html)
        self.assertIn("Messung vor Ablauf der geplanten Dauer beendet – Ergebnis vorläufig.", html)

    def test_ac16_running_report_is_marked_provisional(self):
        html, _ = self.html(days=2, stop=False)
        self.assertIn("Laufende Messung · keine abschließende Bewertung", html)
        self.assertIn("Messung weiterlaufen lassen", html)

    def test_ac06_frozen_report_unchanged(self):
        html, p = self.html()
        cfg = server.load_v2()
        cfg["rooms"][0]["name"] = "Umbenannt"
        server.save_v2(cfg)
        self.assertEqual(server.session_report(self.job["id"], now=p.end + timedelta(days=2)), html)


class CurvePointTests(unittest.TestCase):
    """AC-030-06/15: curve points come only from this job and only from measured pairs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = server.DB
        server.DB = str(Path(self.tmp.name) / "curve.db")
        server.setup()
        cfg = model.empty()
        cfg["building"]["heat_source"] = {"profile": "buderus-emsesp-hmc310", "source": "ha", "emsesp_host": ""}
        server.save_v2(cfg)
        self.cfg = server.load_v2()

    def tearDown(self):
        server.DB = self.old
        self.tmp.cleanup()

    def put(self, sid, at, eid, value, valid=1):
        with server.connect() as c:
            c.execute("INSERT INTO samples(at,entity_id,state,unit,valid,quality,normalized_value,normalized_unit,session_id)"
                      " VALUES(?,?,?,?,?,?,?,?,?)", (at, eid, str(value), "°C", valid, "ok" if valid else "unavailable",
                                                    value if valid else None, "°C", sid))

    def test_pairs_of_this_job_only(self):
        for i, (out, flow) in enumerate(((-2.0, 46.0), (0.0, 44.0), (3.5, 41.0))):
            at = f"2026-12-0{i + 1}T03:00:00+00:00"
            self.put("ma_a", at, "sensor.thermostat_dampedoutdoortemp", out)
            self.put("ma_a", at, "sensor.thermostat_hc1_targetflowtemp", flow)
        self.put("ma_a", "2026-12-05T03:00:00+00:00", "sensor.thermostat_hc1_targetflowtemp", 50.0)   # no partner
        self.put("ma_a", "2026-12-06T03:00:00+00:00", "sensor.thermostat_dampedoutdoortemp", 1.0)
        self.put("ma_a", "2026-12-06T03:00:00+00:00", "sensor.thermostat_hc1_targetflowtemp", 0, valid=0)
        self.put("ma_b", "2026-12-01T03:00:00+00:00", "sensor.thermostat_dampedoutdoortemp", -9.0)       # other job
        self.put("ma_b", "2026-12-01T03:00:00+00:00", "sensor.thermostat_hc1_targetflowtemp", 52.0)
        points, damped, meta = server.curve_points("ma_a", self.cfg)
        self.assertTrue(damped)
        self.assertEqual(points, [(-2.0, 46.0), (0.0, 44.0), (3.5, 41.0)])
        self.assertEqual((meta["n"], meta["from"], meta["to"]),
                         (3, "2026-12-01T03:00:00+00:00", "2026-12-03T03:00:00+00:00"))
        self.assertEqual(meta["outdoor"], "sensor.thermostat_dampedoutdoortemp")
        svg = report._curve_svg(points, damped)
        self.assertEqual(svg.count("<circle"), 3)
        self.assertIn("0 °C", svg)
        self.assertIn("gedämpft", svg)
        self.assertNotIn("<path", svg)

    def test_no_profile_no_points(self):
        server.save_v2(model.empty())
        self.assertEqual(server.curve_points("ma_a", server.load_v2())[0], [])
        self.assertIn("Keine gemeinsamen Messwerte", report._curve_svg([], True))
