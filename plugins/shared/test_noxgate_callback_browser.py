"""Real Chromium callback regression; set NOXGATE_TEST_BROWSER to its executable."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.environ.get("NOXGATE_TEST_BROWSER"), "NOXGATE_TEST_BROWSER is not configured")
class CallbackBrowserTests(unittest.TestCase):
    def test_real_browser_approval_and_denial_keep_same_origin(self):
        plugins = Path(__file__).resolve().parent.parent
        for runtime in ["shared/noxgate_auth.py", "noxgate-media/lib/noxgate_auth.py", "noxgate-game-assets/lib/noxgate_auth.py"]:
            spec = importlib.util.spec_from_file_location("callback_browser_fixture", plugins / runtime)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for fragment in ["code=fixture-code&state=fixture-state", "error=access_denied&state=fixture-state"]:
                with self.subTest(runtime=runtime, denied=fragment.startswith("error=")):
                    server = module.start_callback_server("http://127.0.0.1:0/callback", "fixture-state")
                    observed = {}
                    original = server.RequestHandlerClass.do_POST

                    def record(handler):
                        observed["origin"] = handler.headers.get("Origin")
                        observed["referer"] = handler.headers.get("Referer")
                        return original(handler)

                    server.RequestHandlerClass.do_POST = record
                    origin = "http://127.0.0.1:" + str(server.server_port)
                    try:
                        with tempfile.TemporaryDirectory(prefix="noxgate-browser-fixture-") as profile:
                            result = subprocess.run(
                                [os.environ["NOXGATE_TEST_BROWSER"], "--headless", "--no-first-run",
                                 "--disable-background-networking", "--user-data-dir=" + profile,
                                 "--dump-dom", origin + "/callback#" + fragment],
                                capture_output=True, text=True, timeout=25, check=True,
                            )
                        self.assertTrue(server.ready.wait(1), "browser callback was rejected")
                        self.assertEqual(observed["origin"], origin)
                        self.assertEqual(observed["referer"], origin + "/callback")
                        self.assertEqual(server.result["state"], "fixture-state")
                        self.assertEqual(server.result["code"], "fixture-code" if fragment.startswith("code=") else "")
                        self.assertEqual(server.result["error"], "access_denied" if fragment.startswith("error=") else "")
                        self.assertIn("授权结果已返回插件", result.stdout)
                        self.assertNotIn("Error code: 403", result.stdout)
                        self.assertNotIn("fixture-code", result.stdout)
                        self.assertNotIn("fixture-state", result.stdout)
                    finally:
                        server.shutdown()
                        server.server_close()


if __name__ == "__main__":
    unittest.main()
