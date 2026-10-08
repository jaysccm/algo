#!/bin/sh
# Drives the real script/update and script/ext hooks in a scratch clone with stubbed docker, systemctl, sudo and task.
# Cases: success keeps timer state, a failed new stack rolls back before the timers resume, an image mismatch and a stopped
# stack both refuse before anything is stopped, and a backup failure resumes the timers without switching the release.
set -eu
repo=$(cd "$(dirname "$0")/../.." && pwd)
S=$(mktemp -d); trap 'rm -rf "$S"' EXIT
mkdir "$S/bin"
cat > "$S/bin/docker" <<'STUB'
#!/bin/sh
echo "docker $*" >> "$LOG"
case "$*" in
 "image inspect"*|"inspect --format {{.Image}}"*) echo "$FAKE_ID" ;;
 *"ps --status running --services"*) if [ -n "${STOPPED:-}" ]; then echo db; else printf 'db\ntrading\ndashboard\n'; fi ;;
 *"ps -q"*) echo cid ;;
 *"exec -T dashboard"*) [ "$(cat .release-image)" = "$(cat "$S/bad" 2>/dev/null)" ] && exit 1; exit 0 ;;
 *"exec -T db pg_restore"*) cat >/dev/null ;;
esac
STUB
cat > "$S/bin/systemctl" <<'STUB'
#!/bin/sh
echo "systemctl $*" >> "$LOG"
case "$1" in
 is-active) [ "$3" = pinchy-paper-session.timer ] && exit 0; exit 3 ;;
 is-enabled) case "$3" in pinchy-live-session.timer|pinchy-weekly-learn.timer) echo disabled; exit 1 ;; *) echo enabled ;; esac ;;
esac
STUB
printf '#!/bin/sh\nshift\nexec "$@"\n' > "$S/bin/sudo"
cat > "$S/bin/task" <<'STUB'
#!/bin/sh
echo "task $*" >> "$LOG"
[ -n "${BACKUP_FAILS:-}" ] && [ "$1" = db:backup ] && exit 1
if [ "$1" = db:backup ]; then mkdir -p backups; echo data > "backups/${2#INSTANCE=}-$(date +%s%N).dump"; fi
STUB
chmod +x "$S/bin/"*
export S LOG="$S/log" PATH="$S/bin:$PATH" TASK="$S/bin/task" FAKE_ID="sha256:$(printf 'a%.0s' $(seq 64))"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t

git clone -q "$repo" "$S/w" && cd "$S/w"
# Test the working tree's hooks, not whatever was committed.
cp -r "$repo/script/." script/
git add -A && git commit -q --allow-empty -m hooks
OLD=$(git rev-parse HEAD)
git commit -q --allow-empty -m new && NEW=$(git rev-parse HEAD)
git commit -q --allow-empty -m new2 && NEW2=$(git rev-parse HEAD)
for i in live paper; do echo "INSTANCE=$i" > "instances/$i.env"; chmod 600 "instances/$i.env"; done
touch .env.host instances/live.HALT; chmod 600 .env.host
reset() { git checkout -q --detach "$OLD"; echo "algo:$OLD" > .release-image; rm -f "$S/bad" .update-state; : > "$LOG"; }
update() { EXPECTED_IMAGE_ID=$FAKE_ID script/update --ref "$1" --image "algo:$1" --wait 3 >"$S/out" 2>&1; }
fails=0
check() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; sed 's/^/     /' "$S/out" | tail -8; fails=$((fails + 1)); fi; }
head7() { git rev-parse HEAD; }

reset; update "$NEW"
check "success switches code and image" '[ "$(head7)" = "$NEW" ] && [ "$(cat .release-image)" = "algo:$NEW" ]'
check "success pauses then restores only the active timer" '[ "$(grep -c "systemctl stop" "$LOG")" = 1 ] && grep -q "systemctl start pinchy-paper-session.timer" "$LOG" && ! grep -q "start pinchy-live" "$LOG"'
check "success backs up both instances and clears state" 'grep -q "db:backup INSTANCE=live" "$LOG" && grep -q "db:backup INSTANCE=paper" "$LOG" && [ ! -e .update-state ]'

reset; echo "algo:$NEW2" > "$S/bad"; ! update "$NEW2"
check "failed new stack rolls back code, image and timers" '[ "$(head7)" = "$OLD" ] && [ "$(cat .release-image)" = "algo:$OLD" ] && grep -q "systemctl start pinchy-paper-session.timer" "$LOG" && [ ! -e .update-state ]'

reset; ! EXPECTED_IMAGE_ID=sha256:$(printf 'b%.0s' $(seq 64)) script/update --ref "$NEW" --image "algo:$NEW" >"$S/out" 2>&1
check "image mismatch refuses before stopping anything" '[ "$(head7)" = "$OLD" ] && ! grep -q "systemctl stop" "$LOG"'

reset; ! STOPPED=1 update "$NEW"
check "a stopped stack is refused, not started" '[ "$(head7)" = "$OLD" ] && ! grep -q "systemctl stop\|up -d" "$LOG"'

reset; ! BACKUP_FAILS=1 update "$NEW"
check "backup failure resumes timers without switching the release" '[ "$(head7)" = "$OLD" ] && [ "$(cat .release-image)" = "algo:$OLD" ] && grep -q "systemctl start pinchy-paper-session.timer" "$LOG" && ! grep -q "db:migrate" "$LOG"'

[ "$fails" = 0 ] || exit 1
