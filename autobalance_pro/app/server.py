"""AutoBalance Pro – read-only Home Assistant pilot."""
import json,sys,logging,math,os,re,sqlite3,threading,time,urllib.request
from version import VERSION
import model,coverage,devices,monitoring,assessment,report,retention,uuid
from datetime import datetime,timedelta,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
DB=os.environ.get("AUTO_BALANCE_DB","/data/autobalance.db")
ROOT=Path(__file__).resolve().parent
PORT=int(os.environ.get("PORT","8099"))
TOKEN=os.environ.get("SUPERVISOR_TOKEN","")
HA_URL=os.environ.get("HA_API_URL","http://supervisor/core/api/states")
LOCK=threading.RLock()
logging.basicConfig(level=logging.INFO)
STALE_SECONDS=1800            # own acquisition only (AB-027)
UNCHANGED_HINT_SECONDS=12*3600   # AB-027: unchanged temperature -> neutral hint, value stays valid
UNCHANGED_CHECK_SECONDS=24*3600  # AB-027/Review 9: from here a manual sensor check before any curve assessment
ENTITY_PATTERN=re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
def connect():
    c=sqlite3.connect(DB,timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA secure_delete=ON")   # AB-026: deleted content is overwritten, not just unlinked
    return c
def setup():
    Path(DB).parent.mkdir(parents=True,exist_ok=True)
    with connect() as c:
        c.execute("CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY, at TEXT, entity_id TEXT, state TEXT, unit TEXT, valid INTEGER)")
        c.execute("CREATE INDEX IF NOT EXISTS samples_at ON samples(at)")
        # Additive migration: original raw values and existing rows remain intact.
        columns={row[1] for row in c.execute("PRAGMA table_info(samples)")}
        for name,kind in (("source_at","TEXT"),("quality","TEXT"),("normalized_value","REAL"),("normalized_unit","TEXT"),("source_at_kind","TEXT")):
            if name not in columns:c.execute(f"ALTER TABLE samples ADD COLUMN {name} {kind}")
        c.execute("CREATE TABLE IF NOT EXISTS acquisition (id INTEGER PRIMARY KEY CHECK(id=1), last_attempt TEXT, last_success TEXT, connected INTEGER, error TEXT)")
        # AB-008: schema v2 lives in its own table. The legacy `config` row is kept as a v1
        # projection so a downgrade to v0.1.x still finds a readable configuration.
        c.execute("CREATE TABLE IF NOT EXISTS config_v2 (id INTEGER PRIMARY KEY CHECK(id=1), schema_version INTEGER NOT NULL, value TEXT NOT NULL, v1_mirror TEXT NOT NULL, updated_at TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (id INTEGER PRIMARY KEY, version INTEGER NOT NULL, kind TEXT NOT NULL, applied_at TEXT NOT NULL, backup_path TEXT)")
        # AB-020: app starts are logged so a gap can be related to a restart. Additive table.
        c.execute("CREATE TABLE IF NOT EXISTS app_events (id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT)")
        c.execute("CREATE INDEX IF NOT EXISTS samples_entity_at ON samples(entity_id,at)")
        # AB-013: every saved configuration is kept with its timestamp, so sensor assignments and
        # comfort targets can be evaluated as they were at measuring time (additive table).
        c.execute("CREATE TABLE IF NOT EXISTS config_history (id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL)")
        # AB-013/AB-020: a measuring job (Messauftrag) delimits the evaluated time window per site.
        c.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, name TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, planned_days INTEGER NOT NULL, rooms TEXT NOT NULL, note TEXT)")
        # AB-025: every sample taken during a measuring job carries its id; the job keeps its own
        # configuration snapshots and, once stopped, an immutable report. Additive columns only.
        columns={row[1] for row in c.execute("PRAGMA table_info(samples)")}
        if "session_id" not in columns:c.execute("ALTER TABLE samples ADD COLUMN session_id TEXT")
        c.execute("CREATE INDEX IF NOT EXISTS samples_session ON samples(session_id,at)")
        columns={row[1] for row in c.execute("PRAGMA table_info(sessions)")}
        for name in ("config_start","history","report_html","assessment","finalized_at","app_version","delete_due_at","archived_at"):
            if name not in columns:c.execute(f"ALTER TABLE sessions ADD COLUMN {name} TEXT")
        # AB-026: minimal deletion log without personal data (random job id, time, reason, counts).
        c.execute("CREATE TABLE IF NOT EXISTS deletion_log (id INTEGER PRIMARY KEY, session_ref TEXT NOT NULL, deleted_at TEXT NOT NULL, reason TEXT NOT NULL, samples_deleted INTEGER NOT NULL, backups_deleted INTEGER NOT NULL)")
        # Review 7: job window (no personal data) to clean old-schema app backups; persistent purge status.
        dl_cols={row[1] for row in c.execute("PRAGMA table_info(deletion_log)")}
        for col in ("started_at","ended_at"):
            if col not in dl_cols:c.execute(f"ALTER TABLE deletion_log ADD COLUMN {col} TEXT")
        c.execute("CREATE TABLE IF NOT EXISTS retention_state (id INTEGER PRIMARY KEY CHECK(id=1), last_run TEXT, last_ok TEXT, errors TEXT NOT NULL DEFAULT '[]', register TEXT)")
    sync_config()
    with LOCK, connect() as c:
        if not c.execute("SELECT 1 FROM config_history LIMIT 1").fetchone():
            # Baseline for installations before AB-013: valid since the first stored sample.
            first=c.execute("SELECT MIN(at) FROM samples").fetchone()[0]
            c.execute("INSERT INTO config_history(at,kind,value) VALUES(?,?,?)",(first or utcnow().isoformat(),"baseline",json.dumps(load_v2(),ensure_ascii=False)))
    migrate_sessions()
    with connect() as c:   # AB-026: deadline for jobs stopped before this version
        for sid,end in c.execute("SELECT id,ended_at FROM sessions WHERE ended_at IS NOT NULL AND delete_due_at IS NULL").fetchall():
            c.execute("UPDATE sessions SET delete_due_at=? WHERE id=?",(retention.due_at(coverage.parse(end)).isoformat(),sid))
    apply_register()
    purge()

def backup_database(reason):
    """Consistent copy via the SQLite backup API (safe with WAL), stored next to the DB."""
    folder=Path(DB).parent/"backups"
    folder.mkdir(parents=True,exist_ok=True)
    target=folder/f"autobalance-{reason}-{utcnow().strftime('%Y%m%dT%H%M%S%fZ')}.db"
    source=sqlite3.connect(DB,timeout=10)
    try:
        destination=sqlite3.connect(str(target))
        try:source.backup(destination)
        finally:destination.close()
    finally:source.close()
    logging.info("Datenbanksicherung erstellt: %s",target)
    return str(target)

def _write_config(c,cfg):
    mirror=json.dumps(model.to_v1(cfg),ensure_ascii=False,sort_keys=True)
    c.execute("INSERT INTO config_v2(id,schema_version,value,v1_mirror,updated_at) VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET schema_version=excluded.schema_version,value=excluded.value,v1_mirror=excluded.v1_mirror,updated_at=excluded.updated_at",(model.SCHEMA_VERSION,json.dumps(cfg,ensure_ascii=False),mirror,utcnow().isoformat()))
    c.execute("INSERT INTO config(id,value) VALUES (1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",(mirror,))

def sync_config():
    """Create or update schema v2 from the legacy row. Runs at start-up only.

    * no v2 yet, legacy row present -> backup, convert v1 -> v2 (first start of v0.2)
    * legacy row changed since the last mirror (e.g. after a temporary downgrade) -> backup, merge
    * fresh install -> empty v2 without backup
    Conversion is lenient: legacy data is preserved, strict validation applies on save.
    """
    with LOCK:
        with connect() as c:
            legacy=c.execute("SELECT value FROM config WHERE id=1").fetchone()
            current=c.execute("SELECT schema_version,value,v1_mirror FROM config_v2 WHERE id=1").fetchone()
        if current and current[0]!=model.SCHEMA_VERSION:
            raise RuntimeError(f"Unbekannte Schemaversion {current[0]} – neuere App-Version erforderlich")
        if current and (legacy is None or legacy[0]==current[2]):return
        if current is None and legacy is None:
            with connect() as c:_write_config(c,model.empty())
            return
        legacy_cfg=json.loads(legacy[0]) if legacy else {}
        if current is None:
            kind="v1_to_v2";cfg=model.from_v1(legacy_cfg)
        else:
            kind="legacy_merge";cfg=model.merge_v1(json.loads(current[1]),legacy_cfg)
        path=backup_database("before-"+kind.replace("_","-"))
        with connect() as c:
            _write_config(c,cfg)
            c.execute("INSERT INTO schema_migrations(version,kind,applied_at,backup_path) VALUES(?,?,?,?)",(model.SCHEMA_VERSION,kind,utcnow().isoformat(),path))
        logging.info("Konfiguration auf Schema %s gebracht (%s)",model.SCHEMA_VERSION,kind)
def empty():
    return {"rooms":[],"zones":[],"heating":{"manufacturer":"","model":"","circuit_name":"Heizkreis 1","outdoor":"","flow":"","return":"","compressor":"","dhw":""}}
def load_v2():
    with connect() as c:r=c.execute("SELECT value FROM config_v2 WHERE id=1").fetchone()
    return json.loads(r[0]) if r else model.empty()
