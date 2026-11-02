import importlib.util
import http.client
import json
from pathlib import Path
import subprocess
import sys
import unittest
import urllib.request
import urllib.error

spec = importlib.util.spec_from_file_location("quota", Path(__file__).with_name("server.py"))
quota = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quota)


class Tests(unittest.TestCase):
    def snapshot(self, primary):
        return quota.normalize({"rateLimits": {"limitId": "codex", "primary": primary}})

    def test_percent_is_remaining_not_used(self):
        row = self.snapshot({"usedPercent": 27, "windowDurationMins": 300})["rows"][0]
        self.assertEqual(row["remainingPercent"], 73)

    def test_missing_is_not_zero_or_full(self):
        for value in (None, True, "25", float("nan"), float("inf")):
            row = self.snapshot({"usedPercent": value})["rows"][0]
            self.assertIsNone(row["remainingPercent"])
        self.assertFalse(quota.normalize({})["ok"])

    def test_clamp_and_zero(self):
        for used, remaining in ((0, 100), (100, 0), (110, 0), (-10, 100)):
            self.assertEqual(self.snapshot({"usedPercent": used})["rows"][0]["remainingPercent"], remaining)

    def test_multiple_buckets_and_windows(self):
        result = quota.normalize({"rateLimits": {"primary": {"usedPercent": 99}},
            "rateLimitsByLimitId": {
                "codex": {"primary": {"usedPercent": 20, "windowDurationMins": 300},
                          "secondary": {"usedPercent": 60, "windowDurationMins": 10080}},
                "other": {"limitName": "Model B", "primary": {"usedPercent": 10, "windowDurationMins": 60}}}})
        self.assertEqual(len(result["rows"]), 3)
        self.assertEqual({row["remainingPercent"] for row in result["rows"]}, {80, 40, 90})

    def test_expired_reset_preserves_server_percent(self):
        row = self.snapshot({"usedPercent": 100, "resetsAt": 1})["rows"][0]
        self.assertEqual(row["remainingPercent"], 0)
        self.assertEqual(row["resetsAt"], 1)

    def test_cached_and_failed_reads_do_not_leak_old_account(self):
        usage = quota.Usage()
        class Fake:
            calls = 0
            def read(self):
                self.calls += 1
                if self.calls > 1:
                    raise RuntimeError("连接失败")
                return {"rateLimits": {"primary": {"usedPercent": 20}}}
            def close(self):
                pass
        usage.backend = fake = Fake()
        self.assertTrue(usage.read()["ok"])
        self.assertTrue(usage.read()["ok"])
        self.assertEqual(fake.calls, 1)
        usage.checked_at = 0
        result = usage.read()
        self.assertFalse(result["ok"])
        self.assertEqual(result["rows"], [])

    def test_mcp_stdio_resource_and_discovery(self):
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": quota.RESOURCE}},
                    {"jsonrpc": "2.0", "id": 4, "method": "not-a-method"}]
        process = subprocess.run([sys.executable, str(Path(__file__).with_name("server.py"))],
            input="".join(json.dumps(message) + "\n" for message in messages),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=10)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(len(replies), 4)
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(len(replies[1]["result"]["tools"]), 3)
        self.assertEqual(replies[2]["result"]["contents"][0]["mimeType"], "text/html;profile=mcp-app")
        self.assertEqual(replies[3]["error"]["code"], -32601)

    def test_dashboard_loopback_and_cross_origin(self):
        class Fake:
            def read(self):
                return {"ok": True, "rows": []}
        dashboard = quota.Dashboard(Fake())
        url = dashboard.start()
        try:
            self.assertEqual(dashboard.server.server_address[0], "127.0.0.1")
            with urllib.request.urlopen(url + "api/usage") as response:
                self.assertTrue(json.load(response)["ok"])
                self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
            request = urllib.request.Request(url + "api/usage", headers={"Origin": "https://example.com"})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
            # urllib may rewrite Host through the host's configured proxy.
            connection = http.client.HTTPConnection("127.0.0.1", dashboard.server.server_port)
            connection.request("GET", "/", headers={"Host": "example.com"})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            connection.close()
        finally:
            dashboard.server.shutdown()
            dashboard.server.server_close()


if __name__ == "__main__":
    unittest.main()
