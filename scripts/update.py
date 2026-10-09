#!/usr/bin/env python3
"""Compatibility entry point: the guarded updater lives in script/ext/update (run by the standard script/update)."""

import os
import sys
from pathlib import Path

hook = Path(__file__).resolve().parent.parent / "script" / "ext" / "update"
os.execv(sys.executable, [sys.executable, str(hook), *sys.argv[1:]])
