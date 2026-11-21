"""Private media journal. Raw responses and authorization secrets are never cached."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from pathlib import Path

from noxgate_credentials import CredentialStore, atomic_json, config_home

MAX_BYTES = 512 * 1024 * 1024


class RecoveryError(Exception):
    pass


class RecoveryMissing(RecoveryError):
    pass


def _directory(identifier: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise RecoveryError("媒体恢复引用无效")
    home = config_home().expanduser().absolute()
    if home.is_symlink():
        raise RecoveryError("媒体缓存目录不能是符号链接")
    root = home.resolve()
    path = root / "cache" / "media" / identifier
    if any(parent.is_symlink() for parent in (path, path.parent, path.parent.parent)):
        raise RecoveryError("媒体缓存不能经过符号链接")
    if any((parent / ".git").exists() for parent in path.parents):
        raise RecoveryError("媒体恢复缓存不得位于 Git 项目内；请调整 NOXGATE_HOME")
    return path


def _identity(state: dict, kind: str) -> dict:
    return {"profile": state["profile"], "provider_hash": state["provider_hash"],
            "fingerprint": state["fingerprint"], "kind": kind}


def _write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".media-", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _private_read(path: Path) -> bytes:
    if path.is_symlink():
        raise RecoveryError("媒体缓存不能是符号链接")
    if not path.exists():
        raise RecoveryMissing("媒体恢复缓存已丢失")
    if not path.is_file():
        raise RecoveryError("媒体缓存不是普通文件")
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RecoveryError("媒体缓存文件必须使用 0600 权限")
    with path.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise RecoveryError("媒体恢复缓存过大")
    return raw


def stage(state: dict, kind: str, parts: list[dict]) -> str:
    if not parts or len(parts) > 100:
        raise RecoveryError("没有可缓存的媒体结果")
    identifier = uuid.uuid4().hex
    directory = _directory(identifier)
    manifest = {"version": 1, **_identity(state, kind), "items": []}
    urls = {}
    store = None
    try:
        for index, part in enumerate(parts):
            digest = None
            if part.get("b64_json"):
                try:
                    raw = base64.b64decode(part["b64_json"], validate=True)
                except (ValueError, TypeError):
                    raise RecoveryError("媒体数据不是有效 Base64") from None
                if not raw or len(raw) > MAX_BYTES:
                    raise RecoveryError("媒体缓存为空或过大")
                _write(directory / f"{index}.bin", raw)
                digest = hashlib.sha256(raw).hexdigest()
            elif isinstance(part.get("url"), str):
                urls[str(index)] = part["url"]
            else:
                raise RecoveryError("媒体结果缺少内容")
            manifest["items"].append({"sha256": digest})
        if urls:
            from noxgate_auth import get_profile
            _, profile = get_profile(state["profile"])
            store = CredentialStore(profile.get("store", "file"), profile.get("env_file"))
            # URLs can carry signed access secrets: use the selected secure store.
            store.set_reference(identifier, json.dumps(urls))
            manifest["reference_store"] = profile["store"]
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_json(directory / "manifest.json", manifest)
        return identifier
    except BaseException:
        if store:
            try:
                store.delete_reference(identifier)
            except Exception:
                pass
        shutil.rmtree(directory, ignore_errors=True)
        raise


def recover(state: dict, kind: str, fetch) -> dict:
    identifier = state.get("recovery_id", "")
    directory = _directory(identifier)
    try:
        manifest = json.loads(_private_read(directory / "manifest.json"))
    except FileNotFoundError:
        raise RecoveryMissing("媒体缓存已丢失；异步任务可通过原任务 ID 重查") from None
    except (OSError, ValueError):
        raise RecoveryError("媒体缓存损坏或不可读") from None
    if not isinstance(manifest, dict):
        raise RecoveryError("媒体缓存清单无效")
    if manifest.get("version") != 1 or any(manifest.get(k) != v for k, v in _identity(state, kind).items()):
        raise RecoveryError("媒体缓存与当前 profile、网关或任务不一致")
    items = manifest.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise RecoveryError("媒体缓存清单无效")
    store = CredentialStore(manifest["reference_store"]) if manifest.get("reference_store") else None
    urls = None
    parts = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise RecoveryError("媒体缓存条目无效")
        if item.get("sha256"):
            raw = _private_read(directory / f"{index}.bin")
            if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise RecoveryError("媒体缓存校验失败")
        else:
            if urls is None:
                try:
                    urls = json.loads(store.get_reference(identifier) or "{}") if store else {}
                except (ValueError, TypeError):
                    raise RecoveryError("媒体下载恢复引用无效") from None
            if not isinstance(urls, dict):
                raise RecoveryError("媒体下载恢复引用无效")
            address = urls.get(str(index))
            if not isinstance(address, str):
                raise RecoveryError("媒体下载恢复引用已丢失")
            raw = fetch(address)
            if not raw or len(raw) > MAX_BYTES:
                raise RecoveryError("媒体缓存为空或过大")
            _write(directory / f"{index}.bin", raw)
            item["sha256"] = hashlib.sha256(raw).hexdigest()
            atomic_json(directory / "manifest.json", manifest)
        parts.append({"b64_json": base64.b64encode(raw).decode()})
    if store:
        store.delete_reference(identifier)
        manifest.pop("reference_store", None)
        atomic_json(directory / "manifest.json", manifest)
    if kind == "video":
        return {"status": "completed", "result": parts[0]}
    return {"status": "completed", "data": parts}


def discard(identifier: str) -> None:
    """Remove an unreferenced journal if a state update failed."""
    directory = _directory(identifier)
    try:
        manifest = json.loads(_private_read(directory / "manifest.json"))
        if manifest.get("reference_store"):
            CredentialStore(manifest["reference_store"]).delete_reference(identifier)
    except (OSError, ValueError, RecoveryMissing):
        pass
    shutil.rmtree(directory, ignore_errors=True)
