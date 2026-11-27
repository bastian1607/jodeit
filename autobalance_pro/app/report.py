"""AB-020 AC-06: printable measuring report per measuring job. Pure HTML rendering, no I/O.

The report states what was measured and how far it carries. It never contains a parameter
recommendation and never claims comfort for rooms without valid measurement. If the
technical or professional criteria (AB-014) are not met, the result reads "Messung unzureichend".
"""
from datetime import datetime
from html import escape

import assessment
from coverage import parse

STAGE_CLASS = {"no_data": "bad", "insufficient": "bad", "level": "warn", "level_and_slope": "good"}
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


def render(session, result, gaps, events, params, labels, tz, tz_name, version, now):
    """Return the complete HTML document.

    session: stored measuring job. result: assessment.assess(...) incl. "texts".
    gaps: AB-020 gaps (> 15 min) inside the job window. events: app starts/stops in the window.
    params: [(label, value, unit)] parameter state at the end of the window. labels: {entity_id: text}.
    """
    e = escape
    w = result["window"]
    running = not session.get("ended_at")
    status = "läuft noch – Ergebnis vorläufig" if running else "beendet"
    stage = result["stage"]
    parts = [f"""<!DOCTYPE html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Messbericht {e(session['name'])}</title>
<style>body{{font:14px system-ui,-apple-system,Segoe UI,sans-serif;color:#1d2a33;max-width:900px;margin:24px auto;padding:0 16px}}
h1{{font-size:22px;margin:0}}h2{{font-size:17px;margin:26px 0 8px;border-bottom:1px solid #d6dde2;padding-bottom:4px}}
table{{border-collapse:collapse;width:100%;margin:6px 0}}th,td{{border:1px solid #d6dde2;padding:5px 7px;text-align:left;vertical-align:top}}th{{background:#f1f4f6}}
.result{{border-radius:8px;padding:12px 14px;margin:14px 0;font-size:16px;font-weight:600}}.bad{{background:#fde4e4;color:#8a1c1c}}.warn{{background:#fff2da;color:#6b4a00}}.good{{background:#e1f4e9;color:#15583b}}
.hint{{color:#5a6b76;font-size:12.5px}}ul{{margin:6px 0 6px 18px;padding:0}}@media print{{body{{margin:0}}.noprint{{display:none}}}}</style></head><body>
<p class="noprint"><button onclick="window.print()">Drucken / als PDF speichern</button></p>
<h1>Jodeit AutoBalance Pro · Messbericht</h1>
<p class="hint">Erstellt {e(_t(now, tz))} · App-Version {e(version)} · Zeitzone {e(tz_name)} · Nur Messung, keine Steuerbefehle</p>
<h2>Messauftrag</h2>
{_table(["Bezeichnung", "Beginn", "Ende", "Dauer", "Geplant", "Status"],
        [[e(session['name']), _t(session['started_at'], tz), _t(session['ended_at'], tz) if session.get('ended_at') else "–",
          _n(w['hours'], 1, 'h'), f"{w['planned_days']} Tage", e(status)]])}
<div class="result {STAGE_CLASS[stage]}" id="report-result">{e(result['stage_text'])}</div>"""]
    for text in assessment.report_text(result, lambda iso: _t(iso, tz)):
        parts.append(f"<p>{e(text)}</p>")
    if result["reasons"]:
        parts.append("<ul>" + "".join(f"<li>{e(r)}</li>" for r in result["reasons"]) + "</ul>")
    parts.append('<p class="hint">Dieser Bericht enthält keine Einstellempfehlung. Eine Änderung der Heizkurve '
                 "entscheidet der Techniker. Alle Schwellen sind Pilotwerte (Regelwerk AB-014), noch im Feld zu bestätigen.</p>")

    band = result["outdoor_band"]
    parts.append("<h2>Außentemperatur und geeignete Nächte</h2>")
    parts.append(f"<p>Geeignete Nächte im aktuellen Auswertungsabschnitt: <b>{result['valid_nights']}</b> · "
                 f"verwertbare Stunden: <b>{_n(result['valid_hours'])}</b> · Außentemperatur "
                 f"({'gedämpft, regelungsrelevant' if band['damped'] else 'ungedämpft'}) in geeigneten Nächten: "
                 f"{_n(band['min'], 1, '°C')} bis {_n(band['max'], 1, '°C')} (Spanne {_n(band['span_k'], 1, 'K')}).</p>")
    if band.get("raw_minus_damped_median_k") is not None:
        parts.append(f"<p class=\"hint\">Ungedämpfte minus gedämpfte Außentemperatur (Median): "
                     f"{_n(band['raw_minus_damped_median_k'], 1, 'K')}.</p>")
    parts.append(_table(["Nacht", "Geeignet", "Verwertbar", "Außen (Median)", "Fehlende Slots", "Störungen"],
                        [[_t(n["date"] + "T12:00:00+00:00", tz, False), "ja" if n["valid"] else "nein", _n(n["ok_hours"], 1, "h"),
                          _n(n["outdoor_median"], 1, "°C"), _pct(n["missing_share"]),
                          e(", ".join(DIST_TEXT.get(d, d) for d in n["disturbances"]) or "–")] for n in result["nights"]]))

    parts.append("<h2>Räume</h2>")
    rooms = result["rooms"]
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
    if result["missing_basis"]:
        parts.append("<p><b>Fehlende Aussagegrundlage:</b></p><ul>" + "".join(f"<li>{e(m)}</li>" for m in result["missing_basis"]) + "</ul>")

    if params:
        parts.append("<h2>Parameterstand am Ende des Zeitraums (nur gelesen)</h2>")
        parts.append(_table(["Parameter", "Wert"], [[e(label), e(f"{value} {unit}".strip())] for label, value, unit in params]))

    parts.append("<h2>Grenzen dieses Berichts</h2><ul>"
                 "<li>Gemessen wurde nur in den genannten Räumen und im genannten Zeitraum; keine Jahresvalidierung.</li>"
                 "<li>Fehlende Zeiträume wurden nicht ersetzt oder interpoliert.</li>"
                 "<li>AutoBalance hat während der Messung nichts an Wärmepumpe, Ventilen oder Home Assistant verändert.</li>"
                 "</ul></body></html>")
    return "\n".join(parts)
