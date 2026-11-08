#!/usr/bin/env python3
"""Generate/edit images with the active Codex provider; supports resumable tasks."""
from __future__ import annotations
import argparse
import hashlib
import json
import mimetypes
import sys
from pathlib import Path
PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT / "lib" if (PLUGIN_ROOT / "lib/noxgate_runtime.py").is_file() else PLUGIN_ROOT.parent / "shared"))
from noxgate_runtime import MediaError, generate, resume
ImageRequestError = MediaError

def multipart(fields: dict[str, str], images: list[Path], mask: Path | None) -> tuple[bytes, str]:
    boundary = "codex-" + hashlib.sha256(json.dumps(fields, sort_keys=True).encode() + b"".join(path.read_bytes() for path in images) + (mask.read_bytes() if mask else b"")).hexdigest()[:32]
    chunks: list[bytes] = []
    marker = f"--{boundary}".encode()
    for name, value in fields.items():
        chunks.extend((marker, b'Content-Disposition: form-data; name="' + name.encode() + b'"', b"", value.encode()))
    for path in images:
        if not path.is_file():
            raise ImageRequestError(f"image file not found: {path}")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        chunks.extend((
            marker,
            f'Content-Disposition: form-data; name="image[]"; filename="{path.name}"'.encode(),
            f"Content-Type: {mime}".encode(),
            b"",
            path.read_bytes(),
        ))
    if mask is not None:
        if not mask.is_file():
            raise ImageRequestError(f"mask file not found: {mask}")
        mime = mimetypes.guess_type(mask.name)[0] or "application/octet-stream"
        chunks.extend((
            marker,
            f'Content-Disposition: form-data; name="mask"; filename="{mask.name}"'.encode(),
            f"Content-Type: {mime}".encode(),
            b"",
            mask.read_bytes(),
        ))
    chunks.extend((f"--{boundary}--".encode(), b""))
    return b"\r\n".join(chunks), f"multipart/form-data; boundary={boundary}"



def submit(args):
    if args.command == "resume":
        paths = resume(args.state, output=args.output)
    else:
        fields = {"model": args.model, "prompt": args.prompt, "size": args.size,
                  "quality": args.quality, "output_format": args.output_format, "response_format": "b64_json"}
        if not args.prompt.strip():
            raise MediaError("提示词不能为空")
        output = args.output.expanduser().resolve()
        if args.command == "generate":
            fields.update(background=args.background, n=args.count, stream=False)
            paths = generate("image", fields, output, Path(str(output) + ".task.json"))
        else:
            fields["stream"] = "false"
            body, content_type = multipart(fields, args.image, args.mask)
            paths = generate("image", fields, output, Path(str(output) + ".task.json"),
                             body=body, content_type=content_type, endpoint="images/edits")
    print("\n".join(paths))
    return 0

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("generate", "edit"):
        command = subparsers.add_parser(name)
        command.add_argument("--prompt", required=True)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--model", default="gpt-image-2.5-flare")
        command.add_argument("--size", default="1024x1024")
        command.add_argument("--quality", choices=("low", "medium", "high", "auto"), default="medium")
        command.add_argument("--output-format", choices=("png", "jpeg", "webp"), default="png")
        if name == "generate":
            command.add_argument("--background", choices=("auto", "transparent", "opaque"), default="auto")
            command.add_argument("--count", type=int, choices=range(1, 5), default=1)
        else:
            command.add_argument("--image", type=Path, action="append", required=True)
            command.add_argument("--mask", type=Path)
    restore = subparsers.add_parser("resume")
    restore.add_argument("--state", type=Path, required=True)
    restore.add_argument("--output", type=Path)
    return parser



def main():
    try:
        return submit(build_parser().parse_args())
    except (MediaError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
