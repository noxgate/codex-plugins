"""Exercise packaged tools without using a system Python or downloading dependencies."""
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from zipfile import ZipFile
import zlib


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


def write_png(path, background):
    def chunk(kind, content):
        return (struct.pack(">I", len(content)) + kind + content
                + struct.pack(">I", zlib.crc32(kind + content)))

    rows = []
    for y in range(32):
        pixels = [(0, 255, 0, 255) if 8 <= x < 24 and 8 <= y < 24 else background
                  for x in range(32)]
        rows.append(b"\x00" + bytes(channel for pixel in pixels for channel in pixel))
    data = chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 6, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(b"".join(rows)))
    data += chunk(b"IEND", b"")
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + data)


@contextlib.contextmanager
def saved_profile(config):
    scopes = ["image", "audio", "video", "game-assets"]
    requests = []

    class Gateway(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            if (self.path != "/api/integrations/credentials/current"
                    or self.headers.get("Authorization") != "Bearer ngp_shared_profile_fixture"):
                self.send_error(400)
                return
            response = {"success": True, "data": {"profile_name": "work", "id": 1, "scopes": scopes}}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode("utf-8"))

        def do_POST(self):
            requests.append(self.path)
            self.send_error(400)

    with ThreadingHTTPServer(("127.0.0.1", 0), Gateway) as server:
        base = f"http://127.0.0.1:{server.server_port}"
        gateway = base + "/v1"
        # Use a pre-0.3 profile with no requested_scopes to check upgrade compatibility.
        profile = {"gateway": gateway, "web": base, "agent": "codex", "credential_id": 1,
                   "expires_at": time.time() + 3600, "scopes": scopes, "store": "file"}
        key = hashlib.sha256(f"work\n{gateway}\n1".encode()).hexdigest()
        config.mkdir(mode=0o700)
        write_json(config / "profiles.json", {"active": "work", "profiles": {"work": profile}})
        write_json(config / "credentials.json", {key: "ngp_shared_profile_fixture"})
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield requests
        finally:
            server.shutdown()
            thread.join()


def main():
    system = {"darwin": "macos", "win32": "windows", "linux": "linux"}[sys.platform]
    cpu = {"arm64": "arm64", "aarch64": "arm64", "amd64": "x64", "x86_64": "x64"}[platform.machine().lower()]
    target = f"{system}-{cpu}"
    repo = Path(__file__).resolve().parents[1]
    dist = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else repo / "dist" / target
    manifest = json.loads((dist / "manifest.json").read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory(prefix="noxgate platform test ") as directory:
        root = Path(directory).resolve()
        tools = root / "system tools"
        tools.mkdir()
        if system != "windows":
            checksum = "shasum" if system == "macos" else "sha256sum"
            for name in ("dirname", "uname", "mkdir", "awk", "mktemp", "tar", "gzip", "rm", checksum):
                (tools / name).symlink_to(shutil.which(name))
            if system == "macos":
                (tools / "sysctl").symlink_to(shutil.which("sysctl"))

        config = root / "shared profile"
        env = {name: value for name, value in os.environ.items() if not name.startswith(("NOXGATE_", "PYTHON"))}
        env.update(PATH=str(tools), NOXGATE_HOME=str(config), NOXGATE_OFFLINE="1")
        frames = root / "frames"
        frames.mkdir()
        write_png(frames / "walk01.png", (0, 0, 0, 0))
        write_png(root / "sheet.png", (255, 0, 255, 255))
        frame_manifest = root / "frames.json"
        sheet_manifest = root / "sheets.json"
        write_json(frame_manifest, {"animations": [{"id": "walk", "frames_dir": str(frames),
            "frame_count": 1, "fps": 12, "frame_size": "32x32", "anchor": [0.5, 0.5]}]})
        write_json(sheet_manifest, {"version": "1.0", "sheets": [{
            "id": "sheet", "source": "sheet.png", "size": [32, 32], "key_color": "#FF00FF",
            "background_tolerance": 35, "foreground_threshold": 160, "margin_px": 2,
            "items": [{"asset_id": "icon", "rect": [0, 0, 1, 1], "target_size": [32, 32], "anchor": [0.5, 0.5]}],
        }]})

        with saved_profile(config) as requests:
            for plugin in manifest["plugins"]:
                install = root / plugin["id"]
                with ZipFile(dist / plugin["filename"]) as archive:
                    assert archive.read("runtime/platform.txt").decode().strip() == target
                    for entry in archive.infolist():
                        path = Path(archive.extract(entry, install))
                        if entry.external_attr >> 16 & 0o111:
                            path.chmod(0o755)
                bin_path = install / "bin" / ("noxgate.cmd" if system == "windows" else "noxgate.sh")

                def run(*args):
                    process = subprocess.run([str(bin_path), *map(str, args)], cwd=frames, env=env,
                                             capture_output=True, text=True, timeout=90)
                    if process.returncode:
                        raise RuntimeError(f"{plugin['id']} {args}: {process.stderr}{process.stdout}")
                    return process.stdout

                if plugin["id"] == "noxgate-game-assets":
                    for name in ("asset-batch", "audio-batch", "frame-batch", "sheet-batch"):
                        run(name, "--help")
                    atlas = root / "atlas"
                    split = root / "split"
                    run("frame-batch", "assemble", "--manifest", frame_manifest, "--output-dir", atlas)
                    run("sheet-batch", "split", "--manifest", sheet_manifest, "--output-dir", split)
                    assert (atlas / "walk.png").is_file()
                    assert (split / "icon.png").is_file()
                    run("auth", "ensure", "--capability", "audio")
                else:
                    for capability in ("image", "video", "audio"):
                        run(capability, "--help")
                        run("auth", "ensure", "--capability", capability)

            assert requests == ["/api/integrations/credentials/current"] * 4, requests
            assert len(list((config / "runtime").glob("packages-*"))) == 2
        print(f"{target}: both offline plugin bundles, asset processing, and shared legacy profile passed")


if __name__ == "__main__":
    main()
