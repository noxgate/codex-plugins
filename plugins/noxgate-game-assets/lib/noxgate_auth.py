"""Agent-neutral independent browser authentication; secrets stay in this process."""
from __future__ import annotations

import argparse
import base64
import hmac
import hashlib
import html
import http.server
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from noxgate_credentials import CredentialError, CredentialStore, atomic_json, config_home, storage_lock

DEFAULT_GATEWAY = "https://api.noxgate.net/v1"
DEFAULT_WEB = "https://noxgate.net"
SCOPES = {"image", "video", "audio", "game-assets"}


class AuthError(Exception):
    def __init__(self, code: str):
        self.code = code if re.fullmatch(r"[a-zA-Z_]{1,64}", code) else "authorization_failed"
        super().__init__(f"NoxGate 认证未完成（{self.code}）；请运行 noxgate auth login")


def validate_address(address: str) -> str:
    p = urllib.parse.urlsplit(address)
    if not p.hostname or p.username or p.password or p.query or p.fragment:
        raise AuthError("invalid_gateway")
    if p.scheme != "https" and not (p.scheme == "http" and p.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise AuthError("invalid_gateway")
    return address.rstrip("/")


def read_profiles() -> dict:
    path = config_home() / "profiles.json"
    if not path.exists():
        return {"active": "default", "profiles": {}}
    if path.is_symlink() or not path.is_file():
        raise CredentialError("profile 配置必须是普通文件")
    if os.name != "nt" and (path.stat().st_mode & 0o777) != 0o600:
        raise CredentialError("profile 配置文件必须使用 0600 权限")
    return json.loads(path.read_text(encoding="utf-8"))


def profile_store(profile: dict) -> CredentialStore:
    return CredentialStore(profile.get("store", "file"), profile.get("env_file"))


def profile_name(name: str | None = None) -> str:
    name = name or os.environ.get("NOXGATE_PROFILE") or read_profiles().get("active", "default")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", name):
        raise AuthError("invalid_profile")
    return name


def get_profile(name: str | None = None) -> tuple[str, dict]:
    name = profile_name(name)
    profile = read_profiles().get("profiles", {}).get(name)
    if profile is None:
        raise AuthError("login_required")
    validate_address(profile["gateway"])
    return name, profile


def storage_key(name: str, profile: dict) -> str:
    return hashlib.sha256((name + "\n" + profile["gateway"] + "\n" + str(profile.get("credential_id", "legacy"))).encode()).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise AuthError("unexpected_redirect")


def open_authorization(web: str, request_token: str, state: str | None = None):
    """Open the web authorization URL through a one-use loopback bridge.

    The request token and state are written only to the bridge response body;
    the browser-launch argument contains a loopback URL and no authorization
    secret. The bridge has no logs and accepts one same-machine request.
    """
    params = {"request": request_token}
    if state:
        params["state"] = state
    target = validate_address(web) + "/integrations/authorize#" + urllib.parse.urlencode(params)
    consumed = False
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            nonlocal consumed
            expected = "127.0.0.1:" + str(self.server.server_port)
            if consumed or self.path != "/" or self.headers.get("Host") != expected or self.headers.get("Origin") or self.headers.get("Sec-Fetch-Site", "none") not in {"none", "same-origin"}:
                self.send_error(403)
                return
            consumed = True
            body = ('<!doctype html><meta name="referrer" content="no-referrer"><meta http-equiv="refresh" content="0;url=' + html.escape(target, quote=True) + '">').encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            threading.Thread(target=self.server.shutdown, daemon=True).start()
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        opened = webbrowser.open("http://127.0.0.1:" + str(server.server_port) + "/")
    except Exception:
        opened = False
    if not opened:
        server.shutdown()
        server.server_close()
        return None
    return server


class _CallbackServer(http.server.HTTPServer):
    allow_reuse_address = True


def start_callback_server(redirect_uri: str, expected_state: str):
    parsed = urllib.parse.urlsplit(redirect_uri)
    callback_path = parsed.path
    result = {}
    ready = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            expected_host = parsed.hostname + ":" + str(self.server.server_port)
            if self.path != callback_path or self.headers.get("Host") != expected_host:
                self.send_error(404)
                return
            # no-referrer makes browser form POSTs send Origin: null. Keep the
            # same-origin Origin header; the fragment is cleared before submission.
            body = """<!doctype html><html><head><meta name=referrer content=same-origin><title>NoxGate</title></head><body><p>正在安全返回插件…</p><script>
const p=new URLSearchParams(location.hash.slice(1));history.replaceState(null,'',location.pathname);
const f=document.createElement('form');f.method='post';f.action=location.pathname;
for(const k of ['code','error','state']){const i=document.createElement('input');i.type='hidden';i.name=k;i.value=p.get(k)||'';f.appendChild(i)}
document.body.appendChild(f);f.submit();
</script></body></html>""".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            expected_host = parsed.hostname + ":" + str(self.server.server_port)
            if self.path != callback_path or self.headers.get("Host") != expected_host or self.headers.get("Origin") != "http://" + expected_host:
                self.send_error(403)
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0].lower() != "application/x-www-form-urlencoded":
                self.send_error(415)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400)
                return
            if length < 1 or length > 4096:
                self.send_error(413)
                return
            try:
                fields = urllib.parse.parse_qs(self.rfile.read(length).decode("ascii", "strict"), keep_blank_values=True)
            except (UnicodeDecodeError, ValueError):
                self.send_error(400)
                return
            state = fields.get("state", [""])[0]
            if not hmac.compare_digest(state, expected_state):
                self.send_error(400)
                return
            code = fields.get("code", [""])[0]
            error = fields.get("error", [""])[0]
            if not code and not error:
                self.send_error(400)
                return
            result.update(code=code, error=error, state=state)
            body = "<!doctype html><meta name=referrer content=no-referrer><title>NoxGate</title><p>授权结果已返回插件，可以关闭此页面。</p>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            ready.set()
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    server = _CallbackServer((parsed.hostname, parsed.port), Handler)
    server.result = result
    server.ready = ready
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def random_url_secret(size: int = 32) -> str:
    return secrets.token_urlsafe(size)


