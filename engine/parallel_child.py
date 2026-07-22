#!/usr/bin/env python3
"""Durable ACK-gated guardian for managed parallel workers.

The supervisor starts this process with a private stdin pipe, publishes a
``guardian_ready`` child record containing this process' exact identity and the
payload argv hash, then writes ``ACK_BYTE``.  The guardian starts an inert
bootstrap, attaches containment, durably publishes the bootstrap's exact
PID/birth-token/PGID as ``acked``, atomically claims the matching launch
reservation, publishes an ``authorized`` response bound to that PID, and only
then releases it to execute the real payload.  Thus a cancelled reservation
never runs payload code while a claimed launch is already exactly fenceable.

After ACK, stdin is also the supervisor-liveness lease.  EOF or a termination
signal recursively fences independently-sessioned descendants before the
guardian publishes ``reaped``.  Durable records contain only an argv hash;
full argv may contain the dispatch token and is never serialized.
"""

from __future__ import annotations

import argparse
import ctypes
import math
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import BinaryIO, Mapping, Sequence

from engine import parallel_contract
from engine import parallel_spool
from engine import parallel_state
from engine import parallel_worker
from engine import platform_compat as compat


ACK_BYTE = b"\x06"
PAYLOAD_GO_BYTE = b"\x07"
GUARDIAN_CANCELLED_RC = 125
GUARDIAN_PROTOCOL_RC = 64
GUARDIAN_SPAWN_RC = 126
CHILD_RECORD_SCHEMA = 2
CHILD_STATES = frozenset({"guardian_ready", "acked", "reaped"})
PAYLOAD_CONTAINMENTS = frozenset({
    "posix-exact-tree-v1",
    "windows-job-kill-on-close-v1",
    "windows-job-no-breakaway-v2",
})
WINDOWS_STRICT_PAYLOAD_CONTAINMENT = "windows-job-no-breakaway-v2"
WINDOWS_LEGACY_PAYLOAD_CONTAINMENT = "windows-job-kill-on-close-v1"
_CHILD_TRANSITIONS = {
    "guardian_ready": frozenset({"acked", "reaped"}),
    "acked": frozenset({"reaped"}),
}
_CHILD_RECORD_FIELDS = frozenset({
    "schema",
    "run_id",
    "task",
    "child_id",
    "supervisor_session",
    "supervisor_generation",
    "attempt",
    "resume",
    "guardian_pid",
    "guardian_start_token",
    "argv_hash",
    "payload_pid",
    "payload_start_token",
    "payload_group_id",
    "payload_containment",
    "state",
    "returncode",
})
_PAYLOAD_IDENTITY_FIELDS = frozenset({
    "payload_pid",
    "payload_start_token",
    "payload_group_id",
    "payload_containment",
})
_CHILD_IMMUTABLE_FIELDS = (
    _CHILD_RECORD_FIELDS - {"state", "returncode"} - _PAYLOAD_IDENTITY_FIELDS)


class ParallelChildError(RuntimeError):
    """A guardian launch or durable child record is unsafe or malformed."""


def _normalize_argv(argv: Sequence[object]) -> list[str]:
    if isinstance(argv, (str, bytes, bytearray)):
        raise ParallelChildError("payload argv must be a token sequence")
    try:
        raw_values = list(argv)
    except TypeError as exc:
        raise ParallelChildError("payload argv must be iterable") from exc
    values: list[str] = []
    for raw in raw_values:
        if not isinstance(raw, (str, os.PathLike)):
            raise ParallelChildError("payload argv tokens must be strings or paths")
        value = os.fspath(raw)
        if not isinstance(value, str) or "\x00" in value:
            raise ParallelChildError("payload argv tokens must be NUL-free text")
        values.append(value)
    if not values or not values[0]:
        raise ParallelChildError("payload argv must name an executable")
    return values


def _normalize_run_dir(run_dir: Path | str) -> Path:
    if not isinstance(run_dir, (str, os.PathLike)):
        raise ParallelChildError("run_dir must be a filesystem path")
    value = os.fspath(run_dir)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ParallelChildError("run_dir must be a non-empty NUL-free path")
    # Preserve linked ancestors for parallel_state's fail-closed validation.
    return Path(os.path.abspath(value))


def payload_argv_hash(argv: Sequence[object]) -> str:
    """Return the canonical hash stored in child records, never the argv."""
    return parallel_state.canonical_json_hash(_normalize_argv(argv))


def _require_positive_int(value: object, label: str, *, minimum: int = 1) -> int:
    if (not isinstance(value, int) or isinstance(value, bool)
            or value < minimum):
        raise ParallelChildError(f"{label} must be an integer >= {minimum}")
    return value


