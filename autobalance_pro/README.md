# Jodeit AutoBalance Pro v0.4.0

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

## Messdaten, Aufbewahrung und Löschung (AB-025, AB-026)

- Jede Messung hat eine eigene ID. Messwerte, die während einer Messung erfasst werden, tragen diese ID.
  Auswertung, Bericht und CSV einer Messung lesen nur diese Werte. Beim Start wird die Konfiguration samt
  Geräteprofil gespeichert, beim Stoppen werden Auswertung und Bericht unverändert festgeschrieben.
- **Automatische Löschung nur im Messkoffer-Modus** (App-Option `messkoffer`, Standard `false`; Entscheidung
  Bastian 27.11.2026): Messwerte, Konfigurations-Snapshots, Bericht, Bezeichnung und Notiz einer Messung
  30 Kalendertage nach Messende (interne Jodeit-Regel, keine gesetzliche Frist). Die Frist wird durch Export,
  Bericht oder Neustart nicht verlängert. Laufende Messungen werden nie automatisch gelöscht. Hinweise 7 Tage
  vorher (ohne bestätigte Ablage) und am letzten Tag. Verpasste Fristen werden beim nächsten Start nachgeholt.
  Messwerte außerhalb von Messungen, Konfigurationsverlauf und App-Ereignisse werden dort nach 30 Tagen gelöscht.
- **Stationärer Betrieb (Standard):** keine automatische Löschung. Manuelles Löschen, „Messgerät für nächsten
  Kunden vorbereiten“ und das Löschregister gelten trotzdem.
- „Messung jetzt vom Messgerät löschen …“ (Bezeichnung eingeben) und „Messgerät für nächsten Kunden vorbereiten …“
  in Schritt 5, jeweils als Formular im Seiteninhalt mit „Vorgang abbrechen“. Das Vorbereiten löscht immer alle
  Messungen; Messungen ohne bestätigte Ablage in der Kundenakte nur nach ausdrücklicher Bestätigung (sonst
  bleibt alles unverändert).
- Physische Bereinigung: `PRAGMA secure_delete`, WAL-Checkpoint und `VACUUM`. Lokale App-Sicherungen in
  `/data/backups` werden **nicht nach Dateidatum gelöscht**, sondern bereinigt: gelöschte und abgelaufene Messungen
  per Messungs-ID (bei alten Schemata per Messzeitraum aus dem Löschprotokoll) sowie lose Daten > 30 Tage,
  danach `VACUUM` der Sicherung. Nur beim bestätigten Vorbereiten für den nächsten Kunden werden sie entfernt.
- Löschregister ohne personenbezogene Daten (ID, Zeitpunkt, Grund) in `/share/autobalance/geloeschte-messungen.json`
  (dafür ist der Ordner `share` eingebunden): Nach Wiederherstellung einer reinen App-Sicherung löscht die App
  bereits gelöschte Messungen beim Start erneut. Ein beschädigtes Register wird beiseitegelegt und aus dem
  Löschprotokoll neu aufgebaut; ist `/share` nicht beschreibbar, zeigt die Übersicht einen Hinweis.
- Fehler eines Löschlaufs bleiben in der Übersicht sichtbar (`GET api/v2/retention` → `status`), bis ein späterer
  Lauf erfolgreich war. Neuer Versuch stündlich.
- **Konfiguration im Messkoffer-Modus:** Ist keine Messung mehr vorhanden und wurde die Konfiguration zuletzt
  vor dem Ende einer per Frist gelöschten Messung geändert, werden Räume, Fühlerzuordnung, Anlagenangaben,
  Konfigurationsverlauf und Messwerte außerhalb von Messungen entfernt, ebenso in den lokalen App-Sicherungen.
  Eine vorbereitete Konfiguration ohne abgelaufene Messung bleibt erhalten (keine Leerlauf-Frist).
- Beim Löschen einer Messung werden auch Messwerte außerhalb von Messungen von deren Fühlern entfernt, sofern die
  aktuelle Konfiguration sie nicht mehr verwendet.
- **Grenzen:** Home-Assistant-Sicherungen (vollständig oder App) enthalten den Datenstand zum Sicherungszeitpunkt;
  die App liest oder löscht sie nicht. Deren Aufbewahrung liegt beim Betreiber (Messkoffer: Betriebsprozess).
  Wird eine Vollsicherung inklusive `share` wiederhergestellt, gilt das Register dieses Zeitpunkts: abgelaufene
  Messungen werden beim Start erneut gelöscht, früher manuell gelöschte erscheinen bis zu ihrer ursprünglichen Frist
  wieder.
