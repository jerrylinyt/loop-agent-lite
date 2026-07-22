"""Durable filesystem spool primitives for parallel-run IPC.

The spool deliberately knows nothing about gate or control payload semantics.
It only supplies the shared durability and state-transition rules:

* a producer fully writes and fsyncs a private staging artifact before publish;
* request ids are immutable and unique across every durable state;
* ``pending -> claimed | cancelled`` is a lock-protected atomic rename;
* terminal responses are atomically published and byte-idempotent; and
* recovery readers fail closed on malformed, linked, or ambiguous artifacts.

All public operations use one cross-process transition lock.  The lock is
advisory, so every legitimate spool writer must use this module.
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from engine import platform_compat as compat


REQUEST_ID_RE = re.compile(r"[0-9a-f]{32}")
REQUEST_STATES = ("pending", "claimed", "cancelled")
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
_STAGING_FILES = frozenset({"request.json", "response.json"})
_LOCK_FILE = ".spool-transition.lock"


class SpoolError(RuntimeError):
    """Base class for durable spool failures."""


class InvalidRequestId(SpoolError, ValueError):
    """A request id is not the canonical 32-character lowercase hex form."""


class DuplicateRequestError(SpoolError):
    """A request id has already appeared in a durable spool state."""


class SpoolNotFoundError(SpoolError):
    """No durable request exists for the requested id."""


class SpoolConflictError(SpoolError):
    """An idempotent operation was replayed with different bytes."""


class SpoolCorruptionError(SpoolError):
    """A durable spool artifact is malformed or its state is ambiguous."""


class SpoolSecurityError(SpoolError):
    """A spool path is linked, non-regular, or outside its owned layout."""


@dataclass(frozen=True)
class SpoolRecord:
    """One validated durable request or response artifact."""

    request_id: str
    state: str
    path: Path
    payload: dict
    raw: bytes


@dataclass(frozen=True)
class TransitionResult:
    """Result of a claim/cancel compare-and-swap transition."""

    record: SpoolRecord
    transitioned: bool

    @property
    def state(self) -> str:
        return self.record.state


@dataclass(frozen=True)
class PublishResult:
    """Result of an idempotent terminal-response publication."""

    record: SpoolRecord
    created: bool


@dataclass(frozen=True)
class StagingArtifact:
    """Best-effort recovery view of a private staging directory."""

    staging_id: str
    path: Path
    kind: str | None
    request_id: str | None
    complete: bool
    error: str | None


@dataclass(frozen=True)
class RecoverySnapshot:
    """Validated stable states plus incomplete/private staging observations."""

    pending: tuple[SpoolRecord, ...]
    claimed: tuple[SpoolRecord, ...]
    cancelled: tuple[SpoolRecord, ...]
    responses: tuple[SpoolRecord, ...]
    staging: tuple[StagingArtifact, ...]


@dataclass(frozen=True)
class _StagedPayload:
    staging_id: str
    directory: Path
    path: Path
    raw: bytes


_LOCAL_LOCKS_GUARD = threading.Lock()
_LOCAL_LOCKS: dict[str, threading.RLock] = {}


def require_request_id(value: object) -> str:
    """Return a canonical spool request id or fail before path construction."""
    if not isinstance(value, str) or REQUEST_ID_RE.fullmatch(value) is None:
        raise InvalidRequestId("request_id 必須是 32 字元小寫 hex")
    return value


def _local_lock_for(path: Path) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(str(path)))
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(key, threading.RLock())


def _lstat_optional(path: Path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SpoolSecurityError(f"無法檢查 spool path {path}:{exc}") from exc


def _is_link(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or compat.is_reparse_point(info)


def _absolute_directory_chain(path: Path) -> tuple[Path, ...]:
    """Return every lexical component from the filesystem anchor to ``path``."""
    absolute = Path(os.path.abspath(str(path)))
    anchor = Path(absolute.anchor)
    if not absolute.anchor:
        raise SpoolSecurityError(f"spool path 缺少 filesystem anchor:{absolute}")
    chain = [anchor]
    current = anchor
    for part in absolute.parts[1:]:
        current = current / part
        chain.append(current)
    return tuple(chain)


def _validate_directory_component(path: Path, info, label: str) -> None:
    if _is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise SpoolSecurityError(f"{label} 含有非實體目錄 component:{path}")


def _ensure_real_directory(path: Path, label: str) -> Path:
    """Create a directory without first traversing an unchecked ancestor link."""
    chain = _absolute_directory_chain(path)
    path = chain[-1]

    anchor_info = _lstat_optional(chain[0])
    if anchor_info is None:
        raise SpoolSecurityError(f"{label} filesystem anchor 不存在:{chain[0]}")
    _validate_directory_component(chain[0], anchor_info, label)

    for component in chain[1:]:
        info = _lstat_optional(component)
        if info is None:
            try:
                os.mkdir(component, 0o700)
            except FileExistsError:
                # Another initializer may have won the creation race.  The
                # lstat below still rejects a link or non-directory winner.
                pass
            except OSError as exc:
                raise SpoolSecurityError(
                    f"{label} 無法建立 directory component {component}:{exc}") from exc
            info = _lstat_optional(component)
            if info is None:
                raise SpoolSecurityError(
                    f"{label} directory component 建立後消失:{component}")
        _validate_directory_component(component, info, label)

    # Recheck the whole lexical chain after creation.  This catches an
    # ancestor swapped while a descendant was being created and ensures later
    # open/rename helpers start from the same fail-closed invariant.
    for component in chain:
        info = _lstat_optional(component)
        if info is None:
            raise SpoolSecurityError(f"{label} directory component 消失:{component}")
        _validate_directory_component(component, info, label)
    return path


def _verify_real_directory(path: Path, label: str):
    chain = _absolute_directory_chain(path)
    final_info = None
    for component in chain:
        info = _lstat_optional(component)
        if info is None:
            raise SpoolSecurityError(f"{label} 必須是既存實體目錄:{component}")
        _validate_directory_component(component, info, label)
        final_info = info
    return final_info


def _regular_info(path: Path, label: str):
    info = _lstat_optional(path)
    if info is None:
        return None
    if _is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SpoolSecurityError(f"{label} 必須是單一 regular file:{path}")
    return info


def _safe_open_regular(path: Path, flags: int, mode: int = 0o600) -> int:
    """Open a regular file without following links, including on Windows."""
    path = Path(path)
    parent_info = _verify_real_directory(path.parent, "artifact 父目錄")
    before = _regular_info(path, "spool artifact")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not compat.IS_WINDOWS and not nofollow:
        raise SpoolSecurityError("此 POSIX 平台不支援 O_NOFOLLOW 安全檔案操作")
    binary = getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags | nofollow | binary, mode)
    except FileNotFoundError:
        raise
    except OSError as exc:
        if exc.errno in (getattr(errno, "ELOOP", -1), getattr(errno, "EMLINK", -1)):
            raise SpoolSecurityError(f"spool artifact 不可為 link:{path}") from exc
        raise SpoolSecurityError(f"無法安全開啟 spool artifact {path}:{exc}") from exc
    try:
        opened = os.fstat(fd)
        after = path.lstat()
        parent_after = path.parent.lstat()
        if (_is_link(after) or not stat.S_ISREG(after.st_mode)
                or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
                or _is_link(parent_after) or not stat.S_ISDIR(parent_after.st_mode)
                or (parent_info.st_dev, parent_info.st_ino)
                != (parent_after.st_dev, parent_after.st_ino)
                or (before is not None and (before.st_dev, before.st_ino)
                    != (after.st_dev, after.st_ino))):
            raise SpoolSecurityError(f"spool artifact 開啟期間被替換:{path}")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _fsync_directory(path: Path) -> None:
    """Durably order a rename on platforms that support directory fsync."""
    if compat.IS_WINDOWS:
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise SpoolError(f"無法開啟 spool directory 進行 fsync:{path}:{exc}") from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise SpoolError(f"無法 fsync spool directory {path}:{exc}") from exc
    finally:
        os.close(fd)


def _replace_with_retry(source: Path, target: Path) -> None:
    attempts = 100 if compat.IS_WINDOWS else 1
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt + 1 == attempts:
                raise
            time.sleep(0.005)


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key:{key}")
        result[key] = value
    return result


def _decode_json(raw: bytes, *, path: Path, request_id: str) -> dict:
    try:
        text = raw.decode("utf-8", errors="strict")
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SpoolCorruptionError(f"spool JSON 損壞 {path}:{exc}") from exc
    if not isinstance(payload, dict):
        raise SpoolCorruptionError(f"spool JSON 頂層必須是 object:{path}")
    if payload.get("request_id") != request_id:
        raise SpoolCorruptionError(
            f"spool filename/payload request_id 不符:{path.name}")
    return payload


def _canonical_json(request_id: str, payload: Mapping) -> tuple[bytes, dict]:
    request_id = require_request_id(request_id)
    if not isinstance(payload, Mapping):
        raise SpoolCorruptionError("spool payload 必須是 JSON object")
    materialized = dict(payload)
    if materialized.get("request_id") != request_id:
        raise SpoolCorruptionError("payload.request_id 必須與 spool request_id 完全一致")
    try:
        raw = (json.dumps(
            materialized, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise SpoolCorruptionError(f"spool payload 無法編碼為 JSON:{exc}") from exc
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise SpoolCorruptionError(
            f"spool payload 超過 {MAX_ARTIFACT_BYTES} bytes")
    # Decode our own bytes so returned records never retain mutable/custom Mapping values.
    return raw, _decode_json(raw, path=Path("<new-payload>"), request_id=request_id)


class DurableSpool:
    """Generic durable request/response spool.

    ``requests_root`` owns ``staging/pending/claimed/cancelled``.  Gate IPC can
    pass the sibling ``responses`` directory explicitly; control IPC can use
    the default ``requests_root/responses`` layout.
    """

    def __init__(self, requests_root: Path, *, responses_root: Path | None = None):
        # Reject an overlapping layout before creating *any* path.  Otherwise
        # an invalid responses_root nested under pending/claimed could mutate
        # and permanently corrupt an existing spool even though construction
        # ultimately raises.
        request_candidate = Path(os.path.abspath(os.fspath(Path(requests_root).expanduser())))
        chosen_responses = (request_candidate / "responses"
                            if responses_root is None
                            else Path(os.path.abspath(
                                os.fspath(Path(responses_root).expanduser()))))
        staging_candidate = request_candidate / "staging"
        state_candidates = {
            state: request_candidate / state for state in REQUEST_STATES
        }
        lock_candidate = request_candidate / _LOCK_FILE
        reserved = {
            request_candidate, staging_candidate, lock_candidate,
            *state_candidates.values(),
        }
        if chosen_responses in reserved:
            raise SpoolSecurityError("response root 不可與 request/state 目錄重疊")
        if chosen_responses in request_candidate.parents:
            raise SpoolSecurityError("response root 不可包含 request root")
        for directory in (
                staging_candidate, lock_candidate, *state_candidates.values()):
            if (chosen_responses in directory.parents
                    or directory in chosen_responses.parents):
                raise SpoolSecurityError("response root 不可與 staging/state tree 重疊")

        self.requests_root = _ensure_real_directory(
            request_candidate, "spool request root")
        self.staging_dir = _ensure_real_directory(
            self.requests_root / "staging", "spool staging")
        self.state_dirs = {
            state: _ensure_real_directory(
                self.requests_root / state, f"spool {state}")
            for state in REQUEST_STATES
        }
        self.responses_root = _ensure_real_directory(chosen_responses, "spool responses")
        request_device = self.requests_root.lstat().st_dev
        owned_dirs = (
            self.staging_dir, *self.state_dirs.values(), self.responses_root,
        )
        if any(directory.lstat().st_dev != request_device for directory in owned_dirs):
            raise SpoolSecurityError("request/response spool 必須位於同一 filesystem")
        self.lock_path = self.requests_root / _LOCK_FILE
        self._local_lock = _local_lock_for(self.lock_path)
        fd = _safe_open_regular(self.lock_path, os.O_RDWR | os.O_CREAT)
        os.close(fd)

    def _assert_layout(self) -> None:
        request_info = _verify_real_directory(self.requests_root, "spool request root")
        owned_infos = [
            _verify_real_directory(self.staging_dir, "spool staging"),
        ]
        for state, directory in self.state_dirs.items():
            owned_infos.append(_verify_real_directory(directory, f"spool {state}"))
        owned_infos.append(
            _verify_real_directory(self.responses_root, "spool responses"))
        if any(info.st_dev != request_info.st_dev for info in owned_infos):
            raise SpoolSecurityError("request/response spool 不在同一 filesystem")
        _regular_info(self.lock_path, "spool transition lock")

    @contextmanager
    def _transition_lock(self):
        self._assert_layout()
        with self._local_lock:
            fd = _safe_open_regular(self.lock_path, os.O_RDWR | os.O_CREAT)
            lock_file = os.fdopen(fd, "a+b", closefd=True)
            try:
                compat.lock_file(lock_file, blocking=True)
                self._assert_layout()
                yield
            except OSError as exc:
                raise SpoolError(f"spool transition lock/operation 失敗:{exc}") from exc
            finally:
                try:
                    compat.unlock_file(lock_file)
                except OSError:
                    pass
                lock_file.close()

    @staticmethod
    def _filename(request_id: str) -> str:
        return f"{require_request_id(request_id)}.json"

    def _state_path(self, state: str, request_id: str) -> Path:
        if state not in REQUEST_STATES:
            raise ValueError(f"unknown spool state:{state}")
        return self.state_dirs[state] / self._filename(request_id)

    def _response_path(self, request_id: str) -> Path:
        return self.responses_root / self._filename(request_id)

    def _read_record_path(self, path: Path, state: str, request_id: str) -> SpoolRecord:
        request_id = require_request_id(request_id)
        info = _regular_info(path, f"spool {state}")
        if info is None:
            raise FileNotFoundError(path)
        if info.st_size > MAX_ARTIFACT_BYTES:
            raise SpoolCorruptionError(f"spool artifact 過大:{path}")
        fd = _safe_open_regular(path, os.O_RDONLY)
        try:
            with os.fdopen(fd, "rb", closefd=True) as stream:
                raw = stream.read(MAX_ARTIFACT_BYTES + 1)
        except OSError as exc:
            raise SpoolCorruptionError(f"無法讀取 spool artifact {path}:{exc}") from exc
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise SpoolCorruptionError(f"spool artifact 過大:{path}")
        payload = _decode_json(raw, path=path, request_id=request_id)
        return SpoolRecord(request_id, state, path, payload, raw)

    def _locate_request_locked(self, request_id: str) -> SpoolRecord | None:
        request_id = require_request_id(request_id)
        records = []
        for state in REQUEST_STATES:
            path = self._state_path(state, request_id)
            if _regular_info(path, f"spool {state}") is not None:
                records.append(self._read_record_path(path, state, request_id))
        if len(records) > 1:
            states = ",".join(record.state for record in records)
            raise SpoolCorruptionError(
                f"request_id {request_id} 同時存在多個 state:{states}")
        return records[0] if records else None

    def _read_response_locked(self, request_id: str) -> SpoolRecord | None:
        path = self._response_path(request_id)
        if _regular_info(path, "spool response") is None:
            return None
        return self._read_record_path(path, "response", request_id)

    def _stage_bytes(self, kind: str, raw: bytes) -> _StagedPayload:
        if kind not in _STAGING_FILES:
            raise ValueError(f"invalid staging kind:{kind}")
        self._assert_layout()
        for _attempt in range(100):
            staging_id = uuid.uuid4().hex
            directory = self.staging_dir / staging_id
            try:
                os.mkdir(directory, 0o700)
                break
            except FileExistsError:
                continue
            except OSError as exc:
                raise SpoolError(f"無法建立 private staging directory:{exc}") from exc
        else:  # pragma: no cover - UUID collision exhaustion is not realistic
            raise SpoolError("無法取得唯一 staging id")
        _verify_real_directory(directory, "private staging")
        path = directory / kind
        try:
            fd = _safe_open_regular(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            _fsync_directory(directory)
            return _StagedPayload(staging_id, directory, path, raw)
        except BaseException:
            self._cleanup_staging_path(directory, path)
            raise

    @staticmethod
    def _cleanup_staging_path(directory: Path, path: Path) -> None:
        try:
            info = _regular_info(path, "staging cleanup artifact")
            if info is not None:
                path.unlink()
        except (OSError, SpoolError):
            return
        try:
            directory.rmdir()
        except OSError:
            pass

    def publish_request(self, request_id: str, payload: Mapping) -> SpoolRecord:
        """Stage, fsync, and atomically publish one never-reusable request id."""
        request_id = require_request_id(request_id)
        raw, materialized = _canonical_json(request_id, payload)
        staged = self._stage_bytes("request.json", raw)
        target = self._state_path("pending", request_id)
        published = False
        try:
            with self._transition_lock():
                existing = self._locate_request_locked(request_id)
                response = self._read_response_locked(request_id)
                if existing is not None or response is not None:
                    where = existing.state if existing is not None else "response"
                    raise DuplicateRequestError(
                        f"request_id {request_id} 已存在於 {where}")
                if _lstat_optional(target) is not None:
                    raise DuplicateRequestError(f"request_id {request_id} 已存在")
                _replace_with_retry(staged.path, target)
                published = True
                _fsync_directory(self.state_dirs["pending"])
                _fsync_directory(staged.directory)
            return SpoolRecord(request_id, "pending", target, materialized, raw)
        finally:
            self._cleanup_staging_path(staged.directory, staged.path)
            if published:
                try:
                    _fsync_directory(self.staging_dir)
                except SpoolError:
                    # The request itself is already durably published; an empty
                    # staging directory remnant is recovery-visible, not ambiguity.
                    pass

    def _transition_request(self, request_id: str, destination: str) -> TransitionResult:
        request_id = require_request_id(request_id)
        if destination not in {"claimed", "cancelled"}:
            raise ValueError(f"invalid request destination:{destination}")
        with self._transition_lock():
            current = self._locate_request_locked(request_id)
            if current is None:
                raise SpoolNotFoundError(f"request_id {request_id} 不存在")
            if current.state != "pending":
                return TransitionResult(current, False)
            target = self._state_path(destination, request_id)
            if _lstat_optional(target) is not None:
                raise SpoolCorruptionError(
                    f"request_id {request_id} destination 已存在:{destination}")
            _replace_with_retry(current.path, target)
            _fsync_directory(self.state_dirs["pending"])
            _fsync_directory(self.state_dirs[destination])
            moved = SpoolRecord(
                request_id, destination, target, current.payload, current.raw)
            return TransitionResult(moved, True)

    def claim_request(self, request_id: str) -> TransitionResult:
        """CAS ``pending -> claimed``; a concurrent cancel can be the only winner."""
        return self._transition_request(request_id, "claimed")

    def cancel_request(self, request_id: str) -> TransitionResult:
        """CAS ``pending -> cancelled``; a concurrent claim can be the only winner."""
        return self._transition_request(request_id, "cancelled")

    def publish_response(self, request_id: str, payload: Mapping) -> PublishResult:
        """Atomically publish a terminal response; exact-byte replay is idempotent."""
        request_id = require_request_id(request_id)
        raw, materialized = _canonical_json(request_id, payload)
        staged = self._stage_bytes("response.json", raw)
        target = self._response_path(request_id)
        published = False
        try:
            with self._transition_lock():
                request = self._locate_request_locked(request_id)
                if request is None:
                    raise SpoolNotFoundError(
                        f"response 的 request_id {request_id} 不存在")
                existing = self._read_response_locked(request_id)
                if existing is not None:
                    if existing.raw != raw:
                        raise SpoolConflictError(
                            f"request_id {request_id} response replay bytes 不一致")
                    return PublishResult(existing, False)
                if _lstat_optional(target) is not None:
                    raise SpoolSecurityError(f"response target 不是安全的 absent path:{target}")
                _replace_with_retry(staged.path, target)
                published = True
                _fsync_directory(self.responses_root)
                _fsync_directory(staged.directory)
            record = SpoolRecord(request_id, "response", target, materialized, raw)
            return PublishResult(record, True)
        finally:
            self._cleanup_staging_path(staged.directory, staged.path)
            if published:
                try:
                    _fsync_directory(self.staging_dir)
                except SpoolError:
                    pass

    def get_request(self, request_id: str) -> SpoolRecord | None:
        """Read the request's single durable state under the transition lock."""
        with self._transition_lock():
            return self._locate_request_locked(require_request_id(request_id))

    def get_response(self, request_id: str) -> SpoolRecord | None:
        """Read a response and reject an orphan response as corruption."""
        request_id = require_request_id(request_id)
        with self._transition_lock():
            response = self._read_response_locked(request_id)
            if response is not None and self._locate_request_locked(request_id) is None:
                raise SpoolCorruptionError(
                    f"response {request_id} 沒有對應 durable request")
            return response

    def _list_state_locked(self, state: str) -> tuple[SpoolRecord, ...]:
        directory = self.state_dirs[state]
        records = []
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise SpoolCorruptionError(f"無法列舉 spool {state}:{exc}") from exc
        for path in children:
            if not path.name.endswith(".json"):
                raise SpoolCorruptionError(f"spool {state} 含未知 artifact:{path.name}")
            request_id = path.name[:-5]
            try:
                require_request_id(request_id)
            except InvalidRequestId as exc:
                raise SpoolCorruptionError(
                    f"spool {state} filename 不合法:{path.name}") from exc
            records.append(self._read_record_path(path, state, request_id))
        return tuple(records)

    def _collect_requests_locked(
            self, states=REQUEST_STATES) -> tuple[SpoolRecord, ...]:
        records = tuple(
            record
            for current in states
            for record in self._list_state_locked(current)
        )
        ids = [record.request_id for record in records]
        if len(ids) != len(set(ids)):
            raise SpoolCorruptionError(
                "同一 request_id 同時存在多個 durable state")
        return records

    def list_requests(self, state: str | None = None) -> tuple[SpoolRecord, ...]:
        """List validated stable requests; private staging is never visible here."""
        if state is not None and state not in REQUEST_STATES:
            raise ValueError(f"unknown spool state:{state}")
        with self._transition_lock():
            # Validate global uniqueness even when the caller only wants one
            # state; a filtered view must not hide an ambiguous durable id.
            records = self._collect_requests_locked()
            if state is not None:
                records = tuple(
                    record for record in records if record.state == state)
            return tuple(sorted(records, key=lambda record: record.request_id))

    def _list_responses_locked(self) -> tuple[SpoolRecord, ...]:
        records = []
        try:
            children = sorted(self.responses_root.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise SpoolCorruptionError(f"無法列舉 spool responses:{exc}") from exc
        for path in children:
            if not path.name.endswith(".json"):
                raise SpoolCorruptionError(f"responses 含未知 artifact:{path.name}")
            request_id = path.name[:-5]
            try:
                require_request_id(request_id)
            except InvalidRequestId as exc:
                raise SpoolCorruptionError(
                    f"response filename 不合法:{path.name}") from exc
            records.append(self._read_record_path(path, "response", request_id))
        return tuple(records)

    def list_responses(self) -> tuple[SpoolRecord, ...]:
        """List validated responses and reject any response without a request."""
        with self._transition_lock():
            responses = self._list_responses_locked()
            request_ids = {
                record.request_id for record in self._collect_requests_locked()
            }
            for response in responses:
                if response.request_id not in request_ids:
                    raise SpoolCorruptionError(
                        f"orphan response:{response.request_id}")
            return responses

    def _scan_staging(self) -> tuple[StagingArtifact, ...]:
        artifacts = []
        try:
            children = sorted(self.staging_dir.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise SpoolCorruptionError(f"無法列舉 spool staging:{exc}") from exc
        for directory in children:
            info = _lstat_optional(directory)
            # A live producer cleans its private staging directory after the
            # transition lock is released.  Vanishing between iterdir/lstat is
            # therefore a benign observation race, not durable corruption.
            if info is None:
                continue
            if (_is_link(info) or not stat.S_ISDIR(info.st_mode)
                    or REQUEST_ID_RE.fullmatch(directory.name) is None):
                raise SpoolSecurityError(
                    f"staging entry 必須是 32 hex 實體目錄:{directory}")
            try:
                members = sorted(directory.iterdir(), key=lambda path: path.name)
            except OSError as exc:
                artifacts.append(StagingArtifact(
                    directory.name, directory, None, None, False, str(exc)))
                continue
            if len(members) != 1 or members[0].name not in _STAGING_FILES:
                names = [member.name for member in members]
                artifacts.append(StagingArtifact(
                    directory.name, directory, None, None, False,
                    f"staging 必須恰有 request.json 或 response.json，實得 {names}"))
                continue
            path = members[0]
            kind = "request" if path.name == "request.json" else "response"
            try:
                info = _regular_info(path, "staging artifact")
                if info is None or info.st_size > MAX_ARTIFACT_BYTES:
                    raise SpoolCorruptionError("staging artifact 缺少或過大")
                fd = _safe_open_regular(path, os.O_RDONLY)
                with os.fdopen(fd, "rb", closefd=True) as stream:
                    raw = stream.read(MAX_ARTIFACT_BYTES + 1)
                if len(raw) > MAX_ARTIFACT_BYTES:
                    raise SpoolCorruptionError("staging artifact 過大")
                try:
                    decoded = json.loads(
                        raw.decode("utf-8", errors="strict"),
                        object_pairs_hook=_reject_duplicate_keys,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    raise SpoolCorruptionError(f"staging JSON 尚未完整:{exc}") from exc
                request_id = decoded.get("request_id") if isinstance(decoded, dict) else None
                require_request_id(request_id)
                artifacts.append(StagingArtifact(
                    directory.name, directory, kind, request_id, True, None))
            except (SpoolError, OSError, ValueError) as exc:
                artifacts.append(StagingArtifact(
                    directory.name, directory, kind, None, False, str(exc)))
        return tuple(artifacts)

    def scan_recovery(self) -> RecoverySnapshot:
        """Return a fail-closed stable-state scan plus private staging remnants."""
        with self._transition_lock():
            by_state = {
                state: self._list_state_locked(state)
                for state in REQUEST_STATES
            }
            all_requests = self._collect_requests_locked()
            request_ids = [record.request_id for record in all_requests]
            responses = self._list_responses_locked()
            known = set(request_ids)
            for response in responses:
                if response.request_id not in known:
                    raise SpoolCorruptionError(
                        f"recovery scan 發現 orphan response:{response.request_id}")
            staging = self._scan_staging()
            return RecoverySnapshot(
                pending=by_state["pending"],
                claimed=by_state["claimed"],
                cancelled=by_state["cancelled"],
                responses=responses,
                staging=staging,
            )


__all__ = [
    "DurableSpool",
    "DuplicateRequestError",
    "InvalidRequestId",
    "PublishResult",
    "RecoverySnapshot",
    "REQUEST_STATES",
    "SpoolConflictError",
    "SpoolCorruptionError",
    "SpoolError",
    "SpoolNotFoundError",
    "SpoolRecord",
    "SpoolSecurityError",
    "StagingArtifact",
    "TransitionResult",
    "require_request_id",
]
