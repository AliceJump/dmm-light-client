"""Exercise Xray installation with release-shaped responses and real ZIP/hash checks."""
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from dmmclient.backend.session import DmmError
from dmmclient.backend.xray import NodeStore


API_URL = 'https://api.github.com/repos/XTLS/Xray-core/releases/latest'
ARCHIVE_URL = 'https://github.com/XTLS/Xray-core/releases/download/v26.11.9/Xray-windows-64.zip'
DIGEST_URL = ARCHIVE_URL + '.dgst'
BOOTSTRAP_PROXY = 'http://127.0.0.1:10808'
EXECUTABLE = b'MZ\x00xray-test-executable'
DEFAULT_DIGEST = object()


def make_zip(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return output.getvalue()


def response(url, content, status=200):
    result = requests.Response()
    result.url = url
    result.status_code = status
    result.encoding = 'utf-8'
    result._content = content if isinstance(content, bytes) else content.encode()
    return result


@contextmanager
def installation(*, api_digest=DEFAULT_DIGEST, dgst_text=None, payload=None,
                 include_dgst=True, failure=None):
    """Avoid installed/bundled cores and network access, but keep the real installer."""
    if payload is None:
        payload = make_zip({'xray.exe': EXECUTABLE})
    checksum = hashlib.sha256(payload).hexdigest()
    asset = {'name': 'Xray-windows-64.zip', 'browser_download_url': ARCHIVE_URL}
    if api_digest is DEFAULT_DIGEST:
        asset['digest'] = 'sha256:' + checksum
    elif api_digest is not None:
        asset['digest'] = api_digest
    assets = [asset]
    if include_dgst:
        assets.append({'name': 'Xray-windows-64.zip.dgst', 'browser_download_url': DIGEST_URL})
    if dgst_text is None:
        dgst_text = 'SHA2-256= ' + checksum + '\n'
    responses = {
        API_URL: response(API_URL, json.dumps({'tag_name': 'v26.11.9', 'assets': assets})),
        ARCHIVE_URL: response(ARCHIVE_URL, payload),
        DIGEST_URL: response(DIGEST_URL, dgst_text),
    }
    if failure is not None:
        url, error = failure
        responses[url] = error

    def get(url, **kwargs):
        result = responses[url]
        if isinstance(result, Exception):
            raise result
        return result

    http = MagicMock()
    http.get.side_effect = get
    http.__enter__.return_value = http
    real_is_file = Path.is_file
    with tempfile.TemporaryDirectory() as folder:
        store = NodeStore(Path(folder))
        # A developer's D:/Programs/v2rayn or packaged vendor core must not skip
        # this test's download path. Checks on the actual target remain real.
        def is_file(path):
            return real_is_file(path) if path == store.exe else False

        with patch('dmmclient.backend.xray.requests.Session', return_value=http), \
                patch.object(Path, 'is_file', autospec=True, side_effect=is_file):
            yield store, http


class XrayInstallTests(unittest.TestCase):
    def test_api_digest_is_used_without_downloading_sidecar(self):
        with installation(dgst_text='not a checksum') as (store, http):
            self.assertEqual(store.install(BOOTSTRAP_PROXY), store.exe)
            self.assertEqual(store.exe.read_bytes(), EXECUTABLE)
            self.assertEqual([call.args[0] for call in http.get.call_args_list],
                             [API_URL, ARCHIVE_URL])
            self.assertFalse(http.trust_env)
            self.assertEqual(http.proxies, {'http': BOOTSTRAP_PROXY, 'https': BOOTSTRAP_PROXY})

    def test_official_and_legacy_sidecar_labels(self):
        payload = make_zip({'xray.exe': EXECUTABLE})
        checksum = hashlib.sha256(payload).hexdigest()
        for label, digest, newline in (
                ('SHA2-256', checksum, '\n'),
                ('SHA256', checksum, '\n'),
                ('sha2-256', checksum.upper(), '\r\n')):
            with self.subTest(label=label, newline=repr(newline)):
                # Mirrors XTLS's OpenSSL .dgst, including other algorithms.
                dgst = newline.join(('MD5= ' + 'a' * 32, 'SHA1= ' + 'b' * 40,
                                     label + '= ' + digest, 'SHA2-512= ' + 'c' * 128, ''))
                with installation(api_digest=None, dgst_text=dgst, payload=payload) as (store, http):
                    store.install(BOOTSTRAP_PROXY)
                    self.assertEqual(store.exe.read_bytes(), EXECUTABLE)
                    self.assertIn(DIGEST_URL, [call.args[0] for call in http.get.call_args_list])

    def test_malformed_api_digest_is_rejected_without_installing(self):
        for digest in ('sha256:' + 'a' * 63, 'sha256:' + 'a' * 65, 'sha256:' + 'g' * 64):
            with self.subTest(digest=digest):
                with installation(api_digest=digest) as (store, _):
                    with self.assertRaises(DmmError):
                        store.install(BOOTSTRAP_PROXY)
                    self.assertFalse(store.exe.exists())

    def test_missing_or_malformed_sidecar_checksum_is_rejected(self):
        for dgst in ('', 'SHA2-512= ' + 'a' * 128,
                     'SHA2-256= ' + 'a' * 63, 'SHA2-256= ' + 'a' * 65,
                     'SHA2-256= ' + 'g' * 64, 'SHA256= ' + 'a' * 64 + ' trailing text',
                     'SHA2-256= ' + 'a' * 64 + '\nSHA256= ' + 'b' * 64):
            with self.subTest(dgst=dgst):
                with installation(api_digest=None, dgst_text=dgst) as (store, _):
                    with self.assertRaises(DmmError):
                        store.install(BOOTSTRAP_PROXY)
                    self.assertFalse(store.exe.exists())

    def test_missing_sidecar_asset_is_rejected(self):
        with installation(api_digest=None, include_dgst=False) as (store, _):
            with self.assertRaises(DmmError) as raised:
                store.install(BOOTSTRAP_PROXY)
            self.assertRegex(str(raised.exception), r'SHA-256|校验')
            self.assertFalse(store.exe.exists())

    def test_invalid_release_json_or_missing_windows_asset_reports_cause(self):
        secret = 'private-server-response'
        for body, cause in (('<html>' + secret + '</html>', 'JSON'),
                            (json.dumps({'assets': []}), 'Windows')):
            with self.subTest(cause=cause):
                with installation(failure=(API_URL, response(API_URL, body))) as (store, _):
                    with self.assertRaises(DmmError) as raised:
                        store.install(BOOTSTRAP_PROXY)
                    text = str(raised.exception)
                    self.assertIn(cause, text)
                    self.assertIn('版本', text)
                    self.assertNotIn(secret, text)
                    self.assertFalse(store.exe.exists())

    def test_hash_mismatch_never_installs_executable(self):
        for digest in ('sha256:' + '0' * 64, None):
            with self.subTest(source='API' if digest else 'sidecar'):
                with installation(api_digest=digest, dgst_text='SHA2-256= ' + '0' * 64) as (store, _):
                    with self.assertRaises(DmmError) as raised:
                        store.install(BOOTSTRAP_PROXY)
                    self.assertIn('SHA-256', str(raised.exception))
                    self.assertIn('不匹配', str(raised.exception))
                    self.assertFalse(store.exe.exists())

    def check_network_failure(self, make_error, message):
        secret = 'private-proxy-password-and-token'
        for url, stage in ((API_URL, r'发布|版本'), (ARCHIVE_URL, r'下载|压缩包'),
                           (DIGEST_URL, r'校验')):
            with self.subTest(url=url):
                with installation(api_digest=None, failure=(url, make_error(secret, url))) as (store, _):
                    with self.assertRaises(DmmError) as raised:
                        store.install(BOOTSTRAP_PROXY)
                    text = str(raised.exception)
                    self.assertIn(message, text)
                    self.assertRegex(text, stage)
                    self.assertNotIn(secret, text)
                    self.assertFalse(store.exe.exists())

    def test_proxy_error_reports_type_and_stage_without_exception_details(self):
        self.check_network_failure(lambda secret, url: requests.exceptions.ProxyError(secret),
                                   '代理连接失败')

    def test_timeout_reports_type_and_stage_without_exception_details(self):
        self.check_network_failure(lambda secret, url: requests.exceptions.Timeout(secret),
                                   '连接超时')

    def test_http_error_reports_status_and_stage_without_exception_details(self):
        def forbidden(secret, url):
            result = response(url, secret, status=403)
            result.reason = secret
            return result
        self.check_network_failure(forbidden, 'HTTP 403')

    def test_invalid_archives_do_not_install_executable(self):
        for payload in (make_zip({'README.md': b'no executable'}),
                        make_zip({'xray.exe': b'not a Windows executable'}), b'not a ZIP'):
            with self.subTest(payload=payload[:20]):
                with installation(payload=payload) as (store, _):
                    with self.assertRaises(DmmError):
                        store.install(BOOTSTRAP_PROXY)
                    self.assertFalse(store.exe.exists())

    def test_existing_core_does_not_download_again(self):
        with installation() as (store, http):
            store.exe.parent.mkdir(parents=True)
            store.exe.write_bytes(EXECUTABLE)
            self.assertEqual(store.install(BOOTSTRAP_PROXY), store.exe)
            http.get.assert_not_called()
            self.assertEqual(store.exe.read_bytes(), EXECUTABLE)


if __name__ == '__main__':
    unittest.main()
