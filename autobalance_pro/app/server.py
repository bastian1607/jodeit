"""AutoBalance Pro – read-only Home Assistant pilot."""
import json,logging,math,os,re,sqlite3,threading,time,urllib.request
from version import VERSION
from datetime import datetime,timezone
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
def empty():
    return {"rooms":[],"zones":[],"heating":{"manufacturer":"","model":"","circuit_name":"Heizkreis 1","outdoor":"","flow":"","return":"","compressor":"","dhw":""}}
def load():
    with connect() as c:r=c.execute("SELECT value FROM config WHERE id=1").fetchone()
    return json.loads(r[0]) if r else empty()
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
        if not isinstance(r.get("temperature_sensors"),list) or not r["temperature_sensors"]:raise ValueError("Mindestens ein Temperatursensor pro Raum")
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
def wanted(d):
    ids=set()
    for r in d["rooms"]:
        ids.update(r.get("temperature_sensors",[]))
        ids.update(r.get("window_sensors",[]))
        ids.update(v["entity"] for v in r.get("radiators",[]) if v.get("entity"))
    ids.update(d["heating"].get(key,"") for key in ("outdoor","flow","return","compressor","dhw"))
    ids.discard("")
    ids.discard(None)
    return ids
def utcnow():
    return datetime.now(timezone.utc)

def age_seconds(timestamp,now=None):
    try:
        dt=datetime.fromisoformat(timestamp.replace("Z","+00:00"))
        if dt.tzinfo is None:return None
        return ((now or utcnow())-dt).total_seconds()
    except (AttributeError,TypeError,ValueError):return None

def temperature_ids(d):
    return {v for r in d["rooms"] for v in r["temperature_sensors"]} | {d["heating"].get(k) for k in ("outdoor","flow","return")} - {None,""}

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
    age=age_seconds(e.get("source_at"),now)
    if age is None:return "missing_timestamp",normalized,unit
    if age < -60:return "future_timestamp",normalized,unit
    if age > STALE_SECONDS:return "stale",normalized,unit
    return "ok",normalized,unit

def acquisition_status(now=None):
    if not wanted(load()):return {"last_attempt":None,"last_success":None,"connected":None,"error":"not_configured","stale":True}
    with connect() as c:row=c.execute("SELECT last_attempt,last_success,connected,error FROM acquisition WHERE id=1").fetchone()
    if not row:return {"last_attempt":None,"last_success":None,"connected":None,"error":None,"stale":True}
    attempt,success,connected,error=row
    age=age_seconds(success,now)
    return {"last_attempt":attempt,"last_success":success,"connected":None if connected is None else bool(connected),"error":error,"stale":age is None or age>STALE_SECONDS}

def sample():
    with LOCK:
        d=load()
        desired=wanted(d)
        now=utcnow()
        at=now.isoformat()
        if not desired:
            with connect() as c:c.execute("INSERT INTO acquisition(id,last_attempt,connected,error) VALUES(1,?,NULL,'not_configured') ON CONFLICT(id) DO UPDATE SET last_attempt=excluded.last_attempt,connected=NULL,error='not_configured'",(at,))
            return
        try:state={x["entity_id"]:x for x in get_entities()}
        except Exception:
            with connect() as c:c.execute("INSERT INTO acquisition(id,last_attempt,connected,error) VALUES(1,?,0,'ha_unavailable') ON CONFLICT(id) DO UPDATE SET last_attempt=excluded.last_attempt,connected=0,error='ha_unavailable'",(at,))
            logging.warning("HA-Abfrage fehlgeschlagen (ha_unavailable)")
            return
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
    desired=wanted(load())
    with connect() as c:rows=c.execute("SELECT entity_id,state,unit,valid,at,source_at,quality,normalized_value,normalized_unit FROM samples WHERE id IN (SELECT MAX(id) FROM samples GROUP BY entity_id) ORDER BY entity_id").fetchall()
    observations=[]
    for row in rows:
        item=dict(zip(("entity_id","state","unit","valid","at","source_at","quality","normalized_value","normalized_unit"),row))
        if item["entity_id"] not in desired:continue
        item["quality"]=item["quality"] or "unchecked"
        item["age_seconds"]=age_seconds(item["source_at"],now)
        sampled_age=age_seconds(item["at"],now)
        item["stale"]=item["age_seconds"] is None or item["age_seconds"]>STALE_SECONDS or sampled_age is None or sampled_age>STALE_SECONDS
        item["valid"]=bool(item["valid"] and item["quality"]=="ok" and not item["stale"])
        observations.append(item)
    return observations

def loop():
    while True:
        try:sample()
        except Exception:logging.exception("Lokale Messwerterfassung fehlgeschlagen")
        time.sleep(300)
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
        if path=="/health":return self.send_json(200,{"ok":True,"read_only":True,"version":VERSION})
        if path=="/api/config":return self.send_json(200,load())
        if path=="/api/entities":
            try:return self.send_json(200,{"connected":True,"entities":get_entities()})
            except Exception:return self.send_json(503,{"connected":False,"entities":[],"error":"Home Assistant derzeit nicht erreichbar"})
        if path=="/api/overview":
            with connect() as c:row=c.execute("SELECT COUNT(*),MAX(at) FROM samples").fetchone()
            d=load()
            return self.send_json(200,{"rooms":len(d["rooms"]),"zones":len(d["zones"]),"samples":row[0],"last_sample":row[1],"version":VERSION,"acquisition":acquisition_status(),"last_sample_age_seconds":age_seconds(row[1])})
        if path=="/api/latest":return self.send_json(200,{"observations":latest_observations(),"acquisition":acquisition_status()})
        return self.send_json(404,{"error":"Nicht gefunden"})
    def do_POST(self):
        if urlsplit(self.path).path!="/api/config":return self.send_json(404,{"error":"Nicht gefunden"})
        try:
            size=int(self.headers.get("Content-Length","0"))
            if not 1<=size<=100000:return self.send_json(413,{"error":"Ungültige Datenlänge"})
            data=validate(json.loads(self.rfile.read(size)))
            with LOCK:
                with connect() as c:
                    previous=load()
                    c.execute("INSERT INTO config(id,value) VALUES (1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",(json.dumps(data,ensure_ascii=False),))
                    if wanted(previous)!=wanted(data) or temperature_ids(previous)!=temperature_ids(data):
                        c.execute("DELETE FROM acquisition")
            return self.send_json(200,{"saved":True})
        except (ValueError,TypeError,KeyError,OverflowError) as exc:return self.send_json(400,{"error":str(exc)})
if __name__=="__main__":
    setup()
    threading.Thread(target=loop,daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0",PORT),API).serve_forever()
