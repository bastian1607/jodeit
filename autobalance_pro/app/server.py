"""AutoBalance Pro – read-only Home Assistant pilot."""
import json,sys,logging,math,os,re,sqlite3,threading,time,urllib.request
from version import VERSION
import model,coverage,devices,monitoring,assessment,uuid
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
STALE_SECONDS=1800
ENTITY_PATTERN=re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
def connect():
    c=sqlite3.connect(DB,timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    return c
def setup():
    Path(DB).parent.mkdir(parents=True,exist_ok=True)
    with connect() as c:
        c.execute("CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY, at TEXT, entity_id TEXT, state TEXT, unit TEXT, valid INTEGER)")
        c.execute("CREATE INDEX IF NOT EXISTS samples_at ON samples(at)")
        # Additive migration: original raw values and existing rows remain intact.
        columns={row[1] for row in c.execute("PRAGMA table_info(samples)")}
        for name,kind in (("source_at","TEXT"),("quality","TEXT"),("normalized_value","REAL"),("normalized_unit","TEXT")):
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
    sync_config()
    with LOCK, connect() as c:
        if not c.execute("SELECT 1 FROM config_history LIMIT 1").fetchone():
            # Baseline for installations before AB-013: valid since the first stored sample.
            first=c.execute("SELECT MIN(at) FROM samples").fetchone()[0]
            c.execute("INSERT INTO config_history(at,kind,value) VALUES(?,?,?)",(first or utcnow().isoformat(),"baseline",json.dumps(load_v2(),ensure_ascii=False)))

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
    return [{"entity_id":e.get("entity_id",""),"name":e.get("attributes",{}).get("friendly_name",e.get("entity_id","")),"state":e.get("state",""),"unit":e.get("attributes",{}).get("unit_of_measurement",""),"device_class":e.get("attributes",{}).get("device_class",""),"source_at":e.get("last_reported") or e.get("last_updated")} for e in items if isinstance(e,dict)]
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
    if age > STALE_SECONDS:return "stale",normalized,unit
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
        vals=[]
        for entity in desired:
            e=state.get(entity,{})
            code,normalized,unit=quality(e,entity in temps,now)
            vals.append((at,entity,e.get("state","unavailable"),e.get("unit",""),int(code=="ok"),e.get("source_at"),code,normalized,unit))
        with connect() as c:
            c.executemany("INSERT INTO samples(at,entity_id,state,unit,valid,source_at,quality,normalized_value,normalized_unit) VALUES (?,?,?,?,?,?,?,?,?)",vals)
            c.execute("INSERT INTO acquisition(id,last_attempt,last_success,connected,error) VALUES(1,?,?,1,NULL) ON CONFLICT(id) DO UPDATE SET last_attempt=excluded.last_attempt,last_success=excluded.last_success,connected=1,error=NULL",(at,at))

def latest_observations(now=None):
    now=now or utcnow()
    cfg=load_v2();desired=wanted(cfg);temps=temperature_ids(cfg)
    with connect() as c:rows=c.execute("SELECT entity_id,state,unit,valid,at,source_at,quality,normalized_value,normalized_unit FROM samples WHERE id IN (SELECT MAX(id) FROM samples GROUP BY entity_id) ORDER BY entity_id").fetchall()
    observations=[]
    for row in rows:
        item=dict(zip(("entity_id","state","unit","valid","at","source_at","quality","normalized_value","normalized_unit"),row))
        if item["entity_id"] not in desired:continue
        item["quality"]=item["quality"] or "unchecked"
        item["age_seconds"]=age_seconds(item["source_at"],now)
        sampled_age=age_seconds(item["at"],now)
        report_stale=item["entity_id"] in temps and (item["age_seconds"] is None or item["age_seconds"]>STALE_SECONDS)
        item["stale"]=report_stale or sampled_age is None or sampled_age>STALE_SECONDS  # own acquisition must be fresh for every entity
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

def export_csv(hours=168,now=None):
    import csv,io
    now=now or utcnow();start=(now-timedelta(hours=hours)).isoformat()
    cfg=load_v2();desired=wanted(cfg);labels={sid:f["label"] for sid,f in devices.entities(heat_source(cfg)).items()}
    for r in cfg["rooms"]:
        for e in r.get("temperature_sensors",[]):labels[e]="Raum "+r["name"]+" Temperatur"
        for e in r.get("window_sensors",[]):labels[e]="Raum "+r["name"]+" Fenster"
    buf=io.StringIO();w=csv.writer(buf,delimiter=";")
    w.writerow(["zeit_utc","entity_id","bezeichnung","zustand","wert_normiert","einheit","qualitaet"])
    with connect() as c:
        for at,eid,st,unit,q,nv,nu in c.execute("SELECT at,entity_id,state,unit,quality,normalized_value,normalized_unit FROM samples WHERE at>=? ORDER BY at,entity_id",(start,)):
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

SESSION_NAME=re.compile(r"^[^<>]{1,80}$")
def _session_row(r):
    keys=("id","name","started_at","ended_at","planned_days","rooms","note")
    d=dict(zip(keys,r));d["rooms"]=json.loads(d["rooms"]);return d

def list_sessions():
    with connect() as c:rows=c.execute("SELECT id,name,started_at,ended_at,planned_days,rooms,note FROM sessions ORDER BY started_at DESC").fetchall()
    sessions=[_session_row(r) for r in rows]
    return {"active":next((s for s in sessions if not s["ended_at"]),None),"sessions":sessions}

def get_session(sid):
    with connect() as c:r=c.execute("SELECT id,name,started_at,ended_at,planned_days,rooms,note FROM sessions WHERE id=?",(sid,)).fetchone()
    return _session_row(r) if r else None

def start_session(data,now=None):
    """Start a measuring job. Local bookkeeping only – nothing is sent to HA or the heat pump."""
    if not isinstance(data,dict):raise ValueError("Angaben fehlen")
    name=data.get("name")
    if not isinstance(name,str) or not SESSION_NAME.fullmatch(name.strip()):raise ValueError("Bezeichnung des Messauftrags fehlt (max. 80 Zeichen)")
    days=data.get("planned_days",7)
    if isinstance(days,bool) or not isinstance(days,int) or not 1<=days<=31:raise ValueError("Geplante Dauer 1–31 Tage")
    if data.get("confirmed") is not True:raise ValueError("Auswahl der Messräume bitte bestätigen")
    note=data.get("note") or ""
    if not isinstance(note,str) or len(note)>500:raise ValueError("Notiz ungültig")
    with LOCK:
        cfg=load_v2();hints=monitoring.reference_hints(cfg)
        rooms=[{"id":r["id"],"name":r["name"],"target":r.get("target"),"sensors":list(r.get("temperature_sensors",[])),
                "window_sensors":list(r.get("window_sensors",[])),"hints":hints.get(r["id"],[])} for r in cfg["rooms"]]
        if not any(r["sensors"] for r in rooms):raise ValueError("Mindestens ein Raum mit Temperaturfühler ist für einen Messauftrag nötig")
        if list_sessions()["active"]:raise ValueError("Es läuft bereits ein Messauftrag")
        sid="ma_"+uuid.uuid4().hex[:12];at=(now or utcnow()).isoformat()
        with connect() as c:c.execute("INSERT INTO sessions(id,name,started_at,planned_days,rooms,note) VALUES(?,?,?,?,?,?)",(sid,name.strip(),at,days,json.dumps(rooms,ensure_ascii=False),note.strip()))
    log_event("session_start",sid)
    return get_session(sid)

def stop_session(sid,now=None):
    with LOCK:
        s=get_session(sid) if isinstance(sid,str) else None
        if not s:raise ValueError("Messauftrag nicht gefunden")
        if s["ended_at"]:raise ValueError("Messauftrag ist bereits beendet")
        with connect() as c:c.execute("UPDATE sessions SET ended_at=? WHERE id=?",((now or utcnow()).isoformat(),sid))
    log_event("session_stop",sid)
    return get_session(sid)

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
    now=now or utcnow()
    start=coverage.parse(s["started_at"]);end=coverage.parse(s["ended_at"]) if s["ended_at"] else now
    history=[(coverage.parse(at),cfg) for at,cfg in config_history(s["started_at"],end.isoformat())]
    cfg=history[-1][1] if history else load_v2()
    roles=assessment_roles(cfg)
    ids=set(x for k,x in roles.items() if k!="params" and x)|set(roles["params"])
    for _,h in history:
        for r in h.get("rooms",[]):ids.update(r.get("temperature_sensors",[]));ids.update(r.get("window_sensors",[]))
    values={}
    with connect() as c:
        tick_rows=[r[0] for r in c.execute("SELECT DISTINCT at FROM samples WHERE at>=? AND at<=? ORDER BY at",(s["started_at"],end.isoformat()))]
        for at,eid,state,valid,q,nv in c.execute("SELECT at,entity_id,state,valid,quality,normalized_value FROM samples WHERE at>=? AND at<=?",(s["started_at"],end.isoformat())):
            if eid not in ids:continue
            t=coverage.parse(at)
            if t is None:continue
            values.setdefault(t,{})[eid]={"v":nv,"s":state,"ok":bool(valid) and (q in (None,"ok"))}
    ticks=sorted(t for t in (coverage.parse(x) for x in tick_rows) if t)
    tz,tz_name,tz_ok=plant_timezone()
    result=assessment.assess(start,end,ticks,values,history or [(start,cfg)],roles,tz,s["planned_days"],now)
    result["session"]={k:s[k] for k in ("id","name","started_at","ended_at","planned_days")}
    result["timezone"]={"name":tz_name,"exact":tz_ok}
    result["roles"]=roles
    result["texts"]=assessment.report_text(result)
    return result

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
    while not STOP.is_set():
        try:sample()
        except Exception:logging.exception("Lokale Messwerterfassung fehlgeschlagen")
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
        if path=="/api/v2/heat-source/profiles":
            return self.send_json(200,{"sources":devices.SOURCES,"write_enabled":devices.WRITE_ENABLED,
                "profiles":[{"key":p["key"],"label":p["label"],"verified":p["verified"],"fields":len(p["fields"])} for p in devices.PROFILES.values()]})
        if path=="/api/v2/heat-source/latest":return self.send_json(200,heat_source_snapshot())
        if path=="/api/v2/rooms/status":return self.send_json(200,rooms_status())
        if path=="/api/v2/sessions":return self.send_json(200,list_sessions())
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
        if path not in ("/api/config","/api/v2/config","/api/v2/load-share-suggestion","/api/v2/heat-source/test","/api/v2/sessions/start","/api/v2/sessions/stop"):return self.send_json(404,{"error":"Nicht gefunden"})
        limit=100000 if path=="/api/config" else 500000
        try:
            size=int(self.headers.get("Content-Length","0"))
            if not 1<=size<=limit:return self.send_json(413,{"error":"Ungültige Datenlänge"})
            data=json.loads(self.rfile.read(size))
            if path=="/api/v2/sessions/start":return self.send_json(200,start_session(data))
            if path=="/api/v2/sessions/stop":return self.send_json(200,stop_session(data.get("id") if isinstance(data,dict) else None))
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
            else:save_v2(data)
            return self.send_json(200,{"saved":True})
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