- **Betriebsgrenze Messkoffer (bis zu einer technischen Lösung):** Keine HA-Vollwiederherstellung mit echten
  Kundendaten ohne vorher extern verwahrte Liste der gelöschten Messungs-IDs (`/share/autobalance/…json`); nach
  einer Wiederherstellung vor der nächsten Kundenmessung bereinigen und prüfen. HA-Sicherungen auf dem Messkoffer
  dokumentiert der Techniker und ersetzt/löscht sie bei Fälligkeit gezielt (Betriebsanweisung in Outline).

## Änderungen in v0.4.0 (freigegeben 27.11.2026)

Weiterhin rein lesend: keine Steuerbefehle an Wärmepumpe, Ventile oder Home Assistant.

- AB-013: Räume ohne Temperaturfühler („nicht überwacht“) mit eigener Komfort-Solltemperatur;
  Auslegungs-Innentemperatur mit Quelle, getrennt vom Komfort-Soll (der Anteilsvorschlag nutzt nicht
  mehr das Komfort-Soll). Raumstatus, Abdeckung je Raum/Zone, begründete Messpunkt-Hinweise.
- AB-024: Messungen mit Freitext (Name, Auftrag/Angebot) und Notiz, Knöpfe „Messung beginnen“ /
  „Messung stoppen“ (auch in der Übersicht), Liste früherer Messungen mit „Bericht drucken“ und CSV.
- AB-014: Aussagekraft je Messung (keine Daten / unzureichend / Niveau / Niveau und Steigung) nach dem
  Pilotregelwerk (geeignete Nächte 22–06 Uhr, Störfenster, Außentemperaturband). Keine Parameterempfehlung.
- AB-020 AC-06: druckbarer Messbericht je Messung (`api/v2/sessions/<id>/report`), CSV je Messung;
  Übersicht zeigt „Abdeckung seit Messbeginn“.
- AB-023: Geräteprofil wird aus vorhandenen HA-Entitäten erkannt und mit einem Klick übernommen.
- Datenbank additiv: Tabellen `config_history` und `sessions`. Image enthält `tzdata` (Nächte in Ortszeit).
- Rückweg: v0.3.0 ignoriert die neuen Tabellen/Felder, lehnt aber Räume ohne Fühler beim Speichern ab.

## Änderungen in v0.3.0 (freigegeben 27.11.2026)

Weiterhin rein lesend: keine Steuerbefehle an Wärmepumpe, Ventile oder Home Assistant.

- AB-020: Messlücken über 15 Minuten werden erkannt, in Schritt 5 („Datenabdeckung der letzten 7 Tage“) und
  bei Unterbrechung als Banner angezeigt; Abdeckung je Entität ab ihrer ersten Messung. Neue Endpunkte
  `GET api/v2/data-quality`, `/health` mit Messstatus, `/health/acquisition` (503 nur, wenn die eigene
  Messschleife > 60 min keinen Versuch gemacht hat). Neue Tabelle `app_events` (App-Start/-Stopp), Index
  `samples(entity_id, at)`; beides additiv.
- AB-015: Lesendes Geräteprofil „Buderus · BBQKees/EMS-ESP · Regler HMC310 · Heizkreis 1“ (30 Werte:
  Heizkurve, Vorlauf-Soll, Vorlauf/Rücklauf, Außentemperatur, Betrieb, Warmwasser, Desinfektion, Störung).
  Quelle Home Assistant (MQTT) oder EMS-ESP direkt per REST (nur `GET /api/thermostat|boiler`, nur lokale
  Adressen). Einstellung in Schritt 3, Übersicht „Wärmepumpe und Räume · nur lesen“, CSV-Export
  (`api/v2/export.csv`). `WRITE_ENABLED=False`, Sperrliste für Schreibpfade.
- AB-021: Raum, Heizfläche und Zone lassen sich auch über `http://<LAN-IP>` anlegen (Ersatz für
  `crypto.randomUUID()` in unsicheren Browserkontexten).

## Änderungen in v0.2.1 (freigegeben 26.11.2026)

- AB-010: Sauberes Beenden. SIGTERM/SIGINT stoppen Messschleife und HTTP-Server geordnet, Exit-Code 0. Die Messpause wartet
  auf ein Stoppsignal statt 300 s fest zu schlafen. Behebt den Supervisor-Status „error“ nach dem Stoppen.

