# Jodeit Home Assistant Apps

Dieses Repository enthält den minimalen Home-Assistant-Ableger **Jodeit HEMS** auf Basis des offiziellen EVCC-Docker-Images.

## Ziel

Keep It Simple:

- kein EVCC-Fork
- kein eigener EVCC-Code
- kein zusätzliches Framework
- offizielles `evcc/evcc` Image
- nur Home-Assistant-Verpackung + Jodeit-Branding

## Repository in Home Assistant hinzufügen

Nach Veröffentlichung dieses Ordners als GitHub-Repository:

1. Home Assistant → Einstellungen → Apps → App Store.
2. Menü → Repositories.
3. Die URL dieses GitHub-Repositories hinzufügen.
4. **Jodeit HEMS** installieren.

Das offizielle EVCC-Add-on muss vor dem Start gestoppt werden, weil beide dieselben EVCC-Ports verwenden.

## EVCC-Update freigeben

Die einzige reguläre Versionsänderung erfolgt in:

`jodeit_evcc/config.yaml`

Beispiel:

```yaml
version: "0.317.0"
```

Der Docker-Build verwendet automatisch dieselbe Version des offiziellen Images:

```dockerfile
FROM evcc/evcc:${BUILD_VERSION}
```

Ablauf:

1. Neue EVCC-Version prüfen.
2. `version:` auf die freizugebende Version ändern.
3. Commit/Push nach GitHub.
4. Home Assistant erkennt die höhere Add-on-Version als Update.
5. Jodeit löst das Update auf der Kundenanlage manuell aus.

Kein `latest`-Tag: Auf jeder Anlage bleibt dadurch klar erkennbar, welche EVCC-Version eingesetzt wird.

## Wechsel vom lokalen PoC auf die GitHub-Version

Ein lokal installiertes Add-on (`local_jodeit_evcc`) und die spätere GitHub-Version gelten für Home Assistant als unterschiedliche Add-ons. Vor dem Wechsel daher die bisherige EVCC-Konfiguration/Datenbank sichern bzw. gezielt migrieren. Beide Instanzen nicht gleichzeitig starten.

## Keine Kundendaten in dieses Repository

Nicht einchecken:

- `evcc.yaml` von Kunden
- `evcc.db`
- Passwörter oder Tokens
- Tailscale-Schlüssel
- Sponsor-Tokens
- kundenspezifische Konfiguration