def _require_session(value: object) -> str:
    if (not isinstance(value, str) or len(value) != 32
            or any(ch not in "0123456789abcdef" for ch in value)):
        raise ParallelChildError(
            "supervisor_session must be 32 lowercase hexadecimal characters")
    return value


def _require_start_token(value: object, label: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 256
            or "\x00" in value or any(ord(ch) < 0x20 for ch in value)):
        raise ParallelChildError(
            f"{label} must be a non-empty printable OS identity token")
    return value


def _require_child_id(value: object) -> str:
    try:
        return parallel_spool.require_request_id(value)
    except parallel_spool.SpoolError as exc:
        raise ParallelChildError(str(exc)) from exc


def validate_child_record(value: object) -> dict:
    """Validate and detach one exact durable guardian record."""
    if not isinstance(value, Mapping) or set(value) != _CHILD_RECORD_FIELDS:
        raise ParallelChildError("child record has unexpected or missing fields")
    record = dict(value)
    if record["schema"] != CHILD_RECORD_SCHEMA:
        raise ParallelChildError("unsupported child record schema")
    try:
        record["run_id"] = parallel_contract.require_run_id(record["run_id"])
        record["argv_hash"] = parallel_contract.require_config_hash(
            record["argv_hash"], "argv_hash")
    except parallel_contract.ParallelContractError as exc:
        raise ParallelChildError(str(exc)) from exc
    record["child_id"] = _require_child_id(record["child_id"])
    record["task"] = _require_positive_int(record["task"], "task")
    record["supervisor_session"] = _require_session(
        record["supervisor_session"])
    record["supervisor_generation"] = _require_positive_int(
        record["supervisor_generation"], "supervisor_generation")
    record["attempt"] = _require_positive_int(
        record["attempt"], "attempt", minimum=0)
    if not isinstance(record["resume"], bool):
        raise ParallelChildError("resume must be boolean")
    record["guardian_pid"] = _require_positive_int(
        record["guardian_pid"], "guardian_pid", minimum=2)
    record["guardian_start_token"] = _require_start_token(
        record["guardian_start_token"], "guardian_start_token")

    payload_values = [record[field] for field in _PAYLOAD_IDENTITY_FIELDS]
    has_payload_identity = all(value is not None for value in payload_values)
    if any(value is not None for value in payload_values) and not has_payload_identity:
        raise ParallelChildError("payload identity fields must be all null or all set")
    if has_payload_identity:
        record["payload_pid"] = _require_positive_int(
            record["payload_pid"], "payload_pid", minimum=2)
        record["payload_group_id"] = _require_positive_int(
            record["payload_group_id"], "payload_group_id", minimum=2)
        record["payload_start_token"] = _require_start_token(
            record["payload_start_token"], "payload_start_token")
        if record["payload_containment"] not in PAYLOAD_CONTAINMENTS:
            raise ParallelChildError("unsupported payload containment contract")

    state = record["state"]
    if state not in CHILD_STATES:
        raise ParallelChildError(f"unsupported child state: {state!r}")
    if state == "guardian_ready" and has_payload_identity:
        raise ParallelChildError(
            "guardian_ready child cannot claim a payload identity")
    if state == "acked" and not has_payload_identity:
        raise ParallelChildError(
            "acked child must contain an exact payload identity")
    returncode = record["returncode"]
    if state == "reaped":
        if not isinstance(returncode, int) or isinstance(returncode, bool):
            raise ParallelChildError("reaped child must have an integer returncode")
    elif returncode is not None:
        raise ParallelChildError(
            "guardian_ready and acked child returncode must be null")
    return record


def child_record(
    *,
    run_id: str,
    task: int,
    child_id: str,
    supervisor_session: str,
    supervisor_generation: int,
    attempt: int,
    resume: bool,
    guardian_pid: int,
    argv_hash: str,
    state: str,
    returncode: int | None = None,
    guardian_start_token: str | None = None,
    payload_pid: int | None = None,
    payload_start_token: str | None = None,
    payload_group_id: int | None = None,
    payload_containment: str | None = None,
) -> dict:
    """Build one validated child record without retaining secret argv."""
    if guardian_start_token is None:
        guardian_start_token = compat.process_start_token(guardian_pid)
    if guardian_start_token is None:
        raise ParallelChildError("guardian exact process identity is unavailable")
    return validate_child_record({
        "schema": CHILD_RECORD_SCHEMA,
        "run_id": run_id,
        "task": task,
        "child_id": child_id,
        "supervisor_session": supervisor_session,
        "supervisor_generation": supervisor_generation,
        "attempt": attempt,
        "resume": resume,
        "guardian_pid": guardian_pid,
        "guardian_start_token": guardian_start_token,
        "argv_hash": argv_hash,
        "payload_pid": payload_pid,
        "payload_start_token": payload_start_token,
        "payload_group_id": payload_group_id,
        "payload_containment": payload_containment,
        "state": state,
        "returncode": returncode,
    })


