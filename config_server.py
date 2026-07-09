#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
领克签到 · 可视化配置 + cron 调度 + 日志服务器 (Docker 版)
====================================================
一个进程搞定三件事:
  1. Web 配置页 (编辑 config.json, 含 cron 表达式)
  2. 后台 cron 调度线程 (按表达式定时跑 ql_lynk.py)
  3. 运行日志: 每次运行一个日志文件, 保留最近 35 天, 页面可查看

用法:
  python3 config_server.py                    # 127.0.0.1:8787
  python3 config_server.py --host 0.0.0.0     # Docker 里用这个
  python3 config_server.py --no-scheduler     # 只开配置页, 不跑调度

HTTP 接口:
  GET  /                 配置页
  GET  /api/config       读取 config.json
  POST /api/config       写入 config.json
  GET  /api/logs         运行日志列表
  GET  /api/logs/<name>  某次运行的日志内容
  POST /api/run          立即触发一次运行
  GET  /api/status       调度器状态 (下次运行时间 / 是否运行中)
"""

import os
import re
import sys
import json
import time
import threading
import subprocess
import argparse
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("LYNK_CONFIG_FILE", "").strip() or os.path.join(BASE_DIR, "config.json")
HTML_FILE = os.path.join(BASE_DIR, "config.html")
LOGS_DIR = os.environ.get("LYNK_LOGS_DIR", "").strip() or os.path.join(BASE_DIR, "logs")
SCRIPT = os.path.join(BASE_DIR, "ql_lynk.py")

LOG_RETENTION_DAYS = 35
LOG_NAME_RE = re.compile(r"^run-\d{4}-\d{2}-\d{2}_\d{6}(?:-\w+)?\.log$")

# ==================== 时区 ====================
# 优先用 zoneinfo (需系统或 pip 的 tzdata); 拿不到时退化成固定偏移, 默认 +8 (中国),
# 可用环境变量 LYNK_TZ_OFFSET 覆盖 (小时). 这样即使镜像里没有任何时区数据库也能正确运行.
def _resolve_tz():
    tzname = os.environ.get("TZ", "Asia/Shanghai")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tzname)
    except Exception:
        try:
            offset = float(os.environ.get("LYNK_TZ_OFFSET", "8"))
        except ValueError:
            offset = 8
        return timezone(timedelta(hours=offset))


LOCAL_TZ = _resolve_tz()


def now():
    """当前时间 (按 TZ 时区). 供 cron 匹配 / 日志时间戳统一使用."""
    return datetime.now(LOCAL_TZ)


# ==================== 轻量 cron (无第三方依赖) ====================
def _cron_field(expr, lo, hi):
    """把单个 cron 字段展开成允许值集合. 支持 * , - / 组合."""
    vals = set()
    for part in expr.split(","):
        part = part.strip()
        step = 1
        rng = part
        if "/" in part:
            rng, s = part.split("/", 1)
            step = int(s)
        if rng in ("*", ""):
            start, end = lo, hi
        elif "-" in rng:
            a, b = rng.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(rng)
        if start < lo or end > hi or step < 1:
            raise ValueError(f"字段越界: {part}")
        for v in range(start, end + 1, step):
            vals.add(v)
    return vals


def cron_is_valid(expr):
    try:
        f = expr.split()
        if len(f) != 5:
            return False
        _cron_field(f[0], 0, 59)
        _cron_field(f[1], 0, 23)
        _cron_field(f[2], 1, 31)
        _cron_field(f[3], 1, 12)
        _cron_field(f[4], 0, 7)
        return True
    except Exception:
        return False


def cron_match(expr, dt):
    """标准 5 段 cron 是否匹配某个时间 (分 时 日 月 周; 周 0/7=周日)."""
    f = expr.split()
    if len(f) != 5:
        return False
    if dt.minute not in _cron_field(f[0], 0, 59):
        return False
    if dt.hour not in _cron_field(f[1], 0, 23):
        return False
    if dt.month not in _cron_field(f[3], 1, 12):
        return False
    dows = _cron_field(f[4], 0, 7)
    if 7 in dows:
        dows.add(0)
    dow = (dt.weekday() + 1) % 7  # 周一=1 … 周六=6, 周日=0
    dom_restricted, dow_restricted = f[2] != "*", f[4] != "*"
    dom_ok = dt.day in _cron_field(f[2], 1, 31)
    dow_ok = dow in dows
    # Vixie cron: 日和周都限定时取"或", 否则取被限定的那个
    if dom_restricted and dow_restricted:
        return dom_ok or dow_ok
    if dom_restricted:
        return dom_ok
    if dow_restricted:
        return dow_ok
    return True


def cron_next(expr, base):
    """从 base 之后逐分钟找下一个匹配时间 (上限 366 天)."""
    t = (base + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(366 * 24 * 60):
        if cron_match(expr, t):
            return t
        t += timedelta(minutes=1)
    return None

# config.json 缺失时的默认结构
DEFAULT_CONFIG = {
    "refresh_token": "",
    "device_id": "",
    "cron": "0 9 * * *",
    "auto_share": False,
    "token_b": [],
    "share_content_id": "2072260486405246976",
    "push": {
        "wecom_webhook": "",
        "dingtalk_webhook": "",
        "feishu_webhook": "",
        "tg_bot_token": "",
        "tg_chat_id": "",
        "serverchan_key": "",
        "pushplus_token": "",
        "bark_url": "",
    },
}

# 调度器运行状态 (供 /api/status 读取)
_run_lock = threading.Lock()
_state = {"running": False, "last_start": None, "last_finish": None, "last_rc": None, "last_log": None}


# ==================== config.json 读写 ====================
def read_config():
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            merged = dict(DEFAULT_CONFIG)
            merged.update(data)
            merged["push"] = {**DEFAULT_CONFIG["push"], **(data.get("push") or {})}
            return merged
        except Exception as e:
            print(f"[!] 解析 {CONFIG_FILE} 失败, 用默认值: {e}")
    return dict(DEFAULT_CONFIG)


def write_config(data):
    """只保留已知字段并原子写入 (临时文件 + os.replace)."""
    clean = {
        "refresh_token": str(data.get("refresh_token", "")).strip(),
        "device_id": str(data.get("device_id", "")).strip(),
        "cron": str(data.get("cron", "") or DEFAULT_CONFIG["cron"]).strip(),
        "auto_share": bool(data.get("auto_share", False)),
        "token_b": [str(t).strip() for t in (data.get("token_b") or []) if str(t).strip()],
        "share_content_id": str(data.get("share_content_id", "")).strip(),
        "push": {k: str((data.get("push") or {}).get(k, "")).strip() for k in DEFAULT_CONFIG["push"]},
    }
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)
    return clean


# ==================== 运行 + 日志 ====================
def prune_logs():
    """删除超过 LOG_RETENTION_DAYS 天的日志."""
    if not os.path.isdir(LOGS_DIR):
        return
    cutoff = time.time() - LOG_RETENTION_DAYS * 86400
    for name in os.listdir(LOGS_DIR):
        if not LOG_NAME_RE.match(name):
            continue
        p = os.path.join(LOGS_DIR, name)
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        except Exception:
            pass


def run_job(trigger="cron"):
    """跑一次 ql_lynk.py, 输出写到带时间戳的日志文件. 同一时刻只允许一个."""
    if not _run_lock.acquire(blocking=False):
        return None, "已有任务在运行中, 跳过本次"

    os.makedirs(LOGS_DIR, exist_ok=True)
    ts = now().strftime("%Y-%m-%d_%H%M%S")
    log_name = f"run-{ts}-{trigger}.log"
    log_path = os.path.join(LOGS_DIR, log_name)

    _state.update({"running": True, "last_start": now().isoformat(timespec="seconds"),
                   "last_log": log_name, "last_rc": None, "last_finish": None})
    try:
        # 逐行流式写日志: 全程只经过同一个文件对象 f (不混用底层 fd, 避免偏移错乱),
        # 强制子进程 UTF-8 输出避免 Windows gbk 乱码, 运行中即可实时查看日志.
        child_env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        rc = None
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("===== 领克签到运行日志 =====\n")
            f.write(f"触发方式: {trigger}\n")
            f.write(f"开始时间: {now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"{'=' * 40}\n\n")
            f.flush()
            try:
                proc = subprocess.Popen(
                    [sys.executable, SCRIPT],
                    cwd=BASE_DIR,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=child_env,
                )
                # 看门狗: 超过 600s 强杀
                watchdog = threading.Timer(600, proc.kill)
                watchdog.start()
                try:
                    for line in proc.stdout:
                        f.write(line)
                        f.flush()
                    proc.wait()
                    rc = proc.returncode
                finally:
                    watchdog.cancel()
            except Exception as e:
                f.write(f"[!] 启动子进程失败: {type(e).__name__}: {e}\n")
                rc = -1
            f.write(f"\n{'=' * 40}\n")
            f.write(f"结束时间: {now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"===== EXIT CODE: {rc} =====\n")
        _state.update({"last_rc": rc, "last_finish": now().isoformat(timespec="seconds")})
        return rc, log_name
    finally:
        _state["running"] = False
        _run_lock.release()
        prune_logs()


def log_exit_code(path):
    """从日志尾部解析 EXIT CODE, 返回 int 或 None (运行中)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tail = f.read()[-400:]
        m = re.search(r"EXIT CODE:\s*(-?\d+)", tail)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def list_logs():
    if not os.path.isdir(LOGS_DIR):
        return []
    out = []
    for name in os.listdir(LOGS_DIR):
        if not LOG_NAME_RE.match(name):
            continue
        p = os.path.join(LOGS_DIR, name)
        try:
            st = os.stat(p)
        except Exception:
            continue
        rc = log_exit_code(p)
        out.append({
            "name": name,
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "trigger": name.rsplit("-", 1)[-1].replace(".log", "") if "-" in name else "?",
            "exit_code": rc,
            "status": "running" if rc is None else ("ok" if rc == 0 else "fail"),
        })
    out.sort(key=lambda x: x["name"], reverse=True)
    return out


