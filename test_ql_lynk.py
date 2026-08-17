import base64
import hashlib
import hmac
import os
import tempfile
import unittest
from unittest import mock

import ql_lynk


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