def _child_relative_path(task: int, child_id: str) -> Path:
    return Path("children") / f"task-{task}" / f"{child_id}.json"


def _missing_artifact_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, FileNotFoundError):
            return True
        current = current.__cause__
    return False


def read_child_record(
    run_dir: Path | str,
    task: int,
    child_id: str,
) -> dict:
    """Read one canonical child record and bind filename identity to payload."""
    root = _normalize_run_dir(run_dir)
    task = _require_positive_int(task, "task")
    child_id = _require_child_id(child_id)
    relative = _child_relative_path(task, child_id)
    try:
        value = parallel_state.read_canonical_json(root, relative)
    except parallel_state.ParallelStateError as exc:
        raise ParallelChildError(f"cannot read child record: {exc}") from exc
    record = validate_child_record(value)
    if record["task"] != task or record["child_id"] != child_id:
        raise ParallelChildError(
            "child record filename identity does not match its payload")
    return record


def write_child_record(
    run_dir: Path | str,
    value: Mapping[str, object],
) -> Path:
    """Write one legal mutable child transition as canonical JSON.

    Creation is restricted to ``guardian_ready``.  Existing records may only
    advance ``guardian_ready -> acked -> reaped``.  A supervisor that has
    locally waited a pre-ACK guardian may terminalize it directly with
    ``guardian_ready -> reaped`` because the payload barrier never opened.
    Replaying exactly the same state is idempotent; a same-state payload change
    is a conflict.
    """
    root = _normalize_run_dir(run_dir)
    record = validate_child_record(value)
    relative = _child_relative_path(record["task"], record["child_id"])
    try:
        existing_value = parallel_state.read_canonical_json(root, relative)
    except parallel_state.ParallelStateError as exc:
        if not _missing_artifact_error(exc):
            raise ParallelChildError(f"cannot read child record: {exc}") from exc
        existing = None
    else:
        existing = validate_child_record(existing_value)

    if existing is None:
        if record["state"] != "guardian_ready":
            raise ParallelChildError(
                "child record creation must start at guardian_ready")
    else:
        if (existing["task"] != record["task"]
                or existing["child_id"] != record["child_id"]):
            raise ParallelChildError(
                "child record path identity does not match its payload")
        for field in _CHILD_IMMUTABLE_FIELDS:
            if existing[field] != record[field]:
                raise ParallelChildError(
                    f"child record immutable field conflict: {field}")
        if existing["state"] == "acked":
            for field in _PAYLOAD_IDENTITY_FIELDS:
                if existing[field] != record[field]:
                    raise ParallelChildError(
                        f"child record payload identity conflict: {field}")
        elif existing["state"] == "guardian_ready" and record["state"] == "reaped":
            if any(record[field] is not None for field in _PAYLOAD_IDENTITY_FIELDS):
                raise ParallelChildError(
                    "pre-ACK reap cannot introduce a payload identity")
        if existing["state"] == record["state"]:
            if existing != record:
                raise ParallelChildError(
                    "child record same-state replay differs from durable state")
            return root / relative
        allowed = _CHILD_TRANSITIONS.get(existing["state"], frozenset())
        if record["state"] not in allowed:
            raise ParallelChildError(
                f"illegal child transition: {existing['state']} -> {record['state']}")

    try:
        path = parallel_state.atomic_write_json(root, relative, record)
        persisted = parallel_state.read_canonical_json(root, relative)
    except parallel_state.ParallelStateError as exc:
        raise ParallelChildError(f"cannot write child record: {exc}") from exc
    if validate_child_record(persisted) != record:
        raise ParallelChildError("child record durability verification failed")
    return path


def build_guardian_argv(
    python_executable: str | os.PathLike[str],
    run_dir: Path | str,
    task: int,
    child_id: str,
    payload_argv: Sequence[object],
) -> list[str]:
    """Build the canonical guardian command with its durable record identity."""
    executable = _normalize_argv([python_executable])[0]
    root = _normalize_run_dir(run_dir)
    task = _require_positive_int(task, "task")
    child_id = _require_child_id(child_id)
    payload = _normalize_argv(payload_argv)
    # The immediately following guardian Popen must escape a supervisor-owned
    # Windows kill-on-close Job.  The request is thread-local and one-shot;
    # payload descendants do not inherit this launch policy.
    compat.request_process_group_breakaway()
    return [
        executable,
        "-m",
        "engine.parallel_child",
        "--run-dir",
        str(root),
        "--task",
        str(task),
        "--child-id",
        child_id,
        "--",
        *payload,
    ]


def _read_control_byte(control_stream: BinaryIO):
    """Read without leaving CPython's buffered-stdin lock held at shutdown."""
    try:
        descriptor = control_stream.fileno()
    except (AttributeError, OSError, ValueError):
        return control_stream.read(1)
    return os.read(descriptor, 1)


