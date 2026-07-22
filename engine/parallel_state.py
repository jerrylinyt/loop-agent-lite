"""Durable, side-effect-free contracts for parallel run state.

This module owns the filesystem representation of a parallel run, but it does
not spawn processes and it performs no Git operation.  Four artifacts are
immutable once published: ``manifest.json``, ``run-config.json``, ``plan.json``
and ``assignments/task-N.json``.  Mutable state (for example ``aggregate``) is
written through the same atomic/fsync primitive, but is deliberately kept out
of the immutable hash graph.

Dispatch tokens are the one intentional exception to JSON artifacts.  Their
values live only in supervisor-owned ``dispatch/task-N.token`` files with mode
0600; immutable assignments contain only the SHA-256 digest.  Consequently a
worker state, manifest, log, or receipt never needs to serialize a secret.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping, Sequence

from engine import parallel_contract as contract


SCHEMA_VERSION = 1
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_TOKEN_BYTES = 4096

WORKSPACE_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
REQUEST_ID_RE = re.compile(r"[0-9a-f]{32}")
ATOMIC_TEMP_RE = re.compile(r"\.parallel-state-[0-9a-f]{32}\.tmp")
ENVIRONMENT_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
RESERVED_ENVIRONMENT_NAMES = frozenset({
    "PATH", "PYTHONHOME", "PYTHONPATH", "PYTHONUTF8",
})

RUN_STATUSES = frozenset({
    "initializing", "running", "pause_requested", "paused",
    "cancel_requested", "finalizing", "finalizing_cancel", "blocked",
    "completed", "cancelled",
})
TERMINAL_INTENTS = frozenset({None, "completed", "cancelled"})
TASK_OUTCOMES = frozenset({"pending", "integrated", "blocked", "cancelled"})
RESOURCE_STATES = frozenset({
    "queued", "provisioning", "running", "gate_pending", "gate_claimed",
    "pausing", "paused", "crashed", "recovery_required", "exited",
    "cleaning", "cleaned", "cleanup_failed",
})
WORKER_ASSIGNMENT_STATUSES = frozenset({
    "running", "paused", "recovery-required", "integrated", "blocked",
    "cancelled",
})

_RUN_TRANSITIONS = {
    "initializing": frozenset({
        "running", "pause_requested", "cancel_requested", "blocked",
    }),
    "running": frozenset({
        "pause_requested", "cancel_requested", "finalizing", "blocked",
    }),
    "pause_requested": frozenset({"paused", "cancel_requested", "blocked"}),
    "paused": frozenset({"initializing", "cancel_requested", "blocked"}),
    "cancel_requested": frozenset({"finalizing_cancel", "blocked"}),
    "finalizing": frozenset({"completed", "blocked"}),
    "finalizing_cancel": frozenset({"cancelled", "blocked"}),
    "blocked": frozenset({
        "initializing", "cancel_requested", "finalizing", "finalizing_cancel",
    }),
    "completed": frozenset(),
    "cancelled": frozenset(),
}

_OUTCOME_TRANSITIONS = {
    "pending": frozenset({"integrated", "blocked", "cancelled"}),
    "blocked": frozenset({"cancelled"}),
    "integrated": frozenset(),
    "cancelled": frozenset(),
}

_RESOURCE_TRANSITIONS = {
    "queued": frozenset({"provisioning", "cleaned"}),
    "provisioning": frozenset({
        "running", "pausing", "crashed", "recovery_required", "exited",
    }),
    "running": frozenset({
        "gate_pending", "pausing", "crashed", "recovery_required", "exited",
    }),
    "gate_pending": frozenset({
        "gate_claimed", "pausing", "crashed", "recovery_required", "exited",
    }),
    # A claimed gate normally exits after a terminal receipt.  The one
    # nonterminal result is stale-integration: no merge occurred, so the
    # worker returns to running to sync/revalidate its exact assignment.
    "gate_claimed": frozenset({"running", "recovery_required", "exited"}),
    "pausing": frozenset({"paused", "crashed", "recovery_required", "exited"}),
    "paused": frozenset({"provisioning", "exited"}),
    "crashed": frozenset({"provisioning", "pausing", "recovery_required", "exited"}),
    "recovery_required": frozenset({"gate_claimed", "pausing", "paused", "exited"}),
    "exited": frozenset({"cleaning"}),
    "cleaning": frozenset({"cleaned", "cleanup_failed"}),
    "cleanup_failed": frozenset({"cleaning"}),
    "cleaned": frozenset(),
}

_RUN_CONFIG_FIELDS = frozenset({
    "schema_version", "repo", "primary_repo", "goal", "plan_doc",
    "agent_cmd", "validate_cmd", "flag_threshold", "done_threshold",
    "red_limit", "stall_limit", "stuck_stop", "stuck_stop_count",
    "round_timeout", "validate_timeout", "agent_backoff_max", "notify_cmd",
    "max_parallel", "worker_restart_limit", "environment", "max_rounds",
    "pause_after_plan", "allow_serial_stack",
})
_RUN_CONFIG_REQUIRED = frozenset({
    "repo", "goal", "agent_cmd", "validate_cmd", "flag_threshold",
    "done_threshold", "red_limit", "stall_limit", "stuck_stop_count",
    "round_timeout", "validate_timeout", "agent_backoff_max",
    "max_parallel", "worker_restart_limit",
})

_MANIFEST_FIELDS = frozenset({
    "schema_version", "run_id", "parent_workspace", "integration_branch",
    "integration_ref", "integration_start_sha", "plan_hash",
    "run_config_hash", "batches", "assignments",
})
_MANIFEST_ASSIGNMENT_FIELDS = frozenset({"order", "path", "launch_spec_hash"})
_ASSIGNMENT_FIELDS = frozenset({
    "schema_version", "run_id", "parent_workspace", "assigned_order",
    "batch_index", "stack", "task_hash", "plan_hash", "run_config_hash",
    "dispatch_token_hash", "integration_ref", "task_ref", "worktree_path",
    "worker_repo", "worker_workspace", "worker_workspace_path",
    "gate_client_cmd", "gate_command",
})
_RECEIPT_FIELDS = frozenset({
    "schema_version", "run_id", "manifest_hash", "assignment_hash", "task",
    "request_id", "sequence", "previous_receipt_hash", "integration_before",
    "validated_sha", "validated_round",
})
_AGGREGATE_FIELDS = frozenset({
    "schema_version", "run_id", "version", "control_generation",
    "status", "terminal_intent", "pause_generation", "batch", "tasks",
    "error",
})
_AGGREGATE_TASK_FIELDS = frozenset({
    "order", "batch", "outcome", "resource_state", "restart_count", "error",
})


class ParallelStateError(ValueError):
    """An artifact, state transition, or derived path is unsafe or invalid."""


@dataclass(frozen=True)
class TaskIdentity:
    """Canonical refs and workspace paths for one assignment."""

    run_id: str
    order: int
    integration_ref: str
    task_ref: str
    worktree_path: Path
    worker_workspace: str
    worker_workspace_path: Path


@dataclass(frozen=True)
class ValidatedRunArtifacts:
    """A fully cross-validated immutable run artifact graph."""

    run_dir: Path
    manifest: dict
    run_config: dict
    plan: tuple[dict, ...]
    assignments: Mapping[int, dict]
    manifest_hash: str
    plan_hash: str
    run_config_hash: str
    assignment_hashes: Mapping[int, str]
    # Populated only by materialization/recovery code that is authorized to
    # read secrets.  validate_run_artifacts intentionally leaves this empty.
    dispatch_tokens: Mapping[int, str] = field(
        default_factory=dict, repr=False, compare=False)


def _raise_duplicate_key(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ParallelStateError(f"duplicate JSON key: {key!r}")
        value[key] = item
    return value


def _validate_json_tree(value: object, active: set[int] | None = None) -> None:
    """Reject Python values that do not have one unambiguous JSON meaning."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ParallelStateError("JSON numbers must be finite")
        return
    if isinstance(value, (dict, list, tuple)):
        active = set() if active is None else active
        identity = id(value)
        if identity in active:
            raise ParallelStateError("cyclic values are not JSON")
        active.add(identity)
        try:
            if isinstance(value, dict):
                if any(not isinstance(key, str) for key in value):
                    raise ParallelStateError("JSON object keys must be strings")
                for item in value.values():
                    _validate_json_tree(item, active)
            else:
                for item in value:
                    _validate_json_tree(item, active)
        finally:
            active.remove(identity)
        return
    raise ParallelStateError(f"unsupported JSON value type: {type(value).__name__}")


