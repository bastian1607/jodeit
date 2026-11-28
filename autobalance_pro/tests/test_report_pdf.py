"""AB-030 P1-01: A4 print regression - customer page fits on page 1, appendix starts on page 2.

Run: AUTO_BALANCE_UI_TESTS=1 python -m unittest tests.test_report_pdf
Set AB030_PDF_OUT=<dir> to keep the PDFs for visual review.
"""
import io
import os
import shutil
import sqlite3
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402
import test_report  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
    from pypdf import PdfReader
except ImportError:                                           # pragma: no cover
    sync_playwright = None

LONG = "Einfamilienhaus Musterstraße 123 · Pilotmessung Heizperiode 2026/27 mit sehr langem Auftragstitel zur Prüfung"
MANY = [(x / 10, 48 - 0.6 * x / 10 + (x % 7) * 0.3) for x in range(-120, 150)]
META = {"n": len(MANY), "from": "2026-12-01T12:00:00+00:00", "to": "2026-12-06T12:00:00+00:00",
        "target": "sensor.thermostat_hc1_targetflowtemp", "outdoor": "sensor.thermostat_dampedoutdoortemp"}


@unittest.skipUnless(os.environ.get("AUTO_BALANCE_UI_TESTS") == "1" and sync_playwright,
                     "Optional: set AUTO_BALANCE_UI_TESTS=1 and install playwright/Chromium/pypdf")
class A4PrintTests(test_report.LayoutV2Tests):

    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or shutil.which("chromium")
        cls.browser = cls.pw.chromium.launch(headless=True, executable_path=exe, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def variant(self, label, rooms_total=2, title=None, points=True):
        def change(cfg):
            base = next(r for r in cfg["rooms"] if r["name"] == "Bad")
            if rooms_total == 0:
                cfg["rooms"] = []
                cfg["emitters"] = []
            for i in range(max(0, rooms_total - 2)):
                cfg["rooms"].append(dict(base, id=f"room_x{i}", name=f"Kinderzimmer Obergeschoss Nord {i + 1}"))
        if title:
            with sqlite3.connect(server.DB) as c:
                c.execute("UPDATE sessions SET name=? WHERE id=?", (title, self.job["id"]))
        curve = (MANY, True, META) if points else ([], True, {"n": 0})
        with patch.object(server, "curve_points", return_value=curve):
            html, _ = self.html(change=change if rooms_total != 2 else None)
        page = self.browser.new_page()
        page.set_content(html)
        page.emulate_media(media="print")
        pdf = page.pdf(format="A4")
        page.close()
        out = os.environ.get("AB030_PDF_OUT")
        if out:
            Path(out).mkdir(parents=True, exist_ok=True)
            Path(out, f"{label}.pdf").write_bytes(pdf)
        return [p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf)).pages]

    def check(self, pages):
        self.assertGreaterEqual(len(pages), 2)
        first = pages[0]
        self.assertIn("Nur gemessen, nicht gesteuert", first)
        self.assertIn("Geprüft durch", first)                       # sign-off completely on page 1
        self.assertIn("Datum", first)
        self.assertNotIn("Anhang: Messwerte und Technik", first)
        self.assertTrue(pages[1].lstrip().startswith("Anhang: Messwerte und Technik"), pages[1][:120])

    def test_two_rooms_with_many_points(self):
        self.check(self.variant("a_2raeume_punkte"))

    def test_no_rooms_no_points(self):
        self.check(self.variant("b_0raeume_keine_punkte", rooms_total=0, points=False))

    def test_three_rooms_long_title(self):
        self.check(self.variant("c_3raeume_langer_titel", rooms_total=3, title=LONG))

    def test_six_rooms_long_title_many_points(self):
        pages = self.variant("d_6raeume_langer_titel_punkte", rooms_total=6, title=LONG)
        self.check(pages)
        self.assertIn("3 weitere Räume", pages[0])


for _name in [n for n in dir(test_report.LayoutV2Tests) if n.startswith("test_")]:
    setattr(A4PrintTests, _name, None)          # run only the print tests here


if __name__ == "__main__":
    unittest.main()
