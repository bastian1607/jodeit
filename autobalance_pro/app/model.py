"""AutoBalance Pro configuration model, schema version 2 (AB-008).

Pure functions only: no database, no network, no Home Assistant access.

Schema v2 separates:
  building  – the whole installation (heat pump, design outdoor temperature)
  circuits  – hydraulic heating circuits (supply, design temperatures, curve)
  rooms     – heat load and set point, sensors
  zones     – thermally connected rooms (independent of circuits)
  emitters  – individual radiators / UFH loops; ``circuit_id`` is the
              authoritative hydraulic assignment

Room heat load is never estimated by AutoBalance (decision Bastian 2026-11-21).
It is either ``adopted`` from a documented calculation (method, reference and
date required) or ``missing``. Missing heat load blocks calculations, never
passive measurement.

v1 (v0.1.x) configurations are converted with ``from_v1`` and the legacy UI
keeps working through ``to_v1`` / ``merge_v1``. Conversions are lenient
(legacy data is preserved as-is); strict checks happen in ``validate``.
"""
import copy
import json
import math
import re
import uuid
from datetime import date

SCHEMA_VERSION = 2
DEFAULT_CIRCUIT_ID = "hk1"

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
ENTITY_PATTERN = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")

HEATING_SYSTEMS = {"radiators", "underfloor", "mixed"}
CIRCUIT_MIXING = {"unmixed", "mixed"}
EMITTER_TYPES = {"panel", "column", "convector", "towel", "underfloor", "other"}
HEAT_LOAD_STATUS = {"adopted", "missing"}
OUTPUT_SOURCES = {"manufacturer", "document", "unknown"}

MAX_ROOMS = 100
MAX_EMITTERS = 500
MAX_CIRCUITS = 20
MAX_TEXT = 200


# ---------------------------------------------------------------- empty model

def empty_heat_load():
    return {"status": "missing", "value_w": None, "method": "", "reference": "",
            "author": "", "date": None, "legacy_value_w": None}


def empty_output(value=None):
    return {"value_w": value, "flow_c": None, "return_c": None, "room_c": None,
            "source": "unknown", "reference": ""}


def empty_valve(label=""):
    return {"label": label, "manufacturer": "", "series": "", "variant": "",
            "dimension": "", "current_setting": ""}


def empty_circuit(circuit_id=DEFAULT_CIRCUIT_ID, name="Heizkreis 1"):
    return {"id": circuit_id, "name": name, "mixing": None, "emitter_type": None,
            "design_flow_c": None, "design_return_c": None,
            "heating_curve": {"points": [], "manufacturer_params": {}, "note": "", "reference": ""},
            "entities": {"flow": "", "flow_target": "", "return": "", "pump": ""}}


def empty():
    return {
        "schema_version": SCHEMA_VERSION,
        "building": {
            "name": "", "heating_system": None,
            "design_outdoor_c": None, "design_outdoor_reference": "",
            "heat_pump": {"manufacturer": "", "model": "", "nominal_power_kw": None,
                          "min_flow_l_h": None, "min_flow_reference": ""},
            "entities": {"outdoor": "", "compressor": "", "dhw": ""},
        },
        "circuits": [empty_circuit()],
        "rooms": [],
        "zones": [],
        "emitters": [],
    }


def new_emitter_id():
    return "hf_" + uuid.uuid4().hex[:12]


# ------------------------------------------------------------- v1 conversion

def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _num(value):
    """Lenient number for legacy data: returns float or None, never raises."""
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if math.isfinite(n) else None


def _entity(value):
    return value if isinstance(value, str) else ""


def _list(value):
    return list(value) if isinstance(value, list) else []


def _emitter_from_v1(radiator, room_id, circuit_id, emitter_id=None):
    radiator = radiator if isinstance(radiator, dict) else {}
    return {
        "id": emitter_id or new_emitter_id(),
        "room_id": room_id,
        "circuit_id": circuit_id,
        "name": _text(radiator.get("name")),
        "type": None,
        "construction": "", "height_mm": None, "length_mm": None,
        # Legacy power_w had no reference temperatures: keep the number, mark the source unknown.
        "rated_output": empty_output(_num(radiator.get("power_w"))),
        "exponent": None,
        "load_share_percent": None,
        "valve": empty_valve(_text(radiator.get("valve_model"))),
        "actuator_entity": _entity(radiator.get("entity")),
    }


