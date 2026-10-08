# Sourced by the hooks, not a hook itself (hooks are separate processes). Shared pieces of the guarded release update
# that scripts/update.py used to do in one piece: instance checks, release-image Compose calls, health, timer state.
ROOT=${STRTA_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}
cd "$ROOT"
INSTANCES="live paper"
TIMERS="pinchy-paper-session.timer pinchy-live-backup.timer pinchy-paper-backup.timer pinchy-live-session.timer pinchy-weekly-learn.timer"
JOBS="pinchy-paper-session.service pinchy-live-backup.service pinchy-paper-backup.service pinchy-live-session.service pinchy-weekly-learn.service"
LIVE_TIMERS="pinchy-live-session.timer pinchy-weekly-learn.timer"
TASK=${TASK:-/home/jay/.local/bin/task}
[ -x "$TASK" ] || TASK=$(command -v task || echo task)
IMAGE_FILE=$ROOT/.release-image
STATE=$ROOT/.update-state

fail() { echo "$*" >&2; exit 1; }

# compose <instance> <image> <args...>: the release Compose override pointed at <image>.
compose() {
  _inst=$1; _img=$2; shift 2
  env DEPLOY_IMAGE="$_img" COMPOSE_FILE="$ROOT/docker-compose.yml:$ROOT/compose.release.yml" \
    docker compose -p "pinchy-$_inst" --env-file "instances/$_inst.env" "$@"
}
# task_ <image> <args...>: a Taskfile target under the same release override.
task_() {
  _img=$1; shift
  env DEPLOY_IMAGE="$_img" COMPOSE_FILE="$ROOT/docker-compose.yml:$ROOT/compose.release.yml" "$TASK" "$@"
}
unit_active() { systemctl is-active --quiet "$1"; }
unit_enabled() { systemctl is-enabled --quiet "$1"; }
halt_markers() { { [ -e HALT ] && echo HALT; ls instances/*.HALT 2>/dev/null; } | sort; }
halt_markers_at() { git ls-tree -r --name-only "$1" | grep -E '^(HALT|instances/[^/]+\.HALT)$' | sort || true; }
valid_image() { printf %s "$1" | grep -Eq '^algo:[0-9a-f]{40}$'; }

write_image() {
  printf '%s\n' "$1" > "$IMAGE_FILE.partial"
  chmod 600 "$IMAGE_FILE.partial"
  mv "$IMAGE_FILE.partial" "$IMAGE_FILE"
}

migrate_and_start() {
  for i in $INSTANCES; do task_ "$1" db:migrate "INSTANCE=$i" >/dev/null || fail "db:migrate failed for $i"; done
  for i in $INSTANCES; do
    compose "$i" "$1" up -d --no-build --pull never db trading dashboard >/dev/null || fail "starting $i failed"
  done
}

# healthy <image> <seconds>: db, trading and dashboard answer and run exactly <image>, for both instances.
healthy() {
  _id=$(docker image inspect --format '{{.Id}}' "$1") || return 1
  for i in $INSTANCES; do
    _end=$(( $(date +%s) + $2 ))
    while :; do
      if compose "$i" "$1" exec -T db pg_isready -U algo -d trading >/dev/null 2>&1 &&
         compose "$i" "$1" exec -T trading true >/dev/null 2>&1 &&
         compose "$i" "$1" exec -T dashboard python -c \
           "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/health', timeout=3)" >/dev/null 2>&1; then
        _ok=1
        for s in trading dashboard; do
          c=$(compose "$i" "$1" ps -q "$s" 2>/dev/null)
          [ -n "$c" ] && [ "$(docker inspect --format '{{.Image}}' "$c")" = "$_id" ] || _ok=0
        done
        [ "$_ok" = 1 ] && break
      fi
      [ "$(date +%s)" -lt "$_end" ] || { echo "$i: database, trading or dashboard failed health checks (or not running $1)" >&2; return 1; }
      sleep 3
    done
  done
}

start_timers() { for t in $1; do sudo -n systemctl start "$t" || return 1; done; }
