"""AutoBalance Pro – read-only Home Assistant pilot."""
import json,logging,os,sqlite3,threading,time,urllib.request
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
def empty():
    return {"rooms":[],"zones":[],"heating":{"manufacturer":"","model":"","circuit_name":"Heizkreis 1","outdoor":"","flow":"","return":"","compressor":"","dhw":""}}
def load():
    with connect() as c:r=c.execute("SELECT value FROM config WHERE id=1").fetchone()
    return json.loads(r[0]) if r else empty()
def validate(d):
    if not isinstance(d,dict) or not isinstance(d.get("rooms"),list) or not isinstance(d.get("zones"),list) or not isinstance(d.get("heating"),dict):raise ValueError("Konfigurationsformat ungültig")
    if len(d["rooms"])>100 or len(d["zones"])>100 or len(json.dumps(d))>100000:raise ValueError("Konfiguration zu groß")
    ids=set()
    for r in d["rooms"]:
        if not isinstance(r,dict) or not isinstance(r.get("id"),str) or not r["id"] or r["id"] in ids or not str(r.get("name","")).strip():raise ValueError("Raum-ID/Name fehlt oder doppelt")
        ids.add(r["id"])
        if not isinstance(r.get("temperature_sensors"),list) or not r["temperature_sensors"]:raise ValueError("Mindestens ein Temperatursensor pro Raum")
        if not isinstance(r.get("window_sensors"),list) or not isinstance(r.get("radiators"),list):raise ValueError("Fenster-/Heizkörperliste ungültig")
        if not 5<=float(r.get("target",21))<=35:raise ValueError("Raum-Solltemperatur ungültig")
        if r.get("heat_load") not in ("",None) and not 0<float(r["heat_load"])<=50000:raise ValueError("Raumheizlast ungültig")
        for key,domain in (("temperature_sensors","sensor."),("window_sensors","binary_sensor.")):
            if len(r[key])>30 or any(not isinstance(s,str) or not s.startswith(domain) or len(s)>200 for s in r[key]):raise ValueError("Ungültige Entität: "+key)
        for radiator in r["radiators"]:
            if not isinstance(radiator,dict) or not str(radiator.get("name","")).strip():raise ValueError("Heizkörper benötigt Bezeichnung")
    used=set()
    for z in d["zones"]:
        if not isinstance(z,dict) or not str(z.get("name","")).strip() or not isinstance(z.get("room_ids"),list) or len(z["room_ids"])<2:raise ValueError("Zone benötigt Name und mindestens zwei Räume")
        for rid in z["room_ids"]:
            if rid not in ids or rid in used:raise ValueError("Unbekannter oder mehrfach zonierter Raum")
            used.add(rid)
    return d
def get_entities():
    if not TOKEN:raise RuntimeError("Kein SUPERVISOR_TOKEN – nur in Home Assistant verfügbar")
    request=urllib.request.Request(HA_URL,headers={"Authorization":"Bearer "+TOKEN,"Content-Type":"application/json"})
    with urllib.request.urlopen(request,timeout=15) as response:items=json.load(response)
    if not isinstance(items,list):raise RuntimeError("Home Assistant lieferte keine Entitätenliste")
    return [{"entity_id":e.get("entity_id",""),"name":e.get("attributes",{}).get("friendly_name",e.get("entity_id","")),"state":e.get("state",""),"unit":e.get("attributes",{}).get("unit_of_measurement",""),"device_class":e.get("attributes",{}).get("device_class","")} for e in items if isinstance(e,dict)]
def wanted(d):
    ids=set()
    for r in d["rooms"]:
        ids.update(r.get("temperature_sensors",[]))
        ids.update(r.get("window_sensors",[]))
        ids.update(v["entity"] for v in r.get("radiators",[]) if v.get("entity"))
    ids.update(d["heating"].get(key,"") for key in ("outdoor","flow","return","compressor","dhw"))
    ids.discard("")
    return ids
def sample():
    with LOCK:
        desired=wanted(load())
        if not desired:return
        state={x["entity_id"]:x for x in get_entities()}
        now=datetime.now(timezone.utc).isoformat()
        vals=[]
        for entity in desired:
            e=state.get(entity,{})
            v=e.get("state","unavailable")
            vals.append((now,entity,v,e.get("unit",""),int(v not in ("unavailable","unknown",""))))
        with connect() as c:c.executemany("INSERT INTO samples(at,entity_id,state,unit,valid) VALUES (?,?,?,?,?)",vals)
def loop():
    while True:
        try:sample()
        except Exception as exc:logging.warning("Abfrage nicht möglich: %s",exc)
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
            body=(ROOT/"static"/"index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if path=="/health":return self.send_json(200,{"ok":True,"read_only":True,"version":"0.1.0"})
        if path=="/api/config":return self.send_json(200,load())
        if path=="/api/entities":
            try:return self.send_json(200,{"connected":True,"entities":get_entities()})
            except Exception as exc:return self.send_json(503,{"connected":False,"entities":[],"error":str(exc)})
        if path=="/api/overview":
            with connect() as c:row=c.execute("SELECT COUNT(*),MAX(at) FROM samples").fetchone()
            d=load()
            return self.send_json(200,{"rooms":len(d["rooms"]),"zones":len(d["zones"]),"samples":row[0],"last_sample":row[1]})
        if path=="/api/latest":
            with connect() as c:rows=c.execute("SELECT entity_id,state,unit,valid,at FROM samples WHERE id IN (SELECT MAX(id) FROM samples GROUP BY entity_id) ORDER BY entity_id").fetchall()
            return self.send_json(200,{"observations":[dict(zip(("entity_id","state","unit","valid","at"),r)) for r in rows]})
        return self.send_json(404,{"error":"Nicht gefunden"})
    def do_POST(self):
        if urlsplit(self.path).path!="/api/config":return self.send_json(404,{"error":"Nicht gefunden"})
        try:
            size=int(self.headers.get("Content-Length","0"))
            if not 1<=size<=100000:return self.send_json(413,{"error":"Ungültige Datenlänge"})
            data=validate(json.loads(self.rfile.read(size)))
            with LOCK:
                with connect() as c:c.execute("INSERT INTO config(id,value) VALUES (1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",(json.dumps(data,ensure_ascii=False),))
            return self.send_json(200,{"saved":True})
        except (ValueError,TypeError,KeyError,OverflowError) as exc:return self.send_json(400,{"error":str(exc)})
if __name__=="__main__":
    setup()
    threading.Thread(target=loop,daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0",PORT),API).serve_forever()
