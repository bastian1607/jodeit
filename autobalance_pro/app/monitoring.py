"""AB-013: room monitoring status, coverage and reference-room hints. Pure functions, no I/O.

Design (ChatGPT UX handover AB-013, approved by Bastian 2026-11-27):
  * A room may have 0..n temperature sensors. Without a sensor it is "nicht überwacht":
    it keeps its comfort target, but it never gets an actual temperature, a deviation or a
    comfort verdict.
  * Several sensors in one room are not blindly averaged: if they differ by more than
    DISAGREE_K, the room shows the single values and "Messposition prüfen" instead of a mean.
  * The "critical room" is chosen only from rooms with a valid current measurement.
  * Reference-room hints are reasons derived from documented data only. They never preselect
    rooms and never invent a risk score.
"""
import model

# Proposal (Claude, 2026-11-27): above this spread two sensors in one room are treated as a
# placement problem. Pilot value, to be confirmed in the field.
DISAGREE_K = 1.0

STATUS_TEXT = {
    "monitored": "Überwacht",
    "not_monitored": "Nicht überwacht",
    "waiting": "Fühler zugeordnet · wartet auf erste Messung",
    "sensor_unavailable": "Sensor vorübergehend nicht verfügbar",
    "stale": "Messwert veraltet",
    "check_value": "Messwert prüfen",
    "disagree": "Fühler weichen voneinander ab – Messposition prüfen",
}


def _sensor(entity_id, observation):
    if not observation:
        return {"entity_id": entity_id, "value": None, "quality": "no_data", "valid": False}
    value = observation.get("normalized_value")
    return {"entity_id": entity_id, "value": value, "quality": observation.get("quality") or "unchecked",
            "valid": bool(observation.get("valid") and value is not None), "stale": bool(observation.get("stale"))}


def room_status(room, observations):
    """Status of one room from the latest observations ({entity_id: observation})."""
    sensors = [_sensor(e, observations.get(e)) for e in room.get("temperature_sensors", [])]
    windows = [observations.get(e) for e in room.get("window_sensors", [])]
    result = {"id": room["id"], "name": room["name"], "target": room.get("target"),
              "design_indoor_c": room.get("design_indoor_c"), "sensors": sensors,
              "window_open": any(o and o.get("state") == "on" for o in windows),
              "window_contacts": len(room.get("window_sensors", [])),
              "temperature": None, "deviation_k": None, "spread_k": None}
    valid = [s["value"] for s in sensors if s["valid"]]
    if not sensors:
        status = "not_monitored"
    elif all(s["quality"] == "no_data" for s in sensors):
        status = "waiting"
    elif not valid:
        qualities = {s["quality"] for s in sensors}
        if qualities & {"unavailable", "unknown", "no_data"}:
            status = "sensor_unavailable"
        elif all(s.get("stale") or s["quality"] == "stale" for s in sensors):
            status = "stale"
        else:
            status = "check_value"
    else:
        spread = max(valid) - min(valid)
        result["spread_k"] = round(spread, 2)
        if spread > DISAGREE_K:
            status = "disagree"
        else:
            status = "monitored"
            result["temperature"] = round(sum(valid) / len(valid), 2)
            if room.get("target") is not None:
                result["deviation_k"] = round(result["temperature"] - float(room["target"]), 2)
    result["status"] = status
    result["status_text"] = STATUS_TEXT[status]
    result["monitored"] = status == "monitored"
    return result


def zone_groups(cfg):
    """Thermal zones: shared zones plus every unzoned room as its own closed zone."""
    groups, used = [], set()
    for zone in cfg.get("zones", []):
        groups.append({"name": zone["name"], "room_ids": list(zone["room_ids"]), "shared": True})
        used.update(zone["room_ids"])
    for room in cfg.get("rooms", []):
        if room["id"] not in used:
            groups.append({"name": room["name"], "room_ids": [room["id"]], "shared": False})
    return groups