def _watch_parent(control_stream: BinaryIO, parent_lost: threading.Event) -> None:
    try:
        while True:
            chunk = _read_control_byte(control_stream)
            if chunk in (b"", ""):
                parent_lost.set()
                return
    except (OSError, ValueError):
        parent_lost.set()


def _terminate_payload(process: subprocess.Popen) -> int:
    if process.poll() is None:
        try:
            fenced = compat.fence_process_tree(
                process, graceful_timeout=5, force_timeout=5)
        except (OSError, ProcessLookupError, ValueError):
            fenced = False
        if not fenced and process.poll() is None:
            try:
                compat.kill_process_group(process)
            except (OSError, ProcessLookupError, ValueError):
                try:
                    process.kill()
                except (OSError, ProcessLookupError):
                    pass
    try:
        return int(compat.wait_process(process, timeout=5))
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            return int(compat.wait_process(process, timeout=5))
        except subprocess.TimeoutExpired as exc:
            # Without an observed exit the guardian cannot publish durable
            # reaped proof.  Propagating leaves the ACKed child record intact
            # for a later exact recovery owner instead of hanging forever.
            raise ParallelChildError(
                "payload did not exit after bounded force fence") from exc


def _enable_posix_subreaper() -> bool:
    """Make this guardian the verified adoption root for daemonised payloads."""
    if compat.IS_WINDOWS:
        return True
    if not sys.platform.startswith("linux") or not os.path.isdir("/proc"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = getattr(libc, "prctl")
        prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
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


def _posix_process_stat(pid: int) -> dict | None:
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="ascii") as stream:
            raw = stream.read()
        fields = raw[raw.rfind(")") + 2:].split()
        if len(fields) < 20:
            return None
        return {
            "pid": int(pid),
            "state": fields[0],
            "ppid": int(fields[1]),
            "token": fields[19],
        }
    except (OSError, IndexError, ValueError):
        return None


def _posix_contained_processes() -> dict[int, dict] | None:
    """Return every exact descendant adopted below the resident guardian."""
    guardian_pid = os.getpid()
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    table = {}
    for name in names:
        if not name.isdigit():
            continue
        info = _posix_process_stat(int(name))
        if info is not None:
            table[info["pid"]] = info
    if guardian_pid not in table:
        return None
    children: dict[int, list[int]] = {}
    for pid, info in table.items():
        children.setdefault(info["ppid"], []).append(pid)
    found: set[int] = set()
    pending = [guardian_pid]
    while pending:
        parent = pending.pop()
        for child in children.get(parent, ()):
            if child != guardian_pid and child not in found:
                found.add(child)
                pending.append(child)
    return {pid: table[pid] for pid in found}


def _posix_exact_alive(identity: Mapping[str, object]) -> bool:
    current = _posix_process_stat(int(identity["pid"]))
    return bool(
        current is not None
        and current["token"] == identity["token"]
        and current["state"] != "Z"
    )


def _signal_posix_snapshot(
    snapshot: Mapping[tuple[int, str], Mapping[str, object]], sig,
) -> None:
    for identity in tuple(snapshot.values()):
        if not _posix_exact_alive(identity):
            continue
        try:
            os.kill(int(identity["pid"]), sig)
        except (OSError, ProcessLookupError, PermissionError):
            pass


def _reap_posix_children() -> None:
    while True:
        try:
            waited, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if waited == 0:
            return


def _fence_posix_adopted_descendants() -> bool:
    """Freeze, rescan, and reap the subreaper guardian's complete child tree."""
    snapshot: dict[tuple[int, str], dict] = {}

    def merge(current: Mapping[int, Mapping[str, object]]) -> None:
        for identity in current.values():
            key = (int(identity["pid"]), str(identity["token"]))
            snapshot[key] = dict(identity)

    def discover_and_stop() -> bool:
        for _iteration in range(16):
            current = _posix_contained_processes()
            if current is None:
                return False
            previous = set(snapshot)
            merge(current)
            _signal_posix_snapshot(snapshot, signal.SIGSTOP)
            verify = _posix_contained_processes()
            if verify is None:
                return False
            merge(verify)
            if set(snapshot) == previous:
                return True
        return False

    if not discover_and_stop():
        return False
    # The payload leader has already exited or gone through _terminate_payload.
    # Any adopted process still present is an escaped background mutator.  Keep
    # the stable snapshot frozen and force-kill it; resuming for a graceful
    # signal would reopen a fork-between-scans race before durable publication.
    _signal_posix_snapshot(snapshot, signal.SIGKILL)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        current = _posix_contained_processes()
        if current is None:
            return False
        merge(current)
        _signal_posix_snapshot(snapshot, signal.SIGSTOP)
        _signal_posix_snapshot(snapshot, signal.SIGKILL)
        _reap_posix_children()
        verify = _posix_contained_processes()
        if verify is None:
            return False
        merge(verify)
        if not any(_posix_exact_alive(value) for value in snapshot.values()):
            _reap_posix_children()
            return True
        time.sleep(0.02)
    return not any(_posix_exact_alive(value) for value in snapshot.values())


