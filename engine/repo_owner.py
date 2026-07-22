"""Durable common-Git-dir ownership fence for non-guardian mutators.

The ordinary Loop/Ralph and launcher paths do not run their children through
``RepoExecutor``.  An operating-system lock alone is therefore insufficient:
if their parent is killed, the lock disappears while a detached child can
still be writing the primary repository.  This module supplies the small,
shared durability boundary those callers need.

The boundary is intentionally policy-light and stdlib-only:

* one global primary lock is held for the whole owner lifetime;
* a nonterminal RepoExecutor lease is audited before an owner marker is
  claimed;
* active/recovering markers are never inferred dead or replaced;
* every marker update compares the complete previous marker before writing;
* recovery is a separate API and requires an exact ``True`` result from a
  caller-supplied fencing proof.

Callers remain responsible for their platform-specific spawn barrier and for
only recording a result after the process group/Job has really been reaped.
No argv is persisted; only its canonical SHA-256 hash crosses this boundary.
"""

from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import os
import platform
import re
import select
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping, Sequence

from engine import platform_compat as compat


MARKER_NAME = "loop-agent-lite.owner.json"
MARKER_LOCK_NAME = "loop-agent-lite.owner.lock"
GLOBAL_LOCK_NAME = "loop-agent-lite.run.lock"
EXECUTOR_SIDECAR_NAME = "loop-agent-lite.repo-executor"
EXECUTOR_LEASE_NAME = "operation-lease.json"
EXECUTOR_LOCK_NAME = "operation-fence.lock"

_HEX32_RE = re.compile(r"[0-9a-f]{32}")
_HASH64_RE = re.compile(r"[0-9a-f]{64}")
_EXECUTOR_OPERATIONS = frozenset({
    "PREFLIGHT", "INITIALIZE_RUN_REFS", "CREATE_WORKTREE", "GATE_MERGE",
    "REMOVE_WORKTREE", "SHUTDOWN",
})
_POSIX_GUARDIAN_KIND = "posix-subreaper-guardian-v2"
_WINDOWS_STRICT_JOB_KIND = "windows-job-no-breakaway-v2"
_ATOMIC_REPLACE_LOCK = threading.Lock()

# The controlled POSIX process is a trusted resident guardian, not the
# untrusted payload leader.  A Linux subreaper is the only stdlib-accessible
# primitive that keeps a double-fork/setsid descendant discoverable after its
# original parent exits.  The guardian therefore verifies subreaper mode before
# publishing readiness, remains the durable identity root, freezes and rescans
# every exact descendant before signalling it, and emits completion only after
# the complete adopted tree is unable to mutate the repository.
_POSIX_OWNER_GUARDIAN = r"""
import ctypes
import os
import select
import signal
import struct
import subprocess
import sys
import time

barrier_fd = int(sys.argv[1])
control_fd = int(sys.argv[2])
status_fd = int(sys.argv[3])
payload_argv = sys.argv[4:]
guardian_pid = os.getpid()
guardian_pgid = os.getpgrp()

def status(kind, returncode):
    try:
        return os.write(
            status_fd, kind + struct.pack('!i', int(returncode))) == 5
    except OSError:
        return False

def pause_forever():
    while True:
        signal.pause()

def finish(returncode):
    status(b'D', returncode)
    try:
        os.close(status_fd)
    except OSError:
        pass
    while True:
        try:
            ack = os.read(control_fd, 1)
        except OSError:
            ack = b''
        if ack == b'A':
            try:
                os.close(control_fd)
            except OSError:
                pass
            raise SystemExit(int(returncode))
        if ack == b'E':
            # The tree is proven empty, but the owner could not persist
            # child_reaped.  Exit quiescently and leave the versioned marker
            # fail-closed.
            raise SystemExit(125)
        if ack == b'X':
            # A cancellation byte can race the already-published D proof.
            # Ignore it here and continue waiting for the durable ACK.
            continue
        pause_forever()

def fail_completion(returncode=126):
    status(b'F', returncode)
    try:
        os.close(status_fd)
    except OSError:
        pass
    pause_forever()

def enable_subreaper():
    if not sys.platform.startswith('linux') or not os.path.isdir('/proc'):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = getattr(libc, 'prctl')
        prctl.argtypes = [
            ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.c_ulong, ctypes.c_ulong,
        ]
        prctl.restype = ctypes.c_int
        if prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            return False
        current = ctypes.c_int()
        if prctl(37, ctypes.addressof(current), 0, 0, 0) != 0:
            return False  # PR_GET_CHILD_SUBREAPER
        return current.value == 1
    except (AttributeError, OSError, TypeError, ValueError):
        return False

def process_stat(pid):
    try:
        with open('/proc/%d/stat' % int(pid), encoding='ascii') as stream:
            raw = stream.read()
        fields = raw[raw.rfind(')') + 2:].split()
        if len(fields) < 20:
            return None
        return {
            'pid': int(pid), 'state': fields[0], 'ppid': int(fields[1]),
            'pgid': int(fields[2]), 'token': fields[19],
        }
    except (OSError, IndexError, ValueError):
        return None

def process_table():
    try:
        names = os.listdir('/proc')
    except OSError:
        return None
    table = {}
    for name in names:
        if not name.isdigit():
            continue
        info = process_stat(int(name))
        if info is not None:
            table[info['pid']] = info
    return table

def contained_processes():
    table = process_table()
    if table is None or process_stat(guardian_pid) is None:
        return None
    children = {}
    for pid, info in table.items():
        children.setdefault(info['ppid'], []).append(pid)
    found = set()
    pending = [guardian_pid]
    while pending:
        parent = pending.pop()
        for child in children.get(parent, ()):
            if child != guardian_pid and child not in found:
                found.add(child)
                pending.append(child)
    # Also cover the narrow interval before a newly orphaned process is
    # visibly reparented to this subreaper.
    for pid, info in table.items():
        if pid != guardian_pid and info['pgid'] == guardian_pgid:
            found.add(pid)
    return {pid: table[pid] for pid in found if pid in table}

def exact_alive(identity, include_zombie=False):
    current = process_stat(identity['pid'])
    return bool(
        current is not None and current['token'] == identity['token']
        and (include_zombie or current['state'] != 'Z'))

def send(snapshot, sig):
    for identity in tuple(snapshot.values()):
        if not exact_alive(identity):
            continue
        try:
            os.kill(identity['pid'], sig)
        except (OSError, ProcessLookupError, PermissionError):
            pass

def snapshot_key(snapshot):
    return {(pid, info['token']) for pid, info in snapshot.items()}

def current_live(snapshot):
    return any(exact_alive(identity) for identity in snapshot.values())

def discover_and_stop(snapshot):
    for _ in range(16):
        current = contained_processes()
        if current is None:
            return False
        previous = snapshot_key(snapshot)
        snapshot.update(current)
        send(snapshot, signal.SIGSTOP)
        verify = contained_processes()
        if verify is None:
            return False
        snapshot.update(verify)
        if snapshot_key(snapshot) == previous:
            return True
    return False

def reap_children():
    while True:
        try:
            waited, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if waited == 0:
            return

def fence_payload():
    snapshot = {}
    if not discover_and_stop(snapshot):
        return False
    send(snapshot, signal.SIGCONT)
    send(snapshot, signal.SIGINT)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        current = contained_processes()
        if current is None:
            return False
        snapshot.update(current)
        reap_children()
        if not current_live(snapshot):
            return True
        time.sleep(0.02)
    if not discover_and_stop(snapshot):
        return False
    send(snapshot, signal.SIGKILL)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        current = contained_processes()
        if current is None:
            return False
        snapshot.update(current)
        send(current, signal.SIGKILL)
        reap_children()
        if not current_live(snapshot):
            return True
        time.sleep(0.02)
    return not current_live(snapshot)

if not enable_subreaper():
    status(b'F', 126)
    raise SystemExit(126)
if not status(b'G', 0):
    raise SystemExit(125)
try:
    ready = os.read(barrier_fd, 1)
except OSError:
    ready = b''
finally:
    try:
        os.close(barrier_fd)
    except OSError:
        pass
if ready != b'R' or not payload_argv:
    raise SystemExit(125)

try:
    payload = subprocess.Popen(payload_argv, close_fds=True)
except OSError as exc:
    try:
        sys.stderr.write('repo owner payload launch failed: ' + str(exc) + '\n')
        sys.stderr.flush()
    except OSError:
        pass
    finish(127)

parent_gone = False
cancelled = False
while True:
    returncode = payload.poll()
    if returncode is not None:
        break
    try:
        readable, _, _ = select.select([control_fd], [], [], 0.05)
    except (OSError, ValueError):
        readable = [control_fd]
    if readable:
        try:
            command = os.read(control_fd, 1)
        except OSError:
            command = b''
        if command in {b'', b'X'}:
            parent_gone = command == b''
            cancelled = command == b'X'
            if not fence_payload():
                fail_completion()
            try:
                payload.wait(timeout=3)
            except subprocess.TimeoutExpired:
                if not fence_payload():
                    fail_completion()
            returncode = 125
            break

if not fence_payload():
    fail_completion()
reap_children()
finish(125 if parent_gone or cancelled else int(returncode))
"""

# Compatibility alias for tests and diagnostics which compiled the previous
# barrier source.  Its semantics are now the resident guardian contract.
_POSIX_BARRIER_CODE = _POSIX_OWNER_GUARDIAN


class OwnerKind(str, Enum):
    """Closed set of non-guardian primary mutators."""

    LOOP = "loop"
    RALPH = "ralph"
    DASHBOARD_LAUNCHER = "dashboard-launcher"
    CLI_LAUNCHER = "cli-launcher"
    PARALLEL_LAUNCHER = "parallel-launcher"


class ChildKind(str, Enum):
    """Closed child purposes that may execute below an owner marker."""

    AGENT = "agent"
    VALIDATOR = "validator"
    TOOL = "tool"
    GIT = "git"
    LAUNCHER = "launcher"


class RepoOwnerError(RuntimeError):
    """Base class for owner-fence failures."""


class OwnerAuthorityError(RepoOwnerError):
    """Caller input is outside the closed owner-fence contract."""


class OwnerInvariantError(RepoOwnerError):
    """A durable file or repository identity cannot be trusted."""


