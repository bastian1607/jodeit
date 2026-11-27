# AutoBalance Pro – überprüfter Projektstatus
**Stichtag: 27.11.2026** · Dokumentationssnapshot, keine neue Funktionsfreigabe.

## Quellen

- Code: [bastian1607/jodeit – autobalance_pro](https://github.com/bastian1607/jodeit/tree/main/autobalance_pro), maßgeblich ist `main`.
- Outline: [Pilotplanung](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-pilotplanung-nachtmessung-und-einkaufsvorschlag-9LHNVzzweD) · [Backlog](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-testfeedback-und-entwicklungsbacklog-jLA2PXPFLS) · [AB-008 Datenmodell v2](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-ab-008-datenmodell-v2-entwurf-BUFcmJkMPt) · [AB-009 Oberfläche](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-ab-009-ubergabe-an-chatgpt-oberflache-fur-datenmodell-v2-wF0mbKy2cy) · [Zusammenarbeit](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-zusammenarbeit-chatgpt-codex-github-und-outline-Qbp3dKsZxM); Kurzregeln: [CLAUDE.md](CLAUDE.md).

## Stand v0.3.0 (Release freigegeben von Bastian am 27.11.2026)

- AB-020 (PR #10): Messlücken erkennen und anzeigen, Datenabdeckung je Entität, `/health/acquisition`.
- AB-015 (PR #11): lesendes Geräteprofil Buderus/EMS-ESP HMC310, Quelle HA (MQTT) oder EMS-ESP REST (nur GET), Übersicht „Wärmepumpe und Räume“, CSV-Export.
- AB-021 (PR #12): ID-Erzeugung auch in unsicheren Browserkontexten (`http://`).
- Tests: 100 lokale Tests inkl. Chromium. Offen: externer Wächter (AB-020 AC-04), Messsessions/Bericht, Räume ohne Fühler (AB-013), Kurvenmodell HMC310 (ChatGPT Auftrag 4).

## Stand v0.2.1 (Release freigegeben von Bastian am 26.11.2026)

- AB-011 (PR #8): Meldealter nur noch für Temperaturen; Kontakte/Status nach Erreichbarkeit.
- AB-010: geordnetes Beenden bei SIGTERM, Exit 0.
- Produktneuausrichtung 26.11.2026 (Heizkurven-Service, AB-012–AB-019) in Outline; noch keine Implementierung.

## Stand v0.2.0 (Release freigegeben von Bastian am 21.11.2026)

- **Gemergt:** PR #5 (AB-008 Datenmodell v2, enthält AB-001 Logo) und PR #6 (AB-009 Oberfläche v2). Versionsfreigabe über eigenen Release-PR.
- **Datenmodell v2:** Gebäude, Heizkreise (1–20), Räume, thermische Zonen, Heizflächen mit maßgeblichem Heizkreis. Raumheizlast nur „übernommen“ (Wert, Verfahren, Quelle, Datum) oder „fehlt“ – keine Schätzung. Umstellung beim ersten Start mit SQLite-Sicherung in `/data/backups/`; alte Tabelle bleibt als v1-Abbild für Downgrade erhalten.
- **Oberfläche:** Räume → Zonen → Heizungsanlage & Heizkreise → Heizflächen → Prüfen & Messung; Ventile nur Hersteller/Serie; Anteilsvorschlag nur bei vergleichbaren Daten.
- **Tests:** 69 lokale Tests (Backend, API, Migration, 18 Browsertests). Nicht lokal getestet: Docker-Build, ARM.
- **HA-Rollout:** Ergebnis im Outline-Backlog dokumentiert (Abschnitt „Rollout v0.2.0“).

## Nicht umgesetzt / nicht freigegeben

- Hydraulische Berechnung (l/h, Ventilvoreinstellung), Auswertung der 30-Tage-Messung, Heizkurvenempfehlung, Abschlussbericht.
- Aktive Stellversuche und jede Ansteuerung von Ventilen oder Wärmepumpe.
- FBH-Berechnung; geprüfter Ventilkatalog mit Varianten/Kennlinien; quadratisches App-Icon.

## Offene Fragen

- Anteilsvorschlag: Exponent als Pflicht und Solltemperatur als Auslegungs-Raumtemperatur – fachliche Bestätigung offen (AB-009-Seite).

**Quellenprüfung am 21.11.2026:** GitHub `main` und Outline-Seiten gelesen. Momentaufnahme.