- AB-011: Das Meldealter (30 Minuten) wird nur noch für Temperaturwerte geprüft. Fenster-/Türkontakte und
  Statusentitäten (Verdichter, Warmwasser, Pumpe) melden nur bei Zustandswechsel; für sie zählen
  Erreichbarkeit (`unavailable`/`unknown`) und ein gültiger Zustand. Die Aktualität der eigenen Abfrage
  wird weiterhin für alle Entitäten geprüft. Anlass: reale Anlage, Kontakte dauerhaft als „Veraltet“ markiert.

## Änderungen in v0.2.0 (freigegeben 21.11.2026)

Enthält AB-001 (Logo), AB-008 (Datenmodell v2 mit Migration) und AB-009 (Oberfläche v2). Weiterhin rein lesend; keine Heizlastberechnung, keine hydraulische Empfehlung.

- AB-001: Original-Logo von Jodeit Systeme (`jodeit_evcc/jodeit-logo.png`, unverändert, SHA-256 `8e1593cf…7673`) im Header der Oberfläche und als `logo.png` für den HA App Store. Verwendung von Bastian am 21.11.2026 freigegeben.
- AB-008: Datenmodell v2 (Schemaversion 2) – Gebäude, mehrere hydraulische Heizkreise, Räume mit
  dokumentierter Heizlast, thermische Zonen, einzelne Heizflächen mit Heizkreis, Referenzleistung,
  Heizlastanteil und Ventilangaben. Details: [Outline AB-008](https://outline.bastianlahrmann.synology.me/doc/autobalance-pro-ab-008-datenmodell-v2-entwurf-BUFcmJkMPt).
  - **Heizlast wird nicht berechnet.** Nur `adopted` (Verfahren, Quelle, Datum Pflicht) oder `missing`.
    Ohne Heizlast laufen Messung und Diagnose weiter, Empfehlungen bleiben gesperrt.
  - Neue API: `GET/POST api/v2/config`, `GET api/v2/readiness` (fehlende Daten je Raum, keine Berechnung).
    `api/config` (v1) bleibt für die bestehende Oberfläche erhalten; Speichern darüber verliert keine v2-Felder.
  - Messwerte (`samples`) werden nicht verändert.

- AB-009: Oberfläche für Datenmodell v2 nach freigegebenem ChatGPT-Design (Bastian, 21.11.2026).
  Schritte **1 Räume → 2 Thermische Zonen → 3 Heizungsanlage & Heizkreise → 4 Heizflächen → 5 Prüfen & Messung**.
  Raumheizlast nur „Fehlt“/„Übernommen“ (Wert, Verfahren, Quelle, Datum); Heizflächen als Karten im Raum mit
  maßgeblichem Heizkreis, Referenzleistung, Heizlastanteil (Live-Summe, Vorschlag nur bei vergleichbaren Daten),
  Ventil nur Hersteller/Serie (`IMI Heimeier · Eclipse`, „Anderes“, „Unbekannt“) ohne Kennlinien;
  Heizkurve als Stützpunkte; Prüfseite mit Sprunglinks. Die Oberfläche speichert ausschließlich über `api/v2/config`.
  Neue API: `GET api/v2/catalog`, `POST api/v2/load-share-suggestion` (speichert nichts), erweitertes `api/v2/readiness`.

### Datenbank-Umstellung, Sicherung und Rückweg (AB-008)

- Beim ersten Start legt die App vor der Umstellung eine konsistente Sicherung über die SQLite-Backup-API an:
  `/data/backups/autobalance-before-v1-to-v2-<Zeitstempel>.db`. Umstellung in einer Transaktion; schlägt sie
  fehl, bleibt die alte Konfiguration unverändert und die App startet nicht (Supervisor-Log prüfen).
- v2 liegt in der neuen Tabelle `config_v2`. Die alte Tabelle `config` wird bei jedem Speichern als
  v1-Abbild mitgeschrieben. **Ein Downgrade auf v0.1.2 funktioniert daher ohne Wiederherstellung**
  (v2-Zusatzfelder sind dort nur nicht sichtbar). Änderungen unter v0.1.2 werden beim nächsten Start der
  neuen Version mit erneuter Sicherung übernommen.
- Vollständiger Rückweg: App stoppen, gewünschte Sicherung aus `/data/backups/` als `/data/autobalance.db`
  zurückkopieren (ggf. `-wal`/`-shm` entfernen), App starten – oder die HA-App-Sicherung wiederherstellen.
- Eine Konfiguration mit unbekannter, neuerer Schemaversion wird nicht überschrieben; die App startet dann nicht.


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
