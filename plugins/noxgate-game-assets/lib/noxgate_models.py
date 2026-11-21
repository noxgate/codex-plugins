"""Read model permissions and public rates before a generation is submitted."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from decimal import Decimal, InvalidOperation

from noxgate_runtime import MediaError, load_provider, request_json


CONFIRMED_IMAGE_EDIT_MODELS = {"gpt-image-2.5-flare", "gpt-image-2.5-sunburst"}
OPERATIONS = {
    "image": ("generate", "edit"),
    "game-image": ("generate", "edit"),
    "audio": ("generate", "music", "sound", "tts"),
    "video": ("generate", "text-to-video", "image-to-video", "reference-to-video"),
}
DURATION_METERS = {"duration_second", "audio_second", "video_second", "video_input_second", "video_output_second"}
METER_ALIASES = {"cached_token": "cache_token", "characters": "character", "pages": "page",
                 "requests": "request", "request_count": "request"}
UNITS = {
    "image_count": "张", "image_input_count": "张输入图", "request": "次请求", "character": "字符",
    "page": "页", "duration_second": "秒", "audio_second": "秒音频", "video_second": "秒视频",
    "video_input_second": "秒输入视频", "video_output_second": "秒输出视频",
}


def operation_support(model_id: str, capability: str, kind: str, operation: str) -> str | None:
    """Return confirmed, unverified, or None for an incompatible operation."""
    normalized_id = model_id.lower().translate(str.maketrans("‐‑–", "---"))
    if kind in {"image", "game-image"}:
        if capability != "image_generation":
            return None
        edit_variant = normalized_id.startswith("mj-") and normalized_id.endswith(
            ("-edit", "-remix", "-remove-bg", "-retexture", "-upload-paint", "-variation")
        )
        if operation == "edit":
            return "confirmed" if model_id in CONFIRMED_IMAGE_EDIT_MODELS else "unverified"
        if edit_variant or normalized_id.endswith("-layerize"):
            return "unverified" if kind == "game-image" else None
        return "confirmed"
    if kind == "audio":
        if capability in {"tts", "speech_synthesis"}:
            return "confirmed" if operation == "tts" else None
        if capability != "audio_generation" or operation == "tts" or normalized_id == "suno-persona":
            return None
        if operation == "generate":
            return "confirmed"
        if normalized_id.startswith("suno-"):
            return "confirmed" if operation == "music" else None
        if normalized_id.startswith("doubao-seed-audio-"):
            return "confirmed" if operation == "sound" else None
        return "unverified"
    if capability not in {"video_generation", "video_generation_t2v", "video_generation_i2v"}:
        return None
    variant = {"video_generation_t2v": "text-to-video", "video_generation_i2v": "image-to-video"}.get(capability)
    if variant is None:
        for candidate, suffix in (("text-to-video", "-text-to-video"), ("image-to-video", "-image-to-video"),
                                  ("reference-to-video", "-reference-video")):
            if normalized_id.endswith((suffix, "-" + candidate)):
                variant = candidate
                break
    if operation == "generate" or operation == variant:
        return "confirmed"
    return "unverified" if variant is None else None


def number(value: object) -> Decimal | None:
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def price_lines(config: object) -> list[dict]:
    if not isinstance(config, dict) or not isinstance(config.get("rules"), list):
        return []
    lines = []
    for rule in config["rules"]:
        if not isinstance(rule, dict) or not isinstance(rule.get("meter"), str):
            continue
        meter = rule["meter"].strip().lower()
        meter = METER_ALIASES.get(meter, meter)
        if meter.endswith("_seconds") and meter[:-1] in DURATION_METERS:
            meter = meter[:-1]
        price = number(rule.get("price"))
        if not meter or price is None:
            continue
        if meter.endswith("_token"):
            amount, unit = price, "百万 token"
        elif meter in DURATION_METERS and config.get("billing_unit") == "minute":
            amount, unit = price * 60 / 1_000_000, "分钟"
        else:
            amount, unit = price / 1_000_000, UNITS.get(meter, meter)
        line = {"amount": format(amount.normalize(), "f"), "unit": unit, "meter": meter}
        if rule.get("match"):
            line["conditions"] = rule["match"]
        if rule.get("included_quantity"):
            line["included_quantity"] = rule["included_quantity"]
        lines.append(line)
    return lines


def authorized_models(base: str, token: str, kind: str, operation: str) -> tuple[list[dict], bool]:
    response = request_json(base + "/models", token)
    if not isinstance(response.get("data"), list):
        raise MediaError("网关未返回有效的模型权限列表")
    authorized = {item["id"]: item for item in response["data"]
                  if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]}
    catalog_base = base.removesuffix("/v1") + "/api/public"
    try:
        public = request_json(catalog_base + "/models").get("data")
    except MediaError:
        public = None
    catalog = {item["model_id"]: item for item in public
               if isinstance(item, dict) and isinstance(item.get("model_id"), str)} if isinstance(public, list) else {}
    models = []
    for model_id, entry in sorted(authorized.items()):
        metadata = catalog.get(model_id, {})
        capability = entry.get("capability_type") or metadata.get("capability_type")
        support = operation_support(model_id, capability, kind, operation) if capability else "unverified"
        if support is not None:
            models.append({"id": model_id, "name": metadata.get("name") or model_id,
                           "capability": capability, "operation_support": support,
                           "price_config": metadata.get("price_config")})
    return models, isinstance(public, list)


def listing(kind: str, operation: str, *, count: int | None = None, duration: Decimal | None = None) -> dict:
    if operation not in OPERATIONS[kind]:
        raise MediaError(f"{kind} 不支持模型用途 {operation}")
    if count is not None and count < 1:
        raise MediaError("数量必须大于零")
    if duration is not None and (not duration.is_finite() or duration <= 0):
        raise MediaError("时长必须大于零")
    base, token = load_provider(capability="image" if kind == "game-image" else kind)
    models, catalog_available = authorized_models(base, token, kind, operation)
    catalog_base = base.removesuffix("/v1") + "/api/public"
    try:
        currency = request_json(catalog_base + "/platform-config").get("data", {}).get("payment_currency")
    except (AttributeError, MediaError):
        currency = None
    currency = currency.upper() if isinstance(currency, str) and re.fullmatch(r"[A-Za-z]{3}", currency) else None

    for model in models:
        prices = price_lines(model.pop("price_config"))
        model["pricing_status"] = "available" if prices and currency else "unavailable"
        model["rates"] = prices
        if currency and model["operation_support"] == "confirmed" and len(prices) == 1:
            price = prices[0]
            if "conditions" not in price and "included_quantity" not in price:
                quantity = None
                if price["meter"] in {"image_count", "request"}:
                    quantity = count
                elif duration is not None and price["meter"] in {"duration_second", "audio_second", "video_second", "video_output_second"}:
                    quantity = duration * (count or 1)
                    if price["unit"] == "分钟":
                        quantity /= 60
                if quantity is not None:
                    model["estimated_base_cost"] = format((Decimal(price["amount"]) * quantity).normalize(), "f")
    return {"kind": kind, "operation": operation, "currency": currency, "price_source": "public_base_rate",
            "catalog_available": catalog_available, "count": count,
            "duration_seconds_per_request": str(duration) if duration is not None else None, "models": models}


def show_models(kind: str, operation: str, *, count: int | None = None, duration: Decimal | None = None) -> None:
    print(json.dumps(listing(kind, operation, count=count, duration=duration), ensure_ascii=False, indent=2))


def show_media_request_models(kind: str, payload: dict) -> None:
    operation = "generate"
    if kind == "audio" and payload.get("input"):
        operation = "tts"
    if kind == "video":
        if any(payload.get(key) for key in ("media", "video_url", "audio_url")):
            operation = "reference-to-video"
        elif any(payload.get(key) for key in ("image_url", "image_urls")):
            operation = "image-to-video"
        else:
            operation = "text-to-video"
    duration = number(payload.get("duration"))
    show_models(kind, operation, count=1, duration=duration or None)


def cli() -> int:
    parser = argparse.ArgumentParser(prog="noxgate models")
    parser.add_argument("--kind", required=True, choices=OPERATIONS)
    parser.add_argument("--profile")
    parser.add_argument("--operation", choices=sorted({operation for operations in OPERATIONS.values() for operation in operations}))
    parser.add_argument("--count", type=int, help="number of images or requests for a base-cost estimate")
    parser.add_argument("--duration", help="duration in seconds per request for a base-cost estimate")
    args = parser.parse_args(sys.argv[2:])
    if args.profile:
        os.environ["NOXGATE_PROFILE"] = args.profile
    try:
        duration = number(args.duration)
        if args.duration is not None and not duration:
            raise MediaError("时长必须大于零")
        show_models(args.kind, args.operation or "generate", count=args.count, duration=duration)
        return 0
    except (MediaError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
