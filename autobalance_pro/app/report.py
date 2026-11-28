"""AB-020 AC-06: printable measuring report per measuring job. Pure HTML rendering, no I/O.

The report states what was measured and how far it carries. It never contains a parameter
recommendation and never claims comfort for rooms without valid measurement. If the
technical or professional criteria (AB-014) are not met, the result reads "Messung unzureichend".
"""
from datetime import datetime
from html import escape

import assessment
from coverage import parse

STAGE_CLASS = {"no_data": "bad", "insufficient": "bad", "unverified": "warn", "level": "warn", "level_and_slope": "good"}
DIST_TEXT = {"dhw": "Warmwasser", "defrost": "Abtauung", "heating_off": "kein Heizbetrieb",
             "change": "Parameter-/Sollwertänderung", "window": "Fenster offen"}


def _t(value, tz, with_time=True):
    dt = parse(value) if isinstance(value, str) else value
    if dt is None:
        return "–"
    return dt.astimezone(tz).strftime("%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y")


def _n(value, digits=1, unit=""):
    if value is None:
        return "–"
    text = f"{value:.{digits}f}".replace(".", ",").replace("-", "−")
    return text + (" " + unit if unit else "")


def _pct(share):
    return "–" if share is None else f"{round(share * 100)} %"


def _table(headers, rows):
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body or '<tr><td colspan=' + str(len(headers)) + '>–</td></tr>'}</tbody></table>"


CSS = """
@page{size:A4 portrait;margin:14mm 13mm 16mm}
*{box-sizing:border-box}
body{font:13.5px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#1b2a33;max-width:860px;margin:20px auto;padding:0 14px;background:#fff}
h1{font-size:20px;margin:0;color:#183c4a}h2{font-size:15px;margin:20px 0 8px;color:#183c4a;border-bottom:1.5px solid #183c4a;padding-bottom:3px;text-transform:uppercase;letter-spacing:.03em}
h3{font-size:14px;margin:14px 0 6px}
table{border-collapse:collapse;width:100%;margin:6px 0}th,td{border:1px solid #c9d3d9;padding:4px 6px;text-align:left;vertical-align:top}
th{background:#eef2f4;font-weight:600}thead{display:table-header-group}tr{break-inside:avoid;page-break-inside:avoid}
.head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;border-bottom:3px solid #183c4a;padding-bottom:10px}
.head img{height:46px;width:auto}.head .t{text-align:right}.head .sub{color:#4b5d68;font-size:13px}
.meta{display:flex;flex-wrap:wrap;gap:4px 18px;font-size:12.5px;color:#33454f;margin:8px 0 0}
.state{display:inline-block;border:1.5px solid currentColor;border-radius:4px;padding:1px 7px;font-weight:700;font-size:12px}
.box{border:1.5px solid #183c4a;border-radius:6px;padding:10px 12px;margin:12px 0}
.box h2{border:0;margin:0 0 6px;padding:0}
.big{font-size:16px;font-weight:700;margin:2px 0}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:2px 12px;margin:4px 0}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:10px 0}
.tile{border:1px solid #c9d3d9;border-radius:6px;padding:7px 8px}.tile b{display:block;font-size:16px}.tile span{font-size:11.5px;color:#4b5d68}
.result{border-radius:6px;padding:8px 11px;margin:8px 0;font-weight:600;border:1.5px solid}
.bad{background:#fde4e4;color:#7d1717}.warn{background:#fff2da;color:#5e4100}.good{background:#e1f4e9;color:#124d33}
.nd{color:#5e4100;font-weight:600}
.hint{color:#4b5d68;font-size:12px}ul{margin:5px 0 5px 18px;padding:0}
.sign{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:10px}.sign div{border-top:1px solid #1b2a33;padding-top:3px;font-size:11.5px;color:#4b5d68}
.appendix{break-before:page;page-break-before:always}
.appendix>h1{font-size:17px;border-bottom:3px solid #183c4a;padding-bottom:6px}
svg text{font:11px system-ui,sans-serif;fill:#33454f}
@media (max-width:620px){.tiles{grid-template-columns:repeat(2,1fr)}.head{display:block}.head .t{text-align:left}table{font-size:12px}}
@media print{h2,h3{break-after:avoid;page-break-after:avoid}body{margin:0;max-width:none;padding:0;font-size:11px;line-height:1.35}.noprint{display:none}.box,.tiles,.chart{break-inside:avoid}
h2{font-size:13px;margin:12px 0 5px}.box{padding:7px 10px;margin:8px 0}.tiles{margin:7px 0}.result{margin:6px 0;padding:6px 10px}
.head img{height:38px}h1{font-size:17px}th,td{padding:3px 5px}.sign{margin-top:16px}svg.chart{max-height:170px}
.bad,.warn,.good{background:none}}
"""

