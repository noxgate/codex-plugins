"""Secret storage only. Never print secrets or pass them as process arguments."""
from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path


class CredentialError(Exception):
    pass


def config_home() -> Path:
    override = os.environ.get("NOXGATE_HOME")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if not root:
            raise CredentialError("无法确定 Windows 用户配置目录")
        return Path(root) / "NoxGate"
    if os.sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "NoxGate"
    return Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "noxgate"


def _inside_git_tree(path: Path) -> bool:
    return any((parent / ".git").exists() for parent in (path, *path.parents))


def _has_symlink_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            # macOS exposes standard temporary/system directories through
            # root-level aliases. Follow those OS-owned aliases, but reject
            # links anywhere beneath the selected user path.
            if os.sys.platform == "darwin" and current in {Path("/var"), Path("/tmp"), Path("/etc")}:
                current = current.resolve()
            else:
                return True
    return False


def _windows_acl(path: Path, *, directory: bool) -> None:
    """Grant only the current user; native Windows access checks remain unverified."""
    try:
        who = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True)
        match = re.search(r"S-1-(?:\d+)(?:-\d+)+", who.stdout)
        if not match:
            raise CredentialError("无法确认当前 Windows 用户 SID")
        path_arg = str(path)
        subprocess.run(["icacls", path_arg, "/reset"], capture_output=True, check=True)
        subprocess.run(["icacls", path_arg, "/inheritance:r"], capture_output=True, check=True)
        rights = "(OI)(CI)F" if directory else "F"
        grants = [f"*{match.group(0)}:{rights}"]
        subprocess.run(["icacls", path_arg, "/grant:r", *grants], capture_output=True, check=True)
        subprocess.run(["icacls", path_arg, "/verify"], capture_output=True, check=True)
    except (OSError, subprocess.SubprocessError):
        raise CredentialError("无法设置并验证 Windows 私有 ACL；凭据未保存") from None


def _private_dir(path: Path) -> Path:
    if _has_symlink_component(path):
        raise CredentialError("NoxGate 配置目录不能是符号链接")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise CredentialError("NoxGate 配置目录不是安全的普通目录")
    if os.name == "nt":
        _windows_acl(path, directory=True)
    else:
        os.chmod(path, 0o700)
        if stat.S_IMODE(path.stat().st_mode) != 0o700:
            raise CredentialError("NoxGate 配置目录必须使用 0700 权限")
    return path


def _private_env_path(path: Path) -> Path:
    if not path.is_absolute() or path.name != ".env":
        raise CredentialError("portable-env 必须指定绝对路径且文件名为 .env")
    if _has_symlink_component(path):
        raise CredentialError(".env 或其插件目录不能是符号链接")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or _inside_git_tree(path.parent):
        raise CredentialError("便携凭据不得位于符号链接或 Git 项目目录")
    if os.name == "nt":
        _windows_acl(path.parent, directory=True)
        if path.exists():
            _windows_acl(path, directory=False)
    else:
        os.chmod(path.parent, 0o700)
    return path


