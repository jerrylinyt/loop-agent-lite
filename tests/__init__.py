"""loop-agent-lite test package with deterministic cross-platform text I/O."""

import locale
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path


if os.name == "nt":
    # The production format is UTF-8. Linux already defaults to UTF-8, while
    # traditional Windows installations may make bare fixture reads use cp950.
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    try:
        locale.setlocale(locale.LC_CTYPE, ".UTF-8")
    except locale.Error:
        pass
    for _stream in (sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _reconfigure:
            _reconfigure(encoding="utf-8", errors="backslashreplace")

    # Creating symlinks requires Developer Mode or elevation on Windows.
    # Security tests still run on hosts that allow it; restricted hosts report
    # a precise skip rather than treating OS policy as a product regression.
    _native_symlink_to = Path.symlink_to

    def _symlink_to_or_skip(self, target, target_is_directory=False):
        try:
            return _native_symlink_to(self, target, target_is_directory=target_is_directory)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                raise unittest.SkipTest(
                    "Windows 未啟用 symbolic-link 權限；reparse-point 防線另有平台測試") from exc
            raise

    Path.symlink_to = _symlink_to_or_skip

    _native_read_text = Path.read_text
    _native_write_text = Path.write_text

    def _read_text_utf8(self, encoding=None, errors=None):
        for attempt in range(6):
            try:
                return _native_read_text(self, encoding=encoding or "utf-8", errors=errors)
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.02)

    def _write_text_utf8(self, data, encoding=None, errors=None, newline=None):
        return _native_write_text(
            self, data, encoding=encoding or "utf-8", errors=errors,
            newline="\n" if newline is None else newline)

    Path.read_text = _read_text_utf8
    Path.write_text = _write_text_utf8

    _NativePopen = subprocess.Popen

    class _Utf8Popen(_NativePopen):
        def __init__(self, *args, **kwargs):
            if (kwargs.get("text") or kwargs.get("universal_newlines")) and not kwargs.get("encoding"):
                kwargs["encoding"] = "utf-8"
                kwargs.setdefault("errors", "replace")
            super().__init__(*args, **kwargs)

    subprocess.Popen = _Utf8Popen
