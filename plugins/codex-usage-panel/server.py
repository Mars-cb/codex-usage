#!/usr/bin/env python3
"""Read-only Codex quota MCP plugin; Python 3.7+, no third-party dependencies."""
import argparse
import atexit
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.2.0"
RESOURCE = "ui://codex-usage-panel/v2.html"
ROOT = Path(__file__).resolve().parent


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def normalize(result):
    buckets = result.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        legacy = result.get("rateLimits")
        buckets = {legacy.get("limitId") or "codex": legacy} if isinstance(legacy, dict) else {}
    rows, plans = [], set()
    for key, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        if isinstance(bucket.get("planType"), str):
            plans.add(bucket["planType"])
        for role in ("primary", "secondary"):
            window = bucket.get(role)
            if not isinstance(window, dict):
                continue
            used, minutes, reset = (window.get(k) for k in ("usedPercent", "windowDurationMins", "resetsAt"))
            rows.append({
                "key": str(key) + ":" + role,
                "limitName": bucket.get("limitName") or str(key),
                "windowMinutes": minutes if number(minutes) and minutes > 0 else None,
                "remainingPercent": max(0, min(100, 100 - used)) if number(used) else None,
                "resetsAt": reset if number(reset) and reset > 0 else None,
            })
    rows.sort(key=lambda row: (row["limitName"], row["windowMinutes"] or float("inf"), row["key"]))
    return {"ok": bool(rows), "rows": rows, "plans": sorted(plans),
            "fetchedAt": int(time.time()), "refreshSeconds": 30,
            "source": "本机 Codex 已登录账号", "error": None if rows else "当前账号未返回额度窗口。"}


def find_codex():
    configured = os.environ.get("CODEX_USAGE_BINARY")
    if configured:
        if not Path(configured).is_file() or not os.access(configured, os.X_OK):
            raise RuntimeError("CODEX_USAGE_BINARY 不是可执行文件。")
        return configured
    found = shutil.which("codex")
    if found:
        return found
    candidates = [Path.home() / ".local/opt/chatgpt/usr/lib/chatgpt/resources/codex"]
    # Installed daemon paths change on upgrades; discover them each launch.
    releases = Path.home() / ".codex/packages/app-server-daemon/releases"
    if releases.is_dir():
        def version(path):
            return tuple(int(x) for x in path.parent.parent.name.split("-")[0].split(".") if x.isdigit())
        candidates.extend(sorted(releases.glob("*/bin/codex"), key=version, reverse=True))
    for path in candidates:
        if path.is_file() and os.access(str(path), os.X_OK):
            return str(path)
    raise RuntimeError("未找到 Codex，请设置 CODEX_USAGE_BINARY 为 codex 可执行文件路径。")


