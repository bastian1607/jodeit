# Jodeit HEMS

Minimaler Home-Assistant-Ableger für evcc mit festem Jodeit-Branding.

## Prinzip

- evcc selbst wird nicht geforkt.
- Basis ist das offizielle Docker-Image `evcc/evcc`.
- Die Add-on-Version entspricht der verwendeten evcc-Version.
- Jodeit ergänzt nur Home-Assistant-Verpackung und Branding.

## Persistente Daten

- Datenbank: `/data/evcc.db`
- YAML-Konfiguration: `/config/evcc.yaml`

`/data` bleibt bei normalen Add-on-Updates erhalten. `/config` ist der von Home Assistant bereitgestellte persistente `addon_config`-Ordner des Add-ons.

Beim ersten Start legt das Add-on eine leere `/config/evcc.yaml` an, falls noch keine vorhanden ist. Konfigurationen aus der EVCC-Weboberfläche werden weiterhin in der EVCC-Datenbank gespeichert.

## Wichtig

Das offizielle EVCC-Add-on und Jodeit HEMS dürfen nicht gleichzeitig laufen, da beide dieselben Netzwerkports verwenden.
