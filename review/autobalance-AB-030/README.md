# AB-030 Teil 2 · Druckbilder zur visuellen Abnahme

Erzeugt aus PR #28, Commit `eadb3e0`, mit `AB030_PDF_OUT=… AUTO_BALANCE_UI_TESTS=1 python -m unittest tests.test_report_pdf`
(Chromium, A4, Druckmedium). Nur Testdaten, keine Kundendaten. Dieser Branch wird **nicht** gemergt.

| Variante | Seite 1 | Seite 2 |
|---|---|---|
| a · 2 Räume, viele Messpunkte | ![](a_2raeume_punkte-seite-1.png) | ![](a_2raeume_punkte-seite-2.png) |
| b · 0 Räume, keine Messpunkte | ![](b_0raeume_keine_punkte-seite-1.png) | ![](b_0raeume_keine_punkte-seite-2.png) |
| c · 3 Räume, langer Titel | ![](c_3raeume_langer_titel-seite-1.png) | ![](c_3raeume_langer_titel-seite-2.png) |
| d · 6 Räume, langer Titel, viele Punkte | ![](d_6raeume_langer_titel_punkte-seite-1.png) | ![](d_6raeume_langer_titel_punkte-seite-2.png) |

Alle vier Varianten als ein PDF: `AB-030_Druckvarianten.pdf`.
