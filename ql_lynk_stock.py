#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""领克商城库存监控（青龙面板单次任务版）。

建议在青龙中每分钟执行一次：
    */1 * * * *  python3 ql_lynk_stock.py

必需环境变量：
    PUSH_MAGICPUSH_URL       魔法推送 API 地址
    PUSH_MAGICPUSH_TOKEN     魔法推送 Bearer Token

可选环境变量：
    LYNK_STOCK_ITEM_ID       商品 ID；多个商品时一行一个
    LYNK_STOCK_REGION        省,市,区编码，默认 110000,110100,110101
    LYNK_STOCK_STATE_FILE    状态文件路径
    LYNK_STOCK_CONFIRMATIONS 连续有货确认次数，默认 2
    LYNK_CONFIG_FILE         复用现有 config.json 的路径

库存查询接口无需 refresh_token 或 device_id。脚本只在“无货 -> 有货”时
发送一次通知；再次售罄后若重新补货，会再次通知。
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


DEFAULT_ITEM_ID = "5310000100239002"
DEFAULT_REGION = "110000,110100,110101"
DEFAULT_CONFIRMATIONS = 2
API_BASE = "https://api-dmall.lynkco.com"
PRODUCT_URL_TEMPLATE = (
    "https://app.lynkco.com/app-h5/dist/web/pages/attachment-mall/"
    "detail/page-web.html?id={item_id}&isShare=1"
)
SCRIPT_DIR = Path(__file__).resolve().parent