class AppServer:
    def __init__(self):
        self.process = None
        self.messages = None
        self.sequence = 0

    def close(self):
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def request(self, method, params=None):
        self.sequence += 1
        ident = self.sequence
        message = {"jsonrpc": "2.0", "id": ident, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            try:
                reply = self.messages.get(timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if reply is None:
                raise RuntimeError("Codex app-server 已退出。")
            if reply.get("id") != ident:
                continue
            if "error" in reply:
                raise RuntimeError("Codex 额度接口返回错误，请检查本机登录状态或客户端版本。")
            return reply.get("result", {})
        raise RuntimeError("读取 Codex 额度超时，请稍后重试。")

    def start(self):
        if self.process is not None and self.process.poll() is None:
            return
        self.close()
        self.messages = messages = queue.Queue()
        self.process = process = subprocess.Popen(
            [find_codex(), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=str(ROOT), bufsize=0)

        def read():
            try:
                for line in process.stdout:
                    try:
                        message = json.loads(line)
                        # Ignore notifications instead of accumulating them without bounds.
                        if isinstance(message, dict) and "id" in message:
                            messages.put(message)
                    except (ValueError, UnicodeError):
                        pass
            finally:
                messages.put(None)
        threading.Thread(target=read, daemon=True).start()
        self.request("initialize", {"clientInfo": {
            "name": "codex-usage-panel", "title": "Codex Usage Panel", "version": VERSION}})
        self.send({"jsonrpc": "2.0", "method": "initialized"})

    def read(self):
        self.start()
        return self.request("account/rateLimits/read")


class Usage:
    def __init__(self):
        self.backend = AppServer()
        self.lock = threading.Lock()
        self.snapshot = None
        self.checked_at = 0
        atexit.register(self.backend.close)

    def read(self):
        with self.lock:
            now = time.monotonic()
            if self.snapshot is not None and now - self.checked_at < 15:
                return self.snapshot
            try:
                self.snapshot = normalize(self.backend.read())
            except Exception as error:
                self.backend.close()
                # Never return old account data after a failed refresh or account switch.
                message = str(error) if isinstance(error, RuntimeError) else "读取额度失败，请检查 Codex 本机登录状态。"
                self.snapshot = {"ok": False, "rows": [], "plans": [], "fetchedAt": int(time.time()),
                                 "refreshSeconds": 30, "source": "本机 Codex 已登录账号", "error": message}
            self.checked_at = time.monotonic()
            return self.snapshot


class Dashboard:
    def __init__(self, usage, port=0):
        self.usage = usage
        self.server = None
        self.port = port

    def start(self):
        if self.server is not None:
            return self.url
        usage = self.usage

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                allowed = {"127.0.0.1:" + str(self.server.server_port), "localhost:" + str(self.server.server_port)}
                if self.headers.get("Host") not in allowed:
                    self.send_error(403)
                    return
                origin = self.headers.get("Origin")
                if origin and origin not in {"http://" + host for host in allowed}:
                    self.send_error(403)
                    return
                if self.path == "/api/usage":
                    content = json.dumps(usage.read(), ensure_ascii=False).encode()
                    content_type = "application/json; charset=utf-8"
                elif self.path == "/":
                    content = (ROOT / "web/panel.html").read_bytes()
                    content_type = "text/html; charset=utf-8"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'self'")
                self.end_headers()
                self.wfile.write(content)

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.server.daemon_threads = True
        self.url = "http://127.0.0.1:" + str(self.server.server_port) + "/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        atexit.register(self.server.server_close)
        return self.url


def tool(name, title, description):
    return {"name": name, "title": title, "description": description,
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "destructiveHint": False,
                            "idempotentHint": True, "openWorldHint": False},
            "_meta": {"ui": {"resourceUri": RESOURCE, "visibility": ["model", "app"]},
                      "openai/outputTemplate": RESOURCE, "openai/widgetAccessible": True}}


def launch_floating():
    import importlib.util
    if importlib.util.find_spec("PyQt5") is None:
        return {"ok": False, "error": "当前 Python 缺少 PyQt5，可使用已提供的侧栏面板。"}
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return {"ok": False, "error": "当前执行环境没有桌面显示会话，请在本机桌面执行悬浮窗。"}
    command = [sys.executable, str(ROOT / "floating.py")]
    status = subprocess.run(command + ["--status"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=8)
    if status.returncode == 0:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return {"ok": True, "message": "已显示现有额度悬浮窗。"}
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(12):
        if process.poll() is not None:
            return {"ok": False, "error": "悬浮窗未能启动，请检查本机 Qt 图形环境。"}
        time.sleep(.2)
        status = subprocess.run(command + ["--status"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=8)
        if status.returncode == 0:
            return {"ok": True, "message": "额度悬浮窗已启动。可拖动，双击收起，右键刷新，× 关闭。"}
    return {"ok": False, "error": "尚未确认悬浮窗就绪，请检查桌面或稍后重试。"}


def handle(method, params, usage, dashboard):
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "codex-usage-panel", "version": VERSION}}
    if method == "ping":
        return {}
    if method == "tools/list":
        floating = tool("open_floating_usage", "打开额度悬浮窗",
            "在本机桌面打开类似虚拟宠物的置顶额度小窗。可拖动、收起，每 30 秒自动更新。不会修改客户端、增加开机自启或创建真实 ChatGPT 宠物。需要本机图形会话和 PyQt5。")
        floating.pop("_meta")
        floating["annotations"]["readOnlyHint"] = False
        return {"tools": [floating, tool("open_usage_panel", "打开额度面板",
            "打开自动刷新 Codex 剩余额度的面板。首次打开后每 30 秒刷新，无需反复询问模型。"),
            tool("get_usage", "读取剩余额度", "只读查询本机已登录 Codex 账号的剩余百分比和重置时间。不会调用模型、兑换重置或购买额度。面板可自行调用此工具刷新。") ]}
    if method == "resources/list":
        return {"resources": [{"uri": RESOURCE, "name": "usage-panel", "mimeType": "text/html;profile=mcp-app"}]}
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    if method == "resources/read" and params.get("uri") == RESOURCE:
        return {"contents": [{"uri": RESOURCE, "mimeType": "text/html;profile=mcp-app",
            "text": (ROOT / "web/panel.html").read_text(),
            "_meta": {"ui": {"prefersBorder": True, "csp": {"connectDomains": [], "resourceDomains": []}},
                      "openai/ui": {"availableDisplayModes": ["inline", "pip", "fullscreen"]}}}]}
    if method == "tools/call" and params.get("name") == "open_floating_usage":
        snapshot = launch_floating()
        return {"content": [{"type": "text", "text": snapshot.get("message") or snapshot["error"]}],
                "structuredContent": snapshot, "isError": not snapshot["ok"]}
    if method == "tools/call" and params.get("name") in ("get_usage", "open_usage_panel"):
        snapshot = dict(usage.read())
        if params["name"] == "open_usage_panel":
            snapshot["dashboardUrl"] = dashboard.start()
        summary = snapshot.get("error") or "；".join(
            "{} / {} 分钟：剩余 {}%".format(row["limitName"], row["windowMinutes"],
                row["remainingPercent"] if row["remainingPercent"] is not None else "未知") for row in snapshot["rows"])
        return {"content": [{"type": "text", "text": summary}], "structuredContent": snapshot,
                "isError": not snapshot["ok"]}
    raise LookupError("Unsupported method or resource")


def serve(usage, dashboard):
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}), flush=True)
            continue
        if not isinstance(request, dict):
            continue
        if "id" not in request:
            continue
        try:
            result = handle(request.get("method"), request.get("params") or {}, usage, dashboard)
            response = {"result": result}
        except LookupError:
            response = {"error": {"code": -32601, "message": "Method, tool or resource not found"}}
        except Exception:
            response = {"error": {"code": -32603, "message": "Quota panel request failed"}}
        response.update({"jsonrpc": "2.0", "id": request["id"]})
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Read one sanitized quota snapshot")
    parser.add_argument("--dashboard", action="store_true", help="Run a local auto-refresh panel")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    usage = Usage()
    dashboard = Dashboard(usage, args.port)
    try:
        if args.once:
            print(json.dumps(usage.read(), ensure_ascii=False, indent=2))
        elif args.dashboard:
            print(dashboard.start(), flush=True)
            threading.Event().wait()
        else:
            serve(usage, dashboard)
    except KeyboardInterrupt:
        pass
    finally:
        usage.backend.close()
