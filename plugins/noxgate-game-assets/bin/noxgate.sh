#!/bin/sh
set -eu

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
case "$(uname -s):$(uname -m)" in
    Darwin:arm64) platform=macos-arm64 ;;
    Darwin:x86_64)
        if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" = 1 ]; then
            platform=macos-arm64
        else
            platform=macos-x64
        fi ;;
    Linux:aarch64|Linux:arm64) platform=linux-arm64 ;;
    Linux:x86_64) platform=linux-x64 ;;
    *) printf '%s\n' 'NoxGate: unsupported operating system or CPU' >&2; exit 1 ;;
esac
bundle_platform="$root/runtime/platform.txt"
if [ -f "$bundle_platform" ]; then
    IFS= read -r bundle < "$bundle_platform"
    if [ "$bundle" != "$platform" ]; then
        printf '%s\n' "NoxGate: this package is for $bundle, expected $platform" >&2
        exit 1
    fi
    export NOXGATE_OFFLINE=1
fi

case "$platform" in
    macos-*) default_home="$HOME/Library/Application Support/NoxGate" ;;
    linux-*) default_home="${XDG_CONFIG_HOME:-$HOME/.config}/noxgate" ;;
esac
config=${NOXGATE_HOME:-$default_home}
case "$config" in \~/*) config="$HOME/${config#\~/}" ;; esac
cache="$config/runtime"
umask 077
mkdir -p "$cache"
IFS="$(printf '\t')" read -r name sha url <<EOF
$(awk -F '\t' -v platform="$platform" '$1 == platform && $2 == "python" {print $3 "\t" $4 "\t" $5}' "$root/lib/runtime.lock")
EOF
[ -n "$url" ] || { printf '%s\n' 'NoxGate: Python is missing from runtime.lock' >&2; exit 1; }

target="$cache/$sha"
python="$target/bin/python3"
if [ ! -x "$python" ]; then
    stage=$(mktemp -d "$cache/.python.XXXXXXXX")
    trap 'rm -rf -- "$stage"' EXIT
    trap 'exit 1' HUP INT TERM
    archive="$root/runtime/$name"
    if [ ! -f "$archive" ]; then
        [ "${NOXGATE_OFFLINE:-0}" != 1 ] || { printf '%s\n' "NoxGate: missing bundled runtime $name" >&2; exit 1; }
        archive="$stage/$name"
        printf '%s\n' 'NoxGate: preparing Python for the first launch' >&2
        curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 180 --retry 2 --output "$archive" "$url"
    fi
    case "$platform" in
        macos-*) actual=$(shasum -a 256 < "$archive") ;;
        linux-*) actual=$(sha256sum < "$archive") ;;
    esac
    if [ "${actual%% *}" != "$sha" ]; then
        printf '%s\n' 'NoxGate: Python archive SHA-256 mismatch' >&2
        exit 1
    fi
    tar -xzf "$archive" -C "$stage"
    "$stage/python/bin/python3" -I -c '
import os, sys
from pathlib import Path
try:
    os.rename(sys.argv[1], sys.argv[2])
except OSError:
    if not Path(sys.argv[2], "bin/python3").is_file():
        raise
' "$stage/python" "$target"
    rm -rf -- "$stage"
    trap - EXIT HUP INT TERM
fi

exec "$python" -I "$root/lib/noxgate_bootstrap.py" "$platform" "$@"