def pkce_pair() -> tuple[str, str]:
    verifier = random_url_secret(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def auth_request(gateway: str, route: str, payload: dict | None = None, *, token: str = "", method: str = "POST") -> dict:
    gateway = validate_address(gateway)
    if not gateway.endswith("/v1"):
        raise AuthError("gateway_must_end_v1")
    url = gateway[:-3] + "/api/integrations/" + route
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None, headers=headers, method=method)
    handlers = [NoRedirect()]
    if urllib.parse.urlsplit(gateway).hostname in {"localhost", "127.0.0.1", "::1"}:
        handlers.append(urllib.request.ProxyHandler({}))
    try:
        with urllib.request.build_opener(*handlers).open(request, timeout=30) as response:
            envelope = json.loads(response.read(65537))
    except urllib.error.HTTPError as exc:
        try:
            code = json.loads(exc.read(65537)).get("code", "authorization_failed")
        except (ValueError, AttributeError):
            code = "authorization_failed"
        raise AuthError(code) from None
    except (OSError, ValueError):
        raise AuthError("connection_failed") from None
    if not isinstance(envelope, dict) or not envelope.get("success") or not isinstance(envelope.get("data"), dict):
        raise AuthError("invalid_response")
    return envelope["data"]


def login(name: str, gateway: str, web: str, agent: str, scopes: list[str], backend: str = "file", *, env_file: Path | None = None) -> dict:
    name = profile_name(name)
    gateway, web = validate_address(gateway), validate_address(web)
    if not set(scopes) <= SCOPES or not scopes:
        raise AuthError("invalid_scope")
    if backend == "env":
        raise AuthError("environment_store_requires_external_secret")
    if env_file is not None and backend != "portable-env":
        raise AuthError("env_file_requires_portable_env_store")
    if backend == "portable-env" and env_file is None:
        raise AuthError("portable_env_requires_explicit_file")
    store = CredentialStore(backend, env_file)  # Fail before starting if storage unavailable.
    profile = {"gateway": gateway, "web": web, "agent": agent, "scopes": scopes, "store": backend}
    if backend == "portable-env":
        profile["env_file"] = str(env_file.expanduser().resolve())
    verifier, challenge = pkce_pair()
    state = random_url_secret(24)
    callback = None
    bridge = None
    session = None
    try:
        callback = start_callback_server("http://127.0.0.1:0/callback", state)
        redirect_uri = "http://127.0.0.1:" + str(callback.server_port) + "/callback"
        session = auth_request(gateway, "authorize/start", {"client_id": "noxgate-cli", "profile_name": name, "agent_name": agent, "scopes": scopes,
            "redirect_uri": redirect_uri, "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
        bridge = open_authorization(web, session["request_token"], state)
        if bridge is None:
            raise AuthError("browser_required")
        print("请在浏览器登录 NoxGate，选择 API Key 并批准插件授权。", flush=True)
        deadline = time.monotonic() + min(int(session["expires_in"]), 600)
    except BaseException:
        if session:
            try:
                auth_request(gateway, "authorize/cancel", {"request_token": session["request_token"]})
            except Exception:
                pass
        if bridge:
            bridge.shutdown()
            bridge.server_close()
        if callback:
            callback.shutdown()
            callback.server_close()
        raise
    result = None
    saved = False
    try:
        callback.ready.wait(max(0.0, deadline - time.monotonic()))
        callback_result = callback.result
        if callback_result.get("error"):
            raise AuthError(callback_result["error"])
        if not callback_result.get("code"):
            raise AuthError("expired_token")
        result = auth_request(gateway, "authorize/exchange", {"authorization_code": callback_result["code"], "code_verifier": verifier})
        secret = result.pop("access_token")
        if result.get("profile") != name or not set(result["scopes"]) <= set(scopes):
            raise AuthError("invalid_response")
        profile.update(scopes=result["scopes"], credential_id=result["credential_id"], expires_at=time.time() + result["expires_in"])
        saved = True  # Clean up even a partially failing secure-store write.
        store.set(storage_key(name, profile), secret)
        with storage_lock("profiles"):
            config = read_profiles()
            previous = config.setdefault("profiles", {}).get(name)
            config["profiles"][name] = profile
            config["active"] = name
            atomic_json(config_home() / "profiles.json", config)
        if previous:
            try:
                old_store = profile_store(previous)
                old_key = storage_key(name, previous)
                old_secret = old_store.get(old_key)
                if old_secret:
                    auth_request(previous["gateway"], "credentials/revoke", token=old_secret)
                if previous.get("store") != "env":
                    old_store.delete(old_key)
            except Exception:
                print("新凭据已保存；旧授权未能撤销，请在网页授权列表撤销。", file=sys.stderr)
        return profile
    except BaseException:
        try:
            if saved:
                store.delete(storage_key(name, profile))
        except Exception:
            pass
        try:
            if result and "secret" in locals():
                auth_request(gateway, "credentials/revoke", token=secret)
            else:
                if session:
                    auth_request(gateway, "authorize/cancel", {"request_token": session["request_token"]})
        except Exception:
            pass
        raise
    finally:
        if bridge:
            bridge.shutdown()
            bridge.server_close()
        callback.shutdown()
        callback.server_close()


def ensure(capability: str, name: str | None = None, *, gateway: str | None = None,
           web: str | None = None, agent: str | None = None, backend: str | None = None,
           env_file: Path | None = None, scopes: list[str] | None = None) -> tuple[str, str]:
    name = profile_name(name)
    wanted = set(scopes or []) | {capability}
    if not wanted <= SCOPES:
        raise AuthError("invalid_scope")
    try:
        _, profile = get_profile(name)
    except AuthError as exc:
        if exc.code != "login_required":
            raise
        profile = None
    settings = {"gateway": validate_address(gateway) if gateway is not None else (profile or {}).get("gateway", DEFAULT_GATEWAY),
                "web": validate_address(web) if web is not None else (profile or {}).get("web", DEFAULT_WEB),
                "agent": agent if agent is not None else (profile or {}).get("agent", os.environ.get("NOXGATE_AGENT", "generic")),
                "store": backend if backend is not None else (profile or {}).get("store", "file"),
                "env_file": str(env_file.expanduser().resolve()) if env_file is not None else (profile or {}).get("env_file")}
    if settings["store"] == "portable-env" and not settings["env_file"]:
        raise AuthError("portable_env_requires_explicit_file")
    if env_file is not None and settings["store"] != "portable-env":
        raise AuthError("env_file_requires_portable_env_store")
    if settings["store"] != "portable-env":
        settings.pop("env_file", None)
    def authorize(current):
        requested = sorted(wanted | set((current or {}).get("scopes", [])))
        return login(name, settings["gateway"], settings["web"], settings["agent"], requested,
                     settings["store"], env_file=Path(settings["env_file"]) if settings.get("env_file") else None)
    if profile is None or any(profile.get(k) != v for k, v in settings.items()):
        profile = authorize(profile)
    token = profile_store(profile).get(storage_key(name, profile))
    if not token or not token.startswith("ngp_") or profile.get("expires_at", 0) <= time.time() or not wanted <= set(profile["scopes"]):
        profile = authorize(profile)
        token = profile_store(profile).get(storage_key(name, profile))
    if not token or not wanted <= set(profile["scopes"]):
        raise AuthError("requested_capability_not_approved")
    try:
        info = auth_request(profile["gateway"], "credentials/current", token=token, method="GET")
        if info.get("profile_name") != name or info.get("id") != profile.get("credential_id"):
            raise AuthError("credential_profile_mismatch")
    except AuthError as exc:
        if exc.code != "UNAUTHORIZED":
            raise
        profile = authorize(profile)
        token = profile_store(profile).get(storage_key(name, profile))
    if not token or not wanted <= set(profile["scopes"]):
        raise AuthError("requested_capability_not_approved")
    return profile["gateway"], token


def configure_environment(name: str, gateway: str, web: str, agent: str) -> None:
    name = profile_name(name)
    profile = {"gateway": validate_address(gateway), "web": validate_address(web), "agent": agent, "store": "env"}
    secret = CredentialStore("env").get(storage_key(name, profile))
    if not secret or not secret.startswith("ngp_"):
        raise AuthError("missing_environment_credential")
    info = auth_request(profile["gateway"], "credentials/current", token=secret, method="GET")
    if info["profile_name"] != name:
        raise AuthError("environment_profile_mismatch")
    from datetime import datetime
    profile.update(scopes=info["scopes"], credential_id=info["id"], expires_at=datetime.fromisoformat(info["expires_at"].replace("Z", "+00:00")).timestamp())
    with storage_lock("profiles"):
        config = read_profiles()
        config.setdefault("profiles", {})[name] = profile
        config["active"] = name
        atomic_json(config_home() / "profiles.json", config)


def migrate_storage(name: str, source_backend: str, target_backend: str, env_file: Path | None = None,
                    delete_source: bool = False) -> bool:
    name = profile_name(name)
    config = read_profiles()
    profile = config.get("profiles", {}).get(name)
    if not isinstance(profile, dict):
        raise AuthError("login_required")
    source_backend = source_backend or profile.get("store", "file")
    if source_backend != profile.get("store", "file"):
        raise AuthError("migration_source_does_not_match_profile")
    if source_backend == target_backend:
        raise AuthError("migration_backends_must_differ")
    if target_backend not in {"file", "system", "portable-env"}:
        raise AuthError("invalid_migration_target")
    if delete_source and source_backend == "env":
        raise AuthError("cannot_delete_injected_environment_secret")
    if target_backend == "portable-env" and env_file is None:
        raise AuthError("portable_env_requires_explicit_file")
    if env_file is not None and target_backend != "portable-env":
        raise AuthError("env_file_requires_portable_env_store")

    key = storage_key(name, profile)
    source = profile_store(profile)
    target = CredentialStore(target_backend, env_file)
    secret = source.get(key)
    if not secret or not secret.startswith("ngp_"):
        raise AuthError("source_credential_unavailable")
    if target.get(key):
        raise AuthError("target_profile_credential_already_exists")
    if not profile.get("credential_id"):
        raise AuthError("profile_credential_id_missing")

    current = auth_request(profile["gateway"], "credentials/current", token=secret, method="GET")
    if current.get("profile_name") != name or current.get("id") != profile.get("credential_id"):
        raise AuthError("credential_profile_mismatch")

    target_profile = dict(profile)
    target_profile["store"] = target_backend
    target_profile.pop("env_file", None)
    if target_backend == "portable-env":
        target_profile["env_file"] = str(env_file.expanduser().resolve())

    stored = False
    try:
        target.set(key, secret)
        stored = True
        reread = target.get(key)
        if not reread or not hmac.compare_digest(reread, secret):
            raise AuthError("migration_verification_failed")
        with storage_lock("profiles"):
            latest = read_profiles()
            latest_profile = latest.get("profiles", {}).get(name)
            if latest_profile != profile:
                raise AuthError("profile_changed_during_migration")
            latest["profiles"][name] = target_profile
            atomic_json(config_home() / "profiles.json", latest)
        stored = False  # The target is now the active profile and must be retained.
    except BaseException:
        if stored:
            try:
                target.delete(key)
            except Exception:
                pass
        raise

    source_removed = False
    if delete_source:
        try:
            source.delete(key)
            source_removed = True
        except CredentialError:
            # Metadata is already committed; leaving the old source is safe.
            source_removed = False
    return source_removed


def task_cli() -> int:
    from noxgate_runtime import MediaError, migrate_state
    parser = argparse.ArgumentParser(prog="noxgate tasks")
    parser.add_argument("command", choices=["migrate-state"])
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--source-gateway")
    parser.add_argument("--asset-root", type=Path)
    args = parser.parse_args(sys.argv[2:])
    try:
        migrate_state(args.state.expanduser(), args.profile, source_gateway=args.source_gateway, asset_root=args.asset_root)
        print("旧任务状态已迁移；未重新提交生成请求")
        return 0
    except (AuthError, CredentialError, MediaError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError):
        print("旧状态或缓存不可用；未输出秘密，未重新提交请求", file=sys.stderr)
        return 1


def cli() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "tasks":
        return task_cli()
    parser = argparse.ArgumentParser(prog="noxgate", epilog="存储迁移：noxgate auth migrate-storage --help；旧任务：noxgate tasks migrate-state --help")
    parser.add_argument("group", choices=["auth"])
    parser.add_argument("command", choices=["login", "ensure", "status", "list", "use", "logout", "revoke", "import-codex", "configure-env", "migrate-storage"])
    parser.add_argument("name", nargs="?")
    parser.add_argument("--profile")
    parser.add_argument("--gateway")
    parser.add_argument("--web")
    parser.add_argument("--agent")
    parser.add_argument("--scope", action="append", choices=sorted(SCOPES))
    parser.add_argument("--capability", choices=sorted(SCOPES), default="image")
    parser.add_argument("--store", choices=["system", "file", "portable-env", "env"], help="portable-env stores the plugin credential in plaintext in the explicitly selected .env file")
    parser.add_argument("--env-file", type=Path, help="explicit absolute .env path; portable-env is plaintext and stores only NoxGate plugin credential keys")
    parser.add_argument("--from", dest="source_store", choices=["system", "file", "portable-env", "env"])
    parser.add_argument("--to", dest="target_store", choices=["system", "file", "portable-env"])
    parser.add_argument("--delete-source", action="store_true", help="delete the source storage entry after successful migration")
    args = parser.parse_args()
    try:
        name = profile_name(args.profile or args.name)
        if args.command == "migrate-storage":
            if not args.source_store or not args.target_store:
                raise AuthError("migration_requires_from_and_to")
            removed = migrate_storage(name, args.source_store, args.target_store, args.env_file, args.delete_source)
            print("凭据存储迁移完成：" + name + ("；源存储项已删除" if removed else "；源存储已保留"))
        elif args.command in {"login", "import-codex"}:
            if args.command == "import-codex":
                # Explicit migration never copies or exposes the old API key.
                # Only import the non-secret gateway; browser consent chooses a key.
                import tomllib
                path = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "config.toml"
                cfg = tomllib.loads(path.read_text(encoding="utf-8"))
                selected = cfg.get("profiles", {}).get(cfg.get("profile"), {}).get("model_provider", cfg.get("model_provider"))
                args.gateway = validate_address(cfg["model_providers"][selected]["base_url"])
            login(name, args.gateway or DEFAULT_GATEWAY, args.web or DEFAULT_WEB, args.agent or os.environ.get("NOXGATE_AGENT", "generic"), args.scope or sorted(SCOPES), args.store or "file", env_file=args.env_file)
            print("NoxGate 授权已保存：" + name)
        elif args.command == "ensure":
            ensure(args.capability, name, gateway=args.gateway, web=args.web, agent=args.agent, backend=args.store, env_file=args.env_file, scopes=args.scope)
            print("NoxGate 认证可用：" + name)
        elif args.command == "configure-env":
            configure_environment(name, args.gateway or DEFAULT_GATEWAY, args.web or DEFAULT_WEB, args.agent or os.environ.get("NOXGATE_AGENT", "generic"))
            print("已配置外部 Secret Manager profile：" + name)
        elif args.command == "list":
            config = read_profiles()
            print(json.dumps({"active": config.get("active"), "profiles": list(config.get("profiles", {}))}, ensure_ascii=False))
        elif args.command == "use":
            get_profile(name)
            with storage_lock("profiles"):
                config = read_profiles()
                config["active"] = name
                atomic_json(config_home() / "profiles.json", config)
            print("当前 profile：" + name)
        else:
            _, profile = get_profile(name)
            store = profile_store(profile)
            key = storage_key(name, profile)
            token = store.get(key)
            if args.command == "status":
                if not token:
                    raise AuthError("login_required")
                info = auth_request(profile["gateway"], "credentials/current", token=token, method="GET")
                status = {k: info.get(k) for k in ("profile_name", "agent_name", "scopes", "expires_at", "revoked_at", "id")}
                status["store"] = profile.get("store", "file")
                if status["store"] == "portable-env":
                    status["storage_warning"] = "凭据以明文保存在显式指定的 .env 文件中；有权读取该文件的进程可使用此插件权限"
                print(json.dumps(status, ensure_ascii=False))
            else:
                if args.command == "revoke" and token:
                    auth_request(profile["gateway"], "credentials/revoke", token=token)
                store.delete(key)
                print("已撤销凭据并删除本地凭据" if args.command == "revoke" else "已删除本地凭据；网页仍可撤销远端授权")
        return 0
    except (AuthError, CredentialError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (ValueError, KeyError, TypeError, OSError):
        print("NoxGate 配置或存储无效；未输出任何秘密", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("授权已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(cli())