def from_v1(v1):
    """Convert a v0.1.x configuration to schema v2 without dropping data."""
    v1 = v1 if isinstance(v1, dict) else {}
    heating = v1.get("heating") if isinstance(v1.get("heating"), dict) else {}
    cfg = empty()
    cfg["building"]["heat_pump"]["manufacturer"] = _text(heating.get("manufacturer"))
    cfg["building"]["heat_pump"]["model"] = _text(heating.get("model"))
    for key in ("outdoor", "compressor", "dhw"):
        cfg["building"]["entities"][key] = _entity(heating.get(key))
    circuit = cfg["circuits"][0]
    circuit["name"] = _text(heating.get("circuit_name")) or "Heizkreis 1"
    circuit["entities"]["flow"] = _entity(heating.get("flow"))
    circuit["entities"]["return"] = _entity(heating.get("return"))
    for room in _list(v1.get("rooms")):
        if not isinstance(room, dict):
            continue
        room_id = room.get("id")
        heat_load = empty_heat_load()
        # A v1 heat load has no documented origin; it is kept for reference only.
        heat_load["legacy_value_w"] = _num(room.get("heat_load"))
        cfg["rooms"].append({
            "id": room_id,
            "name": _text(room.get("name")),
            "target": _num(room.get("target")) if _num(room.get("target")) is not None else 21,
            "area": _num(room.get("area")),
            "default_circuit_id": DEFAULT_CIRCUIT_ID,
            "heat_load": heat_load,
            "temperature_sensors": _list(room.get("temperature_sensors")),
            "window_sensors": _list(room.get("window_sensors")),
        })
        for radiator in _list(room.get("radiators")):
            cfg["emitters"].append(_emitter_from_v1(radiator, room_id, DEFAULT_CIRCUIT_ID))
    for zone in _list(v1.get("zones")):
        if isinstance(zone, dict):
            cfg["zones"].append({"id": zone.get("id") if isinstance(zone.get("id"), str) else "",
                                 "name": _text(zone.get("name")),
                                 "room_ids": _list(zone.get("room_ids"))})
    return cfg


def _projected_heat_load(heat_load):
    if heat_load.get("status") == "adopted":
        return heat_load.get("value_w")
    return heat_load.get("legacy_value_w")


def to_v1(cfg):
    """Project schema v2 onto the v0.1.x JSON shape used by the current UI."""
    circuits = cfg.get("circuits") or []
    first = circuits[0] if circuits else empty_circuit()
    building = cfg["building"]
    heating = {
        "manufacturer": building["heat_pump"].get("manufacturer", ""),
        "model": building["heat_pump"].get("model", ""),
        "circuit_name": first.get("name", ""),
        "outdoor": building["entities"].get("outdoor", ""),
        "flow": first["entities"].get("flow", ""),
        "return": first["entities"].get("return", ""),
        "compressor": building["entities"].get("compressor", ""),
        "dhw": building["entities"].get("dhw", ""),
    }
    rooms = []
    for room in cfg["rooms"]:
        rooms.append({
            "id": room["id"], "name": room["name"], "target": room["target"],
            "area": room.get("area"),
            "heat_load": _projected_heat_load(room["heat_load"]),
            "temperature_sensors": list(room["temperature_sensors"]),
            "window_sensors": list(room["window_sensors"]),
            "radiators": [{"id": e["id"], "name": e["name"], "entity": e.get("actuator_entity", ""),
                           "valve_model": e["valve"].get("label", ""),
                           "power_w": e["rated_output"].get("value_w")}
                          for e in cfg["emitters"] if e["room_id"] == room["id"]],
        })
    zones = [{"id": z.get("id", ""), "name": z["name"], "room_ids": list(z["room_ids"])} for z in cfg["zones"]]
    return {"rooms": rooms, "zones": zones, "heating": heating}