def canonical_json_bytes(value: object) -> bytes:
    """Return the sole canonical JSON encoding used for durable hashes."""
    _validate_json_tree(value)
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return text.encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ParallelStateError(f"value is not canonical JSON: {exc}") from exc


def canonical_json_hash(value: object) -> str:
    """SHA-256 of :func:`canonical_json_bytes`."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def dispatch_token_hash(token: str) -> str:
    """Hash an opaque dispatch token without normalizing its value."""
    raw = _validate_dispatch_token(token)
    return hashlib.sha256(raw).hexdigest()


def _validate_dispatch_token(token: object) -> bytes:
    if not isinstance(token, str) or not token or "\x00" in token:
        raise ParallelStateError("dispatch token must be a non-empty string without NUL")
    if "\r" in token or "\n" in token:
        raise ParallelStateError("dispatch token must not contain line breaks")
    raw = token.encode("utf-8")
    if len(raw) > MAX_TOKEN_BYTES:
        raise ParallelStateError("dispatch token is too large")
    return raw


def _require_positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ParallelStateError(f"{label} must be a positive integer")
    return value


def _require_nonnegative_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ParallelStateError(f"{label} must be a non-negative integer")
    return value


def _require_number(value: object, label: str, *, positive: bool) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0 or (positive and float(value) == 0)):
        qualifier = "positive" if positive else "non-negative"
        raise ParallelStateError(f"{label} must be a finite {qualifier} number")
    return float(value)


def _require_workspace_name(value: object, label: str = "workspace") -> str:
    if (not isinstance(value, str) or value.startswith(".")
            or WORKSPACE_NAME_RE.fullmatch(value) is None):
        raise ParallelStateError(f"{label} is not a canonical workspace name")
    return value


def _require_exact_fields(value: object, expected: frozenset[str], label: str) -> dict:
    if not isinstance(value, dict):
        raise ParallelStateError(f"{label} must be a JSON object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ParallelStateError(
            f"{label} fields mismatch (missing={missing}, extra={extra})")
    return value


def _is_link_or_junction(path: Path, info: os.stat_result | None = None) -> bool:
    try:
        info = info if info is not None else path.lstat()
    except OSError as exc:
        raise ParallelStateError(f"cannot inspect path {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        return True
    # Python exposes Windows reparse metadata on stat_result.  Reject every
    # reparse-point kind, not only the junction/symlink variants Path knows,
    # because an artifact path must never redirect outside its anchor.
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if attributes & reparse_flag:
        return True
    junction = getattr(path, "is_junction", None)
    if junction is not None:
        try:
            return bool(junction())
        except OSError as exc:
            raise ParallelStateError(f"cannot inspect junction {path}: {exc}") from exc
    return False


def _require_root_directory(root: Path | str) -> Path:
    path = Path(root)
    if not path.is_absolute():
        path = path.absolute()
    try:
        info = path.lstat()
    except OSError as exc:
        raise ParallelStateError(f"artifact root is unavailable: {path}: {exc}") from exc
    if _is_link_or_junction(path, info) or not stat.S_ISDIR(info.st_mode):
        raise ParallelStateError(f"artifact root must be a real directory: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ParallelStateError(f"artifact root cannot be resolved: {path}: {exc}") from exc
    # Resolve catches a link/junction in an ancestor that lstat(path) cannot.
    if resolved != path:
        raise ParallelStateError(f"artifact root has a linked ancestor: {path}")
    return path


def _relative_parts(relative: Path | str) -> tuple[str, ...]:
    path = Path(relative)
    if path.is_absolute() or path.drive or path.root:
        raise ParallelStateError("artifact path must be relative")
    parts = path.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ParallelStateError("artifact path contains an unsafe component")
    return tuple(parts)


def _check_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ParallelStateError(f"directory is unavailable: {path}: {exc}") from exc
    if _is_link_or_junction(path, info) or not stat.S_ISDIR(info.st_mode):
        raise ParallelStateError(f"path component is not a real directory: {path}")


def _ensure_safe_directory(root: Path | str, relative: Path | str) -> Path:
    base = _require_root_directory(root)
    current = base
    for part in _relative_parts(relative):
        current = current / part
        created = False
        try:
            os.mkdir(current, 0o700)
            created = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise ParallelStateError(f"cannot create directory {current}: {exc}") from exc
        _check_directory(current)
        if created:
            _fsync_directory(current.parent)
    return current


def _assert_safe_existing_directory_prefixes(root: Path, candidate: Path) -> None:
    """Reject links/reparse points in every existing component below root.

    Derivation is read-only, so a suffix is allowed not to exist yet.  Once a
    component is absent none of its descendants can exist without a linked
    ancestor, which this walk has already rejected.
    """
    base = _require_root_directory(root)
    try:
        relative = candidate.relative_to(base)
    except ValueError as exc:
        raise ParallelStateError("derived path escapes workspace root") from exc
    current = base
    missing = False
    for part in _relative_parts(relative):
        current = current / part
        if missing:
            continue
        try:
            info = current.lstat()
        except FileNotFoundError:
            missing = True
            continue
        except OSError as exc:
            raise ParallelStateError(f"cannot inspect derived path {current}: {exc}") from exc
        if _is_link_or_junction(current, info) or not stat.S_ISDIR(info.st_mode):
            raise ParallelStateError(
                f"derived path component must be a real directory: {current}")


def _artifact_path(
    root: Path | str,
    relative: Path | str,
    *,
    create_parents: bool,
) -> Path:
    base = _require_root_directory(root)
    parts = _relative_parts(relative)
    parent = base
    for part in parts[:-1]:
        candidate = parent / part
        if create_parents:
            created = False
            try:
                os.mkdir(candidate, 0o700)
                created = True
            except FileExistsError:
                pass
            except OSError as exc:
                raise ParallelStateError(f"cannot create directory {candidate}: {exc}") from exc
            if created:
                _fsync_directory(candidate.parent)
        _check_directory(candidate)
        parent = candidate
    target = parent / parts[-1]
    try:
        info = target.lstat()
    except FileNotFoundError:
        return target
    except OSError as exc:
        raise ParallelStateError(f"cannot inspect artifact {target}: {exc}") from exc
    if _is_link_or_junction(target, info) or not stat.S_ISREG(info.st_mode):
        raise ParallelStateError(f"artifact must be a regular non-link file: {target}")
    return target


def _safe_exists(root: Path | str, relative: Path | str) -> bool:
    base = _require_root_directory(root)
    parts = _relative_parts(relative)
    current = base
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ParallelStateError(f"cannot inspect artifact {current}: {exc}") from exc
        final = index == len(parts) - 1
        if _is_link_or_junction(current, info):
            raise ParallelStateError(f"linked artifact path is forbidden: {current}")
        if final:
            if not stat.S_ISREG(info.st_mode):
                raise ParallelStateError(f"artifact must be a regular file: {current}")
        elif not stat.S_ISDIR(info.st_mode):
            raise ParallelStateError(f"artifact parent must be a directory: {current}")
    return True


def _read_regular_bytes(path: Path, *, max_bytes: int = MAX_ARTIFACT_BYTES) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ParallelStateError(f"cannot inspect artifact {path}: {exc}") from exc
    if (_is_link_or_junction(path, before) or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1):
        raise ParallelStateError(f"artifact must be a single-link regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ParallelStateError(f"cannot open artifact {path}: {exc}") from exc
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise ParallelStateError(f"artifact changed while opening: {path}")
        if opened.st_size > max_bytes:
            raise ParallelStateError(f"artifact exceeds size limit: {path}")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
            raise ParallelStateError(f"artifact exceeds size limit: {path}")
        return raw
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    """Best-effort directory fsync (unsupported by Windows' CRT)."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)


def _new_temp(parent: Path, mode: int) -> tuple[int, Path]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    for _ in range(32):
        candidate = parent / f".parallel-state-{secrets.token_hex(16)}.tmp"
        try:
            fd = os.open(candidate, flags, mode)
            return fd, candidate
        except FileExistsError:
            continue
        except OSError as exc:
            raise ParallelStateError(f"cannot create atomic temp file: {exc}") from exc
    raise ParallelStateError("cannot allocate a unique atomic temp file")


def _write_temp(parent: Path, payload: bytes, mode: int) -> Path:
    fd, temp = _new_temp(parent, mode)
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        # Windows chmod controls DOS attributes rather than ACLs; applying it
        # after close is harmless and the run-directory ACL is authoritative.
        if os.name == "nt":
            os.chmod(temp, mode)
        return temp
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def _atomic_write_bytes(
    root: Path | str,
    relative: Path | str,
    payload: bytes,
    *,
    immutable: bool,
    mode: int = 0o600,
) -> Path:
    target = _artifact_path(root, relative, create_parents=True)
    temp = _write_temp(target.parent, payload, mode)
    try:
        if immutable:
            try:
                # A hard-link publish gives create-if-absent semantics without
                # ever exposing a partially written final artifact.
                os.link(temp, target, follow_symlinks=False)
            except FileExistsError:
                existing = _read_regular_bytes(target)
                if existing != payload:
                    raise ParallelStateError(
                        f"immutable artifact already exists with different bytes: {target}")
            except OSError as exc:
                raise ParallelStateError(
                    f"cannot publish immutable artifact {target}: {exc}") from exc
            finally:
                try:
                    temp.unlink()
                except FileNotFoundError:
                    pass
            # The target has nlink=1 only after the temp name is removed.
            if _read_regular_bytes(target) != payload:
                raise ParallelStateError(f"immutable artifact verification failed: {target}")
        else:
            replace_error = None
            for attempt in range(20):
                try:
                    os.replace(temp, target)
                    replace_error = None
                    break
                except PermissionError as exc:
                    replace_error = exc
                    if os.name != "nt" or attempt == 19:
                        break
                    # A concurrent Dashboard/control reader can briefly hold a
                    # Windows handle that denies rename/delete sharing.  The
                    # temp remains private and fully fsynced, so bounded retry
                    # preserves the same atomic publication contract.
                    time.sleep(min(0.005 * (attempt + 1), 0.05))
                except OSError as exc:
                    replace_error = exc
                    break
            if replace_error is not None:
                raise ParallelStateError(
                    f"cannot atomically replace {target}: {replace_error}"
                ) from replace_error
        _fsync_directory(target.parent)
        return target
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


def write_or_verify_immutable_json(
    root: Path | str, relative: Path | str, value: object
) -> Path:
    """Atomically create an immutable canonical JSON artifact, or verify it."""
    return _atomic_write_bytes(
        root, relative, canonical_json_bytes(value) + b"\n", immutable=True)


def atomic_write_json(root: Path | str, relative: Path | str, value: object) -> Path:
    """Atomically replace mutable canonical JSON and fsync its parent."""
    return _atomic_write_bytes(
        root, relative, canonical_json_bytes(value) + b"\n", immutable=False)


def read_canonical_json(root: Path | str, relative: Path | str) -> object:
    """Read a regular, non-linked file and require exact canonical encoding."""
    path = _artifact_path(root, relative, create_parents=False)
    raw = _read_regular_bytes(path)
    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_raise_duplicate_key,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ParallelStateError(f"non-finite JSON number: {token}")),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ParallelStateError(f"invalid JSON artifact {path}: {exc}") from exc
    expected = canonical_json_bytes(value) + b"\n"
    if raw != expected:
        raise ParallelStateError(f"artifact is not canonical JSON: {path}")
    return value


