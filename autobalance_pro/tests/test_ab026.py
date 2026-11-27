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

    def test_local_db_backups_containing_the_job_are_removed(self):
        folder = Path(server.DB).parent / "backups"
        folder.mkdir(exist_ok=True)
        old, new = folder / "old.db", folder / "new.db"
        old.write_text("x"); new.write_text("x")
        os.utime(old, ((END - timedelta(days=10)).timestamp(),) * 2)
        os.utime(new, ((END - timedelta(hours=1)).timestamp(),) * 2)
        s = self.stopped("Kunde A", END - timedelta(hours=2), END)
        entry = server.delete_session(s["id"], "manuell", END + timedelta(days=1))
        self.assertEqual(entry["backups_deleted"], 1)
        self.assertTrue(old.exists())
        self.assertFalse(new.exists())

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
        self.put([(END, "sensor.a", 20.0)])                      # passive sample of customer A
        with self.assertRaises(ValueError):
            server.reset_for_new_customer({"confirm": "ja"})
        result = server.reset_for_new_customer({"confirm": server.RESET_WORD})
        self.assertEqual(result["loose_samples"], 1)
        cfg = server.load_v2()
        self.assertEqual(cfg["rooms"], [])
        self.assertEqual(cfg["building"]["heat_source"]["profile"], "")
        self.assertNotIn("sensor.a", json.dumps(server.config_history()))
        self.assertIsNotNone(server.get_session(a["id"]))        # stopped job stays isolated until its deadline
        server.reset_for_new_customer({"confirm": server.RESET_WORD, "delete_jobs": True})
        self.assertIsNone(server.get_session(a["id"]))

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
            info = json.load(urlopen(base + "retention"))
            self.assertEqual(info["retention_days"], 30)
            self.assertEqual(info["log"][0]["reason"], "manuell")
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
