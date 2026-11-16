#!/usr/bin/env python3
"""Install the standalone CLI and only the explicitly requested dependencies."""
import argparse
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

source = Path(__file__).resolve().parents[1]
library = source / "lib" if (source / "lib/noxgate_credentials.py").is_file() else source
sys.path.insert(0, str(library))
from noxgate_credentials import _private_dir, config_home

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--home", type=Path, default=config_home())
parser.add_argument("--bin-dir", type=Path, default=Path("~/.local/bin").expanduser())
parser.add_argument("--game-assets", action="store_true", help="Also install Pillow and NumPy for game asset processing")
parser.add_argument("--system-keyring", action="store_true", help="Install keyring support for an explicitly selected --store system")
parser.add_argument("--skip-dependencies", action="store_true", help="Do not create a virtual environment or install optional dependencies")
args = parser.parse_args()
if args.skip_dependencies and (args.system_keyring or args.game_assets):
    parser.error("--skip-dependencies cannot be combined with --system-keyring or --game-assets")
home = _private_dir(Path(os.path.abspath(args.home.expanduser())))
target = home / "cli"
_private_dir(target)
_private_dir(target / "lib")
for name in ("noxgate_auth.py", "noxgate_credentials.py", "noxgate_runtime.py", "noxgate_recovery.py"):
    library = source / "lib" if (source / "lib").is_dir() else source
    shutil.copy2(library / name, target / "lib" / name)
_private_dir(target / "bin")
shutil.copy2(source / "bin/noxgate", target / "bin/noxgate")
if not args.skip_dependencies and (args.system_keyring or args.game_assets):
    env = home / "venv"
    venv.EnvBuilder(with_pip=True).create(env)
    python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    dependencies = (["keyring>=25,<26"] if args.system_keyring else []) + (["Pillow>=10", "numpy>=1.26"] if args.game_assets else [])
    subprocess.run([str(python), "-m", "pip", "install", *dependencies], check=True)
args.bin_dir.mkdir(parents=True, exist_ok=True)
if os.name == "nt":
    launcher = args.bin_dir / "noxgate.cmd"
    if launcher.exists() and str(target / "bin/noxgate") not in launcher.read_text():
        raise SystemExit("Refusing to overwrite an existing unrelated command")
    installed_python = home / "venv/Scripts/python.exe"
    python = installed_python if installed_python.is_file() else Path(sys.executable)
    launcher.write_text('@"' + str(python) + '" "' + str(target / "bin/noxgate") + '" %*\n')
else:
    launcher = args.bin_dir / "noxgate"
    if launcher.is_symlink() and launcher.resolve() != (target / "bin/noxgate").resolve():
        raise SystemExit("Refusing to replace an unrelated command symlink")
    if launcher.exists() and not launcher.is_symlink():
        raise SystemExit("Refusing to overwrite an existing non-symlink command")
    launcher.unlink(missing_ok=True)
    launcher.symlink_to(target / "bin/noxgate")
    (target / "bin/noxgate").chmod(0o755)
print("NoxGate CLI installed: " + str(launcher))
print("Add this directory to PATH: " + str(args.bin_dir))
