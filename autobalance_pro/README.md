# Jodeit AutoBalance Pro v0.1.1

**Stand 20.11.2026: Erste lauffähige, rein lesende Home-Assistant-App. Nicht für automatische Heizungssteuerung freigegeben.**

## Installation über das vorhandene Jodeit-Repository

1. In Home Assistant **Einstellungen → Apps → App Store** öffnen.
2. Prüfen, ob `https://github.com/bastian1607/jodeit` bereits als App-Repository hinterlegt ist; andernfalls unter Repositories hinzufügen.
3. App-Store neu laden. **Jodeit AutoBalance Pro** suchen.
4. Installieren und starten; Seitenleisten-Eintrag öffnen.
5. Räume, Sensoren, Fensterkontakte, Heizkörper und thermische Zonen einrichten. BBQKees/EMS-ESP-Messwerte sind auswählbar, sofern sie als HA-Entitäten vorhanden sind.

**Hinweis:** Die App ist für Home Assistant OS/Supervised konzipiert; Installation über den HA App Store wurde noch nicht an einem echten System getestet. Keine Router-Portfreigabe erforderlich.

## Änderungen in v0.1.1

- Mehrfachauswahl für Raumtemperatur- und Fensterkontakte als suchbares Dropdown (Anzeigename und Entity-ID).
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
- Noch keine Sensorqualitätsprüfung, Heizlastberechnung, hydraulische Empfehlung, 30-Tage-Analyse oder Heizkurvenoptimierung.
- Ventilantriebe werden **nicht angesteuert**.
- Für Stellversuche müssen Mindestvolumenstrom, Herstellergrenzen, mechanische Kompatibilität und Ausfallverhalten geprüft sein.
- Motorhub-Prozentwerte sind keine l/h-Werte.

## Entwicklung / Tests

```bash
cd autobalance_pro
python -m unittest discover -s tests
```

Für lokalen HTTP-Test:
```bash
AUTO_BALANCE_DB=/tmp/autobalance-test.db PORT=8099 python app/server.py
```
Die Entitätenliste funktioniert nur im Home-Assistant-App-Kontext mit `SUPERVISOR_TOKEN`.

Weiteres Vorgehen: HA-OS-Installationstest, Datenqualität/30-Tage-Analyse, Berechnungsengine, später sichere Stellversuche.

Projektdokumentation: [AutoBalance in Outline](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-pilotplanung-nachtmessung-und-einkaufsvorschlag-9LHNVzzweD).