def coverage_summary(cfg, statuses):
    """Counts for "3 von 8 Räumen überwacht · 2 von 3 Zonen mit Messpunkt". Not a comfort verdict."""
    by_id = {s["id"]: s for s in statuses}
    has_sensor = {r["id"]: bool(r.get("temperature_sensors")) for r in cfg.get("rooms", [])}
    zones = zone_groups(cfg)
    measured, partial = 0, []
    for zone in zones:
        with_sensor = [rid for rid in zone["room_ids"] if has_sensor.get(rid)]
        if with_sensor:
            measured += 1
            if len(with_sensor) < len(zone["room_ids"]):
                partial.append(zone["name"])
    rooms = cfg.get("rooms", [])
    return {
        "rooms": len(rooms),
        "rooms_with_sensor": sum(1 for r in rooms if has_sensor[r["id"]]),
        "rooms_monitored_now": sum(1 for r in rooms if by_id.get(r["id"], {}).get("monitored")),
        "rooms_without_sensor": [r["name"] for r in rooms if not has_sensor[r["id"]]],
        "zones": len(zones), "zones_with_sensor": measured, "zones_partial": partial,
    }


def critical_room(statuses):
    """Monitored room with the largest shortfall against its comfort target (valid data only)."""
    candidates = [s for s in statuses if s["monitored"] and s["deviation_k"] is not None]
    if not candidates:
        return None
    worst = min(candidates, key=lambda s: s["deviation_k"])
    return {"id": worst["id"], "name": worst["name"], "deviation_k": worst["deviation_k"]}


def _emitter_output_at_design(cfg, room):
    """Sum of emitter outputs converted to the circuit design point, or None if not comparable."""
    emitters = [e for e in cfg.get("emitters", []) if e["room_id"] == room["id"]]
    circuits = {c["id"]: c for c in cfg.get("circuits", [])}
    if not emitters or room.get("design_indoor_c") is None:
        return None
    total = 0.0
    for e in emitters:
        out, circuit = e["rated_output"], circuits.get(e["circuit_id"], {})
        if (e.get("type") == "underfloor" or out.get("value_w") is None or out.get("flow_c") is None
                or e.get("exponent") is None or circuit.get("design_flow_c") is None):
            return None
        ref = model._lmtd(float(out["flow_c"]), float(out["return_c"]), float(out["room_c"]))
        design = model._lmtd(float(circuit["design_flow_c"]), float(circuit["design_return_c"]),
                             float(room["design_indoor_c"]))
        if ref is None or design is None:
            return None
        total += float(out["value_w"]) * (design / ref) ** float(e["exponent"])
    return total


def reference_hints(cfg):
    """{room_id: [reason, …]} – reasons to consider a room for one of the ~3 measuring points.

    Only reasons that follow from documented data. No room is selected automatically.
    """
    rooms = cfg.get("rooms", [])
    hints = {r["id"]: [] for r in rooms}
    for room in rooms:
        heat_load = room["heat_load"]
        if heat_load.get("status") == "adopted":
            output = _emitter_output_at_design(cfg, room)
            if output is not None and output < float(heat_load["value_w"]):
                hints[room["id"]].append(
                    f"Heizfläche am Auslegungspunkt ca. {round(output)} W unter dokumentierter Heizlast "
                    f"{round(float(heat_load['value_w']))} W – vor der Heizkurvenoptimierung prüfen")
    per_area = [(float(r["heat_load"]["value_w"]) / float(r["area"]), r) for r in rooms
                if r["heat_load"].get("status") == "adopted" and r.get("area")]
    if len(per_area) >= 2:
        value, room = max(per_area, key=lambda x: x[0])
        hints[room["id"]].append(f"Höchste dokumentierte Heizlast je m² ({round(value)} W/m²)")
    targets = sorted(float(r["target"]) for r in rooms if r.get("target") is not None)
    if len(targets) >= 2:
        median = targets[len(targets) // 2] if len(targets) % 2 else (targets[len(targets) // 2 - 1] + targets[len(targets) // 2]) / 2
        for room in rooms:
            if room.get("target") is not None and float(room["target"]) - median >= 1:
                hints[room["id"]].append(f"Höheres Komfortziel ({str(room['target']).replace('.', ',')} °C)")
    return hints


REFERENCE_HELP = ("Kritische Räume anhand Heizlast, vorhandener Heizkörperleistung, Geschoss, Himmelsrichtung "
                  "und bekannter Komfortprobleme prüfen. Nicht nur sonnige oder benachbarte Räume wählen.")