STAGE_SHORT = {"no_data": "Messung nicht ausreichend", "insufficient": "Messung nicht ausreichend",
               "unverified": "Heizbetrieb nicht nachgewiesen", "level": "Nur Heizkurvenniveau prüfbar",
               "level_and_slope": "Niveau und Steigung prüfbar"}
NOT_DETERMINABLE = "Nicht zuverlässig bestimmbar"
NOT_YET = "Noch nicht berechenbar"


def next_step(result, running):
    """Plain-language next step from the documented AB-014 stage only."""
    stage = result["stage"]
    if running:
        return "Messung weiterlaufen lassen. Die abschließende Bewertung folgt nach Messende."
    if stage in ("no_data", "insufficient"):
        return "Messung verlängern oder Datenlücken beheben; erst dann ist eine Heizkurvenprüfung möglich."
    if stage == "unverified":
        return "Heizbetriebsstatus der Wärmepumpe prüfen bzw. zuordnen und erneut messen."
    if result.get("restricted"):
        return "Warmwasser-/Abtaustatus zuordnen; Ergebnis nur vorläufig durch den Techniker prüfen."
    return "Techniker prüft die Heizkurve anhand dieses Berichts. AutoBalance gibt keine Einstellung vor."


def _curve_svg(points, damped):
    """Measured (outdoor, flow target) pairs as dots - no fitted or modelled curve (AC-030-14/15)."""
    if not points:
        return ('<p class="nd">Keine gemeinsamen Messwerte von Vorlauf-Soll und Außentemperatur in diesem Messauftrag.</p>')
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    x0, x1 = min(-15.0, min(xs) - 1), max(15.0, max(xs) + 1)
    y0, y1 = min(20.0, min(ys) - 2), max(55.0, max(ys) + 2)
    W, H, L, B, T, R = 640, 250, 44, 34, 10, 12
    sx = lambda x: L + (x - x0) / (x1 - x0) * (W - L - R)
    sy = lambda y: T + (y1 - y) / (y1 - y0) * (H - T - B)
    parts = [f'<svg class="chart" viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="Gemessene Vorlauf-Sollwerte über Außentemperatur">',
             f'<rect x="{L}" y="{T}" width="{W-L-R}" height="{H-T-B}" fill="none" stroke="#9aa9b2"/>']
    step = 5
    for gx in range(int(x0 // step * step), int(x1) + 1, step):
        if x0 <= gx <= x1:
            parts.append(f'<line x1="{sx(gx):.1f}" y1="{T}" x2="{sx(gx):.1f}" y2="{H-B}" stroke="#e3e8eb"/>'
                         f'<text x="{sx(gx):.1f}" y="{H-B+14}" text-anchor="middle">{gx}</text>')
    for gy in range(int(y0 // 10 * 10), int(y1) + 1, 10):
        if y0 <= gy <= y1:
            parts.append(f'<line x1="{L}" y1="{sy(gy):.1f}" x2="{W-R}" y2="{sy(gy):.1f}" stroke="#e3e8eb"/>'
                         f'<text x="{L-6}" y="{sy(gy)+4:.1f}" text-anchor="end">{gy}</text>')
    parts.append(f'<line x1="{sx(0):.1f}" y1="{T}" x2="{sx(0):.1f}" y2="{H-B}" stroke="#183c4a" stroke-dasharray="4 3" stroke-width="1.5"/>'
                 f'<text x="{sx(0)+4:.1f}" y="{T+12}">0 °C</text>')
    for x, y in points:
        parts.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="2.2" fill="#126e66" fill-opacity=".55"/>')
    parts.append(f'<text x="{(W+L)/2:.0f}" y="{H-4}" text-anchor="middle">Außentemperatur {"gedämpft" if damped else "ungedämpft"} in °C</text>'
                 f'<text x="12" y="{(H-B)/2:.0f}" transform="rotate(-90 12 {(H-B)/2:.0f})" text-anchor="middle">Vorlauf-Soll °C</text></svg>')
    return "".join(parts)


def render(session, result, gaps, events, params, labels, tz, tz_name, version, now, frozen_at=None, *,
           cfg=None, curve_points=None, curve_damped=True, logo_uri=None):
    """Return the complete HTML document (AB-030 layout v2: customer page 1 + technician appendix).

    session: stored measuring job. result: assessment.assess(...) incl. "texts".
    gaps: AB-020 gaps (> 15 min) inside the job window. events: app starts/stops in the window.
    params: [(label, value, unit)] parameter state at the end of the window. labels: {entity_id: text}.
    cfg: configuration of the job (rooms, emitters, heat source). curve_points: measured (outdoor, flow target).
    Nothing here calculates a recommendation (AB-031 not released): such fields read "Noch nicht berechenbar".
    """
    e = escape
    cfg = cfg or {}
    w = result["window"]
    running = not session.get("ended_at")
    stage = result["stage"]
    hs = (cfg.get("building") or {}).get("heat_source") or {}
    circuits = cfg.get("circuits") or []
    circuit = next((c for c in circuits if c.get("id") == hs.get("circuit_id")), circuits[0] if len(circuits) == 1 else None)
    circuit_text = f"{circuit.get('name') or circuit.get('id')} ({circuit.get('id')})" if circuit else "nicht zugeordnet"
    status = "Laufende Messung · keine abschließende Bewertung" if running else "Abgeschlossen"
    rooms = result["rooms"]
    monitored = [r for r in rooms if r["monitored"]]
    band = result["outdoor_band"]
    technical = result.get("technical_nights", result["valid_nights"])     # frozen pre-review results lack the key
    logo = f'<img src="{logo_uri}" alt="Jodeit Systeme">' if logo_uri else '<b>Jodeit Systeme</b>'
    parts = [f"""<!DOCTYPE html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ergebnisbericht {e(session['name'])}</title><style>{CSS}</style></head><body>
<p class="noprint"><button onclick="window.print()">Drucken / als PDF speichern</button></p>
<div class="head" id="report-head">{logo}<div class="t"><h1>AutoBalance Pro · Ergebnisbericht Heizungsoptimierung</h1>
<div class="sub">Heizkurvenprüfung und Heizkörperempfehlungen</div></div></div>
<div class="meta"><span><b>Auftrag:</b> {e(session['name'])}</span><span><b>ID:</b> {e(session.get('id', '–'))}</span>
<span><b>Heizkreis:</b> {e(circuit_text)}</span>
<span><b>Zeitraum:</b> {e(_t(session['started_at'], tz))} – {e(_t(session['ended_at'], tz) if session.get('ended_at') else 'läuft')}</span>
<span><b>Erstellt:</b> {e(_t(frozen_at or now, tz))}</span><span class="state" id="report-status">{e(status)}</span></div>
<p class="hint" id="report-state">{e("Bericht bei Messende erstellt und unverändert gespeichert. Enthält nur Daten dieser Messung." if frozen_at else "Vorläufiger Bericht einer laufenden Messung. Er wird beim Stoppen mit dem Endstand festgeschrieben.")}</p>

<div class="box" id="report-intervention"><h2>Was hat AutoBalance tatsächlich verändert?</h2>
<p class="big">Nur gemessen, nicht gesteuert</p>
<div class="kv"><span>Empfehlungen:</span><b>{NOT_YET}</b><span>Umsetzung:</span><b>Nicht dokumentiert</b></div>
<p><b>AutoBalance hat die Wärmepumpe nicht verändert.</b> Die nachfolgenden Ergebnisse beruhen auf Messwerten; vorgeschlagene
Änderungen müssen durch eine Fachkraft geprüft und bei Bedarf separat umgesetzt werden.</p></div>

<div class="tiles" id="report-quality">
<div class="tile"><b>{_n(w['hours'] / 24, 1)} Tage</b><span>Messdauer (geplant {w['planned_days']} Tage)</span></div>
<div class="tile"><b>{result['valid_nights']} von {technical}</b><span>Nächte mit bestätigtem Heizbetrieb / technisch auswertbar</span></div>
<div class="tile"><b>{len(monitored)} von {len(rooms)}</b><span>Räume überwacht</span></div>
<div class="tile"><b>{(_n(band['min'], 1) + ' … ' + _n(band['max'], 1, '°C')) if band['min'] is not None else 'nicht erfasst'}</b>
<span>Außentemperatur {'gedämpft' if band['damped'] else 'ungedämpft'}, geeignete Nächte</span></div></div>

<div class="result {'warn' if result.get('restricted') else STAGE_CLASS[stage]}" id="report-result">{e((result['restricted_label'] + ': ') if result.get('restricted') else '')}{e(result['stage_text'])}</div>"""]
    if result.get("disturbance_notice"):
        parts.append(f'<p class="nd" id="report-disturbance">{e(result["disturbance_notice"])}</p>')

    parts.append(f"""<h2>Heizkurve und Vergleich bei 0 °C Außentemperatur</h2>
{_curve_svg(curve_points or [], curve_damped)}
<p class="hint">Punkte: im Messauftrag gemessene Vorlauf-Sollwerte des Reglers (5-Minuten-Werte) über der
{'gedämpften' if curve_damped else 'ungedämpften'} Außentemperatur. Keine berechnete oder geglättete Kurve.</p>
{_table(["Vorlauf-Soll bei 0 °C gedämpfter Außentemperatur", "Wert", "Grundlage"], [
        ["Aktuell", f'<span class="nd">{NOT_DETERMINABLE}</span>',
         "Der Regler liefert keinen Kurvenwert bei 0 °C; die Herstellerkurve ist nicht als Modell hinterlegt."],
        ["Empfohlen", f'<span class="nd">Noch nicht ableitbar</span>', "Keine freigegebene Empfehlungslogik (AB-031)."],
        ["Tatsächlich umgesetzt", f'<span class="nd">Nicht dokumentiert</span>', "Keine Änderung am Regler erfasst."]])}""")

    ranked = sorted(rooms, key=lambda r: (not r["monitored"], -abs(r["deviation_median_k"] or 0)))[:5]
    def observation(r):
        if not r["monitored"]:
            return "nicht überwacht"
        d = r["deviation_median_k"]
        if d is None:
            return "keine Daten in geeigneten Nächten"
        txt = f"im Median {_n(abs(d), 1, 'K')} {'über' if d > 0 else 'unter' if d < 0 else 'auf'} Soll"
        return txt + ("" if r["stationary"] is not False else ", nicht stationär")
    parts.append("<h2>Empfehlungen für die Räume</h2>")
    parts.append(_table(["Raum", "Soll", "Beobachtung (geeignete Nächte)", "Empfehlung"],
                        [[e(r["name"]), _n(r["target"], 1, "°C"), e(observation(r)),
                          f'<span class="nd">{NOT_YET}</span> · Prüfung erforderlich'] for r in ranked]))
    parts.append('<p class="hint">Vollständige Raum- und Heizkörperliste im Anhang. Eine Raumabweichung allein bestimmt '
                 "keine Ventilvoreinstellung.</p>")

    parts.append(f"""<div class="box" id="report-signoff"><h2>Nächster Schritt und Technikerabnahme</h2>
<p><b>Nächster Schritt:</b> {e(next_step(result, running))}</p>
<p>Messbedingung „Thermostatköpfe auf Stufe 5“: <b>nicht bestätigt</b> (wird in dieser Version nicht erfasst).</p>
<p><b>Technische Auswertung, noch nicht durch Techniker abgenommen.</b> Dieser Bericht ist weder eine neue
Heizlastberechnung noch ein Nachweis eines hydraulischen Abgleichs.</p>
<div class="sign"><div>Geprüft durch (Name)</div><div>Datum</div></div></div>""")

    # ------------------------------------------------------------------ technician appendix
    parts.append('<section class="appendix" id="report-appendix"><h1>Anhang: Messwerte und Technik</h1>')
    parts.append(f"<p><b>Messung:</b> {e(session['name'])} · Dauer {_n(w['hours'], 1, 'h')} · geplant {w['planned_days']} Tage"
                 f" · Zeitzone {e(tz_name)}</p>")
    if session.get("note"):
        parts.append("<p><b>Notiz:</b> " + e(session["note"]).replace(chr(10), "<br>") + "</p>")
    for text in assessment.report_text(result, lambda iso: _t(iso, tz)):
        parts.append(f"<p>{e(text)}</p>")
    if result["reasons"]:
        parts.append("<ul>" + "".join(f"<li>{e(r)}</li>" for r in result["reasons"]) + "</ul>")
    parts.append('<p class="hint">Alle Schwellen sind Pilotwerte (Regelwerk AB-014), noch im Feld zu bestätigen.</p>')

    emitters = cfg.get("emitters") or []
    room_names = {r.get("id"): r.get("name") for r in cfg.get("rooms") or []}
    room_load = {r.get("id"): (r.get("heat_load") or {}) for r in cfg.get("rooms") or []}
    circuit_names = {c.get("id"): c.get("name") or c.get("id") for c in circuits}
    def load_text(rid):
        hl = room_load.get(rid) or {}
        return (_n(hl.get("value_w"), 0, "W") + (f" ({e(hl.get('reference'))})" if hl.get("reference") else "")
                if hl.get("status") == "adopted" and hl.get("value_w") else "fehlt")
    def valve_text(v):
        v = v or {}
        name = " ".join(x for x in (v.get("manufacturer"), v.get("series")) if x) or v.get("label") or "unbekannt"
        return name + (f", Einstellung {v['current_setting']}" if v.get("current_setting") else "")
    parts.append("<h2>Heizkörper je Raum</h2>")
    parts.append(_table(["Raum", "Heizfläche", "Heizkreis", "Raumheizlast", "Leistung (Quelle)", "Ventil", "Empfohlene Voreinstellung"],
                        [[e(room_names.get(x.get("room_id"), "–")), e(x.get("name") or x.get("id", "–")),
                          e(circuit_names.get(x.get("circuit_id"), "–")), load_text(x.get("room_id")),
                          (_n((x.get("rated_output") or {}).get("value_w"), 0, "W") + " (" + e((x.get("rated_output") or {}).get("source") or "unbekannt") + ")")
                          if (x.get("rated_output") or {}).get("value_w") else "fehlt",
                          e(valve_text(x.get("valve"))),
                          '<span class="nd">Nicht belastbar berechenbar</span> – Technikerprüfung erforderlich'] for x in emitters]))
    parts.append('<p class="hint">„Thermostatstellung 5“ ist eine Messbedingung, keine Ventilvoreinstellung. Eine motorische '
                 "Ventilöffnung in Prozent entspricht keinem Volumenstrom in l/h.</p>")

    parts.append("<h2>Außentemperatur und geeignete Nächte</h2>")
    parts.append(f"<p>Technisch auswertbare Nächte im aktuellen Auswertungsabschnitt: <b>{technical}</b> · "
                 f"davon Heizbetrieb bestätigt: <b>{result['valid_nights']}</b> · "
                 f"verwertbare Stunden: <b>{_n(result['valid_hours'])}</b> · Außentemperatur "
                 f"({'gedämpft, regelungsrelevant' if band['damped'] else 'ungedämpft'}) in geeigneten Nächten: "
                 f"{_n(band['min'], 1, '°C')} bis {_n(band['max'], 1, '°C')} (Spanne {_n(band['span_k'], 1, 'K')}).</p>")
    if band.get("raw_minus_damped_median_k") is not None:
        parts.append(f"<p class=\"hint\">Ungedämpfte minus gedämpfte Außentemperatur (Median): "
                     f"{_n(band['raw_minus_damped_median_k'], 1, 'K')}.</p>")
    parts.append(_table(["Nacht", "Technisch auswertbar", "Heizbetrieb bestätigt", "Verwertbar", "Außen (Median)", "Fehlende Slots", "Störungen"],
                        [[_t(n["date"] + "T12:00:00+00:00", tz, False), "ja" if n["valid"] else "nein",
                          ("–" if not n["valid"] or "heating_verified" not in n else "ja" if n["heating_verified"] else "nicht nachgewiesen"),
                          _n(n["ok_hours"], 1, "h"),
                          _n(n["outdoor_median"], 1, "°C"), _pct(n["missing_share"]),
                          e(", ".join(DIST_TEXT.get(d, d) for d in n["disturbances"]) or "–")] for n in result["nights"]]))

    parts.append("<h2>Räume</h2>")
    parts.append(_table(["Raum", "Komfort-Soll", "Überwacht", "Ist − Soll (Median, geeignete Nächte)", "Trend", "Fensterkontakt"],
                        [[e(r["name"]), _n(r["target"], 1, "°C"), "ja" if r["monitored"] else "nein",
                          _n(r["deviation_median_k"], 1, "K") if r["monitored"] else "nicht gemessen",
                          ("–" if r["trend_k_per_h"] is None else _n(r["trend_k_per_h"], 2, "K/h")
                           + ("" if r["stationary"] else " (nicht stationär)")),
                          "ja" if r["windows_contacts"] else "nein"] for r in rooms]))
    unmonitored = [r["name"] for r in rooms if not r["monitored"]]
    if unmonitored:
        parts.append(f"<p><b>Nicht überwachte Räume:</b> {e(', '.join(unmonitored))}. Für diese Räume trifft der "
                     "Bericht keine Aussage zum Komfort. Der schlechteste Raum des Gebäudes ist damit nicht sicher identifiziert.</p>")
    chosen = [r for r in session.get("rooms", []) if r.get("hints")]
    if chosen:
        parts.append("<p class=\"hint\">Begründung der Messpunkte bei Start: " + e(" · ".join(
            f"{r['name']}: {', '.join(r['hints'])}" for r in chosen)) + "</p>")

    if params:
        parts.append("<h2>Reglerparameter (im Messzeitraum ausgelesen, nur gelesen)</h2>")
        parts.append(_table(["Parameter", "Wert"], [[e(label), e(f"{value} {unit}".strip())] for label, value, unit in params]))
        parts.append('<p class="hint">Die minimale Außentemperatur des Reglers ist der Auslegungspunkt der Heizkurve, nicht die '
                     "Norm-Außentemperatur der Heizlastberechnung.</p>")

    parts.append("<h2>Technische Datenerfassung</h2>")
    parts.append(_table(["Messgröße", "Zugeordnet ab", "bis", "Erfasst"],
                        [[e(labels.get(c["entity_id"], c["entity_id"])), _t(c["since"], tz), _t(c["until"], tz), _pct(c["share"])]
                         for c in result["coverage"]]))
    if gaps:
        parts.append("<p><b>Messlücken über 15 Minuten</b> (nicht aufgefüllt):</p><ul>" + "".join(
            f"<li>{_t(g['start'], tz)} bis {_t(g['end'], tz)} ({g['minutes']} min){' – andauernd' if g.get('ongoing') else ''}</li>"
            for g in gaps) + "</ul>")
    else:
        parts.append("<p>Keine Messlücken über 15 Minuten.</p>")
    starts = [ev for ev in events if ev["kind"] == "start"]
    if starts:
        parts.append(f"<p class=\"hint\">App-Starts im Messzeitraum: {e(', '.join(_t(ev['at'], tz) for ev in starts))}</p>")

    parts.append("<h2>Störungen und Änderungen</h2>")
    ev = result["events"]
    parts.append(f"<p>Warmwasser: {ev['dhw_ticks']} Messzeitpunkte · Abtauung: {ev['defrost_ticks']} · "
                 f"kein Heizbetrieb: {ev['heating_off_ticks']} · Räume mit Fensteröffnung: "
                 f"{e(', '.join(r['name'] for r in rooms if r['id'] in ev['window_rooms']) or '–')}</p>")
    if result["changes"]:
        parts.append("<ul>" + "".join(
            f"<li>{_t(c['at'], tz)} · {'Raumsollwert' if c['kind'] == 'target' else 'Reglerparameter'}: "
            f"{e(', '.join(labels.get(d, d) for d in c['detail']))}</li>" for c in result["changes"]) + "</ul>")
        parts.append(f"<p class=\"hint\">Ausgewertet wird nur der Abschnitt ab {_t(result['current_segment_start'], tz)}.</p>")
    else:
        parts.append("<p>Keine Änderung an Heizkurve oder Raumsollwerten im Messzeitraum erkannt.</p>")

    parts.append("<h2>Fehlende Grundlagen und Restarbeiten</h2><ul>")
    for m in result["missing_basis"]:
        parts.append(f"<li>{e(m)}</li>")
    parts.append("<li>Fühleraktivität (24-Stunden-Prüfung, AB-028) wird in dieser Version nicht ausgewertet: "
                 "vor fachlicher Freigabe Fühler manuell prüfen.</li>")
    if unmonitored:
        parts.append(f"<li>Nicht überwachte Räume: {e(', '.join(unmonitored))}.</li>")
    parts.append("</ul>")

    prof_label = hs.get("profile") or "kein Geräteprofil"
    src = {"ha": "Home Assistant", "emsesp_rest": "EMS-ESP direkt (REST)"}.get(hs.get("source"), hs.get("source") or "–")
    gateway = hs.get("emsesp_host") if hs.get("source") == "emsesp_rest" else ""
    parts.append("<h2>Datenherkunft</h2>")
    parts.append(_table(["Angabe", "Wert"], [
        ["Messauftrag-ID", e(session.get("id", "–"))], ["App-Version", e(version)],
        ["Konfigurationsschema", e(str(cfg.get("schema_version", "–")))],
        ["Datenzeitraum", e(f"{_t(w['start'], tz)} – {_t(w['end'], tz)}")],
        ["Geräteprofil / Quelle", e(f"{prof_label} · {src}" + (f" · {gateway}" if gateway else ""))],
        ["Heizkreis des Profils", e(circuit_text)]]))
    parts.append('<p class="hint">Rohdaten dieser Messung sind separat als CSV abrufbar. Ein abgeschlossener Bericht wird '
                 "nicht verändert; spätere Feststellungen werden datiert ergänzt.</p>")
    parts.append("<h2>Grenzen dieses Berichts</h2><ul>"
                 "<li>Gemessen wurde nur in den genannten Räumen und im genannten Zeitraum; keine Jahresvalidierung.</li>"
                 "<li>Fehlende Zeiträume wurden nicht ersetzt oder interpoliert.</li>"
                 "<li>AutoBalance hat während der Messung nichts an Wärmepumpe, Ventilen oder Home Assistant verändert.</li>"
                 "</ul></section></body></html>")
    return "\n".join(parts)
