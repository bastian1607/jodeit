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
    return {"profile": "", "source": "ha", "emsesp_host": "", "circuit_id": "", "autofilled": {}}


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


def numeric_temperature_entities(heat_source):
    """AB-032: measured and derived °C values of the profile are stored as numbers.

    Only "measurement" fields get the timestamp/age check (temperature_entities); derived values such as the
    damped outdoor temperature or the flow target report on change only. Controller settings ("setting") stay
    as reported (parameter display and AB-029 comparison unchanged)."""
    return {i for i, f in entities(heat_source).items() if f["unit"] == "°C" and f["kind"] in ("measurement", "derived")}


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
    circuit_id = heat_source.get("circuit_id") or ""
    if not isinstance(circuit_id, str) or len(circuit_id) > 80:
        raise ValueError("Heizkreis des Geräteprofils ungültig")
    autofilled = heat_source.get("autofilled") or {}
    if not isinstance(autofilled, dict) or len(autofilled) > 40:
        raise ValueError("Herkunftsangaben ungültig")
    heat_source.pop("switch_confirmed", None)
    heat_source.update(profile=key, source=source, emsesp_host=host, circuit_id=circuit_id, autofilled=autofilled)
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


# AB-022: fields that must be present before a profile is proposed automatically.
CORE_FIELDS = ("outdoor_damped", "flow_target", "flow", "curve_design", "curve_offset")
DETECT_SHARE = 0.9


def detect(ha_states):
    """Read-only: which profiles match the entities that already exist in Home Assistant?

    A profile is *proposed* only if all core fields and at least 90 % of all fields are found.
    Nothing is saved; the user confirms with one click.
    """
    results = []
    for key, prof in PROFILES.items():
        r = probe({"profile": key, "source": "ha"}, ha_states or {})
        found = {f["key"] for f in r["fields"] if f["found"]}
        core_missing = [k for k in CORE_FIELDS if k not in found]
        results.append({"profile": key, "label": prof["label"], "found": r["found"], "total": r["total"],
                        "core_missing": core_missing,
                        "proposed": not core_missing and r["found"] >= DETECT_SHARE * r["total"]})
    results.sort(key=lambda x: (x["proposed"], x["found"]), reverse=True)
    return {"candidates": [x for x in results if x["found"]], "read_only": True}


# --------------------------------------------------------------------- AB-029 automatic take-over
# Configuration fields that can be filled from a read device profile (review 12).
# * Only empty fields are filled. Every automatic value gets a provenance record in
#   heat_source.autofilled[path] = {value, profile, source, circuit_id, read_at, changed_at}.
# * An automatic value follows the device while it is unchanged by the user and the profile, source and
#   circuit are the same. A value changed by the user is never overwritten.
# * Circuit fields go only to the circuit the profile is bound to (heat_source.circuit_id, or the only one).
# * The controller's minimum outdoor temperature is a curve parameter, never the design outdoor temperature.
# Nothing is written to the device (read-only).
AUTOFILL_ENTITIES = (            # (path, profile field) - HA entity ids, only for source "ha"
    ("building.entities.outdoor", "outdoor"),
    ("building.entities.dhw", "dhw_charging"),
    ("circuit.entities.flow", "flow"),
    ("circuit.entities.flow_target", "flow_target"),
    ("circuit.entities.return", "return"),
    ("circuit.entities.pump", "heating_pump"),
)
HEATING_TYPE_MAP = {"radiator": "radiators", "floor": "underfloor", "floorheating": "underfloor"}
CURVE_PARAMS = ("curve_design", "curve_base", "curve_offset", "curve_max", "min_ext", "room_set",
                "hp_min_flow", "control_mode", "room_influence", "solar_influence")
PROFILE_MANUFACTURER = {"buderus-emsesp-hmc310": "Buderus"}
PARAMS_PATH = "circuit.heating_curve.manufacturer_params"
REFERENCE_PATH = "circuit.heating_curve.reference"
AUTOFILL_LABELS = {
    "building.entities.outdoor": "Außentemperatur (Entität)",
    "building.entities.dhw": "Warmwasserbereitung (Entität)",
    "circuit.entities.flow": "Heizkreis Vorlauf (Entität)",
    "circuit.entities.flow_target": "Heizkreis Vorlauf-Soll (Entität)",
    "circuit.entities.return": "Heizkreis Rücklauf (Entität)",
    "circuit.entities.pump": "Heizkreispumpe (Entität)",
    "building.heat_pump.manufacturer": "Hersteller Wärmepumpe",
    "building.heating_system": "Heizsystem Gebäude",
    "circuit.emitter_type": "Heizflächenart Heizkreis",
    PARAMS_PATH: "Heizkurve laut Regler",
    REFERENCE_PATH: "Quelle Heizkurve",
}
AUTOFILL_STATUS_TEXT = {
    "auto": "Automatisch aus der Anlage übernommen",
    "manual": "Manuell hinterlegter Wert bleibt bestehen",
    "unclear": "Herkunft ungeklärt / Abgleich erforderlich",
    "stale_source": "Aus alter Quelle, Prüfung erforderlich",
    "unsupported": "Vom Profil nicht unterstützt",
    "unavailable": "Derzeit nicht erreichbar / kein gültiger Messwert",
    "rest_no_entity": "Nicht automatisch übernommen (REST ohne HA-Entität)",
    "no_circuit": "Nicht automatisch übernommen: Heizkreis für das Profil wählen",
    "multi_circuit": "Nicht automatisch übernommen: mehrere Heizkreise",
    "unmapped": "Nicht automatisch übernommen: Reglerwert nicht eindeutig",
    "matches_unconfirmed": "Vorhandener Wert stimmt mit der Anlage überein · Herkunft nicht bestätigt · automatische Nachführung aus",
    "empty": "Leer",
}