def _install_signal_handlers(parent_lost: threading.Event):
    if threading.current_thread() is not threading.main_thread():
        return {}
    previous = {}

    def request_shutdown(_signum, _frame):
        parent_lost.set()

    candidates = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        candidates.append(signal.SIGBREAK)
    for candidate in candidates:
        try:
            previous[candidate] = signal.getsignal(candidate)
            signal.signal(candidate, request_shutdown)
        except (OSError, ValueError):
            previous.pop(candidate, None)
    return previous


def _restore_signal_handlers(previous) -> None:
    for candidate, handler in previous.items():
        try:
            signal.signal(candidate, handler)
        except (OSError, ValueError):
            pass


def _validate_guardian_ready(
    run_dir: Path,
    task: int,
    child_id: str,
    argv_hash: str,
) -> dict:
    record = read_child_record(run_dir, task, child_id)
    if record["state"] != "guardian_ready":
        raise ParallelChildError(
            "guardian ACK requires a guardian_ready child record")
    if record["guardian_pid"] != os.getpid():
        raise ParallelChildError("guardian pid does not match child record")
    if not compat.process_matches_identity(
            os.getpid(), record["guardian_start_token"]):
        raise ParallelChildError(
            "guardian OS identity does not match child record")
    if record["argv_hash"] != argv_hash:
        raise ParallelChildError("payload argv hash does not match child record")
    return record