def log(level: str, message: str) -> None:
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] [{level}] {message}", flush=True)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def format_duration(start: Optional[str], end: Optional[str]) -> str:
    start_dt = parse_iso(start)
    end_dt = parse_iso(end)
    if not start_dt or not end_dt:
        return "未知"
    seconds = max(0, int((end_dt - start_dt).total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours or days:
        parts.append(f"{hours}小时")
    if minutes or hours or days:
        parts.append(f"{minutes}分钟")
    if not parts:
        parts.append(f"{seconds}秒")
    return "".join(parts)


def parse_region(raw: str) -> Tuple[str, str, str]:
    values = [part.strip() for part in raw.split(",")]
    if len(values) != 3 or not all(value.isdigit() for value in values):
        raise ValueError("LYNK_STOCK_REGION 必须是 省,市,区 三个数字编码")
    return values[0], values[1], values[2]


def parse_item_ids(raw: str) -> List[str]:
    """解析单个或多行商品 ID，同时兼容逗号分隔并保持原顺序。"""
    values: List[str] = []
    for line in raw.replace(",", "\n").splitlines():
        item_id = line.strip()
        if not item_id:
            continue
        if not item_id.isdigit():
            raise ValueError(f"商品 ID 必须为数字: {item_id}")
        if item_id not in values:
            values.append(item_id)
    if not values:
        raise ValueError("至少需要配置一个商品 ID")
    return values


def sku_label(sku: Dict[str, Any]) -> str:
    attributes = sku.get("attributes") or []
    values = [str(attr.get("attrVal", "")).strip() for attr in attributes]
    values = [value for value in values if value]
    return " / ".join(values) or str(sku.get("name") or sku.get("id") or "未知规格")


def fetch_inventory(
    item_id: str,
    region: Tuple[str, str, str],
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """读取页面同源接口，并以页面使用的 selectedSkuStock 为准。"""
    client = session or requests.Session()
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0 LynkStockMonitor/1.0",
    }
    basic_url = f"{API_BASE}/portal/api/item/render/static/basic/{item_id}"
    response = client.get(basic_url, headers=headers, timeout=15)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success") or not isinstance(payload.get("data"), dict):
        raise RuntimeError(f"商品基础接口返回异常: {str(payload)[:300]}")

    data = payload["data"]
    item = data.get("item") or {}
    skus = data.get("skuList") or []
    if not skus:
        raise RuntimeError("商品接口未返回任何 SKU")

    province_id, city_id, region_id = region
    results: List[Dict[str, Any]] = []
    errors: List[str] = []
    for sku in skus:
        sku_id = str(sku.get("id") or "")
        if not sku_id:
            errors.append("发现缺少 ID 的 SKU")
            continue
        params = {
            "provinceId": province_id,
            "cityId": city_id,
            "regionId": region_id,
            "itemId": item_id,
            "skuId": sku_id,
        }
        try:
            dynamic_url = f"{API_BASE}/portal/api/item/render/dynamic"
            dynamic_response = client.get(
                dynamic_url, params=params, headers=headers, timeout=15
            )
            dynamic_response.raise_for_status()
            dynamic_payload = dynamic_response.json()
            if not dynamic_payload.get("success") or not isinstance(
                dynamic_payload.get("data"), dict
            ):
                raise RuntimeError(str(dynamic_payload)[:200])
            dynamic_data = dynamic_payload["data"]
            stock = int(dynamic_data.get("selectedSkuStock") or 0)
            results.append(
                {
                    "sku_id": sku_id,
                    "label": sku_label(sku),
                    "stock": stock,
                    "price": int(dynamic_data.get("price") or sku.get("price") or 0),
                }
            )
        except (requests.RequestException, ValueError, TypeError, RuntimeError) as exc:
            errors.append(f"SKU {sku_id}: {exc}")

    in_stock = any(result["stock"] > 0 for result in results)
    if not in_stock and errors:
        raise RuntimeError("库存检查不完整，拒绝判定为无货: " + "; ".join(errors))

    return {
        "item_id": item_id,
        "item_name": str(item.get("name") or f"商品 {item_id}"),
        "item_status": item.get("status"),
        "checked_at": now_iso(),
        "in_stock": in_stock,
        "skus": results,
        "errors": errors,
    }


def expand_state_file(raw_path: str, item_id: str, multiple: bool) -> Path:
    """将自定义路径展开成每个商品独立的状态文件。"""
    if "{item_id}" in raw_path:
        return Path(raw_path.replace("{item_id}", item_id)).expanduser()

    path = Path(raw_path).expanduser()
    if not multiple:
        return path
    if path.suffix:
        return path.with_name(f"{path.stem}_{item_id}{path.suffix}")
    return path / f"lynk_stock_{item_id}.json"


def default_state_file(item_id: str, multiple: bool = False) -> Path:
    explicit = os.environ.get("LYNK_STOCK_STATE_FILE", "").strip()
    if explicit:
        return expand_state_file(explicit, item_id, multiple)

    ql_data_dir = os.environ.get("QL_DATA_DIR", "").strip()
    candidates = [Path(ql_data_dir)] if ql_data_dir else []
    candidates.extend([Path("/ql/data"), SCRIPT_DIR])
    for directory in candidates:
        if directory.is_dir() and os.access(str(directory), os.W_OK):
            return directory / f"lynk_stock_{item_id}.json"
    return SCRIPT_DIR / f"lynk_stock_{item_id}.json"


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        log("WARN", f"状态文件读取失败，将重新初始化: {exc}")
        return {}


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def config_candidates() -> List[Path]:
    paths: List[Path] = []
    explicit = os.environ.get("LYNK_CONFIG_FILE", "").strip()
    if explicit:
        paths.append(Path(explicit).expanduser())
    paths.extend([Path("/ql/data/config.json"), SCRIPT_DIR / "config.json"])
    unique: List[Path] = []
    for path in paths:
        if path not in unique:
            unique.append(path)
    return unique


def get_magicpush_config() -> Tuple[str, str]:
    url = os.environ.get("PUSH_MAGICPUSH_URL", "").strip()
    token = os.environ.get("PUSH_MAGICPUSH_TOKEN", "").strip()
    if url and token:
        return url, token

    for path in config_candidates():
        if not path.is_file():
            continue
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
            push = config.get("push") or {}
            url = url or str(push.get("magicpush_url") or "").strip()
            token = token or str(push.get("magicpush_token") or "").strip()
            if url and token:
                return url, token
        except (OSError, ValueError, AttributeError):
            continue
    return url, token


def push_magic(url: str, token: str, title: str, content: str) -> None:
    if not url or not token:
        raise RuntimeError(
            "未配置 PUSH_MAGICPUSH_URL 和 PUSH_MAGICPUSH_TOKEN"
        )
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json={"title": title, "content": content, "type": "markdown"},
        timeout=10,
    )
    response.raise_for_status()


def build_notification(snapshot: Dict[str, Any], state: Dict[str, Any]) -> Tuple[str, str]:
    stocked = [sku for sku in snapshot["skus"] if sku["stock"] > 0]
    lines = [
        f"**{snapshot['item_name']} 已恢复库存**",
        "",
    ]
    for sku in stocked:
        price = sku.get("price") or 0
        price_text = f"，价格 ¥{price / 100:.2f}" if price else ""
        lines.append(
            f"- **{sku['label']}**：库存 `{sku['stock']}`{price_text}"
        )
    lines.extend(
        [
            "",
            f"**缺货持续**：{format_duration(state.get('out_of_stock_since'), state.get('detected_in_stock_at'))}",
            f"**发现时间**：{state.get('detected_in_stock_at')}",
            "",
            f"[立即查看商品]({PRODUCT_URL_TEMPLATE.format(item_id=snapshot['item_id'])})",
        ]
    )
    return "领克商城商品有货", "\n".join(lines)


def build_status_report(
    snapshots: List[Dict[str, Any]], errors: List[Tuple[str, str]]
) -> str:
    lines = ["**当前商品库存状态（测试推送）**", ""]
    for snapshot in snapshots:
        status = "✅ 有货" if snapshot["in_stock"] else "❌ 无货"
        lines.extend(
            [
                f"### {snapshot['item_name']}",
                f"- 商品 ID：`{snapshot['item_id']}`",
                f"- 当前状态：**{status}**",
            ]
        )
        for sku in snapshot["skus"]:
            lines.append(f"- {sku['label']}：库存 `{sku['stock']}`")
        lines.extend(
            [
                f"- 检查时间：{snapshot['checked_at']}",
                f"- [查看商品]({PRODUCT_URL_TEMPLATE.format(item_id=snapshot['item_id'])})",
                "",
            ]
        )

    for item_id, error in errors:
        lines.extend(
            [
                f"### 商品 {item_id}",
                "- 当前状态：**⚠️ 查询失败**",
                f"- 错误：`{error[:300]}`",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def run_test_push(item_ids: List[str]) -> int:
    region = parse_region(os.environ.get("LYNK_STOCK_REGION", DEFAULT_REGION))
    snapshots: List[Dict[str, Any]] = []
    errors: List[Tuple[str, str]] = []
    for index, item_id in enumerate(item_ids, 1):
        log("INFO", f"测试推送查询商品 {index}/{len(item_ids)}: {item_id}")
        try:
            snapshots.append(fetch_inventory(item_id, region))
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            errors.append((item_id, str(exc)))
            log("ERR", f"商品 {item_id} 状态查询失败: {exc}")

    url, token = get_magicpush_config()
    content = build_status_report(snapshots, errors)
    push_magic(url, token, "领克商品库存状态", content)
    log("OK", f"魔法推送测试成功，已发送 {len(item_ids)} 个商品的当前状态")
    return 1 if errors else 0


def run_check(item_id: str, state_file: Path, confirmations: int) -> int:
    region = parse_region(os.environ.get("LYNK_STOCK_REGION", DEFAULT_REGION))
    snapshot = fetch_inventory(item_id, region)
    current_time = snapshot["checked_at"]
    state = load_state(state_file)

    if state.get("item_id") != item_id:
        state = {
            "item_id": item_id,
            "monitor_started_at": current_time,
            "out_of_stock_since": current_time,
            "positive_streak": 0,
            "notification_sent_for_current_stock": False,
        }

    previous_in_stock = bool(state.get("in_stock"))
    state["item_name"] = snapshot["item_name"]
    state["last_checked_at"] = current_time
    state["last_skus"] = snapshot["skus"]
    state["in_stock"] = snapshot["in_stock"]

    if snapshot["in_stock"]:
        state["positive_streak"] = int(state.get("positive_streak") or 0) + 1
        if not previous_in_stock:
            state["detected_in_stock_at"] = current_time
            state["notification_sent_for_current_stock"] = False

        stocked_text = ", ".join(
            f"{sku['label']}={sku['stock']}"
            for sku in snapshot["skus"]
            if sku["stock"] > 0
        )
        log(
            "OK",
            f"发现有货: {stocked_text}；连续确认 {state['positive_streak']}/{confirmations}",
        )

        if (
            state["positive_streak"] >= confirmations
            and not state.get("notification_sent_for_current_stock")
        ):
            url, token = get_magicpush_config()
            title, content = build_notification(snapshot, state)
            try:
                push_magic(url, token, title, content)
                state["notification_sent_for_current_stock"] = True
                state["last_notified_at"] = current_time
                log("OK", "魔法推送通知发送成功")
            except (requests.RequestException, RuntimeError) as exc:
                save_state(state_file, state)
                log("ERR", f"魔法推送失败，下次任务会重试: {exc}")
                return 2
    else:
        if previous_in_stock or not state.get("out_of_stock_since"):
            state["out_of_stock_since"] = current_time
        state["positive_streak"] = 0
        state["detected_in_stock_at"] = None
        state["notification_sent_for_current_stock"] = False
        sku_text = ", ".join(
            f"{sku['label']}={sku['stock']}" for sku in snapshot["skus"]
        )
        waited = format_duration(state.get("out_of_stock_since"), current_time)
        log("INFO", f"仍然无货: {sku_text}；已监控 {waited}")

    save_state(state_file, state)
    log("INFO", f"状态已保存: {state_file}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="领克商城库存监控（青龙单次任务）")
    parser.add_argument(
        "--item-id",
        default=os.environ.get("LYNK_STOCK_ITEM_ID", DEFAULT_ITEM_ID).strip(),
        help="领克商城商品 ID；多个 ID 可换行或逗号分隔",
    )
    parser.add_argument(
        "--state-file",
        help="状态文件路径，默认优先使用 /ql/data",
    )
    parser.add_argument(
        "--confirmations",
        type=int,
        default=int(
            os.environ.get(
                "LYNK_STOCK_CONFIRMATIONS", str(DEFAULT_CONFIRMATIONS)
            )
        ),
        help="连续检测到有货多少次后推送，默认 2",
    )
    parser.add_argument(
        "--test-push",
        action="store_true",
        help="查询全部商品当前库存并发送一条测试通知，不修改监控状态",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        item_ids = parse_item_ids(args.item_id)
    except ValueError as exc:
        log("ERR", str(exc))
        return 2
    if args.confirmations < 1:
        log("ERR", "连续确认次数必须大于等于 1")
        return 2

    if args.test_push:
        try:
            return run_test_push(item_ids)
        except (requests.RequestException, RuntimeError) as exc:
            log("ERR", f"魔法推送测试失败: {exc}")
            return 2

    multiple = len(item_ids) > 1
    result = 0
    for index, item_id in enumerate(item_ids, 1):
        state_file = (
            expand_state_file(args.state_file, item_id, multiple)
            if args.state_file
            else default_state_file(item_id, multiple)
        )
        log("INFO", f"检查商品 {index}/{len(item_ids)}: {item_id}")
        try:
            item_result = run_check(item_id, state_file, args.confirmations)
            result = max(result, item_result)
        except (requests.RequestException, RuntimeError, ValueError, OSError) as exc:
            log("ERR", f"商品 {item_id} 库存检查失败: {exc}")
            result = max(result, 1)
    return result


if __name__ == "__main__":
    sys.exit(main())
