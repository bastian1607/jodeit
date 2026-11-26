"""AB-010: orderly shutdown on SIGTERM with exit code 0."""
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
import server  # noqa: E402


class ShutdownTests(unittest.TestCase):
    def test_stop_returns_zero_and_ends_acquisition_quickly(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, "DB", os.path.join(tmp, "s.db")), \
                patch.object(server, "sample", return_value=None):
            box, result = {}, {}
            started = threading.Event()

            def ready(httpd, stop):
                box["port"], box["stop"] = httpd.server_address[1], stop
                started.set()
            t = threading.Thread(target=lambda: result.setdefault("rc", server.main(port=0, install_signals=False, ready=ready)))
            t.start()
            self.assertTrue(started.wait(5))
            with urlopen(f"http://127.0.0.1:{box['port']}/health") as r:
                self.assertEqual(r.status, 200)
            begin = time.monotonic()
            box["stop"]("TEST")
            box["stop"]("TEST")                      # idempotent
            t.join(10)
            self.assertFalse(t.is_alive())
            self.assertLess(time.monotonic() - begin, 5)   # loop must not sleep its full 300 s
            self.assertEqual(result["rc"], 0)

    def test_real_process_exits_zero_on_sigterm(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, AUTO_BALANCE_DB=os.path.join(tmp, "p.db"), PORT="0", SUPERVISOR_TOKEN="")
            proc = subprocess.Popen([sys.executable, str(APP / "server.py")], cwd=str(APP), env=env,  # same as Dockerfile CMD
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            time.sleep(1.5)
            self.assertIsNone(proc.poll(), proc.stdout.read() if proc.poll() is not None else "")
            proc.send_signal(signal.SIGTERM)
            try:
                out, _ = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                self.fail("process did not stop after SIGTERM")
            self.assertEqual(proc.returncode, 0, out)
            self.assertIn("AutoBalance beendet", out)


if __name__ == "__main__":
    unittest.main()
