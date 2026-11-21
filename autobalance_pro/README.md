# Jodeit AutoBalance Pro v0.1.2

**Stand 20.11.2026: Erste lauffähige, rein lesende Home-Assistant-App. Nicht für automatische Heizungssteuerung freigegeben.**

## Projektstatus und Zusammenarbeit (ab 21.11.2026)

- [Aktueller, datierter Arbeitsstand](PROJECT_STATUS.md) für den Abgleich zwischen GitHub und Outline.
- [Arbeitsregeln für Claude](CLAUDE.md): ChatGPT übernimmt UX/UI- und Produktdesign sowie fachliches Review; **Claude ist federführend für Programmierung und Tests**. Bastian/Jodeit entscheidet über Umsetzung, Merge, Release und Kundenanlagen.
- Outline bleibt führende Produkt- und Backlog-Dokumentation: [Projektplanung](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-pilotplanung-nachtmessung-und-einkaufsvorschlag-9LHNVzzweD), [Backlog](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-testfeedback-und-entwicklungsbacklog-jLA2PXPFLS) und [Workflow](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-zusammenarbeit-chatgpt-codex-github-und-outline-Qbp3dKsZxM).
- Die App bleibt read-only. Neue Funktionen nur nach freigegebenem Auftrag, mit Branch, Tests, PR und dokumentiertem Status.

## Installation über das vorhandene Jodeit-Repository

1. In Home Assistant **Einstellungen → Apps → App Store** öffnen.
2. Prüfen, ob `https://github.com/bastian1607/jodeit` bereits als App-Repository hinterlegt ist; andernfalls unter Repositories hinzufügen.
3. App-Store neu laden. **Jodeit AutoBalance Pro** suchen.
4. Installieren und starten; Seitenleisten-Eintrag öffnen.
5. Räume, Sensoren, Fensterkontakte, Heizkörper und thermische Zonen einrichten. BBQKees/EMS-ESP-Messwerte sind auswählbar, sofern sie als HA-Entitäten vorhanden sind.

**Hinweis:** Die App ist für Home Assistant OS/Supervised konzipiert; Installation über den HA App Store wurde noch nicht an einem echten System getestet. Keine Router-Portfreigabe erforderlich.

## Änderungen in v0.1.2

Dieser freigegebene Release ergänzt v0.1.1. Manifest und Laufzeitversion sind 0.1.2.
UI, Health-Endpunkt und Übersicht verwenden dieselbe
Laufzeitversion aus `app/version.py`; ein Test gleicht sie mit `config.yaml` ab.

- Messqualität und Aktualität werden je Entität angezeigt. Temperaturwerte werden auf
  endliche Zahlen, unterstützte Einheiten und einen breiten Plausibilitätsbereich geprüft;
  Fahrenheit wird zusätzlich nach Celsius normalisiert. Rohwerte bleiben erhalten.
- HA-Abfrageausfälle werden mit letztem Versuch und letztem Erfolg lokal gespeichert und
  in einer eigenen Statusanzeige dargestellt. Eine erreichbare App bedeutet nicht, dass
  die HA-Messung funktioniert. `/health` bleibt ein Prozess-/HTTP-Lebenszeichen.
- Konfigurationsprüfung erfasst auch Heizkörper-/Wärmepumpen-Entitätsdomänen und
  numerische Eingaben. Speichern benötigt keine HA-Verbindung; die Existenz und
  Messqualität einer Entität werden erst beim lesenden Abruf geprüft. Das ist kein
  Nachweis direkter Ventilpositionssteuerung.
- Messwerte werden in der UI regelmäßig aktualisiert. Gelöschte Zuordnungen erscheinen
  nicht mehr in der aktuellen Messwertliste; ihre Historie bleibt in SQLite erhalten.

### Bedeutung der Qualitätsprüfung

Aktualität nutzt den HA-Zeitstempel `last_reported`, ersatzweise `last_updated`,
niemals `last_changed`: Ein gleichbleibender Temperaturwert ist nicht automatisch alt.
Nach 30 Minuten ohne Meldung gilt ein Wert als veraltet; fehlende/ungültige Zeitstempel
und Zeitstempel mehr als 60 Sekunden in der Zukunft gelten als ungültig.
Dies ist eine vorläufige Diagnosegrenze, keine Aussage über den Funkkontakt zum Gerät:
Integrationen melden unterschiedlich häufig, und HA kann Zustände wiederherstellen.
Bei Rückgriff auf `last_updated` können lange unveränderte Werte und Fensterzustände
vorsorglich als veraltet erscheinen; der Zeitstempel ist kein Funk-Lebenszeichen.
Fensterkontakte werden zusätzlich auf `on`/`off` geprüft. Temperaturwerte müssen °C
oder °F verwenden und normalisiert zwischen −60 und +150 °C liegen. Dieser breite
Bereich ersetzt keine geräte- oder raumspezifische Plausibilitätsprüfung.

