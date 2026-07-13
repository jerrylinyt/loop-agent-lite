#!/usr/bin/env python3
"""Immutable DR-2 integration-only invariant; never edits the target repo."""
from pathlib import Path


root = Path.cwd()
integration_worktree = (root / ".git").is_dir()
if (integration_worktree and (root / "docs/dr2-a.txt").exists() and
        (root / "docs/dr2-b.txt").exists() and not (root / "docs/dr2-compat.txt").exists()):
    raise SystemExit("DR-2 integration invariant: create docs/dr2-compat.txt explaining the conflict resolution")
