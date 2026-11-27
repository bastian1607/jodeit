"""Optional browser integration tests.

Run AUTO_BALANCE_UI_TESTS=1 python -m unittest discover -s tests -p test_ui.py.
Requires playwright and Chromium (PLAYWRIGHT_CHROMIUM_EXECUTABLE overrides path).
Only a temporary local API and synthetic HA entities are used.
"""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import server

try:
    from playwright.sync_api import sync_playwright, expect
except ImportError:
    sync_playwright = None


@unittest.skipUnless(os.environ.get('AUTO_BALANCE_UI_TESTS') == '1' and sync_playwright,
                     'Optional: set AUTO_BALANCE_UI_TESTS=1 and install playwright/Chromium')
class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = server.DB
        server.DB = str(Path(self.tmp.name) / 'browser.db')
        server.setup()
        stamp = datetime.now(timezone.utc).isoformat()
        self.entities = [
            dict(entity_id='sensor.room_a', name='Raum Alpha', state='68', unit='°F', device_class='temperature', source_at=stamp, last_reported=stamp, last_updated=stamp),
            dict(entity_id='sensor.room_b', name='Raum Beta', state='21', unit='°C', device_class='temperature', source_at=stamp, last_reported=stamp, last_updated=stamp),
            dict(entity_id='binary_sensor.window', name='Fenster', state='off', unit='', device_class='window', source_at=stamp, last_reported=stamp, last_updated=stamp),
            dict(entity_id='number.valve', name='Ventil Alpha', state='40', unit='%', device_class='', source_at=stamp, last_reported=stamp, last_updated=stamp),
            dict(entity_id='number.second', name='Ventil Beta', state='50', unit='%', device_class='', source_at=stamp, last_reported=stamp, last_updated=stamp),
        ]
        self.ha = patch.object(server, 'get_entities', side_effect=lambda: self.entities)
        self.ha.start()
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.API)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.http.server_port}'
        self.playwright = sync_playwright().start()
        executable = os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE') or shutil.which('chromium')
        try:
            self.browser = self.playwright.chromium.launch(headless=True, executable_path=executable,
                                                         args=['--no-sandbox'])
        except Exception:
            self.cleanup()
            raise
        self.context = self.browser.new_context(viewport={'width': 1280, 'height': 900})
        self.context.tracing.start(screenshots=True, snapshots=True)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

    def cleanup(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.ha.stop()
        server.DB = self.old_db
        self.tmp.cleanup()
        self.playwright.stop()

    def tearDown(self):
        artifacts = Path(os.environ.get('AUTO_BALANCE_UI_ARTIFACTS', '/tmp/autobalance-ui-artifacts'))
        artifacts.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(artifacts / f'{self._testMethodName}.png'), full_page=True)
        self.context.tracing.stop(path=str(artifacts / f'{self._testMethodName}.zip'))
        self.context.close()
        self.browser.close()
        self.cleanup()
        self.assertEqual(self.errors, [], 'Browser JavaScript errors')

    def config(self):
        cfg = server.empty()
        cfg['rooms'] = [dict(id='alpha', name='Alpha', target=21, temperature_sensors=['sensor.room_a'],
                             window_sensors=['binary_sensor.window'], radiators=[dict(name='Radiator', entity='number.valve')])]
        req = Request(self.url + '/api/config', data=json.dumps(cfg).encode(),
                      headers={'Content-Type': 'application/json'}, method='POST')
        with urlopen(req) as response:
            self.assertEqual(response.status, 200)
        return cfg

    def test_version_quality_and_outage(self):
        self.config()
        server.sample()
        self.page.goto(self.url)
        expect(self.page.locator('header small')).to_contain_text('v' + server.VERSION)
        logo = self.page.get_by_role('img', name='Jodeit Systeme')
        expect(logo).to_be_visible()
        self.assertGreater(logo.evaluate('img => img.naturalWidth'), 0)
        expect(self.page.locator('#acquisition-status')).to_contain_text('Home Assistant erreichbar')
        self.page.get_by_role('button', name='5 Prüfen & Messung').click()
        expect(self.page.locator('#sample-list')).to_contain_text('20 °C')
        expect(self.page.locator('#sample-list')).to_contain_text('Gültig')
        self.entities[0]['source_at'] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        server.sample()
        self.page.get_by_role('button', name='Aktualisieren', exact=True).click()
        expect(self.page.locator('#sample-list')).to_contain_text('Gültig')          # AB-027: unchanged value, not stale
        expect(self.page.locator('#sample-list')).not_to_contain_text('Veraltet')
        self.entities[0]['source_at'] = (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat()
        self.entities[0]['source_at_kind'] = 'reported'
        server.sample()
        self.page.get_by_role('button', name='Aktualisieren', exact=True).click()
        hint = self.page.locator('#sample-list .unchanged-hint')                       # review 11 (P2)
        expect(hint).to_contain_text('Seit 13 h keine neuere Meldung bei Home Assistant erkennbar')
        expect(hint).to_contain_text('ein Lebenszeichen des Fühlers ist damit nicht separat bestätigt')
        expect(self.page.locator('#sample-list')).to_contain_text('Letzte Meldung an Home Assistant')
        expect(self.page.locator('#sample-list')).not_to_contain_text('unverändert')     # no claim about a temperature change
        self.entities[0]['source_at_kind'] = None                                      # older samples: kind unknown
        server.sample()
        self.page.get_by_role('button', name='Aktualisieren', exact=True).click()
        expect(hint).to_contain_text('keine neuere Meldung oder Aktualisierung bei Home Assistant erkennbar')
        self.entities[0]['state'] = 'unknown'
        server.sample()
        self.page.get_by_role('button', name='Aktualisieren', exact=True).click()
        expect(self.page.locator('#sample-list')).to_contain_text('Zustand unbekannt')
        with patch.object(server, 'get_entities', side_effect=RuntimeError('Synthetic outage')):
            try:
                server.sample()
            except RuntimeError:
                pass
        self.page.evaluate('stats()')
        expect(self.page.locator('#acquisition-status')).to_contain_text('Home Assistant nicht erreichbar')
        self.page.get_by_role('button', name='1 Räume').click()
        self.page.get_by_role('button', name='Bearbeiten', exact=True).click()
        self.page.get_by_role('button', name='Raum speichern', exact=True).click()
        expect(self.page.locator('#notice')).to_contain_text('Konfiguration gespeichert')
        expect(self.page.locator('#acquisition-status')).to_contain_text('Home Assistant nicht erreichbar')

    def test_legacy_room_id_is_not_executed(self):
        cfg = self.config()
        cfg['rooms'][0]['id'] = "');window.__legacyInjected=true;//"
        with server.connect() as connection:
            connection.execute('UPDATE config SET value=? WHERE id=1', (json.dumps(cfg),))
        server.setup()  # simulate legacy data present at start-up (lenient v1 -> v2 merge)
        self.page.goto(self.url)
        self.page.get_by_role('button', name='1 Räume').click()
        self.page.get_by_role('button', name='Bearbeiten', exact=True).click()
        expect(self.page.locator('#room-editor')).to_be_visible()
        self.assertFalse(self.page.evaluate('Boolean(window.__legacyInjected)'))
        self.page.get_by_role('button', name='Abbrechen', exact=True).click()
        self.page.once('dialog', lambda dialog: dialog.accept())
        self.page.get_by_role('button', name='Löschen', exact=True).click()
        expect(self.page.locator('#room-list')).to_contain_text('Keine Räume angelegt')
        self.assertFalse(self.page.evaluate('Boolean(window.__legacyInjected)'))

    def test_search_selection_persists_and_mobile_layout(self):
        self.config()
        self.page.goto(self.url)
        self.page.get_by_role('button', name='1 Räume').click()
        self.page.get_by_role('button', name='Bearbeiten', exact=True).click()
        picker = self.page.locator('#r-temperature')
        picker.locator('summary').click()
        search = picker.get_by_role('searchbox')
        search.fill('room_b')
        picker.get_by_role('checkbox').check()
        search.fill('Alpha')
        expect(picker.get_by_role('checkbox')).to_be_checked()
        search.fill('')
        expect(picker.get_by_role('checkbox', checked=True)).to_have_count(2)
        self.page.locator('#sec-emitters > summary').click()  # AB-009: emitters live in collapsible cards
        self.page.locator('details.emitter > summary').first.click()
        single = self.page.locator('.rad-entity')
        single.get_by_role('searchbox').fill('second')
        expect(single.get_by_role('combobox')).to_have_value('number.valve')
        single.get_by_role('combobox').select_option('number.second')
        single.get_by_role('searchbox').fill('Alpha')
        expect(single.get_by_role('combobox')).to_have_value('number.second')
        self.page.get_by_role('button', name='Raum speichern', exact=True).click()
        expect(self.page.locator('#notice')).to_contain_text('Konfiguration gespeichert')
        server.setup()  # Same database is reopened; persisted config remains authoritative.
        saved = server.load()
        self.assertEqual(set(saved['rooms'][0]['temperature_sensors']), {'sensor.room_a', 'sensor.room_b'})
        self.assertEqual(saved['rooms'][0]['radiators'][0]['entity'], 'number.second')
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.page.reload()
        expect(self.page.locator('#acquisition-status')).to_be_visible()
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))


if __name__ == '__main__':
    unittest.main()