class OwnerBusy(RepoOwnerError):
    """Another live or durable nonterminal owner remains authoritative."""


class OwnerRecoveryRequired(OwnerBusy):
    """The marker may only be changed through explicit fenced recovery."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z")


def _canonical_json_bytes(value) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise OwnerAuthorityError("value is not canonical JSON") from exc


def _json_clone(value):
    return json.loads(_canonical_json_bytes(value).decode("utf-8"))


def _reject_json_constant(value: str):
    raise ValueError(f"non-finite JSON number:{value}")


def _reject_duplicate_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key:{key}")
        value[key] = item
    return value


def _require_exact_dict(value, fields, label: str) -> dict:
    if not isinstance(value, dict) or set(value) != set(fields):
        actual = sorted(value) if isinstance(value, dict) else type(value).__name__
        raise OwnerInvariantError(
            f"{label} schema mismatch: expected {sorted(fields)}, got {actual}")
    return value


def _require_nonempty_string(value, label: str) -> str:
    if (not isinstance(value, str) or not value or "\x00" in value
            or len(value) > 4096):
        raise OwnerInvariantError(f"{label} must be a bounded non-empty string")
    return value


def _require_hex(value, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise OwnerInvariantError(f"{label} is not canonical lowercase hex")
    return value


def _require_positive_int(value, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise OwnerInvariantError(f"{label} must be a positive integer")
    return value


def _require_nonnegative_int(value, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise OwnerInvariantError(f"{label} must be a non-negative integer")
    return value


def _coerce_owner_kind(value) -> OwnerKind:
    try:
        return value if isinstance(value, OwnerKind) else OwnerKind(value)
    except (TypeError, ValueError) as exc:
        raise OwnerAuthorityError("owner_kind is outside the closed enum") from exc


def _coerce_child_kind(value) -> ChildKind:
    try:
        return value if isinstance(value, ChildKind) else ChildKind(value)
    except (TypeError, ValueError) as exc:
        raise OwnerAuthorityError("child_kind is outside the closed enum") from exc


def _reject_link_components(path: Path, *, allow_missing: bool = False) -> None:
    """Reject symlink/junction traversal without trusting ``resolve`` first."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if allow_missing:
                continue
            raise OwnerAuthorityError(f"path component does not exist:{current}")
        except OSError as exc:
            raise OwnerAuthorityError(f"cannot inspect path component:{current}") from exc
        if current.is_symlink() or compat.is_reparse_point(info):
            raise OwnerAuthorityError(f"path component is a link/reparse point:{current}")


def _canonical_existing_directory(path: Path, label: str) -> Path:
    try:
        lexical = Path(os.path.abspath(os.fspath(path)))
    except (OSError, TypeError, ValueError) as exc:
        raise OwnerAuthorityError(f"{label} is not an absolute path") from exc
    _reject_link_components(lexical)
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise OwnerAuthorityError(f"cannot canonicalize {label}") from exc
    if not resolved.is_dir():
        raise OwnerAuthorityError(f"{label} is not a directory")
    _reject_link_components(resolved)
    return resolved


def _canonical_artifact_path(path: Path, label: str) -> Path:
    try:
        lexical = Path(os.path.abspath(os.fspath(path)))
    except (OSError, TypeError, ValueError) as exc:
        raise OwnerAuthorityError(f"{label} is not an absolute path") from exc
    _reject_link_components(lexical, allow_missing=True)
    try:
        resolved = lexical.resolve(strict=False)
    except OSError as exc:
        raise OwnerAuthorityError(f"cannot canonicalize {label}") from exc
    if resolved != lexical:
        raise OwnerAuthorityError(f"{label} traverses a link/reparse point")
    return resolved


def _git_path(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        check=False, shell=False, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if result.returncode:
        raise OwnerAuthorityError(
            f"cannot resolve Git repository identity:{result.stderr.strip()}")
    value = result.stdout.strip()
    if not value or "\x00" in value:
        raise OwnerInvariantError("Git returned an invalid repository path")
    return value


def _resolve_repository(repo: Path) -> tuple[Path, Path]:
    lexical_repo = _canonical_existing_directory(Path(repo), "repository")
    common_raw = Path(_git_path(lexical_repo, "rev-parse", "--git-common-dir"))
    common_lexical = (common_raw if common_raw.is_absolute()
                      else lexical_repo / common_raw)
    common_dir = _canonical_existing_directory(common_lexical, "Git common-dir")

    # The first entry is Git's main worktree.  Using it as canonical_repo makes
    # every linked worktree converge on the same marker identity.
    listing = _git_path(lexical_repo, "worktree", "list", "--porcelain")
    first = next((line.removeprefix("worktree ") for line in listing.splitlines()
                  if line.startswith("worktree ")), None)
    if first is None:
        raise OwnerInvariantError("Git worktree registry has no main worktree")
    canonical_repo = _canonical_existing_directory(Path(first), "canonical repository")
    return canonical_repo, common_dir


def _open_regular(path: Path, flags: int, mode: int = 0o600) -> int:
    _reject_link_components(path.parent)
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, mode)
    except OSError as exc:
        raise OwnerInvariantError(f"cannot safely open:{path}") from exc
    try:
        handle_info = os.fstat(fd)
        path_info = path.lstat()
        if (not stat.S_ISREG(handle_info.st_mode)
                or not stat.S_ISREG(path_info.st_mode)
                or path.is_symlink()
                or compat.is_reparse_point(path_info)
                or (handle_info.st_dev, handle_info.st_ino)
                != (path_info.st_dev, path_info.st_ino)):
            raise OwnerInvariantError(f"path is not the opened regular file:{path}")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_regular_lock(path: Path):
    fd = _open_regular(path, os.O_RDWR | os.O_CREAT)
    return os.fdopen(fd, "r+b", closefd=True)


@contextmanager
def _short_lock(path: Path):
    stream = _open_regular_lock(path)
    try:
        compat.lock_file(stream, blocking=True)
    except OSError as exc:
        stream.close()
        raise OwnerInvariantError(f"cannot acquire short lock:{path.name}") from exc
    try:
        yield
    finally:
        compat.unlock_file(stream)
        stream.close()


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        # Windows does not expose fsync for ordinary directory handles.
        pass
    finally:
        os.close(fd)


def _atomic_json(path: Path, payload: dict) -> None:
    data = _canonical_json_bytes(payload)
    temporary_path = None
    fd = None
    try:
        _reject_link_components(path.parent)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary)
        stream = os.fdopen(fd, "wb", closefd=True)
        fd = None
        with stream:
            try:
                os.chmod(temporary_path, 0o600)
            except OSError:
                pass
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _reject_link_components(path.parent)
        if os.path.lexists(path):
            path_info = path.lstat()
            if (not stat.S_ISREG(path_info.st_mode) or path.is_symlink()
                    or compat.is_reparse_point(path_info)):
                raise OwnerInvariantError(
                    f"refusing to replace non-regular durable path:{path}")
        # Windows can transiently reject a replace while virus scanners or a
        # diagnostic reader still hold the destination.  The marker lock
        # serializes cooperating processes; this local lock plus a short retry
        # covers those sharing violations without weakening the durable CAS.
        with _ATOMIC_REPLACE_LOCK:
            attempts = 400 if compat.IS_WINDOWS else 1
            for attempt in range(attempts):
                try:
                    os.replace(temporary_path, path)
                    temporary_path = None
                    break
                except OSError:
                    # Some Windows filesystems can report a failed move after
                    # the replacement is already visible.  Accept only the
                    # exact intended canonical bytes; otherwise retry the same
                    # still-private temp file within a bounded two seconds.
                    try:
                        current_fd = _open_regular(path, os.O_RDONLY)
                        with os.fdopen(
                                current_fd, "rb", closefd=True) as current:
                            committed = current.read() == data
                    except (OSError, RepoOwnerError):
                        committed = False
                    if committed:
                        if temporary_path.exists():
                            temporary_path.unlink()
                        temporary_path = None
                        break
                    if attempt + 1 == attempts:
                        raise
                    time.sleep(0.005)
        _fsync_directory(path.parent)
    except RepoOwnerError:
        raise
    except OSError as exc:
        detail = (
            f"{type(exc).__name__}/errno={getattr(exc, 'errno', None)}"
            f"/winerror={getattr(exc, 'winerror', None)}"
        )
        raise OwnerInvariantError(
            f"atomic JSON write failed:{path.name}:{detail}") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _read_json(path: Path, label: str) -> dict:
    try:
        fd = _open_regular(path, os.O_RDONLY)
    except OwnerInvariantError:
        raise
    try:
        stream = os.fdopen(fd, "rb", closefd=True)
        fd = None
        with stream:
            raw = stream.read()
        value = json.loads(
            raw.decode("utf-8"), parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_pairs)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise OwnerInvariantError(f"cannot decode {label}") from exc
    finally:
        if fd is not None:
            os.close(fd)
    if not isinstance(value, dict):
        raise OwnerInvariantError(f"{label} must be a JSON object")
    return value


