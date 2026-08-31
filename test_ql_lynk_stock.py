import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ql_lynk_stock


def snapshot(in_stock=False, checked_at="2026-08-26T10:00:00+08:00"):
    return {
        "item_id": ql_lynk_stock.DEFAULT_ITEM_ID,
        "item_name": "Eva智能车载机器人",
        "item_status": 1,
        "checked_at": checked_at,
        "in_stock": in_stock,
        "skus": [
            {
                "sku_id": "5310000100278002",
                "label": "极地白 / 07GT",
                "stock": 3 if in_stock else 0,
                "price": 116900,
            },
            {
                "sku_id": "5310000100278003",
                "label": "高亮黑 / 07GT",
                "stock": 0,
                "price": 116900,
            },
        ],
        "errors": [],
    }


class LynkStockTests(unittest.TestCase):
    def test_item_ids_support_multiline_and_remove_duplicates(self):
        self.assertEqual(
            ql_lynk_stock.parse_item_ids("5310001\n5310002\n5310001\n"),
            ["5310001", "5310002"],
        )

    def test_multiple_items_get_separate_custom_state_files(self):
        self.assertEqual(
            ql_lynk_stock.expand_state_file(
                "/ql/data/stock.json", "5310001", True
            ),
            Path("/ql/data/stock_5310001.json"),
        )
        self.assertEqual(
            ql_lynk_stock.expand_state_file(
                "/ql/data/stock_{item_id}.json", "5310002", True
            ),
            Path("/ql/data/stock_5310002.json"),
        )

    def test_one_failed_item_does_not_block_remaining_items(self):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            sys,
            "argv",
            [
                "ql_lynk_stock.py",
                "--item-id",
                "5310001\n5310002",
                "--state-file",
                str(Path(temp_dir) / "stock_{item_id}.json"),
            ],
        ), mock.patch.object(
            ql_lynk_stock,
            "run_check",
            side_effect=[RuntimeError("first failed"), 0],
        ) as run_check:
            result = ql_lynk_stock.main()

        self.assertEqual(result, 1)
        self.assertEqual(run_check.call_count, 2)
        self.assertEqual(run_check.call_args_list[0].args[0], "5310001")
        self.assertEqual(run_check.call_args_list[1].args[0], "5310002")

    def test_out_of_stock_initializes_state_without_push(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "state.json"
            with mock.patch.object(
                ql_lynk_stock, "fetch_inventory", return_value=snapshot(False)
            ), mock.patch.object(ql_lynk_stock, "push_magic") as push:
                result = ql_lynk_stock.run_check(
                    ql_lynk_stock.DEFAULT_ITEM_ID, state_file, 2
                )

            self.assertEqual(result, 0)
            push.assert_not_called()
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertFalse(state["in_stock"])
            self.assertEqual(state["positive_streak"], 0)
            self.assertEqual(
                state["out_of_stock_since"], "2026-08-26T10:00:00+08:00"
            )

    def test_two_positive_checks_send_only_one_notification(self):
        times = [
            "2026-08-26T10:01:00+08:00",
            "2026-08-26T10:02:00+08:00",
            "2026-08-26T10:03:00+08:00",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "state.json"
            ql_lynk_stock.save_state(
                state_file,
                {
                    "item_id": ql_lynk_stock.DEFAULT_ITEM_ID,
                    "monitor_started_at": "2026-08-26T09:00:00+08:00",
                    "out_of_stock_since": "2026-08-26T09:00:00+08:00",
                    "in_stock": False,
                    "positive_streak": 0,
                    "notification_sent_for_current_stock": False,
                },
            )
            with mock.patch.object(
                ql_lynk_stock,
                "fetch_inventory",
                side_effect=[snapshot(True, value) for value in times],
            ), mock.patch.object(
                ql_lynk_stock,
                "get_magicpush_config",
                return_value=("https://push.example.test/api/push", "token"),
            ), mock.patch.object(ql_lynk_stock, "push_magic") as push:
                first = ql_lynk_stock.run_check(
                    ql_lynk_stock.DEFAULT_ITEM_ID, state_file, 2
                )
                second = ql_lynk_stock.run_check(
                    ql_lynk_stock.DEFAULT_ITEM_ID, state_file, 2
                )
                third = ql_lynk_stock.run_check(
                    ql_lynk_stock.DEFAULT_ITEM_ID, state_file, 2
                )

            self.assertEqual((first, second, third), (0, 0, 0))
            push.assert_called_once()
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertTrue(state["notification_sent_for_current_stock"])
            self.assertEqual(
                state["detected_in_stock_at"], "2026-08-26T10:01:00+08:00"
            )

    def test_failed_push_remains_pending_for_next_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "state.json"
            ql_lynk_stock.save_state(
                state_file,
                {
                    "item_id": ql_lynk_stock.DEFAULT_ITEM_ID,
                    "monitor_started_at": "2026-08-26T09:00:00+08:00",
                    "out_of_stock_since": "2026-08-26T09:00:00+08:00",
                    "in_stock": False,
                    "positive_streak": 0,
                    "notification_sent_for_current_stock": False,
                },
            )
            with mock.patch.object(
                ql_lynk_stock, "fetch_inventory", return_value=snapshot(True)
            ), mock.patch.object(
                ql_lynk_stock,
                "get_magicpush_config",
                return_value=("https://push.example.test/api/push", "token"),
            ), mock.patch.object(
                ql_lynk_stock,
                "push_magic",
                side_effect=RuntimeError("push unavailable"),
            ):
                result = ql_lynk_stock.run_check(
                    ql_lynk_stock.DEFAULT_ITEM_ID, state_file, 1
                )

            self.assertEqual(result, 2)
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertFalse(state["notification_sent_for_current_stock"])

    def test_magicpush_payload_matches_existing_script(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        with mock.patch.object(
            ql_lynk_stock.requests, "post", return_value=response
        ) as post:
            ql_lynk_stock.push_magic(
                "https://push.example.test/api/push",
                "secret-token",
                "商品有货",
                "**库存 3**",
            )

        post.assert_called_once_with(
            "https://push.example.test/api/push",
            headers={"Authorization": "Bearer secret-token"},
            json={"title": "商品有货", "content": "**库存 3**", "type": "markdown"},
            timeout=10,
        )

    def test_test_push_reports_current_status_for_all_items(self):
        first = snapshot(False)
        second = snapshot(True)
        second["item_id"] = "5310000100238004"
        second["item_name"] = "第二个商品"
        second["skus"][0]["label"] = "07 GT"
        with mock.patch.object(
            ql_lynk_stock,
            "fetch_inventory",
            side_effect=[first, second],
        ), mock.patch.object(
            ql_lynk_stock,
            "get_magicpush_config",
            return_value=("https://push.example.test/api/push", "token"),
        ), mock.patch.object(ql_lynk_stock, "push_magic") as push:
            result = ql_lynk_stock.run_test_push(
                [ql_lynk_stock.DEFAULT_ITEM_ID, "5310000100238004"]
            )

        self.assertEqual(result, 0)
        push.assert_called_once()
        title, content = push.call_args.args[2:]
        self.assertEqual(title, "领克商品库存状态")
        self.assertIn("Eva智能车载机器人", content)
        self.assertIn("5310000100239002", content)
        self.assertIn("❌ 无货", content)
        self.assertIn("第二个商品", content)
        self.assertIn("5310000100238004", content)
        self.assertIn("✅ 有货", content)
        self.assertIn("07 GT：库存 `3`", content)


if __name__ == "__main__":
    unittest.main()