def _same_number(a, b):
    a, b = _num(a), _num(b)
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < 1e-9


def _match_emitters(previous, radiators):
    """Pair legacy radiator entries with existing emitters: id first, then (name, entity)."""
    by_id = {e["id"]: e for e in previous}
    used, pairs = set(), []
    for radiator in radiators:
        radiator = radiator if isinstance(radiator, dict) else {}
        match = by_id.get(radiator.get("id")) if isinstance(radiator.get("id"), str) else None
        if match is None:
            candidates = [e for e in previous if e["id"] not in used
                          and e["name"] == _text(radiator.get("name"))
                          and e.get("actuator_entity", "") == _entity(radiator.get("entity"))]
            match = candidates[0] if len(candidates) == 1 else None
        if match is not None and match["id"] in used:
            match = None
        if match is not None:
            used.add(match["id"])
        pairs.append((radiator, match))
    return pairs


def merge_v1(current, v1):
    """Apply a configuration saved by the v0.1.x UI onto v2 without losing v2-only fields.

    Fields the legacy UI cannot see (heat-load documentation, circuit assignment,
    reference temperatures, valve catalogue data …) are preserved for rooms and
    emitters that still exist. Values the legacy UI changed lose their documented
    status, because the legacy UI cannot capture a reference.
    """
    cfg = copy.deepcopy(current)
    fresh = from_v1(v1)
    heating = v1.get("heating") if isinstance(v1, dict) and isinstance(v1.get("heating"), dict) else {}
    cfg["building"]["heat_pump"]["manufacturer"] = fresh["building"]["heat_pump"]["manufacturer"]
    cfg["building"]["heat_pump"]["model"] = fresh["building"]["heat_pump"]["model"]
    cfg["building"]["entities"].update(fresh["building"]["entities"])
    if not cfg["circuits"]:
        cfg["circuits"] = [empty_circuit()]
    first = cfg["circuits"][0]
    first["name"] = _text(heating.get("circuit_name")) or first["name"]
    first["entities"]["flow"] = _entity(heating.get("flow"))
    first["entities"]["return"] = _entity(heating.get("return"))
    default_circuit = first["id"]

    previous_rooms = {r["id"]: r for r in cfg["rooms"] if isinstance(r.get("id"), str)}
    rooms, emitters = [], []
    v1_rooms = [r for r in _list(v1.get("rooms") if isinstance(v1, dict) else None) if isinstance(r, dict)]
    for v1_room in v1_rooms:
        room_id = v1_room.get("id")
        old = previous_rooms.get(room_id) if isinstance(room_id, str) else None
        room = {
            "id": room_id,
            "name": _text(v1_room.get("name")),
            "target": _num(v1_room.get("target")) if _num(v1_room.get("target")) is not None else 21,
            "area": _num(v1_room.get("area")),
            "default_circuit_id": old["default_circuit_id"] if old else default_circuit,
            "heat_load": copy.deepcopy(old["heat_load"]) if old else empty_heat_load(),
            "temperature_sensors": _list(v1_room.get("temperature_sensors")),
            "window_sensors": _list(v1_room.get("window_sensors")),
        }
        if not _same_number(v1_room.get("heat_load"), _projected_heat_load(room["heat_load"])):
            room["heat_load"] = empty_heat_load()
            room["heat_load"]["legacy_value_w"] = _num(v1_room.get("heat_load"))
        rooms.append(room)
        previous = [e for e in cfg["emitters"] if e["room_id"] == room_id]
        for radiator, match in _match_emitters(previous, _list(v1_room.get("radiators"))):
            if match is None:
                emitters.append(_emitter_from_v1(radiator, room_id, room["default_circuit_id"]))
                continue
            emitter = copy.deepcopy(match)
            emitter["name"] = _text(radiator.get("name"))
            emitter["actuator_entity"] = _entity(radiator.get("entity"))
            emitter["valve"]["label"] = _text(radiator.get("valve_model"))
            if not _same_number(radiator.get("power_w"), emitter["rated_output"].get("value_w")):
                emitter["rated_output"] = empty_output(_num(radiator.get("power_w")))
            emitters.append(emitter)
    cfg["rooms"] = rooms
    cfg["emitters"] = emitters
    cfg["zones"] = fresh["zones"]
    return cfg


