"""AB-014: is a measuring job meaningful for a heating-curve assessment? Pure functions, no I/O.

Rule set: ChatGPT AB-014 "Sieben-Tage-Messkriterien" (2026-11-27), released for implementation by
Bastian on 2026-11-27. All thresholds below are PILOT VALUES to be validated on real plants; they are
neither standard nor manufacturer values and are kept in one place (THRESHOLDS).

What this module does: classify a measuring job into
    "no_data"          – nothing measured
    "insufficient"     – "Messung unzureichend", no heating-curve statement
    "level"            – level/offset assessable within the observed outdoor band, no slope
    "level_and_slope"  – level and slope assessable in two outdoor bands (technician check only)
and explain why. What it never does: compute a parameter change, write anything, or claim
comfort for rooms without valid measurement.

Time model: the app polls every 5 minutes; every stored poll is a "tick". Ticks are mapped to
5-minute slots. A slot is
    missing    – no tick
    disturbed  – window open (+90 min), DHW/disinfection (+60 min), defrost (+60 min),
                 heating not active, parameter/target change (+120 min)
    invalid    – tick present but outdoor, flow or every monitored room value missing/invalid
    ok         – usable for a quasi-stationary comparison
A night (22:00–06:00 local time) is valid (AB-034, pilot v2) if its undisturbed blocks (≥ 30 min, ≥ 80 % ok,
internal gaps ≤ 10 min, inside one evaluation segment) contain either one block of ≥ 4 h or ≥ 4 h of ok slots
in total with at least two blocks of ≥ 60 min. Defrost: the active phase plus 30 min if the flow temperature
has recovered, otherwise 60 min (the night is then marked "nicht stationär nach Abtauung").
"""
import bisect
import statistics
from datetime import datetime, time, timedelta, timezone

THRESHOLDS = {
    "slot_minutes": 5,
    "night_start_hour": 22, "night_end_hour": 6,
    "technical_coverage": 0.85, "long_outage_hours": 6,
    "nights_level": 3, "nights_slope": 4, "nights_per_group": 2, "slope_band_k": 8.0,
    "window_after_min": 90, "dhw_after_min": 60, "change_after_min": 120,
    # AB-034 (ChatGPT Auftrag 20, confirmed by Bastian 28.11.2026): defrost after-run 30 min if the flow
    # temperature is back within ±2 K of its pre-defrost median (15 min) in two consecutive polls, else 60 min.
    "defrost_after_min": 30, "defrost_after_max_min": 60, "defrost_recovery_k": 2.0, "defrost_pre_min": 15,
    # AB-034 night rule: blocks of undisturbed slots instead of one contiguous 4-hour window.
    "night_min_ok_hours": 4.0, "block_min_minutes": 30, "block_long_minutes": 60, "blocks_long_needed": 2,
    "block_max_gap_minutes": 10, "block_ok_share": 0.8,
    "trend_k_per_h": 0.15, "disagree_k": 1.0,
}
SLOT = timedelta(minutes=THRESHOLDS["slot_minutes"])
RULE_VERSION = "AB-014 Pilot v2 (AB-034, 28.11.2026)"

RESTRICTED_LABEL = "Vorläufig · fachlich eingeschränkt"
STAGE_TEXT = {
    "no_data": "Keine Messdaten in dieser Messung",
    "insufficient": "Messung unzureichend – keine Heizkurvenempfehlung",
    "unverified": "Heizbetrieb nicht nachgewiesen. Temperaturdaten vorhanden, aber keine belastbare Heizkurvenbewertung.",
    "level": "Niveau im Messbereich prüfbar – Steigung nicht beurteilbar",
    "level_and_slope": "Niveau und Steigung in den Messbereichen prüfbar (nur Technikerprüfung)",
}