def host_boot_identity() -> str:
    """Return a stable best-effort host boot identity, or fail closed."""
    for candidate in (Path("/proc/sys/kernel/random/boot_id"),):
        try:
            value = candidate.read_text(encoding="ascii").strip().lower()
        except OSError:
            continue
        if re.fullmatch(r"[0-9a-f-]{16,64}", value):
            return f"linux-boot-id:{value}"

    if compat.IS_WINDOWS:
        try:
            # SystemTimeOfDayInformation begins with the exact kernel boot
            # FILETIME.  Unlike ``wall clock - GetTickCount64`` it is stable
            # across independently started processes, so an operator may use
            # a changed value as part of an external reboot proof.
            ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
            ntdll.NtQuerySystemInformation.argtypes = [
                ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong),
            ]
            ntdll.NtQuerySystemInformation.restype = ctypes.c_long
            buffer = (ctypes.c_ubyte * 64)()
            returned = ctypes.c_ulong()
            status = int(ntdll.NtQuerySystemInformation(
                3, ctypes.byref(buffer), ctypes.sizeof(buffer),
                ctypes.byref(returned)))
            # Some Windows versions require the exact structure size and
            # return STATUS_INFO_LENGTH_MISMATCH for a larger buffer.
            if status < 0 and 8 <= returned.value <= 4096:
                buffer = (ctypes.c_ubyte * returned.value)()
                status = int(ntdll.NtQuerySystemInformation(
                    3, ctypes.byref(buffer), ctypes.sizeof(buffer),
                    ctypes.byref(returned)))
            if status < 0 or ctypes.sizeof(buffer) < ctypes.sizeof(ctypes.c_longlong):
                raise OSError("NtQuerySystemInformation failed")
            boot_filetime = ctypes.c_longlong.from_buffer(buffer).value
            machine = platform.node().casefold()
            material = f"{machine}\0{boot_filetime}".encode("utf-8")
            return "windows-boot:" + hashlib.sha256(material).hexdigest()
        except (AttributeError, OSError, ValueError):
            pass

    try:
        for line in Path("/proc/stat").read_text(encoding="ascii").splitlines():
            if line.startswith("btime "):
                return "posix-btime:" + line.split(maxsplit=1)[1]
    except OSError:
        pass

    try:
        result = subprocess.run(
            ["sysctl", "-n", "kern.boottime"], capture_output=True,
            text=True, check=False, shell=False)
        if result.returncode == 0 and result.stdout.strip():
            material = result.stdout.strip().encode("utf-8")
            return "sysctl-boot:" + hashlib.sha256(material).hexdigest()
    except OSError:
        pass
    raise OwnerInvariantError("host boot identity is unavailable")


def process_creation_token(pid: int) -> str:
    """Return a PID-reuse-resistant creation token for one live process."""
    _require_positive_int(pid, "pid")
    if compat.IS_WINDOWS:
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                         wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, pid)
        if not handle:
            raise OwnerInvariantError("cannot open process for creation identity")
        try:
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                    handle, ctypes.byref(created), ctypes.byref(exited),
                    ctypes.byref(kernel), ctypes.byref(user)):
                raise OwnerInvariantError("cannot read process creation identity")
            value = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
            return f"windows-filetime:{value}"
        finally:
            kernel32.CloseHandle(handle)

    stat_path = Path(f"/proc/{pid}/stat")
    try:
        raw = stat_path.read_text(encoding="ascii")
        # comm may contain spaces and parentheses; the final ')' terminates it.
        tail = raw[raw.rfind(")") + 2:].split()
        start_ticks = tail[19]  # field 22; tail begins at field 3
        if not start_ticks.isdigit():
            raise ValueError("invalid start ticks")
        return f"proc-start:{start_ticks}"
    except (OSError, IndexError, ValueError):
        pass

    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
            text=True, check=False, shell=False)
        value = result.stdout.strip()
        if result.returncode == 0 and value:
            return "ps-start:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
    except OSError:
        pass
    raise OwnerInvariantError("process creation identity is unavailable")


def current_owner_identity() -> dict:
    pid = os.getpid()
    return {"pid": pid, "creation_token": process_creation_token(pid)}


def child_process_identity(
        pid: int, *, containment_kind: str, containment_id,
        creation_token: str | None = None) -> dict:
    """Build a strict child identity for ``publish_child_running``."""
    _require_positive_int(pid, "child pid")
    if containment_kind not in {
            "process-group", "job", _POSIX_GUARDIAN_KIND,
            _WINDOWS_STRICT_JOB_KIND}:
        raise OwnerAuthorityError("containment_kind is outside the closed set")
    token = creation_token or process_creation_token(pid)
    _require_nonempty_string(token, "child creation_token")
    identity = {
        "pid": pid,
        "creation_token": token,
        "containment_kind": containment_kind,
        "containment_id": str(containment_id),
    }
    _validate_child_identity(identity)
    return identity


def _windows_resume_suspended_primary_thread(pid: int) -> None:
    """Resume the one primary thread created by ``CREATE_SUSPENDED``."""
    if not compat.IS_WINDOWS:  # pragma: no cover - guarded by caller
        raise OwnerInvariantError("Windows suspended-thread API on POSIX")
    from ctypes import wintypes

    class ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(ThreadEntry32)]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(ThreadEntry32)]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [
        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)  # SNAPTHREAD
    invalid_handle = ctypes.c_void_p(-1).value
    if not snapshot or int(snapshot) == invalid_handle:
        raise OwnerInvariantError("cannot enumerate suspended process threads")
    thread_ids = []
    try:
        entry = ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while ok:
            if int(entry.th32OwnerProcessID) == pid:
                thread_ids.append(int(entry.th32ThreadID))
            entry.dwSize = ctypes.sizeof(entry)
            ok = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    if len(thread_ids) != 1:
        raise OwnerInvariantError(
            f"suspended process has {len(thread_ids)} primary thread candidates")
    thread = kernel32.OpenThread(0x0002, False, thread_ids[0])  # SUSPEND_RESUME
    if not thread:
        raise OwnerInvariantError("cannot open suspended primary thread")
    try:
        previous_count = int(kernel32.ResumeThread(thread))
        if previous_count != 1:
            raise OwnerInvariantError(
                "suspended primary thread has an unexpected suspend count")
    finally:
        kernel32.CloseHandle(thread)


