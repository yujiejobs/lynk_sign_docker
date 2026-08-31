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
        error = (
            "https://example.test/refresh?refreshToken=bearer12345678-1234-1234-1234-123456789abc"
            "&shareCode=abcdef1234567890&deviceId=ok"
        )
        redacted = ql_lynk.redact_sensitive(error)
        self.assertNotIn("12345678-1234", redacted)
        self.assertIn("refreshToken=<redacted>", redacted)
        self.assertNotIn("abcdef1234567890", redacted)
        self.assertIn("shareCode=<redacted>", redacted)

    def test_response_ok_accepts_both_api_shapes(self):
        self.assertTrue(ql_lynk.response_ok({"code": "200"}))
        self.assertTrue(ql_lynk.response_ok({"success": True}))
        self.assertFalse(ql_lynk.response_ok({"code": 400}))

    def test_report_share_task_matches_h5_request_and_uses_task_confirmation(self):
        response = mock.Mock(
            status_code=200,
            headers={"content-type": "application/json"},
            text='{"code":"success","data":"true","message":"API调用成功"}',
        )
        response.json.return_value = {"code": "success", "data": "true", "message": "API调用成功"}
        with mock.patch.object(ql_lynk.requests, "post", return_value=response) as post:
            result = ql_lynk.report_share_task(
                "2072260486405246976",
                "abc123",
                share_url="https://h5.example.test/share?shareCode=abc123",
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["msg"], "分享任务已完成 (任务接口确认)")
        self.assertNotIn("energy_delta", result)
        self.assertNotIn("point_delta", result)
        args, kwargs = post.call_args
        self.assertEqual(args[0], ql_lynk.API_BASE + ql_lynk.EP_SHARE_REPORT)
        self.assertEqual(kwargs["params"], {"shareCode": "abc123"})
        self.assertEqual(kwargs["json"], {
            "businessNo": "2072260486405246976",
            "eventData": {"firstClassification": "文章", "secondClassification": ""},
        })
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertNotIn("token", {key.lower() for key in kwargs["headers"]})
        self.assertNotIn("Cookie", kwargs["headers"])
        self.assertEqual(kwargs["headers"]["X-Ca-Key"], ql_lynk.CA_KEY)

    def test_report_share_task_reports_api_failure(self):
        response = mock.Mock(status_code=200, headers={"content-type": "application/json"}, text="")
        response.json.return_value = {"code": "fail", "message": "rejected"}
        with mock.patch.object(ql_lynk.requests, "post", return_value=response):
            result = ql_lynk.report_share_task("2072260486405246976", "abc123")

        self.assertFalse(result["ok"])
        self.assertIn("code=fail", result["msg"])

    def test_report_share_task_requires_true_confirmation_data(self):
        response = mock.Mock(status_code=200, headers={"content-type": "application/json"}, text="")
        response.json.return_value = {"code": "success", "data": "false", "message": "API调用成功"}
        with mock.patch.object(ql_lynk.requests, "post", return_value=response):
            result = ql_lynk.report_share_task("2072260486405246976", "abc123")

        self.assertFalse(result["ok"])
        self.assertFalse(result["confirmed"])
        self.assertIn("data 未确认", result["msg"])

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

    def test_auto_share_switch_persists_true_and_false(self):
        original = config_server.CONFIG_FILE
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                config_server.CONFIG_FILE = os.path.join(temp_dir, "config.json")
                enabled = config_server.write_config({"auto_share": True})
                self.assertIs(enabled["auto_share"], True)
                self.assertIs(config_server.read_config()["auto_share"], True)

                disabled = config_server.write_config({"auto_share": False})
                self.assertIs(disabled["auto_share"], False)
                self.assertIs(config_server.read_config()["auto_share"], False)
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
