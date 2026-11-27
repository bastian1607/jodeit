"""AB-015: read-only device profiles for heat sources (heat pump controller + heating curve).

Strictly read-only. This module contains no write path:
  * Home Assistant source: values are taken from the state list the app already polls.
  * EMS-ESP REST source: HTTP GET on ``http://<host>/api/<device>`` only (no body, no POST).
No MQTT publish, no HA service call, no EMS-ESP command. ``WRITE_ENABLED`` stays False;
``FORBIDDEN_WRITE`` names parameters that must never become writable, even if a later,
separately approved control feature is built (decision Claude/Bastian 2026-11-27).

Naming rule verified on Bastian's test plant (EMS-ESP 3.8.6, HMC310, 2026-11-27):
the REST path ``<device>[/<sub>]/<key>`` corresponds to the HA object id
``<device>_[<sub>_]<key>`` (e.g. thermostat/hc1/designtemp <-> number.thermostat_hc1_designtemp).
Thermostat paths were compared value by value; boiler REST paths follow the same rule but were
NOT fetched yet (``verified_rest: False``).
"""
import ipaddress
import json
import re
import urllib.request

WRITE_ENABLED = False

# Parameters whose change would silently alter the controller's behaviour. Never writable.
FORBIDDEN_WRITE = {
    "thermostat_hc1_remotetemp",     # fake room temperature "from remote" -> shifts flow via room influence
    "thermostat_hc1_remotehum",
    "thermostat_hc1_control",        # selected control device (RC310, …)
    "thermostat_intoffset",
    "thermostat_hc1_roominfluence",
    "thermostat_hc1_heatingtype",
    "thermostat_hc1_controlmode",
    "boiler_heatingoff", "boiler_heatingactivated", "boiler_selflowtemp", "boiler_heatingtemp",
    "boiler_reset", "boiler_manualdefrost", "boiler_auxheateronly", "boiler_auxheateroff",
}

SOURCES = {"ha": "Home Assistant (MQTT)", "emsesp_rest": "EMS-ESP direkt (REST, nur lesen)"}


def _f(key, label, device, path, domain, kind, unit="", verified_rest=True):
    sub, name = (path.split("/", 1) if "/" in path else (None, path))
    object_id = f"{device}_{sub}_{name}" if sub else f"{device}_{name}"
    return {"key": key, "label": label, "device": device, "path": path, "object_id": object_id,
            "domain": domain, "kind": kind, "unit": unit, "verified_rest": verified_rest}


