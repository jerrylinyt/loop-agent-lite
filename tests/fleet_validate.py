#!/usr/bin/env python3
"""Fail only in the integration worktree until the repair agent adds compat.txt."""
import os
from pathlib import Path


root = Path.cwd()
if os.environ.get("FLEET_VALIDATE_FORCE_FAIL") == "1":
    raise SystemExit("forced baseline validation failure")
is_integration_worktree = (root / ".git").is_dir()
needs_compat = (root / "alpha.txt").exists() and (root / "beta.txt").exists()
if is_integration_worktree and needs_compat and not (root / "compat.txt").exists():
    raise SystemExit("integration-only validation failure: compat.txt missing")