def write_dispatch_token(
    run_dir: Path | str,
    order: int,
    token: str,
    *,
    expected_hash: str | None = None,
) -> Path:
    """Atomically persist a supervisor-owned token with mode 0600."""
    order = _require_positive_int(order, "order")
    raw = _validate_dispatch_token(token)
    actual_hash = hashlib.sha256(raw).hexdigest()
    if expected_hash is not None:
        try:
            expected = contract.require_config_hash(expected_hash, "dispatch_token_hash")
        except contract.ParallelContractError as exc:
            raise ParallelStateError(str(exc)) from exc
        if actual_hash != expected:
            raise ParallelStateError("dispatch token does not match immutable assignment hash")
    relative = f"dispatch/task-{order}.token"
    if _safe_exists(run_dir, relative):
        existing = read_dispatch_token(run_dir, order)
        if existing == token:
            return _artifact_path(run_dir, relative, create_parents=False)
    return _atomic_write_bytes(run_dir, relative, raw, immutable=False, mode=0o600)


def read_dispatch_token(
    run_dir: Path | str,
    order: int,
    *,
    expected_hash: str | None = None,
) -> str:
    """Read a supervisor token, checking permissions and an optional digest."""
    order = _require_positive_int(order, "order")
    path = _artifact_path(run_dir, f"dispatch/task-{order}.token", create_parents=False)
    try:
        info = path.lstat()
    except OSError as exc:
        raise ParallelStateError(f"dispatch token is unavailable: {path}: {exc}") from exc
    # Windows' ``st_mode`` exposes DOS attributes, not the file's effective
    # ACL.  Interpreting its synthetic group/other bits as POSIX access bits
    # rejects correctly owner-scoped files.  On Windows the non-link regular
    # file checks (including single-link identity) remain authoritative; the
    # supervisor/runtime is responsible for its private run-directory ACL.
    if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o600:
        raise ParallelStateError(f"dispatch token permissions must be 0600: {path}")
    raw = _read_regular_bytes(path, max_bytes=MAX_TOKEN_BYTES)
    try:
        token = raw.decode("utf-8")
    except UnicodeError as exc:
        raise ParallelStateError(f"dispatch token is not UTF-8: {path}") from exc
    _validate_dispatch_token(token)
    if expected_hash is not None:
        try:
            expected = contract.require_config_hash(expected_hash, "dispatch_token_hash")
        except contract.ParallelContractError as exc:
            raise ParallelStateError(str(exc)) from exc
        if dispatch_token_hash(token) != expected:
            raise ParallelStateError("dispatch token does not match immutable assignment hash")
    return token


def derive_run_directory(
    workspace_root: Path | str, parent_workspace: str, run_id: str
) -> Path:
    """Derive ``WORKSPACE_ROOT/<base>/parallel/<run_id>`` without creating it."""
    root = _require_root_directory(workspace_root)
    parent = _require_workspace_name(parent_workspace, "parent workspace")
    try:
        canonical_run_id = contract.require_run_id(run_id)
    except contract.ParallelContractError as exc:
        raise ParallelStateError(str(exc)) from exc
    result = root / parent / "parallel" / canonical_run_id
    _assert_safe_existing_directory_prefixes(root, result)
    return result


def _is_within(path: Path, directory: Path) -> bool:
    return path == directory or directory in path.parents