DHW_WORDS = ("hot water", "warmwasser", "dhw")
HEATING_WORDS = ("heating", "heizen", "heizbetrieb")
UNKNOWN_STATES = ("", "unknown", "unavailable", "none")
# Share of usable 5-min slots in a night's evaluation window that must carry a valid status signal
# (pilot value, ChatGPT review Auftrag 7: "frische, valide Statusmessungen über den Nachtabschnitt").
STATUS_SHARE = 0.8
DEFROST_WORDS = ("defrost", "abtau")


def _median(values):
    return round(statistics.median(values), 2) if values else None


def _config_at(history_times, history, t):
    i = bisect.bisect_right(history_times, t) - 1
    return history[max(i, 0)][1] if history else {"rooms": []}


def _num(cell):
    return cell["v"] if cell and cell.get("ok") and cell.get("v") is not None else None


def _state(cell):
    return str(cell.get("s", "")).strip().lower() if cell else ""


def parameter_changes(ticks, values, params):
    """AB-033: a parameter changes only if a valid value differs from its last valid value.

    "unavailable"/"unknown" (e.g. a short EMS-ESP dropout) is a gap, not a change; the value after the gap is
    compared with the last valid value before it."""
    last, out = {}, []
    for t in ticks:
        row = values.get(t, {})
        diff = []
        for p in params:
            s = _state(row.get(p))
            if s in UNKNOWN_STATES:
                continue
            if p in last and last[p] != s:
                diff.append(p)
            last[p] = s
        if diff:
            out.append({"at": t, "kind": "parameter", "detail": diff})
    return out


def _intervals_after(times, minutes):
    """Disturbance from an observed tick until one poll later (state may last that long) + after-run."""
    return [(t, t + SLOT + timedelta(minutes=minutes)) for t in times]


def _defrost_intervals(defrost_ticks, ticks, values, flow_role, th):
    """AB-034: (start, end, recovered) per defrost episode. Consecutive defrost polls form one episode.

    The poll before the episode is excluded as uncertain transition. After the episode (+ one slot) the phase
    lasts at least ``defrost_after_min``; it ends then only if the flow temperature was back within
    ``defrost_recovery_k`` of the median of the ``defrost_pre_min`` before the episode in two consecutive polls,
    otherwise it lasts ``defrost_after_max_min`` and the episode counts as not recovered."""
    marked = set(defrost_ticks)
    episodes, cur = [], []
    for t in ticks:
        if t in marked:
            cur.append(t)
        elif cur:
            episodes.append(cur)
            cur = []
    if cur:
        episodes.append(cur)
    flow = lambda t: _num(values.get(t, {}).get(flow_role)) if flow_role else None
    out = []
    for ep in episodes:
        begin, end = ep[0], ep[-1] + SLOT
        min_end = end + timedelta(minutes=th["defrost_after_min"])
        max_end = end + timedelta(minutes=th["defrost_after_max_min"])
        pre = [flow(t) for t in ticks if begin - timedelta(minutes=th["defrost_pre_min"]) <= t < begin - SLOT]
        ref = _median([v for v in pre if v is not None])
        until, recovered = max_end, False
        if ref is not None:
            after = [t for t in ticks if end <= t < max_end]
            for t1, t2 in zip(after, after[1:]):
                v1, v2 = flow(t1), flow(t2)
                if t2 - t1 <= 2 * SLOT and v1 is not None and v2 is not None \
                        and abs(v1 - ref) <= th["defrost_recovery_k"] and abs(v2 - ref) <= th["defrost_recovery_k"]:
                    until, recovered = max(min_end, t2), True
                    break
        out.append((begin - SLOT, until, recovered))
    return out


def _night_blocks(ns, th):
    """AB-034: undisturbed blocks of a night. ns: [(slot_start, tick|None, state)].

    A block is split by a disturbed slot or by more than ``block_max_gap_minutes`` of missing/invalid slots;
    it starts and ends with an ok slot. Returns [(start, end, ok_ticks, valid)]."""
    max_gap = th["block_max_gap_minutes"] // th["slot_minutes"]
    blocks, cur, pending = [], [], []

    def close():
        if cur:
            length = cur[-1][0] + SLOT - cur[0][0]
            ok = [x[1] for x in cur if x[2] == "ok"]
            valid = (length >= timedelta(minutes=th["block_min_minutes"])
                     and len(ok) >= th["block_ok_share"] * len(cur))
            blocks.append((cur[0][0], cur[-1][0] + SLOT, ok, valid))
        cur.clear()
        pending.clear()
    for slot in ns:
        if slot[2] == "disturbed":
            close()
        elif slot[2] == "ok":
            cur.extend(pending)
            pending.clear()
            cur.append(slot)
        elif cur:
            pending.append(slot)
            if len(pending) > max_gap:
                close()
    close()
    return blocks