def load():
    """Legacy v1 view used by the current UI (`GET /api/config`)."""
    return model.to_v1(load_v2())
def _as_v2(d):
    return d if isinstance(d,dict) and "schema_version" in d else model.from_v1(d)
def save_v2(cfg):
    cfg=model.validate(cfg)
    with LOCK:
        previous=load_v2()
        with connect() as c:
            _write_config(c,cfg)
            c.execute("INSERT INTO config_history(at,kind,value) VALUES(?,?,?)",(utcnow().isoformat(),"save",json.dumps(cfg,ensure_ascii=False)))
            if wanted(previous)!=wanted(cfg) or temperature_ids(previous)!=temperature_ids(cfg):
                c.execute("DELETE FROM acquisition")
    return cfg
def save_v1(data):
    """Save from the legacy UI: v1 rules first, then merge into v2 without losing v2-only fields."""
    validate(data)
    with LOCK:return save_v2(model.merge_v1(load_v2(),data))
def entity_id(value,domains,optional=False):
    if optional and value in (None,""):return
    if not isinstance(value,str) or len(value)>200 or not ENTITY_PATTERN.fullmatch(value) or value.split(".",1)[0] not in domains:
        raise ValueError("Ungültige Entitätszuordnung")

def number(value,low,high,label):
    if isinstance(value,bool):raise ValueError(label+" ungültig")
    try:n=float(value)
    except (TypeError,ValueError,OverflowError):raise ValueError(label+" ungültig")
    if not math.isfinite(n) or not low<=n<=high:raise ValueError(label+" ungültig")

def validate(d):
    if not isinstance(d,dict) or not isinstance(d.get("rooms"),list) or not isinstance(d.get("zones"),list) or not isinstance(d.get("heating"),dict):raise ValueError("Konfigurationsformat ungültig")
    if len(d["rooms"])>100 or len(d["zones"])>100 or len(json.dumps(d))>100000:raise ValueError("Konfiguration zu groß")
    ids=set()
    for r in d["rooms"]:
        if not isinstance(r,dict) or not isinstance(r.get("id"),str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}",r["id"]) or r["id"] in ids or not isinstance(r.get("name"),str) or not r["name"].strip():raise ValueError("Raum-ID/Name fehlt oder doppelt")
        ids.add(r["id"])
        if not isinstance(r.get("temperature_sensors"),list):raise ValueError("Temperatursensorliste ungültig")  # AB-013: 0 sensors allowed
        if not isinstance(r.get("window_sensors"),list) or not isinstance(r.get("radiators"),list) or len(r["radiators"])>100:raise ValueError("Fenster-/Heizkörperliste ungültig")
        number(r.get("target",21),5,35,"Raum-Solltemperatur")
        for key,limit in (("heat_load",50000),("area",1000)):
            if r.get(key) not in ("",None):number(r[key],0.001,limit,key)
        for key,domain in (("temperature_sensors","sensor"),("window_sensors","binary_sensor")):
            if len(r[key])>30:raise ValueError("Zu viele Entitäten")
            for value in r[key]:entity_id(value,{domain})
            if len(set(r[key]))!=len(r[key]):raise ValueError("Entität doppelt zugeordnet")
        for radiator in r["radiators"]:
            if not isinstance(radiator,dict) or not isinstance(radiator.get("name"),str) or not radiator["name"].strip():raise ValueError("Heizkörper benötigt Bezeichnung")
            entity_id(radiator.get("entity"),{"number","climate"},optional=True)
            if radiator.get("power_w") not in (None,""):number(radiator["power_w"],0.001,50000,"Heizkörperleistung")
    used=set()
    for z in d["zones"]:
        if not isinstance(z,dict) or not isinstance(z.get("name"),str) or not z["name"].strip() or not isinstance(z.get("room_ids"),list) or len(z["room_ids"])<2:raise ValueError("Zone benötigt Name und mindestens zwei Räume")
        for rid in z["room_ids"]:
            if not isinstance(rid,str) or rid not in ids or rid in used:raise ValueError("Unbekannter oder mehrfach zonierter Raum")
            used.add(rid)
    for key in ("outdoor","flow","return"):entity_id(d["heating"].get(key),{"sensor"},optional=True)
    for key in ("compressor","dhw"):entity_id(d["heating"].get(key),{"sensor","binary_sensor"},optional=True)
    for key in ("manufacturer","model","circuit_name"):
        if key in d["heating"] and not isinstance(d["heating"][key],str):raise ValueError("Wärmepumpendaten ungültig")
    return d
def get_entities():
    if not TOKEN:raise RuntimeError("Kein SUPERVISOR_TOKEN – nur in Home Assistant verfügbar")
    request=urllib.request.Request(HA_URL,headers={"Authorization":"Bearer "+TOKEN,"Content-Type":"application/json"})
    with urllib.request.urlopen(request,timeout=15) as response:items=json.load(response)
    if not isinstance(items,list):raise RuntimeError("Home Assistant lieferte keine Entitätenliste")
    return [ha_entity(e) for e in items if isinstance(e,dict)]
def ha_entity(e):
    """One HA state object. AB-027/Review 10: keep which HA timestamp ``source_at`` is.

    ``last_reported`` = last report of the device to HA (also with an unchanged value), ``last_updated`` = last
    state/attribute update (fallback for older HA). ``last_changed`` (real value change) is not used."""
    attrs=e.get("attributes") or {}
    kind="reported" if e.get("last_reported") else "updated" if e.get("last_updated") else "unknown"
    return {"entity_id":e.get("entity_id",""),"name":attrs.get("friendly_name",e.get("entity_id","")),"state":e.get("state",""),
        "unit":attrs.get("unit_of_measurement",""),"device_class":attrs.get("device_class",""),
        "source_at":e.get("last_reported") or e.get("last_updated"),"source_at_kind":kind}
def heat_source(d):
    return (_as_v2(d).get("building") or {}).get("heat_source") or {}
def wanted(d):
    return model.entities_of(_as_v2(d))|set(devices.entities(heat_source(d)))
def utcnow():
    return datetime.now(timezone.utc)

def age_seconds(timestamp,now=None):
    try:
        dt=datetime.fromisoformat(timestamp.replace("Z","+00:00"))
        if dt.tzinfo is None:return None
        return ((now or utcnow())-dt).total_seconds()
    except (AttributeError,TypeError,ValueError):return None

def temperature_ids(d):
    return model.temperature_entities_of(_as_v2(d))|devices.temperature_entities(heat_source(d))

def quality(e,is_temperature,now):
    value=e.get("state","unavailable")
    normalized=None
    unit=None
    if value in ("unknown","unavailable",""):return (value or "unavailable"),normalized,unit
    if is_temperature:
        try:normalized=float(value)
        except (TypeError,ValueError,OverflowError):return "invalid_number",None,None
        if not math.isfinite(normalized):return "invalid_number",None,None
        if e.get("unit")=="°F":normalized=(normalized-32)*5/9
        elif e.get("unit")!="°C":return "invalid_unit",None,None
        unit="°C"
        if not -60<=normalized<=150:return "implausible",normalized,unit
    elif e.get("entity_id","").startswith("binary_sensor.") and value not in ("on","off"):
        return "invalid_state",None,None
    if not is_temperature:
        # AB-011: contacts and status entities report on change only. Their age says nothing
        # about validity; availability and a valid state are what count.
        return "ok",normalized,unit
    age=age_seconds(e.get("source_at"),now)
    if age is None:return "missing_timestamp",normalized,unit
    if age < -60:return "future_timestamp",normalized,unit
    # AB-027: many sensors report only on change. A long-unchanged value is the current value, not stale.
    # A failed sensor shows up as unavailable/unknown in Home Assistant; our own polling age is checked separately.
    return "ok",normalized,unit

def acquisition_status(now=None):
    if not wanted(load_v2()):return {"last_attempt":None,"last_success":None,"connected":None,"error":"not_configured","stale":True}
    with connect() as c:row=c.execute("SELECT last_attempt,last_success,connected,error FROM acquisition WHERE id=1").fetchone()
    if not row:return {"last_attempt":None,"last_success":None,"connected":None,"error":None,"stale":True}
    attempt,success,connected,error=row
    age=age_seconds(success,now)
    return {"last_attempt":attempt,"last_success":success,"connected":None if connected is None else bool(connected),"error":error,"stale":age is None or age>STALE_SECONDS}

