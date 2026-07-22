"""Closed, mechanically fenced Git operations for Parallel Loop.

The executor deliberately exposes one request entrypoint and a six-value
operation enum.  Repositories, refs, worktree paths, and the one permitted
validator argv are all derived from an immutable :class:`ImmutableRepoSpec`;
requests cannot provide a path, ref, or shell command.

This module is intentionally the supervisor's in-process mechanical core.
Every mutating operation crosses the durable common-dir lease
(``reserved -> running -> terminal``), and every external Git/validator child
crosses its own contained, durable child lifecycle.  If the supervisor dies,
a replacement never adopts this instance; it fences the recorded child and
replays the exact request through the lease recovery contract.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import select
import stat
import struct
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

from engine import parallel_contract
from engine import parallel_spool
from engine import parallel_state
from engine import platform_compat as compat
from engine import repo_owner


HEX32_RE = re.compile(r"[0-9a-f]{32}")
HASH64_RE = re.compile(r"[0-9a-f]{64}")
WORKSPACE_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
PRIMARY_REF_RE = re.compile(r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*")


# The POSIX operation child is a tiny trusted guardian rather than the payload
# itself.  It cannot start the payload until the executor has durably published
# the guardian's PID/birth-token/process-group identity and releases the ACK
# barrier.  A second pipe is a parent-death lease: EOF recursively fences the
# payload before the guardian exits.  Linux subreaper mode keeps daemonised
# grandchildren below the guardian after their immediate parent exits; the
# process-group scan is the portable fallback for ordinary descendants.
_POSIX_OPERATION_GUARDIAN = r"""
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

def finish(returncode):
    os.write(status_fd, b'D' + struct.pack('!i', int(returncode)))
    os.close(status_fd)
    ack = os.read(control_fd, 1)
    if ack != b'A':
        # Parent death before the durable reaped checkpoint must leave an exact
        # live root for the recovery owner to fence.  The payload tree is
        # already empty; this guardian is quiescent and holds no repository fd.
        while True:
            signal.pause()
    os.close(control_fd)
    raise SystemExit(int(returncode))

def fail_completion(returncode=126):
    os.write(status_fd, b'F' + struct.pack('!i', int(returncode)))
    os.close(status_fd)
    while True:
        signal.pause()

def write_status(kind, returncode):
    try:
        return os.write(
            status_fd, kind + struct.pack('!i', int(returncode))) == 5
    except OSError:
        return False

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
    # Cover the narrow adoption interval before a newly orphaned process is
    # visibly reparented to this verified subreaper.
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
    fail_completion(126)
if not write_status(b'G', 0):
    raise SystemExit(125)

try:
    ack = os.read(barrier_fd, 1)
finally:
    os.close(barrier_fd)
if ack != b'R':
    raise SystemExit(125)

try:
    payload = subprocess.Popen(payload_argv, close_fds=True)
except OSError as exc:
    sys.stderr.write('operation payload launch failed: ' + str(exc) + '\n')
    finish(127)
parent_gone = False
while True:
    returncode = payload.poll()
    if returncode is not None:
        break
    readable, _, _ = select.select([control_fd], [], [], 0.05)
    if readable and os.read(control_fd, 1) in {b'', b'X'}:
        parent_gone = True
        fenced = fence_payload()
        try:
            payload.wait(timeout=3)
        except subprocess.TimeoutExpired:
            fenced = fence_payload() and fenced
        if not fenced:
            fail_completion()
        returncode = 125
        break

if not fence_payload():
    fail_completion()
reap_children()
finish(125 if parent_gone else int(returncode))
"""


class Operation(str, Enum):
    PREFLIGHT = "PREFLIGHT"
    INITIALIZE_RUN_REFS = "INITIALIZE_RUN_REFS"
    CREATE_WORKTREE = "CREATE_WORKTREE"
    GATE_MERGE = "GATE_MERGE"
    REMOVE_WORKTREE = "REMOVE_WORKTREE"
    SHUTDOWN = "SHUTDOWN"


class RepoExecutorError(RuntimeError):
    """Base class for a fail-closed executor result."""


class AuthorityError(RepoExecutorError):
    """The request is not exactly bound to the immutable authority."""


class InvariantError(RepoExecutorError):
    """Repository or durable journal state cannot be safely explained."""


class LeaseBusy(RepoExecutorError):
    """A different nonterminal common-dir operation is still authoritative."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_json_bytes(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_hash(value) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def gate_operation_id(run_id: str, request_id: str) -> str:
    """Return the supervisor/executor canonical operation id for one gate."""
    try:
        canonical_run_id = parallel_contract.require_run_id(run_id)
    except parallel_contract.ParallelContractError as exc:
        raise AuthorityError(str(exc)) from exc
    canonical_request_id = _require_hex(
        request_id, HEX32_RE, "request_id")
    return parallel_state.canonical_json_hash({
        "run_id": canonical_run_id,
        "operation": "gate",
        "identity": [canonical_request_id],
    })[:32]


def _reject_json_constant(value: str):
    raise ValueError(f"non-finite JSON number:{value}")


def _exact_dict(value, fields, label: str) -> dict:
    if not isinstance(value, dict) or set(value) != set(fields):
        actual = sorted(value) if isinstance(value, dict) else type(value).__name__
        raise AuthorityError(f"{label} schema 不符；預期 {sorted(fields)}，收到 {actual}")
    return value


