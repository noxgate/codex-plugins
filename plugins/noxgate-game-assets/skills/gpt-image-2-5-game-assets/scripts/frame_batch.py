#!/usr/bin/env python3
"""Validate and assemble deterministic sprite sheets from generated frame PNGs."""

from __future__ import annotations

import argparse
import json
import math
import re
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError


class FrameError(Exception):
    pass


def parse_size(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", value)
    if not match:
        raise FrameError(f"invalid size {value!r}")
    return int(match[1]), int(match[2])


def power_of_two(value: int) -> int:
    return 1 << (value - 1).bit_length()


def load_manifest(path: Path) -> list[dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FrameError("cannot read frame manifest") from exc
    animations = document.get("animations") if isinstance(document, dict) else None
    if not isinstance(animations, list) or not animations:
        raise FrameError("manifest requires a nonempty animations array")
    result = []
    seen = set()
    for index, item in enumerate(animations, 1):
        if not isinstance(item, dict):
            raise FrameError(f"animation {index} must be an object")
        required = {"id", "frames_dir", "frame_count", "fps", "frame_size", "anchor"}
        if not required.issubset(item):
            raise FrameError(f"animation {index} is missing {sorted(required - set(item))}")
        animation_id = item["id"]
        if not isinstance(animation_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,79}", animation_id) or animation_id in seen:
            raise FrameError(f"animation {index} has invalid or duplicate id")
        seen.add(animation_id)
        width, height = parse_size(item["frame_size"])
        if type(item["frame_count"]) is not int or item["frame_count"] < 1:
            raise FrameError(f"{animation_id}: frame_count must be positive")
        if type(item["fps"]) is not int or item["fps"] < 1 or item["fps"] > 120:
            raise FrameError(f"{animation_id}: fps must be 1..120")
        anchor = item["anchor"]
        if not isinstance(anchor, list) or len(anchor) != 2 or any(not isinstance(v, (int, float)) or not 0 <= v <= 1 for v in anchor):
            raise FrameError(f"{animation_id}: anchor must contain two normalized values")
        padding = item.get("padding", 2)
        if type(padding) is not int or padding < 0 or padding > 64:
            raise FrameError(f"{animation_id}: invalid padding")
        result.append({**item, "frame_size_tuple": (width, height), "padding": padding})
    return result


def frame_paths(item: dict[str, Any]) -> list[Path]:
    directory = Path(item["frames_dir"])
    paths = sorted(directory.glob("*.png"), key=lambda p: [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", p.name)])
    if len(paths) != item["frame_count"]:
        raise FrameError(f"{item['id']}: expected {item['frame_count']} PNG frames, found {len(paths)}")
    return paths


def read_frames(item: dict[str, Any]) -> list[Image.Image]:
    expected = item["frame_size_tuple"]
    frames = []
    for path in frame_paths(item):
        try:
            image = Image.open(path)
            image.load()
        except (OSError, UnidentifiedImageError) as exc:
            raise FrameError(f"{item['id']}: invalid frame {path.name}") from exc
        if image.size != expected:
            raise FrameError(f"{item['id']}: {path.name} has size {image.size}, expected {expected}")
        if image.mode not in {"RGBA", "LA", "P"}:
            raise FrameError(f"{item['id']}: {path.name} must preserve transparency (RGBA/LA/P)")
        frames.append(image.convert("RGBA"))
    return frames


def atlas_layout(count: int, width: int, height: int, padding: int) -> tuple[int, int, int, int]:
    columns = max(1, math.ceil(math.sqrt(count)))
    rows = math.ceil(count / columns)
    cell_w, cell_h = width + padding * 2, height + padding * 2
    atlas_w, atlas_h = power_of_two(columns * cell_w), power_of_two(rows * cell_h)
    return columns, rows, atlas_w, atlas_h


def build(item: dict[str, Any], output_dir: Path) -> None:
    frames = read_frames(item)
    width, height = item["frame_size_tuple"]
    columns, _rows, atlas_w, atlas_h = atlas_layout(len(frames), width, height, item["padding"])
    atlas = Image.new("RGBA", (atlas_w, atlas_h), (0, 0, 0, 0))
    frame_table = []
    cell_w, cell_h = width + item["padding"] * 2, height + item["padding"] * 2
    for index, frame in enumerate(frames):
        x = (index % columns) * cell_w + item["padding"]
        y = (index // columns) * cell_h + item["padding"]
        atlas.alpha_composite(frame, (x, y))
        frame_table.append({
            "frame": index,
            "x": x,
            "y": y,
            "w": width,
            "h": height,
            "duration_ms": round(1000 / item["fps"], 3),
            "anchor": item["anchor"],
        })
    output_dir.mkdir(parents=True, exist_ok=True)
    png_path = output_dir / f"{item['id']}.png"
    json_path = output_dir / f"{item['id']}.json"
    with tempfile.NamedTemporaryFile(dir=output_dir, suffix=".png", delete=False) as tmp:
        temporary_png = Path(tmp.name)
    try:
        atlas.save(temporary_png, "PNG")
        temporary_png.replace(png_path)
    finally:
        temporary_png.unlink(missing_ok=True)
    document = {
        "id": item["id"], "fps": item["fps"], "loop": bool(item.get("loop", True)),
        "atlas": {"width": atlas_w, "height": atlas_h}, "frames": frame_table,
    }
    json_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("plan", "validate", "assemble"))
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    try:
        animations = load_manifest(args.manifest)
        if args.command in {"validate", "assemble"}:
            for item in animations:
                read_frames(item)
        if args.command == "plan":
            total = sum(item["frame_count"] for item in animations)
            print(f"animations={len(animations)} frames={total}")
        elif args.command == "validate":
            print(f"Frame manifests valid: {len(animations)} animations")
        else:
            if args.output_dir is None:
                raise FrameError("assemble requires --output-dir")
            for item in animations:
                build(item, args.output_dir)
            print(f"Assembled sprite sheets: {len(animations)} animations")
        return 0
    except FrameError as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
