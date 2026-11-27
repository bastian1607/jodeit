import os,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))
import server

class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        server.DB=os.path.join(self.tmp.name,"test.db")
        server.setup()
    def tearDown(self):
        self.tmp.cleanup()
    def test_empty(self):
        self.assertEqual(server.load()["rooms"],[])
    def test_room_without_sensor_allowed(self):
        # AB-013: a room without sensor is valid ("nicht überwacht").
        d=server.empty()
        d["rooms"]=[{"id":"a","name":"Bad","target":22,"temperature_sensors":[],"window_sensors":[],"radiators":[]}]
        server.validate(d)
    def test_zone(self):
        d=server.empty()
        d["rooms"]=[{"id":v,"name":v,"target":21,"temperature_sensors":["sensor."+v],"window_sensors":[],"radiators":[]} for v in ("a","b")]
        d["zones"]=[{"id":"x","name":"Offener Bereich","room_ids":["a","b"]}]
        self.assertEqual(server.validate(d),d)
    def test_overlap(self):
        d=server.empty()
        d["rooms"]=[{"id":v,"name":v,"target":21,"temperature_sensors":["sensor."+v],"window_sensors":[],"radiators":[]} for v in ("a","b","c")]
        d["zones"]=[{"id":"x","name":"A","room_ids":["a","b"]},{"id":"y","name":"B","room_ids":["b","c"]}]
        with self.assertRaises(ValueError):server.validate(d)
if __name__=="__main__":unittest.main()
