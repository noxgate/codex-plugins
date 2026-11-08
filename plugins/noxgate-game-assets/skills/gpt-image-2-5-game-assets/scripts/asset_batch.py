#!/usr/bin/env python3
"""Generate a bounded, resumable batch of static game assets through NoxGate."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any
PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT / "lib" if (PLUGIN_ROOT / "lib/noxgate_runtime.py").is_file() else PLUGIN_ROOT.parent / "shared"))
from noxgate_runtime import MediaError, load_provider, request_json as media_request_json, download_bytes

try:
    from PIL import Image, ImageOps, UnidentifiedImageError
except ImportError as exc:
    raise SystemExit("Pillow is required: install it in the selected Python environment") from exc


CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
DEFAULT_CONFIG = CODEX_HOME / "config.toml"
MODELS = {"gpt-image-2.5-flare", "gpt-image-2.5-sunburst"}
FORMATS = {"png": "PNG", "jpeg": "JPEG", "webp": "WEBP"}
ASSET_ID = re.compile(r"^[a-z][a-z0-9_-]{1,79}$")
DIMENSIONS = re.compile(r"^([1-9][0-9]*)x([1-9][0-9]*)$")
MAX_BYTES = 64 * 1024 * 1024
ASSET_FIELDS = {
    "id", "prompt", "model", "size", "resolution", "quality", "background",
    "output_format", "n", "image_urls", "mask_url", "target_size",
}


AssetError = MediaError


def open_url(request: urllib.request.Request, timeout: float):
    host = urllib.parse.urlsplit(request.full_url).hostname
    if host in {"localhost", "127.0.0.1"}:
        return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def dimensions(value: str) -> tuple[int, int]:
    match = DIMENSIONS.fullmatch(value)
    if not match:
        raise AssetError(f"invalid dimensions: {value!r}; expected WIDTHxHEIGHT")
    result = int(match[1]), int(match[2])
    if result[0] > 16384 or result[1] > 16384:
        raise AssetError("dimensions exceed 16384 pixels")
    return result


def load_config(path: Path) -> tuple[str, str, set[str]]:
    base, key = load_provider(path)
    return base, key, MODELS


def load_manifest(path: Path, configured_models: set[str]) -> list[dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssetError("cannot read manifest JSON") from exc
    if not isinstance(document, dict) or set(document) - {"defaults", "assets"}:
        raise AssetError("manifest must contain only defaults and assets")
    defaults = document.get("defaults", {})
    entries = document.get("assets")
    if not isinstance(defaults, dict) or not isinstance(entries, list) or not entries:
        raise AssetError("manifest requires a nonempty assets array and object defaults")
    if set(defaults) - (ASSET_FIELDS - {"id", "prompt"}):
        raise AssetError("defaults contain unknown fields")
    seen: set[str] = set()
    assets: list[dict[str, Any]] = []
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict) or set(entry) - ASSET_FIELDS:
            raise AssetError(f"asset {index}: unknown fields")
        item = {**defaults, **entry}
        asset_id = item.get("id")
        if not isinstance(asset_id, str) or not ASSET_ID.fullmatch(asset_id) or asset_id in seen:
            raise AssetError(f"asset {index}: invalid or duplicate id")
        seen.add(asset_id)
        if not isinstance(item.get("prompt"), str) or not item["prompt"].strip():
            raise AssetError(f"{asset_id}: prompt is required")
        if item.get("model") not in MODELS or item["model"] not in configured_models:
            raise AssetError(f"{asset_id}: model is not an enabled GPT Image 2.5 model")
        item.setdefault("size", "1024x1024")
        item.setdefault("quality", "high")
        item.setdefault("background", "transparent")
        item.setdefault("output_format", "png")
        item.setdefault("n", 1)
        if item["size"] != "auto":
            dimensions(item["size"])
        if item["quality"] not in {"low", "medium", "high", "xhigh", "max", "auto"}:
            raise AssetError(f"{asset_id}: unsupported quality")
        if item["background"] not in {"opaque", "transparent"}:
            raise AssetError(f"{asset_id}: background must be opaque or transparent")
        if item["output_format"] not in FORMATS or (item["background"] == "transparent" and item["output_format"] == "jpeg"):
            raise AssetError(f"{asset_id}: invalid output format/background combination")
        if type(item["n"]) is not int or not 1 <= item["n"] <= 10:
            raise AssetError(f"{asset_id}: n must be an integer from 1 to 10")
        if "target_size" in item:
            dimensions(item["target_size"])
        if "resolution" in item and item["resolution"] not in {"1K", "2K", "4K"}:
            raise AssetError(f"{asset_id}: invalid resolution")
        urls = item.get("image_urls", [])
        if not isinstance(urls, list) or len(urls) > 16 or any(not isinstance(url, str) or not url.startswith("https://") for url in urls):
            raise AssetError(f"{asset_id}: image_urls must be up to 16 HTTPS URLs")
        if "mask_url" in item and (not urls or not isinstance(item["mask_url"], str) or not item["mask_url"].startswith("https://")):
            raise AssetError(f"{asset_id}: mask_url requires image_urls and must be HTTPS")
        assets.append(item)
    return assets


def fingerprint(item: dict[str, Any]) -> str:
    encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def request_json(method: str, url: str, key: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return media_request_json(url, key, method=method, payload=payload)


def wait_for_result(base_url: str, key: str, task_id: str, poll_interval: float, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        url = f"{base_url}/images/generations/{urllib.parse.quote(task_id, safe='')}"
        result = request_json("GET", url, key)
        status = result.get("status")
        if status in {"succeeded", "completed"}:
            return result
        if status in {"failed", "canceled", "cancelled"}:
            raise AssetError(f"gateway task ended with status {status}")
        if time.monotonic() >= deadline:
            raise AssetError("poll timeout; task ID is saved for the next run")
        time.sleep(poll_interval)


def image_data(result: dict[str, Any]) -> list[dict[str, Any]]:
    data = result.get("data")
    if not isinstance(data, list) or not data or any(not isinstance(part, dict) or not (part.get("url") or part.get("b64_json")) for part in data):
        raise AssetError("completed task has no usable image data")
    return data


def read_image_bytes(part: dict[str, Any], base_url: str, key: str) -> bytes:
    raw = download_bytes(part, base_url, key)
    if len(raw) > MAX_BYTES:
        raise AssetError("image exceeds 64 MiB")
    return raw


def render_image(raw: bytes, item: dict[str, Any]) -> tuple[bytes, tuple[int, int], bool]:
    try:
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            image = ImageOps.exif_transpose(source).copy()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise AssetError("downloaded data is not a valid image") from exc
    if image.width * image.height > 64_000_000:
        raise AssetError("image exceeds 64 million pixels")
    if item["background"] == "transparent":
        image = image.convert("RGBA")
        has_transparency = image.getchannel("A").getextrema()[0] < 255
    else:
        image = image.convert("RGB")
        has_transparency = False
    if "target_size" in item:
        width, height = dimensions(item["target_size"])
        if item["background"] == "transparent":
            image.thumbnail((width, height), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            canvas.alpha_composite(image, ((width - image.width) // 2, (height - image.height) // 2))
            image = canvas
        else:
            image = ImageOps.fit(image, (width, height), method=Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format=FORMATS[item["output_format"]])
    return output.getvalue(), image.size, has_transparency


def write_image(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".image-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def finish_asset(item: dict[str, Any], result: dict[str, Any], output_dir: Path, state_path: Path, state: dict[str, Any], base_url: str, key: str) -> str:
    parts = image_data(result)
    if len(parts) != item["n"]:
        raise AssetError(f"expected {item['n']} images, received {len(parts)}")
    files: list[str] = []
    review = False
    for number, part in enumerate(parts, 1):
        raw = read_image_bytes(part, base_url, key)
        content, size, alpha = render_image(raw, item)
        if item["background"] == "transparent" and not alpha:
            review = True
        filename = f"{item['id']}__{number:02}.{item['output_format']}"
        relative = f"_review/{filename}"
        write_image(output_dir / relative, content)
        files.append(relative)
        state["dimensions"] = list(size)
    if not review:
        for index, relative in enumerate(files):
            filename = Path(relative).name
            os.replace(output_dir / relative, output_dir / filename)
            files[index] = filename
    state["status"] = "needs_review" if review else "completed"
    state["files"] = files
    atomic_json(state_path, state)
    return state["status"]


def run_one(item: dict[str, Any], output_dir: Path, base_url: str, key: str, poll_interval: float, timeout: float, may_submit: bool) -> str:
    state_path = output_dir / "_state" / f"{item['id']}.json"
    digest = fingerprint(item)
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("fingerprint") != digest:
            raise AssetError("manifest changed for an existing asset; use a new ID or review its state")
        if state.get("status") in {"completed", "needs_review"}:
            if all((output_dir / path).is_file() for path in state.get("files", [])) and state.get("files"):
                return state["status"]
            raise AssetError("previous output is missing; inspect state before resubmitting")
        if state.get("status") == "received" and state.get("result"):
            return finish_asset(item, state["result"], output_dir, state_path, state, base_url, key)
        if state.get("status") == "submitting":
            raise AssetError("submission outcome unknown; inspect gateway before retrying")
        if state.get("status") != "pending" or not state.get("task_id"):
            raise AssetError("unrecognized saved task state")
        result = wait_for_result(base_url, key, state["task_id"], poll_interval, timeout)
        return finish_asset(item, result, output_dir, state_path, state, base_url, key)
    if not may_submit:
        return "not_submitted"
    state = {"asset_id": item["id"], "fingerprint": digest, "status": "submitting"}
    atomic_json(state_path, state)
    fields = ("model", "prompt", "size", "resolution", "quality", "background", "output_format", "n", "image_urls", "mask_url")
    payload = {field: item[field] for field in fields if field in item}
    try:
        result = request_json("POST", f"{base_url}/images/generations", key, payload)
    except AssetError:
        raise
    status = result.get("status")
    if status in {"failed", "canceled", "cancelled"}:
        state["status"] = "failed"
        atomic_json(state_path, state)
        raise AssetError(f"gateway rejected task with status {status}")
    if status not in {None, "succeeded", "completed"}:
        if not isinstance(result.get("id"), str) or not result["id"]:
            raise AssetError("asynchronous response has no task ID")
        state.update(status="pending", task_id=result["id"])
        atomic_json(state_path, state)
        result = wait_for_result(base_url, key, result["id"], poll_interval, timeout)
    state.update(status="received", result=result)
    atomic_json(state_path, state)
    return finish_asset(item, result, output_dir, state_path, state, base_url, key)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "run"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--max-new-tasks", type=int)
    parser.add_argument("--only", action="append", metavar="ASSET_ID", help="process only this asset ID; repeat for multiple IDs")
    parser.add_argument("--poll-interval", type=float, default=3.0)
    parser.add_argument("--poll-timeout", type=float, default=900.0)
    args = parser.parse_args()
    if args.command == "run" and (args.output_dir is None or args.max_new_tasks is None or args.max_new_tasks < 0):
        parser.error("run requires --output-dir and --max-new-tasks >= 0")
    if args.poll_interval <= 0 or args.poll_timeout <= 0:
        parser.error("poll timings must be positive")
    try:
        base_url, key, configured_models = load_config(args.config)
        assets = load_manifest(args.manifest, configured_models)
        if args.only:
            selected = set(args.only)
            unknown = selected - {item["id"] for item in assets}
            if unknown:
                raise AssetError(f"unknown selected asset IDs: {', '.join(sorted(unknown))}")
            assets = [item for item in assets if item["id"] in selected]
        if args.command == "plan":
            counts = Counter(item["model"] for item in assets)
            print(f"assets={len(assets)} images={sum(item['n'] for item in assets)}")
            for model, count in sorted(counts.items()):
                print(f"{model}: {count} tasks")
            return 0
        output_dir = args.output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        submissions = 0
        failures = 0
        incomplete = 0
        for item in assets:
            state_path = output_dir / "_state" / f"{item['id']}.json"
            existed_before = state_path.exists()
            try:
                status = run_one(
                    item, output_dir, base_url, key, args.poll_interval, args.poll_timeout,
                    submissions < args.max_new_tasks,
                )
                incomplete += status == "not_submitted"
                failures += status == "needs_review"
                print(f"{item['id']}: {status}")
            except (AssetError, OSError, ValueError) as exc:
                failures += 1
                print(f"{item['id']}: error: {exc}", file=sys.stderr)
            finally:
                if not existed_before and state_path.exists():
                    submissions += 1
        print(f"new_tasks={submissions} failures={failures} pending_submission={incomplete}")
        return 1 if failures or incomplete else 0
    except AssetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