def _param(value):
    """Numbers as numbers (the form stores them that way), everything else as text."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return int(number) if number.is_integer() else number


def _empty(value):
    return value in (None, "") or value == {}


def bound_circuit(cfg):
    """The circuit a single-circuit profile (e.g. HMC310 HK1) belongs to, or None if not unambiguous."""
    circuits = cfg.get("circuits") or []
    wanted = (cfg["building"].get("heat_source") or {}).get("circuit_id") or ""
    if wanted:
        return next((c for c in circuits if c.get("id") == wanted), None)
    return circuits[0] if len(circuits) == 1 else None


def _node(cfg, path, circuit):
    head, *rest = path.split(".")
    node = cfg["building"] if head == "building" else circuit
    for key in rest[:-1]:
        node = node.get(key) if isinstance(node, dict) else None
    return node, rest[-1]


def _proposals(cfg, values):
    """{path: (value or None, reason)} for the configured profile and the currently valid readings."""
    source = cfg["building"].get("heat_source") or {}
    prof = profile(source.get("profile"))
    keys = {f["key"] for f in prof["fields"]}
    labels = {f["key"]: f["label"] for f in prof["fields"]}
    circuit = bound_circuit(cfg)

    def reading(key, convert=lambda v: v):
        if key not in keys:
            return None, "unsupported"
        if key not in values:
            return None, "unavailable"
        return convert(values[key]), "ok"

    out = {}
    for path, key in AUTOFILL_ENTITIES:
        if source.get("source", "ha") != "ha":
            out[path] = (None, "rest_no_entity")
        else:
            out[path] = reading(key, lambda v: v["entity_id"])
    out["building.heat_pump.manufacturer"] = ((PROFILE_MANUFACTURER[prof["key"]], "ok") if prof["key"] in PROFILE_MANUFACTURER
                                              else (None, "unsupported"))
    kind, why = reading("heating_type", lambda v: HEATING_TYPE_MAP.get(str(v["value"]).strip().lower()))
    if why == "ok" and kind is None:
        why = "unmapped"
    out["circuit.emitter_type"] = (kind, why)
    out["building.heating_system"] = (kind, why) if len(cfg.get("circuits") or []) == 1 else (None, "multi_circuit")
    params = {labels[k]: _param(values[k]["value"]) for k in CURVE_PARAMS if k in values}
    out[PARAMS_PATH] = (params or None, "ok" if params else "unavailable")
    if circuit is None:
        for path in list(out):
            if path.startswith("circuit."):
                out[path] = (None, "no_circuit")
        out["building.heating_system"] = (None, "no_circuit") if len(cfg.get("circuits") or []) == 1 else out["building.heating_system"]
    return out, circuit


def gateway(source):
    """Identity of the concrete data source (review 13): the REST gateway host; HA has no hardware identity."""
    return (source.get("emsesp_host") or "") if source.get("source", "ha") == "emsesp_rest" else ""


def _current_record(rec, source, circuit, path):
    return (isinstance(rec, dict) and rec.get("profile") == source.get("profile")
            and rec.get("source") == source.get("source", "ha") and (rec.get("gateway") or "") == gateway(source)
            and (not path.startswith("circuit.") or rec.get("circuit_id") == (circuit or {}).get("id")))


def autofill(cfg, values, now_iso, today):
    """AB-029: fill/refresh configuration fields from valid profile readings. Changes ``cfg`` in place.

    ``values`` maps profile field key -> {"entity_id", "value"} for currently valid readings.
    Returns the list of labels that were filled or refreshed in this run.
    """
    source = cfg["building"].get("heat_source") or {}
    if not profile(source.get("profile")):
        return []
    done = source.setdefault("autofilled", {})
    proposals, circuit = _proposals(cfg, values)
    changed = []

    def record(path, value, first=None):
        done[path] = {"value": value, "profile": source["profile"], "source": source.get("source", "ha"), "gateway": gateway(source),
                      "circuit_id": (circuit or {}).get("id") if path.startswith("circuit.") else None,
                      "read_at": first or now_iso, "changed_at": now_iso}

    for path, (new, _) in proposals.items():
        if new is None:
            continue
        node, key = _node(cfg, path, circuit)
        if node is None:
            continue
        current, rec = node.get(key), done.get(path)
        if _empty(current):
            node[key] = new
            record(path, new)
            changed.append(path)
        elif _current_record(rec, source, circuit, path) and rec["value"] == current:
            if current != new:
                node[key] = new
                record(path, new, rec.get("read_at"))
                changed.append(path)
        # Review 13, P0: a filled field without provenance is never taken over by value equality.
    # The source/date of the curve parameters follows their last real change (review 12).
    if circuit is not None and PARAMS_PATH in changed:
        node, key = _node(cfg, REFERENCE_PATH, circuit)
        rec = done.get(REFERENCE_PATH)
        if _empty(node.get(key)) or (_current_record(rec, source, circuit, REFERENCE_PATH) and rec["value"] == node.get(key)):
            text = f"{profile(source['profile'])['label']}, automatisch ausgelesen am {today}"
            node[key] = text
            record(REFERENCE_PATH, text, (rec or {}).get("read_at"))
            changed.append(REFERENCE_PATH)
    return [AUTOFILL_LABELS[p] for p in changed]


def autofill_status(cfg, values):
    """Persistent field status derived from the configuration and the current readings (review 12)."""
    source = cfg["building"].get("heat_source") or {}
    if not profile(source.get("profile")):
        return []
    done = source.get("autofilled") or {}
    proposals, circuit = _proposals(cfg, values)
    proposals[REFERENCE_PATH] = (None, proposals[PARAMS_PATH][1])
    rows = []
    for path, (new, reason) in proposals.items():
        node, key = _node(cfg, path, circuit) if (circuit is not None or not path.startswith("circuit.")) else (None, None)
        current = node.get(key) if node is not None else None
        rec = done.get(path)
        if not _empty(current) and isinstance(rec, dict):
            if not _current_record(rec, source, circuit, path):
                status = "stale_source" if rec.get("value") == current else "manual"
            else:
                status = "auto" if rec.get("value") == current else "manual"
        elif not _empty(current):
            status = "matches_unconfirmed" if new is not None and new == current else "unclear"
        else:
            status = reason if reason != "ok" else "empty"
        rows.append({"path": path, "label": AUTOFILL_LABELS[path], "status": status, "text": AUTOFILL_STATUS_TEXT[status],
                     "value": current, "read_at": (rec or {}).get("read_at") if status in ("auto", "stale_source") else None,
                     "changed_at": (rec or {}).get("changed_at") if status in ("auto", "stale_source") else None,
                     "source": (rec or {}).get("source") if status in ("auto", "stale_source") else None})
    return rows


def source_identity(cfg):
    """(profile, source, gateway, bound circuit) - a change of any part is a source switch (review 12/13)."""
    hs = cfg["building"].get("heat_source") or {}
    return (hs.get("profile") or "", hs.get("source") or "ha", gateway(hs), (bound_circuit(cfg) or {}).get("id"))


def adopt_existing(cfg, paths, values, now_iso):
    """Review 13, AC13-04: the installer explicitly lets the app follow an existing value. Only for filled fields
    without provenance that currently match the device. Returns the adopted labels."""
    source = cfg["building"].get("heat_source") or {}
    if not profile(source.get("profile")):
        raise ValueError("Kein Geräteprofil gewählt")
    proposals, circuit = _proposals(cfg, values)
    done = source.setdefault("autofilled", {})
    adopted = []
    for path in paths:
        if path not in proposals or path in done:
            continue
        new, _ = proposals[path]
        node, key = _node(cfg, path, circuit) if (circuit is not None or not path.startswith("circuit.")) else (None, None)
        if node is None or _empty(node.get(key)) or new is None or node.get(key) != new:
            continue
        done[path] = {"value": new, "profile": source["profile"], "source": source.get("source", "ha"), "gateway": gateway(source),
                      "circuit_id": (circuit or {}).get("id") if path.startswith("circuit.") else None,
                      "read_at": now_iso, "changed_at": now_iso, "adopted_by_installer": now_iso}
        adopted.append(AUTOFILL_LABELS[path])
    return adopted


def auto_values_to_clear(previous, cfg):
    """Paths whose value is still the automatic value of a *different* profile/source/circuit (review 12, P0)."""
    old = (previous["building"].get("heat_source") or {})
    new = (cfg["building"].get("heat_source") or {})
    if source_identity(previous) == source_identity(cfg):
        return []
    done = new.get("autofilled") or old.get("autofilled") or {}
    out = []
    for path, rec in done.items():
        if not isinstance(rec, dict) or path not in AUTOFILL_LABELS:
            continue
        circuit = next((c for c in cfg.get("circuits") or [] if c.get("id") == rec.get("circuit_id")), None)
        node, key = _node(cfg, path, circuit) if (circuit is not None or not path.startswith("circuit.")) else (None, None)
        if node is not None and rec.get("value") == node.get(key) and not _empty(node.get(key)):
            out.append((path, circuit))
    return out


def clear_auto_values(cfg, paths):
    """Remove automatic values of an old source; user values are not in ``paths`` and stay untouched."""
    for path, circuit in paths:
        node, key = _node(cfg, path, circuit)
        current = node.get(key)
        node[key] = {} if path == PARAMS_PATH else None if key in ("heating_system", "emitter_type") else ""
    cfg["building"]["heat_source"]["autofilled"] = {}
    return cfg
