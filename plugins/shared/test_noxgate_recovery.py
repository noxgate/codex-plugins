import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import noxgate_auth as auth
import noxgate_credentials as credentials
import noxgate_recovery as recovery

with patch.dict(os.environ, {"NOXGATE_NO_REEXEC": "1"}):
    import noxgate_runtime as runtime

PNG = b"\x89PNG\r\n\x1a\nfixture"
BASE = "https://example.invalid/v1"
SECRET = "ngp_synthetic_recovery_fixture"


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"NOXGATE_HOME": str(self.root / "home"), "NOXGATE_PROFILE": "work", "NOXGATE_NO_REEXEC": "1"})
        self.env.start()
        self.profile = {"gateway": BASE, "web": "https://example.invalid", "agent": "workbuddy", "store": "file", "scopes": ["image", "audio", "video"], "credential_id": 2, "expires_at": time.time()+3600}
        credentials.atomic_json(credentials.config_home() / "profiles.json", {"active": "work", "profiles": {"work": self.profile}})
        credentials.CredentialStore("file").set(auth.storage_key("work", self.profile), SECRET)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def metadata(self, **overrides):
        state = {"kind": "image", "fingerprint": "digest", "profile": "work", "provider_hash": hashlib.sha256(BASE.encode()).hexdigest(), "status": "received", "model": "fixture-model"}
        state.update(overrides)
        return state

    def test_sync_binary_and_base64_recover_after_output_failure_without_post(self):
        fixtures = [("image", PNG, ".png"), ("audio", b"RIFF0000WAVEfixture", ".wav"), ("video", b"0000ftypfixture", ".mp4")]
        for kind, raw, suffix in fixtures:
            for binary in (False, True):
                with self.subTest(kind=kind, binary=binary):
                    output = self.root / (kind+str(binary)+suffix)
                    state = output.with_suffix(".json")
                    response = (raw, "application/octet-stream") if binary else (json.dumps({"data": [{"b64_json": base64.b64encode(raw).decode()}]} if kind != "video" else {"result": {"b64_json":base64.b64encode(raw).decode()}}).encode(), "application/json")
                    with patch.object(runtime, "request", return_value=response) as request, patch.object(runtime, "save_outputs", side_effect=OSError("disk full")):
                        with self.assertRaises(OSError):
                            runtime.generate(kind, {"model":"fixture-model", "prompt":"fixture"}, output, state, provider=(BASE,SECRET))
                        self.assertEqual(request.call_count, 1)
                    with patch.object(runtime, "request", side_effect=AssertionError("must not POST or GET")):
                        paths = runtime.resume(state, provider=(BASE,SECRET))
                    self.assertEqual(paths, [str(output.resolve())])
                    self.assertEqual(output.read_bytes(), raw)
                    saved = state.read_text()
                    self.assertNotIn("b64_json", saved)
                    self.assertNotIn(SECRET, saved)

    def test_wrong_extension_can_resume_to_correct_path(self):
        state = self.root / "wrong.json"
        response = {"data":[{"b64_json":base64.b64encode(PNG).decode()}]}
        with patch.object(runtime,"request",return_value=(json.dumps(response).encode(),"application/json")):
            with self.assertRaisesRegex(runtime.MediaError,"扩展名"):
                runtime.generate("image",{"model":"fixture","prompt":"fixture"},self.root/"wrong.jpg",state,provider=(BASE,SECRET))
        with patch.object(runtime,"request",side_effect=AssertionError("no network")):
            runtime.resume(state,output=self.root/"correct.png",provider=(BASE,SECRET))
        self.assertEqual((self.root/"correct.png").read_bytes(),PNG)

    def test_signed_url_only_uses_explicit_secure_store_and_download_retries(self):
        state = self.root / "signed.json"
        output = self.root / "signed.png"
        address = "https://cdn.invalid/result.png?signature=synthetic_private_url"
        response = {"data":[{"url":address}],"authorization":SECRET,"ignored":"not media"}
        with patch.object(runtime,"request",side_effect=[(json.dumps(response).encode(),"application/json"),runtime.MediaError("download unavailable")]):
            with self.assertRaisesRegex(runtime.MediaError,"download unavailable"):
                runtime.generate("image",{"model":"fixture","prompt":"fixture"},output,state,provider=(BASE,SECRET))
        identifier = json.loads(state.read_text())["recovery_id"]
        manifest = credentials.config_home()/"cache/media"/identifier/"manifest.json"
        self.assertNotIn(address,state.read_text())
        self.assertNotIn(address,manifest.read_text())
        self.assertNotIn(SECRET,manifest.read_text())
        self.assertIn(address,credentials.CredentialStore("file").get_reference(identifier))
        with patch.object(runtime,"request",return_value=(PNG,"image/png")) as get:
            runtime.resume(state,provider=(BASE,SECRET))
        self.assertEqual(get.call_args.args,(address,""))
        self.assertIsNone(credentials.CredentialStore("file").get_reference(identifier))
        self.assertEqual(stat.S_IMODE(manifest.stat().st_mode),0o600)
        self.assertEqual(output.read_bytes(),PNG)

    def test_system_url_reference_is_not_a_plugin_token_or_process_argument(self):
        backend=type("Keyring",(),{"__module__":"keyring.backends.macOS"})()
        backend.set_password=MagicMock()
        keyring=MagicMock();keyring.get_keyring.return_value=backend
        with patch.dict(sys.modules,{"keyring":keyring}):
            store=credentials.CredentialStore("system")
            store.set_reference("fixture",'{"0":"https://cdn.invalid/?signature=fixture"}')
        backend.set_password.assert_called_once_with("net.noxgate.plugin-recovery","fixture",'{"0":"https://cdn.invalid/?signature=fixture"}')

    def test_cache_binding_and_integrity_are_checked(self):
        state=self.metadata()
        state["recovery_id"]=recovery.stage(state,"image",[{"b64_json":base64.b64encode(PNG).decode()}])
        for field,value in [("profile","other"),("provider_hash","other"),("fingerprint","other")]:
            with self.subTest(field=field),self.assertRaises(recovery.RecoveryError):
                recovery.recover({**state,field:value},"image",None)
        directory=credentials.config_home()/"cache/media"/state["recovery_id"]
        (directory/"0.bin").write_bytes(b"tampered")
        with self.assertRaisesRegex(recovery.RecoveryError,"校验"):
            recovery.recover(state,"image",None)

    def test_cache_refuses_git_paths_symlinks_and_path_traversal(self):
        for home in (self.root/"project",self.root/"link"):
            if home.name=="project":
                (home/".git").mkdir(parents=True)
            else:
                home.symlink_to(credentials.config_home(),target_is_directory=True)
            with patch.dict(os.environ,{"NOXGATE_HOME":str(home)}),self.assertRaises(recovery.RecoveryError):
                recovery.stage(self.metadata(),"image",[{"b64_json":base64.b64encode(PNG).decode()}])
        with self.assertRaises(recovery.RecoveryError):
            recovery.recover(self.metadata(recovery_id="../../elsewhere"),"image",None)

    def test_partial_multi_file_write_recovers_all_files_without_resubmission(self):
        result={"data":[{"b64_json":base64.b64encode(PNG).decode()}]*2}
        state=self.root/"multi.json";output=self.root/"multi.png"
        original=runtime.atomic_write
        def fail_second(path,raw):
            if path.name=="multi_02.png":
                raise OSError("disk full")
            original(path,raw)
        with patch.object(runtime,"request",return_value=(json.dumps(result).encode(),"application/json")),patch.object(runtime,"atomic_write",side_effect=fail_second):
            with self.assertRaises(OSError):runtime.generate("image",{"model":"fixture","prompt":"fixture"},output,state,provider=(BASE,SECRET))
        with patch.object(runtime,"request",side_effect=AssertionError("no network")):
            paths=runtime.resume(state,provider=(BASE,SECRET))
        self.assertEqual(len(paths),2)
        self.assertTrue(all(Path(p).read_bytes()==PNG for p in paths))

    def test_corrupt_manifest_never_falls_back_to_network(self):
        state=self.metadata(task_id="owned-task",output=str(self.root/"cached.png"))
        identifier=recovery.stage(state,"image",[{"b64_json":base64.b64encode(PNG).decode()}])
        state["recovery_id"]=identifier
        path=self.root/"corrupt.json";runtime.save_state(path,state)
        manifest=credentials.config_home()/"cache/media"/identifier/"manifest.json"
        for content in ("{", "[]", "null"):
            manifest.write_text(content)
            with self.subTest(content=content),patch.object(runtime,"request_json",side_effect=AssertionError("fail closed")),self.assertRaises(runtime.MediaError):
                runtime.resume(path,provider=(BASE,SECRET))

    def test_environment_profile_can_download_and_cache_url_without_secure_store_write(self):
        with patch.object(auth,"get_profile",return_value=("work",{**self.profile,"store":"env"})),patch.object(runtime,"request",side_effect=[(json.dumps({"data":[{"url":"https://cdn.invalid/?signature=fixture"}]}).encode(),"application/json"),(PNG,"image/png")]) as requests,patch.object(credentials.CredentialStore,"set_reference",side_effect=AssertionError("read-only store")):
            runtime.generate("image",{"model":"fixture","prompt":"fixture"},self.root/"env.png",self.root/"env.json",provider=(BASE,SECRET))
        self.assertEqual(requests.call_count,2)
        self.assertEqual((self.root/"env.png").read_bytes(),PNG)

    def test_missing_async_cache_requeries_task_but_tampered_cache_fails_closed(self):
        state=self.metadata(task_id="owned-task",recovery_id="a"*32,output=str(self.root/"cached.png"))
        path=self.root/"missing-cache.json";runtime.save_state(path,state)
        response={"status":"completed","data":[{"b64_json":base64.b64encode(PNG).decode()}]}
        with patch.object(runtime,"request_json",return_value=response) as get:
            runtime.resume(path,provider=(BASE,SECRET))
        get.assert_called_once()
        self.assertIn("owned-task",get.call_args.args[0])
        state=json.loads(path.read_text())
        binary=credentials.config_home()/"cache/media"/state["recovery_id"]/"0.bin"
        binary.write_bytes(b"corrupt")
        (self.root/"cached.png").unlink()
        with patch.object(runtime,"request_json",side_effect=AssertionError("must fail closed")),self.assertRaisesRegex(runtime.MediaError,"校验"):
            runtime.resume(path,provider=(BASE,SECRET))

    def test_async_download_failure_requeries_without_url_storage(self):
        state=self.metadata(task_id="owned-task")
        path=self.root/"async-download.json"
        result={"data":[{"url":"https://cdn.invalid/?signature=fixture"}]}
        with patch.object(runtime,"download_bytes",side_effect=runtime.MediaError("unavailable")),patch.object(credentials.CredentialStore,"set_reference",side_effect=AssertionError("async URLs must not be stored")):
            with self.assertRaises(runtime.MediaError):runtime.cache_result(state,path,"image",result,BASE,SECRET)
        self.assertNotIn("recovery_id",state)

    def asset_module(self):
        path=Path(__file__).resolve().parents[1]/"noxgate-game-assets/skills/noxgate-image-assets/scripts/asset_batch.py"
        spec=importlib.util.spec_from_file_location("recovery_asset_batch",path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def test_game_asset_run_accepts_other_authorized_image_models_and_rejects_unlisted_models(self):
        asset = self.asset_module()
        manifest = self.root / "assets.json"
        output = self.root / "assets"
        manifest.write_text(json.dumps({"assets": [
            {"id": "allowed", "model": "doubao-seedream-5.0-pro", "prompt": "fixture"},
            {"id": "denied", "model": "z-image-turbo", "prompt": "fixture"},
        ]}))
        image = io.BytesIO()
        asset.Image.new("RGBA", (2, 2), (1, 2, 3, 0)).save(image, format="PNG")
        response = {"data": [{"b64_json": base64.b64encode(image.getvalue()).decode()}]}
        argv = ["asset-batch", "run", "--manifest", str(manifest), "--output-dir", str(output),
                "--max-new-tasks", "2"]
        stderr = io.StringIO()
        with patch.object(sys, "argv", argv), \
                patch.object(asset, "load_provider", return_value=(BASE, SECRET)), \
                patch.object(asset, "authorized_models", return_value=([{"id": "doubao-seedream-5.0-pro"}], True)) as available, \
                patch.object(asset, "request_json", return_value=response) as request, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            self.assertEqual(asset.main(), 1)
        available.assert_called_once_with(BASE, SECRET, "game-image", "generate")
        request.assert_called_once_with("POST", BASE + "/images/generations", SECRET,
                                        {"model": "doubao-seedream-5.0-pro", "prompt": "fixture", "n": 1})
        self.assertTrue((output / "allowed__01.png").is_file())
        self.assertIn("z-image-turbo", stderr.getvalue())
        self.assertFalse((output / "_state" / "denied.json").exists())

    def test_game_asset_received_task_requeries_original_without_new_post(self):
        asset=self.asset_module();out=self.root/"assets"
        item={"id":"fixture","model":"fixture-model","prompt":"fixture"}
        response={"id":"original-task","status":"completed","data":[{"b64_json":base64.b64encode(PNG).decode()}]}
        with patch.object(asset,"request_json",return_value={"id":"original-task","status":"pending"}) as post,patch.object(asset,"wait_for_result",return_value=response) as poll,patch.object(asset,"finish_asset",side_effect=[runtime.MediaError("interruption"),"completed"]):
            with self.assertRaises(runtime.MediaError):asset.run_one(item,out,BASE,SECRET,1,10,True)
            self.assertEqual(asset.run_one(item,out,BASE,SECRET,1,10,False),"completed")
        self.assertEqual(post.call_count,1)
        self.assertEqual(poll.call_count,2)
        self.assertEqual(poll.call_args.args[2],"original-task")

    def test_game_asset_sync_download_failure_uses_cache_on_second_run(self):
        asset=self.asset_module();out=self.root/"assets"
        item={"id":"fixture","model":"fixture-model","prompt":"fixture","n":1,"background":"opaque","output_format":"png"}
        response={"data":[{"b64_json":base64.b64encode(PNG).decode()}]}
        with patch.object(asset,"request_json",return_value=response) as post,patch.object(asset,"render_image",side_effect=[OSError("output interrupted"),(PNG,(1,1),False)]):
            with self.assertRaises(OSError):asset.run_one(item,out,BASE,SECRET,1,10,True)
            self.assertEqual(asset.run_one(item,out,BASE,SECRET,1,10,False),"completed")
        self.assertEqual(post.call_count,1)
        self.assertEqual((out/"fixture__01.png").read_bytes(),PNG)

    def legacy(self,**changes):
        state=self.metadata();state.pop("profile");state.update(changes)
        path=self.root/"legacy.json";path.write_text(json.dumps(state));return path

    def test_migration_of_completed_output_is_offline_and_removes_response(self):
        output=self.root/"done.png";output.write_bytes(PNG)
        path=self.legacy(status="completed",files=[str(output)],result={"url":"https://invalid/?secret=fixture"})
        with patch.object(auth,"ensure",side_effect=AssertionError("no auth")),patch.object(runtime,"request",side_effect=AssertionError("no requests")):
            runtime.migrate_state(path,"work")
        state=json.loads(path.read_text())
        self.assertEqual(state["profile"],"work");self.assertNotIn("result",state)
        with patch.object(runtime,"request",side_effect=AssertionError("no requests")):
            self.assertEqual(runtime.resume(path,provider=(BASE,SECRET)),[str(output)])

    def test_migration_extracts_legacy_sync_bytes_without_using_signed_urls(self):
        path=self.legacy(result={"data":[{"b64_json":base64.b64encode(PNG).decode(),"url":"https://invalid/?secret=fixture"}],"header":SECRET},output=str(self.root/"migrated.png"))
        with patch.object(runtime,"request",side_effect=AssertionError("no requests")):
            runtime.migrate_state(path,"work")
            runtime.resume(path,provider=(BASE,SECRET))
        self.assertEqual((self.root/"migrated.png").read_bytes(),PNG)
        self.assertNotIn("secret",path.read_text());self.assertNotIn(SECRET,path.read_text())

    def test_migration_pending_task_checks_gateway_ownership_before_writing(self):
        path=self.legacy(status="pending",task_id="owned-task")
        with patch.object(auth,"ensure",return_value=(BASE,SECRET)),patch.object(runtime,"request_json",return_value={"status":"pending"}) as get:
            runtime.migrate_state(path,"work")
        self.assertIn("owned-task",get.call_args.args[0])
        self.assertEqual(json.loads(path.read_text())["profile"],"work")
        denied=self.legacy(status="pending",task_id="other-token-task");before=denied.read_bytes()
        with patch.object(auth,"ensure",return_value=(BASE,SECRET)),patch.object(runtime,"request_json",side_effect=runtime.MediaError("HTTP 403")),self.assertRaises(runtime.MediaError):
            runtime.migrate_state(denied,"work")
        self.assertEqual(denied.read_bytes(),before)

    def test_migration_rejects_wrong_gateway_existing_profile_and_unknown_submission(self):
        cases=[{"provider_hash":"wrong"},{"profile":"other"},{"status":"submitting"}]
        for changes in cases:
            path=self.legacy(**changes);before=path.read_bytes()
            with self.subTest(changes=changes),self.assertRaises(runtime.MediaError):runtime.migrate_state(path,"work")
            self.assertEqual(path.read_bytes(),before)

    def test_old_asset_requires_explicit_source_gateway_and_root(self):
        out=self.root/"legacy-assets";(out/"_state").mkdir(parents=True)
        (out/"fixture.png").write_bytes(PNG)
        path=out/"_state/fixture.json"
        old={"asset_id":"fixture","fingerprint":"fixture","status":"completed","files":["fixture.png"]}
        path.write_text(json.dumps(old))
        with self.assertRaises(runtime.MediaError):runtime.migrate_state(path,"work",asset_root=out)
        with patch.object(auth,"ensure",side_effect=AssertionError("offline")):
            runtime.migrate_state(path,"work",asset_root=out,source_gateway=BASE)
        self.assertEqual(json.loads(path.read_text())["profile"],"work")

    def test_migration_concurrent_change_is_rejected_before_cache_write(self):
        path=self.legacy(result={"data":[{"b64_json":base64.b64encode(PNG).decode()}]})
        original=runtime.json_object
        calls=0
        def changed(raw):
            nonlocal calls
            calls+=1
            state=original(raw)
            if calls==2:state["status"]="pending"
            return state
        with patch.object(runtime,"json_object",side_effect=changed),patch.object(recovery,"stage",side_effect=AssertionError("cache before validation")),self.assertRaisesRegex(runtime.MediaError,"状态已变化"):
            runtime.migrate_state(path,"work")
        self.assertFalse((credentials.config_home()/"cache").exists())

    def test_failed_state_commit_rolls_back_migration_and_recovery_cache(self):
        path=self.legacy(result={"data":[{"b64_json":base64.b64encode(PNG).decode()}]})
        before=path.read_bytes()
        with patch.object(runtime,"save_state",side_effect=OSError("disk full")),self.assertRaises(OSError):
            runtime.migrate_state(path,"work")
        self.assertEqual(path.read_bytes(),before)
        self.assertFalse(list((credentials.config_home()/"cache/media").glob("*/manifest.json")))
        state=self.metadata()
        with patch.object(runtime,"save_state",side_effect=OSError("disk full")),self.assertRaises(OSError):
            runtime.cache_result(state,self.root/"state.json","image",{"data":[{"b64_json":base64.b64encode(PNG).decode()}]},BASE,SECRET)
        self.assertNotIn("recovery_id",state)
        self.assertFalse(list((credentials.config_home()/"cache/media").glob("*/manifest.json")))

    def test_migrated_sync_asset_runs_with_bound_profile_and_cached_bytes(self):
        asset=self.asset_module();out=self.root/"migrate-asset";(out/"_state").mkdir(parents=True)
        item={"id":"fixture","model":"fixture-model","prompt":"fixture","n":1,"background":"opaque","output_format":"png"}
        path=out/"_state/fixture.json"
        old={"asset_id":"fixture","fingerprint":asset.fingerprint(item),"status":"received","result":{"data":[{"b64_json":base64.b64encode(PNG).decode()}]}}
        path.write_text(json.dumps(old))
        runtime.migrate_state(path,"work",source_gateway=BASE,asset_root=out)
        with patch.object(asset,"request_json",side_effect=AssertionError("no new task")),patch.object(asset,"render_image",return_value=(PNG,(1,1),False)):
            self.assertEqual(asset.run_one(item,out,BASE,SECRET,1,10,False),"completed")
        self.assertEqual((out/"fixture__01.png").read_bytes(),PNG)

    def test_actual_game_output_write_failure_preserves_sync_cache(self):
        asset=self.asset_module();out=self.root/"write-interrupted-assets"
        item={"id":"fixture","model":"fixture-model","prompt":"fixture","n":1,"background":"opaque","output_format":"png"}
        response={"data":[{"b64_json":base64.b64encode(PNG).decode()}]}
        original=asset.write_image
        with patch.object(asset,"request_json",return_value=response) as post,patch.object(asset,"render_image",return_value=(PNG,(1,1),False)),patch.object(asset,"write_image",side_effect=OSError("output disk full")):
            with self.assertRaises(OSError):asset.run_one(item,out,BASE,SECRET,1,10,True)
        with patch.object(asset,"request_json",side_effect=AssertionError("no second paid task")),patch.object(asset,"render_image",return_value=(PNG,(1,1),False)),patch.object(asset,"write_image",wraps=original):
            self.assertEqual(asset.run_one(item,out,BASE,SECRET,1,10,False),"completed")
        self.assertEqual(post.call_count,1)
        self.assertEqual((out/"fixture__01.png").read_bytes(),PNG)

    def test_signed_url_secure_reference_is_removed_if_state_commit_fails(self):
        state=self.metadata()
        original=(credentials.config_home()/"credentials.json").read_bytes()
        with patch.object(runtime,"save_state",side_effect=OSError("disk full")),self.assertRaises(OSError):
            runtime.cache_result(state,self.root/"state.json","image",{"data":[{"url":"https://cdn.invalid/?signature=fixture"}]},BASE,SECRET)
        self.assertNotIn("recovery_id",state)
        self.assertEqual((credentials.config_home()/"credentials.json").read_bytes(),original)
        self.assertFalse(list((credentials.config_home()/"cache/media").glob("*/manifest.json")))

    def test_migrate_legacy_state_before_first_profile_use_does_not_read_codex(self):
        path=self.legacy(status="pending",task_id="owned-task")
        original=path.read_bytes()
        with patch.object(auth,"get_profile",side_effect=auth.AuthError("login_required")),patch.object(runtime,"request",side_effect=AssertionError("no requests")),self.assertRaises(auth.AuthError):
            runtime.migrate_state(path,"work")
        self.assertEqual(path.read_bytes(),original)

    def test_migration_cli_is_real_and_returns_safe_output(self):
        output=self.root/"done.png";output.write_bytes(PNG)
        state=self.legacy(status="completed",files=[str(output)])
        stdout=io.StringIO()
        with patch("sys.argv",["noxgate","tasks","migrate-state","--state",str(state),"--profile","work"]),contextlib.redirect_stdout(stdout):
            self.assertEqual(auth.cli(),0)
        self.assertNotIn(SECRET,stdout.getvalue())

    def test_ensure_cli_honors_explicit_configuration(self):
        new={**self.profile,"gateway":"http://127.0.0.1:11280/v1","web":"http://127.0.0.1:11283","agent":"custom-agent","credential_id":3}
        store=MagicMock();store.get.return_value=SECRET
        argv=["noxgate","auth","ensure","--profile","new-profile","--gateway",new["gateway"],"--web",new["web"],"--agent","custom-agent","--store","file","--scope","audio","--capability","image"]
        with patch("sys.argv",argv),patch.object(auth,"login",return_value=new) as login,patch.object(auth,"CredentialStore",return_value=store),patch.object(auth,"auth_request",return_value={"id":3,"profile_name":"new-profile"}),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(auth.cli(),0)
        login.assert_called_once_with("new-profile",new["gateway"],new["web"],"custom-agent",["audio","image"],"file",env_file=None)

    def test_ensure_preserves_existing_settings_and_honors_flags_when_reauthorizing(self):
        for reason in ("valid","expired","scope","revoked"):
            profile={**self.profile,"scopes":["image"] if reason=="scope" else self.profile["scopes"],"expires_at":0 if reason=="expired" else self.profile["expires_at"]}
            with patch.object(auth,"get_profile",return_value=("work",profile)),patch.object(auth,"login",return_value=self.profile) as login,patch.object(auth,"auth_request",side_effect=[auth.AuthError("UNAUTHORIZED")] if reason=="revoked" else None,return_value={"id":2,"profile_name":"work"}):
                auth.ensure("audio","work")
                if reason=="valid":login.assert_not_called()
                else:
                    self.assertEqual(login.call_args.args[1:4],(BASE,self.profile["web"],"workbuddy"))
                    self.assertEqual(login.call_args.args[5],"file")
        self.assertEqual(login.call_args.kwargs,{"env_file":None})


if __name__=="__main__":unittest.main()