def derive_task_identity(
    workspace_root: Path | str,
    parent_workspace: str,
    run_id: str,
    order: int,
    *,
    target_repo: Path | str | None = None,
) -> TaskIdentity:
    """Derive canonical task ref, worktree, and worker workspace identity."""
    root = _require_root_directory(workspace_root)
    parent = _require_workspace_name(parent_workspace, "parent workspace")
    order = _require_positive_int(order, "order")
    try:
        canonical_run_id = contract.require_run_id(run_id)
        integration_ref = contract.integration_ref_for(canonical_run_id)
    except contract.ParallelContractError as exc:
        raise ParallelStateError(str(exc)) from exc
    task_ref = f"refs/heads/loop/{canonical_run_id}/task-{order}"
    worktree = root / parent / "worktrees" / f"{canonical_run_id}-task-{order}"
    worker_workspace = f"{parent}--{canonical_run_id}-task-{order}"
    worker_path = root / worker_workspace
    _assert_safe_existing_directory_prefixes(root, worktree)
    _assert_safe_existing_directory_prefixes(root, worker_path)
    if target_repo is not None:
        repo = Path(target_repo).expanduser().resolve(strict=False)
        worktree_resolved = worktree.resolve(strict=False)
        if _is_within(worktree_resolved, repo):
            raise ParallelStateError(
                "derived worktree must resolve outside the target repository")
    return TaskIdentity(
        run_id=canonical_run_id,
        order=order,
        integration_ref=integration_ref,
        task_ref=task_ref,
        worktree_path=worktree,
        worker_workspace=worker_workspace,
        worker_workspace_path=worker_path,
    )


def _normalize_plan(plan: object) -> list[dict]:
    # Lazy import avoids pulling engine.loop (and its runtime machinery) into
    # simple hash/state consumers at module-import time.
    from engine.work import validate_plan

    normalized, errors = validate_plan(plan)
    if errors or normalized is None:
        detail = "; ".join(str(error) for error in errors)
        raise ParallelStateError(f"invalid frozen plan: {detail}")
    return normalized


def project_stack_batches(plan: Sequence[Mapping[str, object]]) -> list[dict]:
    """Project contiguous stack groups and serial singleton tasks to batches."""
    normalized = _normalize_plan(list(plan))
    batches: list[dict] = []
    current_key: tuple[str, int] | None = None
    current_stack: int | None = None
    current_orders: list[int] = []
    for task in normalized:
        order = task["order"]
        key = (("stack", task["stack"]) if "stack" in task
               else ("serial", order))
        if current_key is not None and key != current_key:
            batches.append({
                "index": len(batches) + 1,
                "stack": current_stack,
                "orders": current_orders,
            })
            current_orders = []
        current_key = key
        current_stack = task.get("stack")
        current_orders.append(order)
    if current_orders:
        batches.append({
            "index": len(batches) + 1,
            "stack": current_stack,
            "orders": current_orders,
        })
    return batches


def _canonical_absolute_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ParallelStateError(f"run config {label} must be a non-empty path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ParallelStateError(f"run config {label} must be absolute")
    return str(path.resolve(strict=False))


def _normalize_environment_name(value: object, label: str) -> str:
    if (not isinstance(value, str)
            or ENVIRONMENT_NAME_RE.fullmatch(value) is None):
        raise ParallelStateError(
            f"{label} must be a portable environment variable name")
    canonical = value.upper()
    if (canonical in RESERVED_ENVIRONMENT_NAMES
            or canonical.startswith("LOOP_")):
        raise ParallelStateError(f"{label} uses reserved environment name {value!r}")
    return value


def normalize_environment_contract(value: object) -> dict:
    """Canonicalize the explicit, non-secret portion of a worker environment.

    Ambient process state is deliberately outside this artifact.  Only ordered
    absolute PATH additions, explicitly non-secret scalar values, and names of
    required secret variables are durable.  Runtime-owned PATH/Python/LOOP_*
    variables cannot be overridden through this contract.
    """
    if value is None:
        value = {
            "path_additions": [],
            "non_secret": {},
            "required_secret_names": [],
        }
    expected = frozenset({"path_additions", "non_secret", "required_secret_names"})
    env = _require_exact_fields(value, expected, "run config environment")
    additions = env["path_additions"]
    if (not isinstance(additions, list)
            or any(not isinstance(item, str) or not item or "\x00" in item
                   for item in additions)):
        raise ParallelStateError("environment.path_additions must be a string list")
    canonical_additions: list[str] = []
    seen_additions: set[str] = set()
    for index, item in enumerate(additions):
        canonical = _canonical_absolute_path(
            item, f"environment.path_additions[{index}]")
        identity = os.path.normcase(canonical)
        if identity not in seen_additions:
            seen_additions.add(identity)
            canonical_additions.append(canonical)
    non_secret = env["non_secret"]
    if not isinstance(non_secret, dict) or any(
            not isinstance(key, str) or not key for key in non_secret):
        raise ParallelStateError("environment.non_secret must be a string-keyed object")
    names_by_identity: dict[str, str] = {}
    for key in non_secret:
        _normalize_environment_name(key, "environment.non_secret key")
        identity = key.upper()
        if identity in names_by_identity:
            raise ParallelStateError(
                "environment.non_secret names must be case-insensitively unique")
        names_by_identity[identity] = key
    for item in non_secret.values():
        if isinstance(item, (dict, list)) or not isinstance(
                item, (str, int, float, bool, type(None))):
            raise ParallelStateError("environment.non_secret values must be JSON scalars")
        if isinstance(item, float) and not math.isfinite(item):
            raise ParallelStateError("environment.non_secret contains a non-finite number")
    required = env["required_secret_names"]
    if (not isinstance(required, list)
            or any(not isinstance(item, str) or not item for item in required)
            or len({item.upper() for item in required}) != len(required)):
        raise ParallelStateError(
            "environment.required_secret_names must be a case-insensitively "
            "unique string list")
    for name in required:
        _normalize_environment_name(
            name, "environment.required_secret_names item")
    overlap = set(names_by_identity) & {name.upper() for name in required}
    if overlap:
        raise ParallelStateError(
            "environment non-secret and required-secret names must be disjoint")
    return {
        "path_additions": canonical_additions,
        "non_secret": dict(sorted(non_secret.items())),
        "required_secret_names": sorted(required),
    }


def normalize_run_config(value: Mapping[str, object]) -> dict:
    """Return the canonical immutable, explicitly non-secret run config."""
    if not isinstance(value, Mapping):
        raise ParallelStateError("run config must be an object")
    source = dict(value)
    unknown = set(source) - _RUN_CONFIG_FIELDS
    missing = _RUN_CONFIG_REQUIRED - set(source)
    if unknown or missing:
        raise ParallelStateError(
            f"run config fields mismatch (missing={sorted(missing)}, "
            f"extra={sorted(unknown)})")
    schema = source.get("schema_version", SCHEMA_VERSION)
    if schema != SCHEMA_VERSION:
        raise ParallelStateError("unsupported run config schema_version")
    repo = _canonical_absolute_path(source["repo"], "repo")
    primary_repo = _canonical_absolute_path(
        source.get("primary_repo", repo), "primary_repo")
    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "repo": repo,
        "primary_repo": primary_repo,
    }
    for key in ("goal", "agent_cmd", "validate_cmd"):
        item = source.get(key)
        if not isinstance(item, str) or not item.strip() or "\x00" in item:
            raise ParallelStateError(f"run config {key} must be a non-empty string")
        result[key] = item.strip()
    for key in ("plan_doc", "notify_cmd"):
        item = source.get(key, "")
        if not isinstance(item, str) or "\x00" in item:
            raise ParallelStateError(f"run config {key} must be a string")
        result[key] = item.strip()
    for key in (
        "flag_threshold", "done_threshold", "red_limit", "stall_limit",
        "stuck_stop_count", "max_parallel", "worker_restart_limit",
    ):
        result[key] = _require_positive_int(source.get(key), key)
    result["round_timeout"] = _require_number(
        source.get("round_timeout"), "round_timeout", positive=False)
    result["validate_timeout"] = _require_number(
        source.get("validate_timeout"), "validate_timeout", positive=True)
    result["agent_backoff_max"] = _require_number(
        source.get("agent_backoff_max"), "agent_backoff_max", positive=False)
    stuck_stop = source.get("stuck_stop", False)
    if not isinstance(stuck_stop, bool):
        raise ParallelStateError("run config stuck_stop must be boolean")
    result["stuck_stop"] = stuck_stop
    result["environment"] = normalize_environment_contract(
        source.get("environment"))
    if "max_rounds" in source:
        result["max_rounds"] = _require_nonnegative_int(
            source["max_rounds"], "max_rounds")
    for key in ("pause_after_plan", "allow_serial_stack"):
        if key in source:
            if not isinstance(source[key], bool):
                raise ParallelStateError(f"run config {key} must be boolean")
            result[key] = source[key]
    return result