def sample():
    with LOCK:
        d=load_v2()
        desired=wanted(d)
        now=utcnow()
        at=now.isoformat()
        if not desired:
            with connect() as c:c.execute("INSERT INTO acquisition(id,last_attempt,connected,error) VALUES(1,?,NULL,'not_configured') ON CONFLICT(id) DO UPDATE SET last_attempt=excluded.last_attempt,connected=NULL,error='not_configured'",(at,))
            return
        source=heat_source(d)
        rest_only=source.get("source")=="emsesp_rest" and desired<=set(devices.entities(source))
        try:state={} if rest_only else {x["entity_id"]:x for x in get_entities()}
        except Exception:
            with connect() as c:c.execute("INSERT INTO acquisition(id,last_attempt,connected,error) VALUES(1,?,0,'ha_unavailable') ON CONFLICT(id) DO UPDATE SET last_attempt=excluded.last_attempt,connected=0,error='ha_unavailable'",(at,))
            logging.warning("HA-Abfrage fehlgeschlagen (ha_unavailable)")
            return
        if source.get("source")=="emsesp_rest" and devices.profile(source.get("profile")):
            # AB-015: read-only GET on the local EMS-ESP; a failure only affects these fields.
            rest,errors=devices.read_rest(source,at)
            state.update(rest)
            HEAT_SOURCE_STATUS.update(at=at,errors=errors)
            if errors:logging.warning("EMS-ESP-Abfrage unvollständig: %s",errors)
        temps=temperature_ids(d)
        with connect() as c:
            active=c.execute("SELECT id FROM sessions WHERE ended_at IS NULL LIMIT 1").fetchone()
        job=active[0] if active else None   # AB-025: samples of a measuring job are tagged with its id
        vals=[]
        for entity in desired:
            e=state.get(entity,{})
            code,normalized,unit=quality(e,entity in temps,now)
            vals.append((at,entity,e.get("state","unavailable"),e.get("unit",""),int(code=="ok"),e.get("source_at"),code,normalized,unit,job,e.get("source_at_kind")))
        with connect() as c:
            c.executemany("INSERT INTO samples(at,entity_id,state,unit,valid,source_at,quality,normalized_value,normalized_unit,session_id,source_at_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?)",vals)
            c.execute("INSERT INTO acquisition(id,last_attempt,last_success,connected,error) VALUES(1,?,?,1,NULL) ON CONFLICT(id) DO UPDATE SET last_attempt=excluded.last_attempt,last_success=excluded.last_success,connected=1,error=NULL",(at,at))

def latest_observations(now=None):
    now=now or utcnow()
    cfg=load_v2();desired=wanted(cfg);temps=temperature_ids(cfg)
    with connect() as c:rows=c.execute("SELECT entity_id,state,unit,valid,at,source_at,quality,normalized_value,normalized_unit,source_at_kind FROM samples WHERE id IN (SELECT MAX(id) FROM samples GROUP BY entity_id) ORDER BY entity_id").fetchall()
    observations=[]
    for row in rows:
        item=dict(zip(("entity_id","state","unit","valid","at","source_at","quality","normalized_value","normalized_unit","source_at_kind"),row))
        item["source_at_kind"]=item["source_at_kind"] or "unknown"   # samples before review 10
        if item["entity_id"] not in desired:continue
        item["quality"]=item["quality"] or "unchecked"
        item["age_seconds"]=age_seconds(item["source_at"],now)
        sampled_age=age_seconds(item["at"],now)
        item["poll_age_seconds"]=sampled_age
        # AB-027: only our own acquisition can be stale. An old HA report time is a reason to check, not a fault.
        unchanged=item["entity_id"] in temps and item["age_seconds"] is not None
        item["unchanged_long"]=unchanged and item["age_seconds"]>UNCHANGED_HINT_SECONDS
        item["unchanged_check"]=unchanged and item["age_seconds"]>UNCHANGED_CHECK_SECONDS
        item["stale"]=sampled_age is None or sampled_age>STALE_SECONDS
        item["valid"]=bool(item["valid"] and item["quality"]=="ok" and not item["stale"])
        observations.append(item)
    return observations

HEAT_SOURCE_STATUS={"at":None,"errors":{}}

def heat_source_snapshot(now=None):
    """AB-015: latest heat-source values next to the room temperatures (read-only)."""
    cfg=load_v2();source=heat_source(cfg);fields=devices.entities(source)
    obs={o["entity_id"]:o for o in latest_observations(now)}
    def value(o):
        if not o:return None
        return o["normalized_value"] if o.get("normalized_value") is not None else o["state"]
    rooms=[monitoring.room_status(r,obs) for r in cfg["rooms"]]
    items=[]
    for sid,f in fields.items():
        o=obs.get(sid)
        items.append({"key":f["key"],"label":f["label"],"kind":f["kind"],"unit":f["unit"],"entity_id":sid,
            "value":value(o) if o and o["state"] not in ("unavailable","unknown") else None,"valid":bool(o and o["valid"]),"at":o["at"] if o else None})
    prof=devices.profile(source.get("profile"))
    return {"profile":prof["key"] if prof else None,"profile_label":prof["label"] if prof else None,
        "source":source.get("source"),"source_errors":HEAT_SOURCE_STATUS["errors"] if source.get("source")=="emsesp_rest" else {},
        "fields":items,"rooms":rooms,"read_only":True}

AUTOFILL_STATUS={"at":None,"ok_at":None,"identity":None,"ok_identity":None,"changed":[]}

def _profile_values(cfg,now):
    """Currently valid readings of the profile entities: {field key: {entity_id, value}}."""
    obs={o["entity_id"]:o for o in latest_observations(now)}
    values={}
    for sid,f in devices.entities(heat_source(cfg)).items():
        o=obs.get(sid)
        if o and o["valid"] and o["state"] not in ("unavailable","unknown",""):
            values[f["key"]]={"entity_id":sid,"value":o["normalized_value"] if o.get("normalized_value") is not None else o["state"]}
    return values

def autofill_from_source(now=None):
    """AB-029: take over what the read device profile delivers into the configuration (read-only towards the device).

    Writes only when a value really changes (history kind 'autofill'). The acquisition status is reset only
    when the measured entities change (review 12). Returns the labels filled or refreshed in this run."""
    now=now or utcnow()
    with LOCK:
        cfg=load_v2()
        if not devices.profile(heat_source(cfg).get("profile")):return None
        before=json.dumps(cfg,sort_keys=True);previous=json.loads(before)
        values=_profile_values(cfg,now)
        changed=devices.autofill(cfg,values,now.isoformat(),local_date(now))
        identity=list(devices.source_identity(cfg))
        AUTOFILL_STATUS.update(at=now.isoformat(),identity=identity,changed=changed)
        if values:AUTOFILL_STATUS.update(ok_at=now.isoformat(),ok_identity=identity)   # AC14-03: bound to the source
        if json.dumps(cfg,sort_keys=True)==before:return changed
        cfg=model.validate(cfg)
        with connect() as c:
            _write_config(c,cfg)
            c.execute("INSERT INTO config_history(at,kind,value) VALUES(?,?,?)",(now.isoformat(),"autofill",json.dumps(cfg,ensure_ascii=False)))
            if wanted(previous)!=wanted(cfg) or temperature_ids(previous)!=temperature_ids(cfg):
                c.execute("DELETE FROM acquisition")
    if changed:logging.info("AB-029: Anlagendaten übernommen: %s",", ".join(changed))
    return changed

def autofill_report(now=None):
    """Persistent status of every auto field, derived from configuration and current readings."""
    cfg=load_v2()
    hs=heat_source(cfg);identity=list(devices.source_identity(cfg))
    same=lambda key:AUTOFILL_STATUS[key]==identity    # AC14-03: never show a check of another source
    return {"checked_at":AUTOFILL_STATUS["at"] if same("identity") else None,
        "checked_ok_at":AUTOFILL_STATUS["ok_at"] if same("ok_identity") else None,
        "last_changes":AUTOFILL_STATUS["changed"] if same("identity") else [],
        "profile_label":(devices.profile(hs.get("profile")) or {}).get("label"),"source":hs.get("source"),"gateway":devices.gateway(hs),
        "circuit_id":(devices.bound_circuit(cfg) or {}).get("id"),
        "fields":devices.autofill_status(cfg,_profile_values(cfg,now or utcnow()))}

def adopt_autofill(data,now=None):
    """AC13-04: explicit installer action - let the app follow existing values that match the device."""
    now=now or utcnow()
    paths=data.get("paths") if isinstance(data,dict) else None
    if not isinstance(paths,list) or not all(isinstance(x,str) for x in paths) or not paths:raise ValueError("Keine Felder gewählt")
    if data.get("confirmed") is not True:raise ValueError("Übernahme bitte bestätigen")
    with LOCK:
        cfg=load_v2()
        adopted=devices.adopt_existing(cfg,paths,_profile_values(cfg,now),now.isoformat())
        if adopted:
            cfg=model.validate(cfg)
            with connect() as c:
                _write_config(c,cfg)
                c.execute("INSERT INTO config_history(at,kind,value) VALUES(?,?,?)",(now.isoformat(),"autofill_adopt",json.dumps(cfg,ensure_ascii=False)))
    return {"adopted":adopted}

def prepare_source_switch(data):
    """Review 12, P0: on a change of profile, source or circuit no automatic value of the old source may stay.

    Needs ``heat_source.switch_confirmed``; then automatic values are emptied (user values stay) and refilled
    from the new source. Returns the data to save."""
    if not isinstance(data,dict) or not isinstance(data.get("building"),dict):return data
    hs=data["building"].get("heat_source")
    if not isinstance(hs,dict):return data
    previous=load_v2();old=heat_source(previous)
    try:new_identity=devices.source_identity(data)
    except (AttributeError,TypeError,KeyError):new_identity=None
    if new_identity and hs.get("source")=="emsesp_rest" and hs.get("emsesp_host"):
        try:new_identity=new_identity[:2]+(devices.normalise_host(hs["emsesp_host"]),)+new_identity[3:]   # compare canonical host
        except ValueError:pass
    switched=devices.source_identity(previous)!=new_identity
    if not switched:return data
    paths=devices.auto_values_to_clear(previous,data)
    if paths and hs.get("switch_confirmed") is not True:
        raise ValueError("Datenquelle gewechselt: Bitte bestätigen, dass "+str(len(paths))+" automatisch übernommene Werte der bisherigen Quelle entfernt und neu übernommen werden.")
    devices.clear_auto_values(data,paths)
    data["building"]["heat_source"]["autofilled"]={}
    return data