# kind: "measurement" = reported continuously, stale-checked like a room temperature
#       "derived"/"setting"/"status" = reported on change only, checked for availability
PROFILES = {
    "buderus-emsesp-hmc310": {
        "key": "buderus-emsesp-hmc310",
        "label": "Buderus Wärmepumpe · BBQKees/EMS-ESP · Regler HMC310 · Heizkreis 1",
        "verified": "Bastians Testanlage, EMS-ESP 3.8.6, HMC310 (ProductID 158, Version 73.03), 27.11.2026",
        "fields": [
            _f("outdoor", "Außentemperatur", "boiler", "outdoortemp", "sensor", "measurement", "°C", False),
            _f("outdoor_damped", "Außentemperatur gedämpft", "thermostat", "dampedoutdoortemp", "sensor", "derived", "°C"),
            _f("flow_target", "Vorlauf-Soll HK1 (berechnet)", "thermostat", "hc1/targetflowtemp", "sensor", "derived", "°C"),
            _f("flow", "Vorlauf", "boiler", "curflowtemp", "sensor", "measurement", "°C", False),
            _f("return", "Rücklauf", "boiler", "rettemp", "sensor", "measurement", "°C", False),
            _f("hp_flow_tc1", "Vorlauf Wärmeübertrager (TC1)", "boiler", "hptc1", "sensor", "measurement", "°C", False),
            _f("hp_return_tc0", "Rücklauf Wärmeübertrager (TC0)", "boiler", "hptc0", "sensor", "measurement", "°C", False),
            _f("heating_active", "Heizbetrieb aktiv", "boiler", "heatingactive", "binary_sensor", "status", "", False),
            _f("heating_pump", "Heizkreispumpe", "boiler", "heatingpump", "binary_sensor", "status", "", False),
            _f("dhw_charging", "Warmwasserbereitung", "boiler", "dhw/charging", "binary_sensor", "status", "", False),
            _f("hp_activity", "Wärmepumpe Betrieb", "boiler", "hpactivity", "sensor", "status", "", False),
            _f("curve_design", "Heizkurve Auslegungstemperatur", "thermostat", "hc1/designtemp", "number", "setting", "°C"),
            _f("curve_base", "Heizkurve Fußpunkt", "thermostat", "hc1/baseflowtemp", "number", "setting", "°C"),
            _f("curve_offset", "Heizkurve Parallelverschiebung", "thermostat", "hc1/offsettemp", "number", "setting", "K"),
            _f("curve_max", "Maximale Vorlauftemperatur", "thermostat", "hc1/maxflowtemp", "number", "setting", "°C"),
            _f("min_ext", "Minimale Außentemperatur (Auslegung)", "thermostat", "minexttemp", "number", "setting", "°C"),
            _f("room_set", "Raumsoll Regler", "thermostat", "hc1/seltemp", "number", "setting", "°C"),
            _f("hp_min_flow", "Minimaler Vorlauf WP", "thermostat", "hc1/hpminflowtemp", "number", "setting", "°C"),
            _f("control_mode", "Regelungsart", "thermostat", "hc1/controlmode", "select", "setting"),
            _f("heating_type", "Heizsystem laut Regler", "thermostat", "hc1/heatingtype", "select", "setting"),
            _f("mode", "Betriebsart", "thermostat", "hc1/mode", "select", "setting"),
            _f("room_influence", "Raumeinfluss", "thermostat", "hc1/roominfluence", "number", "setting", "K"),
            _f("solar_influence", "Solareinfluss", "thermostat", "hc1/solarinfl", "number", "setting", "K"),
            _f("building", "Gebäudetyp (Dämpfung)", "thermostat", "building", "select", "setting"),
            _f("damping", "Dämpfung Außentemperatur", "thermostat", "damping", "switch", "setting"),
            _f("reduce_mode", "Absenkart", "thermostat", "hc1/reducemode", "select", "setting"),
            _f("dhw_disinfecting", "Thermische Desinfektion", "thermostat", "dhw/disinfecting", "switch", "setting"),
            _f("dhw_disinfect_day", "Desinfektion Wochentag", "thermostat", "dhw/disinfectday", "select", "setting"),
            _f("dhw_disinfect_time", "Desinfektion Uhrzeit", "thermostat", "dhw/disinfecttime", "number", "setting", "min"),
            _f("last_code", "Letzte Störung", "thermostat", "lastcode", "sensor", "status"),
        ],
    },
}


def empty_source():
    return {"profile": "", "source": "ha", "emsesp_host": ""}


def profile(key):
    return PROFILES.get(key or "")


def entity_id(field, source):
    """Stable id under which a field is stored in ``samples``."""
    return f"emsesp.{field['object_id']}" if source == "emsesp_rest" else f"{field['domain']}.{field['object_id']}"


def entities(heat_source):
    """{stored_id: field} for the configured profile; empty if none."""
    heat_source = heat_source or {}
    prof = profile(heat_source.get("profile"))
    if not prof:
        return {}
    source = heat_source.get("source", "ha")
    return {entity_id(f, source): f for f in prof["fields"]}


def temperature_entities(heat_source):
    return {i for i, f in entities(heat_source).items() if f["kind"] == "measurement" and f["unit"] == "°C"}


# ------------------------------------------------------------------ validation

_HOSTNAME = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)*$")
_LOCAL_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".fritz.box", ".internal")