def _require_hex(value, pattern, label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise AuthorityError(f"{label} 格式不合法")
    return value


def _require_positive_int(value, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise AuthorityError(f"{label} 必須是正整數")
    return value


def _require_sha(value, label: str) -> str:
    try:
        return parallel_contract.require_git_sha(value, label)
    except parallel_contract.ParallelContractError as exc:
        raise AuthorityError(str(exc)) from exc


@dataclass(frozen=True)
class AssignmentAuthority:
    order: int
    assignment_hash: str
    run_config_hash: str
    launch_spec_hash: str

    def __post_init__(self):
        _require_positive_int(self.order, "assignment.order")
        for field in ("assignment_hash", "run_config_hash", "launch_spec_hash"):
            _require_hex(getattr(self, field), HASH64_RE, field)

    @classmethod
    def from_dict(cls, order, payload: dict) -> "AssignmentAuthority":
        _exact_dict(
            payload,
            {"assignment_hash", "run_config_hash", "launch_spec_hash"},
            f"assignments[{order}]",
        )
        try:
            parsed_order = int(order)
        except (TypeError, ValueError) as exc:
            raise AuthorityError("assignment key 必須是正整數") from exc
        if isinstance(order, bool) or str(parsed_order) != str(order):
            raise AuthorityError("assignment key 必須是 canonical 正整數")
        return cls(order=parsed_order, **payload)

    def as_dict(self) -> dict:
        return {
            "assignment_hash": self.assignment_hash,
            "run_config_hash": self.run_config_hash,
            "launch_spec_hash": self.launch_spec_hash,
        }


@dataclass(frozen=True)
class ImmutableRepoSpec:
    primary_repo: Path
    workspace_root: Path
    parent_workspace: str
    run_id: str
    pending_launch_hash: str
    manifest_hash: str
    primary_ref: str
    integration_start_sha: str
    validator_argv: tuple[str, ...]
    validator_timeout: float
    supervisor_session: str
    generation: int
    assignments: Mapping[int, AssignmentAuthority]

    FIELDS = frozenset({
        "primary_repo", "workspace_root", "parent_workspace", "run_id",
        "pending_launch_hash", "manifest_hash", "primary_ref",
        "integration_start_sha", "validator_argv", "validator_timeout",
        "supervisor_session", "generation", "assignments",
    })

    def __post_init__(self):
        if (not isinstance(self.parent_workspace, str)
                or self.parent_workspace.startswith(".")
                or WORKSPACE_NAME_RE.fullmatch(self.parent_workspace) is None):
            raise AuthorityError("parent_workspace 不合法")
        try:
            parallel_contract.require_run_id(self.run_id)
        except parallel_contract.ParallelContractError as exc:
            raise AuthorityError(str(exc)) from exc
        _require_hex(self.pending_launch_hash, HASH64_RE, "pending_launch_hash")
        _require_hex(self.manifest_hash, HASH64_RE, "manifest_hash")
        _require_sha(self.integration_start_sha, "integration_start_sha")
        _require_hex(self.supervisor_session, HEX32_RE, "supervisor_session")
        _require_positive_int(self.generation, "generation")
        if (not isinstance(self.primary_ref, str)
                or PRIMARY_REF_RE.fullmatch(self.primary_ref) is None
                or ".." in self.primary_ref or "@{" in self.primary_ref
                or self.primary_ref.endswith(".lock")):
            raise AuthorityError("primary_ref 必須是 canonical refs/heads ref")
        if (not isinstance(self.validator_argv, tuple) or not self.validator_argv
                or any(not isinstance(item, str) or not item or "\x00" in item
                       for item in self.validator_argv)):
            raise AuthorityError("validator_argv 必須是非空 immutable argv")
        if (not isinstance(self.validator_timeout, (int, float))
                or isinstance(self.validator_timeout, bool)
                or not math.isfinite(float(self.validator_timeout))
                or self.validator_timeout <= 0):
            raise AuthorityError("validator_timeout 必須 > 0")
        if not isinstance(self.assignments, Mapping) or not self.assignments:
            raise AuthorityError("assignments 必須是非空 mapping")
        normalized = {}
        for order, assignment in self.assignments.items():
            if (not isinstance(order, int) or isinstance(order, bool)
                    or not isinstance(assignment, AssignmentAuthority)
                    or assignment.order != order):
                raise AuthorityError("assignments key/order 不一致")
            normalized[order] = assignment
        object.__setattr__(self, "primary_repo", Path(self.primary_repo))
        object.__setattr__(self, "workspace_root", Path(self.workspace_root))
        object.__setattr__(self, "validator_timeout", float(self.validator_timeout))
        object.__setattr__(self, "assignments", MappingProxyType(normalized))

    @classmethod
    def from_dict(cls, payload: dict) -> "ImmutableRepoSpec":
        _exact_dict(payload, cls.FIELDS, "immutable repo spec")
        assignments_payload = payload["assignments"]
        if not isinstance(assignments_payload, dict):
            raise AuthorityError("assignments 必須是 object")
        assignments = {}
        for order, value in assignments_payload.items():
            assignment = AssignmentAuthority.from_dict(order, value)
            if assignment.order in assignments:
                raise AuthorityError("assignments contain duplicate canonical task orders")
            assignments[assignment.order] = assignment
        validator_argv = payload["validator_argv"]
        if not isinstance(validator_argv, list):
            raise AuthorityError("validator_argv artifact 必須是 array")
        values = dict(payload)
        values["primary_repo"] = Path(values["primary_repo"])
        values["workspace_root"] = Path(values["workspace_root"])
        values["validator_argv"] = tuple(validator_argv)
        values["assignments"] = assignments
        return cls(**values)

    def hash_material(self) -> dict:
        return {
            "primary_repo": str(self.primary_repo.resolve()),
            "workspace_root": str(self.workspace_root.resolve()),
            "parent_workspace": self.parent_workspace,
            "run_id": self.run_id,
            "pending_launch_hash": self.pending_launch_hash,
            "manifest_hash": self.manifest_hash,
            "primary_ref": self.primary_ref,
            "integration_start_sha": self.integration_start_sha,
            "validator_argv": list(self.validator_argv),
            "validator_timeout": self.validator_timeout,
            "supervisor_session": self.supervisor_session,
            "generation": self.generation,
            "assignments": {
                str(order): assignment.as_dict()
                for order, assignment in sorted(self.assignments.items())
            },
        }

    @property
    def spec_hash(self) -> str:
        return canonical_hash(self.hash_material())

    @property
    def authority_hash(self) -> str:
        """Hash immutable repository authority, excluding owner generation."""
        material = self.hash_material()
        material.pop("supervisor_session")
        material.pop("generation")
        return canonical_hash(material)


class RepoExecutor:
    """In-process closed operation core with a durable common-dir fence."""

    def __init__(self, spec: ImmutableRepoSpec | dict, *,
                 fault_injector: Callable[[str], None] | None = None,
        recovery_authorizer: Callable[[dict], bool] | None = None):
        self.spec = (spec if isinstance(spec, ImmutableRepoSpec)
                     else ImmutableRepoSpec.from_dict(spec))
        primary_input = self._absolute_path(self.spec.primary_repo, "primary_repo")
        workspace_input = self._absolute_path(
            self.spec.workspace_root, "workspace_root")
        self._reject_link_components(primary_input)
        self._reject_link_components(workspace_input, allow_missing=True)
        self.primary_repo = self._canonical_existing_dir(
            primary_input, "primary_repo")
        self.workspace_root = self._canonical_path(
            workspace_input, "workspace_root")
        self._reject_link_components(self.primary_repo)
        self._reject_link_components(self.workspace_root, allow_missing=True)
        authority_material = self.spec.hash_material()
        authority_material["primary_repo"] = str(self.primary_repo)
        authority_material["workspace_root"] = str(self.workspace_root)
        authority_material.pop("supervisor_session")
        authority_material.pop("generation")
        self._authority_hash = canonical_hash(authority_material)
        self.common_dir = self._resolve_common_dir()
        self.sidecar_root = self.common_dir / "loop-agent-lite.repo-executor"
        self.operation_lock_path = self.sidecar_root / "operation-fence.lock"
        self.merge_lock_path = self.sidecar_root / "merge.lock"
        self.lease_path = self.sidecar_root / "operation-lease.json"
        self.results_dir = self.sidecar_root / "operation-results"
        self.intents_dir = self.sidecar_root / "intents"
        self.receipts_dir = self.sidecar_root / "receipts"
        self.empty_hooks_dir = self.sidecar_root / "empty-hooks"
        self._global_lock_path = self.common_dir / "loop-agent-lite.run.lock"
        self._global_lock_file = None
        self._started = False
        self._closed = False
        self._session = uuid.uuid4().hex
        self._executor_creation_token = compat.process_start_token(os.getpid())
        if self._executor_creation_token is None:
            raise InvariantError("executor process creation token is unavailable")
        self._active_operation_id = None
        self._active_request_hash = None
        if fault_injector is not None and not callable(fault_injector):
            raise AuthorityError("fault_injector must be callable")
        self._fault_injector = fault_injector
        if recovery_authorizer is not None and not callable(recovery_authorizer):
            raise AuthorityError("recovery_authorizer must be callable")
        self._recovery_authorizer = recovery_authorizer
        ref_check = subprocess.run(
            ["git", "-C", str(self.primary_repo), "check-ref-format", self.spec.primary_ref],
            capture_output=True, text=True, check=False, shell=False,
        )
        if ref_check.returncode:
            raise AuthorityError("primary_ref 未通過 git check-ref-format")

    @staticmethod
    def _absolute_path(path: Path, label: str) -> Path:
        try:
            return Path(os.path.abspath(os.fspath(path)))
        except (OSError, TypeError, ValueError) as exc:
            raise AuthorityError(f"{label} 無法轉成 absolute path:{exc}") from exc

    @staticmethod
    def _canonical_existing_dir(path: Path, label: str) -> Path:
        try:
            resolved = Path(path).resolve(strict=True)
        except OSError as exc:
            raise AuthorityError(f"{label} 無法 canonicalize:{exc}") from exc
        if not resolved.is_dir():
            raise AuthorityError(f"{label} 必須是目錄")
        return resolved

    @staticmethod
    def _canonical_path(path: Path, label: str) -> Path:
        try:
            return Path(path).resolve(strict=False)
        except OSError as exc:
            raise AuthorityError(f"{label} 無法 canonicalize:{exc}") from exc

    @staticmethod
    def _reject_link_components(path: Path, *, allow_missing=False) -> None:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                if allow_missing:
                    continue
                raise AuthorityError(f"path component 不存在:{current}")
            if current.is_symlink() or compat.is_reparse_point(info):
                raise AuthorityError(f"path component 不可為 link/reparse:{current}")

    def _resolve_common_dir(self) -> Path:
        env = dict(os.environ)
        env.update({
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
        })
        env.pop("GIT_EXTERNAL_DIFF", None)
        result = subprocess.run(
            ["git", "--no-pager", "-c", "core.fsmonitor=false",
             "-C", str(self.primary_repo), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, check=False,
            env=env,
        )
        if result.returncode:
            raise AuthorityError(f"primary_repo 不是可用 Git repo:{result.stderr.strip()}")
        raw = Path(result.stdout.strip())
        common = (raw if raw.is_absolute() else self.primary_repo / raw).resolve(strict=True)
        if not common.is_dir():
            raise AuthorityError("Git common-dir 不是目錄")
        self._reject_link_components(common)
        return common

    @property
    def authority_hash(self) -> str:
        return self._authority_hash

    @property
    def sync_ref(self) -> str:
        return parallel_contract.integration_ref_for(self.spec.run_id)

    @property
    def run_dir(self) -> Path:
        try:
            return parallel_state.derive_run_directory(
                self.workspace_root, self.spec.parent_workspace, self.spec.run_id)
        except parallel_state.ParallelStateError as exc:
            raise AuthorityError(f"parallel run directory 不合法:{exc}") from exc

    def task_ref(self, task: int) -> str:
        self._assignment(task)
        return f"refs/heads/loop/{self.spec.run_id}/task-{task}"

    def worktree_path(self, task: int) -> Path:
        self._assignment(task)
        lexical = (self.workspace_root / self.spec.parent_workspace / "worktrees"
                   / f"{self.spec.run_id}-task-{task}")
        self._reject_link_components(lexical.parent, allow_missing=True)
        target = lexical.resolve(strict=False)
        if target != lexical:
            raise AuthorityError("derived worktree path traverses a link/reparse")
        self._assert_contained(target, self.workspace_root, "worktree workspace root")
        if self._is_contained(target, self.primary_repo):
            raise AuthorityError("derived worktree 不可位於 target repo 內")
        return target

    @staticmethod
    def _is_contained(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @classmethod
    def _assert_contained(cls, path: Path, root: Path, label: str) -> None:
        if not cls._is_contained(path, root):
            raise AuthorityError(f"derived path 逸出 {label}")

    def _assignment(self, task: int) -> AssignmentAuthority:
        _require_positive_int(task, "task")
        try:
            return self.spec.assignments[task]
        except KeyError as exc:
            raise AuthorityError(f"task-{task} 不在 immutable assignments") from exc

    def __enter__(self):
        return self

    def __exit__(self, _kind, _value, _traceback):
        self.close()

    def close(self) -> None:
        if self._global_lock_file is not None:
            stream = self._global_lock_file
            self._global_lock_file = None
            try:
                compat.unlock_file(stream)
            finally:
                stream.close()
        self._closed = True

    @staticmethod
    def _release_lock_stream(stream) -> None:
        try:
            compat.unlock_file(stream)
        finally:
            stream.close()

    def _open_regular_lock(self, path: Path):
        """Open one fixed lock path without accepting a link/reparse target."""
        self._reject_link_components(path.parent)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise AuthorityError(f"cannot safely open lock file:{path}") from exc
        try:
            handle_info = os.fstat(fd)
            path_info = path.lstat()
            if (not stat.S_ISREG(handle_info.st_mode)
                    or not stat.S_ISREG(path_info.st_mode)
                    or path.is_symlink()
                    or compat.is_reparse_point(path_info)
                    or (handle_info.st_dev, handle_info.st_ino)
                    != (path_info.st_dev, path_info.st_ino)):
                raise AuthorityError(f"lock path is not the opened regular file:{path}")
            return os.fdopen(fd, "r+b", closefd=True)
        except OSError as exc:
            os.close(fd)
            raise AuthorityError(f"cannot verify regular lock file:{path}") from exc
        except BaseException:
            os.close(fd)
            raise

    def _start(self) -> None:
        if self._closed:
            raise RepoExecutorError("RepoExecutor 已關閉")
        if self._started:
            return
        lock_file = self._open_regular_lock(self._global_lock_path)
        try:
            compat.lock_file(lock_file, blocking=False)
        except BlockingIOError as exc:
            lock_file.close()
            raise LeaseBusy("primary Git global run lock 已被占用") from exc
        except OSError as exc:
            lock_file.close()
            raise InvariantError("primary Git global run lock failed") from exc
        try:
            repo_owner.audit_owner_marker_under_global_lock(
                self.primary_repo, lock_file)
        except repo_owner.OwnerRecoveryRequired as exc:
            self._release_lock_stream(lock_file)
            self._started = False
            raise LeaseBusy(str(exc)) from exc
        except repo_owner.RepoOwnerError as exc:
            self._release_lock_stream(lock_file)
            self._started = False
            raise InvariantError(f"ordinary owner marker audit failed:{exc}") from exc
        except Exception as exc:
            self._release_lock_stream(lock_file)
            self._started = False
            raise InvariantError("ordinary owner marker audit failed closed") from exc
        try:
            self.sidecar_root.mkdir(mode=0o700, parents=False, exist_ok=True)
            self._reject_link_components(self.sidecar_root)
            for directory in (self.results_dir, self.intents_dir, self.receipts_dir,
                              self.empty_hooks_dir):
                directory.mkdir(mode=0o700, exist_ok=True)
                self._reject_link_components(directory)
        except Exception as exc:
            self._release_lock_stream(lock_file)
            self._global_lock_file = None
            self._started = False
            raise AuthorityError(
                "cannot establish canonical executor sidecar") from exc
        self._global_lock_file = lock_file
        self._started = True

    @contextmanager
    def _short_lock(self, path: Path):
        stream = self._open_regular_lock(path)
        try:
            compat.lock_file(stream, blocking=True)
        except OSError as exc:
            stream.close()
            raise InvariantError(f"short lock failed:{path.name}") from exc
        try:
            yield
        finally:
            compat.unlock_file(stream)
            stream.close()

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    @classmethod
    def _atomic_json(cls, path: Path, payload: dict) -> None:
        fd = None
        temporary_path = None
        try:
            cls._reject_link_components(path.parent, allow_missing=True)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            cls._reject_link_components(path.parent)
            fd, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.", dir=path.parent)
            temporary_path = Path(temporary)
            stream = os.fdopen(fd, "wb")
            fd = None
            with stream:
                stream.write(canonical_json_bytes(payload))
                stream.flush()
                os.fsync(stream.fileno())
            cls._reject_link_components(path.parent)
            os.replace(temporary_path, path)
            cls._fsync_directory(path.parent)
        except RepoExecutorError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise InvariantError(f"atomic JSON write failed:{path.name}") from exc
        finally:
            if fd is not None:
                os.close(fd)
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _read_json(path: Path, label: str) -> dict:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise InvariantError(f"{label} 無法安全開啟:{exc}") from exc
        try:
            handle_info = os.fstat(fd)
            path_info = path.lstat()
            if (not stat.S_ISREG(handle_info.st_mode)
                    or not stat.S_ISREG(path_info.st_mode)
                    or compat.is_reparse_point(path_info)
                    or (handle_info.st_dev, handle_info.st_ino)
                    != (path_info.st_dev, path_info.st_ino)):
                raise InvariantError(f"{label} 必須是固定 regular file")
            stream = os.fdopen(fd, "rb", closefd=True)
            fd = None
            with stream:
                raw = stream.read()
            payload = json.loads(
                raw.decode("utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise InvariantError(f"{label} 無法讀取:{exc}") from exc
        finally:
            if fd is not None:
                os.close(fd)
        if not isinstance(payload, dict):
            raise InvariantError(f"{label} 必須是 object")
        return payload

    def _fault(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    def _validate_request(self, request: dict):
        if not isinstance(request, dict):
            raise AuthorityError("request 必須是 object")
        try:
            operation = Operation(request.get("operation"))
        except (TypeError, ValueError) as exc:
            raise AuthorityError("operation 不在 closed RepoExecutor enum") from exc
        task_operation = operation in {
            Operation.CREATE_WORKTREE, Operation.GATE_MERGE, Operation.REMOVE_WORKTREE,
        }
        top_fields = {"operation", "operation_id", "authority", "expected"}
        if task_operation:
            top_fields.add("task")
        _exact_dict(request, top_fields, "request")
        operation_id = _require_hex(request["operation_id"], HEX32_RE, "operation_id")
        task = request.get("task")
        assignment = self._assignment(task) if task_operation else None
        authority = request["authority"]
        expected = request["expected"]

        if operation == Operation.PREFLIGHT:
            _exact_dict(authority, {"pending_launch_hash"}, "PREFLIGHT authority")
            if authority["pending_launch_hash"] != self.spec.pending_launch_hash:
                raise AuthorityError("pending_launch_hash 不符")
            _exact_dict(expected, {"head_ref", "head_sha"}, "PREFLIGHT expected")
            if (expected["head_ref"] != self.spec.primary_ref
                    or expected["head_sha"] != self.spec.integration_start_sha):
                raise AuthorityError("PREFLIGHT expected state 不符 immutable spec")
        elif operation == Operation.INITIALIZE_RUN_REFS:
            _exact_dict(authority, {"manifest_hash"}, "INITIALIZE_RUN_REFS authority")
            self._require_manifest(authority)
            _exact_dict(
                expected, {"integration_start_sha", "sync_ref_absent"},
                "INITIALIZE_RUN_REFS expected",
            )
            if (expected["integration_start_sha"] != self.spec.integration_start_sha
                    or expected["sync_ref_absent"] is not True):
                raise AuthorityError("INITIALIZE_RUN_REFS expected state 不符")
        elif operation == Operation.CREATE_WORKTREE:
            _exact_dict(
                authority, {"manifest_hash", "assignment_hash"},
                "CREATE_WORKTREE authority",
            )
            self._require_assignment_authority(authority, assignment)
            _exact_dict(
                expected, {"base_sha", "task_ref_absent", "worktree_absent"},
                "CREATE_WORKTREE expected",
            )
            _require_sha(expected["base_sha"], "base_sha")
            if expected["task_ref_absent"] is not True or expected["worktree_absent"] is not True:
                raise AuthorityError("CREATE_WORKTREE 只接受 expected-absent CAS")
        elif operation == Operation.GATE_MERGE:
            _exact_dict(
                authority, {"manifest_hash", "assignment_hash", "request_hash"},
                "GATE_MERGE authority",
            )
            self._require_assignment_authority(authority, assignment)
            _require_hex(authority["request_hash"], HASH64_RE, "request_hash")
            _exact_dict(
                expected,
                {"request_id", "validated_sha", "validated_round",
                 "integration_before", "sync_before"},
                "GATE_MERGE expected",
            )
            _require_hex(expected["request_id"], HEX32_RE, "request_id")
            _require_sha(expected["validated_sha"], "validated_sha")
            _require_positive_int(expected["validated_round"], "validated_round")
            _require_sha(expected["integration_before"], "integration_before")
            _require_sha(expected["sync_before"], "sync_before")
        elif operation == Operation.REMOVE_WORKTREE:
            _exact_dict(
                authority, {"manifest_hash", "assignment_hash"},
                "REMOVE_WORKTREE authority",
            )
            self._require_assignment_authority(authority, assignment)
            _exact_dict(
                expected, {"terminal_outcome", "observation_token"},
                "REMOVE_WORKTREE expected",
            )
            if expected["terminal_outcome"] not in {"integrated", "blocked", "cancelled"}:
                raise AuthorityError("REMOVE_WORKTREE terminal_outcome 不合法")
            _require_hex(expected["observation_token"], HASH64_RE, "observation_token")
        else:
            _exact_dict(
                authority, {"supervisor_session", "generation"}, "SHUTDOWN authority",
            )
            _exact_dict(expected, {"idle"}, "SHUTDOWN expected")
            if (authority["supervisor_session"] != self.spec.supervisor_session
                    or authority["generation"] != self.spec.generation
                    or expected["idle"] is not True):
                raise AuthorityError("SHUTDOWN session/generation/idle 不符")
        return operation, operation_id, task, authority, expected

    def _require_manifest(self, authority: dict) -> None:
        if authority.get("manifest_hash") != self.spec.manifest_hash:
            raise AuthorityError("manifest_hash 不符")

    def _require_assignment_authority(
            self, authority: dict, assignment: AssignmentAuthority) -> None:
        self._require_manifest(authority)
        if authority.get("assignment_hash") != assignment.assignment_hash:
            raise AuthorityError("assignment_hash 不符")

    def _operation_result_path(self, operation_id: str) -> Path:
        return self.results_dir / f"{operation_id}.json"

    def _cached_result(self, operation_id: str, request_hash: str) -> dict | None:
        path = self._operation_result_path(operation_id)
        if not os.path.lexists(path):
            return None
        artifact = self._read_json(path, "operation result")
        _exact_dict(
            artifact, {
                "schema_version", "operation_id", "request_hash",
                "result", "result_hash",
            },
            "operation result",
        )
        if (artifact["schema_version"] != 1 or artifact["operation_id"] != operation_id
                or artifact["request_hash"] != request_hash
                or not isinstance(artifact["result"], dict)
                or artifact["result_hash"] != canonical_hash(artifact["result"])):
            raise InvariantError("operation_id 已有不相容 durable result")
        return artifact["result"]

    def _write_result(self, operation_id: str, request_hash: str, result: dict) -> None:
        artifact = {
            "schema_version": 1,
            "operation_id": operation_id,
            "request_hash": request_hash,
            "result": result,
            "result_hash": canonical_hash(result),
        }
        path = self._operation_result_path(operation_id)
        if os.path.lexists(path):
            if self._read_json(path, "immutable operation result") != artifact:
                raise InvariantError("operation result already has different bytes")
            return
        self._atomic_json(path, artifact)
        if self._read_json(path, "immutable operation result") != artifact:
            raise InvariantError("operation result byte verification failed")

    def _new_lease(self, operation: Operation, operation_id: str, request_hash: str,
                   expected: dict, request: dict) -> dict:
        return {
            "schema_version": 2,
            "state": "reserved",
            "operation": operation.value,
            "operation_id": operation_id,
            "request_hash": request_hash,
            "immutable_spec_hash": self.authority_hash,
            "nonce": uuid.uuid4().hex,
            "generation": self.spec.generation,
            "executor_session": self._session,
            "pid": os.getpid(),
            "executor_creation_token": self._executor_creation_token,
            "expected": expected,
            "request": json.loads(json.dumps(request, allow_nan=False)),
            "child_generation": 0,
            "child_state": "idle",
            "child_kind": None,
            "child_argv_hash": None,
            "child_identity": None,
            "child_result": None,
            "child_history": [],
            "updated_at": _now(),
            "terminal_status": None,
            "result_hash": None,
            "reason": None,
        }

    @staticmethod
    def _validate_child_identity(identity) -> None:
        _exact_dict(
            identity,
            {"pid", "start_token", "group_id", "containment_kind"},
            "operation child identity",
        )
        _require_positive_int(identity["pid"], "child.pid")
        _require_positive_int(identity["group_id"], "child.group_id")
        if (not isinstance(identity["start_token"], str)
                or not identity["start_token"]):
            raise InvariantError("operation child start token is invalid")
        if identity["containment_kind"] not in {
            "process-tree",
            "windows-job",
            "windows-job-no-breakaway-v2",
        }:
            raise InvariantError("operation child containment kind is invalid")

    @staticmethod
    def _validate_child_result(result, *, identity_present: bool) -> None:
        _exact_dict(
            result, {"status", "returncode", "recorded_at"},
            "operation child result")
        if result["status"] not in {"exited", "not-started"}:
            raise InvariantError("operation child result status is invalid")
        if result["status"] == "exited":
            if (not identity_present
                    or not isinstance(result["returncode"], int)
                    or isinstance(result["returncode"], bool)):
                raise InvariantError("exited operation child lacks exact evidence")
        elif identity_present or result["returncode"] is not None:
            raise InvariantError("not-started child contains process evidence")
        if not isinstance(result["recorded_at"], str) or not result["recorded_at"]:
            raise InvariantError("operation child result timestamp is invalid")

    @staticmethod
    def _validate_lease_shape(lease: dict) -> None:
        _exact_dict(lease, {
            "schema_version", "state", "operation", "operation_id", "request_hash",
            "immutable_spec_hash", "nonce", "generation", "executor_session", "pid",
            "executor_creation_token", "expected", "child_generation", "child_state",
            "child_kind", "child_argv_hash", "child_identity", "child_result",
            "child_history", "request",
            "updated_at", "terminal_status", "result_hash", "reason",
        }, "operation lease")
        if lease["schema_version"] != 2 or lease["state"] not in {
                "reserved", "running", "terminal"}:
            raise InvariantError("operation lease version/state 不合法")
        try:
            Operation(lease["operation"])
        except (TypeError, ValueError) as exc:
            raise InvariantError("operation lease operation 不合法") from exc
        _require_hex(lease["operation_id"], HEX32_RE, "lease.operation_id")
        _require_hex(lease["request_hash"], HASH64_RE, "lease.request_hash")
        _require_hex(lease["immutable_spec_hash"], HASH64_RE, "lease.immutable_spec_hash")
        _require_hex(lease["nonce"], HEX32_RE, "lease.nonce")
        _require_hex(lease["executor_session"], HEX32_RE, "lease.executor_session")
        _require_positive_int(lease["generation"], "lease.generation")
        _require_positive_int(lease["pid"], "lease.pid")
        if (not isinstance(lease["executor_creation_token"], str)
                or not lease["executor_creation_token"]):
            raise InvariantError("operation lease executor creation token is invalid")
        if not isinstance(lease["expected"], dict):
            raise InvariantError("operation lease expected 必須是 object")
        if (not isinstance(lease["request"], dict)
                or canonical_hash(lease["request"]) != lease["request_hash"]
                or lease["request"].get("operation") != lease["operation"]
                or lease["request"].get("operation_id") != lease["operation_id"]
                or lease["request"].get("expected") != lease["expected"]):
            raise InvariantError("operation lease durable request is inconsistent")
        if (not isinstance(lease["child_generation"], int)
                or isinstance(lease["child_generation"], bool)
                or lease["child_generation"] < 0):
            raise InvariantError("operation lease child generation is invalid")
        if not isinstance(lease["child_history"], list):
            raise InvariantError("operation lease child history is invalid")
        previous_generation = 0
        for entry in lease["child_history"]:
            _exact_dict(
                entry, {"generation", "kind", "argv_hash", "identity", "result"},
                "operation child history entry")
            if (not isinstance(entry["generation"], int)
                    or isinstance(entry["generation"], bool)
                    or entry["generation"] != previous_generation + 1):
                raise InvariantError("operation child history generation is invalid")
            previous_generation = entry["generation"]
            if entry["kind"] not in {"git", "validator"}:
                raise InvariantError("operation child history kind is invalid")
            _require_hex(entry["argv_hash"], HASH64_RE, "child history argv_hash")
            if entry["identity"] is not None:
                RepoExecutor._validate_child_identity(entry["identity"])
            RepoExecutor._validate_child_result(
                entry["result"], identity_present=entry["identity"] is not None)
        if lease["child_state"] not in {"idle", "launching", "running", "reaped"}:
            raise InvariantError("operation lease child state is invalid")
        if lease["child_state"] == "idle":
            if (previous_generation != lease["child_generation"] or any(
                    lease[field] is not None for field in (
                        "child_kind", "child_argv_hash", "child_identity",
                        "child_result"))):
                raise InvariantError("idle operation child contains evidence")
        else:
            if lease["child_generation"] < 1:
                raise InvariantError("non-idle operation child lacks generation")
            if previous_generation != lease["child_generation"] - 1:
                raise InvariantError("operation child history is not contiguous")
            if lease["child_kind"] not in {"git", "validator"}:
                raise InvariantError("operation child kind is invalid")
            _require_hex(
                lease["child_argv_hash"], HASH64_RE, "child.argv_hash")
            if lease["child_state"] == "launching":
                if (lease["child_identity"] is not None
                        or lease["child_result"] is not None):
                    raise InvariantError("launching operation child has premature evidence")
            elif lease["child_state"] == "running":
                RepoExecutor._validate_child_identity(lease["child_identity"])
                if lease["child_result"] is not None:
                    raise InvariantError("running operation child has terminal result")
            else:
                if lease["child_identity"] is not None:
                    RepoExecutor._validate_child_identity(lease["child_identity"])
                RepoExecutor._validate_child_result(
                    lease["child_result"],
                    identity_present=lease["child_identity"] is not None)
        if not isinstance(lease["updated_at"], str) or not lease["updated_at"]:
            raise InvariantError("operation lease updated_at 不合法")
        if (lease["result_hash"] is not None
                and (not isinstance(lease["result_hash"], str)
                     or HASH64_RE.fullmatch(lease["result_hash"]) is None)):
            raise InvariantError("operation lease result_hash 不合法")
        if lease["reason"] is not None and not isinstance(lease["reason"], str):
            raise InvariantError("operation lease reason 不合法")
        if lease["state"] == "terminal":
            if (not isinstance(lease["terminal_status"], str)
                    or not lease["terminal_status"]):
                raise InvariantError("terminal operation lease 缺少 status")
            if lease["child_state"] in {"launching", "running"}:
                raise InvariantError("terminal operation lease retains an unfenced child")
        elif lease["terminal_status"] is not None or lease["result_hash"] is not None:
            raise InvariantError("nonterminal operation lease 帶 terminal fields")

    def _reserve(self, operation: Operation, operation_id: str, request_hash: str,
                 expected: dict, request: dict, *,
                 recovery_authorizer: Callable[[dict], bool] | None) -> dict:
        recovery_snapshot = None
        with self._short_lock(self.operation_lock_path):
            existing = None
            if os.path.lexists(self.lease_path):
                existing = self._read_json(self.lease_path, "operation lease")
                self._validate_lease_shape(existing)
            if existing is not None:
                compatible = (
                    existing["operation"] == operation.value
                    and existing["operation_id"] == operation_id
                    and existing["request_hash"] == request_hash
                    and existing["immutable_spec_hash"] == self.authority_hash
                    and existing["expected"] == expected
                    and existing["request"] == request
                )
                if existing["state"] == "terminal":
                    if compatible:
                        if existing["terminal_status"] == "blocked":
                            raise LeaseBusy(
                                "operation is durably blocked:"
                                f"{existing['reason'] or 'no reason'}")
                        raise InvariantError(
                            "terminal operation lease has no durable result")
                    if existing["operation_id"] == operation_id:
                        raise InvariantError(
                            "operation_id is bound to a different terminal lease")
                    lease = self._new_lease(
                        operation, operation_id, request_hash, expected, request)
                    self._atomic_json(self.lease_path, lease)
                    return lease
                same_owner = (
                    compatible
                    and existing["executor_session"] == self._session
                    and existing["pid"] == os.getpid()
                    and existing["executor_creation_token"]
                    == self._executor_creation_token
                )
                if same_owner:
                    return existing
                if not compatible:
                    raise LeaseBusy(
                        f"nonterminal operation lease 仍在:"
                        f"{existing['operation']}/{existing['operation_id']}"
                    )
                recovery_snapshot = json.loads(
                    json.dumps(existing, allow_nan=False))
            else:
                lease = self._new_lease(
                    operation, operation_id, request_hash, expected, request)
                self._atomic_json(self.lease_path, lease)
                return lease

        # PID liveness alone is never a fencing proof.  The guardian or
        # recovery owner must prove that the prior executor and every contained
        # child are gone.  Do that outside the short operation-fence lock, then
        # reacquire it and compare every byte of the observed lease before CAS.
        if recovery_authorizer is None:
            raise LeaseBusy("nonterminal operation lease requires fenced recovery")
        try:
            proof_input = json.loads(
                json.dumps(recovery_snapshot, allow_nan=False))
            authorized = recovery_authorizer(proof_input) is True
        except Exception as exc:
            raise LeaseBusy("operation lease recovery authorization failed") from exc
        if not authorized:
            raise LeaseBusy("nonterminal operation lease requires fenced recovery")

        with self._short_lock(self.operation_lock_path):
            if not os.path.lexists(self.lease_path):
                raise LeaseBusy("operation lease changed during recovery proof")
            existing = self._read_json(self.lease_path, "operation lease")
            self._validate_lease_shape(existing)
            if existing != recovery_snapshot or existing["state"] == "terminal":
                raise LeaseBusy("operation lease changed during recovery proof")
            lease = self._new_lease(
                operation, operation_id, request_hash, expected, request)
            lease["reason"] = f"recovered-from:{existing['nonce']}"
            self._atomic_json(self.lease_path, lease)
            return lease

    def _mark_running(self, operation_id: str, request_hash: str) -> None:
        with self._short_lock(self.operation_lock_path):
            lease = self._read_json(self.lease_path, "operation lease")
            self._validate_lease_shape(lease)
            if (lease["operation_id"] != operation_id
                    or lease["request_hash"] != request_hash
                    or lease["executor_session"] != self._session
                    or lease["executor_creation_token"]
                    != self._executor_creation_token
                    or lease["state"] not in {"reserved", "running"}):
                raise LeaseBusy("operation lease running CAS 失敗")
            if lease["state"] == "reserved":
                lease["state"] = "running"
                lease["updated_at"] = _now()
                self._atomic_json(self.lease_path, lease)

    def _begin_operation_child(self, kind: str, argv: list[str]) -> int:
        if kind not in {"git", "validator"}:
            raise AuthorityError("operation child kind is outside the closed enum")
        operation_id = self._active_operation_id
        request_hash = self._active_request_hash
        if operation_id is None or request_hash is None:
            raise InvariantError("operation child has no active lease authority")
        argv_hash = canonical_hash(argv)
        with self._short_lock(self.operation_lock_path):
            lease = self._read_json(self.lease_path, "operation lease")
            self._validate_lease_shape(lease)
            if (lease["operation_id"] != operation_id
                    or lease["request_hash"] != request_hash
                    or lease["executor_session"] != self._session
                    or lease["executor_creation_token"]
                    != self._executor_creation_token
                    or lease["state"] != "running"
                    or lease["child_state"] not in {"idle", "reaped"}):
                raise LeaseBusy("operation child launching CAS failed")
            if lease["child_state"] == "reaped":
                lease["child_history"].append({
                    "generation": lease["child_generation"],
                    "kind": lease["child_kind"],
                    "argv_hash": lease["child_argv_hash"],
                    "identity": lease["child_identity"],
                    "result": lease["child_result"],
                })
            generation = lease["child_generation"] + 1
            lease.update({
                "child_generation": generation,
                "child_state": "launching",
                "child_kind": kind,
                "child_argv_hash": argv_hash,
                "child_identity": None,
                "child_result": None,
                "updated_at": _now(),
            })
            self._validate_lease_shape(lease)
            self._atomic_json(self.lease_path, lease)
            return generation

    def _publish_operation_child(
            self, generation: int, kind: str, argv: list[str],
            identity: dict) -> None:
        self._validate_child_identity(identity)
        operation_id = self._active_operation_id
        request_hash = self._active_request_hash
        with self._short_lock(self.operation_lock_path):
            lease = self._read_json(self.lease_path, "operation lease")
            self._validate_lease_shape(lease)
            if (lease["operation_id"] != operation_id
                    or lease["request_hash"] != request_hash
                    or lease["executor_session"] != self._session
                    or lease["executor_creation_token"]
                    != self._executor_creation_token
                    or lease["state"] != "running"
                    or lease["child_generation"] != generation
                    or lease["child_state"] != "launching"
                    or lease["child_kind"] != kind
                    or lease["child_argv_hash"] != canonical_hash(argv)):
                raise LeaseBusy("operation child running CAS failed")
            lease.update({
                "child_state": "running",
                "child_identity": identity,
                "updated_at": _now(),
            })
            self._validate_lease_shape(lease)
            self._atomic_json(self.lease_path, lease)

    def _record_operation_child_not_started(
            self, generation: int, kind: str, argv: list[str]) -> None:
        operation_id = self._active_operation_id
        request_hash = self._active_request_hash
        with self._short_lock(self.operation_lock_path):
            lease = self._read_json(self.lease_path, "operation lease")
            self._validate_lease_shape(lease)
            if (lease["operation_id"] != operation_id
                    or lease["request_hash"] != request_hash
                    or lease["executor_session"] != self._session
                    or lease["executor_creation_token"]
                    != self._executor_creation_token
                    or lease["state"] != "running"
                    or lease["child_generation"] != generation
                    or lease["child_state"] != "launching"
                    or lease["child_kind"] != kind
                    or lease["child_argv_hash"] != canonical_hash(argv)):
                raise LeaseBusy("operation child not-started CAS failed")
            lease.update({
                "child_state": "reaped",
                "child_result": {
                    "status": "not-started",
                    "returncode": None,
                    "recorded_at": _now(),
                },
                "updated_at": _now(),
            })
            self._validate_lease_shape(lease)
            self._atomic_json(self.lease_path, lease)

    def _record_operation_child_reaped(
            self, generation: int, identity: dict, returncode: int) -> None:
        operation_id = self._active_operation_id
        request_hash = self._active_request_hash
        with self._short_lock(self.operation_lock_path):
            lease = self._read_json(self.lease_path, "operation lease")
            self._validate_lease_shape(lease)
            if (lease["operation_id"] != operation_id
                    or lease["request_hash"] != request_hash
                    or lease["executor_session"] != self._session
                    or lease["executor_creation_token"]
                    != self._executor_creation_token
                    or lease["state"] != "running"
                    or lease["child_generation"] != generation
                    or lease["child_state"] != "running"
                    or lease["child_identity"] != identity):
                raise LeaseBusy("operation child reaped CAS failed")
            lease.update({
                "child_state": "reaped",
                "child_result": {
                    "status": "exited",
                    "returncode": int(returncode),
                    "recorded_at": _now(),
                },
                "updated_at": _now(),
            })
            self._validate_lease_shape(lease)
            self._atomic_json(self.lease_path, lease)

    def _checkpoint_operation_child(self, generation: int) -> None:
        """Move exact reaped evidence to history before operation progress."""
        operation_id = self._active_operation_id
        request_hash = self._active_request_hash
        with self._short_lock(self.operation_lock_path):
            lease = self._read_json(self.lease_path, "operation lease")
            self._validate_lease_shape(lease)
            if (lease["operation_id"] != operation_id
                    or lease["request_hash"] != request_hash
                    or lease["executor_session"] != self._session
                    or lease["executor_creation_token"]
                    != self._executor_creation_token
                    or lease["state"] != "running"
                    or lease["child_generation"] != generation
                    or lease["child_state"] != "reaped"):
                raise LeaseBusy("operation child checkpoint CAS failed")
            lease["child_history"].append({
                "generation": lease["child_generation"],
                "kind": lease["child_kind"],
                "argv_hash": lease["child_argv_hash"],
                "identity": lease["child_identity"],
                "result": lease["child_result"],
            })
            lease.update({
                "child_state": "idle",
                "child_kind": None,
                "child_argv_hash": None,
                "child_identity": None,
                "child_result": None,
                "updated_at": _now(),
            })
            self._validate_lease_shape(lease)
            self._atomic_json(self.lease_path, lease)

    def _mark_terminal(self, operation_id: str, request_hash: str, *,
                       status: str, result: dict | None = None,
                       reason: str | None = None) -> None:
        with self._short_lock(self.operation_lock_path):
            lease = self._read_json(self.lease_path, "operation lease")
            self._validate_lease_shape(lease)
            if (lease["operation_id"] != operation_id
                    or lease["request_hash"] != request_hash
                    or lease["executor_session"] != self._session
                    or lease["executor_creation_token"]
                    != self._executor_creation_token
                    or lease["state"] not in {"reserved", "running", "terminal"}):
                raise LeaseBusy("operation lease terminal CAS 失敗")
            if lease["state"] == "terminal":
                if lease["terminal_status"] != status:
                    raise InvariantError("operation lease 已有不同 terminal result")
                return
            if lease["child_state"] in {"launching", "running"}:
                raise LeaseBusy("operation lease cannot terminalize an unfenced child")
            lease.update({
                "state": "terminal",
                "updated_at": _now(),
                "terminal_status": status,
                "result_hash": canonical_hash(result) if result is not None else None,
                "reason": reason if reason is not None else lease.get("reason"),
            })
            self._atomic_json(self.lease_path, lease)

    def execute(self, request: dict) -> dict:
        """Validate and execute one of the six closed operations."""
        return self._execute_request(
            request, recovery_authorizer=self._recovery_authorizer)

    @staticmethod
    def fence_recovery_lease(
            lease: dict, *, graceful_timeout: float = 1.0,
            force_timeout: float = 5.0) -> bool:
        """Prove an old executor/contained child can no longer mutate Git.

        This is suitable as ``recovery_authorizer`` for an explicit recovery
        owner.  It never treats PID liveness alone as authority: both executor
        and child identities are bound to OS creation tokens.  The durable
        ``launching`` window intentionally remains blocked because no exact
        native child identity was published in that state.
        """
        try:
            snapshot = json.loads(json.dumps(lease, allow_nan=False))
            RepoExecutor._validate_lease_shape(snapshot)
        except (RepoExecutorError, TypeError, ValueError):
            return False
        if snapshot["state"] == "terminal":
            return False
        if compat.process_matches_identity(
                snapshot["pid"], snapshot["executor_creation_token"],
                include_zombie=True):
            return False
        # Older schema-v2 Windows children used a breakaway-capable Job.  Even
        # their durable ``reaped``/``idle`` checkpoints cannot prove that an
        # escaped grandchild is gone.  History retains every child identity, so
        # reject the entire same-host recovery lease if that contract appears.
        for historical in snapshot["child_history"]:
            historical_identity = historical["identity"]
            if (isinstance(historical_identity, dict)
                    and historical_identity.get("containment_kind")
                    == "windows-job"):
                return False
        child_state = snapshot["child_state"]
        if child_state == "launching":
            return False
        if child_state == "idle":
            return True
        identity = snapshot["child_identity"]
        if identity is None:
            return child_state == "reaped"
        if identity["containment_kind"] == "windows-job":
            # Schema-v2 leases written before strict Jobs remain parseable, but
            # their payloads could have requested Job breakaway.  Neither root
            # absence nor fencing that root proves an escaped descendant gone.
            return False
        if not compat.process_matches_identity(
                identity["pid"], identity["start_token"], identity["group_id"],
                include_zombie=True):
            # A vanished POSIX process-tree root does not prove that a setsid
            # descendant was not reparented.  Only a durable reaped transition,
            # or Windows' kernel-enforced no-breakaway Job contract, closes
            # that gap.  The legacy breakaway-capable Job kind is deliberately
            # rejected above.
            return (child_state == "reaped"
                    or identity["containment_kind"]
                    == "windows-job-no-breakaway-v2")
        if not compat.fence_process_tree(
                identity["pid"], start_token=identity["start_token"],
                group_id=identity["group_id"],
                graceful_timeout=graceful_timeout,
                force_timeout=force_timeout):
            return False
        return not compat.process_matches_identity(
            identity["pid"], identity["start_token"], identity["group_id"],
            include_zombie=True)

    def reconcile_claimed_gate(
            self, request: dict, *,
            recovery_authorizer: Callable[[dict], bool] | None = None) -> dict:
        """Replay one exact claimed gate through the fenced recovery matrix.

        The caller must reuse the original operation id and request bytes.  A
        foreign nonterminal lease is never adopted from PID/HEAD inference: a
        guardian-provided authorizer must first prove the previous executor and
        every contained Git child are gone.  The method intentionally runs
        before :meth:`audit_recovery_state`, because a safe prepared gate may
        have advanced HEAD/sync while its canonical receipt is not durable yet.
        """
        authorizer = (recovery_authorizer if recovery_authorizer is not None
                      else self._recovery_authorizer)
        if authorizer is not None and not callable(authorizer):
            raise AuthorityError("recovery_authorizer must be callable")
        return self._execute_request(
            request,
            recovery_authorizer=authorizer,
            required_operation=Operation.GATE_MERGE,
        )

    def reconcile_operation(
            self, request: dict, *,
            recovery_authorizer: Callable[[dict], bool] | None = None) -> dict:
        """Replay any exact closed-enum request after explicit child fencing.

        Unlike the gate-only convenience API, this entrypoint also covers
        PREFLIGHT/INITIALIZE_RUN_REFS/CREATE_WORKTREE/REMOVE_WORKTREE/SHUTDOWN
        crash recovery.  The original canonical request (including operation
        id) is mandatory; ``_reserve`` compares the complete prior lease and
        runs the authorizer outside the short lock before the byte-for-byte CAS.
        """
        authorizer = (recovery_authorizer if recovery_authorizer is not None
                      else self._recovery_authorizer)
        if authorizer is not None and not callable(authorizer):
            raise AuthorityError("recovery_authorizer must be callable")
        return self._execute_request(
            request, recovery_authorizer=authorizer)

    def reconcile_pending_operation(
            self, *,
            recovery_authorizer: Callable[[dict], bool] | None = None,
    ) -> dict | None:
        """Load and replay the exact request from a nonterminal durable lease."""
        self._start()
        with self._short_lock(self.operation_lock_path):
            if not os.path.lexists(self.lease_path):
                return None
            lease = self._read_json(self.lease_path, "operation lease")
            self._validate_lease_shape(lease)
            if lease["state"] == "terminal":
                return None
            request = json.loads(json.dumps(lease["request"], allow_nan=False))
        return self.reconcile_operation(
            request, recovery_authorizer=recovery_authorizer)

    def _execute_request(
            self, request: dict, *,
            recovery_authorizer: Callable[[dict], bool] | None,
            required_operation: Operation | None = None) -> dict:
        operation, operation_id, task, _authority, expected = self._validate_request(request)
        if required_operation is not None and operation != required_operation:
            raise AuthorityError(
                f"recovery API only accepts {required_operation.value}")
        request_hash = canonical_hash(request)
        self._start()
        cached = self._cached_result(operation_id, request_hash)
        if cached is not None:
            if operation != Operation.PREFLIGHT:
                self._validated_run_artifacts()
            # A crash may happen after the result commit but before lease terminalization.
            try:
                self._mark_terminal(
                    operation_id, request_hash, status=cached.get("status", "completed"),
                    result=cached,
                )
            except (InvariantError, LeaseBusy):
                # A later terminal lease is allowed; the per-operation result is
                # the durable idempotency authority.
                pass
            return cached
        self._reserve(
            operation, operation_id, request_hash, expected, request,
            recovery_authorizer=recovery_authorizer,
        )
        self._mark_running(operation_id, request_hash)
        if self._active_operation_id is not None:
            raise InvariantError("RepoExecutor does not permit nested operations")
        self._active_operation_id = operation_id
        self._active_request_hash = request_hash
        try:
            result = self._dispatch(operation, operation_id, task, request)
            if not isinstance(result, dict):
                raise InvariantError("operation implementation 未回傳 object")
            self._write_result(operation_id, request_hash, result)
            self._mark_terminal(
                operation_id, request_hash, status=result.get("status", "completed"),
                result=result,
            )
        except RepoExecutorError as exc:
            self._mark_terminal(
                operation_id, request_hash, status="blocked", reason=str(exc),
            )
            raise
        except Exception as exc:
            wrapped = InvariantError(f"operation unexpected failure:{exc}")
            self._mark_terminal(
                operation_id, request_hash, status="blocked", reason=str(wrapped),
            )
            raise wrapped from exc
        if operation == Operation.SHUTDOWN:
            self.close()
        return result

    def _dispatch(self, operation: Operation, operation_id: str, task: int | None,
                  request: dict) -> dict:
        implementations = {
            Operation.PREFLIGHT: self._preflight,
            Operation.INITIALIZE_RUN_REFS: self._initialize_run_refs,
            Operation.CREATE_WORKTREE: self._create_worktree,
            Operation.GATE_MERGE: self._gate_merge,
            Operation.REMOVE_WORKTREE: self._remove_worktree,
            Operation.SHUTDOWN: self._shutdown,
        }
        try:
            return implementations[operation](operation_id, task, request)
        finally:
            self._active_operation_id = None
            self._active_request_hash = None

    @staticmethod
    def _resume_windows_suspended_process(process) -> None:
        if not compat.IS_WINDOWS:  # pragma: no cover - Windows-only primitive
            return
        import ctypes

        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
        ntdll.NtResumeProcess.restype = ctypes.c_long
        status = int(ntdll.NtResumeProcess(int(process._handle)))
        if status < 0:
            raise InvariantError("cannot resume contained Windows operation child")

    @staticmethod
    def _close_fd(fd) -> None:
        if fd is None:
            return
        try:
            os.close(fd)
        except OSError:
            pass

    def _run_operation_child(
            self, argv, *, kind: str, cwd: Path, env: dict,
            timeout: float | None = None) -> subprocess.CompletedProcess:
        """Run one operation child below the durable lease child fence."""
        values = [str(value) for value in argv]
        if (not values or any(not value or "\x00" in value for value in values)):
            raise AuthorityError("operation child argv is invalid")
        generation = self._begin_operation_child(kind, values)
        self._fault("child.after_launching")
        process = None
        identity = None
        barrier_read = barrier_write = None
        control_read = control_write = None
        status_read = status_write = None
        stdout_capture = stderr_capture = None
        exception_containment_fenced = False
        try:
            if compat.IS_WINDOWS:
                flags = (getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                         | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000))
                try:
                    process = subprocess.Popen(
                        values, cwd=str(cwd), stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True, shell=False,
                        env=env, creationflags=flags,
                    )
                except (OSError, ValueError):
                    self._record_operation_child_not_started(
                        generation, kind, values)
                    self._checkpoint_operation_child(generation)
                    raise
                if not compat.attach_process_group(
                        process, allow_breakaway=False):
                    try:
                        process.kill()
                        process.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    raise LeaseBusy(
                        "contained Windows child identity gap requires recovery")
                captured = compat.capture_process_identity(process)
                identity = {
                    **captured,
                    "containment_kind": "windows-job-no-breakaway-v2",
                }
                self._publish_operation_child(
                    generation, kind, values, identity)
                self._fault("child.after_running")
                self._resume_windows_suspended_process(process)
                self._fault("child.after_payload_release")
                try:
                    stdout, stderr = process.communicate(timeout=timeout)
                except subprocess.TimeoutExpired as timeout_error:
                    compat.close_process_group(process)
                    try:
                        stdout, stderr = process.communicate(timeout=5)
                    except subprocess.TimeoutExpired as exc:
                        raise LeaseBusy(
                            "contained Windows child could not be reaped") from exc
                    self._record_operation_child_reaped(
                        generation, identity, int(process.returncode))
                    self._fault("child.after_reaped")
                    self._checkpoint_operation_child(generation)
                    raise timeout_error
                finally:
                    compat.close_process_group(process)
                self._record_operation_child_reaped(
                    generation, identity, int(process.returncode))
                self._fault("child.after_reaped")
                self._checkpoint_operation_child(generation)
                return subprocess.CompletedProcess(
                    values, int(process.returncode), stdout, stderr)

            barrier_read, barrier_write = os.pipe()
            control_read, control_write = os.pipe()
            status_read, status_write = os.pipe()
            inherited = (barrier_read, control_read, status_write)
            for fd in inherited:
                os.set_inheritable(fd, True)
            guardian_argv = [
                sys.executable, "-c", _POSIX_OPERATION_GUARDIAN,
                str(barrier_read), str(control_read), str(status_write), *values,
            ]
            stdout_capture = tempfile.TemporaryFile(
                mode="w+t", encoding="utf-8")
            stderr_capture = tempfile.TemporaryFile(
                mode="w+t", encoding="utf-8")
            try:
                process = subprocess.Popen(
                    guardian_argv, cwd=str(cwd), stdout=stdout_capture,
                    stderr=stderr_capture, text=True, shell=False, env=env,
                    pass_fds=inherited, start_new_session=True,
                )
            except (OSError, ValueError):
                self._record_operation_child_not_started(
                    generation, kind, values)
                self._checkpoint_operation_child(generation)
                raise
            self._close_fd(barrier_read)
            self._close_fd(control_read)
            self._close_fd(status_write)
            barrier_read = control_read = status_write = None
            captured = compat.capture_process_identity(process)
            identity = {
                **captured,
                "containment_kind": "process-tree",
            }
            self._publish_operation_child(generation, kind, values, identity)
            self._fault("child.after_running")
            startup_ready, _, _ = select.select([status_read], [], [], 5.0)
            startup = os.read(status_read, 5) if startup_ready else b""
            if (len(startup) != 5 or startup[:1] != b"G"
                    or struct.unpack("!i", startup[1:])[0] != 0):
                startup_code = (struct.unpack("!i", startup[1:])[0]
                                if len(startup) == 5 else None)
                raise LeaseBusy(
                    "POSIX operation guardian did not prove subreaper readiness"
                    + (f" (code {startup_code})"
                       if startup_code is not None else ""))
            if os.write(barrier_write, b"R") != 1:
                raise LeaseBusy("cannot release operation child ACK barrier")
            self._close_fd(barrier_write)
            barrier_write = None
            self._fault("child.after_payload_release")
            readable, _, _ = select.select([status_read], [], [], timeout)
            timed_out = not readable
            if timed_out:
                if os.write(control_write, b"X") != 1:
                    raise LeaseBusy(
                        "cannot request contained POSIX child timeout fence")
                readable, _, _ = select.select([status_read], [], [], 8.0)
                if not readable:
                    raise LeaseBusy(
                        "contained POSIX child did not publish completion proof")
            completion = b""
            while len(completion) < 5:
                chunk = os.read(status_read, 5 - len(completion))
                if not chunk:
                    break
                completion += chunk
            if len(completion) != 5 or completion[:1] != b"D":
                raise LeaseBusy(
                    "contained POSIX child lacks guardian completion proof")
            payload_returncode = struct.unpack("!i", completion[1:])[0]
            self._record_operation_child_reaped(
                generation, identity, payload_returncode)
            self._fault("child.after_reaped")
            if os.write(control_write, b"A") != 1:
                raise LeaseBusy("cannot ACK contained POSIX child completion")
            self._close_fd(control_write)
            control_write = None
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if not compat.fence_process_tree(
                        identity["pid"], start_token=identity["start_token"],
                        group_id=identity["group_id"],
                        graceful_timeout=0.2, force_timeout=3.0):
                    raise LeaseBusy("contained POSIX guardian did not exit")
            self._checkpoint_operation_child(generation)
            stdout_capture.seek(0)
            stderr_capture.seek(0)
            stdout = stdout_capture.read()
            stderr = stderr_capture.read()
            if timed_out:
                raise subprocess.TimeoutExpired(
                    values, timeout, output=stdout, stderr=stderr)
            return subprocess.CompletedProcess(
                values, payload_returncode, stdout, stderr)
        except Exception:
            # Once native process creation succeeds, never forge a reaped child
            # from a launching identity gap.  A running child has an exact
            # durable birth token and can be fenced here or by explicit recovery.
            if process is not None:
                self._close_fd(barrier_write)
                barrier_write = None
                self._close_fd(control_write)
                control_write = None
                if compat.IS_WINDOWS:
                    compat.close_process_group(process)
                    # Strict no-breakaway Job close is the Windows equivalent
                    # of an exact contained-tree fence.
                    exception_containment_fenced = True
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if identity is not None:
                        fenced = compat.fence_process_tree(
                            identity["pid"], start_token=identity["start_token"],
                            group_id=identity["group_id"],
                            graceful_timeout=0.2, force_timeout=3.0)
                        if fenced:
                            exception_containment_fenced = True
                            try:
                                process.wait(timeout=3)
                            except subprocess.TimeoutExpired:
                                pass
                    else:
                        try:
                            process.kill()
                            process.wait(timeout=3)
                        except (OSError, subprocess.TimeoutExpired):
                            pass
                if (identity is not None and exception_containment_fenced
                        and not compat.process_matches_identity(
                        identity["pid"], identity["start_token"],
                        identity["group_id"], include_zombie=True)):
                    try:
                        lease = self._read_json(self.lease_path, "operation lease")
                        if (lease.get("child_state") == "running"
                                and lease.get("child_identity") == identity):
                            self._record_operation_child_reaped(
                                generation, identity,
                                int(process.returncode if process.returncode is not None else 1))
                            self._checkpoint_operation_child(generation)
                    except RepoExecutorError:
                        pass
            raise
        finally:
            if compat.IS_WINDOWS and process is not None:
                compat.close_process_group(process)
            for fd in (
                    barrier_read, barrier_write, control_read, control_write,
                    status_read, status_write):
                self._close_fd(fd)
            if process is not None and process.poll() is None:
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    if identity is not None:
                        compat.fence_process_tree(
                            identity["pid"], start_token=identity["start_token"],
                            group_id=identity["group_id"],
                            graceful_timeout=0.2, force_timeout=3.0)
                    try:
                        process.communicate(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
            for stream in (
                    getattr(process, "stdin", None),
                    getattr(process, "stdout", None),
                    getattr(process, "stderr", None)):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            for capture in (stdout_capture, stderr_capture):
                if capture is not None:
                    try:
                        capture.close()
                    except OSError:
                        pass

    def _git(self, *args: str, cwd: Path | None = None, check=True,
             empty_hooks=False) -> subprocess.CompletedProcess:
        argv = ["git", "-c", "gc.auto=0"]
        if empty_hooks:
            argv += ["-c", f"core.hooksPath={self.empty_hooks_dir}"]
        argv += [str(arg) for arg in args]
        if self._active_operation_id is None:
            env = dict(os.environ)
            env.update({
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_PAGER": "cat",
                "PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
            })
            env.pop("GIT_EXTERNAL_DIFF", None)
            argv = [
                "git", "--no-pager", "-c", "core.fsmonitor=false", *argv[1:],
            ]
            result = subprocess.run(
                argv, cwd=str(cwd or self.primary_repo), capture_output=True,
                text=True, check=False, shell=False,
                env=env,
            )
        else:
            result = self._run_operation_child(
                argv, kind="git", cwd=(cwd or self.primary_repo),
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        if check and result.returncode:
            raise InvariantError(
                f"fixed Git argv failed rc={result.returncode}:"
                f" {' '.join(argv[:4])}; {result.stderr.strip()}"
            )
        return result

    def _snapshot(self, repo: Path | None = None) -> dict:
        cwd = repo or self.primary_repo
        head_result = self._git("rev-parse", "--verify", "HEAD", cwd=cwd)
        symbolic = self._git("symbolic-ref", "-q", "HEAD", cwd=cwd, check=False)
        status = self._git(
            "status", "--porcelain=v1", "--untracked-files=all", cwd=cwd,
        ).stdout
        return {
            "head": head_result.stdout.strip(),
            "head_ref": symbolic.stdout.strip() if symbolic.returncode == 0 else None,
            "status": status,
        }

    def _require_primary(self, *, head: str | None = None) -> dict:
        snapshot = self._snapshot()
        if snapshot["head_ref"] != self.spec.primary_ref:
            raise InvariantError(
                f"primary 必須 checkout {self.spec.primary_ref}，"
                f"目前為 {snapshot['head_ref'] or 'detached'}"
            )
        if snapshot["status"]:
            raise InvariantError("primary worktree 必須 clean")
        if head is not None and snapshot["head"] != head:
            raise InvariantError(
                f"primary HEAD 不符；預期 {head}，目前 {snapshot['head']}"
            )
        if self._ref_tip(self.spec.primary_ref) != snapshot["head"]:
            raise InvariantError("primary branch tip 與 HEAD 不一致")
        return snapshot

    def _ref_tip(self, ref: str, *, repo: Path | None = None) -> str | None:
        result = self._git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}",
                           cwd=repo, check=False)
        if result.returncode == 1:
            return None
        if result.returncode:
            raise InvariantError(f"無法解析 canonical ref {ref}:{result.stderr.strip()}")
        value = result.stdout.strip()
        _require_sha(value, ref)
        return value

    def _is_ancestor(self, older: str, newer: str) -> bool:
        result = self._git("merge-base", "--is-ancestor", older, newer, check=False)
        if result.returncode not in (0, 1):
            raise InvariantError("git merge-base invariant check 失敗")
        return result.returncode == 0

    @contextmanager
    def _merge_lock(self):
        with self._short_lock(self.merge_lock_path):
            yield

    @staticmethod
    def _artifact_matches(artifact: dict, expected: dict, label: str) -> None:
        for field, value in expected.items():
            if artifact.get(field) != value:
                raise InvariantError(f"{label}.{field} 與 authority 不符")

    def _intent_path(self, kind: str, identifier: str) -> Path:
        return self.intents_dir / f"{kind}-{identifier}.json"

    def _receipt_path(self, kind: str, identifier: str) -> Path:
        return self.receipts_dir / f"{kind}-{identifier}.json"

    def _write_receipt(self, path: Path, body: dict) -> dict:
        receipt = dict(body)
        receipt["receipt_hash"] = canonical_hash(body)
        if os.path.lexists(path):
            existing = self._read_json(path, "immutable operation receipt")
            if existing != receipt:
                raise InvariantError("operation receipt already has different bytes")
            return existing
        self._atomic_json(path, receipt)
        if self._read_json(path, "immutable operation receipt") != receipt:
            raise InvariantError("operation receipt byte verification failed")
        return receipt

    def claimed_request_path(self, request_id: str) -> Path:
        _require_hex(request_id, HEX32_RE, "request_id")
        path = (self.workspace_root / self.spec.parent_workspace / "parallel"
                / self.spec.run_id / "requests" / "claimed" / f"{request_id}.json")
        self._reject_link_components(path.parent, allow_missing=True)
        canonical = path.resolve(strict=False)
        if canonical != path:
            raise AuthorityError("claimed request path traverses a link/reparse")
        self._assert_contained(canonical, self.workspace_root, "workspace root")
        return canonical

    def build_claimed_gate_request(self, *, task: int, request_id: str,
                                   validated_sha: str, validated_round: int,
                                   deadline_at: str) -> dict:
        """Build the canonical gate-client payload (before spool claim).

        The supervisor must publish and claim this exact payload through
        :class:`parallel_spool.DurableSpool`; it must never rewrite the client
        artifact to add executor-only authority fields.
        """
        assignment = self._assignment(task)
        _require_hex(request_id, HEX32_RE, "request_id")
        _require_sha(validated_sha, "validated_sha")
        _require_positive_int(validated_round, "validated_round")
        if (not isinstance(deadline_at, str) or not deadline_at.strip()
                or "\x00" in deadline_at):
            raise AuthorityError("deadline_at 必須是非空字串")
        return {
            "schema": 1,
            "run_id": self.spec.run_id,
            "task": task,
            "request_id": request_id,
            "validated_sha": validated_sha,
            "validated_round": validated_round,
            "deadline_at": deadline_at,
            "run_config_hash": assignment.run_config_hash,
            "launch_spec_hash": assignment.launch_spec_hash,
            "manifest_hash": self.spec.manifest_hash,
        }

    def _worktree_records(self) -> dict[Path, dict]:
        result = self._git("worktree", "list", "--porcelain")
        records = {}
        current = None
        for line in result.stdout.splitlines() + [""]:
            if not line:
                if current is not None:
                    records[current["path"]] = current
                current = None
                continue
            key, _, value = line.partition(" ")
            if key == "worktree":
                try:
                    worktree = Path(value).resolve(strict=False)
                except OSError as exc:
                    raise InvariantError(f"worktree registry path 無法解析:{exc}") from exc
                current = {"path": worktree, "locked": False, "prunable": False}
            elif current is not None:
                if key in {"HEAD", "branch", "detached"}:
                    current[key.lower()] = value if value else True
                elif key == "locked":
                    current["locked"] = True
                    current["lock_reason"] = value
                elif key == "prunable":
                    current["prunable"] = True
        return records

    def observe_worktree(self, task: int) -> dict:
        """Return a hash-bound, read-only cleanup observation for one task."""
        self._start()
        target = self.worktree_path(task)
        # Re-derive after global lock acquisition so a path swap cannot redirect
        # an operation between authority validation and observation.
        if target != self.worktree_path(task):  # pragma: no cover - defensive
            raise InvariantError("derived worktree identity changed")
        records = self._worktree_records()
        record = records.get(target)
        # ``Path.exists`` follows links and reports a dangling symlink absent.
        # Cleanup/recovery authority is lexical, so any directory entry counts
        # as a present resource and link/reparse components fail closed.
        exists = os.path.lexists(target)
        if exists:
            self._reject_link_components(target)
        if exists and record is not None:
            snapshot = self._snapshot(target)
            git_dir_raw = self._git("rev-parse", "--git-dir", cwd=target).stdout.strip()
            git_dir = Path(git_dir_raw)
            git_dir = (git_dir if git_dir.is_absolute() else target / git_dir).resolve(strict=True)
            self._assert_contained(git_dir, self.common_dir, "Git common-dir")
            live_locks = sorted(
                str(path.name) for path in (
                    git_dir / "index.lock", git_dir / "HEAD.lock", git_dir / "locked",
                ) if path.exists()
            )
            observation = {
                "schema_version": 1,
                "task": task,
                "worker_repo": str(target),
                "task_ref": self.task_ref(task),
                "task_ref_tip": self._ref_tip(self.task_ref(task)),
                "exists": True,
                "registered": True,
                "head": snapshot["head"],
                "head_ref": snapshot["head_ref"],
                "status": snapshot["status"],
                "locked": bool(record.get("locked")),
                "live_locks": live_locks,
                "git_dir": str(git_dir),
            }
        else:
            observation = {
                "schema_version": 1,
                "task": task,
                "worker_repo": str(target),
                "task_ref": self.task_ref(task),
                "task_ref_tip": self._ref_tip(self.task_ref(task)),
                "exists": exists,
                "registered": record is not None,
                "head": None,
                "head_ref": None,
                "status": None,
                "locked": bool(record and record.get("locked")),
                "live_locks": [],
                "git_dir": None,
            }
        return {**observation, "observation_token": canonical_hash(observation)}

    def _gate_journal_paths(self, directory: Path, label: str) -> dict[str, Path]:
        """Return only this run's gate journals while auditing old namespaces.

        Gate sidecars live in the repository common-dir and intentionally
        survive SHUTDOWN.  Historical runs therefore coexist here.  A foreign
        entry is skipped only after its run/request/operation identity proves
        that it is a well-formed artifact from that other run; malformed or
        relabelled files still fail closed.
        """
        try:
            children = tuple(directory.iterdir())
        except OSError as exc:
            raise InvariantError(f"cannot enumerate {label}") from exc
        paths = {}
        for path in children:
            if not path.name.startswith("gate-"):
                continue
            match = re.fullmatch(r"gate-([0-9a-f]{32})\.json", path.name)
            if match is None:
                raise InvariantError(f"{label} contains malformed gate artifact")
            request_id = match.group(1)
            artifact = self._read_json(path, label)
            try:
                artifact_run = parallel_contract.require_run_id(
                    artifact.get("run_id"))
            except parallel_contract.ParallelContractError as exc:
                raise InvariantError(
                    f"{label} gate artifact has invalid run identity") from exc
            if artifact_run != self.spec.run_id:
                if (artifact.get("schema_version") != 1
                        or artifact.get("kind") != "GATE_MERGE"
                        or artifact.get("request_id") != request_id
                        or artifact.get("operation_id")
                        != gate_operation_id(artifact_run, request_id)):
                    raise InvariantError(
                        f"{label} historical gate artifact identity is invalid")
                continue
            if request_id in paths:
                raise InvariantError(f"{label} contains duplicate gate artifact")
            paths[request_id] = path
        return paths

    def _audit_latest_operation_lease(
        self,
        allowed_blocked_removes: Mapping[int, str] | None = None,
    ) -> dict:
        with self._short_lock(self.operation_lock_path):
            if not os.path.lexists(self.lease_path):
                raise InvariantError("recovery audit requires an operation lease")
            lease = self._read_json(self.lease_path, "operation lease")
            self._validate_lease_shape(lease)
            if lease["state"] != "terminal":
                raise InvariantError("recovery audit found a nonterminal operation lease")
            if lease["terminal_status"] == "blocked":
                allowed = dict(allowed_blocked_removes or {})
                try:
                    operation, operation_id, task, _authority, expected = (
                        self._validate_request(lease["request"]))
                except RepoExecutorError as exc:
                    raise InvariantError(
                        "durably blocked operation request is invalid") from exc
                if (operation != Operation.REMOVE_WORKTREE
                        or task not in allowed
                        or expected.get("terminal_outcome") != allowed[task]):
                    raise InvariantError(
                        "recovery audit found a durably blocked operation")
                # Only a pre-intent cleanup refusal (for example a dirty
                # worktree) may be superseded after the operator repairs the
                # resource.  Once a remove intent exists, recovery must replay
                # that exact transaction rather than invent a new operation.
                if (os.path.lexists(self._intent_path("remove", operation_id))
                        or os.path.lexists(
                            self._receipt_path("remove", operation_id))):
                    raise InvariantError(
                        "blocked remove has transaction evidence and cannot be superseded")
            result = None
            if lease["terminal_status"] != "blocked":
                result = self._cached_result(
                    lease["operation_id"], lease["request_hash"])
                if result is None:
                    raise InvariantError(
                        "terminal operation lease lacks immutable result")
                if (lease["result_hash"] != canonical_hash(result)
                        or result.get("operation") != lease["operation"]
                        or result.get("operation_id") != lease["operation_id"]):
                    raise InvariantError(
                        "terminal operation lease/result authority mismatch")
            return {
                "operation": lease["operation"],
                "operation_id": lease["operation_id"],
                "terminal_status": lease["terminal_status"],
                "generation": lease["generation"],
                "result": result,
            }

    def _claimed_gate_evidence(
            self, spool: parallel_spool.DurableSpool,
            receipt: dict) -> tuple[dict, str]:
        request_id = receipt["request_id"]
        try:
            record = spool.get_request(request_id)
        except parallel_spool.SpoolError as exc:
            raise InvariantError(
                f"claimed gate request evidence is corrupt:{request_id}") from exc
        if record is None or record.state != "claimed":
            state = None if record is None else record.state
            raise InvariantError(
                f"canonical receipt request is not claimed:{request_id}:{state}")
        artifact = record.payload
        fields = {
            "schema", "run_id", "task", "request_id", "validated_sha",
            "validated_round", "run_config_hash", "launch_spec_hash",
            "manifest_hash", "deadline_at",
        }
        _exact_dict(artifact, fields, "audit claimed gate request")
        assignment = self._assignment(receipt["task"])
        expected = {
            "schema": 1,
            "run_id": self.spec.run_id,
            "task": receipt["task"],
            "request_id": request_id,
            "validated_sha": receipt["validated_sha"],
            "validated_round": receipt["validated_round"],
            "run_config_hash": assignment.run_config_hash,
            "launch_spec_hash": assignment.launch_spec_hash,
            "manifest_hash": self.spec.manifest_hash,
        }
        self._artifact_matches(artifact, expected, "audit claimed gate request")
        if not isinstance(artifact["deadline_at"], str) or not artifact["deadline_at"].strip():
            raise InvariantError("audit claimed gate deadline is invalid")
        return artifact, parallel_state.canonical_json_hash(artifact)

    def _audit_gate_success_evidence(
            self, canonical_receipt: dict, *,
            intent_path: Path, receipt_path: Path,
            spool: parallel_spool.DurableSpool) -> None:
        task = canonical_receipt["task"]
        request_id = canonical_receipt["request_id"]
        claimed, claimed_hash = self._claimed_gate_evidence(
            spool, canonical_receipt)
        operation_id = gate_operation_id(self.spec.run_id, request_id)
        operation_request = {
            "operation": Operation.GATE_MERGE.value,
            "operation_id": operation_id,
            "task": task,
            "authority": {
                "manifest_hash": self.spec.manifest_hash,
                "assignment_hash": canonical_receipt["assignment_hash"],
                "request_hash": claimed_hash,
            },
            "expected": {
                "request_id": request_id,
                "validated_sha": canonical_receipt["validated_sha"],
                "validated_round": canonical_receipt["validated_round"],
                "integration_before": canonical_receipt["integration_before"],
                "sync_before": canonical_receipt["integration_before"],
            },
        }
        authority = self._gate_authority(
            operation_id, task, operation_request,
            {**claimed, "request_hash": claimed_hash},
        )
        self._require_canonical_receipt(canonical_receipt, authority, task)

        intent = self._read_json(intent_path, "audit gate intent")
        intent_fields = set(authority) | {
            "state", "prepared_at", "committed_at", "receipt_hash",
        }
        _exact_dict(intent, intent_fields, "audit gate intent")
        self._artifact_matches(intent, authority, "audit gate intent")
        if (intent["state"] != "committed"
                or not isinstance(intent["prepared_at"], str)
                or not intent["prepared_at"]
                or not isinstance(intent["committed_at"], str)
                or not intent["committed_at"]):
            raise InvariantError("audit gate intent is not committed evidence")

        common_receipt = self._read_json(receipt_path, "audit gate receipt")
        common_fields = set(authority) | {
            "canonical_receipt_hash", "merged_at", "receipt_hash",
        }
        _exact_dict(common_receipt, common_fields, "audit gate receipt")
        self._artifact_matches(common_receipt, authority, "audit gate receipt")
        body = {
            key: value for key, value in common_receipt.items()
            if key != "receipt_hash"
        }
        if (canonical_hash(body) != common_receipt["receipt_hash"]
                or intent["receipt_hash"] != common_receipt["receipt_hash"]
                or common_receipt["canonical_receipt_hash"]
                != parallel_state.canonical_json_hash(canonical_receipt)
                or not isinstance(common_receipt["merged_at"], str)
                or not common_receipt["merged_at"]):
            raise InvariantError("audit gate receipt evidence is incomplete")

        result = self._cached_result(
            operation_id, canonical_hash(operation_request))
        if result is None:
            raise InvariantError("audit gate success lacks durable operation result")
        _exact_dict(result, {
            "operation", "operation_id", "status", "task", "request_id",
            "validated_sha", "validated_round", "receipt_hash",
            "observation_token",
        }, "audit gate operation result")
        if (result["operation"] != Operation.GATE_MERGE.value
                or result["operation_id"] != operation_id
                or result["status"] not in {"merged", "already-merged"}
                or result["task"] != task
                or result["request_id"] != request_id
                or result["validated_sha"] != canonical_receipt["validated_sha"]
                or result["validated_round"] != canonical_receipt["validated_round"]
                or result["receipt_hash"] != common_receipt["receipt_hash"]):
            raise InvariantError("audit gate operation result does not prove success")
        _require_hex(
            result["observation_token"], HASH64_RE,
            "audit gate result observation_token")

    def audit_recovery_state(
        self,
        *,
        allowed_blocked_removes: Mapping[int, str] | None = None,
    ) -> dict:
        """Validate the complete receipt/journal/spool success evidence graph."""
        self._start()
        self._validated_run_artifacts()
        normalized_allowed: dict[int, str] = {}
        if allowed_blocked_removes is not None:
            if not isinstance(allowed_blocked_removes, Mapping):
                raise AuthorityError(
                    "allowed_blocked_removes must be a task/outcome mapping")
            for task, outcome in allowed_blocked_removes.items():
                task_id = _require_positive_int(task, "blocked remove task")
                if outcome not in {"integrated", "blocked", "cancelled"}:
                    raise AuthorityError(
                        "blocked remove terminal outcome is invalid")
                normalized_allowed[task_id] = str(outcome)
        latest_operation = self._audit_latest_operation_lease(normalized_allowed)
        with self._merge_lock():
            _artifacts, receipts = self._canonical_receipt_chain()
            expected_request_ids = {
                receipt["request_id"] for receipt in receipts
            }
            intent_paths = self._gate_journal_paths(
                self.intents_dir, "common-dir intents")
            receipt_paths = self._gate_journal_paths(
                self.receipts_dir, "common-dir receipts")
            if set(intent_paths) != expected_request_ids:
                missing = sorted(expected_request_ids - set(intent_paths))
                extra = sorted(set(intent_paths) - expected_request_ids)
                raise InvariantError(
                    f"canonical/common gate intent graph mismatch:"
                    f"missing={missing},orphan={extra}")
            if set(receipt_paths) != expected_request_ids:
                missing = sorted(expected_request_ids - set(receipt_paths))
                extra = sorted(set(receipt_paths) - expected_request_ids)
                raise InvariantError(
                    f"canonical/common gate receipt graph mismatch:"
                    f"missing={missing},orphan={extra}")

            try:
                spool = parallel_spool.DurableSpool(
                    self.run_dir / "requests",
                    responses_root=self.run_dir / "responses",
                )
            except parallel_spool.SpoolError as exc:
                raise InvariantError("gate spool cannot be audited") from exc
            for receipt in receipts:
                request_id = receipt["request_id"]
                self._audit_gate_success_evidence(
                    receipt,
                    intent_path=intent_paths[request_id],
                    receipt_path=receipt_paths[request_id],
                    spool=spool,
                )

            receipt_tip = (receipts[-1]["validated_sha"] if receipts
                           else self.spec.integration_start_sha)
            primary = self._require_primary(head=receipt_tip)
            sync_tip = self._ref_tip(self.sync_ref)
            if sync_tip != receipt_tip:
                raise InvariantError(
                    "safe sync ref does not equal canonical receipt tip")
        return {
            "schema_version": 1,
            "run_id": self.spec.run_id,
            "manifest_hash": self.spec.manifest_hash,
            "primary_ref": self.spec.primary_ref,
            "primary_sha": primary["head"],
            "sync_ref": self.sync_ref,
            "sync_sha": sync_tip,
            "receipt_count": len(receipts),
            "receipt_tip": receipt_tip,
            "last_receipt_hash": (
                parallel_state.canonical_json_hash(receipts[-1])
                if receipts else None
            ),
            "latest_operation": latest_operation,
        }

    def _preflight(self, operation_id: str, _task: None, request: dict) -> dict:
        del operation_id
        expected = request["expected"]
        before = self._require_primary(head=expected["head_sha"])
        try:
            result = self._run_operation_child(
                list(self.spec.validator_argv), kind="validator",
                cwd=self.primary_repo,
                timeout=self.spec.validator_timeout,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except subprocess.TimeoutExpired as exc:
            raise InvariantError("immutable preflight validator timeout") from exc
        after = self._snapshot()
        if after != before or after["status"]:
            raise InvariantError("preflight validator 改變 Git snapshot")
        if result.returncode:
            tail = "\n".join(result.stderr.strip().splitlines()[-10:])
            raise InvariantError(f"preflight validator failed rc={result.returncode}:{tail}")
        return {
            "operation": Operation.PREFLIGHT.value,
            "operation_id": request["operation_id"],
            "status": "validated",
            "head_ref": after["head_ref"],
            "head_sha": after["head"],
        }

    def _initialize_run_refs(self, operation_id: str, _task: None,
                             request: dict) -> dict:
        del _task
        self._validated_run_artifacts()
        start = self.spec.integration_start_sha
        intent_path = self._intent_path("init", operation_id)
        receipt_path = self._receipt_path("init", operation_id)
        intent_authority = {
            "schema_version": 1,
            "kind": "INITIALIZE_RUN_REFS",
            "operation_id": operation_id,
            "manifest_hash": self.spec.manifest_hash,
            "sync_ref": self.sync_ref,
            "start_sha": start,
        }
        with self._merge_lock():
            self._require_primary(head=start)
            if os.path.lexists(receipt_path):
                if not os.path.lexists(intent_path):
                    raise InvariantError("init receipt 缺少 intent")
                intent = self._read_json(intent_path, "init intent")
                self._artifact_matches(intent, intent_authority, "init intent")
                receipt = self._read_json(receipt_path, "init receipt")
                self._artifact_matches(receipt, intent_authority, "init receipt")
                body = {key: value for key, value in receipt.items()
                        if key != "receipt_hash"}
                if canonical_hash(body) != receipt.get("receipt_hash"):
                    raise InvariantError("init receipt hash mismatch")
                if self._ref_tip(self.sync_ref) != start:
                    raise InvariantError("init receipt 與 sync ref 不一致")
                if intent.get("state") == "prepared":
                    intent.update({
                        "state": "committed", "committed_at": _now(),
                        "receipt_hash": receipt["receipt_hash"],
                    })
                    self._atomic_json(intent_path, intent)
                elif (intent.get("state") != "committed"
                      or intent.get("receipt_hash") != receipt["receipt_hash"]):
                    raise InvariantError("init committed intent/receipt mismatch")
                return {
                    "operation": Operation.INITIALIZE_RUN_REFS.value,
                    "operation_id": operation_id,
                    "status": "already-initialized",
                    "sync_ref": self.sync_ref,
                    "sync_sha": start,
                    "receipt_hash": receipt.get("receipt_hash"),
                }
            if os.path.lexists(intent_path):
                intent = self._read_json(intent_path, "init intent")
                self._artifact_matches(intent, intent_authority, "init intent")
                if intent.get("state") == "committed":
                    raise InvariantError("committed init intent 缺少 receipt")
            else:
                intent = {**intent_authority, "state": "prepared", "prepared_at": _now()}
                self._atomic_json(intent_path, intent)
            self._fault("initialize.after_prepared")
            current = self._ref_tip(self.sync_ref)
            if current is None:
                self._git("update-ref", self.sync_ref, start, "0" * len(start))
            elif current != start:
                raise InvariantError("safe sync ref 已由未知 actor 建立或移動")
            self._fault("initialize.after_ref")
            receipt_body = {
                **intent_authority,
                "created_at": _now(),
            }
            receipt = self._write_receipt(receipt_path, receipt_body)
            self._fault("initialize.after_receipt")
            intent.update({
                "state": "committed", "committed_at": _now(),
                "receipt_hash": receipt["receipt_hash"],
            })
            self._atomic_json(intent_path, intent)
        return {
            "operation": Operation.INITIALIZE_RUN_REFS.value,
            "operation_id": operation_id,
            "status": "initialized",
            "sync_ref": self.sync_ref,
            "sync_sha": start,
            "receipt_hash": receipt["receipt_hash"],
        }

    def _create_worktree(self, operation_id: str, task: int, request: dict) -> dict:
        self._validated_run_artifacts()
        base = request["expected"]["base_sha"]
        assignment = self._assignment(task)
        task_ref = self.task_ref(task)
        branch_name = task_ref.removeprefix("refs/heads/")
        target = self.worktree_path(task)
        intent_path = self._intent_path("create", operation_id)
        receipt_path = self._receipt_path("create", operation_id)
        authority = {
            "schema_version": 1,
            "kind": "CREATE_WORKTREE",
            "operation_id": operation_id,
            "run_id": self.spec.run_id,
            "task": task,
            "manifest_hash": self.spec.manifest_hash,
            "assignment_hash": assignment.assignment_hash,
            "base_sha": base,
            "task_ref": task_ref,
            "worker_repo": str(target),
        }
        with self._merge_lock():
            self._require_primary(head=base)
            if self._ref_tip(self.sync_ref) != base:
                raise InvariantError("CREATE_WORKTREE base_sha 必須等於 safe sync ref")
            if os.path.lexists(receipt_path):
                if not os.path.lexists(intent_path):
                    raise InvariantError("create receipt 缺少 intent")
                intent = self._read_json(intent_path, "create intent")
                self._artifact_matches(intent, authority, "create intent")
                receipt = self._read_json(receipt_path, "create receipt")
                self._artifact_matches(receipt, authority, "create receipt")
                body = {key: value for key, value in receipt.items()
                        if key != "receipt_hash"}
                if canonical_hash(body) != receipt.get("receipt_hash"):
                    raise InvariantError("create receipt hash mismatch")
                observation = self.observe_worktree(task)
                if (not observation["exists"] or not observation["registered"]
                        or observation["head"] != base
                        or observation["head_ref"] != task_ref
                        or observation["status"] or observation["locked"]
                        or observation["live_locks"]):
                    raise InvariantError("create receipt 與 worktree identity 不一致")
                if intent.get("state") == "prepared":
                    intent.update({
                        "state": "committed", "committed_at": _now(),
                        "receipt_hash": receipt["receipt_hash"],
                    })
                    self._atomic_json(intent_path, intent)
                elif (intent.get("state") != "committed"
                      or intent.get("receipt_hash") != receipt["receipt_hash"]):
                    raise InvariantError("create committed intent/receipt mismatch")
                status = "already-created"
            else:
                if os.path.lexists(intent_path):
                    intent = self._read_json(intent_path, "create intent")
                    self._artifact_matches(intent, authority, "create intent")
                    if intent.get("state") == "committed":
                        raise InvariantError("committed create intent 缺少 receipt")
                else:
                    intent = {**authority, "state": "prepared", "prepared_at": _now()}
                    self._atomic_json(intent_path, intent)
                self._fault("create.after_prepared")
                ref_tip = self._ref_tip(task_ref)
                records = self._worktree_records()
                registered = records.get(target)
                if ref_tip is None and not target.exists() and registered is None:
                    self._git("update-ref", task_ref, base, "0" * len(base))
                    ref_tip = base
                if ref_tip != base:
                    raise InvariantError("task ref 非 expected-absent 或 tip 不符")
                self._fault("create.after_ref")
                records = self._worktree_records()
                registered = records.get(target)
                if not target.exists() and registered is None:
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    self._reject_link_components(target.parent)
                    if any(self.empty_hooks_dir.iterdir()):
                        raise InvariantError("owned empty hooks directory is not empty")
                    self._git(
                        "worktree", "add", str(target), branch_name,
                        empty_hooks=True,
                    )
                elif not target.exists() or registered is None:
                    raise InvariantError("worktree path/registry 出現不完整 identity")
                self._fault("create.after_worktree")
                observation = self.observe_worktree(task)
                if (not observation["exists"] or not observation["registered"]
                        or observation["head"] != base or observation["head_ref"] != task_ref
                        or observation["status"] or observation["locked"]
                        or observation["live_locks"]):
                    raise InvariantError("建立後 worktree identity/clean/ref invariant 不符")
                receipt = self._write_receipt(receipt_path, {
                    **authority,
                    "observation_token": observation["observation_token"],
                    "created_at": _now(),
                })
                self._fault("create.after_receipt")
                intent.update({
                    "state": "committed", "committed_at": _now(),
                    "receipt_hash": receipt["receipt_hash"],
                })
                self._atomic_json(intent_path, intent)
                status = "created"
        return {
            "operation": Operation.CREATE_WORKTREE.value,
            "operation_id": operation_id,
            "status": status,
            "task": task,
            "task_ref": task_ref,
            "worker_repo": str(target),
            "head": observation["head"],
            "observation_token": observation["observation_token"],
            "receipt_hash": receipt.get("receipt_hash"),
        }

    def _claimed_gate_request(self, task: int, request: dict) -> dict:
        expected = request["expected"]
        try:
            spool = parallel_spool.DurableSpool(
                self.run_dir / "requests", responses_root=self.run_dir / "responses")
            record = spool.get_request(expected["request_id"])
        except parallel_spool.SpoolError as exc:
            raise InvariantError(f"claimed gate spool 損壞:{exc}") from exc
        if record is None or record.state != "claimed":
            state = None if record is None else record.state
            raise AuthorityError(f"GATE_MERGE request 必須已 claimed，目前為 {state}")
        artifact = record.payload
        fields = {
            "schema", "run_id", "task", "request_id", "validated_sha",
            "validated_round", "run_config_hash", "launch_spec_hash",
            "manifest_hash", "deadline_at",
        }
        _exact_dict(artifact, fields, "claimed gate request")
        assignment = self._assignment(task)
        required = {
            "schema": 1,
            "run_id": self.spec.run_id,
            "task": task,
            "request_id": expected["request_id"],
            "validated_sha": expected["validated_sha"],
            "validated_round": expected["validated_round"],
            "run_config_hash": assignment.run_config_hash,
            "launch_spec_hash": assignment.launch_spec_hash,
            "manifest_hash": self.spec.manifest_hash,
        }
        self._artifact_matches(artifact, required, "claimed gate request")
        request_hash = parallel_state.canonical_json_hash(artifact)
        if (not isinstance(artifact["deadline_at"], str)
                or not artifact["deadline_at"].strip()
                or request_hash != request["authority"]["request_hash"]):
            raise AuthorityError("claimed request hash/deadline 不符")
        return {**artifact, "request_hash": request_hash}

    def _validated_run_artifacts(self) -> parallel_state.ValidatedRunArtifacts:
        try:
            artifacts = parallel_state.validate_run_artifacts(
                self.run_dir, workspace_root=self.workspace_root)
        except parallel_state.ParallelStateError as exc:
            raise InvariantError(f"immutable run artifacts 不合法:{exc}") from exc
        expected_primary_ref = artifacts.manifest["integration_branch"]
        if not expected_primary_ref.startswith("refs/heads/"):
            expected_primary_ref = f"refs/heads/{expected_primary_ref}"
        if (artifacts.manifest_hash != self.spec.manifest_hash
                or artifacts.manifest["run_id"] != self.spec.run_id
                or artifacts.manifest["integration_start_sha"] != self.spec.integration_start_sha
                or expected_primary_ref != self.spec.primary_ref):
            raise InvariantError("immutable run manifest 與 RepoExecutor spec 不符")
        for order, assignment in self.spec.assignments.items():
            if (artifacts.assignment_hashes.get(order) != assignment.assignment_hash
                    or artifacts.run_config_hash != assignment.run_config_hash
                    or assignment.launch_spec_hash != assignment.assignment_hash):
                raise InvariantError(f"task-{order} immutable assignment authority 不符")
        return artifacts

    def _canonical_receipt_chain(self):
        try:
            artifacts, chain = parallel_state.load_receipt_chain(
                self.run_dir, workspace_root=self.workspace_root)
        except parallel_state.ParallelStateError as exc:
            raise InvariantError(f"canonical run receipt chain 損壞:{exc}") from exc
        # load_receipt_chain revalidates the graph; bind it to this executor too.
        if artifacts.manifest_hash != self.spec.manifest_hash:
            raise InvariantError("canonical receipt manifest authority 不符")
        return artifacts, chain

    @staticmethod
    def _require_canonical_receipt(receipt: dict, authority: dict, task: int) -> None:
        expected = {
            "schema_version": 1,
            "run_id": authority["run_id"],
            "manifest_hash": authority["manifest_hash"],
            "assignment_hash": authority["assignment_hash"],
            "task": task,
            "request_id": authority["request_id"],
            "integration_before": authority["integration_before"],
            "validated_sha": authority["validated_sha"],
            "validated_round": authority["validated_round"],
        }
        for field, value in expected.items():
            if receipt.get(field) != value:
                raise InvariantError(f"canonical run receipt.{field} authority 不符")

    def _load_canonical_receipt(self, authority: dict, task: int) -> dict:
        _artifacts, chain = self._canonical_receipt_chain()
        matches = [receipt for receipt in chain if receipt["task"] == task]
        if len(matches) != 1:
            raise InvariantError("common-dir receipt 缺少唯一 canonical run receipt")
        receipt = matches[0]
        self._require_canonical_receipt(receipt, authority, task)
        return receipt

    def _write_or_verify_canonical_receipt(self, authority: dict, task: int) -> dict:
        artifacts, chain = self._canonical_receipt_chain()
        existing = [receipt for receipt in chain if receipt["task"] == task]
        if existing:
            if len(existing) != 1:
                raise InvariantError("canonical receipt task 重複")
            self._require_canonical_receipt(existing[0], authority, task)
            return existing[0]
        expected_before = (chain[-1]["validated_sha"] if chain
                           else artifacts.manifest["integration_start_sha"])
        if authority["integration_before"] != expected_before:
            raise InvariantError("canonical receipt integration chain 不連續")
        previous_hash = (parallel_state.canonical_json_hash(chain[-1]) if chain else None)
        receipt = {
            "schema_version": 1,
            "run_id": authority["run_id"],
            "manifest_hash": authority["manifest_hash"],
            "assignment_hash": authority["assignment_hash"],
            "task": task,
            "request_id": authority["request_id"],
            "sequence": len(chain) + 1,
            "previous_receipt_hash": previous_hash,
            "integration_before": authority["integration_before"],
            "validated_sha": authority["validated_sha"],
            "validated_round": authority["validated_round"],
        }
        try:
            parallel_state.write_or_verify_immutable_json(
                artifacts.run_dir, f"receipts/task-{task}.json", receipt)
            _verified_artifacts, verified = parallel_state.load_receipt_chain(
                artifacts.run_dir, workspace_root=self.workspace_root)
        except parallel_state.ParallelStateError as exc:
            raise InvariantError(f"canonical run receipt publish 失敗:{exc}") from exc
        if not verified or verified[-1] != receipt:
            raise InvariantError("canonical run receipt publish 後 chain 驗證失敗")
        return receipt

    def _gate_authority(self, operation_id: str, task: int, request: dict,
                        claimed: dict) -> dict:
        expected = request["expected"]
        assignment = self._assignment(task)
        return {
            "schema_version": 1,
            "kind": "GATE_MERGE",
            "operation_id": operation_id,
            "run_id": self.spec.run_id,
            "task": task,
            "request_id": expected["request_id"],
            "request_hash": claimed["request_hash"],
            "manifest_hash": self.spec.manifest_hash,
            "assignment_hash": assignment.assignment_hash,
            "run_config_hash": assignment.run_config_hash,
            "launch_spec_hash": assignment.launch_spec_hash,
            "integration_before": expected["integration_before"],
            "integration_ref": self.spec.primary_ref,
            "sync_before": expected["sync_before"],
            "sync_ref": self.sync_ref,
            "task_ref": self.task_ref(task),
            "validated_sha": expected["validated_sha"],
            "validated_round": expected["validated_round"],
        }

    def _verify_gate_worker(self, task: int, validated_sha: str) -> dict:
        observation = self.observe_worktree(task)
        if (not observation["exists"] or not observation["registered"]
                or observation["head_ref"] != self.task_ref(task)
                or observation["head"] != validated_sha or observation["status"]
                or observation["locked"] or observation["live_locks"]):
            raise InvariantError("gate worker 必須是 exact validated SHA/clean/task ref")
        if self._ref_tip(self.task_ref(task)) != validated_sha:
            raise InvariantError("gate task ref tip 與 validated SHA 不一致")
        return observation

    def _verify_existing_gate_receipt(self, receipt: dict, intent: dict,
                                      authority: dict, task: int) -> dict:
        self._artifact_matches(receipt, authority, "gate receipt")
        body = {key: value for key, value in receipt.items() if key != "receipt_hash"}
        if canonical_hash(body) != receipt.get("receipt_hash"):
            raise InvariantError("gate receipt hash 不符")
        canonical_receipt = self._load_canonical_receipt(authority, task)
        canonical_receipt_hash = parallel_state.canonical_json_hash(canonical_receipt)
        if receipt.get("canonical_receipt_hash") != canonical_receipt_hash:
            raise InvariantError("common-dir receipt 與 canonical run receipt 不符")
        primary = self._require_primary()
        validated = authority["validated_sha"]
        if not self._is_ancestor(validated, primary["head"]):
            raise InvariantError("primary HEAD 不含 receipt validated SHA")
        if self._ref_tip(self.sync_ref) != primary["head"]:
            raise InvariantError("sync ref 不等於最新 primary receipt tip")
        observation = self._verify_gate_worker(task, validated)
        if intent.get("state") == "prepared":
            intent.update({
                "state": "committed", "committed_at": _now(),
                "receipt_hash": receipt["receipt_hash"],
            })
            self._atomic_json(
                self._intent_path("gate", authority["request_id"]), intent,
            )
        elif (intent.get("state") != "committed"
              or intent.get("receipt_hash") != receipt["receipt_hash"]):
            raise InvariantError("gate committed intent 與 receipt 不符")
        return observation

    def _gate_merge(self, operation_id: str, task: int, request: dict) -> dict:
        self._validated_run_artifacts()
        claimed = self._claimed_gate_request(task, request)
        authority = self._gate_authority(operation_id, task, request, claimed)
        expected = request["expected"]
        validated = expected["validated_sha"]
        before = expected["integration_before"]
        sync_before = expected["sync_before"]
        request_id = expected["request_id"]
        intent_path = self._intent_path("gate", request_id)
        receipt_path = self._receipt_path("gate", request_id)

        with self._merge_lock():
            # Re-read claimed state inside the Git critical section.  A Stage 3
            # spool transition may only move this file after it is terminal.
            claimed = self._claimed_gate_request(task, request)
            self._artifact_matches(claimed, {
                "request_hash": authority["request_hash"],
            }, "claimed gate request")
            if os.path.lexists(receipt_path):
                if not os.path.lexists(intent_path):
                    raise InvariantError("gate receipt 缺少 prepared intent")
                intent = self._read_json(intent_path, "gate intent")
                self._artifact_matches(intent, authority, "gate intent")
                receipt = self._read_json(receipt_path, "gate receipt")
                observation = self._verify_existing_gate_receipt(
                    receipt, intent, authority, task,
                )
                return {
                    "operation": Operation.GATE_MERGE.value,
                    "operation_id": operation_id,
                    "status": "already-merged",
                    "task": task,
                    "request_id": request_id,
                    "validated_sha": validated,
                    "validated_round": expected["validated_round"],
                    "receipt_hash": receipt["receipt_hash"],
                    "observation_token": observation["observation_token"],
                }

            if os.path.lexists(intent_path):
                intent = self._read_json(intent_path, "gate intent")
                self._artifact_matches(intent, authority, "gate intent")
                if intent.get("state") == "committed":
                    raise InvariantError("committed gate intent 缺少 receipt")
                if intent.get("state") != "prepared":
                    raise InvariantError("gate intent state 不合法")
            else:
                primary = self._require_primary(head=before)
                del primary
                if self._ref_tip(self.sync_ref) != sync_before or sync_before != before:
                    raise InvariantError("gate expected integration/sync chain 不一致")
                self._verify_gate_worker(task, validated)
                if not self._is_ancestor(before, validated):
                    return {
                        "operation": Operation.GATE_MERGE.value,
                        "operation_id": operation_id,
                        "status": "stale-integration",
                        "task": task,
                        "request_id": request_id,
                        "validated_sha": validated,
                        "validated_round": expected["validated_round"],
                        "receipt_hash": None,
                        "observation_token": None,
                    }
                intent = {**authority, "state": "prepared", "prepared_at": _now()}
                self._atomic_json(intent_path, intent)
            self._fault("gate.after_prepared")

            primary = self._require_primary()
            sync_tip = self._ref_tip(self.sync_ref)
            head = primary["head"]
            if head == before and sync_tip == sync_before:
                self._verify_gate_worker(task, validated)
                if not self._is_ancestor(before, validated):
                    raise InvariantError("prepared gate 已不再可 ff-only")
                if any(self.empty_hooks_dir.iterdir()):
                    raise InvariantError("owned empty hooks directory 不為空")
                self._git(
                    "merge", "--ff-only", "--no-edit", validated,
                    empty_hooks=True,
                )
                primary = self._require_primary(head=validated)
                del primary
                head = validated
            elif head == validated and sync_tip in {sync_before, validated}:
                self._verify_gate_worker(task, validated)
            else:
                raise InvariantError(
                    "prepared gate recovery HEAD/sync 組合不在 safe matrix"
                )
            self._fault("gate.after_merge")

            sync_tip = self._ref_tip(self.sync_ref)
            if sync_tip == sync_before:
                self._git("update-ref", self.sync_ref, validated, sync_before)
            elif sync_tip != validated:
                raise InvariantError("gate sync ref CAS 前已被未知 actor 移動")
            self._fault("gate.after_sync")
            self._require_primary(head=validated)
            if self._ref_tip(self.sync_ref) != validated:
                raise InvariantError("gate merge 後 sync ref 未落在 validated SHA")

            canonical_receipt = self._write_or_verify_canonical_receipt(authority, task)
            canonical_receipt_hash = parallel_state.canonical_json_hash(canonical_receipt)
            self._fault("gate.after_run_receipt")
            receipt = self._write_receipt(receipt_path, {
                **authority,
                "canonical_receipt_hash": canonical_receipt_hash,
                "merged_at": _now(),
            })
            self._fault("gate.after_receipt")
            intent.update({
                "state": "committed", "committed_at": _now(),
                "receipt_hash": receipt["receipt_hash"],
            })
            self._atomic_json(intent_path, intent)
            observation = self._verify_gate_worker(task, validated)
        return {
            "operation": Operation.GATE_MERGE.value,
            "operation_id": operation_id,
            "status": "merged",
            "task": task,
            "request_id": request_id,
            "validated_sha": validated,
            "validated_round": expected["validated_round"],
            "receipt_hash": receipt["receipt_hash"],
            "observation_token": observation["observation_token"],
        }

    def _remove_worktree(self, operation_id: str, task: int, request: dict) -> dict:
        self._validated_run_artifacts()
        expected = request["expected"]
        assignment = self._assignment(task)
        task_ref = self.task_ref(task)
        target = self.worktree_path(task)
        authority_without_head = {
            "schema_version": 1,
            "kind": "REMOVE_WORKTREE",
            "operation_id": operation_id,
            "run_id": self.spec.run_id,
            "task": task,
            "manifest_hash": self.spec.manifest_hash,
            "assignment_hash": assignment.assignment_hash,
            "task_ref": task_ref,
            "worker_repo": str(target),
            "observation_token": expected["observation_token"],
            "terminal_outcome": expected["terminal_outcome"],
        }
        intent_path = self._intent_path("remove", operation_id)
        receipt_path = self._receipt_path("remove", operation_id)
        with self._merge_lock():
            if os.path.lexists(intent_path):
                intent = self._read_json(intent_path, "remove intent")
                self._artifact_matches(
                    intent, authority_without_head, "remove intent")
                observed_head = _require_sha(
                    intent.get("observed_head"), "remove intent observed_head")
                authority = {**authority_without_head, "observed_head": observed_head}
                if intent.get("state") not in {"prepared", "committed"}:
                    raise InvariantError("remove intent state is invalid")
            else:
                if os.path.lexists(receipt_path):
                    raise InvariantError("remove receipt has no prepared intent")
                observation = self.observe_worktree(task)
                if observation["observation_token"] != expected["observation_token"]:
                    raise InvariantError(
                        "worktree observation token expired; refusing TOCTOU cleanup")
                if (not observation["exists"] or not observation["registered"]
                        or observation["head_ref"] != task_ref
                        or observation["status"] or observation["locked"]
                        or observation["live_locks"]):
                    raise InvariantError(
                        "worktree dirty/locked/unregistered/ref identity cannot be cleaned")
                observed_head = _require_sha(observation["head"], "observed_head")
                authority = {**authority_without_head, "observed_head": observed_head}
                intent = {**authority, "state": "prepared", "prepared_at": _now()}
                self._atomic_json(intent_path, intent)

            if os.path.lexists(receipt_path):
                receipt = self._read_json(receipt_path, "remove receipt")
                self._artifact_matches(receipt, authority, "remove receipt")
                body = {key: value for key, value in receipt.items()
                        if key != "receipt_hash"}
                if canonical_hash(body) != receipt.get("receipt_hash"):
                    raise InvariantError("remove receipt hash mismatch")
                if (os.path.lexists(target) or target in self._worktree_records()
                        or self._ref_tip(task_ref) is not None):
                    raise InvariantError("remove receipt exists but resource remains")
                if intent["state"] == "prepared":
                    intent.update({
                        "state": "committed",
                        "committed_at": _now(),
                        "receipt_hash": receipt["receipt_hash"],
                    })
                    self._atomic_json(intent_path, intent)
                elif intent.get("receipt_hash") != receipt["receipt_hash"]:
                    raise InvariantError("remove committed intent/receipt mismatch")
                status = "already-removed"
            else:
                if intent["state"] == "committed":
                    raise InvariantError("committed remove intent has no receipt")
                self._fault("remove.after_prepared")
                records = self._worktree_records()
                path_present = os.path.lexists(target)
                registered = target in records
                if path_present and registered:
                    current = self.observe_worktree(task)
                    if (current["observation_token"] != expected["observation_token"]
                            or current["head"] != observed_head
                            or current["head_ref"] != task_ref
                            or current["status"] or current["locked"]
                            or current["live_locks"]):
                        raise InvariantError(
                            "cleanup critical-section observation changed")
                    self._git("worktree", "remove", str(target), empty_hooks=True)
                elif path_present or registered:
                    raise InvariantError(
                        "remove recovery path/registry identity is partial")
                self._fault("remove.after_worktree")
                if os.path.lexists(target) or target in self._worktree_records():
                    raise InvariantError(
                        "git worktree remove did not remove canonical resource")
                ref_tip = self._ref_tip(task_ref)
                if ref_tip == observed_head:
                    self._git("update-ref", "-d", task_ref, observed_head)
                elif ref_tip is not None:
                    raise InvariantError("cleanup task ref was moved by another actor")
                self._fault("remove.after_ref")
                if self._ref_tip(task_ref) is not None:
                    raise InvariantError("cleanup task ref CAS delete failed")
                receipt = self._write_receipt(receipt_path, {
                    **authority,
                    "removed_at": _now(),
                })
                self._fault("remove.after_receipt")
                intent.update({
                    "state": "committed", "committed_at": _now(),
                    "receipt_hash": receipt["receipt_hash"],
                })
                self._atomic_json(intent_path, intent)
                status = "removed"
        return {
            "operation": Operation.REMOVE_WORKTREE.value,
            "operation_id": operation_id,
            "status": status,
            "task": task,
            "task_ref": task_ref,
            "worker_repo": str(target),
            "terminal_outcome": expected["terminal_outcome"],
            "receipt_hash": receipt.get("receipt_hash"),
        }

    def _shutdown(self, operation_id: str, _task: None, request: dict) -> dict:
        del _task
        for path in self.intents_dir.glob("*.json"):
            intent = self._read_json(path, "shutdown journal audit")
            if intent.get("state") != "committed":
                raise InvariantError(f"SHUTDOWN 前仍有 prepared intent:{path.name}")
        return {
            "operation": Operation.SHUTDOWN.value,
            "operation_id": operation_id,
            "status": "shutdown",
            "supervisor_session": request["authority"]["supervisor_session"],
            "generation": request["authority"]["generation"],
        }