def local_date(now):
    try:return now.astimezone(plant_timezone()).strftime("%d.%m.%Y")
    except Exception:return now.strftime("%d.%m.%Y")

def entity_labels(cfg):
    labels={sid:f["label"] for sid,f in devices.entities(heat_source(cfg)).items()}
    for r in cfg["rooms"]:
        for e in r.get("temperature_sensors",[]):labels[e]="Raum "+r["name"]+" Temperatur"
        for e in r.get("window_sensors",[]):labels[e]="Raum "+r["name"]+" Fenster"
    return labels

def export_csv(hours=168,now=None,start=None,end=None):
    import csv,io
    now=now or utcnow();start=start or (now-timedelta(hours=hours)).isoformat();end=end or now.isoformat()
    cfg=load_v2();desired=wanted(cfg);labels=entity_labels(cfg)
    buf=io.StringIO();w=csv.writer(buf,delimiter=";")
    w.writerow(["zeit_utc","entity_id","bezeichnung","zustand","wert_normiert","einheit","qualitaet"])
    with connect() as c:
        for at,eid,st,unit,q,nv,nu in c.execute("SELECT at,entity_id,state,unit,quality,normalized_value,normalized_unit FROM samples WHERE at>=? AND at<=? ORDER BY at,entity_id",(start,end)):
            if eid in desired:w.writerow([at,eid,labels.get(eid,""),st,"" if nv is None else nv,nu or unit or "",q or "unchecked"])
    return buf.getvalue()

def rooms_status(now=None):
    """AB-013: monitoring status per room, coverage, critical room and reference hints."""
    cfg=load_v2();obs={o["entity_id"]:o for o in latest_observations(now)}
    statuses=[monitoring.room_status(r,obs) for r in cfg["rooms"]]
    return {"rooms":statuses,"coverage":monitoring.coverage_summary(cfg,statuses),
        "critical_room":monitoring.critical_room(statuses),"hints":monitoring.reference_hints(cfg),
        "help":monitoring.REFERENCE_HELP,"disagree_k":monitoring.DISAGREE_K}

def config_history(start=None,end=None):
    """[(at, cfg)] ascending; includes the last version before ``start`` so the state at start is known."""
    with connect() as c:
        rows=c.execute("SELECT at,value FROM config_history ORDER BY at,id").fetchall()
    out=[(at,json.loads(v)) for at,v in rows]
    if start:
        before=[x for x in out if x[0]<=start]
        out=(before[-1:] if before else [])+[x for x in out if x[0]>start]
    if end:out=[x for x in out if x[0]<=end]
    return out

def target_changes(start=None,end=None):
    """AB-013 AC-013-08: comfort-target and design-temperature changes per room with timestamp."""
    changes=[];previous={}
    for at,cfg in config_history(start,end):
        for r in cfg.get("rooms",[]):
            cur=(r.get("target"),r.get("design_indoor_c"))
            old=previous.get(r["id"])
            if old is not None and old!=cur:
                changes.append({"at":at,"room_id":r["id"],"room":r.get("name"),"target_from":old[0],"target_to":cur[0],"design_from":old[1],"design_to":cur[1]})
            previous[r["id"]]=cur
    return changes

SESSION_NAME=re.compile(r"^[^\x00-\x1f\x7f]{1,120}$")  # AB-024: free text (name, order/offer …); output is always escaped
SESSION_COLUMNS="id,name,started_at,ended_at,planned_days,rooms,note,finalized_at,app_version,delete_due_at,archived_at"
def _session_row(r):
    d=dict(zip(SESSION_COLUMNS.split(","),r));d["rooms"]=json.loads(d["rooms"]);d["finalized"]=bool(d["finalized_at"])
    d["auto_delete"]=retention.mobile_kit()
    d["deletion_notice"]=retention.notice(coverage.parse(d["delete_due_at"]),bool(d["archived_at"]),utcnow()) if d["delete_due_at"] and d["auto_delete"] else None
    return d

def list_sessions():
    with connect() as c:rows=c.execute(f"SELECT {SESSION_COLUMNS} FROM sessions ORDER BY started_at DESC").fetchall()
    sessions=[_session_row(r) for r in rows]
    return {"active":next((s for s in sessions if not s["ended_at"]),None),"sessions":sessions}

def get_session(sid):
    with connect() as c:r=c.execute(f"SELECT {SESSION_COLUMNS} FROM sessions WHERE id=?",(sid,)).fetchone()
    return _session_row(r) if r else None

def _session_blob(sid,column):
    with connect() as c:r=c.execute(f"SELECT {column} FROM sessions WHERE id=?",(sid,)).fetchone()
    return r[0] if r else None

def session_history(sid,end=None):
    """[(datetime, cfg)] of one job: its start snapshot plus every saved change during the job.

    A stopped job uses its frozen history, so later configurations (e.g. the next customer)
    can never leak into it (AB-025)."""
    frozen=_session_blob(sid,"history")
    if frozen:return [(coverage.parse(at),cfg) for at,cfg in json.loads(frozen)]
    s=get_session(sid);start=s["started_at"]
    snap=_session_blob(sid,"config_start")
    first=json.loads(snap) if snap else (config_history(start)[:1] or [(start,load_v2())])[0][1]
    later=[(at,cfg) for at,cfg in config_history(start,end) if at>start]
    return [(coverage.parse(start),first)]+[(coverage.parse(at),cfg) for at,cfg in later]

def migrate_sessions():
    """AB-025 migration for jobs created by v0.4.0: tag their samples, store the start snapshot,
    finalize stopped jobs. Jobs never overlap (only one can be active), so time windows are unique."""
    with LOCK:
        with connect() as c:
            rows=c.execute("SELECT id,started_at,ended_at,config_start,finalized_at FROM sessions").fetchall()
        for sid,start,end,snap,final in rows:
            if snap is None:
                cfg=(config_history(start)[:1] or [(start,load_v2())])[0][1]
                with connect() as c:
                    c.execute("UPDATE samples SET session_id=? WHERE session_id IS NULL AND at>=? AND (? IS NULL OR at<=?)",(sid,start,end,end))
                    c.execute("UPDATE sessions SET config_start=? WHERE id=?",(json.dumps(cfg,ensure_ascii=False),sid))
                logging.info("Messung %s auf Auftragstrennung umgestellt",sid)
            if end and not final:
                finalize_session(sid,coverage.parse(end))

def start_session(data,now=None):
    """Start a measuring job. Local bookkeeping only – nothing is sent to HA or the heat pump."""
    if not isinstance(data,dict):raise ValueError("Angaben fehlen")
    name=data.get("name")
    if not isinstance(name,str) or not SESSION_NAME.fullmatch(name.strip()):raise ValueError("Bezeichnung der Messung fehlt (max. 120 Zeichen)")
    days=data.get("planned_days",7)
    if isinstance(days,bool) or not isinstance(days,int) or not 1<=days<=31:raise ValueError("Geplante Dauer 1–31 Tage")
    if data.get("confirmed") is not True:raise ValueError("Beginn der Messung bitte bestätigen")
    note=data.get("note") or ""
    if not isinstance(note,str) or len(note)>1000 or re.search(r"[\x00-\x09\x0b-\x1f\x7f]",note):raise ValueError("Notiz ungültig (max. 1000 Zeichen)")
    with LOCK:
        cfg=load_v2();hints=monitoring.reference_hints(cfg)
        rooms=[{"id":r["id"],"name":r["name"],"target":r.get("target"),"sensors":list(r.get("temperature_sensors",[])),
                "window_sensors":list(r.get("window_sensors",[])),"hints":hints.get(r["id"],[])} for r in cfg["rooms"]]
        if not any(r["sensors"] for r in rooms):raise ValueError("Mindestens ein Raum mit Temperaturfühler ist für einen Messauftrag nötig")
        if list_sessions()["active"]:raise ValueError("Es läuft bereits eine Messung – bitte zuerst stoppen")
        sid="ma_"+uuid.uuid4().hex[:12];at=(now or utcnow()).isoformat()
        with connect() as c:c.execute("INSERT INTO sessions(id,name,started_at,planned_days,rooms,note,config_start,app_version) VALUES(?,?,?,?,?,?,?,?)",
            (sid,name.strip(),at,days,json.dumps(rooms,ensure_ascii=False),note.strip(),json.dumps(cfg,ensure_ascii=False),VERSION))
    log_event("session_start",sid)
    return get_session(sid)

def stop_session(sid,now=None):
    with LOCK:
        s=get_session(sid) if isinstance(sid,str) else None
        if not s:raise ValueError("Messauftrag nicht gefunden")
        if s["ended_at"]:raise ValueError("Messauftrag ist bereits beendet")
        end=now or utcnow()
        with connect() as c:c.execute("UPDATE sessions SET ended_at=?,delete_due_at=? WHERE id=?",(end.isoformat(),retention.due_at(end).isoformat(),sid))
        finalize_session(sid,end)
    log_event("session_stop",sid)
    return get_session(sid)

