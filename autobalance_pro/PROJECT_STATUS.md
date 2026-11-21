# AutoBalance Pro – überprüfter Projektstatus
**Stichtag: 21.11.2026** · Dokumentationssnapshot, keine neue Funktionsfreigabe.

## Quellen und Repositories

- Code: [bastian1607/jodeit – autobalance_pro](https://github.com/bastian1607/jodeit/tree/main/autobalance_pro), maßgeblich ist `main`.
- Fachkonzept: [Outline · AutoBalance Pro](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-pilotplanung-nachtmessung-und-einkaufsvorschlag-9LHNVzzweD).
- Rückmeldungen: [Outline · Backlog](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-testfeedback-und-entwicklungsbacklog-jLA2PXPFLS).
- Prozess: [Outline · KI-Zusammenarbeit](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-zusammenarbeit-chatgpt-codex-github-und-outline-Qbp3dKsZxM); Kurzregeln: [CLAUDE.md](CLAUDE.md).

## Nachweisbarer Entwicklungsstand

- Aktuelle **Manifest- und Runtime-Version: v0.1.2** (GitHub-Dateien `config.yaml` und `app/version.py` am 21.11.2026 gelesen).
- Home-Assistant-App mit Ingress-Web-UI, Raumanlage und Solltemperatur, mehreren Raumtemperatur- und Fensterkontakt-Entitäten, mehreren Heizkörpern, thermischen Zonen, Wärmepumpen-Entitätszuordnung (einschließlich möglicher BBQKees-/EMS-ESP-Werte aus HA), lokaler SQLite-Persistenz und zyklischer rein lesender Erfassung.
- v0.1.1: suchbare HA-Entitäts-Dropdowns, vorläufige dunkle UI-Farbgebung.
- v0.1.2: gemeinsame Runtime-Version, Status/Datenqualität mit Temperatur-Plausibilität, °F→°C-Normalisierung, HA-Abfragefehleranzeige, Aktualitätsprüfung, additive SQLite-Migration.
- Laut [Outline-Testnachweis](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-testfeedback-und-entwicklungsbacklog-jLA2PXPFLS): PR #2 und #3 gemergt, **17 lokale Tests** zum damaligen Release bestanden. Dokumentierter HA-Rollout von 0.1.0 auf 0.1.2 mit lokaler App-Sicherung `92a4d2e2`; Supervisor-Start, /health, UI-Auslieferung, Konfigurationserhalt und erste reale lesende Messwerte geprüft. Diese Aussagen sind **historisch dokumentierte Testergebnisse**, keine neue Prüfung am 21.11.2026.
- Ein vollständiger realer Browser-/Ingress-Bedientest, Langzeitbeobachtung sowie echte Heizungs-/Hydrauliktests sind **nicht bestätigt**.
- Der Live-Betriebsstatus einer HA-Anlage wurde bei dieser Dokumentationsaktualisierung **nicht erneut aus Home Assistant gelesen**.

## Nicht umgesetzt / nicht freigegeben

- Automatische Stellversuche und Ventilsteuerung.
- Verlässliche hydraulische l/h-Berechnung aus Heizlast, Heizkörper- und Ventildaten.
- Vollständige 30-Tage-Messanalyse, nächtliche Versuchsauswertung, Heizkurvenempfehlung und Abschlussbericht.
- Nachgewiesene Kompatibilität NodOn-Positionierung ↔ konkretes Heimeier-Eclipse-Ventil.
- Echte mehrkreisige Wärmepumpen-/FBH-Optimierung sowie Anlagen-Fail-safe.

## Offene UI- und Qualitätsthemen

- AB-001: originales Jodeit-Logo; offizielle CI-Werte prüfen.
- AB-002/003: v0.1.1 implementiert; noch ausstehende reale UI-/Ingress-/Mobil-Abnahme.
- AB-004: Zusatzwünsche zurückgestellt.
- AB-005–007: Status und Testnachweise im aktuellen Outline-Backlog maßgeblich; beim nächsten Auftrag frisch lesen.

## Vereinbarte Rollen ab 21.11.2026

**ChatGPT:** UX/UI, Produktdesign, funktionale Anforderungen und fachliches Review.
**Claude:** maßgeblicher Programmierpartner, Code-Änderungen, Tests und PRs.
**Bastian/Jodeit:** Priorisierung, Freigabe, reale HA-/Anlagentests.
**Outline:** führende Dokumentation; **GitHub:** führende Codebasis.
**Codex:** optional unterstützend, keine parallele unkoordinierte Programmierung.

## Nächste sinnvolle Schritte (noch keine Implementierungsfreigabe)

1. Reale HA-Bedienung der v0.1.2 mit Raum-/Zonenverwaltung, Entitätensuche und Reconnect prüfen und Ergebnis im Backlog dokumentieren.
2. ChatGPT gestaltet und spezifiziert offene UI-Fragen, insbesondere Jodeit-Logo/Design und verständliche Statusanzeigen.
3. Claude setzt **freigegebene** Aufgaben isoliert in Branches mit Tests und PRs um.
4. Erst nach Messqualität und abgesicherten technischen Nachweisen Entwicklungsauftrag für hydraulische Berechnung und aktive Stellversuche freigeben.

**Quellenprüfung am 21.11.2026:** GitHub `config.yaml`, `app/version.py`, README, UI-/Backend-Dateien und Outline-Projekt-/Backlogseiten eingelesen. Die Angaben sind eine Momentaufnahme.
