#!/usr/bin/env bash
# Run an existing Taskfile or Compose command against the verified release image.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
image_file="$root/.release-image"
if [ ! -f "$image_file" ]; then
    echo "Missing $image_file; no release image is selected" >&2
    exit 2
fi

image="$(cat "$image_file")"
if [[ ! "$image" =~ ^algo:[0-9a-f]{40}$ ]]; then
    echo "Invalid release image tag in $image_file" >&2
    exit 2
fi

export DEPLOY_IMAGE="$image"
export COMPOSE_FILE="$root/docker-compose.yml:$root/compose.release.yml"
cd "$root"
exec "$@"