def finalize_session(sid,end):
    """Freeze history, assessment and report at the end of a job (AB-025, AC6-09). Never rewritten."""
    with LOCK:
        history=session_history(sid,end.isoformat())
        with connect() as c:c.execute("UPDATE sessions SET history=? WHERE id=? AND history IS NULL",
            (json.dumps([(t.isoformat(),cfg) for t,cfg in history],ensure_ascii=False),sid))
        result=session_assessment(sid,end)
        html=session_report(sid,end,frozen_at=end,result=result)
        with connect() as c:c.execute("UPDATE sessions SET assessment=?,report_html=?,finalized_at=?,app_version=COALESCE(app_version,?) WHERE id=? AND finalized_at IS NULL",
            (json.dumps(result,ensure_ascii=False),html,end.isoformat(),VERSION,sid))

# ------------------------------------------------------------------ AB-026 retention and deletion
def _backup_files():
    folder=Path(DB).parent/"backups"
    return sorted(folder.glob("*.db")) if folder.exists() else []

def _compact(path=None):
    """Make deleted rows physically disappear from the database file (WAL and free pages)."""
    c=sqlite3.connect(path or DB,timeout=30)
    try:
        c.execute("PRAGMA secure_delete=ON")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.execute("VACUUM")
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:c.close()

def deleted_info(sid):
    with connect() as c:r=c.execute("SELECT deleted_at,reason FROM deletion_log WHERE session_ref=? ORDER BY id DESC LIMIT 1",(sid,)).fetchone()
    return {"deleted_at":r[0],"reason":r[1]} if r else None

def retention_state():
    with connect() as c:r=c.execute("SELECT last_run,last_ok,errors,register FROM retention_state WHERE id=1").fetchone()
    mode={"mobile_kit":retention.mobile_kit()}
    if not r:return dict(mode,last_run=None,last_ok=None,errors=[],register=None,register_text=None)
    return dict(mode,last_run=r[0],last_ok=r[1],errors=json.loads(r[2] or "[]"),register=r[3],
        register_text=retention.REGISTER_STATUS_TEXT.get(r[3]) if r[3] else None)

def _set_state(**kw):
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO retention_state(id,errors) VALUES(1,'[]')")
        for k,v in kw.items():
            c.execute(f"UPDATE retention_state SET {k}=? WHERE id=1",(json.dumps(v,ensure_ascii=False) if k=="errors" else v,))

def _sync_register(now):
    """Write every locally logged deletion to the register outside /data. Status is persisted."""
    with connect() as c:log=[{"id":r[0],"deleted_at":r[1],"reason":r[2]} for r in c.execute("SELECT session_ref,deleted_at,reason FROM deletion_log ORDER BY id")]
    status=retention.write_register(log,stamp=now.strftime("%Y%m%dT%H%M%SZ")) if log else retention.read_register_status()[1]
    _set_state(register=status)
    if status=="unavailable":logging.warning("Löschregister nicht beschreibbar")
    return status

def _deletion_targets(now):
    """Ids and time windows of jobs that must not survive in any copy on this device."""
    with connect() as c:rows=c.execute("SELECT session_ref,started_at,ended_at FROM deletion_log").fetchall()
    ids={r[0] for r in rows}|{x["id"] for x in retention.read_register()}
    windows=[(r[1],r[2]) for r in rows if r[1] and r[2]]
    return ids,windows

def job_entities(sid):
    """Entity ids configured in one job (start snapshot plus changes). Used to remove its loose samples."""
    try:return set().union(*(wanted(cfg) for _,cfg in session_history(sid))) if get_session(sid) else set()
    except Exception:return set()

def _expired_job_ends(c):
    return [coverage.parse(r[0]) for r in c.execute("SELECT ended_at FROM deletion_log WHERE reason='frist' AND ended_at IS NOT NULL")]

def _last_change(c,tables):
    """Newest configuration timestamp in a database (config history, else config_v2, else samples)."""
    for table,column in (("config_history","at"),("config_v2","updated_at"),("samples","at")):
        if table in tables:
            v=c.execute(f"SELECT MAX({column}) FROM {table}").fetchone()[0]
            if v:return coverage.parse(v)
    return None

def neutralize_config(now=None):
    """Mobile kit only (review 8, P0): remove the customer configuration once it belongs only to expired data.

    Replaces configuration and history by the empty configuration and removes all samples outside jobs and
    the acquisition status. Never touches a running or remaining job. Returns True if something was removed."""
    now=now or utcnow()
    if not retention.mobile_kit():return False
    with LOCK:
        with connect() as c:
            tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            jobs=c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            if not retention.config_expired(_last_change(c,tables),jobs,_expired_job_ends(c),now):return False
            cfg_empty=load_v2()==model.empty()
            loose=c.execute("SELECT COUNT(*) FROM samples WHERE session_id IS NULL").fetchone()[0]
            if cfg_empty and not loose:return False
            empty_cfg=model.empty()
            c.execute("DELETE FROM samples WHERE session_id IS NULL")
            c.execute("DELETE FROM config_history")
            c.execute("DELETE FROM acquisition")
            _write_config(c,empty_cfg)
            c.execute("INSERT INTO config_history(at,kind,value) VALUES(?,?,?)",(now.isoformat(),"neutralized",json.dumps(empty_cfg)))
    log_event("config_neutralized",None)
    logging.info("Messkoffer: Kundenkonfiguration nach Fristablauf entfernt")
    return True

def sanitize_backup(path,now=None,entities=()):
    """Remove deleted or expired jobs and expired loose data from one local app backup (review 7).

    The file is never judged by its modification time. Jobs are matched by id; for backups from
    schemas without ``session_id`` the job windows from the deletion log are used. Returns counts."""
    now=now or utcnow()
    ids,windows=_deletion_targets(now)
    kit=retention.mobile_kit()   # Bastian 27.11.: automatic expiry only on the mobile measuring kit
    limit=(now-timedelta(days=retention.LOOSE_DATA_DAYS)).isoformat()
    c=sqlite3.connect(str(path),timeout=10)
    removed={"jobs":0,"samples":0,"config":False}
    try:
        c.execute("PRAGMA secure_delete=ON")
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "sessions" in tables:
            scols={r[1] for r in c.execute("PRAGMA table_info(sessions)")}
            for sid,st,en,due in c.execute("SELECT id,started_at,ended_at,"+("delete_due_at" if "delete_due_at" in scols else "NULL")+" FROM sessions").fetchall():
                expired=kit and en is not None and (due or retention.due_at(coverage.parse(en)).isoformat())<=now.isoformat()
                if sid in ids or expired:
                    ids.add(sid)
                    if st and en:windows.append((st,en))
            for sid in ids:removed["jobs"]+=c.execute("DELETE FROM sessions WHERE id=?",(sid,)).rowcount
        if "samples" in tables:
            cols={r[1] for r in c.execute("PRAGMA table_info(samples)")}
            if "session_id" in cols:
                for sid in ids:removed["samples"]+=c.execute("DELETE FROM samples WHERE session_id=?",(sid,)).rowcount
                loose="session_id IS NULL AND "
            else:loose=""
            for st,en in windows:removed["samples"]+=c.execute(f"DELETE FROM samples WHERE {loose}at>=? AND at<=?",(st,en)).rowcount
            if kit:removed["samples"]+=c.execute(f"DELETE FROM samples WHERE {loose}at<?",(limit,)).rowcount
            for eid in entities:   # loose samples of the deleted job's sensors (not part of its window)
                removed["samples"]+=c.execute(f"DELETE FROM samples WHERE {loose}entity_id=?",(eid,)).rowcount
        if "config_history" in tables:
            for st,en in windows:c.execute("DELETE FROM config_history WHERE at>=? AND at<=?",(st,en))
            if kit:
                newest=c.execute("SELECT MAX(id) FROM config_history").fetchone()[0]
                c.execute("DELETE FROM config_history WHERE at<? AND id<>?",(limit,newest or -1))
        if kit and "app_events" in tables:c.execute("DELETE FROM app_events WHERE at<?",(limit,))
        if kit:
            with connect() as live:expired_ends=_expired_job_ends(live)
            for st,en in windows:expired_ends.append(coverage.parse(en))   # jobs removed from this copy
            jobs_left=c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] if "sessions" in tables else 0
            if retention.config_expired(_last_change(c,tables),jobs_left,expired_ends,now):
                for table in ("config_v2","config","config_history","acquisition"):
                    if table in tables:c.execute(f"DELETE FROM {table}")
                if "samples" in tables:
                    removed["samples"]+=c.execute("DELETE FROM samples"+(" WHERE session_id IS NULL" if "session_id" in {r[1] for r in c.execute("PRAGMA table_info(samples)")} else "")).rowcount
                removed["config"]=True
        c.commit()
    finally:c.close()
    _compact(str(path))
    return removed

def _sanitize_all_backups(now,errors,entities=()):
    cleaned=0
    for f in _backup_files():
        try:
            r=sanitize_backup(f,now,entities)
            cleaned+=1 if r["jobs"] or r["samples"] or r["config"] else 0
        except Exception:
            logging.exception("Lokale App-Sicherung konnte nicht bereinigt werden")
            errors.append({"code":"backup","text":"Lokale App-Sicherung "+f.name+" konnte nicht bereinigt werden.","at":now.isoformat()})
    return cleaned

