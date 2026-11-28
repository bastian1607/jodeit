# AutoBalance Pro – überprüfter Projektstatus
**Stichtag: 28.11.2026** · Dokumentationssnapshot, keine neue Funktionsfreigabe.

## Quellen

- Code: [bastian1607/jodeit – autobalance_pro](https://github.com/bastian1607/jodeit/tree/main/autobalance_pro), maßgeblich ist `main`.
- Outline: [Pilotplanung](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-pilotplanung-nachtmessung-und-einkaufsvorschlag-9LHNVzzweD) · [Backlog](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-testfeedback-und-entwicklungsbacklog-jLA2PXPFLS) · [AB-008 Datenmodell v2](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-ab-008-datenmodell-v2-entwurf-BUFcmJkMPt) · [AB-009 Oberfläche](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-ab-009-ubergabe-an-chatgpt-oberflache-fur-datenmodell-v2-wF0mbKy2cy) · [Zusammenarbeit](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-zusammenarbeit-chatgpt-codex-github-und-outline-Qbp3dKsZxM); Kurzregeln: [CLAUDE.md](CLAUDE.md).

## Stand v0.6.2 (Release freigegeben von Bastian am 28.11.2026)

- AB-030 Teil 2, Berichtslayout v2 (PR #28); Reviews ChatGPT 18 inkl. Nachbesserung.
- Tests: 235 lokale Tests inkl. Chromium und A4-PDF-Regression auf `main` `c7ebffe`. Papierausdruck mit echten Daten nach Installation.
- Offen: AB-031 (gesperrt), AB-028.

## Stand v0.6.1 (Release freigegeben von Bastian am 28.11.2026)

- AB-030 Teil 1 (PR #26); Reviews ChatGPT 16–17.
- Tests: 217 lokale Tests inkl. Chromium auf `main` `af8a7a1`. iPhone/HTTPS/Druck nach Installation.
- Offen: AB-030 Teil 2 (Layout v2), AB-031 (gesperrt), AB-028.

## Stand v0.6.0 (Release freigegeben von Bastian am 27.11.2026)

- AB-029 (PR #24); Reviews ChatGPT 12–15 in Outline.
- Tests: 215 lokale Tests inkl. Chromium auf `main` `49a9551`. HA-/Anlagentest (lesend) nach Installation.
- Offen: AB-028, HA-Backup/Restore-Prozess Messkoffer, AB-022 P1/P2, AB-016.

## Stand v0.5.0 (Release freigegeben von Bastian am 27.11.2026)

- AB-025 (PR #20), AB-026 (PR #21), AB-027 (PR #22); Reviews ChatGPT 7–11 in Outline.
- Tests: 195 lokale Tests inkl. Chromium auf `main` `918c0be`. HA-Test der Migration folgt nach Installation.
- Offen: AB-028, HA-Backup/Restore-Prozess Messkoffer, AB-022 P1/P2, AB-016.

## Stand v0.4.0 (Release freigegeben von Bastian am 27.11.2026)

- AB-013 (PR #14), AB-014 (PR #15), AB-020 AC-06 Messbericht (PR #16), AB-023 Profilerkennung (PR #17),
  AB-024 Messungen mit Freitext/Beginnen/Stoppen/Drucken (PR #18).
- Tests: 148 lokale Tests inkl. Chromium. Schwellen AB-014 sind Pilotwerte (Feldvalidierung offen).
- Offen: AB-022 P1/P2 (UX-Nacharbeit), Löschkonzept für Messungen, PDF-Import (AB-016), AC-04 externer
  Wächter (von Bastian nicht gewünscht).

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
