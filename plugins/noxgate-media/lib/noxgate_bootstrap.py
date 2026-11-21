"""Prepare locked dependencies and run a tool using the plugin's Python."""
import hashlib
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from noxgate_credentials import CredentialError, _private_dir, config_home


TOOLS = {
    "image": "noxgate-imagegen/scripts/noxgate_image.py",
    "video": "noxgate-videogen/scripts/noxgate_video.py",
    "audio": "noxgate-audiogen/scripts/noxgate_audio.py",
    "asset-batch": "gpt-image-2-5-game-assets/scripts/asset_batch.py",
    "frame-batch": "gpt-image-2-5-game-assets/scripts/frame_batch.py",
    "sheet-batch": "gpt-image-2-5-game-assets/scripts/sheet_batch.py",
    "audio-batch": "noxgate-game-audio/scripts/batch_audio.py",
}


def wheel_specs(root, platform, game_assets):
    groups = {"core", "game"} if game_assets else {"core"}
    for line in (root / "lib/runtime.lock").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            target, group, filename, digest, url = line.split("\t")
            if target in {platform, "all"} and group in groups:
                yield filename, digest, url


def prepare_wheels(root, platform, game_assets):
    specs = list(wheel_specs(root, platform, game_assets))
    fingerprint = hashlib.sha256(
        "\n".join(digest for _, digest, _ in specs).encode("ascii")
    ).hexdigest()
    cache = _private_dir(config_home() / "runtime")
    target = cache / ("packages-" + fingerprint)
    if target.is_dir():
        return target

    with tempfile.TemporaryDirectory(prefix=".packages-", dir=cache) as staging:
        stage = Path(staging)
        wheels = []
        for filename, digest, url in specs:
            if Path(filename).name != filename:
                raise ValueError("Invalid wheel filename in runtime.lock")
            source = root / "runtime" / filename
            if not source.is_file():
                if os.environ.get("NOXGATE_OFFLINE") == "1":
                    raise ValueError(f"Missing bundled wheel {filename}")
                source = stage / filename
                curl = (str(Path(os.environ["SystemRoot"]) / "System32/curl.exe")
                        if os.name == "nt" else "curl")
                subprocess.run([curl, "--fail", "--silent", "--show-error", "--location",
                                "--proto", "=https", "--proto-redir", "=https", "--retry", "2",
                                "--connect-timeout", "15", "--max-time", "180",
                                "--output", str(source), url], check=True)
            with source.open("rb") as handle:
                if hashlib.file_digest(handle, "sha256").hexdigest() != digest:
                    raise ValueError(f"Wheel SHA-256 mismatch: {filename}")
            wheels.append(str(source))

        packages = stage / "installed"
        subprocess.run([sys.executable, "-I", "-m", "pip", "--isolated", "--disable-pip-version-check",
                        "install", "--no-index", "--no-deps", "--no-compile",
                        "--target", str(packages), *wheels], check=True, stdout=subprocess.DEVNULL,
                       env={**os.environ, "PIP_CONFIG_FILE": os.devnull})
        try:
            packages.rename(target)
        except OSError:
            if not target.is_dir():
                raise
    return target


def main():
    root = Path(__file__).resolve().parents[1]
    platform, *args = sys.argv[1:]
    tools = {name: root / "skills" / path for name, path in TOOLS.items()
             if (root / "skills" / path).is_file()}
    if not args or args[0] in {"-h", "--help"}:
        print("noxgate auth | models | tasks | " + " | ".join(tools))
        print("Use noxgate <command> --help for arguments.")
        return
    command, *rest = args
    if command in {"auth", "models", "tasks"}:
        entry = root / "bin/noxgate"
        forwarded = args
    elif command in tools:
        entry = tools[command]
        forwarded = rest
    else:
        raise ValueError(f"Unknown or unavailable NoxGate command: {command}")

    packages = prepare_wheels(root, platform, "asset-batch" in tools)
    sys.path.insert(0, str(packages))
    sys.path.insert(0, str(entry.parent))
    import ssl

    if not ssl.get_default_verify_paths().cafile:
        os.environ.setdefault("SSL_CERT_FILE", str(packages / "certifi/cacert.pem"))
    os.environ["NOXGATE_NO_REEXEC"] = "1"
    sys.argv = [str(entry), *forwarded]
    runpy.run_path(str(entry), run_name="__main__")


if __name__ == "__main__":
    try:
        main()
    except (CredentialError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"NoxGate: {exc}") from None