def delete_session(sid,reason,now=None,compact=True):
    """Delete one stopped job completely: its samples, snapshots, frozen report, name and note.

    Other jobs are untouched (their samples carry their own session_id). Local app backups are
    cleaned of this job record by record; no backup file is deleted. Returns the log entry."""
    now=now or utcnow()
    with LOCK:
        s=get_session(sid)
        if not s:raise ValueError("Messung nicht gefunden")
        if not s["ended_at"]:raise ValueError("Laufende Messung zuerst stoppen")
        gone=job_entities(sid)-wanted(load_v2())   # sensors of this job the current configuration no longer uses
        with connect() as c:
            n=c.execute("DELETE FROM samples WHERE session_id=?",(sid,)).rowcount
            for eid in gone:n+=c.execute("DELETE FROM samples WHERE session_id IS NULL AND entity_id=?",(eid,)).rowcount
            c.execute("DELETE FROM sessions WHERE id=?",(sid,))
            c.execute("INSERT INTO deletion_log(session_ref,deleted_at,reason,samples_deleted,backups_deleted,started_at,ended_at) VALUES(?,?,?,?,?,?,?)",
                (sid,now.isoformat(),reason,n,0,s["started_at"],s["ended_at"]))
            log_id=c.execute("SELECT last_insert_rowid()").fetchone()[0]
        errors=[]
        cleaned=_sanitize_all_backups(now,errors,gone)
        with connect() as c:c.execute("UPDATE deletion_log SET backups_deleted=? WHERE id=?",(cleaned,log_id))
        register=_sync_register(now)
        if errors:_set_state(errors=retention_state()["errors"]+errors)
        if compact:_compact()
    log_event("session_deleted",sid)
    logging.info("Messung %s gelöscht (%s): %s Messwerte, %s App-Sicherungen bereinigt",sid,reason,n,cleaned)
    return {"session_ref":sid,"deleted_at":now.isoformat(),"reason":reason,"samples_deleted":n,"backups_cleaned":cleaned,"register":register}

class _Skip(Exception):
    pass

def purge(now=None):
    """Delete jobs past their deadline and loose data older than 30 days; clean local app backups.
    Safe to run any time; catches up missed deadlines after downtime (AC-026-05). Errors are kept
    as a persistent status until a later run succeeds (retried hourly)."""
    now=now or utcnow()
    done=[];errors=[]
    kit=retention.mobile_kit()   # Bastian 27.11.: automatic deletion only on the mobile measuring kit
    with LOCK:
        with connect() as c:
            due=[r[0] for r in c.execute("SELECT id FROM sessions WHERE ended_at IS NOT NULL AND delete_due_at<=?",(now.isoformat(),))] if kit else []
        for sid in due:
            try:done.append(delete_session(sid,"frist",now,compact=False))
            except Exception:
                logging.exception("Automatische Löschung fehlgeschlagen")   # no personal data in the log
                errors.append({"code":"job","text":"Eine fällige Messung konnte nicht gelöscht werden. Neuer Versuch in einer Stunde.","at":now.isoformat()})
        limit=(now-timedelta(days=retention.LOOSE_DATA_DAYS)).isoformat()
        loose=0
        try:
            if not kit:raise _Skip()
            with connect() as c:
                loose=c.execute("DELETE FROM samples WHERE session_id IS NULL AND at<?",(limit,)).rowcount
                newest=c.execute("SELECT MAX(id) FROM config_history").fetchone()[0]
                active=c.execute("SELECT MIN(started_at) FROM sessions WHERE ended_at IS NULL").fetchone()[0]
                keep_from=min(limit,active) if active else limit   # a running job still needs its change history
                c.execute("DELETE FROM config_history WHERE at<? AND id<>?",(keep_from,newest or -1))
                c.execute("DELETE FROM app_events WHERE at<?",(limit,))
        except _Skip:pass
        except Exception:
            logging.exception("Bereinigung loser Daten fehlgeschlagen")
            errors.append({"code":"loose","text":"Messwerte außerhalb von Messungen konnten nicht bereinigt werden.","at":now.isoformat()})
        neutralized=False
        try:neutralized=neutralize_config(now)
        except Exception:
            logging.exception("Kundenkonfiguration konnte nicht entfernt werden")
            errors.append({"code":"config","text":"Kundenkonfiguration konnte nach Fristablauf nicht entfernt werden.","at":now.isoformat()})
        cleaned=_sanitize_all_backups(now,errors)
        if done or loose or neutralized:
            try:_compact()
            except Exception:
                logging.exception("Datenbank konnte nicht verdichtet werden")
                errors.append({"code":"compact","text":"Gelöschte Daten konnten nicht physisch entfernt werden (VACUUM).","at":now.isoformat()})
        _sync_register(now)
        _set_state(last_run=now.isoformat(),errors=errors,**({} if errors else {"last_ok":now.isoformat()}))
    return {"jobs":done,"loose_samples":loose,"config_neutralized":neutralized,"backups_cleaned":cleaned,"errors":errors}

def apply_register():
    """After an app-only restore: delete jobs again that were deleted before (register outside /data)."""
    entries,status=retention.read_register_status()
    _set_state(register=status)
    for entry in entries:
        if get_session(entry["id"]):
            try:delete_session(entry["id"],"erneut nach Wiederherstellung")
            except ValueError:logging.warning("Wiederhergestellte Messung läuft noch und wird nicht gelöscht")

def archive_session(sid,now=None):
    """Technician confirms that report and data are stored in the customer file. Does not move the deadline."""
    with LOCK:
        s=get_session(sid) if isinstance(sid,str) else None
        if not s:raise ValueError("Messung nicht gefunden")
        if not s["ended_at"]:raise ValueError("Laufende Messung zuerst stoppen")
        with connect() as c:c.execute("UPDATE sessions SET archived_at=COALESCE(archived_at,?) WHERE id=?",((now or utcnow()).isoformat(),sid))
    return get_session(sid)

class UnarchivedJobs(ValueError):
    def __init__(self,jobs):
        super().__init__("Nicht in der Kundenakte bestätigte Messungen: "+", ".join(j["name"] for j in jobs)+". Löschen ausdrücklich bestätigen.")
        self.jobs=jobs

RESET_WORD="NEUER KUNDE"
def reset_for_new_customer(data,now=None):
    """Prepare the measuring device for the next customer (review 7).

    Deletes every stopped job, configuration, configuration history, passive samples and the local
    app backups. All or nothing: refused while a job runs, and refused without ``allow_unarchived``
    if a job has no confirmed filing in the customer file. Validation happens before anything is deleted."""
    if not isinstance(data,dict) or data.get("confirm")!=RESET_WORD:raise ValueError(f"Zur Bestätigung „{RESET_WORD}“ eingeben")
    now=now or utcnow()
    with LOCK:
        sessions=list_sessions()
        if sessions["active"]:raise ValueError("Laufende Messung zuerst stoppen")
        open_jobs=[{"id":s["id"],"name":s["name"],"ended_at":s["ended_at"]} for s in sessions["sessions"] if not s["archived_at"]]
        if open_jobs and data.get("allow_unarchived") is not True:raise UnarchivedJobs(open_jobs)
        deleted=[delete_session(s["id"],"zurückgesetzt",now,compact=False) for s in sessions["sessions"]]
        empty_cfg=model.empty()
        with connect() as c:
            loose=c.execute("DELETE FROM samples WHERE session_id IS NULL").rowcount
            c.execute("DELETE FROM config_history")
            c.execute("DELETE FROM acquisition")
            _write_config(c,empty_cfg)
            c.execute("INSERT INTO config_history(at,kind,value) VALUES(?,?,?)",(now.isoformat(),"reset",json.dumps(empty_cfg)))
        removed=0
        for f in _backup_files():   # explicitly confirmed by the technician; listed in the reset form
            f.unlink(missing_ok=True);removed+=1
        _compact()
    log_event("reset",None)
    return {"jobs_deleted":len(deleted),"loose_samples":loose,"backups_removed":removed}

def plant_timezone():
    """Local time zone of the plant for the night definition (AB-014). HA passes TZ to apps."""
    name=os.environ.get("TZ") or "Europe/Berlin"
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name),name,True
    except Exception:
        logging.warning("Zeitzone %s nicht verfügbar – MEZ (UTC+1) angenommen",name)
        return timezone(timedelta(hours=1)),"UTC+01:00 (Ersatz)",False

PARAM_KEYS=("curve_design","curve_offset","curve_base","curve_max","room_set","mode","control_mode","min_ext")
def assessment_roles(cfg):
    """Which stored entity plays which role. Heat-source profile first, then manual HA assignment."""
    fields={f["key"]:sid for sid,f in devices.entities(heat_source(cfg)).items()}
    b=cfg["building"]["entities"];c=(cfg.get("circuits") or [{}])[0].get("entities",{})
    pick=lambda *xs:next((x for x in xs if x),None)
    return {"outdoor_damped":fields.get("outdoor_damped"),"outdoor":pick(fields.get("outdoor"),b.get("outdoor")),
        "flow":pick(fields.get("flow"),c.get("flow")),"flow_target":pick(fields.get("flow_target"),c.get("flow_target")),
        "return":pick(fields.get("return"),c.get("return")),"heating_active":fields.get("heating_active"),
        "dhw":pick(fields.get("dhw_charging"),b.get("dhw")),"hp_activity":fields.get("hp_activity"),
        "params":[fields[k] for k in PARAM_KEYS if k in fields]}