def normalise_host(value):
    """Accept 'ip', 'ip:port', 'name', 'name.local[:port]' in the local network only."""
    if not isinstance(value, str):
        raise ValueError("EMS-ESP-Adresse ungültig")
    text = value.strip().lower()
    for prefix in ("http://", "https://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = text.rstrip("/")
    host, _, port = text.partition(":")
    if port and (not port.isdigit() or not 1 <= int(port) <= 65535):
        raise ValueError("EMS-ESP-Port ungültig")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if not (ip.is_private or ip.is_link_local) or ip.is_loopback or ip.version != 4:
            raise ValueError("EMS-ESP-Adresse muss eine lokale IPv4-Adresse sein (z. B. 192.168.x.x)")
    elif not _HOSTNAME.fullmatch(host) or ("." in host and not host.endswith(_LOCAL_SUFFIXES)):
        raise ValueError("EMS-ESP-Adresse muss eine lokale IP-Adresse oder ein lokaler Gerätename sein")
    return host + (":" + port if port else "")


def validate_source(heat_source):
    if heat_source is None:
        return empty_source()
    if not isinstance(heat_source, dict):
        raise ValueError("Wärmepumpen-Datenquelle ungültig")
    key = heat_source.get("profile") or ""
    if key and key not in PROFILES:
        raise ValueError("Unbekanntes Geräteprofil")
    source = heat_source.get("source") or "ha"
    if source not in SOURCES:
        raise ValueError("Unbekannte Datenquelle")
    host = heat_source.get("emsesp_host") or ""
    if key and source == "emsesp_rest":
        if not host:
            raise ValueError("Für EMS-ESP direkt bitte die IP-Adresse eintragen")
        host = normalise_host(host)
    elif host:
        host = normalise_host(host)
    heat_source.update(profile=key, source=source, emsesp_host=host)
    return heat_source


# ------------------------------------------------------------------ reading

MAX_RESPONSE = 1_000_000


def fetch_device(host, device, timeout=10, opener=urllib.request.urlopen):
    """GET http://<host>/api/<device> – the only network access of this module."""
    if device not in ("thermostat", "boiler"):
        raise ValueError("Unbekanntes EMS-ESP-Gerät")
    url = f"http://{normalise_host(host)}/api/{device}"
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    with opener(request, timeout=timeout) as response:
        body = response.read(MAX_RESPONSE + 1)
    if len(body) > MAX_RESPONSE:
        raise ValueError("Antwort von EMS-ESP zu groß")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("EMS-ESP lieferte kein JSON-Objekt")
    return data


def rest_value(data, path):
    node = data
    for part in path.split("/"):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return None if isinstance(node, (dict, list)) else node


def _state_text(value):
    if value is None:
        return "unavailable"
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def read_rest(heat_source, now_iso, fetch=None):
    """Read the profile via REST. Returns ({stored_id: HA-like state dict}, {device: error})."""
    fetch = fetch or fetch_device
    prof = profile(heat_source.get("profile"))
    states, errors, payload = {}, {}, {}
    if not prof:
        return states, errors
    for device in sorted({f["device"] for f in prof["fields"]}):
        try:
            payload[device] = fetch(heat_source["emsesp_host"], device)
        except Exception as exc:  # network, JSON, HTTP errors -> fields become unavailable
            errors[device] = type(exc).__name__
    for field in prof["fields"]:
        data = payload.get(field["device"])
        value = rest_value(data, field["path"]) if data is not None else None
        sid = entity_id(field, "emsesp_rest")
        states[sid] = {"entity_id": sid, "name": field["label"], "state": _state_text(value),
                       "unit": field["unit"], "device_class": "", "source_at": now_iso}
    return states, errors


def probe(heat_source, ha_states=None, now_iso="", fetch=None):
    """Read-only connection test: what does the profile find right now?"""
    heat_source = validate_source(dict(heat_source or {}))
    prof = profile(heat_source["profile"])
    if not prof:
        raise ValueError("Bitte ein Geräteprofil wählen")
    errors = {}
    if heat_source["source"] == "emsesp_rest":
        states, errors = read_rest(heat_source, now_iso, fetch)
    else:
        states = ha_states or {}
    rows = []
    for sid, field in entities(heat_source).items():
        state = (states.get(sid) or {}).get("state")
        found = state not in (None, "unavailable", "unknown", "")
        rows.append({"key": field["key"], "label": field["label"], "entity_id": sid, "kind": field["kind"],
                     "value": state if found else None, "unit": field["unit"], "found": found})
    return {"profile": prof["key"], "source": heat_source["source"], "errors": errors,
            "found": sum(r["found"] for r in rows), "total": len(rows), "fields": rows}
