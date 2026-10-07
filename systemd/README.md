# Pinchy systemd jobs

These units mirror the installed jaywin cron schedule observed on 2026-10-06.
They are source files for the eventual bee deployment; adding them to the
repository does not install or enable a timer.

| Timer | Local time (America/Denver) | Cutover state |
| --- | --- | --- |
| `pinchy-paper-session.timer` | Weekdays 12:30 | Enable only after jaywin's paper cron is disabled and bee's paper smoke passes |
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
The updater must write a checked, prebuilt tag such as `algo:<40-hex-commit>`
to the gitignored `.release-image` file. The job wrapper rejects a missing or
invalid tag and sets the release Compose override for Taskfile/Compose commands.

Routine updates use `scripts/update.py <full-commit>` with
`EXPECTED_IMAGE_ID=sha256:<image-id>` supplied by the verified release workflow.
The updater requires a prior `.release-image`, checks that no job is running,
pauses only active timers, makes and validates both backups, applies migrations
with only the database up, starts the prebuilt image, checks the database and
dashboard, then restores the prior timer state. If the new code fails, it
restores the previous code and image; it cannot undo schema migrations.
`systemd/pinchy-sudoers` lists only the timer start/stop commands it needs.
Validate that file with `visudo -cf` before installing it. Initial installation
and database restore are separate from this routine updater.

Installation should copy the unit files into `/etc/systemd/system/` and run
`systemctl daemon-reload`, leaving all timers disabled until the data and
schedule cutover. Enabling a timer before disabling the corresponding jaywin
cron entry can produce overlapping work against the same Alpaca account.