# ---------------------------------------------------------------- validation

class Invalid(ValueError):
    pass


def _fail(message):
    raise Invalid(message)


def _check_text(value, label, required=False):
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        _fail(label + " ungültig")
    if required and not value.strip():
        _fail(label + " fehlt")


def _check_number(value, low, high, label, optional=True):
    if value is None and optional:
        return None
    if isinstance(value, bool):
        _fail(label + " ungültig")
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        _fail(label + " ungültig")
    if not math.isfinite(n) or not low <= n <= high:
        _fail(label + " ungültig")
    return n


def _check_entity(value, domains, label):
    if value in (None, ""):
        return
    if (not isinstance(value, str) or len(value) > MAX_TEXT or not ENTITY_PATTERN.fullmatch(value)
            or value.split(".", 1)[0] not in domains):
        _fail("Ungültige Entitätszuordnung: " + label)


def _check_enum(value, allowed, label):
    if value is not None and value not in allowed:
        _fail(label + " ungültig")


def _check_id(value, label):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        _fail(label + "-ID ungültig")


def _check_date(value, label):
    if not isinstance(value, str):
        _fail(label + " fehlt")
    try:
        date.fromisoformat(value)
    except ValueError:
        _fail(label + " ungültig (JJJJ-MM-TT)")


def _check_dict(value, label):
    if not isinstance(value, dict):
        _fail(label + " ungültig")
    return value


def _validate_building(building):
    _check_dict(building, "Gebäude")
    _check_text(building.get("name", ""), "Gebäudebezeichnung")
    _check_enum(building.get("heating_system"), HEATING_SYSTEMS, "Heizsystem")
    _check_number(building.get("design_outdoor_c"), -40, 10, "Norm-Außentemperatur")
    _check_text(building.get("design_outdoor_reference", ""), "Quelle Norm-Außentemperatur")
    pump = _check_dict(building.get("heat_pump"), "Wärmepumpe")
    for key in ("manufacturer", "model", "min_flow_reference"):
        _check_text(pump.get(key, ""), "Wärmepumpe " + key)
    _check_number(pump.get("nominal_power_kw"), 0.1, 200, "Nennheizleistung")
    if _check_number(pump.get("min_flow_l_h"), 1, 20000, "Mindestvolumenstrom") is not None:
        _check_text(pump.get("min_flow_reference", ""), "Quelle Mindestvolumenstrom", required=True)
    entities = _check_dict(building.get("entities"), "Gebäude-Entitäten")
    _check_entity(entities.get("outdoor"), {"sensor"}, "Außentemperatur")
    _check_entity(entities.get("compressor"), {"sensor", "binary_sensor"}, "Verdichter")
    _check_entity(entities.get("dhw"), {"sensor", "binary_sensor"}, "Warmwasser")