def _night_qualifies(blocks, th):
    """One block of ≥ 4 h, or ≥ 4 h of ok slots with at least two blocks of ≥ 60 min (AC20-01…03)."""
    need = timedelta(hours=th["night_min_ok_hours"])
    if any(b - a >= need for a, b, _, valid in blocks if valid):
        return True
    ok_time = sum(len(ok) for _, _, ok, valid in blocks if valid) * SLOT
    long_blocks = sum(1 for a, b, _, valid in blocks if valid and b - a >= timedelta(minutes=th["block_long_minutes"]))
    return ok_time >= need and long_blocks >= th["blocks_long_needed"]


def _in_any(intervals, t):
    return any(a <= t < b for a, b in intervals)


def _slope(points):
    """Least-squares slope in K/h for [(datetime, value)]; None for < 2 points."""
    if len(points) < 2:
        return None
    t0 = points[0][0]
    xs = [(t - t0).total_seconds() / 3600 for t, _ in points]
    ys = [v for _, v in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return None if den == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def _rooms_at(cfg):
    return [r for r in cfg.get("rooms", [])]


def assess(start, end, ticks, values, history, roles, tz, planned_days=7, now=None):
    """Assess one measuring job.

    start/end: aware datetimes of the job window. ticks: ascending aware datetimes.
    values: {tick: {entity_id: {"v": float|None, "s": str, "ok": bool}}}.
    history: [(aware datetime, cfg)] ascending; the first entry is the configuration at ``start``.
    roles: entity ids for outdoor_damped, outdoor, flow, flow_target, return, heating_active,
           dhw, hp_activity and "params" (list of curve/set-point entities whose change starts a
           new evaluation segment).
    tz: tzinfo for the night definition (local time of the plant).
    """
    now = now or end
    th = THRESHOLDS
    ticks = [t for t in ticks if start <= t <= end]
    history_times = [h[0] for h in history]
    out = {"window": {"start": start.isoformat(), "end": end.isoformat(),
                      "hours": round((end - start).total_seconds() / 3600, 1), "planned_days": planned_days,
                      "complete": (end - start) >= timedelta(days=planned_days)},
           "thresholds": dict(th), "pilot_values": True, "read_only": True}
    missing_basis = []
    outdoor_role = roles.get("outdoor_damped") or roles.get("outdoor")
    if not roles.get("outdoor_damped"):
        missing_basis.append("Regelungsrelevante (gedämpfte) Außentemperatur nicht verfügbar"
                             + (" – ungedämpfte Außentemperatur verwendet" if roles.get("outdoor") else ""))
    if not outdoor_role:
        missing_basis.append("Keine Außentemperatur zugeordnet")
    flow_roles = [r for r in (roles.get("flow"), roles.get("flow_target")) if r]
    if not flow_roles:
        missing_basis.append("Keine Vorlauftemperatur zugeordnet")
    if not roles.get("heating_active") and not roles.get("hp_activity"):
        missing_basis.append("Heizbetrieb nicht erkennbar (kein Betriebsstatus)")
    if not roles.get("dhw") and not roles.get("hp_activity"):
        missing_basis.append("Warmwasserbereitung nicht erkennbar – Störungsfreiheit nicht belegt")
    if not roles.get("hp_activity"):
        missing_basis.append("Abtauung nicht erkennbar – Störungsfreiheit nicht belegt")

    # ---- events: DHW, defrost, parameter and target changes
    dhw_ticks, defrost_ticks, heating_off, changes = [], [], set(), []
    changes.extend(parameter_changes(ticks, values, roles.get("params", [])))
    in_dhw = in_defrost = False
    for t in ticks:
        row = values.get(t, {})
        activity = _state(row.get(roles["hp_activity"])) if roles.get("hp_activity") else ""
        activity_known = bool(roles.get("hp_activity")) and activity not in UNKNOWN_STATES
        dhw_state = _state(row.get(roles["dhw"])) if roles.get("dhw") else ""
        # AB-034: a DHW/defrost phase lasts through status gaps until a valid status shows it has ended
        if dhw_state == "on" or any(w in activity for w in DHW_WORDS):
            in_dhw = True
        elif dhw_state == "off" or activity_known:
            in_dhw = False
        if in_dhw:
            dhw_ticks.append(t)
        if any(w in activity for w in DEFROST_WORDS):
            in_defrost = True
        elif activity_known:
            in_defrost = False
        if in_defrost:
            defrost_ticks.append(t)
        if roles.get("heating_active") and _state(row.get(roles["heating_active"])) == "off":
            heating_off.add(t)
    prev_targets = None
    for at, cfg in history:
        targets = {r["id"]: (r.get("name"), r.get("target")) for r in cfg.get("rooms", [])}
        if prev_targets is not None and at > start:
            for rid, (name, target) in targets.items():
                if rid in prev_targets and prev_targets[rid][1] != target:
                    changes.append({"at": at, "kind": "target", "detail": [f"{name}: {prev_targets[rid][1]} → {target} °C"]})
        prev_targets = targets
    changes.sort(key=lambda c: c["at"])
    dhw_iv = _intervals_after(dhw_ticks, th["dhw_after_min"])
    defrost_ep = _defrost_intervals(defrost_ticks, ticks, values, roles.get("flow"), th)
    defrost_iv = [(a, b) for a, b, _ in defrost_ep]
    change_iv = _intervals_after([c["at"] for c in changes], th["change_after_min"])

    # window contacts per room: open intervals incl. 90 min after closing
    window_open = {}
    for t in ticks:
        cfg = _config_at(history_times, history, t)
        for r in cfg.get("rooms", []):
            if any(_state(values.get(t, {}).get(w)) == "on" for w in r.get("window_sensors", [])):
                window_open.setdefault(r["id"], []).append(t)
    window_iv = {rid: _intervals_after(ts, th["window_after_min"]) for rid, ts in window_open.items()}

    # ---- classify ticks
    tick_info = {}
    for t in ticks:
        row = values.get(t, {})
        cfg = _config_at(history_times, history, t)
        reasons = []
        if _in_any(dhw_iv, t):
            reasons.append("dhw")
        if _in_any(defrost_iv, t):
            reasons.append("defrost")
        if t in heating_off:
            reasons.append("heating_off")
        if _in_any(change_iv, t):
            reasons.append("change")
        outdoor = _num(row.get(outdoor_role)) if outdoor_role else None
        outdoor_raw = _num(row.get(roles["outdoor"])) if roles.get("outdoor") else None
        flow = next((_num(row.get(f)) for f in flow_roles if _num(row.get(f)) is not None), None)
        # AC6-02: heating must be confirmed by a valid status value, an assigned entity is not enough.
        heat_state = _state(row.get(roles["heating_active"])) if roles.get("heating_active") else ""
        activity = _state(row.get(roles["hp_activity"])) if roles.get("hp_activity") else ""
        activity_known = activity not in UNKNOWN_STATES
        heating_confirmed = heat_state == "on" or (heat_state in UNKNOWN_STATES and any(w in activity for w in HEATING_WORDS))
        dhw_state = _state(row.get(roles["dhw"])) if roles.get("dhw") else ""
        dhw_known = dhw_state in ("on", "off") or activity_known
        disturbances_known = dhw_known and activity_known      # defrost is only visible via the activity
        rooms = {}
        for r in cfg.get("rooms", []):
            sensors = r.get("temperature_sensors", [])
            if not sensors:
                continue
            vals = [_num(row.get(s)) for s in sensors]
            vals = [v for v in vals if v is not None]
            ok = bool(vals) and (max(vals) - min(vals) <= th["disagree_k"])
            disturbed = _in_any(window_iv.get(r["id"], []), t)
            rooms[r["id"]] = {"value": sum(vals) / len(vals) if ok else None, "target": r.get("target"),
                              "ok": ok and not disturbed, "window": disturbed}
        usable_rooms = [rid for rid, x in rooms.items() if x["ok"]]
        if rooms and not usable_rooms and any(x["window"] for x in rooms.values()):
            reasons.append("window")
        if reasons:
            state = "disturbed"
        elif outdoor is None or flow is None or not usable_rooms:
            # AC20-05: an unknown operating status is no confirmed heating/undisturbed state; this is checked
            # per night via heating_verified / disturbances_verified (Review 7 keeps "technically valid" separate)
            state = "invalid"
        else:
            state = "ok"
        tick_info[t] = {"state": state, "reasons": reasons, "outdoor": outdoor, "outdoor_raw": outdoor_raw,
                        "rooms": rooms, "heating": heating_confirmed, "disturbances_known": disturbances_known}

    # ---- slots and nights (local time)
    slots = []
    cursor = start
    tick_list = sorted(tick_info)
    j = 0
    while cursor < end:
        nxt = cursor + SLOT
        while j < len(tick_list) and tick_list[j] < cursor:
            j += 1
        hit = tick_list[j] if j < len(tick_list) and tick_list[j] < nxt else None
        slots.append((cursor, hit, tick_info[hit]["state"] if hit else "missing"))
        cursor = nxt

    # ---- segments: a parameter/target change starts a new evaluation section (no mixing)
    boundaries = [start] + [c["at"] for c in changes]
    seg_of = lambda t: max(i for i, b in enumerate(boundaries) if b <= max(t, start))
    nights = []
    if slots:
        first_local = start.astimezone(tz).date() - timedelta(days=1)
        last_local = end.astimezone(tz).date()
        day = first_local
        while day <= last_local:
            n_start = datetime.combine(day, time(th["night_start_hour"]), tz).astimezone(timezone.utc)
            n_end = datetime.combine(day + timedelta(days=1), time(th["night_end_hour"]), tz).astimezone(timezone.utc)
            day += timedelta(days=1)
            if n_end <= start or n_start >= end:
                continue
            ns = [s for s in slots if n_start <= s[0] < n_end]
            if not ns:
                continue
            n_end_seg = seg_of(max(n_end - SLOT, start))
            blocks = _night_blocks(ns, th)
            valid_blocks = [x for x in blocks if x[3]]
            seg = max((seg_of(x[0]) for x in valid_blocks), default=n_end_seg)
            # AC20-07: never add up hours across a real parameter/target change
            counted = [x for x in valid_blocks if seg_of(x[0]) == seg]
            qualifies = _night_qualifies(counted, th)
            ok_ticks = [s[1] for s in ns if s[2] == "ok"]
            in_window = [t for x in counted for t in x[2]] if qualifies else []
            basis = in_window or ok_ticks           # descriptive values of an unsuitable night: all ok polls
            outdoor = [tick_info[t]["outdoor"] for t in basis]
            outdoor_raw = [tick_info[t]["outdoor_raw"] for t in basis if tick_info[t]["outdoor_raw"] is not None]
            reasons = sorted({r for s in ns if s[1] for r in tick_info[s[1]]["reasons"]})
            share = lambda key: (sum(1 for t in in_window if tick_info[t][key]) / len(in_window)) if in_window else 0.0
            episodes = [e for e in defrost_ep if n_start <= e[0] + SLOT < n_end]
            nights.append({
                "heating_verified": qualifies and share("heating") >= STATUS_SHARE,
                "disturbances_verified": qualifies and share("disturbances_known") >= STATUS_SHARE,
                "date": (day - timedelta(days=1)).isoformat(), "start": n_start.isoformat(), "end": n_end.isoformat(),
                "valid": qualifies, "segment": seg,
                "valid_window": [counted[0][0].isoformat(), counted[-1][1].isoformat()] if qualifies else None,
                "blocks": [{"start": x[0].isoformat(), "end": x[1].isoformat(), "ok_hours": round(len(x[2]) * th["slot_minutes"] / 60, 2),
                            "counted": qualifies and x in counted} for x in valid_blocks],
                "counted_hours": round(len(in_window) * th["slot_minutes"] / 60, 1),
                "ok_hours": round(len(ok_ticks) * th["slot_minutes"] / 60, 1),
                "missing_share": round(sum(1 for s in ns if s[2] == "missing") / len(ns), 2),
                "outdoor_median": _median(outdoor), "outdoor_raw_median": _median(outdoor_raw),
                "defrost_events": len(episodes), "defrost_unrecovered": sum(1 for e in episodes if not e[2]),
                "disturbances": reasons, "_ok_ticks": ok_ticks, "_start": n_start, "_end": n_end,
                "_blocks": [(x[0], x[1], x[2]) for x in counted] if qualifies else [],
            })

    current_segment = len(boundaries) - 1
    valid_current = [n for n in nights if n["segment"] == current_segment and n["valid"]]
    # Only nights with confirmed heating operation count for level and slope (AC6-02).
    current = [n for n in valid_current if n["heating_verified"]]

    # ---- technical coverage of essential entities, counted only while assigned (AC-09)
    assigned = {}
    for role in ("outdoor_damped", "outdoor", "flow", "flow_target"):
        if roles.get(role):
            assigned[roles[role]] = [(start, end)]
    for i, (at, cfg) in enumerate(history):
        a_from = max(at, start)
        a_to = min(history[i + 1][0], end) if i + 1 < len(history) else end
        if a_to <= a_from:
            continue
        for r in cfg.get("rooms", []):
            for sensor in r.get("temperature_sensors", []):
                spans = assigned.setdefault(sensor, [])
                if spans and spans[-1][1] == a_from:
                    spans[-1] = (spans[-1][0], a_to)
                else:
                    spans.append((a_from, a_to))
    coverage = []
    for eid, spans in assigned.items():
        expected = sum(max(1, int((b - a).total_seconds() // SLOT.total_seconds())) for a, b in spans)
        got = sum(1 for t in ticks if any(a <= t < b for a, b in spans) and _num(values.get(t, {}).get(eid)) is not None)
        coverage.append({"entity_id": eid, "since": spans[0][0].isoformat(), "until": spans[-1][1].isoformat(),
                         "share": round(min(1.0, got / expected), 3)})
    outages = []
    for a, b in zip([start] + ticks, ticks + [end]):
        if (b - a) >= timedelta(hours=th["long_outage_hours"]):
            outages.append({"start": a.isoformat(), "end": b.isoformat(), "hours": round((b - a).total_seconds() / 3600, 1)})
    low = [c for c in coverage if c["share"] < th["technical_coverage"]]

    # ---- rooms: deviation from comfort target in valid night windows of the current segment
    last_cfg = history[-1][1] if history else {"rooms": []}
    room_results = []
    for r in last_cfg.get("rooms", []):
        devs, trends = [], []
        for n in current:
            for _, _, block_ticks in n["_blocks"]:          # AC20-09: trends never bridge a disturbance
                pts = [(t, tick_info[t]["rooms"][r["id"]]) for t in block_ticks
                       if r["id"] in tick_info[t]["rooms"] and tick_info[t]["rooms"][r["id"]]["ok"]]
                devs += [x["value"] - float(x["target"]) for _, x in pts if x["target"] is not None]
                slope = _slope([(t, x["value"]) for t, x in pts])
                if slope is not None:
                    trends.append(slope)
        monitored = bool(r.get("temperature_sensors"))
        trend = _median(trends)
        room_results.append({"id": r["id"], "name": r["name"], "target": r.get("target"), "monitored": monitored,
                             "deviation_median_k": _median(devs), "trend_k_per_h": trend,
                             "stationary": None if trend is None else abs(trend) <= th["trend_k_per_h"],
                             "windows_contacts": bool(r.get("window_sensors"))})
        if monitored and not r.get("window_sensors"):
            missing_basis.append(f"{r['name']}: kein Fensterkontakt – Fensteröffnungen nicht erkennbar")

    # ---- stage
    reasons = []
    band = [n["outdoor_median"] for n in current if n["outdoor_median"] is not None]
    sorted_band = sorted(band)
    if not ticks:
        stage = "no_data"
        reasons.append("In dieser Messung wurden keine Messwerte gespeichert.")
    else:
        stage = "insufficient"
        if not outdoor_role or not flow_roles:
            reasons.append("Außen- oder Vorlauftemperatur fehlt – Heizkurve nicht beurteilbar.")
        elif not any(r["monitored"] for r in room_results):
            reasons.append("Kein überwachter Raum.")
        elif low:
            reasons.append("Technische Abdeckung unter "
                           f"{round(th['technical_coverage'] * 100)} % bei: " + ", ".join(c["entity_id"] for c in low) + ".")
        elif len(valid_current) < th["nights_level"]:
            reasons.append(f"Nur {len(valid_current)} geeignete Nächte (mindestens {th['nights_level']} nötig).")
        elif len(current) < th["nights_level"]:
            stage = "unverified"
            if not roles.get("heating_active") and not roles.get("hp_activity"):
                reasons.append("Kein Betriebsstatus der Wärmepumpe zugeordnet – Heizbetrieb nicht nachweisbar. "
                               "Geräteprofil oder Heizbetriebs-Entität zuordnen.")
            else:
                reasons.append(f"Nur {len(current)} von {len(valid_current)} technisch auswertbaren Nächten mit nachgewiesenem "
                               "Heizbetrieb (Status fehlt, unbekannt oder zeigt keinen Heizbetrieb).")
        else:
            stage = "level"
            if len(current) < th["nights_slope"]:
                reasons.append(f"Für die Steigung sind mindestens {th['nights_slope']} geeignete Nächte nötig.")
            elif sorted_band[-th["nights_per_group"]] - sorted_band[th["nights_per_group"] - 1] < th["slope_band_k"]:
                reasons.append("Steigung mangels Temperaturspreizung nicht beurteilbar "
                               f"(je {th['nights_per_group']} Nächte müssen ≥ {th['slope_band_k']:g} K auseinanderliegen).")
            else:
                stage = "level_and_slope"
        if outages:
            reasons.append(f"{len(outages)} Messausfall/-ausfälle über {th['long_outage_hours']} h – gesondert prüfen.")
    # WW/defrost phases must be visible in the evaluated nights, otherwise the result is restricted.
    disturbance_checked = bool(current) and all(n["disturbances_verified"] for n in current)
    disturbance_notice = None
    if stage in ("level", "level_and_slope") and not disturbance_checked:
        disturbance_notice = "WW-/Abtauphasen nicht zuverlässig erfasst. Messergebnis fachlich eingeschränkt."
    if not out["window"]["complete"] and stage != "no_data":
        reasons.append("Messung läuft noch – Ergebnis vorläufig.")

    for n in nights:
        for key in ("_ok_ticks", "_start", "_end", "_blocks"):
            n.pop(key, None)
    raw_diffs = [n["outdoor_raw_median"] - n["outdoor_median"] for n in nights
                 if n["outdoor_raw_median"] is not None and n["outdoor_median"] is not None and roles.get("outdoor_damped")]
    out.update({
        "stage": stage, "reasons": reasons,
        "stage_text": STAGE_TEXT[stage], "disturbance_notice": disturbance_notice,
        # Review 8: without verified DHW/defrost coverage a level/slope statement is only provisional.
        "restricted": disturbance_notice is not None, "restricted_label": RESTRICTED_LABEL if disturbance_notice else None,
        "disturbances_verified": disturbance_checked,
        "nights": nights, "technical_nights": len(valid_current), "valid_nights": len(current), "valid_hours": round(sum(n["counted_hours"] for n in current), 1),
        "rule_version": RULE_VERSION,
        "defrost": {"events": len(defrost_ep), "unrecovered": sum(1 for e in defrost_ep if not e[2])},
        "outdoor_band": {"min": sorted_band[0] if band else None, "max": sorted_band[-1] if band else None,
                         "span_k": round(sorted_band[-1] - sorted_band[0], 1) if band else None,
                         "damped": bool(roles.get("outdoor_damped")),
                         "raw_minus_damped_median_k": _median(raw_diffs)},
        "segments": len(boundaries), "current_segment_start": boundaries[-1].isoformat(),
        "changes": [{"at": c["at"].isoformat(), "kind": c["kind"], "detail": c["detail"]} for c in changes],
        "events": {"dhw_ticks": len(dhw_ticks), "defrost_ticks": len(defrost_ticks), "heating_off_ticks": len(heating_off),
                   "window_rooms": sorted(window_open)},
        "coverage": coverage, "long_outages": outages, "rooms": room_results,
        "missing_basis": sorted(set(missing_basis)),
    })
    return out


def _de(value):
    return f"{value:.1f}".replace(".", ",").replace("-", "−")


def report_text(result, fmt=lambda iso: iso):
    """German report sentences for the cases defined in the AB-014 concept. ``fmt`` formats ISO times."""
    band = result["outdoor_band"]
    outdoor = "regelungsrelevante (gedämpfte) Außentemperatur" if band["damped"] else "ungedämpfte Außentemperatur"
    texts = []
    if result["stage"] == "unverified":
        texts.append(f"Von {result['technical_nights']} technisch auswertbaren Nächten war der Heizbetrieb nur in "
                     f"{result['valid_nights']} durch gültige Statuswerte der Wärmepumpe nachgewiesen. Temperaturdaten sind "
                     "vorhanden, eine belastbare Heizkurvenbewertung ist damit nicht möglich. Der Bericht enthält nur "
                     "beschreibende Messdaten.")
    if result["stage"] in ("no_data", "insufficient"):
        texts.append(f"Im Messzeitraum waren {result['valid_nights']} geeignete Nächte mit insgesamt "
                     f"{_de(result['valid_hours'])} verwertbaren Stunden vorhanden. "
                     f"Erforderlich sind nach dem aktuellen Pilotregelwerk mindestens {THRESHOLDS['nights_level']} Nächte. "
                     "Messung unzureichend. Heizkurvenvorschlag nicht belastbar. Messdauer verlängern oder Anlage manuell prüfen.")
    if result["stage"] == "level" and band["min"] is not None:
        texts.append(f"Die {outdoor} lag während geeigneter Messzeiten zwischen "
                     f"{_de(band['min'])} und {_de(band['max'])} °C (Spanne {_de(band['span_k'])} K). "
                     "Nur Heizkurvenniveau innerhalb des gemessenen Außentemperaturbereichs beurteilbar. "
                     "Steigung und Frostverhalten nicht nachgewiesen.")
    if result["stage"] == "level_and_slope":
        texts.append(f"Die Messung deckte eine {outdoor} von {_de(band['min'])} bis {_de(band['max'])} °C mit "
                     f"{result['valid_nights']} geeigneten Nächten ab. Eine Anpassung des Auslegungspunkts kann fachlich "
                     "geprüft werden. Die Aussage gilt nur für diese Messbereiche; eine volle Jahresvalidierung ist nicht erfolgt.")
    if result.get("disturbance_notice"):
        texts.append(result["disturbance_notice"])
    for o in result["long_outages"]:
        texts.append(f"Zwischen {fmt(o['start'])} und {fmt(o['end'])} fehlen Messwerte. Dieser Zeitraum wurde nicht ersetzt. "
                     "Der Bericht kennzeichnet die eingeschränkte Aussagekraft.")
    return texts