### Datenbank, Backup und Rückkehr zur Vorversion

Vor dem ersten Start dieses Branches die App stoppen und `/data/autobalance.db` sichern
(bei laufender SQLite-Datenbank wäre eine konsistente SQLite-Backup-API statt einfacher
Dateikopie nötig). Die Datenbank wird beim Start um die Messwertspalten `source_at`, `quality`,
`normalized_value`, `normalized_unit` und die Tabelle `acquisition` additiv erweitert: vorhandene
Konfiguration und Rohmesswerte bleiben erhalten. Alte Messwerte besitzen keinen
nachträglichen Qualitätsnachweis und werden als ungeprüft behandelt.

Die Vorversion liest ihre bisherigen Spalten weiterhin; für eine vollständige Rückkehr
App stoppen, Vorversion installieren und die gesicherte Datenbank zurückspielen.
Dabei gehen nach der Sicherung erhobene Messwerte verloren. Kein manuelles SQL und keine
HA-Service-Aufrufe sind für die Aktualisierung vorgesehen.

## Änderungen in v0.1.1

- Suchbare Dropdowns für sämtliche HA-Entitätsauswahlen (Anzeigename und Entity-ID), mit Mehrfachauswahl bei Temperatur- und Fensterkontakten.
- Dezente dunkle Header- und Button-Farbgebung als vorläufige Jodeit-nahe Gestaltung; exakte CI-Farben und Original-Logo sind noch nicht bestätigt.
- Keine funktionale Veränderung der Heizungssteuerung: App bleibt read-only.

## Bereits im Quellcode

- Eigene Ingress-Oberfläche mit Raumverwaltung und gespeicherten Einstellungen.
- Mehrere Temperatur- und Fenster-/Türkontakt-Entitäten je Raum; mehrere Heizkörper und optionale Stellmotor-Zuordnung.
- Geschlossene Einzelzone pro Raum als Ausgangspunkt; eigene thermische Zonen aus mehreren Räumen.
- Wärmepumpenseite für Hersteller, Modell und relevante Messentitäten.
- 5-minütige read-only Abfrage der ausgewählten HA-Entitäten; lokale SQLite-Datenbank unter `/data`.
- Keine MQTT-Verbindung, kein Pairing und keine aktiven HA-Service-Aufrufe.

## Grenzen

- Der HA-App/Ingress-Integrationstest steht noch aus.
- Sensorqualität wird in v0.1.2 grundlegend geprüft; keine Gerätefähigkeitsprüfung, Heizlastberechnung, hydraulische Empfehlung, 30-Tage-Analyse oder Heizkurvenoptimierung.
- Ventilantriebe werden **nicht angesteuert**.
- Für Stellversuche müssen Mindestvolumenstrom, Herstellergrenzen, mechanische Kompatibilität und Ausfallverhalten geprüft sein.
- Motorhub-Prozentwerte sind keine l/h-Werte.

## Entwicklung / Tests

```bash
cd autobalance_pro
python -m unittest discover -s tests -v
```

Optionale Browserregression (Python-Paket `playwright` und Chromium erforderlich):
```bash
AUTO_BALANCE_UI_TESTS=1 python -m unittest discover -s tests -p test_ui.py -v
```
`PLAYWRIGHT_CHROMIUM_EXECUTABLE` kann den Browserpfad überschreiben.
Tests verwenden ausschließlich temporäre Datenbanken und synthetische HA-Entitäten;
Screenshots/Traces liegen standardmäßig unter `/tmp/autobalance-ui-artifacts`.
Sie ersetzen keinen Test im echten Home Assistant oder unter dessen Ingress.

Für lokalen HTTP-Test:
```bash
AUTO_BALANCE_DB=/tmp/autobalance-test.db PORT=8099 python app/server.py
```
Die Entitätenliste funktioniert nur im Home-Assistant-App-Kontext mit `SUPERVISOR_TOKEN`.

Weiteres Vorgehen: HA-OS-Installationstest, Datenqualität/30-Tage-Analyse, Berechnungsengine, später sichere Stellversuche.

Projektdokumentation: [AutoBalance in Outline](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-pilotplanung-nachtmessung-und-einkaufsvorschlag-9LHNVzzweD).
