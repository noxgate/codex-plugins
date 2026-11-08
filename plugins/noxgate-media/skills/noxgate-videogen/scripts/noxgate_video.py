#!/usr/bin/env python3
from pathlib import Path
import sys
PLUGIN_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PLUGIN_ROOT / "lib" if (PLUGIN_ROOT / "lib/noxgate_runtime.py").is_file() else PLUGIN_ROOT.parent / "shared"))
from noxgate_runtime import cli
raise SystemExit(cli("video"))