def session_assessment(sid,now=None):
    """AB-014: evaluate a measuring job from stored data only (read-only)."""
    s=get_session(sid)
    if not s:return None
    frozen=_session_blob(sid,"assessment")
    if frozen:return json.loads(frozen)
    now=now or utcnow()
    start=coverage.parse(s["started_at"]);end=coverage.parse(s["ended_at"]) if s["ended_at"] else now
    history=session_history(sid,end.isoformat())
    cfg=history[-1][1]
    roles=assessment_roles(cfg)
    ids=set(x for k,x in roles.items() if k!="params" and x)|set(roles["params"])
    for _,h in history:
        for r in h.get("rooms",[]):ids.update(r.get("temperature_sensors",[]));ids.update(r.get("window_sensors",[]))
    values={}
    with connect() as c:
        # AB-025: only samples recorded for this job – never global or other customers' data.
        tick_rows=[r[0] for r in c.execute("SELECT DISTINCT at FROM samples WHERE session_id=? ORDER BY at",(sid,))]
        for at,eid,state,valid,q,nv in c.execute("SELECT at,entity_id,state,valid,quality,normalized_value FROM samples WHERE session_id=?",(sid,)):
            if eid not in ids:continue
            t=coverage.parse(at)
            if t is None:continue
            values.setdefault(t,{})[eid]={"v":nv,"s":state,"ok":bool(valid) and (q in (None,"ok"))}
    ticks=sorted(t for t in (coverage.parse(x) for x in tick_rows) if t)
    tz,tz_name,tz_ok=plant_timezone()
    result=assessment.assess(start,end,ticks,values,history,roles,tz,s["planned_days"],now)
    result["session"]={k:s[k] for k in ("id","name","started_at","ended_at","planned_days")}
    result["timezone"]={"name":tz_name,"exact":tz_ok}
    result["roles"]=roles
    result["texts"]=assessment.report_text(result)
    return result

def session_report(sid,now=None,frozen_at=None,result=None):
    """AB-020 AC-06 / AB-025: report of one job (HTML). A stopped job returns its frozen report."""
    s=get_session(sid)
    if not s:return None
    if frozen_at is None:
        stored=_session_blob(sid,"report_html")
        if stored:return stored
    now=now or utcnow()
    result=result or session_assessment(sid,now)
    end=s["ended_at"] or now.isoformat()
    cfg=session_history(sid,end)[-1][1]
    with connect() as c:
        ticks=[coverage.parse(r[0]) for r in c.execute("SELECT DISTINCT at FROM samples WHERE session_id=? ORDER BY at",(sid,))]
        events=[{"at":a,"kind":k} for a,k in c.execute("SELECT at,kind FROM app_events WHERE at>=? AND at<=? ORDER BY at",(s["started_at"],end))]
        fields=devices.entities(heat_source(cfg));params=[]
        for sid_,f in fields.items():
            if f["kind"]!="setting":continue
            # AB-025: last value recorded *within this job*; nothing older, nothing from another job.
            row=c.execute("SELECT state FROM samples WHERE session_id=? AND entity_id=? ORDER BY at DESC LIMIT 1",(sid,sid_)).fetchone()
            ok=row and row[0] not in ("unavailable","unknown","")
            params.append((f["label"],row[0] if ok else "im Auftrag nicht gemessen",f["unit"] if ok else ""))
    start_dt=coverage.parse(s["started_at"]);end_dt=coverage.parse(end);ticks=[t for t in ticks if t]
    def gap(a,b,ongoing=False):return {"start":a,"end":b,"ongoing":ongoing,"minutes":round((b-a).total_seconds()/60)}
    if not ticks:
        gaps=[gap(start_dt,end_dt,not s["ended_at"])] if (end_dt-start_dt).total_seconds()>coverage.WARN_SECONDS else []
    else:
        # A finished job ends at its end time: a missing tail is an ordinary gap, not an ongoing one.
        gaps=coverage.find_gaps(ticks if not s["ended_at"] else ticks+[end_dt],end_dt,start_dt)
        if (ticks[0]-start_dt).total_seconds()>coverage.WARN_SECONDS:gaps.insert(0,gap(start_dt,ticks[0]))
    gaps=[dict(g,start=g["start"].isoformat(),end=g["end"].isoformat()) for g in gaps]
    tz,tz_name,_=plant_timezone()
    return report.render(s,result,gaps,events,params,entity_labels(cfg),tz,tz_name,s.get("app_version") or VERSION,now,frozen_at=frozen_at)

def session_csv(sid):
    """CSV of one job: only its own samples, labelled with its own configuration (AB-025)."""
    import csv,io
    s=get_session(sid)
    if not s:return None
    cfg=session_history(sid,s["ended_at"])[-1][1];labels=entity_labels(cfg)
    buf=io.StringIO();w=csv.writer(buf,delimiter=";")
    w.writerow(["zeit_utc","entity_id","bezeichnung","zustand","wert_normiert","einheit","qualitaet"])
    with connect() as c:
        for at,eid,st,unit,q,nv,nu in c.execute("SELECT at,entity_id,state,unit,quality,normalized_value,normalized_unit FROM samples WHERE session_id=? ORDER BY at,entity_id",(sid,)):
            w.writerow([at,eid,labels.get(eid,""),st,"" if nv is None else nv,nu or unit or "",q or "unchecked"])
    return buf.getvalue()

def log_event(kind,detail=None):
    with connect() as c:c.execute("INSERT INTO app_events(at,kind,detail) VALUES(?,?,?)",(utcnow().isoformat(),kind,detail))

def data_quality(hours=168,now=None):
    """AB-020 report: gaps, coverage per entity, restarts. Read-only on the local DB."""
    now=now or utcnow()
    start=(now-timedelta(hours=hours)).isoformat()
    desired=wanted(load_v2())
    with connect() as c:
        before=c.execute("SELECT MAX(at) FROM samples WHERE at<?",(start,)).fetchone()[0]
        ticks=[r[0] for r in c.execute("SELECT DISTINCT at FROM samples WHERE at>=? ORDER BY at",(start,))]
        rows=[r for r in c.execute("SELECT entity_id,MIN(at),COUNT(*),SUM(valid) FROM samples WHERE at>=? GROUP BY entity_id ORDER BY entity_id",(start,)) if r[0] in desired]
        events=[{"at":a,"kind":k} for a,k in c.execute("SELECT at,kind FROM app_events WHERE at>=? ORDER BY at",(start,))]
        attempt=c.execute("SELECT last_attempt FROM acquisition WHERE id=1").fetchone()
    report=coverage.analyse(([before] if before else [])+ticks,rows,attempt[0] if attempt else None,now,hours)
    report["configured"]=bool(desired)
    if not desired:report["status"]="not_configured"
    report["events"]=events
    seen={r[0] for r in rows}
    report["entities_without_samples"]=sorted(desired-seen)
    return report

def acquisition_health(now=None):
    """Compact status for /health and external monitors (no database writes)."""
    try:
        r=data_quality(hours=24,now=now)
        return {"status":r["status"],"last_tick":r["last_tick"],"last_tick_age_seconds":r["last_tick_age_seconds"],"loop_stalled":r["loop_stalled"]}
    except Exception:
        logging.exception("Datenqualität nicht ermittelbar")
        return {"status":"unknown","last_tick":None,"last_tick_age_seconds":None,"loop_stalled":None}

STOP=threading.Event()
def loop(interval=300):
    last_purge=time.monotonic()
    while not STOP.is_set():
        try:sample()
        except Exception:logging.exception("Lokale Messwerterfassung fehlgeschlagen")
        try:autofill_from_source()          # AB-029: also refreshes values the app filled itself
        except Exception:logging.exception("Anlagendaten konnten nicht übernommen werden")
        if time.monotonic()-last_purge>=retention.PURGE_INTERVAL_SECONDS:
            last_purge=time.monotonic()
            try:purge()
            except Exception:logging.exception("Löschlauf fehlgeschlagen")
        STOP.wait(interval)  # AB-010: wakes immediately on shutdown
