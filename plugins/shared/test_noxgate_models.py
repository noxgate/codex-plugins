import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

with patch.dict(os.environ, {"NOXGATE_NO_REEXEC": "1"}):
    import noxgate_models as models
    import noxgate_runtime as runtime


class ModelSelectionTests(unittest.TestCase):
    def test_prices_are_limited_to_current_key_and_the_requested_use(self):
        accessible = {"data": [{"id": name} for name in ("suno-v6-beta", "doubao-seed-audio-1-0", "private-model")]}
        catalog = {"data": [
            {"model_id": "suno-v6-beta", "capability_type": "audio_generation",
             "price_config": {"rules": [{"meter": "request", "price": 200000}]}},
            {"model_id": "doubao-seed-audio-1-0", "capability_type": "audio_generation"},
            {"model_id": "unavailable-music", "capability_type": "audio_generation"},
        ]}
        with patch.object(models, "load_provider", return_value=("https://fixture.invalid/v1", "ngp_fixture")), \
                patch.object(models, "request_json", autospec=True, side_effect=[accessible, catalog, {"data": {"payment_currency": "usd"}}]) as request:
            result = models.listing("audio", "music", count=3)
        self.assertEqual(request.call_args_list[0].args, ("https://fixture.invalid/v1/models", "ngp_fixture"))
        self.assertTrue(all(len(call.args) == 1 for call in request.call_args_list[1:]))
        self.assertEqual(result["currency"], "USD")
        by_id = {model["id"]: model for model in result["models"]}
        self.assertEqual(set(by_id), {"private-model", "suno-v6-beta"})
        self.assertEqual(by_id["private-model"]["operation_support"], "unverified")
        self.assertEqual(by_id["private-model"]["rates"], [])
        self.assertEqual(by_id["suno-v6-beta"]["rates"][0]["amount"], "0.2")
        self.assertEqual(by_id["suno-v6-beta"]["estimated_base_cost"], "0.6")

    def test_duration_rate_per_minute_and_estimated_base_cost(self):
        entry = {"model_id": "wan3.0-text-to-video", "capability_type": "video_generation",
                 "price_config": {"billing_unit": "minute", "rules": [{"meter": "video_seconds", "price": 100000}]}}
        with patch.object(models, "load_provider", return_value=("https://fixture.invalid/v1", "ngp_fixture")), \
                patch.object(models, "request_json", side_effect=[
                    {"data": [{"id": entry["model_id"]}]}, {"data": [entry]}, {"data": {"payment_currency": "cny"}}
                ]):
            result = models.listing("video", "text-to-video", count=2, duration=Decimal(15))
        model = result["models"][0]
        self.assertEqual(model["rates"], [{"amount": "6", "unit": "分钟", "meter": "video_second"}])
        self.assertEqual(model["estimated_base_cost"], "3")

    def test_catalog_failure_preserves_authorized_model_ids_without_quoting_a_price(self):
        with patch.object(models, "load_provider", return_value=("https://fixture.invalid/v1", "ngp_fixture")), \
                patch.object(models, "request_json", side_effect=[
                    {"data": [{"id": "gpt-image-2.5-flare"}, {"id": "another-image"}]},
                    runtime.MediaError("catalog unavailable"), runtime.MediaError("currency unavailable"),
                ]):
            result = models.listing("game-image", "generate", count=2)
        self.assertFalse(result["catalog_available"])
        self.assertIsNone(result["currency"])
        self.assertEqual([model["id"] for model in result["models"]], ["another-image", "gpt-image-2.5-flare"])
        for model in result["models"]:
            self.assertEqual(model["operation_support"], "unverified")
            self.assertEqual(model["rates"], [])
            self.assertNotIn("estimated_base_cost", model)

    def test_game_image_catalog_uses_key_permissions_instead_of_model_family(self):
        allowed = {"data": [{"id": name} for name in ("gpt-image-2.5-flare", "seedream", "suno-v6-beta")]}
        public = {"data": [
            {"model_id": "gpt-image-2.5-flare", "capability_type": "image_generation"},
            {"model_id": "seedream", "capability_type": "image_generation",
             "price_config": {"rules": [{"meter": "image_count", "price": 50000}]}},
            {"model_id": "suno-v6-beta", "capability_type": "audio_generation"},
            {"model_id": "z-image-turbo", "capability_type": "image_generation"},
        ]}
        with patch.object(models, "load_provider", return_value=("https://fixture.invalid/v1", "ngp_fixture")), \
                patch.object(models, "request_json", side_effect=[allowed, public, {"data": {"payment_currency": "usd"}}]):
            result = models.listing("game-image", "generate", count=2)
        self.assertEqual([model["id"] for model in result["models"]], ["gpt-image-2.5-flare", "seedream"])
        self.assertEqual(result["models"][1]["estimated_base_cost"], "0.1")

    def test_image_picker_keeps_latest_authorized_versions_and_complete_catalog(self):
        image_ids = (
            "gpt-image-2.5-sunburst", "gpt-image-2.6-flare", "gpt-image-2.6-sunburst",
            "doubao-seedream-4.5", "doubao-seedream-5.0-pro", "gemini-3-pro-image",
            "gemini-3.1-flash-image-preview", "mj-v8.9", "mj-v8.10", "mj-v8.10-edit", "z-image-turbo",
        )
        allowed = {"data": [{"id": name} for name in (*image_ids, "suno-v6-beta")]}
        public = {"data": [
            {"model_id": name, "capability_type": "image_generation"}
            for name in (*image_ids, "gpt-image-2.7-flare", "doubao-seedream-6.0-pro")
        ] + [{"model_id": "suno-v6-beta", "capability_type": "audio_generation"}]}
        with patch.object(models, "load_provider", return_value=("https://fixture.invalid/v1", "ngp_fixture")), \
                patch.object(models, "request_json", side_effect=[allowed, public, {"data": {}}]):
            result = models.listing("game-image", "generate")
        self.assertEqual({model["id"] for model in result["models"]}, set(image_ids))
        self.assertEqual({family["family"]: family["model_ids"] for family in result["latest_by_family"]}, {
            "GPT Image": ["gpt-image-2.6-flare", "gpt-image-2.6-sunburst"],
            "Seedream": ["doubao-seedream-5.0-pro"],
            "Gemini Flash": ["gemini-3.1-flash-image-preview"],
            "Gemini Pro": ["gemini-3-pro-image"],
            "Midjourney": ["mj-v8.10"],
            "z-image-turbo": ["z-image-turbo"],
        })

    def test_image_edit_picker_prefers_confirmed_model_over_newer_unverified_model(self):
        entries = [{"id": name, "capability_type": "image_generation"}
                   for name in ("gpt-image-2.5-flare", "gpt-image-2.6-flare")]
        with patch.object(models, "load_provider", return_value=("https://fixture.invalid/v1", "ngp_fixture")), \
                patch.object(models, "request_json", side_effect=[{"data": entries}, {"data": []}, {"data": {}}]):
            result = models.listing("image", "edit")
        self.assertEqual([item["operation_support"] for item in result["models"]], ["confirmed", "unverified"])
        self.assertEqual(result["latest_by_family"], [{"family": "GPT Image", "model_ids": ["gpt-image-2.5-flare"]}])

    def test_generation_without_a_model_only_lists_choices(self):
        plugins = Path(__file__).resolve().parents[1]
        tools = {
            "image": plugins / "noxgate-media/skills/noxgate-imagegen/scripts/noxgate_image.py",
            "audio": plugins / "noxgate-media/skills/noxgate-audiogen/scripts/noxgate_audio.py",
            "video": plugins / "noxgate-media/skills/noxgate-videogen/scripts/noxgate_video.py",
            "asset": plugins / "noxgate-game-assets/skills/noxgate-image-assets/scripts/asset_batch.py",
            "batch": plugins / "noxgate-game-assets/skills/noxgate-game-audio/scripts/batch_audio.py",
        }
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            request = folder / "request.json"
            request.write_text(json.dumps({"prompt": "fixture"}))
            manifest = folder / "manifest.json"
            manifest.write_text(json.dumps({"assets": [{"id": "demo", "prompt": "fixture"}]}))
            output = folder / "output"
            for kind, script in tools.items():
                with self.subTest(kind=kind):
                    if kind == "image":
                        args = ["generate", "--prompt", "fixture", "--output", str(output)]
                    elif kind in {"asset", "batch"}:
                        args = ["run", "--manifest", str(manifest), "--output-dir", str(output), "--max-new-tasks", "1"]
                    else:
                        args = ["generate", "--request", str(request), "--output", str(output)]
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with patch.object(sys, "argv", [str(script), *args]), \
                            patch.object(runtime, "generate", side_effect=AssertionError("generation submitted")), \
                            patch.object(runtime, "load_provider", side_effect=AssertionError("generation reached")), \
                            patch.object(models, "listing", return_value={"models": [{"id": "fixture"}]}) as listing, \
                            contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                            self.assertRaises(SystemExit) as exit_status:
                        runpy.run_path(str(script), run_name="__main__")
                    self.assertEqual(exit_status.exception.code, 2)
                    listing.assert_called_once()
                    self.assertIn("fixture", stdout.getvalue())
                    self.assertIn("请选择", stderr.getvalue())
                    self.assertFalse(output.exists())
                    self.assertFalse((folder / "output.task.json").exists())