def _require_gate_command(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ParallelStateError("gate client command must be a non-empty string")
    return value.strip()


def _require_branch(value: object) -> str:
    if (not isinstance(value, str) or not value.strip() or "\x00" in value
            or "\r" in value or "\n" in value):
        raise ParallelStateError("integration branch must be a non-empty safe string")
    return value.strip()


def _normalize_token_mapping(
    dispatch_tokens: Mapping[int | str, str] | None,
    orders: Sequence[int],
) -> dict[int, str] | None:
    if dispatch_tokens is None:
        return None
    if not isinstance(dispatch_tokens, Mapping):
        raise ParallelStateError("dispatch_tokens must be a task-order mapping")
    normalized: dict[int, str] = {}
    for raw_order, token in dispatch_tokens.items():
        if isinstance(raw_order, str) and raw_order.isascii() and raw_order.isdigit():
            order = int(raw_order)
        else:
            order = _require_positive_int(raw_order, "dispatch token order")
        if order in normalized:
            raise ParallelStateError(f"duplicate dispatch token for task {order}")
        _validate_dispatch_token(token)
        normalized[order] = token
    if set(normalized) != set(orders):
        raise ParallelStateError("dispatch_tokens must contain exactly one token per task")
    if len(set(normalized.values())) != len(normalized):
        raise ParallelStateError("dispatch tokens must be unique per task")
    return normalized


def materialize_run_artifacts(
    workspace_root: Path | str,
    parent_workspace: str,
    run_id: str,
    plan: Sequence[Mapping[str, object]],
    run_config: Mapping[str, object],
    integration_start_sha: str,
    integration_branch: str,
    gate_client_cmd: str,
    *,
    dispatch_tokens: Mapping[int | str, str] | None = None,
) -> ValidatedRunArtifacts:
    """Create or byte-verify one run's immutable artifact graph.

    If ``dispatch_tokens`` is omitted, an existing supervisor token is reused
    (important after a crash); otherwise a cryptographically random token is
    generated.  An immutable assignment without its token cannot be reversed
    from the digest and therefore fails closed unless the caller supplies it.
    """
    root = _require_root_directory(workspace_root)
    parent = _require_workspace_name(parent_workspace, "parent workspace")
    try:
        run_id = contract.require_run_id(run_id)
        start_sha = contract.require_git_sha(
            integration_start_sha, "integration_start_sha")
    except contract.ParallelContractError as exc:
        raise ParallelStateError(str(exc)) from exc
    branch = _require_branch(integration_branch)
    gate_command = _require_gate_command(gate_client_cmd)
    normalized_plan = _normalize_plan(list(plan))
    normalized_config = normalize_run_config(run_config)
    orders = [task["order"] for task in normalized_plan]
    supplied_tokens = _normalize_token_mapping(dispatch_tokens, orders)
    run_relative = Path(parent) / "parallel" / run_id
    run_dir = _ensure_safe_directory(root, run_relative)

    # A published manifest marks the graph complete.  Never heal a missing or
    # corrupt immutable child underneath it; resume must fail closed.
    if _safe_exists(run_dir, "manifest.json"):
        validate_run_artifacts(run_dir, workspace_root=root)

    plan_hash = canonical_json_hash(normalized_plan)
    run_config_hash = canonical_json_hash(normalized_config)
    batches = project_stack_batches(normalized_plan)
    batch_by_order = {
        order: batch["index"] for batch in batches for order in batch["orders"]}
    task_by_order = {task["order"]: task for task in normalized_plan}

    actual_tokens: dict[int, str] = {}
    assignments: dict[int, dict] = {}
    assignment_hashes: dict[int, str] = {}
    manifest_entries: list[dict] = []
    for order in orders:
        assignment_relative = f"assignments/task-{order}.json"
        existing_assignment = None
        if _safe_exists(run_dir, assignment_relative):
            existing_assignment = read_canonical_json(run_dir, assignment_relative)
            if not isinstance(existing_assignment, dict):
                raise ParallelStateError("existing assignment is not an object")
        token_relative = f"dispatch/task-{order}.token"
        if supplied_tokens is not None:
            token = supplied_tokens[order]
        elif _safe_exists(run_dir, token_relative):
            token = read_dispatch_token(run_dir, order)
        elif existing_assignment is not None:
            raise ParallelStateError(
                f"task {order} assignment exists but its dispatch token is unavailable")
        else:
            token = secrets.token_urlsafe(32)
        token_hash = dispatch_token_hash(token)
        if existing_assignment is not None:
            try:
                frozen_token_hash = contract.require_config_hash(
                    existing_assignment.get("dispatch_token_hash"),
                    "dispatch_token_hash",
                )
            except contract.ParallelContractError as exc:
                raise ParallelStateError(str(exc)) from exc
            if frozen_token_hash != token_hash:
                raise ParallelStateError(
                    f"task {order} token does not match its immutable assignment")
        write_dispatch_token(run_dir, order, token, expected_hash=token_hash)
        actual_tokens[order] = token

        identity = derive_task_identity(
            root, parent, run_id, order, target_repo=normalized_config["repo"])
        task = task_by_order[order]
        assignment = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "parent_workspace": parent,
            "assigned_order": order,
            "batch_index": batch_by_order[order],
            "stack": task.get("stack"),
            "task_hash": canonical_json_hash(task),
            "plan_hash": plan_hash,
            "run_config_hash": run_config_hash,
            "dispatch_token_hash": token_hash,
            "integration_ref": identity.integration_ref,
            "task_ref": identity.task_ref,
            "worktree_path": str(identity.worktree_path),
            # Compatibility alias consumed by the canonical argv builder.
            "worker_repo": str(identity.worktree_path),
            "worker_workspace": identity.worker_workspace,
            "worker_workspace_path": str(identity.worker_workspace_path),
            "gate_client_cmd": gate_command,
            # Compatibility alias consumed by the canonical argv builder.
            "gate_command": gate_command,
        }
        assignment_hash = canonical_json_hash(assignment)
        assignments[order] = assignment
        assignment_hashes[order] = assignment_hash
        manifest_entries.append({
            "order": order,
            "path": assignment_relative,
            "launch_spec_hash": assignment_hash,
        })

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "parent_workspace": parent,
        "integration_branch": branch,
        "integration_ref": contract.integration_ref_for(run_id),
        "integration_start_sha": start_sha,
        "plan_hash": plan_hash,
        "run_config_hash": run_config_hash,
        "batches": batches,
        "assignments": manifest_entries,
    }

    write_or_verify_immutable_json(run_dir, "run-config.json", normalized_config)
    write_or_verify_immutable_json(run_dir, "plan.json", normalized_plan)
    for order in orders:
        write_or_verify_immutable_json(
            run_dir, f"assignments/task-{order}.json", assignments[order])
    # Publish the graph root last so a present manifest always means every
    # immutable child must already exist and verify.
    write_or_verify_immutable_json(run_dir, "manifest.json", manifest)

    validated = validate_run_artifacts(run_dir, workspace_root=root)
    for order, assignment in validated.assignments.items():
        read_dispatch_token(
            run_dir, order, expected_hash=assignment["dispatch_token_hash"])
    return replace(validated, dispatch_tokens=dict(actual_tokens))


def _validate_batch_projection(value: object, plan: Sequence[Mapping]) -> list[dict]:
    if not isinstance(value, list):
        raise ParallelStateError("manifest batches must be a list")
    expected = project_stack_batches(plan)
    if value != expected:
        raise ParallelStateError("manifest batches do not match the frozen plan")
    return value


