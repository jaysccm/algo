# Pinchy systemd jobs

These units mirror the jaywin cron schedule observed on 2026-10-06 and are
installed on pearl, the production host since 2026-10-26 (bee ran them from
2026-10-07 until then). Adding or changing them in the repository does not
install, change or enable a timer; that is a reviewed host step.

| Timer | Local time (America/Denver) | Cutover state |
| --- | --- | --- |
| `pinchy-paper-session.timer` | Weekdays 12:30 | Enabled on pearl; enable on a new host only after the previous host's timer is disabled |
| `pinchy-live-backup.timer` | Weekdays 20:00 | Enable after backup and restore rehearsal |
| `pinchy-paper-backup.timer` | Weekdays 20:05 | Enable after backup and restore rehearsal |
| `pinchy-live-session.timer` | Weekdays 13:00 | Keep disabled; `instances/live.HALT` stays present |
| `pinchy-weekly-learn.timer` | Sunday 05:00 | Keep disabled; `instances/live.HALT` stays present |

Every service calls `cron-wrap.sh` for HALT, heartbeat and failure reporting.
The two backup services use `--ignore-halt`, matching jaywin's cron. The
services expect the checkout at `/home/jay/services/algo`, go-task at
`/home/jay/.local/bin/task`, and instance files plus `.env.host` on that host.
Verify those paths and file permissions before installing. The source host's
`instances/live.HALT` must be present in the destination checkout.
On the production host, set `ALGO_DASHBOARD_PUBLISH=true` and the Cloudflare
Pages settings in the paper env, and `ALGO_DASHBOARD_PUBLISH=false` in the live
env. Keep exactly one scheduled publisher; the previous host's paper job and
timer must be disabled before the new host's timer is enabled.
The updater must write a checked, prebuilt tag such as `algo:<40-hex-commit>`
to the gitignored `.release-image` file. The job wrapper rejects a missing or
invalid tag and sets the release Compose override for Taskfile/Compose commands.

Routine updates run `script/update <full-commit>`, whose project hook is `script/ext/update`, with
`EXPECTED_IMAGE_ID=sha256:<image-id>` supplied by the verified release workflow.
The updater requires a prior `.release-image`, checks that no job is running,
starts a stopped database so it can be backed up (a deploy converges; HALT and the disabled live timers gate trading, not a stopped stack),
pauses only active timers, makes and validates both backups, applies migrations
with only the database up, starts the prebuilt image, checks the database and
dashboard, then restores the prior timer state. If the new code fails, it
restores the previous code and image; it cannot undo schema migrations.
`systemd/pinchy-sudoers` lists only the timer start/stop commands it needs.
Validate that file with `visudo -cf` before installing it. Initial installation
and database restore are separate from this routine updater.

Installation should copy the unit files into `/etc/systemd/system/` and run
`systemctl daemon-reload`, leaving all timers disabled until the data and
schedule cutover. Enabling a timer before disabling the corresponding timer or cron entry on the previous host can produce overlapping work against the same Alpaca account.

## Standard scripts (JAY-147)

The checkout also exposes the shared `script/` entry points (vendored from `jaysccm/actions` `strta/`, see `script/VENDORED`;
Pinchy's logic is in `script/ext/`). `script/deploy <full-commit>` runs `script/update`, which delegates to `script/ext/update` (release mode only: it needs
`DEPLOY_IMAGE=algo:<commit>` and `EXPECTED_IMAGE_ID`, which `host/deploy.sh` passes from the release workflow) and then
`script/status`, which prints `scripts/status.py` and fails if the live HALT sentinel is missing, a live timer is
enabled or active, a stack is not fully running, or the paper and backup timers are not enabled. `script/setup` only prepares
`.env.host` and `instances/*.env` from their templates; a first install stays the reviewed cutover procedure.

