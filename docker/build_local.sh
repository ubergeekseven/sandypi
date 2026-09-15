#!/usr/bin/env bash
# Builds and starts an image from this repository.
#
# The two version files the Dockerfile expects (git_shash.json and frontend/.env)
# are generated from the current commit and are not in git, so they have to be
# written before the build context is sent to the daemon. That is the only reason
# this script exists.
#
# Usage, from the main sandypi folder:
#   ./docker/build_local.sh            build and start
#   ./docker/build_local.sh --no-start only build the image
#
# On Windows, run it from Git Bash or WSL.

set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d .git ]; then
    echo "This script has to run inside a git clone: the image is tagged with the commit hash." >&2
    exit 1
fi

# "python3" does not exist on a standard Windows install, where the launcher is
# called "python"; "python" on an old linux box may still be python 2
PYTHON=""
for candidate in python3 python py; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)" 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "No python 3 interpreter found (looked for python3, python, py)." >&2
    exit 1
fi

echo "==> Writing the version files (using $PYTHON)"
"$PYTHON" dev_tools/update_frontend_version_hash.py

if [ "${1:-}" = "--no-start" ]; then
    echo "==> Building sandypi:local"
    docker build -f docker/Dockerfile -t sandypi:local .
    echo
    echo "Done. Start it with:"
    echo "  docker compose -f docker/docker-compose.local.yml up -d"
else
    echo "==> Building and starting"
    docker compose -f docker/docker-compose.local.yml up -d --build
    echo
    echo "Done. The interface is on http://localhost:5100"
    echo "Follow the logs with:"
    echo "  docker compose -f docker/docker-compose.local.yml logs -f"
fi