def _list_exact_regular_files(
    root: Path | str, relative_dir: str, expected_names: set[str]
) -> None:
    directory = _artifact_path(
        root, f"{relative_dir}/.directory-probe", create_parents=False).parent
    _check_directory(directory)
    actual: set[str] = set()
    try:
        entries = list(os.scandir(directory))
    except OSError as exc:
        raise ParallelStateError(f"cannot list artifact directory {directory}: {exc}") from exc
    for entry in entries:
        path = Path(entry.path)
        try:
            info = path.lstat()
        except OSError as exc:
            raise ParallelStateError(f"cannot inspect artifact {path}: {exc}") from exc
        if _is_link_or_junction(path, info) or not stat.S_ISREG(info.st_mode):
            raise ParallelStateError(f"unexpected non-regular artifact: {path}")
        # SIGKILL may leave a fully fsynced, unpublished temp name.  It has no
        # authority because only canonical manifest paths are hashed.  Ignore
        # that exact staging shape while rejecting every other extra name.
        if ATOMIC_TEMP_RE.fullmatch(entry.name) is None:
            actual.add(entry.name)
    if actual != expected_names:
        raise ParallelStateError(
            f"{relative_dir} files mismatch (expected={sorted(expected_names)}, "
            f"actual={sorted(actual)})")


def validate_run_artifacts(
    run_dir: Path | str, *, workspace_root: Path | str | None = None
) -> ValidatedRunArtifacts:
    """Load and cross-validate all immutable artifacts for one run.

    Dispatch token *values* are intentionally outside this projection.  A
    supervisor may separately call :func:`read_dispatch_token` with the hash
    from a validated assignment when it needs to launch or resume a worker.
    """
    run_path = _require_root_directory(run_dir)
    manifest = _require_exact_fields(
        read_canonical_json(run_path, "manifest.json"),
        _MANIFEST_FIELDS,
        "manifest",
    )
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise ParallelStateError("unsupported manifest schema_version")
    try:
        run_id = contract.require_run_id(manifest["run_id"])
        start_sha = contract.require_git_sha(
            manifest["integration_start_sha"], "integration_start_sha")
        plan_hash = contract.require_config_hash(manifest["plan_hash"], "plan_hash")
        config_hash = contract.require_config_hash(
            manifest["run_config_hash"], "run_config_hash")
    except contract.ParallelContractError as exc:
        raise ParallelStateError(str(exc)) from exc
    parent = _require_workspace_name(
        manifest["parent_workspace"], "manifest parent workspace")
    _require_branch(manifest["integration_branch"])
    if manifest["integration_ref"] != contract.integration_ref_for(run_id):
        raise ParallelStateError("manifest integration_ref is not canonical")
    if manifest["integration_start_sha"] != start_sha:
        raise ParallelStateError("manifest integration_start_sha is not canonical")

    if workspace_root is None:
        # Canonical layout is <root>/<parent>/parallel/<run_id>.
        if (run_path.name != run_id or run_path.parent.name != "parallel"
                or run_path.parent.parent.name != parent):
            raise ParallelStateError("run directory does not match manifest identity")
        root = _require_root_directory(run_path.parent.parent.parent)
    else:
        root = _require_root_directory(workspace_root)
        expected_run = derive_run_directory(root, parent, run_id)
        if run_path != expected_run:
            raise ParallelStateError("run directory is outside its canonical workspace path")

    config_value = read_canonical_json(run_path, "run-config.json")
    if not isinstance(config_value, dict):
        raise ParallelStateError("run-config.json must be an object")
    normalized_config = normalize_run_config(config_value)
    if config_value != normalized_config:
        raise ParallelStateError("run-config.json is not in normalized form")
    if canonical_json_hash(normalized_config) != config_hash:
        raise ParallelStateError("run_config_hash does not match run-config.json")

    plan_value = read_canonical_json(run_path, "plan.json")
    normalized_plan = _normalize_plan(plan_value)
    if plan_value != normalized_plan:
        raise ParallelStateError("plan.json is not in normalized form")
    if canonical_json_hash(normalized_plan) != plan_hash:
        raise ParallelStateError("plan_hash does not match plan.json")
    _validate_batch_projection(manifest["batches"], normalized_plan)

    entries = manifest["assignments"]
    if not isinstance(entries, list):
        raise ParallelStateError("manifest assignments must be a list")
    orders = [task["order"] for task in normalized_plan]
    if len(entries) != len(orders):
        raise ParallelStateError("manifest must contain one assignment per task")
    batch_by_order = {
        order: batch["index"]
        for batch in manifest["batches"] for order in batch["orders"]}
    task_by_order = {task["order"]: task for task in normalized_plan}
    assignments: dict[int, dict] = {}
    assignment_hashes: dict[int, str] = {}
    expected_files: set[str] = set()
    for expected_order, entry_value in zip(orders, entries):
        entry = _require_exact_fields(
            entry_value, _MANIFEST_ASSIGNMENT_FIELDS, "manifest assignment entry")
        order = _require_positive_int(entry["order"], "manifest assignment order")
        if order != expected_order:
            raise ParallelStateError("manifest assignments must follow plan order")
        relative = f"assignments/task-{order}.json"
        if entry["path"] != relative:
            raise ParallelStateError("manifest assignment path is not canonical")
        try:
            declared_hash = contract.require_config_hash(
                entry["launch_spec_hash"], "launch_spec_hash")
        except contract.ParallelContractError as exc:
            raise ParallelStateError(str(exc)) from exc
        assignment = _require_exact_fields(
            read_canonical_json(run_path, relative),
            _ASSIGNMENT_FIELDS,
            f"assignment task-{order}",
        )
        if assignment["schema_version"] != SCHEMA_VERSION:
            raise ParallelStateError("unsupported assignment schema_version")
        try:
            token_hash = contract.require_config_hash(
                assignment["dispatch_token_hash"], "dispatch_token_hash")
        except contract.ParallelContractError as exc:
            raise ParallelStateError(str(exc)) from exc
        identity = derive_task_identity(
            root, parent, run_id, order, target_repo=normalized_config["repo"])
        task = task_by_order[order]
        expected = {
            "run_id": run_id,
            "parent_workspace": parent,
            "assigned_order": order,
            "batch_index": batch_by_order[order],
            "stack": task.get("stack"),
            "task_hash": canonical_json_hash(task),
            "plan_hash": plan_hash,
            "run_config_hash": config_hash,
            "dispatch_token_hash": token_hash,
            "integration_ref": identity.integration_ref,
            "task_ref": identity.task_ref,
            "worktree_path": str(identity.worktree_path),
            "worker_repo": str(identity.worktree_path),
            "worker_workspace": identity.worker_workspace,
            "worker_workspace_path": str(identity.worker_workspace_path),
        }
        for field_name, expected_value in expected.items():
            if assignment[field_name] != expected_value:
                raise ParallelStateError(
                    f"assignment {order} {field_name} does not match immutable authority")
        command = _require_gate_command(assignment["gate_client_cmd"])
        if assignment["gate_command"] != command:
            raise ParallelStateError("assignment gate command aliases disagree")
        actual_hash = canonical_json_hash(assignment)
        if actual_hash != declared_hash:
            raise ParallelStateError(
                f"assignment {order} hash does not match manifest")
        assignments[order] = assignment
        assignment_hashes[order] = actual_hash
        expected_files.add(f"task-{order}.json")
    _list_exact_regular_files(run_path, "assignments", expected_files)
    return ValidatedRunArtifacts(
        run_dir=run_path,
        manifest=copy.deepcopy(manifest),
        run_config=copy.deepcopy(normalized_config),
        plan=tuple(copy.deepcopy(normalized_plan)),
        assignments=copy.deepcopy(assignments),
        manifest_hash=canonical_json_hash(manifest),
        plan_hash=plan_hash,
        run_config_hash=config_hash,
        assignment_hashes=copy.deepcopy(assignment_hashes),
    )