def _validate_circuit(circuit):
    _check_dict(circuit, "Heizkreis")
    _check_id(circuit.get("id"), "Heizkreis")
    _check_text(circuit.get("name"), "Heizkreisname", required=True)
    _check_enum(circuit.get("mixing"), CIRCUIT_MIXING, "Heizkreistyp")
    _check_enum(circuit.get("emitter_type"), HEATING_SYSTEMS, "Heizflächenart")
    flow = _check_number(circuit.get("design_flow_c"), 20, 90, "Auslegungsvorlauf")
    ret = _check_number(circuit.get("design_return_c"), 15, 85, "Auslegungsrücklauf")
    if (flow is None) != (ret is None):
        _fail("Auslegungsvorlauf und -rücklauf nur gemeinsam angeben")
    if flow is not None and flow <= ret:
        _fail("Auslegungsvorlauf muss über dem Rücklauf liegen")
    curve = _check_dict(circuit.get("heating_curve"), "Heizkurve")
    points = curve.get("points")
    if not isinstance(points, list) or len(points) == 1 or len(points) > 10:
        _fail("Heizkurve: keine oder 2–10 Stützpunkte")
    outdoor = set()
    for point in points:
        _check_dict(point, "Heizkurvenpunkt")
        o = _check_number(point.get("outdoor_c"), -40, 30, "Heizkurve Außentemperatur", optional=False)
        _check_number(point.get("flow_c"), 15, 90, "Heizkurve Vorlauf", optional=False)
        if o in outdoor:
            _fail("Heizkurve: Außentemperatur doppelt")
        outdoor.add(o)
    params = _check_dict(curve.get("manufacturer_params"), "Herstellerparameter Heizkurve")
    if len(params) > 20:
        _fail("Zu viele Herstellerparameter")
    for key, value in params.items():
        _check_text(key, "Heizkurvenparameter", required=True)
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            _fail("Heizkurvenparameter ungültig")
        if isinstance(value, str):
            _check_text(value, "Heizkurvenparameter")
    _check_text(curve.get("note", ""), "Heizkurve Notiz")
    _check_text(curve.get("reference", ""), "Heizkurve Quelle")
    entities = _check_dict(circuit.get("entities"), "Heizkreis-Entitäten")
    for key in ("flow", "flow_target", "return"):
        _check_entity(entities.get(key), {"sensor"}, "Heizkreis " + key)
    _check_entity(entities.get("pump"), {"sensor", "binary_sensor"}, "Heizkreispumpe")


def _validate_heat_load(heat_load):
    _check_dict(heat_load, "Heizlast")
    _check_enum(heat_load.get("status"), HEAT_LOAD_STATUS, "Heizlaststatus")
    if heat_load.get("status") is None:
        _fail("Heizlaststatus fehlt")
    _check_number(heat_load.get("legacy_value_w"), 0.001, 50000, "Alter Heizlastwert")
    for key in ("method", "reference", "author"):
        _check_text(heat_load.get(key, ""), "Heizlast " + key)
    if heat_load["status"] == "adopted":
        _check_number(heat_load.get("value_w"), 1, 50000, "Raumheizlast", optional=False)
        _check_text(heat_load.get("method", ""), "Heizlast-Verfahren", required=True)
        _check_text(heat_load.get("reference", ""), "Heizlast-Quelle", required=True)
        _check_date(heat_load.get("date"), "Heizlast-Datum")
    elif heat_load.get("value_w") is not None:
        _fail("Heizlastwert nur mit dokumentierter Übernahme")


def _validate_room(room, circuit_ids):
    _check_dict(room, "Raum")
    _check_id(room.get("id"), "Raum")
    _check_text(room.get("name"), "Raumname", required=True)
    _check_number(room.get("target"), 5, 35, "Raum-Solltemperatur", optional=False)
    _check_number(room.get("area"), 0.001, 1000, "Raumfläche")
    if room.get("default_circuit_id") is not None and room["default_circuit_id"] not in circuit_ids:
        _fail("Raum verweist auf unbekannten Heizkreis")
    _validate_heat_load(room.get("heat_load"))
    for key, domain in (("temperature_sensors", "sensor"), ("window_sensors", "binary_sensor")):
        values = room.get(key)
        if not isinstance(values, list) or len(values) > 30:
            _fail("Entitätsliste ungültig")
        for value in values:
            if value in (None, ""):
                _fail("Leere Entitätszuordnung")
            _check_entity(value, {domain}, key)
        if len(set(values)) != len(values):
            _fail("Entität doppelt zugeordnet")
    if not room["temperature_sensors"]:
        _fail("Mindestens ein Temperatursensor pro Raum")


