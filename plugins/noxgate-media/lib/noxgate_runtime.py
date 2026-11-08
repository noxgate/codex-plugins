"""Shared NoxGate transport. Bundled into each plugin; Python 3.11+, stdlib only."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ENDPOINTS = {"image": "images/generations", "audio": "audio/speech", "video": "videos/generations"}
SUCCESS = {"succeeded", "completed"}
FAILURE = {"failed", "canceled", "cancelled"}
MAX_BYTES = 512 * 1024 * 1024


class MediaError(Exception):
    pass


def config_path() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser() / "config.toml"


def load_provider(path: Path | None = None) -> tuple[str, str]:
    path = path or config_path()
    try:
        config = tomllib.loads(path.expanduser().read_text(encoding="utf-8"))
        profile = config.get("profiles", {}).get(config.get("profile"), {})
        name = profile.get("model_provider", config.get("model_provider"))
        provider = config["model_providers"][name]
        address = provider["base_url"]
        if not isinstance(address, str):
            raise ValueError()
        address = address.rstrip("/")
        validate_url(address)
        token = provider.get("experimental_bearer_token")
        if not token and provider.get("env_key"):
            token = os.environ.get(provider["env_key"])
        if not token and provider.get("requires_openai_auth"):
            token = os.environ.get("OPENAI_API_KEY")
            if not token:
                auth = json.loads((path.parent / "auth.json").read_text(encoding="utf-8"))
                token = auth.get("OPENAI_API_KEY")
        if not isinstance(token, str) or not token.strip():
            raise MediaError("当前 Codex provider 缺少可读取的 API 凭据；请检查其 bearer token 或 env_key 配置")
        return address, token.strip()
    except MediaError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise MediaError("无法读取当前 Codex provider 配置；请先完成 Codex 接入配置") from None


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
            raise MediaError("生成请求发生重定向，已停止；请检查当前 provider 的地址")
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
    encoded = json.dumps(value, ensure_ascii=False, indent=2)
    if token:
        encoded = encoded.replace(token, "[已隐藏]")
    atomic_write(path, (encoded + "\n").encode())


def result_items(kind: str, value: dict) -> list[dict]:
    if kind == "image":
        items = value.get("data")
    elif kind == "audio":
        items = value.get("audios")
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
        task_id = result.get("id") or state.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise MediaError("异步响应缺少任务 ID；请检查网关记录，不要重新提交")
        state.update(status="pending", task_id=task_id, result=result)
        save_state(state_path, state, token)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MediaError("等待任务超时；任务 ID 已保存，请用 resume 继续查询")
        time.sleep(min(interval, remaining))
        result = request_json(task_url(base, kind, task_id, state.get("model", "")), token)
        interval = min(interval * 1.3, 15)


def generate(kind: str, payload: dict, output: Path, state_path: Path, *, timeout: float = 900,
             interval: float = 2, body: bytes | None = None, content_type: str = "application/json",
             endpoint: str | None = None, provider: tuple[str, str] | None = None) -> list[str]:
    validate_payload(payload, kind)
    base, token = provider or load_provider()
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode() + (body or b"")).hexdigest()
    if state_path.exists():
        state = json_object(state_path.read_bytes())
        if state.get("fingerprint") != digest:
            raise MediaError("相同状态路径对应的请求已变化；请保留原状态并使用新的输出路径")
        return resume(state_path, output=output, timeout=timeout, interval=interval, provider=(base, token))
    state = {"schemaVersion": 1, "kind": kind, "model": payload.get("model"), "status": "submitting",
             "fingerprint": digest, "output": str(output.resolve()),
             "provider_hash": hashlib.sha256(base.encode()).hexdigest()}
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
        # Persist the sync response before validation/download so failures never trigger a new POST.
        result = {"b64_json": base64.b64encode(raw).decode(), "status": "succeeded"}
    else:
        result = json_object(raw)
    state.update(status="received", result=result, task_id=result.get("id"))
    save_state(state_path, state, token)
    return resume(state_path, output=output, timeout=timeout, interval=interval, provider=(base, token))


def resume(state_path: Path, *, output: Path | None = None, timeout: float = 900, interval: float = 2,
           provider: tuple[str, str] | None = None) -> list[str]:
    base, token = provider or load_provider()
    state = json_object(state_path.read_bytes())
    if state.get("provider_hash") != hashlib.sha256(base.encode()).hexdigest():
        raise MediaError("当前 provider 地址与提交任务时不同；请切回原配置后继续")
    kind = state.get("kind")
    if kind not in ENDPOINTS:
        raise MediaError("无法识别的任务状态文件")
    if state.get("status") == "submitting":
        raise MediaError("提交结果未知；请先检查网关记录，不能自动重新提交")
    if state.get("status") == "completed" and output is None:
        paths = state.get("files", [])
        if paths and all(Path(p).is_file() for p in paths):
            return paths
        # Redownload saved results without creating a new paid task.
    result = wait_for_result(base, token, kind, state.get("result", {}), state, state_path, timeout, interval)
    state.update(status="received", result=result)
    save_state(state_path, state, token)
    paths = save_outputs(kind, result, base, token, output or Path(state["output"]))
    state.update(status="completed", files=paths)
    save_state(state_path, state, token)
    return paths


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
        raise MediaError("请求文件不得包含连接地址或凭据；使用当前 Codex 配置")
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
            base, token = load_provider()
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
