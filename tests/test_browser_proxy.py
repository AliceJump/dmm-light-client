import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from dmmclient.backend.browser_proxy import (ProxySettings, enable_system_proxy,
    restore_browser_proxy, restore_system_proxy, system_proxy_active, temporary_browser_proxy)
from dmmclient.backend.session import DmmError


class FakeWindowsProxy:
    def __init__(self, settings):
        self.settings = settings
        self.writes = []

    def get(self):
        return self.settings

    def set(self, settings):
        self.writes.append(settings)
        self.settings = settings


class BrowserProxyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        # Original PAC + auto-detection and DMM bypass must survive a round trip.
        self.original = ProxySettings(13, '127.0.0.1:10808',
            '<local>;*.dmm.com;*.dmm.co.jp;192.168.*', 'https://proxy.example/proxy.pac')
        self.backend = FakeWindowsProxy(self.original)

    def enable(self, proxy='http://127.0.0.1:19080'):
        enable_system_proxy(proxy, self.root, self.backend)

    def restore(self):
        return restore_system_proxy(self.root, self.backend)

    def test_toggle_removes_dmm_bypass_and_restores_pac_and_detection(self):
        self.enable()
        self.assertEqual(self.backend.settings, ProxySettings(3, '127.0.0.1:19080',
            '<local>;192.168.*', self.original.auto_config_url))
        self.assertTrue(system_proxy_active(self.root, self.backend))
        self.assertTrue(self.restore())
        self.assertEqual(self.backend.settings, self.original)
        self.assertFalse(system_proxy_active(self.root, self.backend))

    def test_switching_our_proxy_keeps_the_first_original(self):
        self.enable()
        self.enable('http://127.0.0.1:19081')
        self.assertEqual(self.backend.settings.server, '127.0.0.1:19081')
        self.restore()
        self.assertEqual(self.backend.settings, self.original)

    def test_restore_preserves_external_changes_including_pac(self):
        for change in ({'server': '127.0.0.1:2080'}, {'flags': 5},
                       {'bypass': '<local>;*.example.com'},
                       {'auto_config_url': 'https://other.example/proxy.pac'}):
            with self.subTest(change=change):
                self.backend.settings = self.original
                self.enable()
                external = replace(self.backend.settings, **change)
                self.backend.settings = external
                self.assertFalse(system_proxy_active(self.root, self.backend))
                self.restore()
                self.assertEqual(self.backend.settings, external)
                self.assertFalse((self.root / 'system-browser-proxy.json').exists())

    def test_reenable_after_external_change_backs_up_the_new_configuration(self):
        self.enable()
        external = ProxySettings(1, '127.0.0.1:2080', '<local>')
        self.backend.settings = external
        self.enable()
        self.restore()
        self.assertEqual(self.backend.settings, external)

    def test_socks_urls_are_rejected_before_writing_windows_proxy(self):
        for proxy in ('socks5://127.0.0.1:1080', 'socks5h://127.0.0.1:1080'):
            with self.subTest(proxy=proxy), self.assertRaisesRegex(DmmError, 'HTTP'):
                self.enable(proxy)
        self.assertEqual(self.backend.writes, [])
        self.assertFalse((self.root / 'system-browser-proxy.json').exists())

    def test_failed_notification_after_applying_settings_rolls_back(self):
        original_set = self.backend.set

        def fail_once(settings):
            original_set(settings)
            if len(self.backend.writes) == 1:
                raise DmmError('浏览器代理变更通知失败')

        with patch.object(self.backend, 'set', side_effect=fail_once):
            with self.assertRaisesRegex(DmmError, '通知失败'):
                self.enable()
        self.assertEqual(self.backend.settings, self.original)
        self.assertFalse((self.root / 'system-browser-proxy.json').exists())

    def test_login_and_toggle_restore_in_order_without_fighting(self):
        self.enable()
        manual = self.backend.settings
        with temporary_browser_proxy('http://127.0.0.1:19081', self.root, self.backend) as check:
            check()
            with self.assertRaisesRegex(DmmError, '登录正在'):
                self.restore()
            with self.assertRaisesRegex(DmmError, '登录正在'):
                self.enable()
            self.assertFalse(restore_browser_proxy(self.root, self.backend))
            self.assertEqual(self.backend.settings.server, '127.0.0.1:19081')
        self.assertEqual(self.backend.settings, manual)
        self.assertTrue(system_proxy_active(self.root, self.backend))
        self.restore()
        self.assertEqual(self.backend.settings, self.original)

    def test_login_using_an_already_selected_proxy_still_protects_it(self):
        self.enable()
        with temporary_browser_proxy('http://127.0.0.1:19080', self.root, self.backend) as check:
            journal = self.root / 'temporary-browser-proxy.json'
            self.assertTrue(journal.exists())
            self.assertIn('owner', json.loads(journal.read_text(encoding='utf-8')))
            with self.assertRaisesRegex(DmmError, '登录正在'):
                self.restore()
            # A separate process has no in-memory login marker and must use
            # the journal to notice this active, same-address login.
            writes = len(self.backend.writes)
            with patch('dmmclient.backend.browser_proxy._ACTIVE_LOGINS', set()), \
                    patch('dmmclient.backend.browser_proxy._other_owner_alive', return_value=True):
                with self.assertRaisesRegex(DmmError, '另一个客户端正在登录'):
                    self.restore()
                with self.assertRaisesRegex(DmmError, '另一个客户端正在登录'):
                    self.enable()
            self.assertEqual(len(self.backend.writes), writes)
            check()
        self.assertFalse(journal.exists())
        self.restore()
        self.assertEqual(self.backend.settings, self.original)

    def test_login_reports_external_override_and_preserves_it(self):
        with self.assertRaisesRegex(DmmError, '被其他软件修改'):
            with temporary_browser_proxy('http://127.0.0.1:19080', self.root, self.backend) as check:
                external = ProxySettings(3, '127.0.0.1:2080', '<local>')
                self.backend.settings = external
                check()
        self.assertEqual(self.backend.settings, external)
        self.assertFalse((self.root / 'temporary-browser-proxy.json').exists())
        # The aborted login must not leave an in-process ownership lock behind.
        self.enable()
        self.restore()
        self.assertEqual(self.backend.settings, external)

    def test_recovery_unwinds_interrupted_login_then_system_toggle(self):
        manual = ProxySettings(3, '127.0.0.1:19080', '<local>')
        login = replace(manual, server='127.0.0.1:19081')
        for filename, original, applied in (
                ('system-browser-proxy.json', self.original, manual),
                ('temporary-browser-proxy.json', manual, login)):
            # Journals without owner metadata remain recoverable.
            (self.root / filename).write_text(json.dumps({
                'original': asdict(original), 'applied': asdict(applied)}), encoding='utf-8')
        self.backend.settings = login
        restore_browser_proxy(self.root, self.backend)
        self.restore()
        self.assertEqual(self.backend.settings, self.original)
        self.assertFalse(list(self.root.glob('*browser-proxy.json')))

    def test_legacy_login_journal_without_pac_field_is_recoverable(self):
        original = {'flags': 1, 'server': '', 'bypass': '<local>'}
        applied = {'flags': 3, 'server': '127.0.0.1:19080', 'bypass': '<local>'}
        for pac in ('', 'https://proxy.example/original.pac'):
            with self.subTest(pac=pac):
                journal = self.root / 'temporary-browser-proxy.json'
                journal.write_text(json.dumps({
                    'original': original, 'applied': applied}), encoding='utf-8')
                self.backend.settings = ProxySettings(**applied, auto_config_url=pac)
                restore_browser_proxy(self.root, self.backend)
                self.assertEqual(self.backend.settings, ProxySettings(**original, auto_config_url=pac))
                self.assertFalse(journal.exists())

    def test_another_running_clients_journal_is_not_recovered(self):
        self.enable()
        applied = self.backend.settings
        with patch('dmmclient.backend.browser_proxy._other_owner_alive', return_value=True):
            self.assertFalse(self.restore())
            with self.assertRaisesRegex(DmmError, '另一个客户端'):
                self.enable()
        self.assertEqual(self.backend.settings, applied)
        self.assertTrue((self.root / 'system-browser-proxy.json').exists())


if __name__ == '__main__':
    unittest.main()