def _windows_job_active_processes(process) -> int:
    if not compat.IS_WINDOWS:  # pragma: no cover - guarded by caller
        return 0
    from ctypes import wintypes

    class JobBasicAccountingInformation(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    handle = getattr(process, "_loop_job_handle", None)
    if not handle:
        raise OwnerInvariantError("controlled Windows child has no Job handle")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    info = JobBasicAccountingInformation()
    returned = wintypes.DWORD()
    if not kernel32.QueryInformationJobObject(
            handle, 1, ctypes.byref(info), ctypes.sizeof(info),
            ctypes.byref(returned)):
        raise OwnerInvariantError("cannot query controlled Windows Job")
    return int(info.ActiveProcesses)


def _windows_terminate_job(process, exit_code: int = 1) -> None:
    if not compat.IS_WINDOWS:  # pragma: no cover - guarded by caller
        return
    from ctypes import wintypes

    handle = getattr(process, "_loop_job_handle", None)
    if not handle:
        raise OwnerInvariantError("controlled Windows child has no Job handle")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    if not kernel32.TerminateJobObject(handle, int(exit_code) & 0xFFFFFFFF):
        raise OwnerInvariantError("cannot terminate controlled Windows Job")


def _read_pipe_exact(fd: int, size: int, timeout: float) -> bytes:
    """Read one small POSIX guardian frame without an unbounded startup wait."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    value = b""
    while len(value) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            readable, _, _ = select.select([fd], [], [], remaining)
        except InterruptedError:
            continue
        if not readable:
            break
        try:
            chunk = os.read(fd, size - len(value))
        except InterruptedError:
            continue
        if not chunk:
            break
        value += chunk
    return value


class ControlledOwnerChild:
    """Popen-compatible child whose complete containment is durably fenced.

    On POSIX ``process`` is the trusted subreaper guardian.  Its background
    completion watcher durably records the payload return code only after the
    guardian proves every adopted descendant is gone, then ACKs the guardian
    so normal Popen ``wait``/``communicate`` semantics remain available.
    """

    def __init__(self, *, fence: "RepoOwnerFence", child_generation: int,
                 process: subprocess.Popen, argv: Sequence[str],
                 containment_kind: str, containment_id: str,
                 control_write: int | None = None,
                 status_read: int | None = None):
        self.fence = fence
        self.child_generation = child_generation
        self.process = process
        self.args = list(argv)
        self.containment_kind = containment_kind
        self.containment_id = containment_id
        self._recorded = False
        self._containment_closed = False
        self._force_fenced = False
        self._payload_returncode = None
        self._proof_error: BaseException | None = None
        self._control_write = control_write
        self._status_read = status_read
        self._lifecycle_lock = threading.Lock()
        self._completion = threading.Event()
        self._watcher = None
        if containment_kind == _POSIX_GUARDIAN_KIND:
            if control_write is None or status_read is None:
                raise OwnerInvariantError(
                    "POSIX guardian is missing completion channels")
            self._watcher = threading.Thread(
                target=self._watch_guardian_completion,
                name=f"repo-owner-guardian-{process.pid}", daemon=True)
            self._watcher.start()

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def returncode(self):
        if self._payload_returncode is not None:
            return self._payload_returncode
        return self.process.returncode

    @property
    def stdin(self):
        return self.process.stdin

    @property
    def stdout(self):
        return self.process.stdout

    @property
    def stderr(self):
        return self.process.stderr

    def poll(self):
        if self._payload_returncode is not None:
            return self._payload_returncode
        return self.process.poll()

    def wait(self, timeout=None):
        if self.containment_kind != _POSIX_GUARDIAN_KIND:
            return self.process.wait(timeout=timeout)
        started = time.monotonic()
        self.process.wait(timeout=timeout)
        remaining = None
        if timeout is not None:
            remaining = max(0.0, float(timeout) - (time.monotonic() - started))
        if not self._completion.wait(remaining):
            raise subprocess.TimeoutExpired(self.args, timeout)
        if self._proof_error is not None:
            raise OwnerBusy(
                "controlled POSIX guardian lacks completion proof") from self._proof_error
        return self.returncode

    def communicate(self, input=None, timeout=None):
        result = self.process.communicate(input=input, timeout=timeout)
        if (self.containment_kind == _POSIX_GUARDIAN_KIND
                and self._completion.is_set() and self._proof_error is not None):
            raise OwnerBusy(
                "controlled POSIX guardian lacks completion proof") from self._proof_error
        return result

    @staticmethod
    def _read_exact(fd: int, size: int) -> bytes:
        value = b""
        while len(value) < size:
            try:
                chunk = os.read(fd, size - len(value))
            except InterruptedError:
                continue
            if not chunk:
                break
            value += chunk
        return value

    def _close_channel(self, name: str) -> None:
        fd = getattr(self, name)
        if fd is None:
            return
        setattr(self, name, None)
        try:
            os.close(fd)
        except OSError:
            pass

    def _write_control(self, command: bytes) -> bool:
        fd = self._control_write
        if fd is None:
            return False
        try:
            return os.write(fd, command) == len(command)
        except OSError:
            return False

    def _watch_guardian_completion(self) -> None:
        """Persist the guardian proof before allowing its durable root to exit."""
        try:
            completion = self._read_exact(int(self._status_read), 5)
            if len(completion) != 5 or completion[:1] != b"D":
                code = (struct.unpack("!i", completion[1:])[0]
                        if len(completion) == 5 else None)
                raise OwnerBusy(
                    "controlled POSIX guardian reported no descendant proof"
                    + (f" (code {code})" if code is not None else ""))
            returncode = struct.unpack("!i", completion[1:])[0]
            with self._lifecycle_lock:
                marker = self.fence.record_child_result(
                    self.child_generation, returncode)
                if marker["child_state"] != "child_reaped":
                    raise OwnerInvariantError(
                        "guardian completion did not persist child_reaped")
                self._payload_returncode = returncode
                self._recorded = True
            if not self._write_control(b"A"):
                raise OwnerBusy("cannot ACK completed POSIX guardian")
        except BaseException as exc:
            self._proof_error = exc
            # A D record proves the tree empty even if durable CAS failed.  E
            # lets that quiescent guardian exit while the marker stays
            # deliberately nonterminal.  An F guardian ignores this and stays
            # resident for explicit recovery fencing.
            self._write_control(b"E")
        finally:
            self._close_channel("_status_read")
            self._close_channel("_control_write")
            self._completion.set()

    def _containment_empty(self) -> bool:
        if compat.IS_WINDOWS:
            if self._containment_closed:
                return True
            return _windows_job_active_processes(self.process) == 0
        if self.containment_kind == _POSIX_GUARDIAN_KIND:
            return bool(
                (self._recorded or self._force_fenced)
                and self.process.poll() is not None)
        try:
            os.killpg(int(self.containment_id), 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        if self._force_fenced and Path("/proc").is_dir():
            # After SIGKILL no member can fork.  Linux may retain orphaned
            # zombies until PID 1 reaps them; zombies cannot mutate the repo
            # and need not keep a proven-fenced marker nonterminal forever.
            pgid = int(self.containment_id)
            for stat_path in Path("/proc").glob("[0-9]*/stat"):
                try:
                    raw = stat_path.read_text(encoding="ascii")
                    tail = raw[raw.rfind(")") + 2:].split()
                    state, process_group = tail[0], int(tail[2])
                except (OSError, IndexError, ValueError):
                    continue
                if process_group == pgid and state != "Z":
                    return False
            return True
        return False

    def _wait_containment_empty(self, timeout: float) -> bool:
        if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                or timeout < 0):
            raise OwnerAuthorityError("containment timeout must be non-negative")
        deadline = time.monotonic() + float(timeout)
        while True:
            if self._containment_empty():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    def interrupt_containment(self) -> None:
        if self._containment_closed:
            return
        if self.containment_kind == _POSIX_GUARDIAN_KIND:
            self._write_control(b"X")
            return
        try:
            compat.interrupt_process_group(self.process)
        except ProcessLookupError:
            return

    def kill_containment(self, timeout: float = 5.0) -> None:
        """Force the complete process group/Job to zero active processes."""
        if self._containment_closed:
            return
        if compat.IS_WINDOWS:
            if _windows_job_active_processes(self.process):
                _windows_terminate_job(self.process)
        elif self.containment_kind == _POSIX_GUARDIAN_KIND:
            self._write_control(b"X")
            deadline = time.monotonic() + float(timeout)
            self._completion.wait(max(0.0, deadline - time.monotonic()))
            remaining = max(0.0, deadline - time.monotonic())
            try:
                self.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                identity = compat.capture_process_identity(self.process)
                if not compat.fence_process_tree(
                        self.process,
                        start_token=identity["start_token"],
                        group_id=identity["group_id"],
                        graceful_timeout=0.2,
                        force_timeout=max(0.2, remaining)):
                    raise OwnerBusy(
                        "cannot fence controlled POSIX guardian tree")
                self.process.wait(timeout=max(0.2, remaining))
                with self._lifecycle_lock:
                    self._force_fenced = True
                    if self._payload_returncode is None:
                        self._payload_returncode = 125
            if (self._proof_error is not None
                    and not (self._force_fenced or self._recorded)):
                raise OwnerBusy(
                    "controlled POSIX guardian could not prove completion") \
                    from self._proof_error
        else:
            try:
                os.killpg(int(self.containment_id), signal.SIGKILL)
                self._force_fenced = True
            except ProcessLookupError:
                self._force_fenced = True
                pass
            except PermissionError as exc:
                raise OwnerBusy("cannot fence controlled process group") from exc
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise OwnerBusy("controlled child leader did not exit") from exc
        if not self._wait_containment_empty(timeout):
            raise OwnerBusy("controlled child containment is still active")

    def record_result(self, containment_timeout: float = 0.5) -> dict:
        """Write ``child_reaped`` only after leader and all descendants exit."""
        if self._recorded:
            return self.fence.marker
        if self.containment_kind == _POSIX_GUARDIAN_KIND:
            if (not isinstance(containment_timeout, (int, float))
                    or isinstance(containment_timeout, bool)
                    or containment_timeout < 0):
                raise OwnerAuthorityError(
                    "containment timeout must be non-negative")
            self._completion.wait(float(containment_timeout))
            with self._lifecycle_lock:
                if self._recorded:
                    return self.fence.marker
                if self._force_fenced and self.process.poll() is not None:
                    marker = self.fence.record_child_result(
                        self.child_generation,
                        int(self._payload_returncode
                            if self._payload_returncode is not None else 125),
                    )
                    self._recorded = True
                    return marker
                if self._proof_error is not None:
                    raise OwnerBusy(
                        "controlled POSIX guardian lacks completion proof") \
                        from self._proof_error
            raise OwnerBusy("controlled POSIX guardian is still active")
        returncode = self.process.poll()
        if returncode is None:
            raise OwnerBusy("controlled child leader is still active")
        if not self._wait_containment_empty(containment_timeout):
            raise OwnerBusy("controlled child descendants are still active")
        marker = self.fence.record_child_result(
            self.child_generation, int(returncode))
        self._recorded = True
        if compat.IS_WINDOWS and not self._containment_closed:
            compat.close_process_group(self.process)
            self._containment_closed = True
        return marker

    def terminate(self) -> None:
        self.interrupt_containment()

    def kill(self) -> None:
        self.kill_containment()


def _validate_owner_identity(identity) -> dict:
    _require_exact_dict(identity, {"pid", "creation_token"}, "owner_identity")
    _require_positive_int(identity["pid"], "owner_identity.pid")
    _require_nonempty_string(
        identity["creation_token"], "owner_identity.creation_token")
    return identity


def _validate_child_identity(identity) -> dict:
    _require_exact_dict(
        identity,
        {"pid", "creation_token", "containment_kind", "containment_id"},
        "child_identity",
    )
    _require_positive_int(identity["pid"], "child_identity.pid")
    _require_nonempty_string(
        identity["creation_token"], "child_identity.creation_token")
    if identity["containment_kind"] not in {
            "process-group", "job", _POSIX_GUARDIAN_KIND,
            _WINDOWS_STRICT_JOB_KIND}:
        raise OwnerInvariantError("child containment kind is unknown")
    _require_nonempty_string(
        identity["containment_id"], "child_identity.containment_id")
    return identity


_MARKER_FIELDS = frozenset({
    "schema_version", "canonical_repo", "common_dir", "owner_kind",
    "workspace", "state_path", "session", "generation", "state",
    "owner_identity", "host_boot_identity", "child_generation",
    "child_state", "child_kind", "argv_hash", "child_identity",
    "child_result", "recovery_history", "created_at", "updated_at",
    "terminal_reason",
})


def _validate_child_result(result, *, recovery: bool) -> dict:
    _require_exact_dict(
        result, {"status", "returncode", "recorded_at", "proof"},
        "child_result")
    if result["status"] not in {"exited", "recovered"}:
        raise OwnerInvariantError("child result status is unknown")
    if result["status"] == "exited":
        if (not isinstance(result["returncode"], int)
                or isinstance(result["returncode"], bool)):
            raise OwnerInvariantError("exited child result requires integer returncode")
        if result["proof"] != "waited-and-reaped":
            raise OwnerInvariantError("exited child result has invalid proof kind")
    else:
        if not recovery or result["returncode"] is not None:
            raise OwnerInvariantError("recovered child result is outside recovery")
        if result["proof"] != "explicit-fencing-callback":
            raise OwnerInvariantError("recovered child result has invalid proof kind")
    _require_nonempty_string(result["recorded_at"], "child_result.recorded_at")
    return result


def _validate_recovery_history(history, marker: dict) -> list:
    if not isinstance(history, list):
        raise OwnerInvariantError("recovery_history must be an array")
    previous_to_generation = None
    for index, event in enumerate(history):
        _require_exact_dict(event, {
            "from_state", "from_session", "from_generation",
            "from_owner_identity", "to_session", "to_generation",
            "to_owner_identity", "authorized_at", "proof",
        }, f"recovery_history[{index}]")
        if event["from_state"] not in {"active", "recovering"}:
            raise OwnerInvariantError("recovery history from_state is invalid")
        _require_hex(event["from_session"], _HEX32_RE, "recovery from_session")
        _require_hex(event["to_session"], _HEX32_RE, "recovery to_session")
        from_generation = _require_positive_int(
            event["from_generation"], "recovery from_generation")
        to_generation = _require_positive_int(
            event["to_generation"], "recovery to_generation")
        if to_generation != from_generation + 1:
            raise OwnerInvariantError("recovery generation is not a CAS increment")
        if previous_to_generation is not None and from_generation != previous_to_generation:
            raise OwnerInvariantError("recovery generation chain is discontinuous")
        previous_to_generation = to_generation
        _validate_owner_identity(event["from_owner_identity"])
        _validate_owner_identity(event["to_owner_identity"])
        _require_nonempty_string(event["authorized_at"], "recovery authorized_at")
        if event["proof"] != "explicit-fencing-callback":
            raise OwnerInvariantError("recovery proof kind is invalid")
    if history:
        last = history[-1]
        if (last["to_generation"] != marker["generation"]
                or last["to_session"] != marker["session"]
                or last["to_owner_identity"] != marker["owner_identity"]):
            raise OwnerInvariantError("recovery history tip does not own marker")
    return history


def _validate_marker(marker: dict) -> dict:
    _require_exact_dict(marker, _MARKER_FIELDS, "owner marker")
    if marker["schema_version"] != 1:
        raise OwnerInvariantError("owner marker schema_version is unsupported")
    for field in ("canonical_repo", "common_dir", "workspace", "state_path"):
        value = _require_nonempty_string(marker[field], f"owner marker {field}")
        if not Path(value).is_absolute():
            raise OwnerInvariantError(f"owner marker {field} is not absolute")
    try:
        OwnerKind(marker["owner_kind"])
    except (TypeError, ValueError) as exc:
        raise OwnerInvariantError("owner marker owner_kind is unknown") from exc
    _require_hex(marker["session"], _HEX32_RE, "owner marker session")
    _require_positive_int(marker["generation"], "owner marker generation")
    if marker["state"] not in {"active", "recovering", "terminal"}:
        raise OwnerInvariantError("owner marker state is unknown")
    _validate_owner_identity(marker["owner_identity"])
    _require_nonempty_string(
        marker["host_boot_identity"], "owner marker host_boot_identity")
    child_generation = _require_nonnegative_int(
        marker["child_generation"], "owner marker child_generation")
    if marker["child_state"] not in {
            "idle", "launching", "child_running", "child_reaped"}:
        raise OwnerInvariantError("owner marker child_state is unknown")
    if marker["child_state"] == "idle":
        if any(marker[field] is not None for field in (
                "child_kind", "argv_hash", "child_identity", "child_result")):
            raise OwnerInvariantError("idle child marker retains active child fields")
    else:
        if child_generation < 1:
            raise OwnerInvariantError("non-idle child has no generation")
        try:
            ChildKind(marker["child_kind"])
        except (TypeError, ValueError) as exc:
            raise OwnerInvariantError("owner marker child_kind is unknown") from exc
        _require_hex(marker["argv_hash"], _HASH64_RE, "owner marker argv_hash")
        if marker["child_state"] == "launching":
            if marker["child_identity"] is not None or marker["child_result"] is not None:
                raise OwnerInvariantError("launching child contains premature identity/result")
        elif marker["child_state"] == "child_running":
            _validate_child_identity(marker["child_identity"])
            if marker["child_result"] is not None:
                raise OwnerInvariantError("running child contains a terminal result")
        else:
            recovery_result = (isinstance(marker["child_result"], dict)
                               and marker["child_result"].get("status") == "recovered")
            if marker["child_identity"] is None:
                if not recovery_result:
                    raise OwnerInvariantError("reaped child has no durable identity")
            else:
                _validate_child_identity(marker["child_identity"])
            _validate_child_result(
                marker["child_result"],
                recovery=bool(marker["recovery_history"]),
            )
    _validate_recovery_history(marker["recovery_history"], marker)
    _require_nonempty_string(marker["created_at"], "owner marker created_at")
    _require_nonempty_string(marker["updated_at"], "owner marker updated_at")
    if marker["state"] == "terminal":
        _require_nonempty_string(marker["terminal_reason"], "terminal_reason")
        if marker["child_state"] not in {"idle", "child_reaped"}:
            raise OwnerInvariantError("terminal owner still has an unfenced child")
    elif marker["terminal_reason"] is not None:
        raise OwnerInvariantError("nonterminal owner contains terminal_reason")
    if marker["state"] == "recovering" and not marker["recovery_history"]:
        raise OwnerInvariantError("recovering owner has no recovery audit")
    return marker


def _validate_executor_child_identity(identity) -> dict:
    _require_exact_dict(
        identity,
        {"pid", "start_token", "group_id", "containment_kind"},
        "RepoExecutor child identity",
    )
    _require_positive_int(identity["pid"], "executor child pid")
    _require_positive_int(identity["group_id"], "executor child group_id")
    _require_nonempty_string(
        identity["start_token"], "executor child start_token")
    if identity["containment_kind"] not in {
            "process-tree", "windows-job", _WINDOWS_STRICT_JOB_KIND}:
        raise OwnerInvariantError("RepoExecutor child containment is unknown")
    return identity


def _validate_executor_child_result(result, *, identity_present: bool) -> dict:
    _require_exact_dict(
        result, {"status", "returncode", "recorded_at"},
        "RepoExecutor child result")
    if result["status"] not in {"exited", "not-started"}:
        raise OwnerInvariantError("RepoExecutor child result status is unknown")
    if result["status"] == "exited":
        if (not identity_present
                or not isinstance(result["returncode"], int)
                or isinstance(result["returncode"], bool)):
            raise OwnerInvariantError(
                "RepoExecutor exited child lacks exact evidence")
    elif identity_present or result["returncode"] is not None:
        raise OwnerInvariantError(
            "RepoExecutor not-started child contains process evidence")
    _require_nonempty_string(
        result["recorded_at"], "executor child recorded_at")
    return result


def _validate_executor_lease(lease: dict) -> dict:
    fields = {
        "schema_version", "state", "operation", "operation_id", "request_hash",
        "immutable_spec_hash", "nonce", "generation", "executor_session", "pid",
        "executor_creation_token", "expected", "request",
        "child_generation", "child_state", "child_kind", "child_argv_hash",
        "child_identity", "child_result", "child_history",
        "updated_at", "terminal_status", "result_hash", "reason",
    }
    _require_exact_dict(lease, fields, "RepoExecutor operation lease")
    if lease["schema_version"] != 2:
        raise OwnerInvariantError("RepoExecutor lease schema is unsupported")
    if lease["state"] not in {"reserved", "running", "terminal"}:
        raise OwnerInvariantError("RepoExecutor lease state is unknown")
    if lease["operation"] not in _EXECUTOR_OPERATIONS:
        raise OwnerInvariantError("RepoExecutor lease operation is unknown")
    _require_hex(lease["operation_id"], _HEX32_RE, "executor operation_id")
    _require_hex(lease["request_hash"], _HASH64_RE, "executor request_hash")
    _require_hex(
        lease["immutable_spec_hash"], _HASH64_RE, "executor immutable_spec_hash")
    _require_hex(lease["nonce"], _HEX32_RE, "executor nonce")
    _require_positive_int(lease["generation"], "executor generation")
    _require_hex(lease["executor_session"], _HEX32_RE, "executor session")
    _require_positive_int(lease["pid"], "executor pid")
    _require_nonempty_string(
        lease["executor_creation_token"], "executor creation token")
    if not isinstance(lease["expected"], dict):
        raise OwnerInvariantError("RepoExecutor expected state is not an object")
    request = lease["request"]
    if not isinstance(request, dict):
        raise OwnerInvariantError("RepoExecutor durable request is not an object")
    try:
        request_hash = hashlib.sha256(_canonical_json_bytes(request)).hexdigest()
    except OwnerAuthorityError as exc:
        raise OwnerInvariantError(
            "RepoExecutor durable request is not canonical JSON") from exc
    task_operations = {"CREATE_WORKTREE", "GATE_MERGE", "REMOVE_WORKTREE"}
    request_fields = {"operation", "operation_id", "authority", "expected"}
    if lease["operation"] in task_operations:
        request_fields.add("task")
    _require_exact_dict(request, request_fields, "RepoExecutor durable request")
    if (request_hash != lease["request_hash"]
            or request["operation"] != lease["operation"]
            or request["operation_id"] != lease["operation_id"]
            or request["expected"] != lease["expected"]
            or not isinstance(request["authority"], dict)):
        raise OwnerInvariantError(
            "RepoExecutor durable request is inconsistent")
    if "task" in request:
        _require_positive_int(request["task"], "executor request task")

    child_generation = _require_nonnegative_int(
        lease["child_generation"], "executor child generation")
    history = lease["child_history"]
    if not isinstance(history, list):
        raise OwnerInvariantError("RepoExecutor child history is not an array")
    previous_generation = 0
    for index, entry in enumerate(history):
        _require_exact_dict(
            entry, {"generation", "kind", "argv_hash", "identity", "result"},
            f"RepoExecutor child_history[{index}]")
        generation = _require_positive_int(
            entry["generation"], "executor child history generation")
        if generation != previous_generation + 1:
            raise OwnerInvariantError(
                "RepoExecutor child history generation is not contiguous")
        previous_generation = generation
        if entry["kind"] not in {"git", "validator"}:
            raise OwnerInvariantError("RepoExecutor child history kind is unknown")
        _require_hex(
            entry["argv_hash"], _HASH64_RE,
            "executor child history argv_hash")
        if entry["identity"] is not None:
            _validate_executor_child_identity(entry["identity"])
        _validate_executor_child_result(
            entry["result"], identity_present=entry["identity"] is not None)

    if lease["child_state"] not in {"idle", "launching", "running", "reaped"}:
        raise OwnerInvariantError("RepoExecutor child state is unknown")
    if lease["child_state"] == "idle":
        if (previous_generation != child_generation
                or any(lease[field] is not None for field in (
                    "child_kind", "child_argv_hash", "child_identity",
                    "child_result"))):
            raise OwnerInvariantError("idle RepoExecutor child retains evidence")
    else:
        if child_generation < 1 or previous_generation != child_generation - 1:
            raise OwnerInvariantError(
                "active RepoExecutor child history is not contiguous")
        if lease["child_kind"] not in {"git", "validator"}:
            raise OwnerInvariantError("RepoExecutor child kind is unknown")
        _require_hex(
            lease["child_argv_hash"], _HASH64_RE,
            "executor child argv_hash")
        if lease["child_state"] == "launching":
            if (lease["child_identity"] is not None
                    or lease["child_result"] is not None):
                raise OwnerInvariantError(
                    "launching RepoExecutor child has premature evidence")
        elif lease["child_state"] == "running":
            _validate_executor_child_identity(lease["child_identity"])
            if lease["child_result"] is not None:
                raise OwnerInvariantError(
                    "running RepoExecutor child has a terminal result")
        else:
            if lease["child_identity"] is not None:
                _validate_executor_child_identity(lease["child_identity"])
            _validate_executor_child_result(
                lease["child_result"],
                identity_present=lease["child_identity"] is not None)

    _require_nonempty_string(lease["updated_at"], "executor updated_at")
    if lease["reason"] is not None and not isinstance(lease["reason"], str):
        raise OwnerInvariantError("RepoExecutor reason is invalid")
    if lease["state"] == "terminal":
        _require_nonempty_string(lease["terminal_status"], "executor terminal_status")
        if lease["child_state"] in {"launching", "running"}:
            raise OwnerInvariantError(
                "terminal RepoExecutor lease retains an unfenced child")
        if (lease["result_hash"] is not None
                and (not isinstance(lease["result_hash"], str)
                     or _HASH64_RE.fullmatch(lease["result_hash"]) is None)):
            raise OwnerInvariantError("RepoExecutor result_hash is invalid")
    elif lease["terminal_status"] is not None or lease["result_hash"] is not None:
        raise OwnerInvariantError("nonterminal RepoExecutor lease has terminal fields")
    return lease


def _audit_executor_terminal_result(sidecar: Path, lease: dict) -> dict:
    """Require a successful terminal lease to have immutable result evidence."""
    if lease["terminal_status"] == "blocked":
        raise OwnerBusy(
            "RepoExecutor terminal operation is blocked and requires exact recovery:"
            f"{lease['operation']}/{lease['operation_id']}")
    if lease["result_hash"] is None:
        raise OwnerInvariantError(
            "successful RepoExecutor terminal lease lacks result_hash")
    result_path = (sidecar / "operation-results"
                   / f"{lease['operation_id']}.json")
    artifact = _read_json(result_path, "RepoExecutor immutable operation result")
    _require_exact_dict(
        artifact,
        {"schema_version", "operation_id", "request_hash", "result", "result_hash"},
        "RepoExecutor operation result",
    )
    if artifact["schema_version"] != 1:
        raise OwnerInvariantError("RepoExecutor result schema version is unknown")
    if not isinstance(artifact["result"], dict):
        raise OwnerInvariantError("RepoExecutor result payload is not an object")
    try:
        computed = hashlib.sha256(
            _canonical_json_bytes(artifact["result"])).hexdigest()
    except OwnerAuthorityError as exc:
        raise OwnerInvariantError(
            "RepoExecutor result payload is not canonical JSON") from exc
    expected_status = artifact["result"].get("status", "completed")
    if (artifact["operation_id"] != lease["operation_id"]
            or artifact["request_hash"] != lease["request_hash"]
            or artifact["result_hash"] != computed
            or lease["result_hash"] != computed
            or lease["terminal_status"] != expected_status):
        raise OwnerInvariantError(
            "RepoExecutor terminal lease/result evidence does not match")
    return artifact


def audit_owner_marker_under_global_lock(
        repo: Path, global_lock_file) -> dict | None:
    """Read-only audit for a caller that already holds the primary lock.

    RepoExecutor/parallel startup calls this *after* acquiring the canonical
    ``loop-agent-lite.run.lock``.  The supplied descriptor is verified to be
    that exact regular file; lock ownership itself is a caller precondition
    because neither ``flock`` nor ``msvcrt.locking`` exposes a portable
    read-only ownership query.

    Missing and terminal markers are safe.  Active/recovering markers require
    the explicit :meth:`RepoOwnerFence.recover` path and are never rewritten by
    this audit.  The function intentionally does not create even a short lock
    artifact: the already-held global lock is the writer serialization point.
    """
    canonical_repo, common_dir = _resolve_repository(Path(repo))
    global_path = common_dir / GLOBAL_LOCK_NAME
    _reject_link_components(global_path.parent)
    try:
        fd = (global_lock_file if isinstance(global_lock_file, int)
              else global_lock_file.fileno())
        handle_info = os.fstat(fd)
        path_info = global_path.lstat()
    except (AttributeError, OSError, ValueError) as exc:
        raise OwnerAuthorityError(
            "global_lock_file is not the canonical global lock") from exc
    if (not stat.S_ISREG(handle_info.st_mode)
            or not stat.S_ISREG(path_info.st_mode)
            or global_path.is_symlink()
            or compat.is_reparse_point(path_info)
            or (handle_info.st_dev, handle_info.st_ino)
            != (path_info.st_dev, path_info.st_ino)):
        raise OwnerAuthorityError(
            "global_lock_file is not the canonical global lock")
    marker_path = common_dir / MARKER_NAME
    if not os.path.lexists(marker_path):
        return None
    marker = _validate_marker(_read_json(marker_path, "owner marker"))
    if (marker["canonical_repo"] != str(canonical_repo)
            or marker["common_dir"] != str(common_dir)):
        raise OwnerInvariantError(
            "owner marker canonical repository identity does not match")
    if marker["state"] != "terminal":
        raise OwnerRecoveryRequired(
            "nonterminal ordinary owner marker requires explicit recovery")
    return _json_clone(marker)


class RepoOwnerFence:
    """One held global lock plus its durable non-guardian owner marker."""

    def __init__(self, *, canonical_repo: Path, common_dir: Path,
                 owner_kind: OwnerKind, workspace: Path, state_path: Path,
                 session: str, owner_identity: dict, boot_identity: str):
        self.canonical_repo = canonical_repo
        self.common_dir = common_dir
        self.owner_kind = owner_kind
        self.workspace = workspace
        self.state_path = state_path
        self.session = session
        self.owner_identity = _json_clone(owner_identity)
        self.boot_identity = boot_identity
        self.marker_path = common_dir / MARKER_NAME
        self.marker_lock_path = common_dir / MARKER_LOCK_NAME
        self.global_lock_path = common_dir / GLOBAL_LOCK_NAME
        self.executor_sidecar = common_dir / EXECUTOR_SIDECAR_NAME
        self._global_lock_file = None
        self._marker = None
        self._closed = False
        self._active_child = None
        self._active_child_lock = threading.Lock()

    @classmethod
    def _new(cls, repo: Path, *, owner_kind, workspace: Path, state_path: Path,
             session: str | None, owner_identity: Mapping | None,
             boot_identity: str | None) -> "RepoOwnerFence":
        canonical_repo, common_dir = _resolve_repository(Path(repo))
        normalized_workspace = _canonical_artifact_path(Path(workspace), "workspace")
        normalized_state = _canonical_artifact_path(Path(state_path), "state_path")
        kind = _coerce_owner_kind(owner_kind)
        owner_session = session or uuid.uuid4().hex
        try:
            _require_hex(owner_session, _HEX32_RE, "session")
        except OwnerInvariantError as exc:
            raise OwnerAuthorityError(str(exc)) from exc
        identity = dict(owner_identity) if owner_identity is not None else current_owner_identity()
        try:
            _validate_owner_identity(identity)
        except OwnerInvariantError as exc:
            raise OwnerAuthorityError(str(exc)) from exc
        boot = boot_identity if boot_identity is not None else host_boot_identity()
        try:
            _require_nonempty_string(boot, "host_boot_identity")
        except OwnerInvariantError as exc:
            raise OwnerAuthorityError(str(exc)) from exc
        return cls(
            canonical_repo=canonical_repo, common_dir=common_dir,
            owner_kind=kind, workspace=normalized_workspace,
            state_path=normalized_state, session=owner_session,
            owner_identity=identity, boot_identity=boot,
        )

    @classmethod
    def claim(cls, repo: Path, *, owner_kind, workspace: Path, state_path: Path,
              session: str | None = None, owner_identity: Mapping | None = None,
              boot_identity: str | None = None) -> "RepoOwnerFence":
        """Claim terminal/missing marker under the held global primary lock."""
        fence = cls._new(
            repo, owner_kind=owner_kind, workspace=workspace,
            state_path=state_path, session=session,
            owner_identity=owner_identity, boot_identity=boot_identity)
        fence._acquire_global()
        try:
            fence._audit_executor_lease()
            with _short_lock(fence.marker_lock_path):
                existing = None
                if os.path.lexists(fence.marker_path):
                    existing = _validate_marker(
                        _read_json(fence.marker_path, "owner marker"))
                    fence._assert_repository_marker(existing)
                if existing is not None and existing["state"] != "terminal":
                    raise OwnerRecoveryRequired(
                        "foreign/nonterminal owner marker requires explicit recovery")
                generation = 1 if existing is None else existing["generation"] + 1
                now = _now()
                marker = {
                    "schema_version": 1,
                    "canonical_repo": str(fence.canonical_repo),
                    "common_dir": str(fence.common_dir),
                    "owner_kind": fence.owner_kind.value,
                    "workspace": str(fence.workspace),
                    "state_path": str(fence.state_path),
                    "session": fence.session,
                    "generation": generation,
                    "state": "active",
                    "owner_identity": _json_clone(fence.owner_identity),
                    "host_boot_identity": fence.boot_identity,
                    "child_generation": 0,
                    "child_state": "idle",
                    "child_kind": None,
                    "argv_hash": None,
                    "child_identity": None,
                    "child_result": None,
                    "recovery_history": [],
                    "created_at": now,
                    "updated_at": now,
                    "terminal_reason": None,
                }
                _validate_marker(marker)
                _atomic_json(fence.marker_path, marker)
                fence._marker = marker
            return fence
        except BaseException:
            fence.close()
            raise

    @classmethod
    def recover(
            cls, repo: Path, *, expected_owner_kind, expected_workspace: Path,
            expected_state_path: Path, expected_session: str,
            expected_generation: int,
            recovery_authorizer: Callable[[dict], bool],
            recovery_session: str | None = None,
            recovery_identity: Mapping | None = None,
            boot_identity: str | None = None) -> "RepoOwnerFence":
        """Generation-CAS a recorded nonterminal owner into ``recovering``.

        The callback runs without the short marker lock, but while the global
        primary lock remains held.  It must prove that the previous owner and
        every possible child/process group/Job are gone.  PID liveness, Git
        HEAD, or a truthy non-``True`` value are not accepted as proof.
        """
        if not callable(recovery_authorizer):
            raise OwnerAuthorityError("recovery_authorizer must be callable")
        _require_positive_int(expected_generation, "expected_generation")
        try:
            _require_hex(expected_session, _HEX32_RE, "expected_session")
        except OwnerInvariantError as exc:
            raise OwnerAuthorityError(str(exc)) from exc
        fence = cls._new(
            repo, owner_kind=expected_owner_kind,
            workspace=expected_workspace, state_path=expected_state_path,
            session=recovery_session, owner_identity=recovery_identity,
            boot_identity=boot_identity)
        fence._acquire_global()
        try:
            fence._audit_executor_lease()
            with _short_lock(fence.marker_lock_path):
                if not os.path.lexists(fence.marker_path):
                    raise OwnerRecoveryRequired("owner marker does not exist")
                observed = _validate_marker(
                    _read_json(fence.marker_path, "owner marker"))
                fence._assert_repository_marker(observed)
                fence._assert_recovery_expectation(
                    observed, expected_session=expected_session,
                    expected_generation=expected_generation)
                snapshot = _json_clone(observed)

            try:
                authorized = recovery_authorizer(_json_clone(snapshot)) is True
            except Exception as exc:
                raise OwnerRecoveryRequired(
                    "owner recovery authorization failed") from exc
            if not authorized:
                raise OwnerRecoveryRequired(
                    "owner/child fencing proof was not established")

            with _short_lock(fence.marker_lock_path):
                current = _validate_marker(
                    _read_json(fence.marker_path, "owner marker"))
                if current != snapshot:
                    raise OwnerBusy("owner marker changed during recovery proof")
                now = _now()
                generation = current["generation"] + 1
                event = {
                    "from_state": current["state"],
                    "from_session": current["session"],
                    "from_generation": current["generation"],
                    "from_owner_identity": _json_clone(current["owner_identity"]),
                    "to_session": fence.session,
                    "to_generation": generation,
                    "to_owner_identity": _json_clone(fence.owner_identity),
                    "authorized_at": now,
                    "proof": "explicit-fencing-callback",
                }
                recovered = copy.deepcopy(current)
                recovered.update({
                    "session": fence.session,
                    "generation": generation,
                    "state": "recovering",
                    "owner_identity": _json_clone(fence.owner_identity),
                    "host_boot_identity": fence.boot_identity,
                    "updated_at": now,
                    "terminal_reason": None,
                    "recovery_history": [*current["recovery_history"], event],
                })
                if current["child_state"] != "idle":
                    recovered["child_state"] = "child_reaped"
                    recovered["child_result"] = {
                        "status": "recovered",
                        "returncode": None,
                        "recorded_at": now,
                        "proof": "explicit-fencing-callback",
                    }
                _validate_marker(recovered)
                _atomic_json(fence.marker_path, recovered)
                fence._marker = recovered
            return fence
        except BaseException:
            fence.close()
            raise

    @classmethod
    def inspect(cls, repo: Path) -> dict | None:
        """Return a diagnostic marker snapshot; it grants no authority."""
        _canonical_repo, common_dir = _resolve_repository(Path(repo))
        marker_path = common_dir / MARKER_NAME
        if not os.path.lexists(marker_path):
            return None
        with _short_lock(common_dir / MARKER_LOCK_NAME):
            if not os.path.lexists(marker_path):
                return None
            return _json_clone(_validate_marker(
                _read_json(marker_path, "owner marker")))

    @property
    def marker(self) -> dict:
        if self._marker is None:
            raise OwnerAuthorityError("owner marker has not been claimed")
        return _json_clone(self._marker)

    @property
    def generation(self) -> int:
        return self.marker["generation"]

    def begin_child(self, child_kind, argv: Sequence[str]) -> int:
        """Durably publish ``launching`` before the caller attempts spawn."""
        kind = _coerce_child_kind(child_kind)
        if (not isinstance(argv, (list, tuple)) or not argv
                or any(not isinstance(item, str) or not item or "\x00" in item
                       for item in argv)):
            raise OwnerAuthorityError("argv must be a non-empty string sequence")
        argv_hash = hashlib.sha256(
            _canonical_json_bytes(list(argv))).hexdigest()

        def transition(marker: dict) -> None:
            if marker["state"] not in {"active", "recovering"}:
                raise OwnerBusy("terminal owner cannot launch a child")
            if marker["child_state"] != "idle":
                raise OwnerBusy("previous child lifecycle is not checkpointed idle")
            marker.update({
                "child_generation": marker["child_generation"] + 1,
                "child_state": "launching",
                "child_kind": kind.value,
                "argv_hash": argv_hash,
                "child_identity": None,
                "child_result": None,
                "updated_at": _now(),
            })

        marker = self._transition(transition)
        return marker["child_generation"]

    def spawn_child(self, child_kind, argv: Sequence[str], **popen_kwargs) -> ControlledOwnerChild:
        """Atomically prepare and spawn a zero-payload-gap controlled child."""
        child_generation = self.begin_child(child_kind, argv)
        return self.spawn_prepared_child(
            child_generation, argv, **popen_kwargs)

    def spawn_prepared_child(
            self, child_generation: int, argv: Sequence[str],
            **popen_kwargs) -> ControlledOwnerChild:
        """Spawn the exact argv previously bound by :meth:`begin_child`.

        POSIX starts a verified Linux subreaper guardian in a new session.  It
        cannot start the payload until ``child_running`` is durable and cannot
        exit normally until its recursive descendant proof is durably ACKed.
        Windows creates the payload suspended, verifies membership in a strict
        no-breakaway Job, publishes that versioned identity, then resumes the
        sole primary thread.
        """
        _require_positive_int(child_generation, "child_generation")
        if (not isinstance(argv, (list, tuple)) or not argv
                or any(not isinstance(item, str) or not item or "\x00" in item
                       for item in argv)):
            raise OwnerAuthorityError("argv must be a non-empty string sequence")
        values = list(argv)
        argv_hash = hashlib.sha256(_canonical_json_bytes(values)).hexdigest()
        marker = self.marker
        if (marker["state"] not in {"active", "recovering"}
                or marker["child_state"] != "launching"
                or marker["child_generation"] != child_generation
                or marker["argv_hash"] != argv_hash):
            raise OwnerBusy("prepared child does not match durable launching authority")
        forbidden = {
            "args", "executable", "preexec_fn", "pass_fds",
            "start_new_session", "process_group", "creationflags", "shell",
        }
        supplied_forbidden = sorted(forbidden.intersection(popen_kwargs))
        if supplied_forbidden:
            raise OwnerAuthorityError(
                "controlled spawn owns Popen fields:"
                + ",".join(supplied_forbidden))
        if popen_kwargs.get("close_fds") is False and not compat.IS_WINDOWS:
            raise OwnerAuthorityError("controlled POSIX spawn requires close_fds")

        process = None
        controlled = None
        identity = None
        barrier_read = barrier_write = None
        control_read = control_write = None
        status_read = status_write = None
        try:
            if compat.IS_WINDOWS:
                flags = 0x00000004 | getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                process = subprocess.Popen(
                    values, creationflags=flags, shell=False, **popen_kwargs)
                if not compat.attach_process_group(
                        process, allow_breakaway=False):
                    raise OwnerInvariantError(
                        "cannot attach suspended child to strict Job")
                if not compat.verify_process_group_containment(
                        process, allow_breakaway=False):
                    raise OwnerInvariantError(
                        "suspended child strict Job membership is unproven")
                containment_kind = _WINDOWS_STRICT_JOB_KIND
                containment_id = (
                    f"strict-job:{self.session}:{self.generation}:"
                    f"{child_generation}")
                identity = child_process_identity(
                    process.pid, containment_kind=containment_kind,
                    containment_id=containment_id)
                controlled = ControlledOwnerChild(
                    fence=self, child_generation=child_generation,
                    process=process, argv=values,
                    containment_kind=containment_kind,
                    containment_id=containment_id)
                self._retain_controlled_child(controlled)
                self.publish_child_running(child_generation, identity)
                _windows_resume_suspended_primary_thread(process.pid)
            else:
                barrier_read, barrier_write = os.pipe()
                control_read, control_write = os.pipe()
                status_read, status_write = os.pipe()
                inherited = (barrier_read, control_read, status_write)
                for fd in inherited:
                    os.set_inheritable(fd, True)
                guardian = [
                    sys.executable, "-c", _POSIX_OWNER_GUARDIAN,
                    str(barrier_read), str(control_read), str(status_write),
                    *values,
                ]
                process = subprocess.Popen(
                    guardian, pass_fds=inherited, start_new_session=True,
                    shell=False, **popen_kwargs)
                for fd in inherited:
                    os.close(fd)
                barrier_read = control_read = status_write = None
                startup = _read_pipe_exact(status_read, 5, 5.0)
                if (len(startup) != 5 or startup[:1] != b"G"
                        or struct.unpack("!i", startup[1:])[0] != 0):
                    code = (struct.unpack("!i", startup[1:])[0]
                            if len(startup) == 5 else None)
                    raise OwnerInvariantError(
                        "POSIX subreaper guardian did not prove readiness"
                        + (f" (code {code})" if code is not None else ""))
                if (not compat.attach_process_group(process)
                        or not compat.verify_process_group_containment(process)):
                    raise OwnerInvariantError(
                        "POSIX guardian identity is not an exact live root")
                containment_kind = _POSIX_GUARDIAN_KIND
                containment_id = str(process.pid)
                identity = child_process_identity(
                    process.pid, containment_kind=containment_kind,
                    containment_id=containment_id)
                controlled = ControlledOwnerChild(
                    fence=self, child_generation=child_generation,
                    process=process, argv=values,
                    containment_kind=containment_kind,
                    containment_id=containment_id,
                    control_write=control_write, status_read=status_read)
                control_write = status_read = None
                self._retain_controlled_child(controlled)
                self.publish_child_running(child_generation, identity)
                if os.write(barrier_write, b"R") != 1:
                    raise OwnerInvariantError("cannot release POSIX payload barrier")
                os.close(barrier_write)
                barrier_write = None
            return controlled
        except BaseException:
            for fd in (
                    barrier_read, barrier_write, control_read, control_write,
                    status_read, status_write):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            if controlled is not None:
                # Once the exact Job/guardian handle exists, retain it across
                # every catchable failure.  Best-effort quiescence here keeps
                # the payload safe; the top-level owner finalizer retries the
                # same proof before it may publish a terminal marker.
                try:
                    controlled.kill_containment()
                    if self.marker["child_state"] == "child_running":
                        controlled.record_result(containment_timeout=5.0)
                except BaseException:
                    pass
            elif process is not None:
                try:
                    if compat.IS_WINDOWS:
                        if getattr(process, "_loop_job_handle", None):
                            _windows_terminate_job(process)
                        elif process.poll() is None:
                            process.kill()
                    else:
                        try:
                            process.wait(timeout=0.5)
                        except subprocess.TimeoutExpired:
                            if identity is not None:
                                compat.fence_process_tree(
                                    process,
                                    start_token=getattr(
                                        process, "_loop_start_token", None),
                                    group_id=getattr(
                                        process, "_loop_group_id", None),
                                    graceful_timeout=0.1,
                                    force_timeout=3.0)
                            else:
                                process.kill()
                            process.wait(timeout=5)
                    process.wait(timeout=5)
                except (OSError, OwnerInvariantError, subprocess.TimeoutExpired):
                    pass
                finally:
                    if compat.IS_WINDOWS and getattr(
                            process, "_loop_job_handle", None):
                        compat.close_process_group(process)
            # A retained handle can prove a published child is reaped.  A
            # failure in the identity-publication gap remains ``launching``
            # and deliberately requires explicit recovery.
            raise

    def publish_child_running(
            self, child_generation: int, identity: Mapping) -> dict:
        """CAS ``launching`` to the durable contained process identity."""
        _require_positive_int(child_generation, "child_generation")
        normalized = dict(identity)
        try:
            _validate_child_identity(normalized)
        except OwnerInvariantError as exc:
            raise OwnerAuthorityError(str(exc)) from exc

        def transition(marker: dict) -> None:
            if (marker["state"] not in {"active", "recovering"}
                    or marker["child_state"] != "launching"
                    or marker["child_generation"] != child_generation):
                raise OwnerBusy("child_running CAS does not match launching child")
            marker.update({
                "child_state": "child_running",
                "child_identity": _json_clone(normalized),
                "updated_at": _now(),
            })

        return self._transition(transition)

    def record_child_result(self, child_generation: int, returncode: int) -> dict:
        """Record a result only after the caller has reaped all descendants."""
        _require_positive_int(child_generation, "child_generation")
        if not isinstance(returncode, int) or isinstance(returncode, bool):
            raise OwnerAuthorityError("returncode must be an integer")

        def transition(marker: dict) -> None:
            if (marker["state"] not in {"active", "recovering"}
                    or marker["child_state"] != "child_running"
                    or marker["child_generation"] != child_generation):
                raise OwnerBusy("child result CAS does not match running child")
            marker.update({
                "child_state": "child_reaped",
                "child_result": {
                    "status": "exited",
                    "returncode": returncode,
                    "recorded_at": _now(),
                    "proof": "waited-and-reaped",
                },
                "updated_at": _now(),
            })

        return self._transition(transition)

    def checkpoint_child(self, child_generation: int) -> dict:
        """Return a reaped child to idle after caller state is checkpointed."""
        _require_positive_int(child_generation, "child_generation")

        def transition(marker: dict) -> None:
            if (marker["state"] not in {"active", "recovering"}
                    or marker["child_state"] != "child_reaped"
                    or marker["child_generation"] != child_generation):
                raise OwnerBusy("child checkpoint CAS does not match reaped child")
            marker.update({
                "child_state": "idle",
                "child_kind": None,
                "argv_hash": None,
                "child_identity": None,
                "child_result": None,
                "updated_at": _now(),
            })

        marker = self._transition(transition)
        with self._active_child_lock:
            if (self._active_child is not None
                    and self._active_child.child_generation == child_generation):
                self._active_child = None
        return marker

    def quiesce_active_child(self, containment_timeout: float = 5.0) -> dict:
        """Fence a retained child before a catchable owner exit.

        A caller can receive ``KeyboardInterrupt`` at any bytecode after a
        controlled spawn, including between ``communicate()`` and its normal
        result checkpoint.  The fence therefore retains the exact Job or
        guardian handle itself.  Only that handle may convert ``child_running``
        to ``child_reaped``; a launching/unknown child remains fail-closed for
        explicit recovery.
        """
        marker = self.marker
        if marker["child_state"] in {"idle", "child_reaped"}:
            return marker
        with self._active_child_lock:
            child = self._active_child
        if (child is None
                or child.child_generation != marker["child_generation"]):
            raise OwnerBusy(
                "owner has no retained handle for its nonterminal child")
        child.kill_containment(timeout=containment_timeout)
        if marker["child_state"] != "child_running":
            raise OwnerBusy(
                "launching child was fenced but lacks durable running identity")
        return child.record_result(containment_timeout=containment_timeout)

    def terminalize(self, reason: str) -> dict:
        """Durably terminalize a quiesced owner, then release its global lock."""
        try:
            _require_nonempty_string(reason, "terminal reason")
        except OwnerInvariantError as exc:
            raise OwnerAuthorityError(str(exc)) from exc

        def transition(marker: dict) -> None:
            if marker["state"] not in {"active", "recovering"}:
                raise OwnerBusy("owner is already terminal")
            if marker["child_state"] not in {"idle", "child_reaped"}:
                raise OwnerBusy("owner cannot terminalize before child is reaped")
            marker.update({
                "state": "terminal",
                "terminal_reason": reason,
                "updated_at": _now(),
            })

        marker = self._transition(transition)
        with self._active_child_lock:
            self._active_child = None
        self.close()
        return marker

    def close(self) -> None:
        """Release only the OS lock; never forge a terminal durable marker."""
        if self._global_lock_file is not None:
            try:
                compat.unlock_file(self._global_lock_file)
            finally:
                self._global_lock_file.close()
                self._global_lock_file = None
        self._closed = True

    def __enter__(self) -> "RepoOwnerFence":
        return self

    def __exit__(self, _kind, _value, _traceback) -> None:
        self.close()

    def _acquire_global(self) -> None:
        if self._closed or self._global_lock_file is not None:
            raise OwnerAuthorityError("owner fence global lock lifecycle is invalid")
        stream = _open_regular_lock(self.global_lock_path)
        try:
            compat.lock_file(stream, blocking=False)
        except (BlockingIOError, PermissionError) as exc:
            stream.close()
            raise OwnerBusy("primary Git global run lock is held") from exc
        except OSError as exc:
            stream.close()
            raise OwnerInvariantError("primary Git global run lock failed") from exc
        self._global_lock_file = stream

    def _audit_executor_lease(self) -> None:
        if self._global_lock_file is None:
            raise OwnerAuthorityError("RepoExecutor audit requires held global lock")
        lease_path = self.executor_sidecar / EXECUTOR_LEASE_NAME
        if os.path.lexists(self.executor_sidecar):
            _reject_link_components(self.executor_sidecar)
            if not self.executor_sidecar.is_dir():
                raise OwnerInvariantError("RepoExecutor sidecar is not a directory")
        if not os.path.lexists(lease_path):
            return
        with _short_lock(self.executor_sidecar / EXECUTOR_LOCK_NAME):
            if not os.path.lexists(lease_path):
                raise OwnerInvariantError("RepoExecutor lease disappeared during audit")
            lease = _validate_executor_lease(
                _read_json(lease_path, "RepoExecutor operation lease"))
            if lease["state"] != "terminal":
                raise OwnerBusy(
                    "RepoExecutor has a nonterminal operation lease:"
                    f"{lease['operation']}/{lease['operation_id']}")
            _audit_executor_terminal_result(self.executor_sidecar, lease)

    def _assert_repository_marker(self, marker: dict) -> None:
        if (marker["canonical_repo"] != str(self.canonical_repo)
                or marker["common_dir"] != str(self.common_dir)):
            raise OwnerInvariantError(
                "owner marker canonical repository identity does not match")

    def _assert_recovery_expectation(
            self, marker: dict, *, expected_session: str,
            expected_generation: int) -> None:
        if marker["state"] not in {"active", "recovering"}:
            raise OwnerRecoveryRequired("only a nonterminal marker can be recovered")
        expected = {
            "owner_kind": self.owner_kind.value,
            "workspace": str(self.workspace),
            "state_path": str(self.state_path),
            "session": expected_session,
            "generation": expected_generation,
        }
        actual = {field: marker[field] for field in expected}
        if actual != expected:
            raise OwnerRecoveryRequired(
                "recorded owner kind/workspace/state/session/generation mismatch")

    def _retain_controlled_child(self, child: ControlledOwnerChild) -> None:
        """Publish the in-process proof handle before payload release."""
        with self._active_child_lock:
            if self._active_child is not None:
                raise OwnerBusy("a controlled child handle is already retained")
            self._active_child = child

    def _transition(self, transition: Callable[[dict], None]) -> dict:
        if self._closed or self._global_lock_file is None or self._marker is None:
            raise OwnerAuthorityError("owner fence no longer holds the global lock")
        # On Windows, SIGBREAK normally raises KeyboardInterrupt at an arbitrary
        # bytecode.  Defer it across this complete read/CAS/write section so it
        # cannot strand a just-opened marker fd that blocks the retrying replace.
        with compat.defer_windows_break():
            with _short_lock(self.marker_lock_path):
                if not os.path.lexists(self.marker_path):
                    raise OwnerBusy("owner marker disappeared")
                current = _validate_marker(_read_json(self.marker_path, "owner marker"))
                if current != self._marker:
                    raise OwnerBusy("owner marker changed before generation CAS")
                if (current["session"] != self.session
                        or current["generation"] != self._marker["generation"]
                        or current["owner_identity"] != self.owner_identity):
                    raise OwnerBusy("owner marker is owned by a different identity")
                updated = copy.deepcopy(current)
                transition(updated)
                _validate_marker(updated)
                try:
                    _atomic_json(self.marker_path, updated)
                    self._marker = updated
                except BaseException:
                    # An async exception can land after os.replace committed but
                    # before the in-memory CAS snapshot advanced.  Adopt only the
                    # byte-for-byte intended marker while still under the short
                    # lock; every other observation remains a hard failure.
                    try:
                        observed = _validate_marker(
                            _read_json(self.marker_path, "owner marker"))
                    except RepoOwnerError:
                        observed = None
                    if observed == updated:
                        self._marker = updated
                    raise
                return _json_clone(updated)


__all__ = [
    "ChildKind", "ControlledOwnerChild", "EXECUTOR_LEASE_NAME",
    "EXECUTOR_SIDECAR_NAME", "GLOBAL_LOCK_NAME", "MARKER_NAME",
    "OwnerAuthorityError", "OwnerBusy", "OwnerInvariantError", "OwnerKind",
    "OwnerRecoveryRequired", "RepoOwnerError", "RepoOwnerFence",
    "audit_owner_marker_under_global_lock", "child_process_identity",
    "current_owner_identity", "host_boot_identity", "process_creation_token",
]
