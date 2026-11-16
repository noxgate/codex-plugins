"""Shared NoxGate transport. Bundled into each plugin; Python 3.11+, stdlib only."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Media entry points also use the isolated CLI environment created by install.py.
from noxgate_credentials import config_home

_venv = config_home() / "venv"
_python = _venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
if _python.is_file() and Path(sys.prefix).resolve() != _venv.resolve() and os.environ.get("NOXGATE_NO_REEXEC") != "1":
    os.execv(str(_python), [str(_python), *sys.argv])

ENDPOINTS = {"image": "images/generations", "audio": "audio/speech", "video": "videos/generations"}
SUCCESS = {"succeeded", "completed"}
FAILURE = {"failed", "canceled", "cancelled"}
MAX_BYTES = 512 * 1024 * 1024


class MediaError(Exception):
    pass


def load_provider(path: Path | None = None, capability: str = "image") -> tuple[str, str]:
    if path is not None:
        raise MediaError("不再读取 Codex 配置；请显式运行 noxgate auth import-codex")
    try:
        from noxgate_auth import ensure
        return ensure(capability)
    except Exception as exc:
        if isinstance(exc, MediaError):
            raise
        from noxgate_auth import AuthError, CredentialError
        if isinstance(exc, (AuthError, CredentialError)):
            raise MediaError(str(exc)) from None
        raise MediaError("NoxGate 认证未完成；请运行插件 bin/noxgate auth login，并在浏览器授权") from None


def validate_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise MediaError("媒体地址必须是无内嵌凭据的有效 URL")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise MediaError("媒体地址必须使用 HTTPS（本机调试允许 HTTP）")


def origin(url: str) -> tuple:
    p = urllib.parse.urlsplit(url)
    return p.scheme, p.hostname, p.port or (443 if p.scheme == "https" else 80)


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        if req.get_method() not in {"GET", "HEAD"}:
            raise MediaError("生成请求发生重定向，已停止；请检查当前 NoxGate profile 的地址")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and origin(req.full_url) != origin(newurl):
            redirected.remove_header("Authorization")
        return redirected


def request(url: str, token: str = "", *, method: str = "GET", body: bytes | None = None,
            content_type: str = "application/json", timeout: float = 120) -> tuple[bytes, str]:
    validate_url(url)
    headers = {"Accept": "application/json, audio/*, video/*, image/*"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    handlers: list[Any] = [SafeRedirect()]
    if urllib.parse.urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"}:
        handlers.append(urllib.request.ProxyHandler({}))
    try:
        with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise MediaError("媒体响应超过 512 MiB，已停止下载")
            return raw, response.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        # Provider error bodies may contain URLs, credentials or echoed inputs.
        raise MediaError(f"接口返回 HTTP {exc.code}；请检查模型权限、参数或网关日志") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise MediaError("接口请求失败或超时；提交结果可能未知，不会自动重新提交付费请求") from None


def json_object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise MediaError("接口返回了无效 JSON") from None
    if not isinstance(value, dict):
        raise MediaError("接口返回的 JSON 不是对象")
    return value


def request_json(url: str, token: str, *, method: str = "GET", payload: dict | None = None) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    return json_object(request(url, token, method=method, body=body)[0])


def atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".noxgate-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_state(path: Path, value: dict, token: str = "") -> None:
    # Never persist untrusted provider responses, signed URLs, headers or bodies.
    if "fingerprint" in value and ("kind" in value or "asset_id" in value):
        value = {k: v for k, v in value.items() if k in {"schemaVersion", "kind", "model", "status", "fingerprint", "output", "provider_hash", "profile", "task_id", "files", "recovery_id", "asset_id", "dimensions"}}
    encoded = json.dumps(value, ensure_ascii=False, indent=2)
    if token:
        encoded = encoded.replace(token, "[已隐藏]")
    atomic_write(path, (encoded + "\n").encode())


def result_items(kind: str, value: dict) -> list[dict]:
    if kind == "image":
        items = value.get("data")
    elif kind == "audio":
        items = value.get("audios") or value.get("data")
    else:
        item = value.get("result")
        items = [item] if isinstance(item, dict) else None
    if isinstance(items, list) and items:
        return [part for part in items if isinstance(part, dict) and (part.get("url") or part.get("b64_json"))]
    if value.get("url") or value.get("b64_json"):
        return [value]
    return []


def download_bytes(item: dict, base_url: str, token: str) -> bytes:
    if item.get("b64_json"):
        try:
            raw = base64.b64decode(item["b64_json"], validate=True)
        except (ValueError, TypeError):
            raise MediaError("媒体数据不是有效 Base64") from None
    else:
        address = urllib.parse.urljoin(base_url + "/", item["url"])
        validate_url(address)
        raw, _ = request(address, token if origin(address) == origin(base_url) else "")
    if not raw or len(raw) > MAX_BYTES:
        raise MediaError("媒体文件为空或过大")
    return raw


def validate_media(kind: str, raw: bytes, suffix: str) -> None:
    suffix = suffix.lower()
    if kind == "image":
        valid = raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"\xff\xd8\xff") or (raw.startswith(b"RIFF") and raw[8:12] == b"WEBP")
    elif kind == "video":
        valid = raw[4:8] == b"ftyp" or raw.startswith(b"\x1aE\xdf\xa3")
    else:
        valid = (raw.startswith((b"ID3", b"OggS", b"fLaC")) or (raw.startswith(b"RIFF") and raw[8:12] == b"WAVE")
                 or (len(raw) > 1 and raw[0] == 255 and raw[1] & 224 == 224) or raw[4:8] == b"ftyp"
                 or (suffix == ".pcm" and len(raw) % 2 == 0))
    if not valid:
        raise MediaError(f"返回内容不是有效的 {kind} 文件；保留任务状态以便检查")
    expected = {".png": raw.startswith(b"\x89PNG"), ".jpg": raw.startswith(b"\xff\xd8"), ".jpeg": raw.startswith(b"\xff\xd8"),
                ".webp": raw[8:12] == b"WEBP", ".wav": raw[8:12] == b"WAVE", ".ogg": raw.startswith(b"OggS"),
                ".flac": raw.startswith(b"fLaC"), ".mp4": raw[4:8] == b"ftyp", ".webm": raw.startswith(b"\x1aE\xdf\xa3")}
    if suffix in expected and not expected[suffix]:
        raise MediaError("文件扩展名与实际媒体格式不符；请调整输出路径后使用 resume")


def save_outputs(kind: str, result: dict, base_url: str, token: str, output: Path) -> list[str]:
    items = result_items(kind, result)
    if not items:
        raise MediaError("任务已结束但没有可下载的媒体结果")
    paths = []
    for index, item in enumerate(items, 1):
        path = output if len(items) == 1 else output.with_name(f"{output.stem}_{index:02}{output.suffix}")
        raw = download_bytes(item, base_url, token)
        validate_media(kind, raw, path.suffix)
        atomic_write(path, raw)
        paths.append(str(path.resolve()))
    return paths


def task_url(base: str, kind: str, task_id: str, model: str = "") -> str:
    url = f"{base}/{ENDPOINTS[kind]}/{urllib.parse.quote(task_id, safe='')}"
    return url + ("?" + urllib.parse.urlencode({"model": model}) if model else "")


def wait_for_result(base: str, token: str, kind: str, result: dict, state: dict, state_path: Path,
                    timeout: float, interval: float) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        status = result.get("status")
        if status in FAILURE:
            state.update(status=status, result=result)
            save_state(state_path, state, token)
            raise MediaError(f"任务以 {status} 状态结束；不会自动重生")
        if status in SUCCESS or (not status and result_items(kind, result)):
            return result
        task_id = result.get("id") or result.get("task_id") or state.get("task_id")
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", task_id):
            raise MediaError("异步响应缺少任务 ID；请检查网关记录，不要重新提交")
        state.update(status="pending", task_id=task_id, result=result)
        save_state(state_path, state, token)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MediaError("等待任务超时；任务 ID 已保存，请用 resume 继续查询")
        time.sleep(min(interval, remaining))
        result = request_json(task_url(base, kind, task_id, state.get("model", "")), token)
        interval = min(interval * 1.3, 15)


def cache_result(state: dict, state_path: Path, kind: str, result: dict, base: str, token: str) -> dict:
    from noxgate_recovery import RecoveryError, RecoveryMissing, discard, recover, stage
    from noxgate_credentials import CredentialError
    try:
        if state.get("recovery_id"):
            try:
                return recover(state, kind, lambda url: download_bytes({"url": url}, base, token))
            except RecoveryMissing:
                if not state.get("task_id") or not result_items(kind, result):
                    raise
                state.pop("recovery_id", None)
        parts = result_items(kind, result)
        for part in parts:
            if part.get("url"):
                part["url"] = urllib.parse.urljoin(base + "/", part["url"])
                validate_url(part["url"])
        # Async URLs can be fetched again via the task ID; keep only bytes.
        # Read-only injected credentials can download directly but cannot journal URLs.
        from noxgate_auth import get_profile
        read_only_store = False
        if not state.get("task_id") and any(part.get("url") for part in parts):
            _, profile = get_profile(state["profile"])
            read_only_store = profile.get("store", "file") in {"env", "portable-env"}
        if state.get("task_id") or read_only_store:
            parts = [{"b64_json": base64.b64encode(download_bytes(part, base, token)).decode()} for part in parts]
        identifier = stage(state, kind, parts)
        state["recovery_id"] = identifier
        try:
            save_state(state_path, state, token)
        except BaseException:
            state.pop("recovery_id", None)
            discard(identifier)
            raise
        return recover(state, kind, lambda url: download_bytes({"url": url}, base, token))
    except RecoveryMissing:
        raise
    except (RecoveryError, CredentialError) as exc:
        raise MediaError(str(exc)) from None


def generate(kind: str, payload: dict, output: Path, state_path: Path, *, timeout: float = 900,
             interval: float = 2, body: bytes | None = None, content_type: str = "application/json",
             endpoint: str | None = None, provider: tuple[str, str] | None = None) -> list[str]:
    validate_payload(payload, kind)
    base, token = provider or load_provider(capability=kind)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode() + (body or b"")).hexdigest()
    if state_path.exists():
        state = json_object(state_path.read_bytes())
        if state.get("fingerprint") != digest:
            raise MediaError("相同状态路径对应的请求已变化；请保留原状态并使用新的输出路径")
        return resume(state_path, output=output, timeout=timeout, interval=interval, provider=(base, token))
    state = {"schemaVersion": 1, "kind": kind, "model": payload.get("model"), "status": "submitting",
             "fingerprint": digest, "output": str(output.resolve()),
             "provider_hash": hashlib.sha256(base.encode()).hexdigest(),
             "profile": __import__("noxgate_auth").profile_name()}
    # Exclusive reservation prevents two processes from submitting the same job.
    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with state_path.open("x", encoding="utf-8") as handle:
            json.dump(state, handle)
    except FileExistsError:
        raise MediaError("该任务已由其他进程占用；请检查状态后使用 resume") from None
    raw, mime = request(f"{base}/{endpoint or ENDPOINTS[kind]}", token, method="POST",
                        body=body if body is not None else json.dumps(payload, ensure_ascii=False).encode(),
                        content_type=content_type, timeout=300)
    if mime != "application/json" and not mime.endswith("+json"):
        # Extract media bytes; the private journal persists them before output writes.
        result = {"b64_json": base64.b64encode(raw).decode(), "status": "succeeded"}
    else:
        result = json_object(raw)
    task_id = result.get("id") or result.get("task_id")
    if task_id is not None and (not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", task_id)):
        raise MediaError("任务 ID 格式无效；请检查网关记录，不会重新提交")
    state.update(status="received", result=result, task_id=task_id)
    save_state(state_path, state, token)
    result = wait_for_result(base, token, kind, result, state, state_path, timeout, interval)
    result = cache_result(state, state_path, kind, result, base, token)
    paths = save_outputs(kind, result, base, token, output)
    state.update(status="completed", files=paths)
    save_state(state_path, state, token)
    return paths


def resume(state_path: Path, *, output: Path | None = None, timeout: float = 900, interval: float = 2,
           provider: tuple[str, str] | None = None) -> list[str]:
    state = json_object(state_path.read_bytes())
    kind = state.get("kind")
    if kind not in ENDPOINTS:
        raise MediaError("无法识别的任务状态文件")
    from noxgate_auth import profile_name
    if "profile" not in state:
        raise MediaError("旧任务需要显式迁移：noxgate tasks migrate-state --state PATH --profile NAME")
    if state.get("profile") != profile_name():
        raise MediaError("任务 profile 不一致；请切回原 profile")
    base, token = provider or load_provider(capability=kind)
    if state.get("provider_hash") != hashlib.sha256(base.encode()).hexdigest():
        raise MediaError("当前 NoxGate profile 地址与提交任务时不同；请切回原配置后继续")
    if state.get("status") == "submitting":
        raise MediaError("提交结果未知；请先检查网关记录，不能自动重新提交")
    if state.get("status") == "completed" and (output is None or str(output.resolve()) == state.get("output")):
        paths = state.get("files", [])
        if paths and all(Path(p).is_file() for p in paths):
            return paths
        # Redownload saved results without creating a new paid task.
    result = None
    if state.get("recovery_id"):
        from noxgate_recovery import RecoveryMissing, recover
        try:
            result = recover(state, kind, lambda url: download_bytes({"url": url}, base, token))
        except RecoveryMissing:
            if not state.get("task_id"):
                raise MediaError("同步媒体缓存已丢失；不会自动重新提交付费任务") from None
            state.pop("recovery_id", None)
        except Exception as exc:
            from noxgate_recovery import RecoveryError
            from noxgate_credentials import CredentialError
            if isinstance(exc, (RecoveryError, CredentialError)):
                raise MediaError(str(exc)) from None
            raise
    if result is None:
        if not state.get("task_id"):
            raise MediaError("该旧同步任务没有恢复缓存；请检查已有文件或网关记录，不会重新提交付费请求")
        result = request_json(task_url(base, kind, state["task_id"], state.get("model", "")), token)
        result = wait_for_result(base, token, kind, result, state, state_path, timeout, interval)
        state.update(status="received")
        save_state(state_path, state, token)
        result = cache_result(state, state_path, kind, result, base, token)
    paths = save_outputs(kind, result, base, token, output or Path(state["output"]))
    state.update(status="completed", files=paths)
    save_state(state_path, state, token)
    return paths


def migrate_state(state_path: Path, name: str, *, source_gateway: str | None = None, asset_root: Path | None = None) -> None:
    """Explicitly adopt legacy metadata; online tasks are authorized by the gateway."""
    from noxgate_auth import ensure, get_profile, profile_name, validate_address
    from noxgate_credentials import storage_lock
    name = profile_name(name)
    _, profile = get_profile(name)
    base = validate_address(profile["gateway"])
    state = json_object(state_path.read_bytes())
    snapshot = dict(state)
    if state.get("profile"):
        raise MediaError("状态已绑定 profile；不能通过迁移重新绑定")
    is_asset = "asset_id" in state
    kind = "image" if is_asset else state.get("kind")
    if kind not in ENDPOINTS or not isinstance(state.get("fingerprint"), str):
        raise MediaError("无法识别的旧任务状态")
    if state.get("status") == "submitting":
        raise MediaError("提交结果未知；请先检查网关记录，不能迁移或重新提交")
    gateway_hash = hashlib.sha256(base.encode()).hexdigest()
    if state.get("provider_hash"):
        if state["provider_hash"] != gateway_hash:
            raise MediaError("旧任务网关与目标 profile 不一致")
    elif not is_asset or not source_gateway or validate_address(source_gateway) != base:
        raise MediaError("旧游戏资产缺少网关指纹；请显式指定与目标 profile 一致的 --source-gateway")
    result = None
    files = state.get("files", [])
    if is_asset:
        expected_root = state_path.resolve().parent.parent
        if asset_root is None or asset_root.expanduser().resolve() != expected_root:
            raise MediaError("游戏资产迁移必须显式指定状态 _state 目录对应的 --asset-root")
        root = asset_root.expanduser().resolve()
        paths = [root / path for path in files]
        if any(not path.resolve().is_relative_to(root) for path in paths):
            raise MediaError("旧资产文件路径超出资产目录")
    else:
        paths = [Path(path).expanduser() for path in files]
    offline_complete = state.get("status") in {"completed", "needs_review"} and bool(paths) and all(path.is_file() for path in paths)
    # Do not trust a legacy URL/body; only extract media bytes from a local sync result.
    previous = state.get("result")
    local_parts = result_items(kind, previous) if isinstance(previous, dict) else []
    local_media = bool(local_parts) and all(isinstance(part.get("b64_json"), str) for part in local_parts)
    if not offline_complete and not local_media:
        task_id = state.get("task_id")
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", task_id):
            raise MediaError("旧任务没有可恢复媒体或任务 ID；无法安全迁移，不会重新提交")
        authenticated_base, token = ensure(kind, name)
        if authenticated_base != base:
            raise MediaError("迁移期间 profile 网关发生变化")
        # The server checks user Token ownership, media capability and current model rights.
        result = request_json(task_url(base, kind, task_id, state.get("model", "")), token)
        if not state.get("model") and isinstance(result.get("model"), str):
            state["model"] = result["model"]
    allowed = {"schemaVersion", "kind", "model", "status", "fingerprint", "output", "provider_hash", "task_id", "files", "asset_id", "dimensions"}
    clean = {k: v for k, v in state.items() if k in allowed}
    clean.update(schemaVersion=2, profile=name, provider_hash=gateway_hash)
    with storage_lock("task-migration"):
        current = json_object(state_path.read_bytes())
        if current != snapshot:
            raise MediaError("迁移期间任务状态已变化；请重新检查")
        identifier = None
        if local_media and not offline_complete:
            from noxgate_recovery import RecoveryError, discard, stage
            try:
                identifier = stage(clean, kind, local_parts)
            except RecoveryError as exc:
                raise MediaError(str(exc)) from None
            clean.update(recovery_id=identifier, status="received")
        try:
            save_state(state_path, clean)
        except BaseException:
            if identifier:
                discard(identifier)
            raise


def validate_payload(value: dict, kind: str) -> dict:
    if not isinstance(value, dict):
        raise MediaError("请求必须是 JSON 对象")
    if kind not in ENDPOINTS:
        raise MediaError("不支持的媒体类型")
    if any(not isinstance(key, str) for key in value):
        raise MediaError("请求字段名称无效")
    if not isinstance(value.get("model"), str) or not value["model"].strip():
        raise MediaError("请求必须指定可调用的 model")
    if kind == "audio" and bool(value.get("input")) == bool(value.get("prompt")):
        raise MediaError("音频请求必须在 input 和 prompt 中选择一个")
    if kind == "video" and not any(value.get(key) for key in ("prompt", "image_url", "image_urls", "media")):
        raise MediaError("视频请求缺少提示词或参考媒体")
    if kind == "image" and (not isinstance(value.get("prompt"), str) or not value["prompt"].strip()):
        raise MediaError("图片请求需要非空 prompt")
    if kind == "video" and ("image_url" in value or "image_urls" in value or "media" in value):
        # Local files must not be mistaken for publicly reachable upstream URLs.
        for key in ("image_url", "image_urls", "video_url", "audio_url"):
            urls = value.get(key, [])
            if isinstance(urls, str): urls = [urls]
            if not isinstance(urls, list) or any(not isinstance(url, str) or not url.startswith("https://") for url in urls):
                raise MediaError("视频参考媒体必须是上游可访问的 HTTPS URL")
    if any(key in value for key in ("api_key", "base_url", "token", "authorization")):
        raise MediaError("请求文件不得包含连接地址或凭据；使用独立 NoxGate profile")
    return value


def read_payload(path: Path, kind: str) -> dict:
    return validate_payload(json_object(path.read_bytes()), kind)


def cli(kind: str) -> int:
    parser = argparse.ArgumentParser(description=f"NoxGate {kind} generation")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("generate")
    create.add_argument("--request", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--state", type=Path)
    restore = sub.add_parser("resume")
    restore.add_argument("--state", type=Path, required=True)
    restore.add_argument("--output", type=Path)
    for p in (create, restore):
        p.add_argument("--timeout", type=float, default=1800 if kind == "video" else 900)
        p.add_argument("--poll-interval", type=float, default=2)
    for name in ("status", "cancel") if kind == "video" else ("status",):
        command = sub.add_parser(name)
        command.add_argument("--task-id", required=True)
        command.add_argument("--model", default="")
    sub.add_parser("models")
    args = parser.parse_args()
    try:
        if args.command in {"generate", "resume"}:
            if args.timeout <= 0 or args.poll_interval <= 0:
                raise MediaError("等待时间和轮询间隔必须大于零")
            if args.command == "generate":
                payload = read_payload(args.request, kind)
                paths = generate(kind, payload, args.output.expanduser(), args.state or Path(str(args.output) + ".task.json"),
                                 timeout=args.timeout, interval=args.poll_interval)
            else:
                paths = resume(args.state, output=args.output, timeout=args.timeout, interval=args.poll_interval)
            print("\n".join(paths))
        else:
            base, token = load_provider(capability=kind)
            if args.command == "models":
                value = request_json(base + "/models", token)
                for model in value.get("data", []):
                    print(model.get("id", ""))
            elif args.command == "cancel":
                request(task_url(base, kind, args.task_id, args.model), token, method="DELETE")
                print("已请求取消视频任务；结果取决于上游当前状态")
            else:
                value = request_json(task_url(base, kind, args.task_id, args.model), token)
                print(json.dumps({key: value.get(key) for key in ("id", "status", "progress")}, ensure_ascii=False))
        return 0
    except (MediaError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