def atomic_json(path: Path, value: dict) -> None:
    _private_dir(path.parent)
    if path.is_symlink():
        raise CredentialError("NoxGate 配置文件不能是符号链接")
    fd, name = tempfile.mkstemp(prefix=".noxgate-", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        else:
            os.close(fd)
            fd = -1
            _windows_acl(Path(name), directory=False)
            fd = os.open(name, os.O_WRONLY | os.O_TRUNC)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        if os.name == "nt":
            _windows_acl(path, directory=False)
    finally:
        if fd >= 0:
            os.close(fd)
        Path(name).unlink(missing_ok=True)


@contextlib.contextmanager
def storage_lock(name: str, *, timeout: float = 10):
    home = _private_dir(config_home())
    path = home / (name + ".lock")
    if path.is_symlink():
        raise CredentialError("NoxGate 存储锁不能是符号链接")
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            _windows_acl(path, directory=False)
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            deadline = time.monotonic() + timeout
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if time.monotonic() >= deadline:
                        raise CredentialError("等待另一个 NoxGate 进程释放存储锁超时") from None
                    time.sleep(0.2)
        else:
            os.fchmod(fd, 0o600)
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


class CredentialStore:
    def __init__(self, backend: str = "file", env_file: Path | str | None = None):
        self.backend = backend
        self.env_file = Path(env_file).expanduser() if env_file is not None else None
        self.keyring = None
        if backend == "system":
            try:
                import keyring
                selected = keyring.get_keyring()
                module = type(selected).__module__
                if module not in {"keyring.backends.macOS", "keyring.backends.Windows", "keyring.backends.SecretService"}:
                    raise CredentialError("没有受支持的系统安全存储；请显式选择 --store file")
                self.keyring = selected
            except ImportError:
                raise CredentialError("缺少系统安全存储依赖；请安装原生适配后重试，或显式选择 --store file") from None
            except CredentialError:
                raise
            except Exception:
                raise CredentialError("系统安全存储不可用；请启用并解锁系统钥匙串") from None
        elif backend == "portable-env":
            if self.env_file is None:
                raise CredentialError("portable-env 必须显式提供 --env-file 绝对路径")
            if not self.env_file.is_absolute():
                raise CredentialError("--env-file 必须是绝对路径")
        elif backend not in {"file", "env"}:
            raise CredentialError("不支持的凭据存储")

    @staticmethod
    def _portable_key(key: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise CredentialError("portable-env profile 凭据键无效")
        return "NOXGATE_PLUGIN_TOKEN_" + key

    def _file(self) -> Path:
        home = _private_dir(config_home())
        path = home / "credentials.json"
        if _inside_git_tree(home):
            raise CredentialError("凭据文件不得位于 Git 项目目录；请使用用户配置目录")
        if path.is_symlink():
            raise CredentialError("凭据文件不能是符号链接")
        return path

    def _read(self) -> dict:
        path = self._file()
        if not path.exists():
            return {}
        if not path.is_file() or (os.name != "nt" and stat.S_IMODE(path.stat().st_mode) != 0o600):
            raise CredentialError("凭据文件必须是权限为 0600 的普通文件")
        if os.name == "nt":
            _windows_acl(path, directory=False)
        return json.loads(path.read_text(encoding="utf-8"))

    def _env_values(self) -> tuple[Path, str]:
        if self.env_file is None:
            raise CredentialError("portable-env 缺少显式 .env 路径")
        path = _private_env_path(self.env_file)
        try:
            if path.exists() and (not path.is_file() or (os.name != "nt" and stat.S_IMODE(path.stat().st_mode) != 0o600)):
                raise CredentialError("portable-env 文件必须是权限为 0600 的普通文件")
            if os.name == "nt" and path.exists():
                _windows_acl(path, directory=False)
            return path, path.read_text(encoding="utf-8") if path.exists() else ""
        except UnicodeError:
            raise CredentialError("portable-env 文件必须是 UTF-8 文本") from None

    @staticmethod
    def _parse_env(text: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = re.fullmatch(r"(?:export\s+)?(NOXGATE_PLUGIN_TOKEN_[0-9a-f]{64})\s*=\s*(.*?)\s*", stripped)
            if not match:
                raise CredentialError("portable-env 仅允许 NoxGate 插件凭据键、注释和空行")
            key, value = match.groups()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if not value.startswith("ngp_") or any(c.isspace() or ord(c) < 32 for c in value):
                raise CredentialError("portable-env 插件凭据格式无效")
            if key in values:
                raise CredentialError("portable-env 中存在重复的 profile 凭据键")
            values[key] = value
        return values

    def get(self, key: str) -> str | None:
        try:
            if self.backend == "env":
                return os.environ.get("NOXGATE_PLUGIN_TOKEN")
            if self.backend == "portable-env":
                _, text = self._env_values()
                return self._parse_env(text).get(self._portable_key(key))
            if self.keyring:
                return self.keyring.get_password("net.noxgate.plugins", key)
            return self._read().get(key)
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("读取安全存储失败；请检查权限或解锁系统钥匙串") from None

    def set(self, key: str, secret: str) -> None:
        try:
            if not secret.startswith("ngp_") or any(c.isspace() or ord(c) < 32 for c in secret):
                raise CredentialError("拒绝保存格式无效的插件凭据")
            if self.backend == "env":
                raise CredentialError("环境变量存储只读；请由外部 Secret Manager 注入凭据")
            if self.backend == "portable-env":
                path, text = self._env_values()
                env_key = self._portable_key(key)
                values = self._parse_env(text)
                values[env_key] = secret
                lines = text.splitlines()
                lines = [line for line in lines if not re.fullmatch(r"\s*(?:export\s+)?" + re.escape(env_key) + r"\s*=.*", line)]
                lines.append(env_key + "=" + secret)
                self._write_env(path, "\n".join(lines).lstrip("\n") + "\n")
            elif self.keyring:
                self.keyring.set_password("net.noxgate.plugins", key, secret)
            else:
                with storage_lock("credentials"):
                    data = self._read()
                    data[key] = secret
                    atomic_json(self._file(), data)
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("保存安全凭据失败；不会降级为其他存储") from None

    def _write_env(self, path: Path, content: str) -> None:
        fd, name = tempfile.mkstemp(prefix=".noxgate-env-", dir=path.parent)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            else:
                os.close(fd)
                fd = -1
                _windows_acl(Path(name), directory=False)
                fd = os.open(name, os.O_WRONLY | os.O_TRUNC)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                fd = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
            if os.name != "nt":
                os.chmod(path, 0o600)
            else:
                _windows_acl(path, directory=False)
        finally:
            if fd >= 0:
                os.close(fd)
            Path(name).unlink(missing_ok=True)

    def delete(self, key: str) -> None:
        if self.backend == "env":
            raise CredentialError("请从外部 Secret Manager 删除 NOXGATE_PLUGIN_TOKEN")
        if self.backend == "portable-env":
            path, text = self._env_values()
            env_key = self._portable_key(key)
            self._parse_env(text)
            lines = [line for line in text.splitlines() if not re.fullmatch(r"\s*(?:export\s+)?" + re.escape(env_key) + r"\s*=.*", line)]
            self._write_env(path, "\n".join(lines) + ("\n" if lines else ""))
        elif self.keyring:
            if self.get(key):
                self.keyring.delete_password("net.noxgate.plugins", key)
        else:
            with storage_lock("credentials"):
                data = self._read()
                data.pop(key, None)
                atomic_json(self._file(), data)

    def get_reference(self, key: str) -> str | None:
        """Read only an opaque recovery URL reference, never a plugin credential."""
        try:
            if self.backend in {"env", "portable-env"}:
                raise CredentialError("所选凭据存储不能安全保存媒体恢复引用")
            if self.keyring:
                return self.keyring.get_password("net.noxgate.plugin-recovery", key)
            return self._read().get("recovery:" + key)
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("读取媒体恢复安全引用失败") from None

    def set_reference(self, key: str, value: str) -> None:
        try:
            if self.backend in {"env", "portable-env"}:
                raise CredentialError("所选凭据存储不能安全保存媒体恢复引用")
            if self.keyring:
                self.keyring.set_password("net.noxgate.plugin-recovery", key, value)
            else:
                with storage_lock("credentials"):
                    data = self._read()
                    data["recovery:" + key] = value
                    atomic_json(self._file(), data)
        except CredentialError:
            raise
        except Exception:
            raise CredentialError("保存媒体恢复安全引用失败；不会写入任务文件") from None

    def delete_reference(self, key: str) -> None:
        if self.backend in {"env", "portable-env"}:
            raise CredentialError("所选凭据存储没有可删除的媒体恢复引用")
        if self.keyring:
            if self.get_reference(key) is not None:
                self.keyring.delete_password("net.noxgate.plugin-recovery", key)
        else:
            with storage_lock("credentials"):
                data = self._read()
                data.pop("recovery:" + key, None)
                atomic_json(self._file(), data)
