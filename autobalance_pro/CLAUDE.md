# AutoBalance Pro – Arbeitsregeln für Claude
Stand: 2026-11-21. Gilt für `autobalance_pro/`. Vor jedem Auftrag aktuellen Stand erneut prüfen.

## Zuständigkeiten

- **Bastian / Jodeit:** Product Owner. Priorisiert, testet und gibt Änderungen, PR-Merges, Releases und Eingriffe in Kundenanlagen ausdrücklich frei.
- **ChatGPT:** Produktkonzept, UX-/UI-Design, Informationsarchitektur, Design-Reviews, funktionale Anforderungen und fachliche Abnahmeempfehlungen. Gibt keine implizite Freigabe für Implementierung oder Anlagensteuerung.
- **Claude:** Führende Entwicklungsinstanz für Code, Detailarchitektur, Tests, Bugfixes, Git-Branches und Pull Requests. Klärt technische Unschärfen und dokumentiert Abweichungen von abgestimmten Designs.
- **Outline:** Führende Quelle für Anforderungen, Backlog, Entscheidungen, Teststatus und laufende Dokumentation.
- **GitHub:** Führende Quelle für Code und tatsächlich gemergte Versionen.

ChatGPT und Claude teilen **keinen automatisch synchronisierten Chat-Kontext**. Informationen ausschließlich über die aktuellen Outline-Seiten, GitHub-Code/PRs und explizite Arbeitsaufträge austauschen. Vor Änderungen beide Quellen frisch lesen.

## Pflichtlektüre bei jedem Auftrag

1. [Jodeit HEMS · Start hier](https://outline.bastianlahrmann.synology.me/doc/start-hier-jodeit-hems-dokumentation-obL06FyNkM)
2. [AutoBalance Pro · Pilotplanung](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-pilotplanung-nachtmessung-und-einkaufsvorschlag-9LHNVzzweD)
3. [AutoBalance Pro · Entwicklungsbacklog und Testfeedback](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-testfeedback-und-entwicklungsbacklog-jLA2PXPFLS)
4. [AutoBalance Pro · KI-Zusammenarbeit und Entwicklungsworkflow](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-zusammenarbeit-chatgpt-codex-github-und-outline-Qbp3dKsZxM)
5. [PROJECT_STATUS.md](PROJECT_STATUS.md) und die aktuellen Dateien in `autobalance_pro/`.

## Arbeitsablauf

1. **Erst lesen:** Outline, Backlog, `main`, bestehende Branches/PRs, Tests und tatsächliche App-Version. Abweichungen zwischen Dokumentation und Code festhalten.
2. **Nur beauftragte Arbeiten:** Backlog-Einträge sind keine automatische Entwicklungsfreigabe. Für UI-Änderungen sind ChatGPT-Designvorgaben maßgeblich, soweit Bastian sie freigegeben hat; fehlende oder widersprüchliche Anforderungen zunächst klären.
3. **Branch statt direkter Main-Änderung:** `feature/autobalance-AB-XXX-kurzname` oder `fix/autobalance-AB-XXX-kurzname`. Andere Jodeit-Apps im Repository nicht ohne Auftrag verändern.
4. **Umsetzung mit Qualitätssicherung:** gezielte Unit-/API-/Browser-Tests, Versionskonsistenz, Docker-Build, Ingress, Datenbankmigration und Rückwärtskompatibilität prüfen, soweit die jeweilige Testumgebung das zulässt. Testgrenzen klar nennen.
5. **Review:** Pull Request mit Backlog-ID, Änderungen, Tests, UI-Screenshots bei Designaufgaben, Risiken, Rollback und HA-Testschritten bereitstellen. **Kein Merge oder Release ohne ausdrückliche Freigabe.**
6. **Dokumentation:** neue bestätigte Ergebnisse und offene Punkte im selben Arbeitsgang in Outline pflegen; betroffene Seite vor Änderung erneut lesen. Datum, Quell-Commit/PR, Ergebnis, nächster Schritt. `PROJECT_STATUS.md` bei veränderter tatsächlich gemergter Version aktualisieren. Nicht behaupten, dass Code in HA getestet sei, wenn nur lokale Tests gelaufen sind.
7. **Freigabe durch Jodeit:** Bastian entscheidet über Produktivbetrieb, Kundentests und Aktionen an Heizungsanlagen.

## Sichere technische Grenzen

- Die derzeitige App ist ein **read-only Mess-/Konfigurationspilot**. Ohne **gesonderten, ausdrücklich freigegebenen und fachlich abgesicherten Auftrag** keine Ventile ansteuern, keine Wärmepumpen-Parameter schreiben, keine Wärmepumpen ausschalten, keine HA-Automationen oder Kundenzugänge ändern.
- Vor aktiven Versuchen: Stellmotor-Eignung und Fail-safe, Ventilkompatibilität, garantierten Mindestvolumenstrom, Kommunikations- und Gerätausfall, Abbruchbedingungen und hydraulische Grenzen nachweisen.
- Motorhub-Prozent `!=` gemessene Liter pro Stunde; mechanische Eclipse-Voreinstellung nur aus hydraulischer Berechnung und geeigneten Messbefunden ableiten.
- Thermische Zonen und hydraulische Heizkreise getrennt modellieren; mehrere Sensoren, Kontakte und Heizkörper pro Raum unterstützen.
- BBQKees/EMS-ESP über **tatsächlich vorhandene** Home-Assistant-Entitäten oder **lesend** per EMS-ESP-REST (`GET http://<host>/api/<gerät>`) anbinden (Architekturentscheidung 26.11.2026, AB-015). Keine MQTT-Publishes, keine HA-Dienste, keine EMS-ESP-Kommandos. Noch nicht verfügbare Werte nicht erfinden; Profilfelder nur nach Prüfung an realer Anlage aufnehmen.
- Lokal und ohne Cloud nutzbar halten; keine Secrets, Kundendaten, Tokens oder Passwörter in GitHub/Outline speichern. Nur Verweis auf Passwortmanager.

## Status und Kommunikationspflicht

`PROJECT_STATUS.md` ist eine datierte Momentaufnahme, kein Ersatz für GitHub-/Outline-Prüfung. Zwischenstufen ausdrücklich unterscheiden: **geplant**, **implementiert**, **lokal getestet**, **in HA getestet**, **an realer Anlage geprüft**. Fehlende Zugriffe offen benennen. Keine verdeckten, unaufgeforderten Code- oder Anlagenänderungen.

Codex kann unterstützend verwendet werden, ist aber **nicht** die standardmäßig zuständige Entwicklungsinstanz; diese Rolle liegt nach Entscheidung vom 21.11.2026 bei Claude.