def _acked_record(
    run_dir: Path,
    task: int,
    child_id: str,
    argv_hash: str,
    payload_identity: Mapping[str, object],
) -> dict:
    record = _validate_guardian_ready(run_dir, task, child_id, argv_hash)
    acked = dict(record)
    try:
        acked["payload_pid"] = int(payload_identity["pid"])
        acked["payload_start_token"] = str(payload_identity["start_token"])
        acked["payload_group_id"] = int(payload_identity["group_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ParallelChildError("payload exact identity is malformed") from exc
    acked["payload_containment"] = (
        WINDOWS_STRICT_PAYLOAD_CONTAINMENT
        if compat.IS_WINDOWS else "posix-exact-tree-v1")
    acked["state"] = "acked"
    write_child_record(run_dir, acked)
    if read_child_record(run_dir, task, child_id) != acked:
        raise ParallelChildError("acked child record verification failed")
    return acked


def _publish_reaped(run_dir: Path, acked: Mapping[str, object], rc: int) -> None:
    reaped = dict(acked)
    reaped["state"] = "reaped"
    reaped["returncode"] = int(rc)
    write_child_record(run_dir, reaped)
    if read_child_record(
            run_dir, int(reaped["task"]), str(reaped["child_id"])) != reaped:
        raise ParallelChildError("reaped child record verification failed")


def recover_acked_child(
    run_dir: Path | str,
    task: int,
    child_id: str,
    *,
    expected_record: Mapping[str, object],
    returncode: int = GUARDIAN_CANCELLED_RC,
) -> dict:
    """Fence and terminalize one orphaned ACKed payload by exact identity.

    This is intentionally a compare-and-fence API for a recovery owner.  The
    caller must supply the complete ACKed record it audited; any durable change
    fails closed.  A still-live exact guardian also fails closed.  PID reuse is
    never signalled because both guardian and payload identities include an OS
    birth token, and the payload additionally binds its process-group id.

    POSIX recovery requires the exact payload root to remain alive so its full
    descendant tree can be enumerated and fenced.  If that root disappeared
    before recovery, independently-sessioned descendants cannot be proven
    absent and the record remains ACKed.  On Windows, recovery is safe only
    for the recorded no-breakaway Job contract after the exact guardian has
    exited; kernel Job closure then proves all descendants were killed.  The
    legacy breakaway-capable contract remains readable but is never accepted
    as recovery proof for a nonterminal payload.
    """
    root = _normalize_run_dir(run_dir)
    task = _require_positive_int(task, "task")
    child_id = _require_child_id(child_id)
    expected = validate_child_record(expected_record)
    durable = read_child_record(root, task, child_id)
    if expected != durable:
        raise ParallelChildError(
            "child recovery compare failed: durable record changed")
    if durable["task"] != task or durable["child_id"] != child_id:
        raise ParallelChildError("child recovery identity does not match path")
    if durable["state"] == "reaped":
        return durable
    if durable["state"] != "acked":
        raise ParallelChildError("only an ACKed child can be recovered")
    if not isinstance(returncode, int) or isinstance(returncode, bool):
        raise ParallelChildError("recovery returncode must be an integer")

    if compat.process_matches_identity(
            durable["guardian_pid"], durable["guardian_start_token"]):
        raise ParallelChildError(
            "exact guardian is still alive; recovery ownership is unsafe")

    if (compat.IS_WINDOWS
            and durable["payload_containment"]
            != WINDOWS_STRICT_PAYLOAD_CONTAINMENT):
        raise ParallelChildError(
            "legacy Windows payload containment is not recovery proof")

    payload_alive = compat.process_matches_identity(
        durable["payload_pid"],
        durable["payload_start_token"],
        durable["payload_group_id"],
    )
    if payload_alive:
        fenced = compat.fence_process_tree(
            durable["payload_pid"],
            start_token=durable["payload_start_token"],
            group_id=durable["payload_group_id"],
        )
        if not fenced:
            raise ParallelChildError(
                "exact payload tree could not be proven fenced")
    elif not (
        compat.IS_WINDOWS
        and durable["payload_containment"]
        == WINDOWS_STRICT_PAYLOAD_CONTAINMENT
    ):
        raise ParallelChildError(
            "POSIX payload root disappeared before descendant fencing proof")

    if compat.process_matches_identity(
            durable["payload_pid"], durable["payload_start_token"],
            durable["payload_group_id"]):
        raise ParallelChildError("payload identity remains live after fence")
    # Re-read immediately before the irreversible durable transition.  This is
    # a second CAS check against concurrent guardian/recovery publication.
    if read_child_record(root, task, child_id) != durable:
        raise ParallelChildError(
            "child recovery compare failed before terminal publication")
    reaped = dict(durable)
    reaped["state"] = "reaped"
    reaped["returncode"] = int(returncode)
    write_child_record(root, reaped)
    persisted = read_child_record(root, task, child_id)
    if persisted != reaped:
        raise ParallelChildError("recovered reap durability verification failed")
    return persisted


def recover_orphan_child(
    run_dir: Path | str,
    task: int,
    child_id: str,
    *,
    expected_record: Mapping[str, object],
    returncode: int = GUARDIAN_CANCELLED_RC,
) -> dict:
    """Recover a dead guardian at either durable pre-payload boundary.

    ``guardian_ready`` proves that the payload bootstrap was never durably
    released: the guardian must publish ``acked`` before sending the payload
    GO byte.  Once the exact guardian identity is gone, that state can
    therefore be terminalized without guessing a payload PID.  ``acked``
    delegates to the stricter process-tree/Job fencing path above.
    """
    root = _normalize_run_dir(run_dir)
    task = _require_positive_int(task, "task")
    child_id = _require_child_id(child_id)
    expected = validate_child_record(expected_record)
    durable = read_child_record(root, task, child_id)
    if expected != durable:
        raise ParallelChildError(
            "child recovery compare failed: durable record changed")
    if durable["task"] != task or durable["child_id"] != child_id:
        raise ParallelChildError("child recovery identity does not match path")
    if durable["state"] == "reaped":
        return durable
    if durable["state"] == "acked":
        return recover_acked_child(
            root, task, child_id, expected_record=durable,
            returncode=returncode)
    if durable["state"] != "guardian_ready":
        raise ParallelChildError("unsupported orphan child state")
    if not isinstance(returncode, int) or isinstance(returncode, bool):
        raise ParallelChildError("recovery returncode must be an integer")
    if compat.process_matches_identity(
            durable["guardian_pid"], durable["guardian_start_token"]):
        raise ParallelChildError(
            "exact guardian is still alive; recovery ownership is unsafe")

    # Re-read immediately before the irreversible transition.  A guardian
    # that managed to publish ACK while we checked its identity wins the CAS.
    if read_child_record(root, task, child_id) != durable:
        raise ParallelChildError(
            "child recovery compare failed before terminal publication")
    reaped = dict(durable)
    reaped["state"] = "reaped"
    reaped["returncode"] = int(returncode)
    write_child_record(root, reaped)
    persisted = read_child_record(root, task, child_id)
    if persisted != reaped:
        raise ParallelChildError("recovered reap durability verification failed")
    return persisted


def _payload_bootstrap_argv(payload: Sequence[str]) -> list[str]:
    return [
        sys.executable,
        "-m",
        "engine.parallel_child",
        "--payload-bootstrap",
        "--",
        *payload,
    ]


def _replace_stdin_with_devnull() -> None:
    descriptor = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(descriptor, 0)
    finally:
        if descriptor != 0:
            os.close(descriptor)


def run_payload_bootstrap(payload_argv: Sequence[object]) -> int:
    """Hold the real payload behind the guardian's durable identity barrier.

    POSIX overlays this bootstrap with the real payload so PID, birth token,
    and PGID remain exactly those in the durable child record.  Windows keeps
    the bootstrap as the Job root and waits for the real payload, so recovery
    can still fence the entire Job/tree through the durable bootstrap identity.
    """
    payload = _normalize_argv(payload_argv)
    try:
        go = _read_control_byte(sys.stdin.buffer)
    except (OSError, ValueError):
        return GUARDIAN_CANCELLED_RC
    if go in (b"", ""):
        return GUARDIAN_CANCELLED_RC
    if go != PAYLOAD_GO_BYTE:
        return GUARDIAN_PROTOCOL_RC
    _replace_stdin_with_devnull()
    resolved = compat.resolve_command(payload)
    if not compat.IS_WINDOWS:
        try:
            os.execvpe(resolved[0], resolved, os.environ)
        except OSError:
            return GUARDIAN_SPAWN_RC
        raise AssertionError("os.execvpe returned unexpectedly")
    try:
        process = subprocess.Popen(resolved, stdin=subprocess.DEVNULL)
    except (OSError, ValueError, subprocess.SubprocessError):
        return GUARDIAN_SPAWN_RC
    try:
        return int(compat.wait_process(process))
    except KeyboardInterrupt:
        try:
            compat.kill_process_group(process)
        except (OSError, ProcessLookupError, ValueError):
            try:
                process.kill()
            except (OSError, ProcessLookupError):
                pass
        return GUARDIAN_CANCELLED_RC


def run_guardian(
    payload_argv: Sequence[object],
    *,
    run_dir: Path | str,
    task: int,
    child_id: str,
    control_stream: BinaryIO | None = None,
    poll_interval: float = 0.02,
    launch_authorizer=None,
) -> int:
    """Publish ACK/reap transitions and fence payload on parent loss."""
    payload = _normalize_argv(payload_argv)
    root = _normalize_run_dir(run_dir)
    task = _require_positive_int(task, "task")
    child_id = _require_child_id(child_id)
    argv_hash = payload_argv_hash(payload)
    if (not isinstance(poll_interval, (int, float))
            or isinstance(poll_interval, bool)
            or not math.isfinite(float(poll_interval))
            or poll_interval <= 0):
        raise ParallelChildError("poll_interval must be a finite positive number")

    stream = control_stream if control_stream is not None else sys.stdin.buffer
    try:
        ack = _read_control_byte(stream)
    except (OSError, ValueError):
        return GUARDIAN_CANCELLED_RC
    if ack in (b"", ""):
        return GUARDIAN_CANCELLED_RC
    if ack != ACK_BYTE:
        print("parallel child guardian received an invalid ACK byte", file=sys.stderr)
        return GUARDIAN_PROTOCOL_RC

    parent_lost = threading.Event()
    previous_handlers = _install_signal_handlers(parent_lost)
    posix_containment_ready = (
        compat.IS_WINDOWS or _enable_posix_subreaper())

    process: subprocess.Popen | None = None
    payload_control: BinaryIO | None = None
    acked: dict | None = None
    tree_proven = True
    guardian_rc = GUARDIAN_PROTOCOL_RC
    record_rc = GUARDIAN_PROTOCOL_RC
    diagnostic: str | None = None
    try:
        try:
            # Validate authority before even starting the inert bootstrap.
            _validate_guardian_ready(root, task, child_id, argv_hash)
            if not posix_containment_ready:
                raise ParallelChildError(
                    "POSIX subreaper containment is unavailable")
        except ParallelChildError:
            diagnostic = (
                "parallel child guardian POSIX subreaper readiness failed"
                if not posix_containment_ready
                else "parallel child guardian durable ACK validation failed"
            )
        else:
            watcher = threading.Thread(
                target=_watch_parent,
                args=(stream, parent_lost),
                name="parallel-child-parent-watch",
                daemon=True,
            )
            watcher.start()
            if parent_lost.is_set():
                guardian_rc = GUARDIAN_CANCELLED_RC
                record_rc = GUARDIAN_CANCELLED_RC
            else:
                try:
                    # The bootstrap cannot execute payload code until the
                    # exact PID/birth-token/PGID is durably ACKed below.
                    process = subprocess.Popen(
                        _payload_bootstrap_argv(payload),
                        stdin=subprocess.PIPE,
                        **compat.popen_group_kwargs(),
                    )
                    payload_control = process.stdin
                    if payload_control is None:
                        raise ParallelChildError(
                            "payload bootstrap control pipe is unavailable")
                    if compat.attach_process_group(
                            process, allow_breakaway=False) is not True:
                        raise ParallelChildError(
                            "payload process-group containment failed")
                    identity = compat.capture_process_identity(process)
                    acked = _acked_record(
                        root, task, child_id, argv_hash, identity)
                    if parent_lost.is_set():
                        raise InterruptedError("supervisor lease ended before payload release")
                    authorizer = (
                        parallel_worker.claim_guardian_launch
                        if launch_authorizer is None else launch_authorizer)
                    authorizer(
                        root, acked, payload_pid=process.pid)
                    if parent_lost.is_set():
                        raise InterruptedError(
                            "supervisor lease ended after launch claim")
                    payload_control.write(PAYLOAD_GO_BYTE)
                    payload_control.flush()
                    payload_control.close()
                    process.stdin = None
                    payload_control = None
                except (OSError, ValueError, subprocess.SubprocessError,
                        ParallelChildError, parallel_contract.ParallelContractError,
                        InterruptedError) as exc:
                    if payload_control is not None:
                        try:
                            payload_control.close()
                        except OSError:
                            pass
                        if process is not None:
                            process.stdin = None
                        payload_control = None
                    if process is not None:
                        _terminate_payload(process)
                    if parent_lost.is_set():
                        guardian_rc = GUARDIAN_CANCELLED_RC
                        record_rc = GUARDIAN_CANCELLED_RC
                    elif isinstance(
                            exc, parallel_worker.LaunchReservationUnavailable):
                        guardian_rc = GUARDIAN_CANCELLED_RC
                        record_rc = GUARDIAN_CANCELLED_RC
                    else:
                        guardian_rc = GUARDIAN_SPAWN_RC
                        record_rc = GUARDIAN_SPAWN_RC
                        diagnostic = "parallel child guardian payload spawn failed"
                else:
                    while True:
                        returncode = process.poll()
                        if returncode is not None:
                            compat.wait_process(process)
                            guardian_rc = int(returncode)
                            record_rc = int(returncode)
                            break
                        if parent_lost.wait(float(poll_interval)):
                            _terminate_payload(process)
                            guardian_rc = GUARDIAN_CANCELLED_RC
                            record_rc = GUARDIAN_CANCELLED_RC
                            break
    finally:
        if payload_control is not None:
            try:
                payload_control.close()
            except OSError:
                pass
            if process is not None:
                process.stdin = None
        if process is not None:
            if process.poll() is None:
                _terminate_payload(process)
            # On Windows this also kills any descendants left after a normal
            # payload leader exit.  It is a no-op on POSIX.
            compat.close_process_group(process)
            if not compat.IS_WINDOWS:
                try:
                    tree_proven = (
                        posix_containment_ready
                        and _fence_posix_adopted_descendants()
                    )
                except (OSError, ProcessLookupError, ValueError):
                    tree_proven = False
                if not tree_proven:
                    guardian_rc = GUARDIAN_PROTOCOL_RC
                    diagnostic = (
                        "parallel child guardian descendant reap proof failed")
        _restore_signal_handlers(previous_handlers)

    if diagnostic:
        print(diagnostic, file=sys.stderr)
    if acked is not None and tree_proven:
        try:
            _publish_reaped(root, acked, record_rc)
        except ParallelChildError:
            print(
                "parallel child guardian durable reap publication failed",
                file=sys.stderr,
            )
            return GUARDIAN_PROTOCOL_RC
    return guardian_rc


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="durable ACK-gated managed parallel child guardian")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--task", required=True, type=int)
    parser.add_argument("--child-id", required=True)
    parser.add_argument("payload", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if values[:1] == ["--payload-bootstrap"]:
        payload = values[1:]
        if payload[:1] == ["--"]:
            payload = payload[1:]
        try:
            return run_payload_bootstrap(payload)
        except ParallelChildError:
            return GUARDIAN_PROTOCOL_RC
    args = build_argument_parser().parse_args(values)
    payload = list(args.payload)
    if payload[:1] == ["--"]:
        payload = payload[1:]
    if compat.IS_WINDOWS:
        try:
            import msvcrt

            msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        except (AttributeError, OSError, ValueError):
            pass
    try:
        return run_guardian(
            payload,
            run_dir=args.run_dir,
            task=args.task,
            child_id=args.child_id,
        )
    except ParallelChildError:
        print("parallel child guardian protocol error", file=sys.stderr)
        return GUARDIAN_PROTOCOL_RC


__all__ = [
    "ACK_BYTE",
    "CHILD_RECORD_SCHEMA",
    "CHILD_STATES",
    "GUARDIAN_CANCELLED_RC",
    "GUARDIAN_PROTOCOL_RC",
    "GUARDIAN_SPAWN_RC",
    "ParallelChildError",
    "PAYLOAD_CONTAINMENTS",
    "PAYLOAD_GO_BYTE",
    "build_guardian_argv",
    "child_record",
    "main",
    "payload_argv_hash",
    "read_child_record",
    "recover_acked_child",
    "recover_orphan_child",
    "run_guardian",
    "run_payload_bootstrap",
    "validate_child_record",
    "write_child_record",
]


if __name__ == "__main__":
    raise SystemExit(main())