def _validate_emitter(emitter, room_ids, circuit_ids):
    _check_dict(emitter, "Heizfläche")
    _check_id(emitter.get("id"), "Heizfläche")
    if emitter.get("room_id") not in room_ids:
        _fail("Heizfläche verweist auf unbekannten Raum")
    if emitter.get("circuit_id") not in circuit_ids:
        _fail("Heizfläche verweist auf unbekannten Heizkreis")
    _check_text(emitter.get("name"), "Heizflächenbezeichnung", required=True)
    _check_enum(emitter.get("type"), EMITTER_TYPES, "Heizflächentyp")
    _check_text(emitter.get("construction", ""), "Bauart")
    _check_number(emitter.get("height_mm"), 1, 5000, "Bauhöhe")
    _check_number(emitter.get("length_mm"), 1, 20000, "Baulänge")
    output = _check_dict(emitter.get("rated_output"), "Wärmeleistung")
    _check_number(output.get("value_w"), 0.001, 50000, "Heizkörperleistung")
    temps = [_check_number(output.get(k), low, high, "Referenztemperatur")
             for k, low, high in (("flow_c", 20, 95), ("return_c", 15, 90), ("room_c", 5, 30))]
    if any(t is None for t in temps) and any(t is not None for t in temps):
        _fail("Referenztemperaturen der Heizkörperleistung nur vollständig angeben")
    if temps[0] is not None and not temps[0] > temps[1] > temps[2]:
        _fail("Referenztemperaturen: Vorlauf > Rücklauf > Raum")
    _check_enum(output.get("source"), OUTPUT_SOURCES, "Quelle Heizkörperleistung")
    _check_text(output.get("reference", ""), "Quelle Heizkörperleistung")
    _check_number(emitter.get("exponent"), 1.0, 2.0, "Heizkörperexponent")
    _check_number(emitter.get("load_share_percent"), 0.001, 100, "Anteil an Raumheizlast")
    valve = _check_dict(emitter.get("valve"), "Ventil")
    for key in ("label", "manufacturer", "series", "variant", "dimension", "current_setting"):
        _check_text(valve.get(key, ""), "Ventil " + key)
    _check_entity(emitter.get("actuator_entity"), {"number", "climate"}, "Stellantrieb")


def validate_structure(cfg):
    """Full validation except the per-room load-share completeness rule (for unsaved editor states)."""
    return validate(cfg, check_shares=False)


def validate(cfg, check_shares=True):
    """Strict validation of a complete schema-v2 configuration. Returns cfg or raises Invalid."""
    if not isinstance(cfg, dict) or cfg.get("schema_version") != SCHEMA_VERSION:
        _fail("Konfigurationsformat ungültig (Schemaversion 2 erwartet)")
    for key in ("circuits", "rooms", "zones", "emitters"):
        if not isinstance(cfg.get(key), list):
            _fail("Konfigurationsformat ungültig")
    if (len(cfg["rooms"]) > MAX_ROOMS or len(cfg["zones"]) > MAX_ROOMS or len(cfg["emitters"]) > MAX_EMITTERS
            or len(cfg["circuits"]) > MAX_CIRCUITS or len(json.dumps(cfg)) > 400000):
        _fail("Konfiguration zu groß")
    _validate_building(cfg.get("building"))
    circuit_ids = set()
    for circuit in cfg["circuits"]:
        _validate_circuit(circuit)
        if circuit["id"] in circuit_ids:
            _fail("Heizkreis-ID doppelt")
        circuit_ids.add(circuit["id"])
    room_ids = set()
    for room in cfg["rooms"]:
        _validate_room(room, circuit_ids)
        if room["id"] in room_ids:
            _fail("Raum-ID doppelt")
        room_ids.add(room["id"])
    emitter_ids = set()
    for emitter in cfg["emitters"]:
        _validate_emitter(emitter, room_ids, circuit_ids)
        if emitter["id"] in emitter_ids:
            _fail("Heizflächen-ID doppelt")
        emitter_ids.add(emitter["id"])
    for room_id in (room_ids if check_shares else ()):
        shares = [e.get("load_share_percent") for e in cfg["emitters"] if e["room_id"] == room_id]
        given = [s for s in shares if s is not None]
        if given and (len(given) != len(shares) or abs(sum(float(s) for s in given) - 100) > 0.5):
            _fail("Heizlastanteile eines Raums: für alle Heizflächen angeben, Summe 100 %")
    used = set()
    for zone in cfg["zones"]:
        _check_dict(zone, "Zone")
        _check_text(zone.get("id", ""), "Zonen-ID")
        _check_text(zone.get("name"), "Zonenname", required=True)
        if not isinstance(zone.get("room_ids"), list) or len(zone["room_ids"]) < 2:
            _fail("Zone benötigt Name und mindestens zwei Räume")
        for room_id in zone["room_ids"]:
            if not isinstance(room_id, str) or room_id not in room_ids or room_id in used:
                _fail("Unbekannter oder mehrfach zonierter Raum")
            used.add(room_id)
    return cfg


