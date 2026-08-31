import base64
import hashlib
import hmac
import os
import tempfile
import unittest
from unittest import mock

import ql_lynk
import config_server


class LynkScriptTests(unittest.TestCase):
    def test_native_signature_matches_gateway_canonical_form(self):
        headers = ql_lynk.build_native_sig("POST", ql_lynk.EP_SIGN, b"{}")
        self.assertEqual(headers["content-md5"], "mZFLkyvTelC5g8XnyQrpOw==")
        self.assertEqual(
            headers["x-ca-signature-headers"],
            "x-ca-nonce,x-ca-key,x-ca-timestamp",
        )
        parts = [
            "POST",
            headers["accept"],
            headers["content-md5"],
            headers["content-type"],
            headers["date"],
            f"x-ca-key:{headers['x-ca-key']}",
            f"x-ca-nonce:{headers['x-ca-nonce']}",
            f"x-ca-timestamp:{headers['x-ca-timestamp']}",
            ql_lynk.EP_SIGN,
        ]
        expected = base64.b64encode(
            hmac.new(
                ql_lynk.SIGN_CA_SECRET.encode(),
                "\n".join(parts).encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        self.assertEqual(headers["x-ca-signature"], expected)

    def test_cache_defaults_next_to_config_on_windows_and_local_runs(self):
        original = ql_lynk.CONFIG_FILE
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                ql_lynk.CONFIG_FILE = os.path.join(temp_dir, "config.json")
                with mock.patch.dict(os.environ, {"LYNK_CACHE_FILE": ""}):
                    self.assertEqual(
                        ql_lynk.get_cache_file(),
                        os.path.join(temp_dir, "lynk_access_token.json"),
                    )
        finally:
            ql_lynk.CONFIG_FILE = original

    def test_sensitive_query_values_are_redacted(self):
        error = "https://example.test/refresh?refreshToken=bearer12345678-1234-1234-1234-123456789abc&deviceId=ok"
        redacted = ql_lynk.redact_sensitive(error)
        self.assertNotIn("12345678-1234", redacted)
        self.assertIn("refreshToken=<redacted>", redacted)

    def test_response_ok_accepts_both_api_shapes(self):
        self.assertTrue(ql_lynk.response_ok({"code": "200"}))
        self.assertTrue(ql_lynk.response_ok({"success": True}))
        self.assertFalse(ql_lynk.response_ok({"code": 400}))

    def test_visit_share_url_is_anonymous_and_checks_main_account_energy(self):
        response = mock.Mock(status_code=200)
        response.raise_for_status.return_value = None
        with mock.patch.object(ql_lynk, "lynk_energy", side_effect=[
            {"code": "success", "data": {"point": 100}},
            {"code": "success", "data": {"point": 105}},
        ]), mock.patch.object(ql_lynk.requests, "get", return_value=response) as get, mock.patch.object(
            ql_lynk.time, "sleep"
        ):
            result = ql_lynk.visit_share_url("https://h5.example.test/share?shareCode=abc", "main-access-token")

        self.assertTrue(result["ok"])
        self.assertEqual(result["energy_delta"], 5)
        _, kwargs = get.call_args
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertNotIn("token", {key.lower() for key in kwargs["headers"]})
        self.assertTrue(kwargs["allow_redirects"])

    def test_visit_share_url_reports_http_failure(self):
        with mock.patch.object(ql_lynk, "lynk_energy", return_value={}), mock.patch.object(
            ql_lynk.requests, "get", side_effect=ql_lynk.requests.ConnectionError("offline")
        ):
            result = ql_lynk.visit_share_url("https://h5.example.test/share", "main-access-token")

        self.assertFalse(result["ok"])
        self.assertIn("匿名访问失败", result["msg"])

    def test_deprecated_b_token_is_not_returned_or_saved(self):
        original = config_server.CONFIG_FILE
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                config_server.CONFIG_FILE = os.path.join(temp_dir, "config.json")
                with open(config_server.CONFIG_FILE, "w", encoding="utf-8") as config_file:
                    config_file.write('{"refresh_token":"main","token_b":["secret-b"]}')

                loaded = config_server.read_config()
                self.assertNotIn("token_b", loaded)
                saved = config_server.write_config(loaded)
                self.assertNotIn("token_b", saved)
                with open(config_server.CONFIG_FILE, "r", encoding="utf-8") as config_file:
                    self.assertNotIn("token_b", config_file.read())
        finally:
            config_server.CONFIG_FILE = original

    def test_magicpush_uses_bearer_token_and_markdown_payload(self):
        response = mock.Mock(ok=True)
        env = {
            "PUSH_MAGICPUSH_URL": "https://push.example.test/api/push",
            "PUSH_MAGICPUSH_TOKEN": "secret-token",
        }
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            ql_lynk.requests, "post", return_value=response
        ) as post:
            result = ql_lynk.push_text("签到成功", "**获得 10 积分**")

        self.assertEqual(result, "魔法推送: OK")
        post.assert_called_once_with(
            "https://push.example.test/api/push",
            headers={"Authorization": "Bearer secret-token"},
            json={"title": "签到成功", "content": "**获得 10 积分**", "type": "markdown"},
            timeout=10,
        )

    def test_magicpush_requires_both_url_and_token(self):
        with mock.patch.dict(
            os.environ,
            {"PUSH_MAGICPUSH_URL": "https://push.example.test/api/push"},
            clear=True,
        ), mock.patch.object(ql_lynk.requests, "post") as post:
            self.assertEqual(ql_lynk.push_text("标题", "内容"), "(未配置推送渠道)")
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
