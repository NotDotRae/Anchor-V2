#!/bin/sh
set -eu
cd -- "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1

# macOS may provide an older system Python alongside a newer Homebrew Python.
for anchor_python in python3 python3.15 python3.14 python3.13 python3.12; do
    if command -v "$anchor_python" >/dev/null 2>&1 &&
        "$anchor_python" -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then
        exec "$anchor_python" -u -B -m anchorbot.launcher run "$@"
    fi
done

echo "Anchor requires Python 3.12 or newer. On macOS, install it with: brew install python" >&2
exit 1