# ---------------------------------------------------------- derived entity sets

def entities_of(cfg):
    """All HA entities a v2 configuration reads (never writes)."""
    ids = set()
    for room in cfg["rooms"]:
        ids.update(room.get("temperature_sensors", []))
        ids.update(room.get("window_sensors", []))
    ids.update(e.get("actuator_entity") for e in cfg["emitters"])
    ids.update(cfg["building"]["entities"].values())
    for circuit in cfg["circuits"]:
        ids.update(circuit["entities"].values())
    return {i for i in ids if isinstance(i, str) and i}


def temperature_entities_of(cfg):
    ids = {v for room in cfg["rooms"] for v in room.get("temperature_sensors", [])}
    ids.add(cfg["building"]["entities"].get("outdoor"))
    for circuit in cfg["circuits"]:
        ids.update(circuit["entities"].get(k) for k in ("flow", "flow_target", "return"))
    return {i for i in ids if isinstance(i, str) and i}


def calculation_blockers(cfg):
    """Rooms that cannot receive a flow/valve recommendation yet, with German reasons.

    Informational only; no calculation is implemented in AB-008.
    """
    blockers = {}
    for room in cfg["rooms"]:
        reasons = []
        if room["heat_load"].get("status") != "adopted":
            reasons.append("Heizlast fehlt")
        emitters = [e for e in cfg["emitters"] if e["room_id"] == room["id"]]
        if not emitters:
            reasons.append("Keine Heizfläche erfasst")
        if any(e["rated_output"].get("flow_c") is None or e["rated_output"].get("value_w") is None for e in emitters):
            reasons.append("Heizkörperleistung ohne Referenztemperaturen")
        if len(emitters) > 1 and any(e.get("load_share_percent") is None for e in emitters):
            reasons.append("Heizlastanteile der Heizflächen fehlen")
        circuits = {c["id"]: c for c in cfg["circuits"]}
        if any(circuits.get(e["circuit_id"], {}).get("design_flow_c") is None for e in emitters):
            reasons.append("Auslegungstemperaturen des Heizkreises fehlen")
        if reasons:
            blockers[room["id"]] = reasons
    return blockers


# ------------------------------------------------------------ AB-009 helpers

# Only manufacturer/series without variants, flow ranges or characteristic curves
# (decision Bastian 2026-11-21: no unverified product data in the catalogue).
VALVE_CATALOG = [
    {"key": "imi-heimeier-eclipse", "manufacturer": "IMI Heimeier", "series": "Eclipse"},
]


def _blocker(code, text, **target):
    return {"code": code, "text": text, **target}


