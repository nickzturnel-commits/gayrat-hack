import json
import unittest

import happ_limit as hl


class Response:
    def __init__(self, status=200, text=""):
        self.status_code = status
        self.text = text


class FakeClient:
    def __init__(self, get=None, post=None):
        self.get_response = get
        self.post_response = post
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if isinstance(self.get_response, Exception):
            raise self.get_response
        return self.get_response

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if isinstance(self.post_response, Exception):
            raise self.post_response
        return self.post_response


PROVIDER = "Ab12Cd34"
AUTH_KEY = "0123456789ABCDEFghijklmnopQRSTUV"
INSTALL_CODE = "Abcdef123456"
DRIVE_URL = "https://www.googleapis.com/drive/v3/files/file?key=drive-key&alt=media"


class LimitedSourceUrlTests(unittest.TestCase):
    def test_appends_install_id_as_fragment(self):
        self.assertEqual(hl.limited_source_url(DRIVE_URL, INSTALL_CODE),
                         DRIVE_URL + "#?installid=" + INSTALL_CODE)

    def test_rejects_invalid_code_non_https_and_existing_fragment(self):
        invalid = ((DRIVE_URL, "bad code"), (DRIVE_URL, ""),
                   ("http://example.test/profile", INSTALL_CODE),
                   (DRIVE_URL + "#existing", INSTALL_CODE))
        for url, code in invalid:
            with self.subTest(url=url, code=code), self.assertRaises(hl.HappLimitError):
                hl.limited_source_url(url, code)


class InstallCodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_documented_query_and_accepts_valid_install_code(self):
        client = FakeClient(get=Response(200, json.dumps({"rc": 1, "msg": "Ok", "install_code": INSTALL_CODE})))
        code = await hl.create_install_code(client, " " + PROVIDER + " ", " " + AUTH_KEY + " ", 99)
        self.assertEqual(code, INSTALL_CODE)
        method, url, kwargs = client.calls[0]
        self.assertEqual((method, url), ("GET", hl.HAPP_ADD_INSTALL_URL))
        self.assertEqual(kwargs["params"], {"provider_code": PROVIDER, "auth_key": AUTH_KEY, "install_limit": 99})
        self.assertEqual(kwargs["timeout"], hl.REQUEST_TIMEOUT)

    async def test_provider_keys_and_limits_are_validated_before_network_request(self):
        self.assertTrue(hl.is_configured(PROVIDER, AUTH_KEY))
        self.assertFalse(hl.is_configured("short", AUTH_KEY))
        self.assertFalse(hl.is_configured(PROVIDER, "short"))
        client = FakeClient(get=Response(200, json.dumps({"rc": 1, "install_code": INSTALL_CODE})))
        cases = (("", AUTH_KEY, 2), ("short", AUTH_KEY, 2), (PROVIDER, "short", 2),
                 (PROVIDER, AUTH_KEY, 0), (PROVIDER, AUTH_KEY, 100),
                 (PROVIDER, AUTH_KEY, True), (PROVIDER, AUTH_KEY, 2.5))
        for provider, key, limit in cases:
            with self.subTest(provider=provider, limit=limit), self.assertRaises(hl.HappLimitError):
                await hl.create_install_code(client, provider, key, limit)
        self.assertEqual(client.calls, [])

    async def test_transport_and_provider_errors_do_not_leak_credentials_or_urls(self):
        transport = FakeClient(get=RuntimeError(
            "GET https://api.happ-proxy.com/api/add-install?auth_key=" + AUTH_KEY + " failed"))
        with self.assertRaises(hl.HappLimitError) as caught:
            await hl.create_install_code(transport, PROVIDER, AUTH_KEY, 3)
        self.assertNotIn(AUTH_KEY, str(caught.exception))
        self.assertNotIn("happ-proxy", str(caught.exception))

        body = json.dumps({"rc": 0, "msg": "bad key " + AUTH_KEY + " https://private.example/path"})
        rejected = FakeClient(get=Response(200, body))
        with self.assertRaises(hl.HappLimitError) as caught:
            await hl.create_install_code(rejected, PROVIDER, AUTH_KEY, 3)
        self.assertNotIn(AUTH_KEY, str(caught.exception))
        self.assertNotIn("private.example", str(caught.exception))
        self.assertLess(len(str(caught.exception)), 260)

    async def test_rejects_http_malformed_json_and_invalid_install_codes(self):
        responses = (Response(503), Response(200, "<html>"),
                     Response(200, json.dumps({"rc": 1, "install_code": "bad code"})))
        for response in responses:
            with self.subTest(response=response.text), self.assertRaises(hl.HappLimitError):
                await hl.create_install_code(FakeClient(get=response), PROVIDER, AUTH_KEY, 2)


class EncryptTests(unittest.IsolatedAsyncioTestCase):
    def test_extracts_link_from_plain_text_and_nested_json(self):
        link = "happ://crypt5/EncryptedValue"
        payloads = (link, "  " + link + "\n", json.dumps(link),
                    json.dumps({"encrypted_url": link}),
                    json.dumps({"data": {"result": [link]}}),
                    json.dumps({"rc": 1, "link": link}))
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertEqual(hl.extract_encrypted_link(payload), link)
        for payload in ("", "error", json.dumps({"error": "bad"}), "happ://other/x", "happ://crypt5/bad link"):
            with self.subTest(payload=payload):
                self.assertIsNone(hl.extract_encrypted_link(payload))

    async def test_encrypts_json_url_and_returns_happ_link(self):
        link = "happ://crypt5/EncryptedValue"
        client = FakeClient(post=Response(200, json.dumps({"data": {"url": link}})))
        self.assertEqual(await hl.encrypt_link(client, DRIVE_URL), link)
        method, url, kwargs = client.calls[0]
        self.assertEqual((method, url), ("POST", hl.HAPP_CRYPTO_URL))
        self.assertEqual(kwargs["json"], {"url": DRIVE_URL})

    async def test_encrypt_failures_return_safe_messages(self):
        for result in (Response(500), Response(200, "not a link"), OSError("https://crypto.happ.su/private")):
            with self.subTest(result=result), self.assertRaises(hl.HappLimitError) as caught:
                await hl.encrypt_link(FakeClient(post=result), DRIVE_URL)
            self.assertNotIn("crypto.happ.su", str(caught.exception))
            self.assertNotIn(DRIVE_URL, str(caught.exception))


class BuildLimitedLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_creates_counter_then_encrypts_installid_url(self):
        client = FakeClient(
            get=Response(200, json.dumps({"rc": 1, "install_code": INSTALL_CODE})),
            post=Response(200, "happ://crypt5/EncryptedValue"))
        code, link = await hl.build_limited_link(client, PROVIDER, AUTH_KEY, DRIVE_URL, 7)
        self.assertEqual((code, link), (INSTALL_CODE, "happ://crypt5/EncryptedValue"))
        self.assertEqual(client.calls[1][2]["json"],
                         {"url": DRIVE_URL + "#?installid=" + INSTALL_CODE})

    async def test_reuses_saved_counter_without_creating_another(self):
        client = FakeClient(get=RuntimeError("should not be called"),
                            post=Response(200, "happ://crypt5/EncryptedValue"))
        code, _ = await hl.build_limited_link(client, PROVIDER, AUTH_KEY, DRIVE_URL, 7,
                                              existing_code=INSTALL_CODE)
        self.assertEqual(code, INSTALL_CODE)
        self.assertEqual([method for method, _, _ in client.calls], ["POST"])


if __name__ == "__main__":
    unittest.main()
