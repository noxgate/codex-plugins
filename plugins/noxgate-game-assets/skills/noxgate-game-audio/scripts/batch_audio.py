#!/usr/bin/env python3
"""Bounded, resumable game-audio production through the current Codex provider."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT / "lib" if (PLUGIN_ROOT / "lib/noxgate_runtime.py").is_file() else PLUGIN_ROOT.parent / "shared"))
from noxgate_runtime import MediaError, generate, resume, validate_payload, save_state

AUDIO_FORMATS = {"mp3", "wav", "ogg", "opus", "flac", "aac", "m4a", "pcm"}


def load(path: Path) -> list[dict]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assets = value.get("assets") if isinstance(value, dict) else None
    if not isinstance(assets, list) or not assets:
        raise MediaError("manifest 需要非空 assets 数组")
    seen, outputs = set(), set()
    for item in assets:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not re.fullmatch(r"[a-z][a-z0-9_-]{1,79}", item["id"]) or item["id"] in seen:
            raise MediaError("每个资产需要唯一的安全 id（小写字母、数字、下划线或连字符）")
        payload = {k: v for k, v in item.items() if k not in {"id", "output", "kind", "acceptance"}}
        validate_payload(payload, "audio")
        fmt = item.get("format", item.get("response_format", "mp3"))
        if fmt not in AUDIO_FORMATS:
            raise MediaError(f"{item['id']}: 不支持的输出格式")
        output = item.get("output", item["id"] + "." + fmt)
        if not isinstance(output, str) or Path(output).name != output or "\\" in output or output.startswith('.') or Path(output).suffix.lstrip('.') not in AUDIO_FORMATS:
            raise MediaError(f"{item['id']}: output 必须是安全的音频文件名")
        # Multi-result tasks use numbered names; asset ID remains the unique prefix.
        if not Path(output).stem.startswith(item["id"]) or output in outputs:
            raise MediaError("output 应以资产 id 开头且不能与其他资产重复")
        seen.add(item["id"])
        outputs.add(output)
        item["output"] = output
    return assets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "run", "resume"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--max-new-tasks", type=int)
    parser.add_argument("--only", action="append")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--poll-interval", type=float, default=2)
    args = parser.parse_args()
    try:
        if args.timeout <= 0 or args.poll_interval <= 0:
            parser.error("timeout 和 poll-interval 必须大于零")
        if args.command == "resume":
            if not args.state:
                parser.error("resume 需要 --state")
            print("\n".join(resume(args.state, output=args.output, timeout=args.timeout, interval=args.poll_interval)))
            return 0
        if not args.manifest:
            parser.error("需要 --manifest")
        assets = load(args.manifest)
        if args.only:
            unknown = set(args.only) - {item["id"] for item in assets}
            if unknown:
                raise MediaError("找不到指定的资产 id")
            assets = [item for item in assets if item["id"] in args.only]
        if args.command == "plan":
            print(f"assets={len(assets)}")
            for item in assets:
                print(f"{item['id']}: {item['model']}")
            return 0
        if not args.output_dir or args.max_new_tasks is None or args.max_new_tasks < 0:
            parser.error("run 需要 --output-dir 和 --max-new-tasks >= 0")
        output_dir = args.output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        submitted = failures = skipped = completed = 0
        records = []
        for item in assets:
            output = output_dir / item["output"]
            state = output_dir / "_state" / (item["id"] + ".task.json")
            existed_before = state.exists()
            if not existed_before and submitted >= args.max_new_tasks:
                skipped += 1
                records.append({"id": item["id"], "status": "not_submitted"})
                continue
            payload = {k: v for k, v in item.items() if k not in {"id", "output", "kind", "acceptance"}}
            try:
                paths = generate("audio", payload, output, state, timeout=args.timeout, interval=args.poll_interval)
                completed += 1
                records.append({"id": item["id"], "status": "pending_audio_review", "files": paths})
                print(f"{item['id']}: pending_audio_review")
            except (MediaError, OSError, ValueError) as exc:
                failures += 1
                records.append({"id": item["id"], "status": "incomplete"})
                print(f"{item['id']}: error: {exc}", file=sys.stderr)
            finally:
                # Count attempted submissions even when HTTP or polling fails.
                if not existed_before and state.exists():
                    submitted += 1
        save_state(output_dir / "audio-review.json", {"assets": records})
        print(f"new_tasks={submitted} completed={completed} failures={failures} pending_submission={skipped}")
        return 1 if failures or skipped else 0
    except (MediaError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
