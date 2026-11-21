import contextlib
import http.client
import importlib.util
import io
import json
import os
import stat
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import noxgate_auth as auth
import noxgate_credentials as credentials

with patch.dict(os.environ, {"NOXGATE_NO_REEXEC": "1"}):
    import noxgate_runtime as runtime


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"NOXGATE_HOME": self.temp.name, "NOXGATE_NO_REEXEC": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_file_store_is_explicit_and_private(self):
        default_store = credentials.CredentialStore()
        self.assertEqual(default_store.backend, "file")
        store = credentials.CredentialStore("file")
        store.set("profile", "ngp_test_secret")
        path = Path(self.temp.name) / "credentials.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(store.get("profile"), "ngp_test_secret")
        store.delete("profile")
        self.assertIsNone(store.get("profile"))
        path.chmod(0o644)
        with self.assertRaises(credentials.CredentialError):
            store.get("profile")

    def test_first_ensure_requests_shared_permissions_and_reuses_saved_credential(self):
        secret = "ngp_shared_fixture"
        credential = {
            "gateway": auth.DEFAULT_GATEWAY, "web": auth.DEFAULT_WEB, "agent": "generic",
            "store": "file", "credential_id": 1, "expires_at": time.time() + 3600,
        }

        def authorize(name, gateway, web, agent, scopes, backend, *, env_file):
            profile = {**credential, "scopes": scopes, "requested_scopes": scopes}
            credentials.CredentialStore().set(auth.storage_key(name, profile), secret)
            credentials.atomic_json(credentials.config_home() / "profiles.json", {
                "active": name, "profiles": {name: profile}
            })
            return profile

        remote = {"id": 1, "profile_name": "work", "scopes": sorted(auth.SCOPES)}
        with patch.object(auth, "login", side_effect=authorize) as login, \
                patch.object(auth, "auth_request", return_value=remote):
            self.assertEqual(auth.ensure("image", "work"), (auth.DEFAULT_GATEWAY, secret))
            for capability in ("audio", "video", "game-assets", "image"):
                self.assertEqual(auth.ensure(capability, "work"), (auth.DEFAULT_GATEWAY, secret))
        login.assert_called_once()
        self.assertEqual(login.call_args.args[4], sorted(auth.SCOPES))

    def test_ensure_does_not_repeat_authorization_for_an_already_declined_capability(self):
        profile = {
            "gateway": auth.DEFAULT_GATEWAY, "web": auth.DEFAULT_WEB, "agent": "generic",
            "store": "file", "credential_id": 1, "expires_at": time.time() + 3600,
            "scopes": ["image"], "requested_scopes": sorted(auth.SCOPES),
        }
        secret = "ngp_partial_fixture"
        credentials.CredentialStore().set(auth.storage_key("work", profile), secret)
        credentials.atomic_json(credentials.config_home() / "profiles.json", {
            "active": "work", "profiles": {"work": profile}
        })
        remote = {"id": 1, "profile_name": "work", "scopes": ["image"]}
        with patch.object(auth, "login") as login, patch.object(auth, "auth_request", return_value=remote):
            for _ in range(2):
                with self.assertRaises(auth.AuthError) as failure:
                    auth.ensure("audio", "work")
                self.assertEqual(failure.exception.code, "requested_capability_not_approved")
            self.assertEqual(auth.ensure("image", "work"), (auth.DEFAULT_GATEWAY, secret))
        login.assert_not_called()

    def test_unknown_system_backend_fails_closed(self):
        keyring = MagicMock()
        keyring.get_keyring.return_value = object()
        with patch.dict("sys.modules", {"keyring": keyring}):
            with self.assertRaises(credentials.CredentialError):
                credentials.CredentialStore("system")
        self.assertFalse((Path(self.temp.name) / "credentials.json").exists())

    def test_three_system_adapters_keep_secrets_in_process(self):
        for module in ("keyring.backends.macOS", "keyring.backends.Windows", "keyring.backends.SecretService"):
            backend = type("Keyring", (), {"__module__": module})()
            backend.get_password = MagicMock(return_value="ngp_system_secret")
            backend.set_password = MagicMock()
            backend.delete_password = MagicMock()
            keyring = MagicMock()
            keyring.get_keyring.return_value = backend
            with patch.dict("sys.modules", {"keyring": keyring}):
                store = credentials.CredentialStore("system")
                store.set("profile", "ngp_system_secret")
                self.assertEqual(store.get("profile"), "ngp_system_secret")
                store.delete("profile")
                backend.set_password.assert_called_once_with("net.noxgate.plugins", "profile", "ngp_system_secret")

    def test_portable_env_requires_explicit_private_path_and_only_plugin_keys(self):
        plugin_dir = Path(self.temp.name) / "plugin"
        env_path = plugin_dir / ".env"
        first = "a" * 64
        second = "b" * 64
        store = credentials.CredentialStore("portable-env", env_path)
        store.set(first, "ngp_first_secret")
        store.set(second, "ngp_second_secret")
        self.assertEqual(store.get(first), "ngp_first_secret")
        self.assertEqual(store.get(second), "ngp_second_secret")
        self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(plugin_dir.stat().st_mode), 0o700)
        store.delete(first)
        self.assertIsNone(store.get(first))
        self.assertEqual(store.get(second), "ngp_second_secret")
        self.assertTrue(env_path.read_text().startswith("NOXGATE_PLUGIN_TOKEN_" + second + "="))
        with self.assertRaises(credentials.CredentialError):
            credentials.CredentialStore("portable-env")
        with self.assertRaises(credentials.CredentialError):
            credentials.CredentialStore("portable-env", Path("relative/.env"))
        env_path.write_text("OTHER_SECRET=do-not-preserve\n")
        env_path.chmod(0o600)
        with self.assertRaises(credentials.CredentialError):
            store.set(first, "ngp_rejected")

    def test_portable_env_rejects_symlink_and_recovery_references(self):
        plugin_dir = Path(self.temp.name) / "plugin"
        plugin_dir.mkdir()
        actual = plugin_dir / "actual.env"
        actual.write_text("")
        actual.chmod(0o600)
        link = plugin_dir / ".env"
        link.symlink_to(actual)
        store = credentials.CredentialStore("portable-env", link)
        with self.assertRaises(credentials.CredentialError):
            store.get("a" * 64)
        link.unlink()
        safe = credentials.CredentialStore("portable-env", plugin_dir / ".env")
        with self.assertRaises(credentials.CredentialError):
            safe.set_reference("task", "https://example.test/signed")

    def test_login_uses_browser_callback_and_persists_only_plugin_secret_in_explicit_store(self):
        session = {"request_token": "request_test_secret", "expires_in": 600}
        issued = {"access_token": "ngp_test_secret", "profile": "work", "scopes": ["image"], "credential_id": 42, "expires_in": 3600}
        callback = MagicMock(server_port=54321, result={"code": "authorization_code"})
        callback.ready = threading.Event()
        callback.ready.set()
        output = io.StringIO()
        with patch.object(auth, "auth_request", side_effect=[session, issued]) as request, patch.object(auth, "start_callback_server", return_value=callback), patch.object(auth, "open_authorization", return_value=MagicMock()) as browser, contextlib.redirect_stdout(output):
            profile = auth.login("work", auth.DEFAULT_GATEWAY, auth.DEFAULT_WEB, "codex", ["image"], "file")
        self.assertNotIn("request_test_secret", output.getvalue())
        self.assertNotIn("authorization_code", output.getvalue())
        self.assertNotIn("ngp_test_secret", output.getvalue())
        config = (Path(self.temp.name) / "profiles.json").read_text()
        self.assertNotIn("ngp_test_secret", config)
        self.assertNotIn("request_test_secret", config)
        self.assertEqual(auth.get_profile("work")[1], profile)
        self.assertEqual(browser.call_args.args[0], auth.DEFAULT_WEB)
        self.assertEqual(request.call_args_list[0].args[1], "authorize/start")
        self.assertEqual(request.call_args_list[1].args[1], "authorize/exchange")
        self.assertEqual(request.call_args_list[0].args[2]["code_challenge_method"], "S256")
        self.assertNotEqual(request.call_args_list[1].args[2]["code_verifier"], request.call_args_list[0].args[2]["code_challenge"])

    def test_browser_failure_cancels_request_without_exposing_secret(self):
        session = {"request_token": "request_test_secret", "expires_in": 600}
        callback = MagicMock(server_port=54321)
        output = io.StringIO()
        with patch.object(auth, "auth_request", return_value=session) as request, patch.object(auth, "start_callback_server", return_value=callback), patch.object(auth, "open_authorization", return_value=None), contextlib.redirect_stdout(output):
            with self.assertRaises(auth.AuthError):
                auth.login("work", auth.DEFAULT_GATEWAY, auth.DEFAULT_WEB, "codex", ["image"], "file")
        self.assertNotIn("request_test_secret", output.getvalue())
        self.assertEqual(request.call_args_list[-1].args[1], "authorize/cancel")

    def test_no_codex_fallback_or_explicit_path_reads(self):
        with patch.object(auth, "ensure", return_value=(auth.DEFAULT_GATEWAY, "ngp_test")) as ensure:
            self.assertEqual(runtime.load_provider(capability="video"), (auth.DEFAULT_GATEWAY, "ngp_test"))
            ensure.assert_called_once_with("video")
        with self.assertRaises(runtime.MediaError):
            runtime.load_provider(Path("/must/not/read/config.toml"))

    def test_profile_switch_isolation(self):
        config = {"active": "work", "profiles": {"work": {"gateway": auth.DEFAULT_GATEWAY}, "self-hosted": {"gateway": "http://127.0.0.1:11280/v1"}}}
        credentials.atomic_json(Path(self.temp.name) / "profiles.json", config)
        self.assertEqual(auth.profile_name(), "work")
        self.assertNotEqual(auth.storage_key("work", config["profiles"]["work"]), auth.storage_key("self-hosted", config["profiles"]["self-hosted"]))

    def test_state_excludes_untrusted_response_and_credentials(self):
        path = Path(self.temp.name) / "task.json"
        runtime.save_state(path, {"kind": "image", "fingerprint": "digest", "profile": "work", "task_id": "task", "result": {"url": "https://example.test/?token=secret"}, "authorization": "ngp_secret"})
        state = json.loads(path.read_text())
        self.assertNotIn("result", state)
        self.assertNotIn("authorization", state)

    def test_browser_launch_arguments_contain_no_authorization_secret(self):
        with patch.object(auth.webbrowser, "open", return_value=True) as browser:
            server = auth.open_authorization(auth.DEFAULT_WEB, "request-secret-fixture", "state-secret-fixture")
            try:
                launched = browser.call_args.args[0]
                self.assertTrue(launched.startswith("http://127.0.0.1:"))
                self.assertNotIn("request-secret-fixture", launched)
                self.assertNotIn("state-secret-fixture", launched)
            finally:
                server.shutdown()
                server.server_close()

    def test_loopback_callback_accepts_only_same_origin_post_with_matching_state(self):
        state = "state-secret-fixture"
        server = auth.start_callback_server("http://127.0.0.1:0/callback", state)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request("GET", "/callback")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Referrer-Policy"), "same-origin")
            callback_page = response.read().decode()
            self.assertIn("<meta name=referrer content=same-origin>", callback_page)
            for origin in [None, "null", "https://noxgate.net", "http://attacker.invalid"]:
                headers = {"Content-Type": "application/x-www-form-urlencoded"}
                if origin is not None:
                    headers["Origin"] = origin
                connection.request("POST", "/callback", body="code=fixture-code&state=" + state, headers=headers)
                rejected = connection.getresponse()
                self.assertEqual(rejected.status, 403)
                rejected.read()
                self.assertFalse(server.ready.is_set())
            self.assertIn("location.hash", callback_page)
            self.assertLess(callback_page.index("<body>"), callback_page.index("<script>"))
            connection.request(
                "POST",
                "/callback",
                body="code=one-time-code&state=wrong-state",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": f"http://127.0.0.1:{server.server_port}",
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            connection.request(
                "POST",
                "/callback",
                body="code=one-time-code&state=" + state,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": f"http://127.0.0.1:{server.server_port}",
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            self.assertTrue(server.ready.wait(1))
            self.assertEqual(server.result, {"code": "one-time-code", "error": "", "state": state})
        finally:
            connection.close()
            server.shutdown()
            server.server_close()


    def test_failed_login_preserves_existing_profile_secret(self):
        old = {"gateway": auth.DEFAULT_GATEWAY, "credential_id": 5, "store": "file"}
        credentials.atomic_json(Path(self.temp.name) / "profiles.json", {"active": "work", "profiles": {"work": old}})
        store = credentials.CredentialStore("file")
        store.set(auth.storage_key("work", old), "ngp_old_test")
        session = {"request_token": "request_test_secret", "expires_in": 600}
        callback = MagicMock(server_port=54321, result={"code": "authorization_code"})
        callback.ready = threading.Event()
        callback.ready.set()
        with patch.object(auth, "auth_request", side_effect=[session, auth.AuthError("access_denied")]), patch.object(auth, "start_callback_server", return_value=callback), patch.object(auth, "open_authorization", return_value=MagicMock()), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(auth.AuthError):
                auth.login("work", auth.DEFAULT_GATEWAY, auth.DEFAULT_WEB, "generic", ["image"], "file")
        self.assertEqual(store.get(auth.storage_key("work", old)), "ngp_old_test")
        self.assertEqual(auth.get_profile("work")[1], old)

    def test_media_types_share_auth_and_completed_tasks_never_resubmit(self):
        import base64
        fixtures = {"image": (b"\x89PNG\r\n\x1a\nfixture", ".png"), "audio": (b"RIFF0000WAVEfixture", ".wav"), "video": (b"0000ftypfixture", ".mp4")}
        for kind, (raw, suffix) in fixtures.items():
            output = Path(self.temp.name) / (kind + suffix)
            state = output.with_suffix(".json")
            payload = {"model": "fixture-model", "prompt": "fixture"}
            response = {"b64_json": base64.b64encode(raw).decode(), "status": "succeeded"}
            with patch.object(auth, "ensure", return_value=(auth.DEFAULT_GATEWAY, "ngp_fixture_secret")) as ensure, patch.object(runtime, "request", return_value=(json.dumps(response).encode(), "application/json")) as request:
                paths = runtime.generate(kind, payload, output, state)
                self.assertEqual(runtime.generate(kind, payload, output, state), paths)
                self.assertEqual(request.call_count, 1)
                self.assertTrue(all(call.args[0] == kind for call in ensure.call_args_list))
            self.assertEqual(output.read_bytes(), raw)
            self.assertNotIn("ngp_fixture_secret", state.read_text())
            self.assertNotIn("b64_json", state.read_text())

    def test_explicit_migration_imports_only_public_gateway(self):
        codex = Path(self.temp.name) / "codex"
        codex.mkdir()
        tick = chr(34)
        (codex / "config.toml").write_text('model_provider = "fixture"\n[model_providers.fixture]\nbase_url = "https://api.noxgate.net/v1"\nexperimental_bearer_token = "sk_never_import_fixture"\n')
        with patch.dict(os.environ, {"CODEX_HOME": str(codex)}), patch("sys.argv", ["noxgate", "auth", "import-codex"]), patch.object(auth, "login") as login, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(auth.cli(), 0)
        self.assertEqual(login.call_args.args[1], auth.DEFAULT_GATEWAY)
        self.assertNotIn("sk_never_import_fixture", str(login.call_args))

    def test_secret_file_cannot_be_saved_in_a_repository(self):
        (Path(self.temp.name) / ".git").mkdir()
        with self.assertRaises(credentials.CredentialError):
            credentials.CredentialStore("file").set("fixture", "ngp_fixture")
        self.assertFalse((Path(self.temp.name) / "credentials.json").exists())

    def test_storage_migration_is_explicit_and_keeps_source_by_default(self):
        key = "c" * 64
        profile = {
            "gateway": auth.DEFAULT_GATEWAY,
            "web": auth.DEFAULT_WEB,
            "agent": "codex",
            "scopes": ["image"],
            "store": "file",
            "credential_id": 81,
            "expires_at": time.time() + 3600,
        }
        credentials.atomic_json(Path(self.temp.name) / "profiles.json", {"active": "work", "profiles": {"work": profile}})
        secret = "ngp_migration_secret"
        credentials.CredentialStore("file").set(key, secret)
        env_path = Path(self.temp.name) / "plugin" / ".env"
        with patch.object(auth, "storage_key", return_value=key), patch.object(
            auth, "auth_request", return_value={"profile_name": "work", "id": 81}
        ):
            removed = auth.migrate_storage("work", "file", "portable-env", env_path)
        self.assertFalse(removed)
        self.assertEqual(credentials.CredentialStore("file").get(key), secret)
        self.assertEqual(credentials.CredentialStore("portable-env", env_path).get(key), secret)
        self.assertEqual(auth.get_profile("work")[1]["store"], "portable-env")
        self.assertNotIn(secret, (Path(self.temp.name) / "profiles.json").read_text())

    def test_failed_storage_migration_rolls_back_target_and_profile(self):
        key = "d" * 64
        profile = {"gateway": auth.DEFAULT_GATEWAY, "store": "file", "credential_id": 82}
        credentials.atomic_json(Path(self.temp.name) / "profiles.json", {"active": "work", "profiles": {"work": profile}})
        secret = "ngp_rollback_secret"
        source = credentials.CredentialStore("file")
        source.set(key, secret)
        env_path = Path(self.temp.name) / "plugin" / ".env"
        original_get = credentials.CredentialStore.get
        target_reads = 0

        def incorrect_verification(store, storage_key):
            nonlocal target_reads
            value = original_get(store, storage_key)
            if store.backend == "portable-env":
                target_reads += 1
                if target_reads == 2:
                    return "ngp_wrong_secret"
            return value

        with patch.object(auth, "storage_key", return_value=key), patch.object(
            auth, "auth_request", return_value={"profile_name": "work", "id": 82}
        ), patch.object(credentials.CredentialStore, "get", incorrect_verification):
            with self.assertRaises(auth.AuthError):
                auth.migrate_storage("work", "file", "portable-env", env_path)
        self.assertEqual(source.get(key), secret)
        self.assertIsNone(credentials.CredentialStore("portable-env", env_path).get(key))
        self.assertEqual(auth.get_profile("work")[1], profile)

    def test_async_audio_recovers_using_task_id_without_response_state(self):
        import base64
        output = Path(self.temp.name) / "async.wav"
        state = Path(self.temp.name) / "async-state.json"
        raw = b"RIFF0000WAVEfixture"
        replies = [(json.dumps({"task_id": "audio-task", "status": "pending"}).encode(), "application/json"), (json.dumps({"task_id": "audio-task", "status": "succeeded", "b64_json": base64.b64encode(raw).decode()}).encode(), "application/json")]
        with patch.object(auth, "ensure", return_value=(auth.DEFAULT_GATEWAY, "ngp_fixture")), patch.object(runtime, "request", side_effect=replies) as request, patch.object(runtime.time, "sleep"):
            runtime.generate("audio", {"model": "fixture", "input": "fixture"}, output, state)
        self.assertIn("audio-task", request.call_args_list[1].args[0])
        self.assertEqual(output.read_bytes(), raw)
        self.assertNotIn("b64_json", state.read_text())


if __name__ == "__main__":
    unittest.main()