class API(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args):logging.info(fmt,*args)
    def send_json(self,code,obj):
        body=json.dumps(obj,ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Cache-Control","no-store")
        self.send_header("Content-Length",str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        path=urlsplit(self.path).path
        if path in ("/","/index.html"):
            body=(ROOT/"static"/"index.html").read_text().replace("__APP_VERSION__",VERSION).encode()
            self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if path=="/jodeit-logo.png":
            body=(ROOT/"static"/"jodeit-logo.png").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type","image/png")
            self.send_header("Cache-Control","max-age=86400")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if path=="/health":return self.send_json(200,{"ok":True,"read_only":True,"version":VERSION,"schema_version":model.SCHEMA_VERSION,"acquisition":acquisition_health()})
        if path=="/health/acquisition":
            # AB-020: for an external monitor. 503 only when the app's own acquisition loop is stuck
            # (no attempt for > 60 min); an unreachable HA is reported in /health, not here.
            h=acquisition_health()
            return self.send_json(503 if h["loop_stalled"] and h["status"]!="not_configured" else 200,h)
        if path=="/api/v2/data-quality":
            query=dict(p.split("=",1) for p in urlsplit(self.path).query.split("&") if "=" in p)
            try:hours=int(query.get("hours","168"))
            except ValueError:return self.send_json(400,{"error":"hours ungültig"})
            if not 1<=hours<=24*31:return self.send_json(400,{"error":"hours ungültig"})
            return self.send_json(200,data_quality(hours))
        if path=="/api/config":return self.send_json(200,load())
        if path=="/api/v2/config":return self.send_json(200,load_v2())
        if path=="/api/v2/readiness":
            cfg=load_v2();result=model.readiness(cfg)
            result["calculation_blockers"]=model.calculation_blockers(cfg)  # AB-008 format, kept for compatibility
            return self.send_json(200,result)
        if path=="/api/v2/catalog":return self.send_json(200,{"valves":model.VALVE_CATALOG})
        if path=="/api/v2/heat-source/detect":
            # AB-022: read-only match of existing HA entities against the profiles; stores nothing.
            try:ha={x["entity_id"]:x for x in get_entities()}
            except Exception:return self.send_json(503,{"error":"Home Assistant derzeit nicht erreichbar"})
            return self.send_json(200,devices.detect(ha))
        if path=="/api/v2/heat-source/profiles":
            return self.send_json(200,{"sources":devices.SOURCES,"write_enabled":devices.WRITE_ENABLED,
                "profiles":[{"key":p["key"],"label":p["label"],"verified":p["verified"],"fields":len(p["fields"])} for p in devices.PROFILES.values()]})
        if path=="/api/v2/heat-source/latest":return self.send_json(200,heat_source_snapshot())
        if path=="/api/v2/heat-source/autofill":return self.send_json(200,autofill_report())
        if path=="/api/v2/rooms/status":return self.send_json(200,rooms_status())
        if path=="/api/v2/sessions":return self.send_json(200,dict(list_sessions(),retention=retention_state()))
        if path.startswith("/api/v2/sessions/") and path.count("/")==5:
            gone=deleted_info(path.split("/")[4])
            if gone and not get_session(path.split("/")[4]):
                return self.send_json(410,{"error":"Messung wurde am "+gone["deleted_at"][:10]+" gelöscht","deleted":gone})
        if path=="/api/v2/retention":
            with connect() as c:log=[dict(zip(("session_ref","deleted_at","reason","samples_deleted","backups_cleaned"),r)) for r in c.execute("SELECT session_ref,deleted_at,reason,samples_deleted,backups_deleted FROM deletion_log ORDER BY id DESC LIMIT 50")]
            return self.send_json(200,{"retention_days":retention.RETENTION_DAYS,"warn_days":list(retention.WARN_DAYS),"log":log,"register":str(retention.REGISTER_DIR),"status":retention_state()})
        if path.startswith("/api/v2/sessions/") and path.endswith("/report"):
            body=session_report(path.split("/")[4])
            if body is None:return self.send_json(404,{"error":"Messauftrag nicht gefunden"})
            body=body.encode()
            self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Cache-Control","no-store")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if path.startswith("/api/v2/sessions/") and path.endswith("/export.csv"):
            s=get_session(path.split("/")[4])
            if not s:return self.send_json(404,{"error":"Messauftrag nicht gefunden"})
            body=("\ufeff"+session_csv(s["id"])).encode()
            self.send_response(200)
            self.send_header("Content-Type","text/csv; charset=utf-8")
            self.send_header("Content-Disposition","attachment; filename=autobalance-"+s["id"]+".csv")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if path.startswith("/api/v2/sessions/") and path.endswith("/assessment"):
            result=session_assessment(path.split("/")[4])
            return self.send_json(200,result) if result else self.send_json(404,{"error":"Messauftrag nicht gefunden"})
        if path=="/api/v2/target-changes":return self.send_json(200,{"changes":target_changes()})
        if path=="/api/v2/export.csv":
            body=("\ufeff"+export_csv()).encode()
            self.send_response(200)
            self.send_header("Content-Type","text/csv; charset=utf-8")
            self.send_header("Content-Disposition","attachment; filename=autobalance-messwerte.csv")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if path=="/api/entities":
            try:return self.send_json(200,{"connected":True,"entities":get_entities()})
            except Exception:return self.send_json(503,{"connected":False,"entities":[],"error":"Home Assistant derzeit nicht erreichbar"})
        if path=="/api/overview":
            with connect() as c:row=c.execute("SELECT COUNT(*),MAX(at) FROM samples").fetchone()
            d=load_v2()
            return self.send_json(200,{"rooms":len(d["rooms"]),"zones":len(d["zones"]),"samples":row[0],"last_sample":row[1],"version":VERSION,"acquisition":acquisition_status(),"last_sample_age_seconds":age_seconds(row[1])})
        if path=="/api/latest":return self.send_json(200,{"observations":latest_observations(),"acquisition":acquisition_status()})
        return self.send_json(404,{"error":"Nicht gefunden"})
    def do_POST(self):
        path=urlsplit(self.path).path
        if path not in ("/api/config","/api/v2/config","/api/v2/heat-source/autofill/adopt","/api/v2/load-share-suggestion","/api/v2/heat-source/test","/api/v2/sessions/start","/api/v2/sessions/stop","/api/v2/sessions/archive","/api/v2/sessions/delete","/api/v2/reset"):return self.send_json(404,{"error":"Nicht gefunden"})
        limit=100000 if path=="/api/config" else 500000
        try:
            size=int(self.headers.get("Content-Length","0"))
            if not 1<=size<=limit:return self.send_json(413,{"error":"Ungültige Datenlänge"})
            data=json.loads(self.rfile.read(size))
            if path=="/api/v2/heat-source/autofill/adopt":return self.send_json(200,adopt_autofill(data))
            if path=="/api/v2/sessions/start":return self.send_json(200,start_session(data))
            if path=="/api/v2/sessions/stop":return self.send_json(200,stop_session(data.get("id") if isinstance(data,dict) else None))
            if path=="/api/v2/sessions/archive":return self.send_json(200,archive_session(data.get("id") if isinstance(data,dict) else None))
            if path=="/api/v2/sessions/delete":
                s=get_session(data.get("id")) if isinstance(data,dict) and isinstance(data.get("id"),str) else None
                if not s:raise ValueError("Messung nicht gefunden")
                if data.get("confirm")!=s["name"]:raise ValueError("Zur Bestätigung die Bezeichnung der Messung genau eingeben")
                return self.send_json(200,delete_session(s["id"],"manuell"))
            if path=="/api/v2/reset":
                try:return self.send_json(200,reset_for_new_customer(data))
                except UnarchivedJobs as ex:return self.send_json(409,{"error":str(ex),"unarchived":ex.jobs})
            if path=="/api/v2/heat-source/test":
                # Read-only probe of the unsaved selection; stores nothing, sends nothing to the device.
                if not isinstance(data,dict):raise ValueError("Datenquelle fehlt")
                ha={}
                if (data.get("source") or "ha")=="ha":
                    try:ha={x["entity_id"]:x for x in get_entities()}
                    except Exception:return self.send_json(503,{"error":"Home Assistant derzeit nicht erreichbar"})
                return self.send_json(200,devices.probe(data,ha,utcnow().isoformat()))
            if path=="/api/v2/load-share-suggestion":
                # Works on the unsaved editor state; validates structure but stores nothing.
                if not isinstance(data,dict) or not isinstance(data.get("room_id"),str):raise ValueError("room_id fehlt")
                shares,reason=model.suggest_load_shares(model.validate_structure(data.get("config")),data["room_id"])
                return self.send_json(200,{"shares":shares,"reason":reason})
            if path=="/api/config":save_v1(data)
            else:save_v2(prepare_source_switch(data))
            report=None
            if path=="/api/v2/config" and devices.profile(heat_source(load_v2()).get("profile")):
                try:
                    sample()                       # read the profile values now, not in up to 5 minutes
                    autofill_from_source()         # AB-029: take over what the device delivers
                    report=autofill_report()
                except Exception:logging.exception("Anlagendaten konnten nicht übernommen werden")
            return self.send_json(200,{"saved":True,"autofill":report})
        except (ValueError,TypeError,KeyError,OverflowError,AttributeError) as exc:return self.send_json(400,{"error":str(exc)})
def main(port=None,install_signals=True,ready=None):
    """Run acquisition and HTTP API; return 0 after an orderly SIGTERM/SIGINT shutdown (AB-010)."""
    import signal
    setup()
    log_event("start",VERSION)
    STOP.clear()
    httpd=ThreadingHTTPServer(("0.0.0.0",PORT if port is None else port),API)
    worker=threading.Thread(target=loop,daemon=True,name="acquisition")
    def stop(signum=None,frame=None):
        if STOP.is_set():return
        logging.info("Beenden angefordert (Signal %s)",signum)
        STOP.set()
        threading.Thread(target=httpd.shutdown,daemon=True).start()  # shutdown() blocks; never call it on the serving thread
    if install_signals:
        for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,stop)
    worker.start()
    if ready is not None:ready(httpd,stop)
    try:httpd.serve_forever()
    finally:
        httpd.server_close()
        worker.join(timeout=10)  # an in-flight HA request may take up to its own timeout
        try:log_event("stop")
        except Exception:logging.exception("Stopp-Ereignis nicht gespeichert")
        logging.info("AutoBalance beendet")
    return 0

if __name__=="__main__":
    sys.exit(main())