def build_initial_aggregate(
    run_id: str, plan: Sequence[Mapping[str, object]]
) -> dict:
    """Build the initial aggregate with independent task/result lifecycles."""
    try:
        run_id = contract.require_run_id(run_id)
    except contract.ParallelContractError as exc:
        raise ParallelStateError(str(exc)) from exc
    normalized = _normalize_plan(list(plan))
    batches = project_stack_batches(normalized)
    batch_by_order = {
        order: batch["index"] for batch in batches for order in batch["orders"]}
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "version": 0,
        "control_generation": 0,
        "status": "initializing",
        "terminal_intent": None,
        "pause_generation": 0,
        "batch": batches[0]["index"] if batches else None,
        "tasks": [
            {
                "order": task["order"],
                "batch": batch_by_order[task["order"]],
                "outcome": "pending",
                "resource_state": "queued",
                "restart_count": 0,
                "error": None,
            }
            for task in normalized
        ],
        "error": None,
    }


def validate_aggregate(
    value: object,
    *,
    run_id: str | None = None,
    plan: Sequence[Mapping[str, object]] | None = None,
) -> dict:
    """Validate an aggregate snapshot and return it unchanged."""
    aggregate = _require_exact_fields(value, _AGGREGATE_FIELDS, "aggregate")
    if aggregate["schema_version"] != SCHEMA_VERSION:
        raise ParallelStateError("unsupported aggregate schema_version")
    try:
        actual_run_id = contract.require_run_id(aggregate["run_id"])
    except contract.ParallelContractError as exc:
        raise ParallelStateError(str(exc)) from exc
    if run_id is not None:
        try:
            expected_run_id = contract.require_run_id(run_id)
        except contract.ParallelContractError as exc:
            raise ParallelStateError(str(exc)) from exc
        if actual_run_id != expected_run_id:
            raise ParallelStateError("aggregate run_id mismatch")
    if aggregate["status"] not in RUN_STATUSES:
        raise ParallelStateError("unknown aggregate run status")
    if aggregate["terminal_intent"] not in TERMINAL_INTENTS:
        raise ParallelStateError("unknown aggregate terminal_intent")
    _require_nonnegative_int(aggregate["version"], "aggregate version")
    _require_nonnegative_int(
        aggregate["control_generation"], "aggregate control_generation")
    generation = aggregate["pause_generation"]
    _require_nonnegative_int(generation, "pause_generation")
    batch = aggregate["batch"]
    if batch is not None:
        _require_positive_int(batch, "aggregate batch")
    if aggregate["error"] is not None and not isinstance(aggregate["error"], str):
        raise ParallelStateError("aggregate error must be null or string")
    tasks = aggregate["tasks"]
    if not isinstance(tasks, list) or not tasks:
        raise ParallelStateError("aggregate tasks must be a non-empty list")
    seen: set[int] = set()
    for item in tasks:
        task = _require_exact_fields(item, _AGGREGATE_TASK_FIELDS, "aggregate task")
        order = _require_positive_int(task["order"], "aggregate task order")
        if order in seen:
            raise ParallelStateError("aggregate contains a duplicate task order")
        seen.add(order)
        _require_positive_int(task["batch"], "aggregate task batch")
        if task["outcome"] not in TASK_OUTCOMES:
            raise ParallelStateError("unknown task outcome")
        if task["resource_state"] not in RESOURCE_STATES:
            raise ParallelStateError("unknown task resource_state")
        _require_nonnegative_int(task["restart_count"], "restart_count")
        if task["error"] is not None and not isinstance(task["error"], str):
            raise ParallelStateError("task error must be null or string")
        if task["outcome"] == "pending" and task["resource_state"] == "cleaned":
            raise ParallelStateError("a cleaned task cannot retain pending outcome")
    if [task["order"] for task in tasks] != sorted(seen):
        raise ParallelStateError("aggregate tasks must be in order")
    if plan is not None:
        normalized = _normalize_plan(list(plan))
        expected_orders = [task["order"] for task in normalized]
        if [task["order"] for task in tasks] != expected_orders:
            raise ParallelStateError("aggregate tasks do not match frozen plan")
        batches = project_stack_batches(normalized)
        expected_batches = {
            order: item["index"] for item in batches for order in item["orders"]}
        if any(task["batch"] != expected_batches[task["order"]] for task in tasks):
            raise ParallelStateError("aggregate task batches do not match frozen plan")
        allowed_batches = {item["index"] for item in batches}
        if batch is not None and batch not in allowed_batches:
            raise ParallelStateError("aggregate batch does not exist in frozen plan")
    intent = aggregate["terminal_intent"]
    status = aggregate["status"]
    if status in {"finalizing", "completed"} and intent != "completed":
        raise ParallelStateError("completion states require terminal_intent=completed")
    if status in {"cancel_requested", "finalizing_cancel", "cancelled"} \
            and intent != "cancelled":
        raise ParallelStateError("cancellation states require terminal_intent=cancelled")
    if status == "completed" and any(
            task["outcome"] != "integrated"
            or task["resource_state"] != "cleaned"
            for task in tasks):
        raise ParallelStateError(
            "completed run requires every task integrated and cleaned")
    if status == "cancelled" and any(
            task["outcome"] not in {"integrated", "cancelled"}
            or task["resource_state"] != "cleaned"
            for task in tasks):
        raise ParallelStateError(
            "cancelled run requires terminal outcomes and cleaned resources")
    return aggregate


def _copy_valid_aggregate(value: Mapping[str, object]) -> dict:
    validate_aggregate(value)
    return copy.deepcopy(value)


def set_terminal_intent(value: Mapping[str, object], intent: str) -> dict:
    """Set terminal intent once; it can never be cleared or changed."""
    if intent not in {"completed", "cancelled"}:
        raise ParallelStateError("terminal intent must be completed or cancelled")
    result = _copy_valid_aggregate(value)
    current = result["terminal_intent"]
    if current is not None and current != intent:
        raise ParallelStateError("terminal intent is durable and cannot be changed")
    result["terminal_intent"] = intent
    validate_aggregate(result)
    return result


def transition_run_status(value: Mapping[str, object], target: str) -> dict:
    """Apply one legal run-state transition, including intent-aware replay."""
    if target not in RUN_STATUSES:
        raise ParallelStateError("unknown target run status")
    result = _copy_valid_aggregate(value)
    current = result["status"]
    if target == current:
        return result
    if target not in _RUN_TRANSITIONS[current]:
        raise ParallelStateError(f"illegal run transition: {current} -> {target}")
    intent = result["terminal_intent"]
    if target in {"cancel_requested", "finalizing_cancel", "cancelled"}:
        if intent != "cancelled":
            raise ParallelStateError("cancellation transition requires cancelled intent")
    if target in {"finalizing", "completed"} and intent != "completed":
        raise ParallelStateError("completion transition requires completed intent")
    if current == "blocked":
        required = {
            "initializing": None,
            "finalizing": "completed",
            "finalizing_cancel": "cancelled",
            "cancel_requested": "cancelled",
        }.get(target)
        if intent != required:
            raise ParallelStateError("blocked replay does not match terminal intent")
    result["status"] = target
    validate_aggregate(result)
    return result


def _task_index(value: Mapping[str, object], order: int) -> int:
    order = _require_positive_int(order, "order")
    for index, task in enumerate(value["tasks"]):
        if task["order"] == order:
            return index
    raise ParallelStateError(f"aggregate has no task {order}")


_UNSET = object()


