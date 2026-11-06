import platform
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "main/xiaozhi-server"))
from dudu_resources import ResourceMonitor, summarize
from dudu_web import create_web_app
from aiohttp.test_utils import TestClient, TestServer


class ResourceTests(unittest.TestCase):
    def test_cpu_mean_uses_elapsed_time_and_preserves_one_core_convention(self):
        summary = summarize([
            {"timestamp": 0, "interval_seconds": 1, "cpu_core_percent": 200, "rss_bytes": 10},
            {"timestamp": 2, "interval_seconds": 2, "cpu_core_percent": 50, "rss_bytes": 30},
        ])
        self.assertEqual(summary["cpu_core_percent"]["mean"], 100)
        self.assertEqual(summary["cpu_core_percent"]["max"], 200)
        self.assertEqual(summary["rss_bytes"]["max"], 30)

    def test_session_boundaries_and_retention(self):
        monitor = ResourceMonitor(retention=3)
        monitor.sample()
        monitor.begin("call")
        monitor.sample()
        monitor.end("call")
        for _ in range(5):
            monitor.sample()
        data = monitor.snapshot("call", include_samples=True)
        self.assertEqual(len(data["samples"]), 3)
        self.assertEqual(data["session"]["summary"]["samples"], 1)
        self.assertIsNotNone(data["session"]["ended_at"])
        self.assertEqual(data["current"]["active_calls"], 0)

    @unittest.skipUnless(platform.system() in ("Darwin", "Linux"), "Unix child CPU accounting")
    def test_child_memory_and_reaped_cpu_are_included(self):
        monitor = ResourceMonitor()
        monitor.sample()
        code = ("import time; data=bytearray(4*1024*1024); print('ready',flush=True); "
                "end=time.perf_counter()+.15\nwhile time.perf_counter()<end: pass\n")
        with subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True) as child:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            live = monitor.sample()
            self.assertGreater(live["children_rss_bytes"], 0)
            self.assertGreaterEqual(live["children"], 1)
            child.wait(timeout=5)
        finished = monitor.sample()
        self.assertGreater(finished["cpu_core_percent"], 0)
        self.assertEqual(finished["children"], 0)


class ResourceAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_current_and_export_include_call_summary_without_credentials(self):
        monitor = ResourceMonitor()
        monitor.begin("test-call")
        monitor.sample()
        monitor.end("test-call")
        server = SimpleNamespace(config={}, _llm=SimpleNamespace(model_name="qwen-flash"),
                                 get_connection_config=lambda headers: {})
        app = create_web_app(server, False, 8000, monitor)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/resources?session_id=test-call&samples=1")
            data = await response.json()
            self.assertEqual(response.status, 200)
            self.assertEqual(data["session"]["summary"]["samples"], 1)
            self.assertEqual(len(data["samples"]), 1)
            self.assertIn("cpu_percent_convention", data["host"])
            self.assertNotIn("api_key", str(data))


if __name__ == "__main__":
    unittest.main()