def readiness(cfg):
    """Data completeness per room and for the building – informational only.

    No hydraulic calculation exists yet; an empty blocker list means
    "Auslegungsdaten vollständig", never "Empfehlung verfügbar".
    """
    circuits = {c["id"]: c for c in cfg["circuits"]}
    building = []
    if cfg["building"].get("heating_system") is None:
        building.append(_blocker("heating_system_missing", "Heizsystem der Anlage fehlt", section="building"))
    if cfg["building"].get("design_outdoor_c") is None:
        building.append(_blocker("design_outdoor_missing", "Norm-Außentemperatur fehlt", section="building"))
    rooms = {}
    for room in cfg["rooms"]:
        blockers = []
        if room["heat_load"].get("status") != "adopted":
            blockers.append(_blocker("heat_load_missing", "Heizlast fehlt", room_id=room["id"], section="heat_load"))
        emitters = [e for e in cfg["emitters"] if e["room_id"] == room["id"]]
        if not emitters:
            blockers.append(_blocker("no_emitter", "Keine Heizfläche erfasst", room_id=room["id"], section="emitters"))
        for e in emitters:
            if e.get("type") == "underfloor":
                blockers.append(_blocker("underfloor_not_supported", f"{e['name']}: FBH-Berechnung noch nicht verfügbar",
                                         room_id=room["id"], emitter_id=e["id"], section="emitter"))
            elif e["rated_output"].get("value_w") is None or e["rated_output"].get("flow_c") is None:
                blockers.append(_blocker("output_reference_missing", f"{e['name']}: Heizkörperleistung ohne Referenztemperaturen",
                                         room_id=room["id"], emitter_id=e["id"], section="emitter"))
        if len(emitters) > 1 and any(e.get("load_share_percent") is None for e in emitters):
            blockers.append(_blocker("shares_missing", "Heizlastanteile der Heizflächen fehlen", room_id=room["id"], section="emitters"))
        for circuit_id in sorted({e["circuit_id"] for e in emitters}):
            circuit = circuits.get(circuit_id, {})
            if circuit.get("design_flow_c") is None:
                blockers.append(_blocker("circuit_design_missing", f"Auslegungstemperaturen fehlen: {circuit.get('name', circuit_id)}",
                                         circuit_id=circuit_id, section="circuit"))
        rooms[room["id"]] = {"measurement_ready": bool(room.get("temperature_sensors")), "blockers": blockers}
    return {"building": building, "rooms": rooms,
            "note": "Reine Datenvollständigkeit. Eine hydraulische Berechnung ist noch nicht implementiert."}


def _lmtd(flow, ret, room):
    if not flow > ret > room:
        return None
    return (flow - ret) / math.log((flow - room) / (ret - room))


def suggest_load_shares(cfg, room_id):
    """Proportional load-share proposal for the emitters of one room.

    Outputs are converted to the circuit design temperatures (EN 442 relation
    Q = Qn * (ΔT_design / ΔT_ref)^n) so they are comparable. Returns
    (shares, None) or (None, reason). Nothing is saved; the user confirms.
    """
    room = next((r for r in cfg["rooms"] if r["id"] == room_id), None)
    if room is None:
        return None, "Raum nicht gefunden"
    emitters = [e for e in cfg["emitters"] if e["room_id"] == room_id]
    if not emitters:
        return None, "Keine Heizfläche erfasst"
    if len(emitters) == 1:
        return {emitters[0]["id"]: 100.0}, None
    circuits = {c["id"]: c for c in cfg["circuits"]}
    design_room = room.get("target")
    outputs = []
    for e in emitters:
        if e.get("type") == "underfloor":
            return None, f"{e['name']}: FBH wird nicht mit dem Heizkörperverfahren verglichen"
        out = e["rated_output"]
        if out.get("value_w") is None or out.get("flow_c") is None:
            return None, f"{e['name']}: Leistung mit vollständigen Referenztemperaturen fehlt"
        if e.get("exponent") is None:
            return None, f"{e['name']}: Heizkörperexponent fehlt"
        circuit = circuits.get(e["circuit_id"], {})
        if circuit.get("design_flow_c") is None:
            return None, f"{circuit.get('name', e['circuit_id'])}: Auslegungstemperaturen fehlen"
        ref = _lmtd(float(out["flow_c"]), float(out["return_c"]), float(out["room_c"]))
        design = _lmtd(float(circuit["design_flow_c"]), float(circuit["design_return_c"]), float(design_room))
        if ref is None or design is None:
            return None, f"{e['name']}: Auslegungsrücklauf muss über der Raumtemperatur liegen"
        outputs.append((e["id"], float(out["value_w"]) * (design / ref) ** float(e["exponent"])))
    total = sum(q for _, q in outputs)
    shares, assigned = {}, 0.0
    for i, (emitter_id, q) in enumerate(outputs):
        if i == len(outputs) - 1:
            shares[emitter_id] = round(100.0 - assigned, 1)
        else:
            shares[emitter_id] = round(100.0 * q / total, 1)
            assigned += shares[emitter_id]
    return shares, None