def transition_task(
    value: Mapping[str, object],
    order: int,
    *,
    outcome: str | None = None,
    resource_state: str | None = None,
    explicit_abort: bool = False,
    explicit_resume: bool = False,
    cleanup_retry: bool = False,
    error: str | None | object = _UNSET,
) -> dict:
    """Apply legal, independent outcome/resource transitions for one task."""
    result = _copy_valid_aggregate(value)
    index = _task_index(result, order)
    task = result["tasks"][index]
    old_outcome = task["outcome"]
    old_resource = task["resource_state"]
    if outcome is not None and outcome != old_outcome:
        if outcome not in TASK_OUTCOMES or outcome not in _OUTCOME_TRANSITIONS[old_outcome]:
            raise ParallelStateError(
                f"illegal task outcome transition: {old_outcome} -> {outcome}")
        if outcome == "cancelled" and explicit_abort is not True:
            raise ParallelStateError("cancelled outcome requires explicit Abort")
        task["outcome"] = outcome
    if resource_state is not None and resource_state != old_resource:
        if (resource_state not in RESOURCE_STATES
                or resource_state not in _RESOURCE_TRANSITIONS[old_resource]):
            raise ParallelStateError(
                f"illegal resource transition: {old_resource} -> {resource_state}")
        if old_resource in {"paused", "crashed"} and resource_state == "provisioning" \
                and explicit_resume is not True:
            raise ParallelStateError("resource resume requires an explicit Resume/restart")
        if old_resource == "cleanup_failed" and resource_state == "cleaning" \
                and cleanup_retry is not True:
            raise ParallelStateError("cleanup retry must be explicit")
        if old_resource == "queued" and resource_state == "cleaned":
            resulting_outcome = task["outcome"]
            if explicit_abort is not True or resulting_outcome != "cancelled":
                raise ParallelStateError(
                    "queued resources become cleaned only through explicit Abort")
        if resource_state in {"provisioning", "running", "gate_pending", "gate_claimed"}:
            if task["outcome"] != "pending":
                raise ParallelStateError("a terminal task outcome cannot reactivate work")
            if result["terminal_intent"] is not None:
                raise ParallelStateError("terminal intent forbids worker reactivation")
        task["resource_state"] = resource_state
    if error is not _UNSET:
        if error is not None and not isinstance(error, str):
            raise ParallelStateError("task error must be null or string")
        task["error"] = error
    validate_aggregate(result)
    return result


def advance_pause_generation(value: Mapping[str, object]) -> dict:
    """Increment the durable Pause generation exactly once."""
    result = _copy_valid_aggregate(value)
    result["pause_generation"] += 1
    validate_aggregate(result)
    return result


def increment_restart_count(
    value: Mapping[str, object], order: int, *, limit: int | None = None
) -> dict:
    """Increment one task's monotonic restart counter, optionally enforcing a limit."""
    result = _copy_valid_aggregate(value)
    index = _task_index(result, order)
    next_count = result["tasks"][index]["restart_count"] + 1
    if limit is not None:
        limit = _require_nonnegative_int(limit, "restart limit")
        if next_count > limit:
            raise ParallelStateError("worker restart limit exceeded")
    result["tasks"][index]["restart_count"] = next_count
    validate_aggregate(result)
    return result


def require_worker_assignment_status(value: object) -> str:
    """Validate the stable worker-reported assignment status enum."""
    if not isinstance(value, str) or value not in WORKER_ASSIGNMENT_STATUSES:
        raise ParallelStateError("unknown worker assignment status")
    return value


def validate_receipt_chain(
    receipts: Sequence[Mapping[str, object]],
    artifacts: ValidatedRunArtifacts,
) -> tuple[dict, ...]:
    """Validate the immutable integration chain and all receipt authorities."""
    if not isinstance(artifacts, ValidatedRunArtifacts):
        raise ParallelStateError("artifacts must be a validated run graph")
    if not isinstance(receipts, Sequence) or isinstance(receipts, (str, bytes)):
        raise ParallelStateError("receipts must be a sequence")
    materialized: list[dict] = []
    for item in receipts:
        receipt = _require_exact_fields(
            dict(item) if isinstance(item, Mapping) else item,
            _RECEIPT_FIELDS,
            "receipt",
        )
        materialized.append(copy.deepcopy(receipt))
    try:
        materialized.sort(key=lambda item: _require_positive_int(
            item["sequence"], "receipt sequence"))
    except KeyError as exc:
        raise ParallelStateError("receipt is missing its sequence") from exc
    expected_tip = artifacts.manifest["integration_start_sha"]
    previous_hash: str | None = None
    seen_tasks: set[int] = set()
    seen_requests: set[str] = set()
    plan_orders = {task["order"] for task in artifacts.plan}
    for expected_sequence, receipt in enumerate(materialized, start=1):
        if receipt["schema_version"] != SCHEMA_VERSION:
            raise ParallelStateError("unsupported receipt schema_version")
        if receipt["sequence"] != expected_sequence:
            raise ParallelStateError("receipt sequence must be contiguous from one")
        if receipt["run_id"] != artifacts.manifest["run_id"]:
            raise ParallelStateError("receipt run_id mismatch")
        if receipt["manifest_hash"] != artifacts.manifest_hash:
            raise ParallelStateError("receipt manifest_hash mismatch")
        order = _require_positive_int(receipt["task"], "receipt task")
        if order not in plan_orders or order in seen_tasks:
            raise ParallelStateError("receipt task is unknown or duplicated")
        seen_tasks.add(order)
        if receipt["assignment_hash"] != artifacts.assignment_hashes[order]:
            raise ParallelStateError("receipt assignment_hash mismatch")
        request_id = receipt["request_id"]
        if (not isinstance(request_id, str)
                or REQUEST_ID_RE.fullmatch(request_id) is None
                or request_id in seen_requests):
            raise ParallelStateError("receipt request_id is invalid or duplicated")
        seen_requests.add(request_id)
        if receipt["previous_receipt_hash"] != previous_hash:
            raise ParallelStateError("receipt hash chain is broken")
        try:
            integration_before = contract.require_git_sha(
                receipt["integration_before"], "integration_before")
            validated_sha = contract.require_git_sha(
                receipt["validated_sha"], "validated_sha")
        except contract.ParallelContractError as exc:
            raise ParallelStateError(str(exc)) from exc
        if integration_before != expected_tip:
            raise ParallelStateError("receipt integration chain is broken")
        _require_positive_int(receipt["validated_round"], "validated_round")
        previous_hash = canonical_json_hash(receipt)
        expected_tip = validated_sha
    return tuple(materialized)


def load_receipt_chain(
    run_dir: Path | str,
    *,
    workspace_root: Path | str | None = None,
) -> tuple[ValidatedRunArtifacts, tuple[dict, ...]]:
    """Load present per-task receipts and validate them as one chain."""
    artifacts = validate_run_artifacts(run_dir, workspace_root=workspace_root)
    present: list[dict] = []
    expected_names: set[str] = set()
    receipts_dir = artifacts.run_dir / "receipts"
    try:
        receipts_dir.lstat()
        receipts_present = True
    except FileNotFoundError:
        receipts_present = False
    except OSError as exc:
        raise ParallelStateError(
            f"cannot inspect receipt directory {receipts_dir}: {exc}") from exc
    if receipts_present:
        _check_directory(receipts_dir)
        for order in artifacts.assignments:
            relative = f"receipts/task-{order}.json"
            if _safe_exists(artifacts.run_dir, relative):
                present.append(read_canonical_json(artifacts.run_dir, relative))
                expected_names.add(f"task-{order}.json")
        _list_exact_regular_files(artifacts.run_dir, "receipts", expected_names)
    return artifacts, validate_receipt_chain(present, artifacts)


def project_completed_from_receipts(
    receipts: Sequence[Mapping[str, object]],
    artifacts: ValidatedRunArtifacts,
) -> list[dict]:
    """Project receipt truth into the legacy base ``completed`` shape."""
    chain = validate_receipt_chain(receipts, artifacts)
    by_order = {receipt["task"]: receipt for receipt in chain}
    return [
        {
            "order": order,
            "base_sha": by_order[order]["integration_before"],
            "sha": by_order[order]["validated_sha"],
            "round": by_order[order]["validated_round"],
        }
        for order in (task["order"] for task in artifacts.plan)
        if order in by_order
    ]
