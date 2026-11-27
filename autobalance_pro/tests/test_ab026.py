"""AB-026: automatic deletion 30 days after the end of a job, notices, restore, reset (AC-026-01 … -09)."""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import retention  # noqa: E402
import server  # noqa: E402
from test_ab025 import Base, T0, customer  # noqa: E402

END = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


class Retention(Base):
    def setUp(self):
        super().setUp()
        self.reg = tempfile.TemporaryDirectory()
        self.reg_patch = patch.object(retention, "REGISTER_DIR", Path(self.reg.name) / "autobalance")
        self.reg_patch.start()

    def tearDown(self):
        self.reg_patch.stop()
        self.reg.cleanup()
        super().tearDown()

    def stopped(self, name, start, end, sensor="sensor.a"):
        s = self.job(name, customer("Raum", sensor), start, hours=int((end - start).total_seconds() // 3600))
        with self.at(end):
            server.stop_session(s["id"])
        return s

    def count(self, sid=None):
        with sqlite3.connect(server.DB) as c:
            if sid:
                return c.execute("SELECT COUNT(*) FROM samples WHERE session_id=?", (sid,)).fetchone()[0]
            return c.execute("SELECT COUNT(*) FROM samples").fetchone()[0]


class DeadlineTests(Retention):
    def test_ac01_deadline_is_end_plus_30_days_and_never_reset(self):
        s = self.stopped("Kunde A", END - timedelta(hours=4), END)
        self.assertEqual(server.get_session(s["id"])["delete_due_at"], "2026-12-27T12:00:00+00:00")
        server.session_report(s["id"])
        server.session_csv(s["id"])
        with self.at(END + timedelta(days=3)):
            server.archive_session(s["id"])
            server.setup()                                      # restart
        self.assertEqual(server.get_session(s["id"])["delete_due_at"], "2026-12-27T12:00:00+00:00")

    def test_ac02_running_job_is_never_deleted(self):
        s = self.job("Kunde A", customer("A", "sensor.a"), END - timedelta(days=60), hours=2)
        server.purge(END + timedelta(days=60))
        self.assertIsNotNone(server.get_session(s["id"]))
        self.assertEqual(self.count(s["id"]), 24)
        self.assertIsNone(server.get_session(s["id"])["delete_due_at"])

    def test_ac03_before_at_and_after_deadline(self):
        s = self.stopped("Kunde A", END - timedelta(hours=2), END)
        due = END + timedelta(days=30)
        server.purge(due - timedelta(seconds=1))
        self.assertIsNotNone(server.get_session(s["id"]))
        server.purge(due)
        self.assertIsNone(server.get_session(s["id"]))
        self.assertEqual(self.count(s["id"]), 0)
        self.assertEqual(server.deleted_info(s["id"])["reason"], "frist")

    def test_ac04_deleting_first_job_leaves_second_untouched(self):
        a = self.stopped("Kunde A", END - timedelta(days=3), END - timedelta(days=2), "sensor.a")
        b = self.stopped("Kunde B", END - timedelta(days=1), END, "sensor.b")
        report_b = server.session_report(b["id"])
        samples_b = self.count(b["id"])
        server.purge(END + timedelta(days=28, hours=12))        # A due, B not
        self.assertIsNone(server.get_session(a["id"]))
        self.assertEqual(self.count(b["id"]), samples_b)
        self.assertEqual(server.session_report(b["id"]), report_b)

    def test_ac05_restart_after_40_days_offline_catches_up(self):
        s = self.stopped("Kunde A", END - timedelta(hours=2), END)
        with self.at(END + timedelta(days=40)):
            server.setup()
        self.assertIsNone(server.get_session(s["id"]))

    def test_notices_week_and_day_before(self):
        s = self.stopped("Kunde A", END - timedelta(hours=2), END)
        with self.at(END + timedelta(days=20)):
            self.assertIsNone(server.get_session(s["id"])["deletion_notice"])
        with self.at(END + timedelta(days=24)):
            self.assertEqual(server.get_session(s["id"])["deletion_notice"], "week")
            server.archive_session(s["id"])
            self.assertIsNone(server.get_session(s["id"])["deletion_notice"])   # archived: no weekly warning
        with self.at(END + timedelta(days=29, hours=13)):
            self.assertEqual(server.get_session(s["id"])["deletion_notice"], "day")


class DeletionTests(Retention):
    def test_ac06_deleted_job_is_gone_everywhere(self):
        s = self.stopped("Kunde A · Müller", END - timedelta(hours=2), END)
        server.delete_session(s["id"], "manuell")
        self.assertIsNone(server.session_report(s["id"]))
        self.assertIsNone(server.session_csv(s["id"]))
        self.assertIsNone(server.session_assessment(s["id"]))
        with sqlite3.connect(server.DB) as c:
            log = c.execute("SELECT * FROM deletion_log").fetchall()
        self.assertNotIn("Müller", json.dumps(log, ensure_ascii=False))
        raw = Path(server.DB).read_bytes() + (Path(server.DB + "-wal").read_bytes() if Path(server.DB + "-wal").exists() else b"")
        self.assertNotIn("Müller".encode(), raw)               # physically removed (secure_delete + VACUUM)

    def test_running_job_cannot_be_deleted(self):
        s = self.job("Kunde A", customer("A", "sensor.a"), END, hours=1)
        with self.assertRaises(ValueError):
            server.delete_session(s["id"], "manuell")

    def snapshot(self, name):
        folder = Path(server.DB).parent / "backups"
        folder.mkdir(exist_ok=True)
        target = folder / name
        src, dst = sqlite3.connect(server.DB), sqlite3.connect(str(target))
        src.backup(dst); src.close(); dst.close()
        return target

    def test_review7_a_backup_with_two_jobs_loses_only_the_deleted_one(self):
        a = self.stopped("Kunde A · Müller", END - timedelta(days=3), END - timedelta(days=2))
        b = self.stopped("Kunde B · Schulze", END - timedelta(hours=2), END)
        backup = self.snapshot("autobalance-migration.db")
        old = (END - timedelta(days=400)).timestamp()
        os.utime(backup, (old, old))                          # mtime must not matter
        entry = server.delete_session(a["id"], "manuell", END + timedelta(days=1))
        self.assertTrue(backup.exists())                       # never deleted, only cleaned
        self.assertEqual(entry["backups_cleaned"], 1)
        with sqlite3.connect(str(backup)) as c:
            ids = {r[0] for r in c.execute("SELECT id FROM sessions")}
            self.assertEqual(ids, {b["id"]})
            self.assertEqual(c.execute("SELECT COUNT(*) FROM samples WHERE session_id=?", (a["id"],)).fetchone()[0], 0)
            self.assertGreater(c.execute("SELECT COUNT(*) FROM samples WHERE session_id=?", (b["id"],)).fetchone()[0], 0)
        self.assertNotIn("Müller".encode(), backup.read_bytes())   # physically removed from the copy
        self.assertIn("Schulze".encode(), backup.read_bytes())

    def test_review7_a_old_schema_backup_cleaned_by_job_window(self):
        a = self.stopped("Kunde A", END - timedelta(hours=2), END)
        folder = Path(server.DB).parent / "backups"
        folder.mkdir(exist_ok=True)
        legacy = folder / "autobalance-v0.1.db"
        with sqlite3.connect(str(legacy)) as c:                # schema before AB-024: no session_id
            c.execute("CREATE TABLE samples (id INTEGER PRIMARY KEY, at TEXT, entity_id TEXT, state TEXT, unit TEXT, valid INTEGER)")
            c.executemany("INSERT INTO samples(at,entity_id,state,unit,valid) VALUES(?,?,?,?,1)",
                          [((END - timedelta(hours=1)).isoformat(), "sensor.a", "20", "°C"),
                           ((END + timedelta(hours=5)).isoformat(), "sensor.a", "20", "°C")])
        server.delete_session(a["id"], "manuell", END + timedelta(days=1))
        with sqlite3.connect(str(legacy)) as c:
            self.assertEqual([r[0] for r in c.execute("SELECT at FROM samples")], [(END + timedelta(hours=5)).isoformat()])

    def test_review7_a_expired_job_in_backup_removed_by_purge(self):
        a = self.stopped("Kunde A", END - timedelta(hours=2), END)
        backup = self.snapshot("autobalance-x.db")
        with sqlite3.connect(server.DB) as c:                   # the main DB lost the job (e.g. earlier restore)
            c.execute("DELETE FROM samples WHERE session_id=?", (a["id"],))
            c.execute("DELETE FROM sessions WHERE id=?", (a["id"],))
        server.purge(END + timedelta(days=29))
        with sqlite3.connect(str(backup)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)   # not yet due
        server.purge(END + timedelta(days=30, minutes=1))
        with sqlite3.connect(str(backup)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_ac07_restore_of_app_backup_deletes_again(self):
        s = self.stopped("Kunde A", END - timedelta(hours=2), END)
        copy = os.path.join(self.reg.name, "app-backup.db")      # app-only backup (consistent copy)
        src, dst = sqlite3.connect(server.DB), sqlite3.connect(copy)
        src.backup(dst); src.close(); dst.close()
        server.delete_session(s["id"], "manuell", END + timedelta(days=1))
        self.assertTrue((retention.REGISTER_DIR / retention.REGISTER_FILE).exists())
        src, dst = sqlite3.connect(copy), sqlite3.connect(server.DB)   # restore /data; register stays
        src.backup(dst); src.close(); dst.close()
        self.assertIsNotNone(server.get_session(s["id"]))
        with self.at(END + timedelta(days=2)):
            server.setup()
        self.assertIsNone(server.get_session(s["id"]))
        self.assertEqual(server.deleted_info(s["id"])["reason"], "erneut nach Wiederherstellung")

    def test_review7_b_full_restore_with_register_rollback(self):
        a = self.stopped("Kunde A", END - timedelta(hours=2), END)
        b = self.stopped("Kunde B", END - timedelta(hours=1), END + timedelta(days=10))
        copy = os.path.join(self.reg.name, "full-backup.db")
        src, dst = sqlite3.connect(server.DB), sqlite3.connect(copy)
        src.backup(dst); src.close(); dst.close()
        register = retention.REGISTER_DIR / retention.REGISTER_FILE
        server.delete_session(a["id"], "manuell", END + timedelta(days=1))
        server.delete_session(b["id"], "manuell", END + timedelta(days=11))
        register.unlink()                                       # full HA restore also rolls back /share
        src, dst = sqlite3.connect(copy), sqlite3.connect(server.DB)
        src.backup(dst); src.close(); dst.close()
        with self.at(END + timedelta(days=31)):
            server.setup()                                      # A is past its deadline again -> deleted
        self.assertIsNone(server.get_session(a["id"]))
        self.assertIsNotNone(server.get_session(b["id"]))       # documented limit: B returns until its own deadline
        self.assertEqual(server.get_session(b["id"])["delete_due_at"], (END + timedelta(days=40)).isoformat())
        server.purge(END + timedelta(days=40, minutes=1))
        self.assertIsNone(server.get_session(b["id"]))
        self.assertIn(a["id"], {x["id"] for x in retention.read_register()})   # register rebuilt from the log

    def test_review7_c_register_not_writable(self):
        s = self.stopped("Kunde A", END - timedelta(hours=2), END)
        with patch.object(retention, "REGISTER_DIR", Path("/nicht/vorhanden/autobalance")):
            entry = server.delete_session(s["id"], "manuell", END + timedelta(days=1))
            self.assertEqual(entry["register"], "unavailable")
            self.assertIsNone(server.get_session(s["id"]))      # deletion itself still happens
            self.assertEqual(server.retention_state()["register"], "unavailable")
            self.assertIn("nicht beschreibbar", server.retention_state()["register_text"])

    def test_review7_c_corrupt_register_is_kept_aside_and_rebuilt(self):
        a = self.stopped("Kunde A", END - timedelta(hours=2), END)
        b = self.stopped("Kunde B", END - timedelta(hours=1), END)
        server.delete_session(a["id"], "manuell", END + timedelta(days=1))
        register = retention.REGISTER_DIR / retention.REGISTER_FILE
        register.write_text("{kaputt")
        self.assertEqual(retention.read_register_status()[1], "corrupt")
        entry = server.delete_session(b["id"], "manuell", END + timedelta(days=2))
        self.assertEqual(entry["register"], "corrupt")
        self.assertEqual({x["id"] for x in retention.read_register()}, {a["id"], b["id"]})
        self.assertTrue(list(retention.REGISTER_DIR.glob(retention.REGISTER_FILE + ".defekt-*")))
        self.assertEqual(server.retention_state()["register"], "corrupt")

    def test_review7_purge_error_is_persistent_until_next_success(self):
        s = self.stopped("Kunde A", END - timedelta(hours=2), END)
        with patch.object(server, "delete_session", side_effect=OSError("disk")):
            r = server.purge(END + timedelta(days=31))
        self.assertTrue(r["errors"])
        state = server.retention_state()
        self.assertEqual(state["errors"][0]["code"], "job")
        self.assertNotEqual(state["last_ok"], (END + timedelta(days=31)).isoformat())
        self.assertEqual(state["last_run"], (END + timedelta(days=31)).isoformat())
        self.assertIsNotNone(server.get_session(s["id"]))
        server.purge(END + timedelta(days=31, hours=1))          # hourly retry
        self.assertIsNone(server.get_session(s["id"]))
        self.assertEqual(server.retention_state()["errors"], [])
        self.assertEqual(server.retention_state()["last_ok"], (END + timedelta(days=31, hours=1)).isoformat())

    def test_loose_data_older_than_30_days_is_purged(self):
        self.put([(END - timedelta(days=31), "sensor.z", 20.0), (END - timedelta(days=29), "sensor.z", 20.0)])
        server.purge(END)
        self.assertEqual(self.count(), 1)

    def test_ac08_no_write_path(self):
        s = self.stopped("Kunde A", END - timedelta(hours=2), END)
        with patch("urllib.request.urlopen", side_effect=AssertionError("no network")):
            server.purge(END + timedelta(days=31))
        self.assertIsNone(server.get_session(s["id"]))


class ResetTests(Retention):
    def test_reset_for_new_customer(self):
        a = self.stopped("Kunde A", END - timedelta(hours=2), END)
        b = self.stopped("Kunde B", END - timedelta(hours=1), END)
        server.archive_session(a["id"])
        self.put([(END, "sensor.a", 20.0)])                      # passive sample of customer A
        with self.assertRaises(ValueError):
            server.reset_for_new_customer({"confirm": "ja"})
        with self.assertRaises(server.UnarchivedJobs) as e:        # B has no confirmed filing
            server.reset_for_new_customer({"confirm": server.RESET_WORD})
        self.assertEqual([j["id"] for j in e.exception.jobs], [b["id"]])
        self.assertIsNotNone(server.get_session(a["id"]))        # review 7 (d): refused -> nothing deleted
        self.assertEqual(self.count(), self.count(a["id"]) + self.count(b["id"]) + 1)
        self.assertNotEqual(server.load_v2()["rooms"], [])
        result = server.reset_for_new_customer({"confirm": server.RESET_WORD, "allow_unarchived": True})
        self.assertEqual(result["jobs_deleted"], 2)              # always all jobs
        self.assertEqual(result["loose_samples"], 1)
        cfg = server.load_v2()
        self.assertEqual(cfg["rooms"], [])
        self.assertEqual(cfg["building"]["heat_source"]["profile"], "")
        self.assertNotIn("sensor.a", json.dumps(server.config_history()))
        self.assertEqual(self.count(), 0)

    def test_reset_without_unarchived_jobs_needs_no_extra_consent(self):
        a = self.stopped("Kunde A", END - timedelta(hours=2), END)
        server.archive_session(a["id"])
        self.assertEqual(server.reset_for_new_customer({"confirm": server.RESET_WORD})["jobs_deleted"], 1)

    def test_reset_refused_while_job_runs(self):
        self.job("Kunde A", customer("A", "sensor.a"), END, hours=1)
        with self.assertRaises(ValueError):
            server.reset_for_new_customer({"confirm": server.RESET_WORD})


class ApiTests(Retention):
    def test_endpoints(self):
        s = self.stopped("Kunde A", END - timedelta(hours=2), END)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.API)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_port}/api/v2/"

        def post(path, body):
            return json.load(urlopen(Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})))
        try:
            self.assertTrue(post("sessions/archive", {"id": s["id"]})["archived_at"])
            with self.assertRaises(HTTPError) as e:
                post("sessions/delete", {"id": s["id"], "confirm": "falsch"})
            e.exception.close()
            post("sessions/delete", {"id": s["id"], "confirm": "Kunde A"})
            with self.assertRaises(HTTPError) as e:
                urlopen(base + "sessions/" + s["id"] + "/report")
            self.assertEqual(e.exception.code, 410)
            e.exception.close()
            b = self.stopped("Kunde B", END - timedelta(hours=1), END)
            with self.assertRaises(HTTPError) as e:
                post("reset", {"confirm": server.RESET_WORD})
            self.assertEqual(e.exception.code, 409)
            self.assertEqual(json.load(e.exception)["unarchived"][0]["id"], b["id"])
            e.exception.close()
            self.assertIn("retention", json.load(urlopen(base + "sessions")))
            info = json.load(urlopen(base + "retention"))
            self.assertIn("status", info)
            self.assertEqual(info["retention_days"], 30)
            self.assertEqual(info["log"][0]["reason"], "manuell")
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
