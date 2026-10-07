#!/usr/bin/env python3
"""Read-only host status for Pinchy's live and paper instances."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTANCES = ("live", "paper")
TIMERS = {
    "live": ("pinchy-live-session.timer", "pinchy-weekly-learn.timer", "pinchy-live-backup.timer"),
    "paper": ("pinchy-paper-session.timer", "pinchy-paper-backup.timer"),
}


def run(*args: str) -> tuple[int, str]:
    try:
        result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=False)
    except OSError:
        return 127, "unavailable"
    return result.returncode, result.stdout.strip()


def timer_state(timer: str) -> str:
    _, enabled = run("systemctl", "is-enabled", timer)
    _, active = run("systemctl", "is-active", timer)
    return f"{enabled or 'unknown'}/{active or 'unknown'}"


def last_backup(instance: str) -> str:
    files = list((ROOT / "backups").glob(f"{instance}-*.dump"))
    if not files:
        return "none"
    latest = max(files, key=lambda path: path.stat().st_mtime)
    return latest.name


def last_session(instance: str, containers: str) -> str:
    db_name = f"pinchy-{instance}-db-1"
    if db_name not in containers:
        return "unavailable (database container absent)"
    rc, value = run(
        "docker",
        "exec",
        db_name,
        "psql",
        "-U",
        "algo",
        "-d",
        "trading",
        "-Atc",
        "SELECT session_date::text || ' ' || status FROM sessions ORDER BY session_date DESC, id DESC LIMIT 1",
    )
    return (value or "none") if rc == 0 else "unavailable (database not ready)"


def main() -> None:
    _, commit = run("git", "rev-parse", "--short=12", "HEAD")
    _, dirty = run("git", "status", "--porcelain", "--untracked-files=no")
    image_file = ROOT / ".release-image"
    image = image_file.read_text().strip() if image_file.exists() else "unset"
    if image != "unset" and not re.fullmatch(r"algo:[0-9a-f]{40}", image):
        image = "invalid"
    print(f"commit: {commit or 'unknown'}{' (dirty)' if dirty else ''}")
    print(f"release image: {image}")
    print(f"global HALT: {'present' if (ROOT / 'HALT').exists() else 'absent'}")
    for instance in INSTANCES:
        env = ROOT / "instances" / f"{instance}.env"
        halted = ROOT / "instances" / f"{instance}.HALT"
        _, containers = run(
            "docker", "ps", "-a", "--filter", f"label=com.docker.compose.project=pinchy-{instance}",
            "--format", "{{.Names}} {{.Status}}",
        )
        print(f"\n{instance}: env={'present' if env.exists() else 'missing'}, "
              f"HALT={'present' if halted.exists() else 'absent'}")
        print(f"  containers: {containers.replace(chr(10), '; ') or 'none'}")
        print(f"  last session: {last_session(instance, containers)}")
        print(f"  last backup: {last_backup(instance)}")
        for timer in TIMERS[instance]:
            print(f"  {timer}: {timer_state(timer)}")


if __name__ == "__main__":
    main()