# ==================== cron 调度线程 ====================
def scheduler_loop():
    """每 20s 检查一次: 当前分钟是否匹配 config.json 里的 cron 表达式."""
    print("[INFO] cron 调度器已启动")
    last_run_minute = None
    while True:
        try:
            cur = now().replace(second=0, microsecond=0)
            cfg = read_config()
            cron = (cfg.get("cron") or "").strip()
            if cron and cron_is_valid(cron):
                if cron_match(cron, cur) and cur != last_run_minute:
                    last_run_minute = cur
                    print(f"[INFO] cron 命中 {cur.strftime('%H:%M')} → 触发运行")
                    threading.Thread(target=run_job, args=("cron",), daemon=True).start()
        except Exception as e:
            print(f"[!] 调度器异常: {e}")
        time.sleep(20)


def next_run_time():
    try:
        cfg = read_config()
        cron = (cfg.get("cron") or "").strip()
        if cron and cron_is_valid(cron):
            nxt = cron_next(cron, now())
            return nxt.strftime("%Y-%m-%d %H:%M:%S") if nxt else None
    except Exception:
        pass
    return None


# ==================== HTTP ====================
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html", "/config.html"):
            if not os.path.isfile(HTML_FILE):
                return self._send(500, "config.html 缺失", "text/plain; charset=utf-8")
            with open(HTML_FILE, "r", encoding="utf-8") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if self.path == "/api/config":
            return self._send(200, read_config())
        if self.path == "/api/logs":
            return self._send(200, {"logs": list_logs(), "retention_days": LOG_RETENTION_DAYS})
        if self.path.startswith("/api/logs/"):
            name = self.path[len("/api/logs/"):]
            if not LOG_NAME_RE.match(name):
                return self._send(400, {"ok": False, "error": "非法文件名"})
            p = os.path.join(LOGS_DIR, name)
            if not os.path.isfile(p):
                return self._send(404, {"ok": False, "error": "日志不存在"})
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                return self._send(200, f.read(), "text/plain; charset=utf-8")
        if self.path == "/api/status":
            return self._send(200, {**_state, "next_run": next_run_time()})
        return self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path == "/api/config":
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                write_config(json.loads(raw.decode("utf-8")))
                print(f"[OK] 配置已写入 {CONFIG_FILE}")
                return self._send(200, {"ok": True, "path": CONFIG_FILE, "next_run": next_run_time()})
            except Exception as e:
                return self._send(400, {"ok": False, "error": f"{type(e).__name__}: {e}"})
        if self.path == "/api/run":
            if _state["running"]:
                return self._send(409, {"ok": False, "error": "已有任务在运行中"})
            threading.Thread(target=run_job, args=("manual",), daemon=True).start()
            return self._send(200, {"ok": True, "msg": "已触发, 稍后在日志里查看"})
        return self._send(404, {"ok": False, "error": "not found"})

    def log_message(self, fmt, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description="领克签到配置 + 调度 + 日志服务器")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址 (Docker 用 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8787, help="监听端口 (默认 8787)")
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开浏览器")
    parser.add_argument("--no-scheduler", action="store_true", help="不启动 cron 调度线程")
    args = parser.parse_args()

    if not os.path.isfile(CONFIG_FILE):
        write_config(DEFAULT_CONFIG)
        print(f"[INFO] 已生成默认配置文件: {CONFIG_FILE}")
    os.makedirs(LOGS_DIR, exist_ok=True)
    prune_logs()

    if not args.no_scheduler:
        threading.Thread(target=scheduler_loop, daemon=True).start()

    url = f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{args.port}/"
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("=" * 56)
    print("  领克签到 · 配置中心已启动")
    print(f"  配置文件: {CONFIG_FILE}")
    print(f"  日志目录: {LOGS_DIR}  (保留 {LOG_RETENTION_DAYS} 天)")
    print(f"  cron:     {read_config().get('cron')}   下次: {next_run_time()}")
    print(f"  访问地址: {url}")
    print("  按 Ctrl+C 停止")
    print("=" * 56)
    if not args.no_browser and args.host != "0.0.0.0":
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFO] 已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
