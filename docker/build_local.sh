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

set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d .git ]; then
    echo "This script has to run inside a git clone: the image is tagged with the commit hash." >&2
    exit 1
fi

echo "==> Writing the version files"
python3 dev_tools/update_frontend_version_hash.py

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
