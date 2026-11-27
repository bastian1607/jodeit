import copy
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import urlopen, Request
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'app'))
import server

class QualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = server.DB
        server.DB = os.path.join(self.tmp.name, 'test.db')
        server.setup()
        self.now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
        self.config = server.empty()
        self.config['rooms'] = [{'id':'a','name':'Room','target':21,'temperature_sensors':['sensor.room'],'window_sensors':['binary_sensor.window'],'radiators':[]}]
        self.save()
    def tearDown(self):
        server.DB = self.old_db
        self.tmp.cleanup()
    def save(self):
        with server.connect() as c:
            c.execute('INSERT OR REPLACE INTO config VALUES(1,?)', (json.dumps(self.config),))
        server.setup()  # legacy row written directly: start-up sync merges it into schema v2
    def entity(self, **kw):
        e = {'entity_id':'sensor.room','state':'68','unit':'°F','source_at':self.now.isoformat()}
        e.update(kw)
        return e
    def sample(self, entities):
        with patch.object(server, 'utcnow', return_value=self.now), patch.object(server, 'get_entities', return_value=entities):
            server.sample()
    def test_normalizes_fahrenheit_and_preserves_raw(self):
        self.sample([self.entity(),self.entity(entity_id='binary_sensor.window',state='on',unit='')])
        rows=server.latest_observations(self.now)
        temp=next(r for r in rows if r['entity_id']=='sensor.room')
        self.assertEqual((temp['state'],temp['unit']),('68','°F'))
        self.assertEqual((temp['normalized_value'],temp['normalized_unit']),(20,'°C'))
        self.assertTrue(temp['valid'])
        self.assertFalse(server.latest_observations(self.now+timedelta(hours=1))[0]['valid'])
    def test_quality_reasons(self):
        cases=[({'state':'NaN'},'invalid_number'),({'state':'inf'},'invalid_number'),({'state':'abc'},'invalid_number'),({'unit':'W'},'invalid_unit'),({'state':'9999'},'implausible'),({'state':'unknown'},'unknown'),({'state':'unavailable'},'unavailable'),({'source_at':None},'missing_timestamp'),({'source_at':'bad'},'missing_timestamp'),({'source_at':(self.now+timedelta(minutes=2)).isoformat()},'future_timestamp'),({'source_at':(self.now-timedelta(minutes=31)).isoformat()},'ok')]
        for changes,expected in cases:
            with self.subTest(changes=changes):self.assertEqual(server.quality(self.entity(**changes),True,self.now)[0],expected)
        self.assertEqual(server.quality(self.entity(entity_id='binary_sensor.window',state='broken'),False,self.now)[0],'invalid_state')
    def test_missing_entities_and_outage_recovery(self):
        self.sample([self.entity()])
        self.assertEqual(server.latest_observations(self.now)[0]['quality'],'unavailable')
        self.assertTrue(server.acquisition_status(self.now)['connected'])
        with patch.object(server,'utcnow',return_value=self.now), patch.object(server,'get_entities',side_effect=RuntimeError('secret-url')):
            server.sample()
        status=server.acquisition_status(self.now)
        self.assertFalse(status['connected'])
        self.assertEqual(status['error'],'ha_unavailable')
        self.assertEqual(status['last_success'],self.now.isoformat())
        server.setup()  # Restart/migration preserves failure state.
        self.assertEqual(server.acquisition_status(self.now)['error'],'ha_unavailable')
        self.sample([self.entity()])
        self.assertIsNone(server.acquisition_status(self.now)['error'])
    def test_latest_filters_deleted_assignments(self):
        self.sample([self.entity()])
        self.config['rooms']=[]
        self.save()
        self.assertEqual(server.latest_observations(self.now),[])
        server.sample()
        self.assertEqual(server.acquisition_status()['error'],'not_configured')
    def test_offline_schema_validation(self):
        server.validate(self.config)
        cases=[('target',True),('area','NaN'),('area',-2),('heat_load','inf')]
        for key,value in cases:
            d=copy.deepcopy(self.config); d['rooms'][0][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):server.validate(d)
        for value in ('light.x','sensor.','sensor.Bad','sensor.bad/value',[],3):
            d=copy.deepcopy(self.config);d['rooms'][0]['radiators']=[{'name':'R','entity':value}]
            with self.subTest(entity=value),self.assertRaises(ValueError):server.validate(d)
        for key,value in (('outdoor','climate.wp'),('dhw','switch.x'),('flow',[])):
            d=copy.deepcopy(self.config);d['heating'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):server.validate(d)
        d=copy.deepcopy(self.config);d['rooms'][0]['radiators']=[{'name':'R','entity':'number.valve','power_w':'200'}]
        with patch.object(server,'get_entities',side_effect=AssertionError('Must remain offline')):self.assertEqual(server.validate(d),d)
    def test_null_optional_assignment_is_not_sampled(self):
        self.config['rooms']=[]
        self.config['heating']['outdoor']=None
        self.save()
        self.assertEqual(server.wanted(self.config),set())
        with patch.object(server,'get_entities',side_effect=AssertionError('No configured IDs')):server.sample()
        self.assertEqual(server.acquisition_status()['error'],'not_configured')
    def test_unsafe_room_id_rejected(self):
        self.config['rooms'][0]['id']="x');alert(1)//"
        with self.assertRaises(ValueError):server.validate(self.config)
    def test_old_database_migrates_idempotently(self):
        os.remove(server.DB)
        with sqlite3.connect(server.DB) as c:
            c.execute('CREATE TABLE samples(id INTEGER PRIMARY KEY,at TEXT,entity_id TEXT,state TEXT,unit TEXT,valid INTEGER)')
            c.execute('INSERT INTO samples VALUES(1,?,?,?,?,?)',(self.now.isoformat(),'sensor.room','20','°C',1))
        server.setup();server.setup();self.save()
        row=next(r for r in server.latest_observations(self.now) if r['entity_id']=='sensor.room')
        self.assertEqual(row['state'],'20')
        self.assertEqual(row['quality'],'unchecked')
        self.assertFalse(row['valid'])
    def test_ha_metadata_prefers_last_reported(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
        raw=[{'entity_id':'sensor.room','state':'20','attributes':{'unit_of_measurement':'°C'},'last_reported':self.now.isoformat(),'last_updated':'old','last_changed':'older'}]
        with patch.object(server,'TOKEN','test'),patch.object(server.urllib.request,'urlopen',return_value=Response()),patch.object(server.json,'load',return_value=raw):
            self.assertEqual(server.get_entities()[0]['source_at'],self.now.isoformat())
        raw[0].pop('last_reported')
        raw[0]['last_updated']=self.now.isoformat()
        with patch.object(server,'TOKEN','test'),patch.object(server.urllib.request,'urlopen',return_value=Response()),patch.object(server.json,'load',return_value=raw):
            self.assertEqual(server.get_entities()[0]['source_at'],self.now.isoformat())
    def test_ab027_unchanged_temperature_stays_valid(self):
        unchanged=self.entity(source_at=(self.now-timedelta(hours=20)).isoformat())   # device reports on change only
        self.assertEqual(server.quality(unchanged,True,self.now)[0],'ok')
        self.sample([unchanged])
        row=next(r for r in server.latest_observations(self.now) if r['entity_id']=='sensor.room')
        self.assertTrue(row['valid'])
        self.assertFalse(row['stale'])
        self.assertTrue(row['unchanged_long'])                                         # neutral hint only
        self.assertFalse(row['unchanged_check'])                                       # 20 h < 24 h
        row=next(r for r in server.latest_observations(self.now+timedelta(minutes=31)) if r['entity_id']=='sensor.room')   # own acquisition stopped
        self.assertTrue(row['stale'])
        self.assertFalse(row['valid'])
        self.assertEqual(server.quality(dict(unchanged,state='unavailable'),True,self.now)[0],'unavailable')
    def test_review10_ha_timestamp_kind(self):
        fresh=(self.now-timedelta(minutes=1)).isoformat();old=(self.now-timedelta(hours=20)).isoformat()
        e=server.ha_entity({"entity_id":"sensor.t","state":"20","attributes":{"unit_of_measurement":"°C"},
                            "last_reported":fresh,"last_updated":old,"last_changed":old})
        self.assertEqual((e["source_at"],e["source_at_kind"]),(fresh,"reported"))     # fresh report, value unchanged for 20 h
        e=server.ha_entity({"entity_id":"sensor.t","state":"20","last_updated":old,"last_changed":old})
        self.assertEqual((e["source_at"],e["source_at_kind"]),(old,"updated"))        # older HA without last_reported
        e=server.ha_entity({"entity_id":"sensor.t","state":"20","last_changed":old})
        self.assertEqual((e["source_at"],e["source_at_kind"]),(None,"unknown"))       # last_changed is never used
    def test_review10_fresh_report_unchanged_value_no_hint(self):
        changed=(self.now-timedelta(hours=36)).isoformat()
        self.sample([self.entity(source_at=(self.now-timedelta(minutes=2)).isoformat(),source_at_kind='reported')])
        row=next(r for r in server.latest_observations(self.now) if r['entity_id']=='sensor.room')
        self.assertTrue(row['valid'])
        self.assertEqual(row['source_at_kind'],'reported')
        self.assertFalse(row['unchanged_long'])                                         # value may be unchanged 36 h; reported -> no hint
        self.assertNotEqual(row['source_at'],changed)
    def test_review10_flags_reach_room_status(self):
        import monitoring
        self.sample([self.entity(source_at=(self.now-timedelta(hours=26)).isoformat(),source_at_kind='reported')])
        row=next(r for r in server.latest_observations(self.now+timedelta(minutes=2)) if r['entity_id']=='sensor.room')
        s=monitoring._sensor('sensor.room',row)
        self.assertTrue(s['valid'] and s['unchanged_long'] and s['unchanged_check'])
        self.assertEqual(s['source_at_kind'],'reported')
        self.assertEqual(s['poll_age_seconds'],120)
    def test_ab027_historical_stale_samples_stay_excluded(self):
        with server.connect() as c:
            c.execute("INSERT INTO samples(at,entity_id,state,unit,valid,source_at,quality,normalized_value,normalized_unit) VALUES(?,?,?,?,?,?,?,?,?)",
                      ((self.now-timedelta(days=1)).isoformat(),'sensor.room','20.5','°C',0,(self.now-timedelta(days=1,hours=2)).isoformat(),'stale',20.5,'°C'))
        server.setup()                                                                   # no silent re-qualification on start
        with server.connect() as c:
            self.assertEqual(c.execute("SELECT quality,valid FROM samples WHERE quality='stale'").fetchall(),[('stale',0)])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM schema_migrations WHERE kind LIKE 'ab027%'").fetchone()[0],0)
    def test_ab027_repeated_polls_of_unchanged_value(self):
        since=(self.now-timedelta(hours=30)).isoformat()
        for k in range(3):                                                               # HA keeps delivering 20 °C, unchanged
            with patch.object(server,'utcnow',return_value=self.now+timedelta(minutes=5*k)),patch.object(server,'get_entities',return_value=[self.entity(state='20',unit='°C',source_at=since)]):
                server.sample()
        with server.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM samples WHERE entity_id='sensor.room' AND quality='ok' AND valid=1").fetchone()[0],3)
        row=next(r for r in server.latest_observations(self.now+timedelta(minutes=10)) if r['entity_id']=='sensor.room')
        self.assertTrue(row['valid'] and row['unchanged_long'] and row['unchanged_check'])
    def test_ab011_event_based_entities_are_not_aged_out(self):
        old=(self.now-timedelta(hours=7)).isoformat()
        contact=self.entity(entity_id='binary_sensor.window',state='off',unit='',source_at=old)
        self.assertEqual(server.quality(contact,False,self.now)[0],'ok')
        self.assertEqual(server.quality(dict(contact,source_at=None),False,self.now)[0],'ok')
        self.assertEqual(server.quality(dict(contact,state='unavailable'),False,self.now)[0],'unavailable')
        self.assertEqual(server.quality(dict(contact,state='unknown'),False,self.now)[0],'unknown')
        status=self.entity(entity_id='sensor.verdichter',state='aus',unit='',source_at=old)
        self.assertEqual(server.quality(status,False,self.now)[0],'ok')
        self.assertEqual(server.quality(self.entity(source_at=old),True,self.now)[0],'ok')      # AB-027: unchanged, not stale
        self.sample([self.entity(),contact])
        rows={r['entity_id']:r for r in server.latest_observations(self.now)}
        self.assertTrue(rows['binary_sensor.window']['valid'])
        self.assertFalse(rows['binary_sensor.window']['stale'])
        later=self.now+timedelta(minutes=31)                                                  # own acquisition stopped
        rows={r['entity_id']:r for r in server.latest_observations(later)}
        self.assertTrue(rows['binary_sensor.window']['stale'])
        self.assertFalse(rows['binary_sensor.window']['valid'])
    def test_http_version_and_validation(self):
        manifest=(Path(__file__).resolve().parents[1]/'config.yaml').read_text()
        self.assertEqual(re.search(r'^version:\s*(.+)$',manifest,re.M).group(1),server.VERSION)
        http=ThreadingHTTPServer(('127.0.0.1',0),server.API)
        worker=threading.Thread(target=http.serve_forever,daemon=True);worker.start()
        base=f'http://127.0.0.1:{http.server_port}'
        try:
            with urlopen(base+'/health') as response:self.assertEqual(json.load(response)['version'],server.VERSION)
            with urlopen(base+'/') as response:
                html=response.read().decode()
                self.assertNotIn('__APP_VERSION__',html)
                self.assertIn('v'+server.VERSION,html)
                self.assertIn('src="jodeit-logo.png"',html)
            with urlopen(base+'/jodeit-logo.png') as response:
                self.assertEqual(response.headers['Content-Type'],'image/png')
                self.assertEqual(response.read()[:8],b'\x89PNG\r\n\x1a\n')
            with urlopen(base+'/api/overview') as response:self.assertEqual(json.load(response)['version'],server.VERSION)
            d=copy.deepcopy(self.config);d['heating']['outdoor']='light.bad'
            with self.assertRaises(HTTPError) as caught:urlopen(Request(base+'/api/config',json.dumps(d).encode(),{'Content-Type':'application/json'}))
            self.assertEqual(caught.exception.code,400)
            caught.exception.close()
            self.sample([self.entity()])
            with patch.object(server,'get_entities',side_effect=RuntimeError('offline')):server.sample()
            with urlopen(Request(base+'/api/config',json.dumps(self.config).encode(),{'Content-Type':'application/json'})) as response:self.assertTrue(json.load(response)['saved'])
            self.assertEqual(server.acquisition_status()['error'],'ha_unavailable')
            changed=copy.deepcopy(self.config)
            changed['rooms'][0]['temperature_sensors']=['sensor.other']
            with urlopen(Request(base+'/api/config',json.dumps(changed).encode(),{'Content-Type':'application/json'})) as response:self.assertTrue(json.load(response)['saved'])
            self.assertIsNone(server.acquisition_status()['connected'])
            self.assertIsNone(server.acquisition_status()['last_success'])
        finally:http.shutdown();http.server_close();worker.join()

if __name__=='__main__':unittest.main()
