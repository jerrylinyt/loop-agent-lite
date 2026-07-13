#!/usr/bin/env python3
"""Parallel track supervisor with isolated worktrees and expected-old CAS integration."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid

from engine import loop as L
from engine.work import validate_plan


SCHEMA_VERSION = 1
TRACK_STATUSES = {"pending", "running", "merge-ready", "merging", "repairing",
                  "merged", "stopped", "failed", "cleaned"}
MERGE_STAGES = {"prepared", "ref-updated", "validating", "validated",
                "rollback-prepared", "rolled-back"}
OID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
TRACK_ENV_PLACEHOLDERS = {"{track}", "{safe_track}", "{index}", "{port}"}
FLEET_PHASES = {"planning", "splitting", "awaiting-approval", "exec", "merging",
                "final", "stopping", "stopped", "cleaning", "done", "failed"}
RESUMABLE_PHASES = {"planning", "splitting", "exec", "final", "cleaning"}


def recoverable_phase(state: dict) -> str:
    """Map every terminal/intermediate phase to a phase execute() can safely resume."""
    phase = state.get("phase")
    if phase in RESUMABLE_PHASES:
        return phase
    if phase == "merging":
        return "final" if str((state.get("merge_tx") or {}).get("track") or "") == "@final" else "exec"
    if phase in {"stopping", "stopped", "awaiting-approval", "failed"}:
        prior = state.get("resume_phase")
        if prior in RESUMABLE_PHASES:
            return prior
        return "splitting" if phase == "awaiting-approval" else "exec"
    if phase == "done":
        return "cleaning"
    return "exec"


def mark_fleet_interrupted(state: dict) -> None:
    state["resume_phase"] = recoverable_phase(state)
    state["phase"] = "stopped"
    state.setdefault("loop", {})["pid"] = None


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Parallel loop fleet supervisor")
    p.add_argument("--repo", default=".")
    p.add_argument("--name", default="")
    p.add_argument("--integration-branch", default="")
    p.add_argument("--goal", default="goal.md")
    p.add_argument("--plan-doc", default="")
    p.add_argument("--agent-cmd", required=True)
    p.add_argument("--validate-cmd", required=True)
    p.add_argument("--max-parallel", type=int, default=4)
    p.add_argument("--merge-threshold", type=int, default=2)
    p.add_argument("--flag-threshold", type=int, default=L.FLAG_THRESHOLD)
    p.add_argument("--done-threshold", type=int, default=L.DONE_THRESHOLD)
    p.add_argument("--red-limit", type=int, default=L.RED_LIMIT)
    p.add_argument("--stall-limit", type=int, default=L.STALL_LIMIT)
    p.add_argument("--round-timeout", type=float, default=L.ROUND_TIMEOUT_MIN)
    p.add_argument("--validate-timeout", type=float, default=L.VALIDATE_TIMEOUT_SEC)
    p.add_argument("--agent-backoff-max", type=float, default=L.AGENT_BACKOFF_MAX_SEC)
    p.add_argument("--max-child-restarts", type=int, default=0)
    p.add_argument("--track-env-json", default="{}")
    p.add_argument("--track-port-base", type=int, default=0)
    p.add_argument("--pause-after-plan", action="store_true")
    p.add_argument("--import-plan", default="")
    p.add_argument("--consume-import-plan", action="store_true")
    p.add_argument("--notify-cmd", default="")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--expected-run-id", default="", help=argparse.SUPPRESS)
    return p


def run(repo: Path, *args: str, check=True) -> subprocess.CompletedProcess:
    result = subprocess.run(list(args), cwd=repo, text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {shlex.join(args)}\n{result.stdout}\n{result.stderr}")
    return result


def git(repo: Path, *args: str, check=True) -> subprocess.CompletedProcess:
    return run(repo, "git", *args, check=check)


def process_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (TypeError, ValueError, ProcessLookupError, PermissionError):
        return False


def validate_track_env(value) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("track env 必須是 JSON object")
    normalized = {}
    reserved = {"TMPDIR", "XDG_CACHE_HOME", "npm_config_cache", "LOOP_WS",
                "LOOP_ROUND_TOKEN", "PYTHONPATH", "PATH", "HOME", "SHELL",
                "USER", "LOGNAME", "PWD", "OLDPWD", "SHLVL", "IFS", "ENV",
                "BASH_ENV", "ZDOTDIR", "VIRTUAL_ENV", "JAVA_TOOL_OPTIONS",
                "JDK_JAVA_OPTIONS"}
    protected_prefixes = ("LOOP_", "GIT_", "PYTHON", "LD_", "DYLD_", "CODEX_",
                          "OPENAI_", "SSH_", "AWS_", "GOOGLE_", "AZURE_", "NODE_",
                          "NPM_", "BUN_", "DENO_", "RUBY", "PERL")
    secret_name = re.compile(
        r"(?:^|_)(?:TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIALS?|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY)(?:_|$)")
    for key, template in value.items():
        if (not isinstance(key, str) or ENV_NAME_RE.fullmatch(key) is None or
                key in reserved or key.startswith(protected_prefixes)):
            raise ValueError(f"track env 名稱不可覆蓋 coordinator/runtime：{key!r}")
        if secret_name.search(key):
            raise ValueError(f"track env 不可包含會被持久化的 credential 欄位：{key}")
        if not isinstance(template, str):
            raise ValueError(f"track env {key} 值必須是字串 template")
        stripped = template
        for marker in TRACK_ENV_PLACEHOLDERS:
            stripped = stripped.replace(marker, "")
        if "{" in stripped or "}" in stripped:
            raise ValueError(f"track env {key} 只允許 placeholders {sorted(TRACK_ENV_PLACEHOLDERS)}")
        normalized[key] = template
    return normalized


class Fleet:
    def __init__(self, args):
        self.args = args
        self.repo = Path(args.repo).expanduser().resolve()
        self.name = args.name or self.repo.name
        L.require_workspace_name(self.name)
        self.parent = L.workspace_path(L.WORKSPACE_ROOT, self.name)
        self.fleet_path = self.parent / "fleet.json"
        self.checkpoint_path = self.parent / "fleet.last-good.json"
        self.control_path = self.parent / "fleet-control.json"
        self.console = self.parent / "console.log"
        self.children: dict[str, subprocess.Popen] = {}
        self.planning_process: subprocess.Popen | None = None
        self.commands: dict[str, list[str]] = {}
        self.state: dict = {}
        self.integration_worktree_lock_acquired = False
        self.pending_runtime_marker: dict | None = None

    def log(self, message: str):
        line = f"[{time.strftime('%H:%M:%S')}] 🔀 Fleet｜{message}"
        print(line, flush=True)
        L.append_regular_text(self.console, line + "\n")

    def crash_point(self, name: str):
        """Deterministic process-death hook used only by the crash-matrix harness."""
        if os.environ.get("LOOP_FLEET_CRASH_AT") == name:
            self.log(f"fault injection crash｜{name}")
            os._exit(97)

    def save(self):
        phase = self.state.get("phase")
        history = self.state.setdefault("phase_history", [])
        now = time.time()
        now_iso = L.datetime.now().astimezone().isoformat(timespec="milliseconds")
        if phase and (not history or history[-1].get("phase") != phase):
            if history and history[-1].get("ended_at") is None:
                history[-1]["ended_at"] = now_iso
                history[-1]["duration_seconds"] = round(
                    max(0.0, now - float(history[-1].get("started_epoch", now))), 3)
            history.append({"phase": phase, "started_at": now_iso, "started_epoch": now,
                            "ended_at": None, "duration_seconds": None})
        if phase in {"done", "failed"} and history and history[-1].get("ended_at") is None:
            history[-1]["ended_at"] = now_iso
            history[-1]["duration_seconds"] = round(
                max(0.0, now - float(history[-1].get("started_epoch", now))), 3)
        if len(history) > 200:
            del history[:-200]
        for track in self.state.get("tracks") or []:
            status = track.get("status")
            status_history = track.setdefault("status_history", [])
            if status and (not status_history or status_history[-1].get("status") != status):
                status_history.append({"status": status, "at": now_iso})
            if len(status_history) > 200:
                del status_history[:-200]
        transaction = self.state.get("merge_tx")
        if isinstance(transaction, dict):
            merge_history = self.state.setdefault("merge_history", [])
            identity = (transaction.get("track"), transaction.get("candidate_sha"), transaction.get("stage"))
            previous = merge_history[-1] if merge_history else {}
            if identity != (previous.get("track"), previous.get("candidate_sha"), previous.get("stage")):
                merge_history.append({key: transaction.get(key) for key in (
                    "track", "expected_sha", "candidate_sha", "stage", "validation_error")})
                merge_history[-1]["at"] = now_iso
            if len(merge_history) > 500:
                del merge_history[:-500]
        self.state["phase_started_at"] = history[-1]["started_at"] if history else None
        data = json.dumps(self.state, ensure_ascii=False, indent=2).encode()
        L.atomic_write_bytes(self.fleet_path, data)
        L.atomic_write_bytes(self.checkpoint_path, data)

    def stop_requested(self) -> bool:
        """Read a run-bound sideband request without letting Dashboard write fleet truth."""
        try:
            payload = json.loads(L.read_regular_text(self.control_path, "fleet control"))
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return False
        return (payload.get("schema_version") == 1 and
                payload.get("run_id") == self.state.get("run_id") and
                payload.get("action") == "stop")

    def request_round_stop(self, workspace: Path, state: dict) -> bool:
        """Ask one loop session to finish its current round; stale/missing sessions are ignored."""
        loop_state = state.get("loop") if isinstance(state.get("loop"), dict) else {}
        pid, session_id = loop_state.get("pid"), loop_state.get("session_id")
        if not pid or not session_id or not process_alive(pid):
            return False
        if (L.stop_after_round_requested(workspace, pid, session_id) or
                L.stop_after_round_claimed(workspace, pid, session_id)):
            return True
        payload = {"pid": int(pid), "session_id": session_id,
                   "requested_at": L.datetime.now().astimezone().isoformat(timespec="seconds")}
        L.atomic_write_bytes(workspace / L.STOP_AFTER_ROUND_FILE,
                             json.dumps(payload, ensure_ascii=False).encode())
        return True

    def mark_stopped(self, from_phase: str):
        self.state.update(phase="stopped", resume_phase=from_phase)
        self.state.setdefault("loop", {})["pid"] = None
        self.save()
        self.log(f"已安全停止｜可用同一 run-id {self.state['run_id']} resume")
        L.notify(self.args.notify_cmd, "fleet_stopped", self.name)

    def load(self):
        candidates = []
        errors = []
        for path in (self.fleet_path, self.checkpoint_path):
            try:
                value = json.loads(L.read_regular_text(path, path.name))
                self.validate_state(value)
                self.state = value
                try:
                    parent_state, _parent_data, _parent_recovered = L.load_checkpointed_state(
                        self.parent / "state.json", repair=False)
                except FileNotFoundError:
                    if (value.get("phase") != "planning" or value.get("plan") or
                            value.get("dashboard_revision", 0) != 0):
                        raise RuntimeError("fleet truth 缺少 parent state mirror")
                else:
                    mirror_revision = parent_state.get("fleet_truth_revision")
                    if (parent_state.get("workspace_kind") != "fleet-parent" or
                            parent_state.get("fleet_run_id") != value.get("run_id") or
                            (mirror_revision is not None and
                             value.get("dashboard_revision", 0) != mirror_revision)):
                        raise RuntimeError("fleet truth 與 parent mirror revision/identity 不符")
                self.validate_loaded_base_identity()
                # Runtime/config identity is frozen by this candidate.  CLI resume
                # arguments are only locators and must not redefine child env/commands.
                self.apply_frozen_resume_config()
                self.validate_loaded_identity()
                candidates.append(value)
            except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
                errors.append(f"{path.name}: {error}")
                continue
        if not candidates:
            self.state = {}
            detail = "; ".join(errors) if errors else "沒有可讀候選"
            raise RuntimeError(f"fleet.json 與 checkpoint 都不能安全 resume：{detail}")
        self.state = candidates[0]
        self.apply_frozen_resume_config()
        self.validate_loaded_identity()

    def validate_loaded_base_identity(self):
        """Validate enough immutable identity to trust this candidate's frozen config."""
        state = self.state
        if Path(state.get("integration_worktree", "")).resolve() != self.repo:
            raise RuntimeError("resume repo 與 fleet.json 不符")
        if state.get("integration_ref") != self.integration_ref:
            raise RuntimeError("resume integration ref 與 fleet.json 不符")
        if not OID_RE.fullmatch(str(state.get("expected_integration_sha", ""))):
            raise RuntimeError("fleet expected integration SHA 不合法")
        stored_plan = state.get("plan")
        if not stored_plan and state.get("plan_sha256") is None:
            plan = []
        else:
            plan, errors = validate_plan(stored_plan)
            if errors:
                raise RuntimeError("fleet master plan 不合法：" + "; ".join(errors))
        raw = json.dumps(plan, ensure_ascii=False, separators=(",", ":")).encode()
        digest = hashlib.sha256(raw).hexdigest() if plan else None
        if state.get("plan_sha256") != digest:
            raise RuntimeError("fleet master plan hash 不符，拒絕推測 resume")

    def apply_frozen_resume_config(self):
        """Use the already identity-validated recovery candidate as the only config truth."""
        config = self.state["config"]
        for name in ("agent_cmd", "validate_cmd", "goal", "plan_doc", "max_parallel",
                     "merge_threshold", "done_threshold", "flag_threshold", "red_limit", "stall_limit",
                     "round_timeout", "validate_timeout", "agent_backoff_max", "max_child_restarts",
                     "notify_cmd", "pause_after_plan", "track_env", "track_port_base"):
            setattr(self.args, name, config[name])

    @staticmethod
    def allocate_dynamic_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            return int(server.getsockname()[1])

    def render_track_env(self, track_name: str, safe: str, index: int, port: int):
        runtime = self.parent / "runtime" / safe
        result = {"TMPDIR": str(runtime / "tmp"),
                  "XDG_CACHE_HOME": str(runtime / "cache"),
                  "npm_config_cache": str(runtime / "cache" / "npm"),
                  "LOOP_TRACK_NAME": track_name,
                  "LOOP_TRACK_SAFE_NAME": safe,
                  "LOOP_TRACK_INDEX": str(index),
                  "LOOP_TRACK_PORT": str(port)}
        replacements = {"{track}": track_name, "{safe_track}": safe,
                        "{index}": str(index), "{port}": str(port)}
        for key, template in self.args.track_env.items():
            value = template
            for marker, replacement in replacements.items():
                value = value.replace(marker, replacement)
            if "{" in value or "}" in value:
                raise RuntimeError(f"track env {key} 含未知 placeholder")
            result[key] = value
        return result

    def validate_tracked_inputs(self):
        for rel in (self.args.goal, self.args.plan_doc):
            if not rel:
                continue
            path = L.repo_relative_path(self.repo, rel)
            if (not path.is_file() or
                    git(self.repo, "ls-files", "--error-unmatch", rel, check=False).returncode):
                raise RuntimeError(f"{rel} 必須存在、是 regular file 且已 tracked")

    def acquire_integration_worktree_lock(self, *, announce=True):
        """Hold the exact lock used by standalone loop before any integration-tree mutation."""
        if self.integration_worktree_lock_acquired:
            return
        git_dir_raw = git(self.repo, "rev-parse", "--git-dir").stdout.strip()
        git_dir = Path(git_dir_raw)
        if not git_dir.is_absolute():
            git_dir = self.repo / git_dir
        git_dir = git_dir.resolve()
        self.integration_worktree_lock_file = L.acquire_run_lock(
            git_dir / "loop-agent-lite.run.lock", f"Git worktree {self.repo}")
        self.integration_worktree_lock_acquired = True
        if announce:
            self.log("已取得 integration worktree 單 writer 鎖")

    def acquire_parent_run_lock(self):
        """Bridge parent root identity to the long-lived fleet writer lock."""
        with L.workspace_operation_lock(L.WORKSPACE_ROOT, self.name, blocking=False):
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            parent_fd = os.open(self.parent, os.O_RDONLY | os.O_DIRECTORY | nofollow)
            try:
                opened = os.fstat(parent_fd)
                L.acquire_run_lock(self.parent / ".fleet.run.lock", f"fleet '{self.name}'")
                current = self.parent.stat(follow_symlinks=False)
                if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                    raise RuntimeError("fleet parent 在取得 writer lock 時已被同名重建")
                expected_run_id = self.args.expected_run_id
                if expected_run_id:
                    observed = None
                    errors = []
                    for filename in ("fleet.json", "fleet.last-good.json"):
                        try:
                            fd = os.open(filename, os.O_RDONLY | nofollow, dir_fd=parent_fd)
                            with os.fdopen(fd, "r", encoding="utf-8", closefd=True) as stream:
                                candidate = json.load(stream)
                            if not isinstance(candidate, dict):
                                raise ValueError("頂層不是 object")
                            observed = candidate.get("run_id")
                            break
                        except (FileNotFoundError, OSError, ValueError,
                                json.JSONDecodeError, UnicodeDecodeError) as error:
                            errors.append(f"{filename}: {error}")
                    if observed != expected_run_id:
                        raise RuntimeError(
                            "fleet parent run-id 在 Dashboard 確認後已更新：" +
                            (str(observed) if observed else "; ".join(errors)))
            finally:
                os.close(parent_fd)

    def integration_fingerprint(self):
        return (git(self.repo, "rev-parse", "HEAD").stdout.strip(),
                git(self.repo, "status", "--porcelain=v1", "-z").stdout)

    def configure_runtime_identity(self):
        """Bind integration validators to this exact parent generation/session."""
        try:
            parent_state, _data, _recovered = L.load_checkpointed_state(
                self.parent / "state.json", repair=False)
            generation = parent_state.get("workspace_generation")
        except (FileNotFoundError, L.StateLoadError) as error:
            raise RuntimeError("fleet validator 缺少可驗證的 parent workspace generation") from error
        L.configure_runtime_identity(
            self.parent, generation, self.state["loop"]["session_id"],
            workspace_name=self.name, repo=self.repo)
        self.clear_pending_runtime_identity()

    def clear_pending_runtime_identity(self):
        L.remove_pending_runtime_identity(self.pending_runtime_marker)
        self.pending_runtime_marker = None

    def validate_integration(self, context: str):
        """Run the frozen validator and prove it did not mutate tracked/untracked Git state."""
        before = self.integration_fingerprint()
        self.log(f"{context}｜開始 validate")
        ok, tail, timed_out = L.run_validate(shlex.split(self.args.validate_cmd), self.repo,
                                             self.args.validate_timeout)
        after = self.integration_fingerprint()
        mutated = after != before
        if mutated:
            note = (f"{context} validator 修改了 Git HEAD/index/worktree；已保留現場並停止。"
                    f"原 HEAD={before[0]}，目前 HEAD={after[0]}")
            tail = (tail + "\n" + note).strip()
            ok = False
        self.log(f"{context}｜validate {'PASS' if ok else 'FAIL'}"
                 + ("（timeout）" if timed_out else "")
                 + ("（validator mutation）" if mutated else ""))
        return ok, tail, timed_out, mutated

    def assert_known_integration_tree(self, *allowed_shas: str):
        """Only reset a crash scene whose index/worktree exactly equals a journaled commit tree."""
        if git(self.repo, "ls-files", "-u").stdout:
            raise RuntimeError("CAS resume 發現未解 index 衝突，拒絕 reset")
        if git(self.repo, "diff", "--quiet", check=False).returncode:
            raise RuntimeError("CAS resume 發現 worktree/index 額外差異，拒絕 reset")
        if git(self.repo, "ls-files", "--others", "--exclude-standard").stdout:
            raise RuntimeError("CAS resume 發現未追蹤檔案，拒絕 reset")
        index_tree = git(self.repo, "write-tree").stdout.strip()
        allowed_trees = {git(self.repo, "rev-parse", f"{sha}^{{tree}}").stdout.strip()
                         for sha in allowed_shas}
        if index_tree not in allowed_trees:
            raise RuntimeError("CAS resume 的 index 不等於 journal expected/candidate，拒絕 reset")

    @staticmethod
    def validate_state(state):
        if state.get("schema_version") != SCHEMA_VERSION or state.get("workspace_kind") != "fleet-parent":
            raise ValueError("fleet schema 不符")
        if not isinstance(state.get("run_id"), str) or len(state["run_id"]) != 32:
            raise ValueError("fleet run_id 不合法")
        if not re.fullmatch(r"[0-9a-f]{32}", state["run_id"]):
            raise ValueError("fleet run_id 必須是小寫 hex")
        if not isinstance(state.get("tracks"), list) or not isinstance(state.get("merge_queue"), list):
            raise ValueError("fleet tracks/merge_queue 型別不合法")
        if state.get("phase") not in FLEET_PHASES:
            raise ValueError("fleet phase 不合法")
        resume_phase = state.get("resume_phase")
        if resume_phase is not None and resume_phase not in RESUMABLE_PHASES:
            raise ValueError("fleet resume_phase 不合法")
        if state.get("phase") == "failed" and resume_phase is None:
            raise ValueError("failed fleet 缺少 resume_phase")
        sessions = state.get("supervisor_session_history")
        current_session = ((state.get("loop") or {}).get("session_id")
                           if isinstance(state.get("loop"), dict) else None)
        if (not isinstance(sessions, list) or not sessions or len(sessions) > 100 or
                len(sessions) != len(set(sessions)) or current_session not in sessions or
                any(re.fullmatch(r"[0-9a-f]{32}", str(item)) is None for item in sessions)):
            raise ValueError("fleet supervisor_session_history 不合法")
        if "last_error" in state:
            last_error = state["last_error"]
            if (not isinstance(last_error, dict) or
                    last_error.get("phase") not in FLEET_PHASES or
                    not isinstance(last_error.get("message"), str) or
                    not isinstance(last_error.get("at"), str)):
                raise ValueError("fleet last_error 不合法")
        for key in ("initial_integration_sha", "expected_integration_sha"):
            if not OID_RE.fullmatch(str(state.get(key, ""))):
                raise ValueError(f"fleet {key} 不合法")
        if (not isinstance(state.get("integration_ref"), str) or
                not state["integration_ref"].startswith("refs/heads/") or
                not isinstance(state.get("integration_worktree"), str) or
                not state["integration_worktree"]):
            raise ValueError("fleet integration identity 不合法")
        if (not isinstance(state.get("plan_generation"), int) or
                isinstance(state.get("plan_generation"), bool) or state["plan_generation"] < 0 or
                (state.get("plan_sha256") is not None and
                 re.fullmatch(r"[0-9a-f]{64}", str(state.get("plan_sha256"))) is None)):
            raise ValueError("fleet plan generation/hash 不合法")
        dashboard_revision = state.get("dashboard_revision", 0)
        if (not isinstance(dashboard_revision, int) or isinstance(dashboard_revision, bool) or
                dashboard_revision < 0):
            raise ValueError("fleet dashboard_revision 不合法")
        config = state.get("config")
        if not isinstance(config, dict):
            raise ValueError("fleet config 缺失或型別不合法")
        for key in ("repo", "agent_cmd", "validate_cmd", "goal", "plan_doc", "notify_cmd"):
            if not isinstance(config.get(key), str) or (key in {"repo", "agent_cmd", "validate_cmd", "goal"} and not config[key]):
                raise ValueError(f"fleet config.{key} 不合法")
        numeric = {
            "max_parallel": (1, 8, True), "merge_threshold": (1, None, True),
            "done_threshold": (1, None, True), "flag_threshold": (1, None, True),
            "red_limit": (1, None, True), "stall_limit": (1, None, True),
            "round_timeout": (0, None, False), "validate_timeout": (0, None, False),
            "agent_backoff_max": (0, None, False), "max_child_restarts": (0, None, True),
            "track_port_base": (0, 65527, True),
        }
        for key, (low, high, integer) in numeric.items():
            value = config.get(key)
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or
                    value < low or (high is not None and value > high) or (integer and int(value) != value) or
                    (key == "validate_timeout" and value <= 0)):
                raise ValueError(f"fleet config.{key} 不合法")
        if not isinstance(config.get("pause_after_plan"), bool):
            raise ValueError("fleet config.pause_after_plan 不合法")
        try:
            if validate_track_env(config.get("track_env")) != config.get("track_env"):
                raise ValueError("fleet config.track_env 未正規化")
        except ValueError as error:
            raise ValueError(f"fleet config.track_env 不合法:{error}") from error
        input_identity = state.get("input_identity")
        if not isinstance(input_identity, dict) or config["goal"] not in input_identity:
            raise ValueError("fleet input_identity 缺少 goal")
        expected_inputs = {config["goal"]} | ({config["plan_doc"]} if config["plan_doc"] else set())
        if set(input_identity) != expected_inputs:
            raise ValueError("fleet input_identity 與 config 不符")
        for relative, identity in input_identity.items():
            if (not isinstance(relative, str) or not isinstance(identity, dict) or
                    not re.fullmatch(r"[0-9a-f]{64}", str(identity.get("sha256", ""))) or
                    not OID_RE.fullmatch(str(identity.get("blob", "")))):
                raise ValueError("fleet input_identity hash/blob 不合法")
        names = []
        safe_names = []
        for track in state["tracks"]:
            if not isinstance(track, dict) or not isinstance(track.get("name"), str):
                raise ValueError("track 結構不合法")
            if track.get("status") not in TRACK_STATUSES:
                raise ValueError("track status 不合法")
            expected_safe = L.fleet_track_safe_name(track["name"])
            if (track.get("safe_name") != expected_safe or
                    not isinstance(track.get("branch_ref"), str) or
                    not isinstance(track.get("worktree"), str) or
                    not isinstance(track.get("child_workspace"), str) or
                    not isinstance(track.get("plan_path"), str) or
                    not isinstance(track.get("env"), dict)):
                raise ValueError("track persisted identity 型別不合法")
            for key in ("restart_count", "integration_validate_failures", "control_generation"):
                value = track.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise ValueError(f"track {key} 不合法")
            if (not isinstance(track.get("index"), int) or isinstance(track.get("index"), bool) or
                    track["index"] < 1 or not isinstance(track.get("port"), int) or
                    isinstance(track.get("port"), bool) or not 1 <= track["port"] <= 65535):
                raise ValueError("track index/port 不合法")
            cleanup_stage = track.get("cleanup_stage")
            if cleanup_stage not in {None, "evidence-captured", "worktree-removed",
                                     "child-removing", "child-removed", "complete"}:
                raise ValueError("track cleanup_stage 不合法")
            if ((track.get("status") == "cleaned") != (cleanup_stage == "complete") or
                    (cleanup_stage not in {None, "complete"} and track.get("status") != "merged")):
                raise ValueError("track cleanup_stage/status 組合不合法")
            if "event_history" in track and (not isinstance(track["event_history"], list) or
                                               len(track["event_history"]) > 500 or
                                               any(not isinstance(item, dict)
                                                   for item in track["event_history"])):
                raise ValueError("track event_history 不合法")
            adopted_sessions = track.get("adopted_child_sessions", [])
            if (not isinstance(adopted_sessions, list) or len(adopted_sessions) > 100 or
                    len(adopted_sessions) != len(set(adopted_sessions)) or
                    any(re.fullmatch(r"[0-9a-f]{32}", str(item)) is None
                        for item in adopted_sessions)):
                raise ValueError("track adopted_child_sessions 不合法")
            for cursor_key in ("imported_child_phase_events", "imported_child_phase_event_seq"):
                if cursor_key in track and (
                        not isinstance(track[cursor_key], int) or isinstance(track[cursor_key], bool) or
                        track[cursor_key] < 0):
                    raise ValueError(f"track {cursor_key} 不合法")
            if cleanup_stage is not None and (
                    not isinstance(track.get("evidence_path"), str) or
                    re.fullmatch(r"[0-9a-f]{64}", str(track.get("evidence_sha256", ""))) is None):
                raise ValueError("track cleanup evidence identity 不合法")
            cleanup_identity = (
                track.get("cleanup_child_dev"), track.get("cleanup_child_ino"),
                track.get("cleanup_child_generation"))
            if any(value is not None for value in cleanup_identity):
                if (not isinstance(cleanup_identity[0], int) or cleanup_identity[0] < 0 or
                        not isinstance(cleanup_identity[1], int) or cleanup_identity[1] < 1 or
                        re.fullmatch(r"[0-9a-f]{32}",
                                     str(cleanup_identity[2] or "")) is None):
                    raise ValueError("track cleanup child inode/generation 不合法")
            if cleanup_stage in {"child-removing", "child-removed", "complete"} and not all(
                    value is not None for value in cleanup_identity):
                raise ValueError("track cleanup child journal identity 缺失")
            names.append(track["name"])
            safe_names.append(expected_safe)
        if len(names) != len(set(names)):
            raise ValueError("track 名稱重複")
        if len(safe_names) != len(set(safe_names)):
            raise ValueError("track safe name 衝突")
        if any(name not in names for name in state["merge_queue"]):
            raise ValueError("merge_queue 指向不存在的 track")
        tx = state.get("merge_tx")
        if tx is not None:
            if (not isinstance(tx, dict) or tx.get("track") not in names or
                    tx.get("stage") not in MERGE_STAGES or
                    not OID_RE.fullmatch(str(tx.get("expected_sha", ""))) or
                    not OID_RE.fullmatch(str(tx.get("candidate_sha", "")))):
                raise ValueError("merge_tx 結構或身分不合法")

    def validate_loaded_identity(self):
        """Reject altered persisted identities before resume touches Git or starts a child."""
        state = self.state
        self.validate_loaded_base_identity()
        expected = str(state["expected_integration_sha"])
        plan = state.get("plan") or []
        expected_names = {task["track"] for task in plan}
        for directory, label in ((self.parent / ".plans", "fleet plans 目錄"),
                                 (self.parent / "worktrees", "fleet worktrees 目錄"),
                                 (self.parent / "runtime", "fleet runtime 目錄")):
            L.workspace_directory(directory, label)
        seen_paths = set()
        for track in state["tracks"]:
            name = track["name"]
            safe = L.fleet_track_safe_name(name)
            expected_branch = f"refs/heads/loop/{state['run_id']}/{safe}"
            expected_worktree = (self.parent / "worktrees" / safe).resolve()
            expected_child = f"{self.name}--{safe}"
            expected_plan_path = (self.parent / ".plans" / f"{safe}.json").resolve()
            index, port = track.get("index"), track.get("port")
            if (not isinstance(index, int) or isinstance(index, bool) or index < 1 or
                    not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535):
                raise RuntimeError(f"track {name!r} index/port 身分不符")
            expected_env = self.render_track_env(name, safe, index, port)
            actual_value = str(track.get("worktree", ""))
            actual_path = Path(actual_value).resolve()
            if (name not in expected_names or track.get("safe_name") != safe or
                    track.get("branch_ref") != expected_branch or
                    actual_value != str(expected_worktree) or actual_path != expected_worktree or
                    track.get("child_workspace") != expected_child or
                    track.get("plan_path") != str(expected_plan_path) or
                    track.get("env") != expected_env):
                raise RuntimeError(f"track {name!r} persisted 身分不符")
            if actual_path in seen_paths:
                raise RuntimeError("不同 track 共用同一 worktree，拒絕 resume")
            seen_paths.add(actual_path)
            if track.get("cleanup_stage") is not None:
                expected_evidence = (self.parent / "evidence" / "tracks" / safe /
                                     "evidence.json").resolve()
                if track.get("evidence_path") != str(expected_evidence):
                    raise RuntimeError(f"track {name!r} evidence path 身分不符")
                self.verify_track_evidence(track, expected_evidence)
            if "cleanup_child_tombstone" in track:
                expected_tombstone = L.workspace_path(
                    L.WORKSPACE_ROOT, f"delete-{state['run_id']}-{safe}")
                if track.get("cleanup_child_tombstone") != str(expected_tombstone):
                    raise RuntimeError(f"track {name!r} cleanup tombstone 身分不符")
            registered = f"worktree {expected_worktree}\n" in git(
                self.repo, "worktree", "list", "--porcelain").stdout
            worktree_directory = L.workspace_directory(expected_worktree, f"track {name} worktree")
            if track.get("status") == "cleaned":
                if registered or worktree_directory is not None:
                    raise RuntimeError(f"track {name!r} 已標 cleaned 但 worktree 仍存在")
                child_path = L.workspace_path(L.WORKSPACE_ROOT, expected_child)
                if L.workspace_directory(child_path, f"track {name} child workspace") is not None:
                    raise RuntimeError(f"track {name!r} 已標 cleaned 但 child workspace 仍存在")
                runtime_path = self.parent / "runtime" / safe
                if L.workspace_directory(runtime_path, f"track {name} runtime") is not None:
                    raise RuntimeError(f"track {name!r} 已標 cleaned 但 runtime 仍存在")
                continue
            if worktree_directory is None:
                if track.get("status") != "merged":
                    raise RuntimeError(f"track {name!r} worktree 遺失")
                continue
            if not registered:
                raise RuntimeError(f"track {name!r} worktree 未註冊")
            actual_branch = git(expected_worktree, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
            branch_tip = git(expected_worktree, "rev-parse", "HEAD").stdout.strip()
            track_common = Path(git(expected_worktree, "rev-parse", "--git-common-dir").stdout.strip())
            if not track_common.is_absolute():
                track_common = expected_worktree / track_common
            if (actual_branch != expected_branch or track_common.resolve() != self.common_git_dir or
                    git(self.repo, "rev-parse", expected_branch).stdout.strip() != branch_tip):
                raise RuntimeError(f"track {name!r} Git 身分不符")
            if git(self.repo, "merge-base", "--is-ancestor",
                   state["initial_integration_sha"], branch_tip, check=False).returncode:
                raise RuntimeError(f"track {name!r} branch 改寫或遺失初始祖先")
            if not track.get("started") and branch_tip != state["expected_integration_sha"]:
                raise RuntimeError(f"track {name!r} 未啟動卻已有未知 commit")
        tx = state.get("merge_tx")
        if tx and tx["expected_sha"] != expected:
            raise RuntimeError("merge_tx expected SHA 與 fleet checkpoint 不符")
        actual = git(self.repo, "rev-parse", self.integration_ref).stdout.strip()
        allowed_refs = {expected}
        if tx:
            allowed_refs.add(str(tx.get("candidate_sha")))
        if actual not in allowed_refs:
            raise RuntimeError(f"integration ref 已移到未知第三個 SHA：expected={expected} actual={actual}")
        self.assert_frozen_inputs(check_ref=False)

    def verify_track_evidence(self, track, evidence_path: Path | None = None):
        """Verify the manifest and every copied prompt before cleanup resume trusts it."""
        path = evidence_path or Path(str(track.get("evidence_path") or ""))
        data = L.read_regular_bytes(path, f"track {track['name']} evidence")
        if len(data) > 2_000_000 or L.sha256_bytes(data) != track.get("evidence_sha256"):
            raise RuntimeError(f"track {track['name']!r} evidence hash/size 不符")
        try:
            evidence = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"track {track['name']!r} evidence JSON 不合法") from error
        if (not isinstance(evidence, dict) or evidence.get("schema_version") != 1 or
                evidence.get("track") != track["name"] or
                evidence.get("child_workspace") != track["child_workspace"]):
            raise RuntimeError(f"track {track['name']!r} evidence 身分不符")
        expected_agent_hash = hashlib.sha256(self.args.agent_cmd.encode()).hexdigest()
        expected_validate_hash = hashlib.sha256(self.args.validate_cmd.encode()).hexdigest()
        if (evidence.get("agent_command_sha256") != expected_agent_hash or
                evidence.get("validate_command_sha256") != expected_validate_hash):
            raise RuntimeError(f"track {track['name']!r} evidence command hash 與 frozen config 不符")
        artifacts = evidence.get("prompt_artifacts")
        if not isinstance(artifacts, list) or len(artifacts) > 500:
            raise RuntimeError(f"track {track['name']!r} prompt evidence manifest 不合法")
        prompt_dir = path.parent / "prompts"
        if L.workspace_directory(prompt_dir, f"track {track['name']} prompt evidence") is None:
            raise RuntimeError(f"track {track['name']!r} prompt evidence 目錄遺失")
        names = []
        total = 0
        for artifact in artifacts:
            if (not isinstance(artifact, dict) or
                    not isinstance(artifact.get("name"), str) or
                    Path(artifact["name"]).name != artifact["name"] or
                    re.fullmatch(r"round-[^/]+\.md", artifact["name"]) is None or
                    re.fullmatch(r"[0-9a-f]{64}", str(artifact.get("sha256", ""))) is None or
                    not isinstance(artifact.get("size"), int) or
                    isinstance(artifact.get("size"), bool) or artifact["size"] < 0):
                raise RuntimeError(f"track {track['name']!r} prompt evidence entry 不合法")
            names.append(artifact["name"])
            prompt_data = L.read_regular_bytes(
                prompt_dir / artifact["name"], f"track {track['name']} prompt evidence")
            total += len(prompt_data)
            if (len(prompt_data) != artifact["size"] or
                    L.sha256_bytes(prompt_data) != artifact["sha256"]):
                raise RuntimeError(f"track {track['name']!r} prompt evidence hash/size 不符")
        if len(names) != len(set(names)) or total > 8_000_000:
            raise RuntimeError(f"track {track['name']!r} prompt evidence manifest 重複或過大")
        actual_names = sorted(item.name for item in prompt_dir.glob("round-*.md"))
        if sorted(names) != actual_names:
            raise RuntimeError(f"track {track['name']!r} prompt evidence manifest 與目錄不符")

    def preflight(self):
        if not self.repo.is_dir():
            raise RuntimeError("repo 不存在")
        top = Path(git(self.repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
        if top != self.repo:
            raise RuntimeError(f"--repo 必須是 integration worktree 根目錄：{top}")
        if (self.repo / ".gitmodules").exists():
            raise RuntimeError("第一版不支援 submodule repo")
        current_ref = git(self.repo, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
        if not current_ref:
            raise RuntimeError("detached HEAD 不支援")
        requested = self.args.integration_branch
        if requested:
            requested = requested if requested.startswith("refs/heads/") else f"refs/heads/{requested}"
            if requested != current_ref:
                raise RuntimeError(f"integration branch {requested} 必須由 --repo checkout（目前 {current_ref}）")
        self.integration_ref = requested or current_ref
        common_raw = git(self.repo, "rev-parse", "--git-common-dir").stdout.strip()
        common_dir = Path(common_raw)
        if not common_dir.is_absolute():
            common_dir = self.repo / common_dir
        common_dir = common_dir.resolve()
        self.common_git_dir = common_dir
        lock_key = hashlib.sha256(self.integration_ref.encode()).hexdigest()[:16]
        self.integration_lock_path = common_dir / f"loop-fleet-{lock_key}.lock"
        L.acquire_run_lock(self.integration_lock_path,
                           f"integration ref {self.integration_ref} writer")

    def capture_frozen_inputs(self):
        """保存 tracked input 的內容 hash 與 Git blob；child 不得自行猜測需求變更。"""
        identity = {}
        for relative in (self.args.goal, self.args.plan_doc):
            if not relative:
                continue
            path = L.repo_relative_path(self.repo, relative)
            blob = git(self.repo, "rev-parse", f"{self.integration_ref}:{relative}").stdout.strip()
            identity[relative] = {"sha256": L.sha256_bytes(L.read_regular_bytes(path, relative)),
                                  "blob": blob}
        return identity

    def assert_frozen_inputs(self, *, check_ref=True):
        if check_ref and self.state.get("merge_tx") is None:
            actual = git(self.repo, "rev-parse", self.integration_ref).stdout.strip()
            if actual != self.state.get("expected_integration_sha"):
                raise RuntimeError(f"integration ref 已由未知外力變更：{actual}")
        expected = self.state.get("input_identity")
        if not isinstance(expected, dict) or not expected:
            raise RuntimeError("fleet input identity 缺失")
        for relative, frozen in expected.items():
            path = L.repo_relative_path(self.repo, relative)
            current_hash = L.sha256_bytes(L.read_regular_bytes(path, relative))
            current_blob = git(self.repo, "rev-parse", f"{self.integration_ref}:{relative}", check=False).stdout.strip()
            if current_hash != frozen.get("sha256") or current_blob != frozen.get("blob"):
                raise RuntimeError(f"tracked input {relative} 已變更；保留 branches 並停止，請刪除後重新規劃")

    def planning_command(self, run_id: str) -> list[str]:
        try:
            parent_state, _data, _recovered = L.load_checkpointed_state(
                self.parent / "state.json", repair=False)
            expected_generation = parent_state.get("workspace_generation")
        except FileNotFoundError:
            expected_generation = "new"
        command = [sys.executable, "-m", "engine.loop", "--repo", str(self.repo), "--name", self.name,
                   "--goal", self.args.goal, "--agent-cmd", self.args.agent_cmd,
                   "--validate-cmd", self.args.validate_cmd,
                   "--flag-threshold", str(self.args.flag_threshold),
                   "--done-threshold", str(self.args.done_threshold),
                   "--red-limit", str(self.args.red_limit), "--stall-limit", str(self.args.stall_limit),
                   "--round-timeout", str(self.args.round_timeout),
                   "--validate-timeout", str(self.args.validate_timeout),
                   "--agent-backoff-max", str(self.args.agent_backoff_max),
                   "--workspace-kind", "fleet-parent", "--fleet-run-id", run_id,
                   "--expected-workspace-generation", str(expected_generation),
                   "--handoff-after-plan", "--inherited-worktree-lock-fd",
                   str(self.integration_worktree_lock_file.fileno())]
        if self.args.plan_doc:
            command += ["--plan-doc", self.args.plan_doc]
        return command

    def initial_state(self, run_id: str) -> dict:
        expected = git(self.repo, "rev-parse", self.integration_ref).stdout.strip()
        session_id = uuid.uuid4().hex
        return {
            "schema_version": SCHEMA_VERSION, "run_id": run_id,
            "workspace_kind": "fleet-parent", "phase": "planning", "resume_phase": None,
            "integration_ref": self.integration_ref, "integration_worktree": str(self.repo),
            "initial_integration_sha": expected, "expected_integration_sha": expected,
            "input_identity": self.capture_frozen_inputs(),
            "plan_sha256": None, "plan_generation": 0, "plan": [], "order_map": {}, "tracks": [],
            "dashboard_revision": 0,
            "merge_queue": [], "merge_tx": None, "merge_history": [],
            "supervisor_session_history": [session_id],
            "config": {"repo": str(self.repo), "agent_cmd": self.args.agent_cmd,
                       "validate_cmd": self.args.validate_cmd, "goal": self.args.goal,
                       "plan_doc": self.args.plan_doc, "max_parallel": self.args.max_parallel,
                       "merge_threshold": self.args.merge_threshold,
                       "done_threshold": self.args.done_threshold,
                       "flag_threshold": self.args.flag_threshold,
                       "red_limit": self.args.red_limit,
                       "stall_limit": self.args.stall_limit,
                       "round_timeout": self.args.round_timeout,
                       "validate_timeout": self.args.validate_timeout,
                       "agent_backoff_max": self.args.agent_backoff_max,
                       "max_child_restarts": self.args.max_child_restarts,
                       "track_env": self.args.track_env,
                       "track_port_base": self.args.track_port_base,
                       "notify_cmd": self.args.notify_cmd,
                       "pause_after_plan": self.args.pause_after_plan},
            "loop": {"pid": os.getpid(), "session_id": session_id,
                     "started_at": L.datetime.now().isoformat(timespec="seconds")},
        }

    def run_planning_handoff(self):
        run_id = self.state["run_id"]
        self.state.update(phase="planning", resume_phase=None)
        self.save()
        self.log(f"啟動 planning handoff｜run={run_id}")
        planning_env = L.expose_checkout_package(
            {**os.environ, "LOOP_FLEET_RUN_ID": run_id})
        process = subprocess.Popen(self.planning_command(run_id), cwd=self.repo,
                                   env=planning_env,
                                   pass_fds=(self.integration_worktree_lock_file.fileno(),),
                                   start_new_session=True)
        self.planning_process = process
        stop_sent = False
        try:
            while True:
                try:
                    returncode = process.wait(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    stop_needed = self.stop_requested()
                    if not stop_sent:
                        try:
                            self.assert_frozen_inputs()
                        except (OSError, ValueError, RuntimeError) as error:
                            self.state["stop_reason"] = str(error)
                            self.save()
                            self.log(str(error))
                            stop_needed = True
                    if stop_needed:
                        try:
                            parent_state = json.loads(L.read_regular_text(
                                self.parent / "state.json", "fleet parent state"))
                        except (OSError, ValueError, json.JSONDecodeError):
                            continue
                        stop_sent = self.request_round_stop(self.parent, parent_state) or stop_sent
        except KeyboardInterrupt:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                process.wait()
            raise
        finally:
            self.planning_process = None
        if stop_sent or self.stop_requested():
            self.clear_parent_loop_marker()
            self.mark_stopped("planning")
            return False
        if returncode:
            raise RuntimeError(f"planning loop 失敗 rc={returncode}")
        # Idempotently assert that this Fleet still owns the exact inherited worktree
        # lock before reading the handoff or allowing split/CAS work.
        self.acquire_integration_worktree_lock()
        self.assert_frozen_inputs()
        self.assert_known_integration_tree(self.state["expected_integration_sha"])
        parent_state = json.loads(L.read_regular_text(
            self.parent / "state.json", "fleet parent state"))
        plan, errors = validate_plan(parent_state.get("plan"))
        if errors or parent_state.get("phase") != "exec":
            raise RuntimeError("planning handoff 沒有產生已收斂 plan v2：" + "; ".join(errors))
        raw = json.dumps(plan, ensure_ascii=False, separators=(",", ":")).encode()
        self.state.update(phase="splitting", resume_phase=None,
                          plan_sha256=hashlib.sha256(raw).hexdigest(),
                          plan_generation=parent_state.get("plan_version", 1), plan=plan)
        self.save()
        if self.args.pause_after_plan:
            self.state.update(phase="awaiting-approval", resume_phase="splitting")
            self.state["loop"]["pid"] = None
            self.save()
            self.log("plan 已收斂｜依選配 pause-after-plan 停止，resume 即核准同一 plan hash")
            return False
        self.split_tracks(include_final=False)
        return True

    def create_new(self):
        if L.workspace_directory(self.parent, "fleet workspace") is None:
            raise RuntimeError("fleet parent 尚未由 execute 安全建立")
        if not self.state:
            self.state = self.initial_state(uuid.uuid4().hex)
            self.save()
        if self.args.import_plan:
            if not self.state.get("plan"):
                self.freeze_import_plan()
            if self.args.consume_import_plan:
                Path(self.args.import_plan).unlink(missing_ok=True)
            self.split_tracks(include_final=False)
            return True
        return self.run_planning_handoff()

    def freeze_import_plan(self):
        """Persist normalized imported-plan truth before any fallible baseline validation."""
        try:
            raw_plan = json.loads(L.read_regular_text(
                Path(self.args.import_plan), "fleet import plan"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"import plan 無法讀取:{error}") from error
        plan, errors = validate_plan(raw_plan)
        if errors:
            raise RuntimeError("import plan v2 不合法：" + "; ".join(errors))
        if len({task["track"] for task in plan if task["track"] != "@final"}) < 2:
            raise RuntimeError("parallel import plan 至少需要兩個一般 track")
        raw = json.dumps(plan, ensure_ascii=False, separators=(",", ":")).encode()
        self.state.update(phase="splitting", plan=plan,
                          plan_sha256=hashlib.sha256(raw).hexdigest(), plan_generation=1)
        parent_workspace = L.Workspace(self.name)
        parent_state = parent_workspace.fresh_state("fleet-parent", self.state["run_id"])
        parent_state.update(phase="exec", plan=plan, plan_version=1,
                            current_order=plan[0]["order"] if plan else 0,
                            config=self.state["config"])
        parent_workspace.save_state(parent_state)
        self.save()

    def stop_processes(self):
        processes = list(self.children.values())
        if self.planning_process is not None:
            processes.append(self.planning_process)
        for process in processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass

    def clear_parent_loop_marker(self):
        state_path = self.parent / "state.json"
        try:
            state = json.loads(L.read_regular_text(state_path, "fleet parent state"))
        except (OSError, ValueError, json.JSONDecodeError):
            return
        loop_state = state.get("loop") if isinstance(state.get("loop"), dict) else {}
        loop_state["pid"] = None
        state["loop"] = loop_state
        if state.get("round_started_at") and not state.get("round_interrupted_at"):
            state["round_interrupted_at"] = L.datetime.now().astimezone().isoformat(timespec="seconds")
        L.write_checkpointed_state(state_path, json.dumps(state, ensure_ascii=False, indent=2).encode())

    def split_tracks(self, *, include_final: bool):
        plan = self.state["plan"]
        names = []
        for task in plan:
            track = task["track"]
            if (track == "@final") != include_final or track in names:
                continue
            names.append(track)
        existing = {item["name"] for item in self.state["tracks"]}
        pending = [name for name in names if name not in existing]
        # Prove every deterministic child namespace before the first branch/worktree
        # mutation.  Holding all operation locks makes the check atomic with this split;
        # a conflicting standalone workspace is preserved and no partial track is made.
        with ExitStack() as namespace_locks:
            child_names = sorted(f"{self.name}--{L.fleet_track_safe_name(name)}"
                                 for name in pending)
            for child_name in child_names:
                L.require_workspace_name(child_name)
                namespace_locks.enter_context(
                    L.workspace_operation_lock(L.WORKSPACE_ROOT, child_name, blocking=False))
            conflicts = [child_name for child_name in child_names
                         if L.workspace_directory(
                             L.workspace_path(L.WORKSPACE_ROOT, child_name),
                             f"fleet child namespace {child_name}") is not None]
            if conflicts:
                raise RuntimeError(
                    "fleet child workspace 名稱已被占用，未建立任何 track：" +
                    ", ".join(conflicts))
            for name in pending:
                self.create_track(name)
        self.state["phase"] = "final" if include_final else "exec"
        self.save()

    def create_track(self, name: str):
        safe = L.fleet_track_safe_name(name)
        child_name = f"{self.name}--{safe}"
        L.require_workspace_name(child_name)
        plans = L.ensure_real_directory(self.parent / ".plans", "fleet plans 目錄")
        worktrees = L.ensure_real_directory(self.parent / "worktrees", "fleet worktrees 目錄")
        L.ensure_real_directory(self.parent / "runtime", "fleet runtime 目錄")
        worktree = worktrees / safe
        branch = f"refs/heads/loop/{self.state['run_id']}/{safe}"
        if git(self.repo, "check-ref-format", branch, check=False).returncode:
            raise RuntimeError(f"branch ref 不合法：{branch}")
        tasks = [task for task in self.state["plan"] if task["track"] == name]
        local = []
        mapping = {}
        for order, task in enumerate(tasks, 1):
            item = {key: value for key, value in task.items() if key != "order"}
            item["order"] = order
            local.append(item)
            mapping[str(order)] = task["order"]
        plan_path = plans / f"{safe}.json"
        L.atomic_write_bytes(plan_path, json.dumps(local, ensure_ascii=False, indent=2).encode())
        branch_name = branch.removeprefix("refs/heads/")
        branch_exists = git(self.repo, "show-ref", "--verify", "--quiet", branch, check=False).returncode == 0
        registrations = git(self.repo, "worktree", "list", "--porcelain").stdout
        registered = f"worktree {worktree.resolve()}\n" in registrations
        worktree_directory = L.workspace_directory(worktree, f"track {name} worktree")
        if worktree_directory is not None:
            if not registered:
                raise RuntimeError(f"track {name} 路徑存在但不是 fleet 記錄的 Git worktree")
            actual_branch = git(worktree, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
            if actual_branch != branch:
                raise RuntimeError(f"track {name} worktree branch 身分不符：{actual_branch}")
            actual_tip = git(worktree, "rev-parse", "HEAD").stdout.strip()
            if actual_tip != self.state["expected_integration_sha"]:
                raise RuntimeError(f"track {name} crash 殘留 worktree 已有未知 commit，拒絕接管")
            if git(worktree, "status", "--porcelain").stdout.strip():
                raise RuntimeError(f"track {name} crash 殘留 worktree 不乾淨，拒絕接管")
            track_common = Path(git(worktree, "rev-parse", "--git-common-dir").stdout.strip())
            if not track_common.is_absolute():
                track_common = worktree / track_common
            if track_common.resolve() != self.common_git_dir:
                raise RuntimeError(f"track {name} worktree common-dir 身分不符")
        elif branch_exists:
            branch_tip = git(self.repo, "rev-parse", branch).stdout.strip()
            if branch_tip != self.state["expected_integration_sha"]:
                raise RuntimeError(f"track {name} 殘留 branch 已有未知變更，拒絕猜測接管")
            git(self.repo, "worktree", "add", str(worktree), branch_name)
        else:
            git(self.repo, "worktree", "add", "-b", branch_name,
                str(worktree), self.state["expected_integration_sha"])
        actual_branch = git(worktree, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
        actual_tip = git(worktree, "rev-parse", "HEAD").stdout.strip()
        if (actual_branch != branch or actual_tip != self.state["expected_integration_sha"] or
                git(worktree, "status", "--porcelain").stdout.strip()):
            raise RuntimeError(f"track {name} worktree 建立後身分不符")
        self.crash_point("track-worktree-created")
        index = len(self.state["tracks"]) + 1
        used_ports = {item.get("port") for item in self.state["tracks"]}
        if self.args.track_port_base:
            port = self.args.track_port_base + index - 1
        else:
            port = self.allocate_dynamic_port()
            while port in used_ports:
                port = self.allocate_dynamic_port()
        if not 1 <= port <= 65535:
            raise RuntimeError(f"track {name} port 超出 1..65535")
        track_env = self.render_track_env(name, safe, index, port)
        track = {"name": name, "safe_name": safe, "index": index, "port": port,
                 "branch_ref": branch,
                 "worktree": str(worktree.resolve()), "child_workspace": child_name,
                 "plan_path": str(plan_path), "status": "pending", "tip": None,
                 "started": False,
                 "restart_count": 0, "integration_validate_failures": 0,
                 "control_generation": 0,
                 "env": track_env}
        self.state["order_map"][name] = mapping
        self.state["tracks"].append(track)
        self.save()

    def child_command(self, track: dict, initial: bool, expected_generation: str) -> list[str]:
        command = [sys.executable, "-m", "engine.loop", "--repo", track["worktree"],
                   "--name", track["child_workspace"], "--goal", self.args.goal,
                   "--agent-cmd", self.args.agent_cmd, "--validate-cmd", self.args.validate_cmd,
                   "--done-threshold", str(self.args.done_threshold),
                   "--red-limit", str(self.args.red_limit), "--stall-limit", str(self.args.stall_limit),
                   "--round-timeout", str(self.args.round_timeout),
                   "--validate-timeout", str(self.args.validate_timeout),
                   "--agent-backoff-max", str(self.args.agent_backoff_max),
                   "--workspace-kind", "fleet-child", "--fleet-run-id", self.state["run_id"],
                   "--expected-workspace-generation", expected_generation,
                   "--fleet-parent", self.name,
                   "--fleet-parent-session-id", self.state["loop"]["session_id"],
                   "--track", track["name"],
                   "--merge-target-ref", self.integration_ref,
                   "--merge-threshold", str(self.args.merge_threshold)]
        if self.args.plan_doc:
            command += ["--plan-doc", self.args.plan_doc]
        if initial:
            command += ["--import-plan", track["plan_path"], "--start-phase", "exec"]
        return command

    def start_track(self, track: dict, *, initial=False):
        existing_child = self.child_state(track)
        expected_generation = (str(existing_child.get("workspace_generation"))
                               if existing_child else "new")
        command = self.child_command(track, initial, expected_generation)
        env = L.expose_checkout_package(dict(os.environ))
        runtime_root = L.ensure_real_directory(self.parent / "runtime", "fleet runtime 目錄")
        track_runtime = L.ensure_real_directory(runtime_root / track["safe_name"],
                                                f"track {track['name']} runtime")
        tmp_dir = L.ensure_real_directory(track_runtime / "tmp", f"track {track['name']} tmp")
        cache_dir = L.ensure_real_directory(track_runtime / "cache", f"track {track['name']} cache")
        npm_cache = L.ensure_real_directory(cache_dir / "npm", f"track {track['name']} npm cache")
        expected_env = self.render_track_env(track["name"], track["safe_name"],
                                             track["index"], track["port"])
        expected_env.update({"TMPDIR": str(tmp_dir), "XDG_CACHE_HOME": str(cache_dir),
                             "npm_config_cache": str(npm_cache)})
        track_env = track.get("env") or expected_env
        if track_env != expected_env:
            raise RuntimeError(f"track {track['name']} persisted runtime env 身分不符")
        track["env"] = track_env
        env.update({"LOOP_FLEET_RUN_ID": self.state["run_id"],
                    "LOOP_FLEET_TRACK": track["name"], **track_env})
        process = subprocess.Popen(command, cwd=track["worktree"], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.STDOUT, start_new_session=True,
                                   env=env)
        self.children[track["name"]] = process
        self.commands[track["name"]] = command
        track["status"] = "running"
        track["started"] = True
        track.setdefault("started_at", L.datetime.now().astimezone().isoformat(timespec="milliseconds"))
        track["pid"] = process.pid
        self.log(f"啟動 track {track['name']}｜pid={process.pid}")
        self.save()

    def child_state_at(self, track, child_dir: Path):
        if L.workspace_directory(child_dir, f"track {track['name']} child workspace") is None:
            return None
        path = child_dir / "state.json"
        try:
            state, _data, _recovered = L.load_checkpointed_state(path, repair=False)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, L.StateLoadError) as error:
            raise RuntimeError(f"track {track['name']} child state 無法安全讀取:{error}") from error
        return self.validate_child_state(track, state)

    def validate_child_state(self, track, state):
        config = state.get("config") if isinstance(state.get("config"), dict) else {}
        expected_repo = str(Path(track["worktree"]).resolve())
        expected_tasks = []
        for order, task in enumerate((item for item in self.state["plan"]
                                      if item["track"] == track["name"]), 1):
            local = {key: value for key, value in task.items() if key != "order"}
            local["order"] = order
            expected_tasks.append(local)
        try:
            same_agent = shlex.split(config.get("agent_cmd", "")) == shlex.split(self.args.agent_cmd)
            same_validate = shlex.split(config.get("validate_cmd", "")) == shlex.split(self.args.validate_cmd)
        except ValueError as error:
            raise RuntimeError(f"track {track['name']} child command 無法解析") from error
        if (state.get("workspace_kind") != "fleet-child" or
                state.get("fleet_run_id") != self.state["run_id"] or
                state.get("fleet_parent") != self.name or state.get("track") != track["name"] or
                state.get("merge_target_ref") != self.integration_ref or
                state.get("fleet_parent_session_id") not in
                self.state.get("supervisor_session_history", []) or
                Path(str(config.get("repo", ""))).resolve() != Path(expected_repo) or
                state.get("plan") != expected_tasks or not same_agent or not same_validate):
            raise RuntimeError(f"track {track['name']} child state 身分不符")
        return state

    def child_state_from_fd(self, track, directory_fd: int):
        errors = []
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        for filename in ("state.json", "state.last-good.json"):
            try:
                fd = os.open(filename, os.O_RDONLY | nofollow, dir_fd=directory_fd)
                with os.fdopen(fd, "rb", closefd=True) as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ValueError("不是單一 regular file")
                    state = L.decode_state_bytes(stream.read(), filename)
                return self.validate_child_state(track, state)
            except (FileNotFoundError, OSError, ValueError, L.StateLoadError) as error:
                errors.append(f"{filename}: {error}")
        raise RuntimeError(f"track {track['name']} child state 無法安全讀取:" + "; ".join(errors))

    def child_state(self, track):
        child_dir = L.workspace_path(L.WORKSPACE_ROOT, track["child_workspace"])
        return self.child_state_at(track, child_dir)

    def child_is_running(self, track, state=None):
        state = state if state is not None else self.child_state(track)
        if not state:
            return False
        loop_state = state.get("loop") if isinstance(state.get("loop"), dict) else {}
        pid = loop_state.get("pid")
        child_dir = L.workspace_path(L.WORKSPACE_ROOT, track["child_workspace"])
        return bool(pid and process_alive(pid) and L.run_lock_held(child_dir / ".run.lock"))

    def record_track_event(self, track, event: str, **details):
        history = track.setdefault("event_history", [])
        item = {"event": event,
                "at": L.datetime.now().astimezone().isoformat(timespec="milliseconds"), **details}
        comparable = {key: value for key, value in item.items() if key != "at"}
        previous = ({key: value for key, value in history[-1].items() if key != "at"}
                    if history else None)
        if comparable != previous:
            history.append(item)
            if len(history) > 500:
                del history[:-500]
            return True
        return False

    def record_child_adoption(self, track, child_loop):
        """Audit adoption once per supervisor session, even for the same child session."""
        changed = False
        child_session = child_loop.get("session_id")
        sessions = track.setdefault("adopted_child_sessions", [])
        if child_session not in sessions:
            sessions.append(child_session)
            if len(sessions) > 100:
                del sessions[:-100]
            changed = True
        parent_session = self.state["loop"]["session_id"]
        already_adopted = any(
            event.get("event") == "child-adopted" and
            event.get("parent_session_id") == parent_session
            for event in track.get("event_history") or [])
        if not already_adopted:
            changed = self.record_track_event(
                track, "child-adopted", pid=child_loop.get("pid"),
                child_session_id=child_session,
                parent_session_id=parent_session) or changed
        return changed

    def sync_child_snapshot(self, track, child):
        if not child:
            return False
        changed = False
        phase_events = child.get("phase_events") or []
        child_sequence = child.get("phase_event_seq")
        sequenced = (isinstance(child_sequence, int) and not isinstance(child_sequence, bool) and
                     all(isinstance(entry.get("seq"), int) and not isinstance(entry.get("seq"), bool)
                         for entry in phase_events))
        if sequenced:
            cursor = track.get("imported_child_phase_event_seq")
            if cursor is None:
                old_cursor = int(track.get("imported_child_phase_events", 0))
                if old_cursor > len(phase_events):
                    raise RuntimeError(f"track {track['name']} child phase history 倒退")
                cursor = phase_events[old_cursor - 1]["seq"] if old_cursor else 0
            if cursor > child_sequence:
                raise RuntimeError(f"track {track['name']} child phase sequence 倒退")
            if phase_events and cursor < phase_events[0]["seq"] - 1:
                self.record_track_event(track, "child-phase-gap", after_seq=cursor,
                                        resumed_at_seq=phase_events[0]["seq"])
                changed = True
            for entry in phase_events:
                if entry["seq"] <= cursor:
                    continue
                changed = self.record_track_event(
                    track, "child-phase", phase=entry.get("phase"),
                    merge_stage=entry.get("merge_stage"), round=entry.get("round"),
                    child_at=entry.get("at"), child_seq=entry["seq"]) or changed
            if cursor != child_sequence:
                track["imported_child_phase_event_seq"] = child_sequence
                changed = True
        else:
            # Read-only compatibility for a child state written before phase_event_seq.
            imported = int(track.get("imported_child_phase_events", 0))
            if imported > len(phase_events):
                raise RuntimeError(f"track {track['name']} child phase history 倒退")
            for entry in phase_events[imported:]:
                changed = self.record_track_event(
                    track, "child-phase", phase=entry.get("phase"),
                    merge_stage=entry.get("merge_stage"), round=entry.get("round"),
                    child_at=entry.get("at")) or changed
            if imported != len(phase_events):
                track["imported_child_phase_events"] = len(phase_events)
                changed = True
        snapshot = {"phase": child.get("phase"), "merge_stage": child.get("merge_stage"),
                    "round": child.get("round"), "done_count": child.get("done_count"),
                    "merge_ready_sha": child.get("merge_ready_sha")}
        if snapshot == track.get("last_child_snapshot"):
            return changed
        track["last_child_snapshot"] = snapshot
        self.record_track_event(track, "child-state", **snapshot)
        return True

    def assert_merge_ready_child(self, track, child):
        if not child or child.get("phase") != "merge-ready":
            raise RuntimeError(f"track {track['name']} 尚無合法 merge-ready child state")
        if child.get("fleet_parent_session_id") != self.state["loop"]["session_id"]:
            raise RuntimeError(f"track {track['name']} merge-ready 來自舊 supervisor session")
        if self.child_is_running(track, child):
            raise RuntimeError(f"track {track['name']} child writer 仍在執行，拒絕 merge")
        candidate = str(child.get("merge_ready_sha") or "")
        if not OID_RE.fullmatch(candidate) or child.get("last_green_sha") != candidate:
            raise RuntimeError(f"track {track['name']} merge-ready/last-green 身分矛盾")
        completed = child.get("completed") or []
        expected_orders = {task["order"] for task in child.get("plan") or []}
        if {entry.get("order") for entry in completed} != expected_orders:
            raise RuntimeError(f"track {track['name']} completed anchors 不完整")
        for entry in completed:
            sha = str(entry.get("sha") or "")
            if (not OID_RE.fullmatch(sha) or
                    git(Path(track["worktree"]), "merge-base", "--is-ancestor",
                        sha, candidate, check=False).returncode):
                raise RuntimeError(f"track {track['name']} completed anchor 已被改寫或不在 candidate")
        return candidate

    def graceful_stop(self, from_phase: str):
        """Stop dispatching, let every active child finish its current round, and preserve all worktrees."""
        self.state.update(phase="stopping", resume_phase=from_phase)
        self.save()
        self.log("收到 graceful stop｜停止派發並等待 active child 完成本輪")
        while True:
            alive = False
            for track in self.state.get("tracks") or []:
                process = self.children.get(track["name"])
                child = self.child_state(track)
                adopted_alive = process is None and self.child_is_running(track, child)
                if adopted_alive:
                    alive = True
                    workspace = L.workspace_path(L.WORKSPACE_ROOT, track["child_workspace"])
                    self.request_round_stop(workspace, child)
                    continue
                if process is None or process.poll() is not None:
                    if process is not None:
                        self.children.pop(track["name"], None)
                    if child and child.get("phase") == "merge-ready":
                        track.update(status="merge-ready", pid=None)
                    elif track.get("status") in {"running", "repairing"}:
                        track.update(status="stopped", pid=None)
                    continue
                alive = True
                if child:
                    workspace = L.workspace_path(L.WORKSPACE_ROOT, track["child_workspace"])
                    self.request_round_stop(workspace, child)
            self.save()
            if not alive:
                break
            time.sleep(0.1)
        self.mark_stopped(from_phase)

    def write_repair_control(self, track, candidate, note):
        track["control_generation"] += 1
        payload = {"schema_version": 1, "run_id": self.state["run_id"], "track": track["name"],
                   "generation": track["control_generation"], "action": "repair",
                   "expected_child_sha": candidate,
                   "integration_sha": self.state["expected_integration_sha"],
                   "note": note[-8000:]}
        child = L.workspace_path(L.WORKSPACE_ROOT, track["child_workspace"])
        L.atomic_write_bytes(child / "fleet-control.json", json.dumps(payload, ensure_ascii=False).encode())

    def gate_and_merge(self, track) -> bool:
        self.assert_frozen_inputs()
        child = self.child_state(track)
        repo = Path(track["worktree"])
        if not child or child.get("phase") != "merge-ready":
            return False
        candidate = self.assert_merge_ready_child(track, child)
        if L.workspace_directory(repo, f"track {track['name']} worktree") is None:
            raise RuntimeError(f"{track['name']} merge-ready worktree 遺失")
        registrations = git(self.repo, "worktree", "list", "--porcelain").stdout
        actual_branch = git(repo, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
        track_common = Path(git(repo, "rev-parse", "--git-common-dir").stdout.strip())
        if not track_common.is_absolute():
            track_common = repo / track_common
        if (f"worktree {repo.resolve()}\n" not in registrations or
                actual_branch != track["branch_ref"] or
                track_common.resolve() != self.common_git_dir):
            raise RuntimeError(f"{track['name']} worktree registration/branch/common-dir 身分矛盾")
        branch_tip = git(repo, "rev-parse", track["branch_ref"]).stdout.strip()
        if not candidate or candidate != branch_tip or candidate != git(repo, "rev-parse", "HEAD").stdout.strip():
            raise RuntimeError(f"{track['name']} candidate/branch/HEAD 身分矛盾")
        expected = self.state["expected_integration_sha"]
        actual = git(self.repo, "rev-parse", self.integration_ref).stdout.strip()
        if actual != expected:
            raise RuntimeError(f"integration ref 出現未知 SHA：expected={expected} actual={actual}")
        if git(repo, "status", "--porcelain").stdout.strip():
            raise RuntimeError(f"{track['name']} merge-ready worktree 不乾淨")
        if git(repo, "merge-base", "--is-ancestor", expected, candidate, check=False).returncode:
            track["status"] = "repairing"
            self.start_track(track)
            return False
        tx = {"track": track["name"], "expected_sha": expected,
              "candidate_sha": candidate, "stage": "prepared"}
        if git(self.repo, "status", "--porcelain").stdout.strip():
            raise RuntimeError("integration worktree 在 CAS 交易前出現未知 dirty 狀態")
        self.state["merge_tx"] = tx
        track["status"] = "merging"
        self.record_track_event(track, "merge-prepared", expected_sha=expected,
                                candidate_sha=candidate)
        self.save()
        self.log(f"track {track['name']}｜CAS prepared {expected[:8]} → {candidate[:8]}")
        self.crash_point("prepared")
        git(self.repo, "update-ref", self.integration_ref, candidate, expected)
        self.log(f"track {track['name']}｜integration ref CAS 已更新")
        self.crash_point("ref-updated-unjournaled")
        tx["stage"] = "ref-updated"; self.save()
        self.crash_point("ref-updated")
        git(self.repo, "reset", "--hard", candidate)
        self.log(f"track {track['name']}｜integration worktree 已同步 candidate")
        self.crash_point("worktree-reset")
        tx["stage"] = "validating"; self.save()
        self.crash_point("validating")
        ok, tail, _timed_out, validator_mutated = self.validate_integration(
            f"track {track['name']} candidate")
        if ok:
            tx["stage"] = "validated"; self.save()
            self.state["expected_integration_sha"] = candidate
            track.update(status="merged", tip=candidate, pid=None)
            self.record_track_event(track, "merged", candidate_sha=candidate)
            self.state["merge_tx"] = None
            self.save()
            self.log(f"track {track['name']} 已 CAS 合入 {candidate[:8]}")
            L.notify(self.args.notify_cmd, "track_merged", f"{self.name}/{track['name']}")
            self.crash_point("merged-saved")
            self.cleanup_track(track)
            return True
        tx["validation_error"] = tail[-8000:]
        tx["stage"] = "rollback-prepared"
        self.record_track_event(track, "rollback-prepared", candidate_sha=candidate)
        self.save()
        self.log(f"track {track['name']}｜candidate 驗證失敗，準備 rollback")
        self.crash_point("rollback-prepared")
        git(self.repo, "update-ref", self.integration_ref, expected, candidate)
        self.log(f"track {track['name']}｜integration ref 已 CAS rollback")
        self.crash_point("rollback-ref")
        if validator_mutated:
            self.save()
            raise RuntimeError("integration validator 修改了 Git 現場；ref 已安全 rollback，"
                               "但未知 worktree 變更已保留，拒絕 reset")
        git(self.repo, "reset", "--hard", expected)
        self.log(f"track {track['name']}｜integration worktree 已回 baseline")
        self.crash_point("rollback-reset")
        baseline_ok, baseline_tail, _timed_out, baseline_mutated = self.validate_integration(
            f"track {track['name']} rollback baseline")
        if not baseline_ok:
            if baseline_mutated:
                raise RuntimeError("rollback baseline validator 修改了 Git 現場；已保留並停止")
            raise RuntimeError(f"rollback 後 baseline validate 仍失敗:\n{baseline_tail}")
        tx["stage"] = "rolled-back"; self.save()
        self.crash_point("rolled-back")
        track["integration_validate_failures"] += 1
        track["status"] = "repairing"
        self.record_track_event(track, "repairing", candidate_sha=candidate)
        track["last_integration_error"] = tail[-2000:]
        self.write_repair_control(track, candidate, tail)
        self.state["merge_tx"] = None
        self.save()
        self.log(f"track {track['name']} integration validate 失敗，已 rollback 並回送 agent 修復")
        L.notify(self.args.notify_cmd, "track_repairing", f"{self.name}/{track['name']}")
        self.start_track(track)
        return False

    def capture_track_evidence(self, track, child_state):
        evidence_root = L.ensure_real_directory(self.parent / "evidence", "fleet evidence 目錄")
        tracks_root = L.ensure_real_directory(evidence_root / "tracks", "fleet track evidence 目錄")
        destination = L.ensure_real_directory(tracks_root / track["safe_name"],
                                              f"track {track['name']} evidence 目錄")
        child = L.workspace_path(L.WORKSPACE_ROOT, track["child_workspace"])
        history_text = ""
        console_text = ""
        for path, label in ((child / "history.log", "child history"),
                            (child / "console.log", "child console")):
            try:
                value = L.read_regular_text(path, label)
            except FileNotFoundError:
                value = ""
            if path.name == "history.log":
                history_text = value
            else:
                console_text = value
        history_tail = history_text[-200_000:]
        console_tail = console_text[-200_000:]
        prompt_source = child / "prompts"
        prompt_destination = L.ensure_real_directory(destination / "prompts",
                                                     f"track {track['name']} prompt evidence")
        prompt_artifacts = []
        prompt_total = 0
        if L.workspace_directory(prompt_source, f"track {track['name']} prompts") is not None:
            prompt_paths = sorted(prompt_source.glob("round-*.md"))
            if len(prompt_paths) > 500:
                raise RuntimeError(f"track {track['name']} prompt evidence 超過 500 份上限")
            for prompt in prompt_paths:
                data = L.read_regular_bytes(prompt, f"track {track['name']} prompt")
                prompt_total += len(data)
                if prompt_total > 8_000_000:
                    raise RuntimeError(f"track {track['name']} prompt evidence 超過 8MB 上限")
                target = prompt_destination / prompt.name
                L.atomic_write_bytes(target, data)
                prompt_artifacts.append({"name": prompt.name, "sha256": L.sha256_bytes(data),
                                         "size": len(data)})
        child_config = child_state.get("config") or {}
        command = str(child_config.get("agent_cmd") or "")
        validate_command = str(child_config.get("validate_cmd") or "")
        evidence = {
            "schema_version": 1,
            "track": track["name"],
            "child_workspace": track["child_workspace"],
            "captured_at": L.datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "state": {key: child_state.get(key) for key in (
                "workspace_kind", "fleet_run_id", "fleet_parent", "track", "phase", "round",
                "flag", "done_count", "completed", "last_green_sha", "merge_stage",
                "merge_target_ref", "merge_target_tip", "merge_ready_sha", "red_streak",
                "stall_rounds", "agent_failure_streak", "task_reset_counts", "issues",
                "phase_events")},
            "no_progress_count": history_text.count("changed=False"),
            "agent_command_sha256": hashlib.sha256(command.encode()).hexdigest(),
            "validate_command_sha256": hashlib.sha256(validate_command.encode()).hexdigest(),
            "prompt_artifacts": prompt_artifacts,
            "console_tail": console_tail,
            "history_tail": history_tail,
            "event_history": track.get("event_history") or [],
        }
        data = json.dumps(evidence, ensure_ascii=False, indent=2).encode()
        if len(data) > 2_000_000:
            raise RuntimeError(f"track {track['name']} evidence.json 超過 2MB 上限")
        evidence_path = destination / "evidence.json"
        L.atomic_write_bytes(evidence_path, data)
        track["evidence_path"] = str(evidence_path.resolve())
        track["evidence_sha256"] = L.sha256_bytes(data)
        track["diagnostics"] = {"round": child_state.get("round", 0),
                                "completed": len(child_state.get("completed") or []),
                                "issues": child_state.get("issues") or [],
                                "history_tail": history_tail.splitlines()[-20:],
                                "no_progress_count": evidence["no_progress_count"]}

    @contextmanager
    def locked_child_directory(self, directory: Path, track_name: str, *,
                               require_writer_lock=True, expected_inode=None):
        """Bind a root entry to one fd; source children additionally require the writer lock."""
        root = L.ensure_real_directory(L.WORKSPACE_ROOT, "workspace root")
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise RuntimeError("平台不支援 O_NOFOLLOW child cleanup")
        root_fd = directory_fd = None
        lock_file = None
        try:
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | nofollow)
            directory_fd = os.open(directory.name, os.O_RDONLY | os.O_DIRECTORY | nofollow,
                                   dir_fd=root_fd)
            opened = os.fstat(directory_fd)
            entry = os.stat(directory.name, dir_fd=root_fd, follow_symlinks=False)
            if ((opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino) or
                    not stat.S_ISDIR(opened.st_mode)):
                raise RuntimeError("child workspace entry/inode 不符")
            inode = (opened.st_dev, opened.st_ino)
            if expected_inode is not None and inode != tuple(expected_inode):
                raise RuntimeError("child cleanup entry 與 journal inode 不符")
            if require_writer_lock:
                lock_fd = os.open(".run.lock", os.O_RDWR | nofollow, dir_fd=directory_fd)
                lock_file = os.fdopen(lock_fd, "r+b", closefd=True)
                lock_info = os.fstat(lock_file.fileno())
                if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1:
                    raise RuntimeError("child writer 鎖不是單一 regular file")
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield {"root_fd": root_fd, "directory_fd": directory_fd,
                   "name": directory.name, "inode": inode}
        except (FileNotFoundError, OSError, BlockingIOError, ValueError) as error:
            raise RuntimeError(f"track {track_name} child writer 鎖仍被持有或不安全") from error
        finally:
            if lock_file is not None:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                lock_file.close()
            if directory_fd is not None:
                os.close(directory_fd)
            if root_fd is not None:
                os.close(root_fd)

    @staticmethod
    def remove_open_tree(parent_fd: int, name: str, directory_fd: int, label: str):
        """Delete the already-open directory inode without reopening a pathname."""
        opened = os.fstat(directory_fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if ((opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino) or
                not stat.S_ISDIR(current.st_mode)):
            raise RuntimeError(f"{label} entry 在刪除前已替換")
        for entry in list(os.scandir(directory_fd)):
            info = os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                child_fd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=directory_fd)
                try:
                    Fleet.remove_open_tree(directory_fd, entry.name, child_fd,
                                           f"{label}/{entry.name}")
                finally:
                    os.close(child_fd)
            else:
                os.unlink(entry.name, dir_fd=directory_fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise RuntimeError(f"{label} entry 在 rmdir 前已替換")
        os.rmdir(name, dir_fd=parent_fd)

    def cleanup_race_hook(self, _stage: str, _track, _source: Path, _tombstone: Path):
        """Deterministic no-op hook patched by replacement-race regression tests."""

    def remove_child_workspace(self, track, child: Path, tombstone: Path):
        """Resume-safe exact child removal; absence is idempotent, replacement is never deleted."""
        lock_names = sorted({track["child_workspace"], tombstone.name})
        with ExitStack() as operations:
            for workspace_name in lock_names:
                operations.enter_context(
                    L.workspace_operation_lock(L.WORKSPACE_ROOT, workspace_name, blocking=False))
            child_dir = L.workspace_directory(child, f"track {track['name']} child workspace")
            tombstone_dir = L.workspace_directory(tombstone,
                                                  f"track {track['name']} child tombstone")
            if child_dir is not None and tombstone_dir is not None:
                raise RuntimeError(f"track {track['name']} child 與 tombstone 同時存在")
            target = child_dir or tombstone_dir
            if target is None:
                if track.get("cleanup_child_ino") is None:
                    raise RuntimeError(
                        f"track {track['name']} child 在 journal identity 落盤前遺失")
                return
            journal_inode = (track.get("cleanup_child_dev"),
                             track.get("cleanup_child_ino"))
            recovering_tombstone = target == tombstone and journal_inode[0] is not None
            with self.locked_child_directory(
                    target, track["name"], require_writer_lock=not recovering_tombstone,
                    expected_inode=journal_inode if journal_inode[0] is not None else None) as locked:
                journal_generation = track.get("cleanup_child_generation")
                if journal_inode[0] is None:
                    if target != child:
                        raise RuntimeError(
                            f"track {track['name']} tombstone 缺 cleanup journal identity")
                    verified = self.child_state_from_fd(track, locked["directory_fd"])
                    child_loop = (verified.get("loop")
                                  if isinstance(verified.get("loop"), dict) else {})
                    if process_alive(child_loop.get("pid")):
                        raise RuntimeError(f"track {track['name']} child pid 仍存活，拒絕清理")
                    generation = verified.get("workspace_generation")
                    if re.fullmatch(r"[0-9a-f]{32}", str(generation or "")) is None:
                        raise RuntimeError(
                            f"track {track['name']} child workspace generation 不合法")
                    track["cleanup_child_dev"], track["cleanup_child_ino"] = locked["inode"]
                    track["cleanup_child_generation"] = generation
                    track["cleanup_child_tombstone"] = str(tombstone)
                    track["cleanup_stage"] = "child-removing"
                    self.save()
                    self.crash_point("cleanup-child-removing")
                    journal_inode = locked["inode"]
                    journal_generation = generation
                elif locked["inode"] != journal_inode:
                    raise RuntimeError(
                        f"track {track['name']} cleanup target inode 與 journal 不符")
                if target == child:
                    verified = self.child_state_from_fd(track, locked["directory_fd"])
                    child_loop = (verified.get("loop")
                                  if isinstance(verified.get("loop"), dict) else {})
                    if process_alive(child_loop.get("pid")):
                        raise RuntimeError(f"track {track['name']} child pid 仍存活，拒絕清理")
                    if verified.get("workspace_generation") != journal_generation:
                        raise RuntimeError(
                            f"track {track['name']} child generation 與 cleanup journal 不符")
                # Once renamed, the deterministic tombstone itself keeps the inode live.
                # Recovery may therefore finish a partially-unlinked tree even when both
                # state copies were among the entries removed before the crash.
                self.cleanup_race_hook("after-identity", track, target, tombstone)
                root_fd = locked["root_fd"]
                directory_fd = locked["directory_fd"]
                current = os.stat(target.name, dir_fd=root_fd, follow_symlinks=False)
                if locked["inode"] != (current.st_dev, current.st_ino):
                    raise RuntimeError(f"track {track['name']} cleanup target 在驗證後已替換")
                delete_name = target.name
                if target == child:
                    try:
                        os.stat(tombstone.name, dir_fd=root_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise RuntimeError(f"track {track['name']} tombstone 已存在")
                    os.rename(child.name, tombstone.name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
                    delete_name = tombstone.name
                renamed = os.stat(delete_name, dir_fd=root_fd, follow_symlinks=False)
                if locked["inode"] != (renamed.st_dev, renamed.st_ino):
                    raise RuntimeError(f"track {track['name']} tombstone inode 身分不符")
                self.remove_open_tree(root_fd, delete_name, directory_fd,
                                      f"track {track['name']} child tombstone")

    def cleanup_track(self, track):
        """Idempotent cleanup journal: evidence → worktree → child → runtime → cleaned."""
        if track.get("status") == "cleaned":
            if track.get("cleanup_stage") != "complete":
                raise RuntimeError(f"track {track['name']} cleaned/cleanup stage 不一致")
            return
        if track.get("status") != "merged":
            raise RuntimeError(f"track {track['name']} 尚未 merged，拒絕 cleanup")
        repo = Path(track["worktree"])
        child = L.workspace_path(L.WORKSPACE_ROOT, track["child_workspace"])
        stage = track.get("cleanup_stage")
        if stage is None:
            child_state = self.child_state(track)
            if not child_state:
                raise RuntimeError(f"已合併 track {track['name']} 缺 child state，無法保存證據")
            if self.child_is_running(track, child_state):
                raise RuntimeError(f"已合併 track {track['name']} child writer 仍在執行")
            self.capture_track_evidence(track, child_state)
            track["cleanup_stage"] = stage = "evidence-captured"
            self.record_track_event(track, "cleanup-evidence-captured")
            self.save()
            self.crash_point("cleanup-evidence-captured")

        if stage == "evidence-captured":
            worktrees_root = self.parent / "worktrees"
            L.workspace_directory(worktrees_root, "fleet worktrees 目錄")
            registrations = git(self.repo, "worktree", "list", "--porcelain").stdout
            registered = f"worktree {repo.resolve()}\n" in registrations
            repo_dir = L.workspace_directory(repo, f"track {track['name']} worktree")
            if repo_dir is not None:
                if not registered:
                    raise RuntimeError(f"已合併 track {track['name']} worktree 未註冊")
                if git(repo, "status", "--porcelain").stdout.strip():
                    raise RuntimeError(f"已合併 track {track['name']} worktree 不乾淨，拒絕清理")
                if git(repo, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip() != track["branch_ref"]:
                    raise RuntimeError(f"已合併 track {track['name']} branch 身分不符")
                track_common = Path(git(repo, "rev-parse", "--git-common-dir").stdout.strip())
                if not track_common.is_absolute():
                    track_common = repo / track_common
                if track_common.resolve() != self.common_git_dir:
                    raise RuntimeError(f"已合併 track {track['name']} common-dir 身分不符")
                git(self.repo, "worktree", "remove", str(repo))
            elif registered:
                git(self.repo, "worktree", "prune")
            git(self.repo, "worktree", "prune")
            track["cleanup_stage"] = stage = "worktree-removed"
            self.record_track_event(track, "cleanup-worktree-removed")
            self.save()
            self.crash_point("cleanup-worktree-removed")

        if stage == "worktree-removed":
            tombstone = L.workspace_path(L.WORKSPACE_ROOT,
                                         f"delete-{self.state['run_id']}-{track['safe_name']}")
            self.remove_child_workspace(track, child, tombstone)
            track["cleanup_stage"] = stage = "child-removed"
            self.record_track_event(track, "cleanup-child-removed")
            self.save()
            self.crash_point("cleanup-child-removed")

        if stage == "child-removing":
            tombstone = Path(track.get("cleanup_child_tombstone") or "")
            expected_tombstone = L.workspace_path(
                L.WORKSPACE_ROOT, f"delete-{self.state['run_id']}-{track['safe_name']}")
            if tombstone != expected_tombstone:
                raise RuntimeError(f"track {track['name']} cleanup tombstone 身分不符")
            self.remove_child_workspace(track, child, tombstone)
            track["cleanup_stage"] = stage = "child-removed"
            self.record_track_event(track, "cleanup-child-removed", recovered=True)
            self.save()

        if stage == "child-removed":
            runtime_root = self.parent / "runtime"
            L.workspace_directory(runtime_root, "fleet runtime 目錄")
            runtime = runtime_root / track["safe_name"]
            runtime_dir = L.workspace_directory(runtime, f"track {track['name']} runtime")
            if runtime_dir is not None:
                shutil.rmtree(runtime_dir)
            track["cleanup_stage"] = "complete"
            track["status"] = "cleaned"
            track["ended_at"] = L.datetime.now().astimezone().isoformat(timespec="milliseconds")
            self.record_track_event(track, "cleaned")
            self.save()
            self.log(f"track {track['name']}｜cleanup 完成")

    def recover_merge_transaction(self):
        """Resume only stage/ref combinations produced by the journaled CAS state machine."""
        tx = self.state.get("merge_tx")
        if not tx:
            return
        track = next((item for item in self.state["tracks"] if item["name"] == tx.get("track")), None)
        if track is None:
            raise RuntimeError("merge_tx 指向不存在的 track")
        expected, candidate, stage = (tx.get("expected_sha"), tx.get("candidate_sha"),
                                      tx.get("stage"))
        actual = git(self.repo, "rev-parse", self.integration_ref).stdout.strip()
        if actual not in {expected, candidate}:
            raise RuntimeError(f"CAS resume 發現第三個 SHA：{actual}")
        legal = {
            "prepared": {expected, candidate},
            "ref-updated": {candidate},
            "validating": {candidate},
            "validated": {candidate},
            "rollback-prepared": {candidate, expected},
            "rolled-back": {expected},
        }
        if stage not in legal or actual not in legal[stage]:
            raise RuntimeError(f"CAS resume 非法 stage/ref 組合：stage={stage} ref={actual}")

        # prepared+expected means update-ref never happened. Abort the transaction, but
        # force the child to rebind to this new supervisor session before it can queue again.
        if stage == "prepared" and actual == expected:
            self.assert_known_integration_tree(expected)
            ok, tail, _timed_out, mutated = self.validate_integration("CAS prepared baseline recovery")
            if not ok:
                reason = "validator 修改現場" if mutated else "驗證失敗"
                raise RuntimeError(f"CAS prepared baseline recovery {reason}:\n{tail}")
            track.update(status="stopped", pid=None)
            self.record_track_event(track, "transaction-aborted", stage=stage,
                                    expected_sha=expected, candidate_sha=candidate)
            self.state["merge_tx"] = None
            self.save()
            return

        rollback_required = stage in {"rollback-prepared", "rolled-back"}
        if rollback_required:
            self.assert_known_integration_tree(candidate, expected)
            if actual == candidate:
                self.log(f"track {track['name']}｜resume retry CAS rollback")
                git(self.repo, "update-ref", self.integration_ref, expected, candidate)
            git(self.repo, "reset", "--hard", expected)
            tx["stage"] = "rolled-back"
            self.save()
            ok, tail, _timed_out, mutated = self.validate_integration("CAS rollback recovery baseline")
            if not ok:
                reason = "validator 修改現場" if mutated else "驗證失敗"
                raise RuntimeError(f"CAS rollback recovery baseline {reason}:\n{tail}")
            note = str(tx.get("validation_error") or
                       "fleet crash 後從 rollback journal 恢復")
            if not tx.get("repair_recorded"):
                track["integration_validate_failures"] += 1
                track["status"] = "repairing"
                track["last_integration_error"] = note[-2000:]
                self.write_repair_control(track, candidate, note)
                tx["repair_recorded"] = True
                self.record_track_event(track, "repairing", candidate_sha=candidate,
                                        recovered=True)
                self.save()
            self.state["merge_tx"] = None
            self.save()
            return

        # The ref moved to candidate. ref-updated may still have the exact expected
        # index/worktree; validating/validated must already be the candidate tree.
        allowed_tree = (expected, candidate) if stage in {"prepared", "ref-updated"} else (candidate,)
        self.assert_known_integration_tree(*allowed_tree)
        git(self.repo, "reset", "--hard", candidate)
        if stage == "validated":
            ok, tail, mutated = True, "", False
        else:
            ok, tail, _timed_out, mutated = self.validate_integration("CAS candidate recovery")
        if ok:
            tx["stage"] = "validated"
            self.save()
            self.state["expected_integration_sha"] = candidate
            track.update(status="merged", tip=candidate, pid=None)
            self.record_track_event(track, "merged", candidate_sha=candidate, recovered=True)
            self.state["merge_tx"] = None
            self.save()
            self.cleanup_track(track)
            return

        tx["validation_error"] = tail[-8000:]
        tx["stage"] = "rollback-prepared"
        self.record_track_event(track, "rollback-prepared", candidate_sha=candidate,
                                recovered=True)
        self.save()
        git(self.repo, "update-ref", self.integration_ref, expected, candidate)
        if mutated:
            raise RuntimeError("CAS recovery validator 修改了 Git 現場；ref 已 rollback，未知變更已保留")
        git(self.repo, "reset", "--hard", expected)
        tx["stage"] = "rolled-back"
        self.save()
        baseline_ok, baseline_tail, _timed_out, baseline_mutated = self.validate_integration(
            "CAS recovery rollback baseline")
        if not baseline_ok:
            reason = "validator 修改現場" if baseline_mutated else "驗證失敗"
            raise RuntimeError(f"CAS recovery rollback baseline {reason}:\n{baseline_tail}")
        track["integration_validate_failures"] += 1
        track["status"] = "repairing"
        track["last_integration_error"] = tail[-2000:]
        self.write_repair_control(track, candidate, tail)
        tx["repair_recorded"] = True
        self.record_track_event(track, "repairing", candidate_sha=candidate, recovered=True)
        self.save()
        self.state["merge_tx"] = None
        self.save()

    def supervise_group(self, final: bool):
        group = [t for t in self.state["tracks"] if (t["name"] == "@final") == final and
                 t["status"] not in {"merged", "cleaned"}]
        while group:
            try:
                self.assert_frozen_inputs()
            except (OSError, ValueError, RuntimeError) as error:
                self.state["stop_reason"] = str(error)
                self.save()
                self.log(str(error))
                self.graceful_stop("final" if final else "exec")
                return False
            if self.stop_requested():
                self.graceful_stop("final" if final else "exec")
                return False
            snapshot_changed = False
            adopted = set()
            for track in group:
                child = self.child_state(track)
                snapshot_changed = self.sync_child_snapshot(track, child) or snapshot_changed
                process = self.children.get(track["name"])
                owned_alive = process is not None and process.poll() is None
                if child and (owned_alive or self.child_is_running(track, child)):
                    if not owned_alive:
                        adopted.add(track["name"])
                        child_loop = child.get("loop") if isinstance(child.get("loop"), dict) else {}
                        snapshot_changed = (self.record_child_adoption(track, child_loop) or
                                            snapshot_changed)
                    continue
                if child and child.get("phase") == "merge-ready":
                    if child.get("fleet_parent_session_id") == self.state["loop"]["session_id"]:
                        if track.get("status") != "merge-ready":
                            track.update(status="merge-ready", pid=None)
                            snapshot_changed = True
                    elif track.get("status") not in {"merged", "cleaned"}:
                        track.update(status="stopped", pid=None)
                        snapshot_changed = True
            if snapshot_changed:
                self.save()
            for track in group:
                if track.get("status") == "merge-ready" and track["name"] not in self.state["merge_queue"]:
                    self.state["merge_queue"].append(track["name"])
                    self.record_track_event(track, "queued")
                    self.save()
            if self.state["merge_queue"]:
                queued_name = self.state["merge_queue"].pop(0)
                queued = next((track for track in group if track["name"] == queued_name), None)
                if queued is not None:
                    self.record_track_event(queued, "dequeued")
                self.save()
                if queued is not None and queued.get("status") == "merge-ready":
                    self.state["phase"] = "merging"
                    self.save()
                    self.gate_and_merge(queued)
                    self.state["phase"] = "final" if final else "exec"
                    self.save()
                    group = [t for t in group if t["status"] not in {"merged", "cleaned"}]
                    continue
            active = sum(1 for p in self.children.values() if p.poll() is None) + len(adopted)
            for track in group:
                if active >= self.args.max_parallel:
                    break
                process = self.children.get(track["name"])
                if process is not None:
                    continue
                if track["name"] in adopted:
                    continue
                if track["status"] in {"pending", "running", "repairing", "stopped"}:
                    self.start_track(track, initial=not track.get("started", False)); active += 1
            progressed = False
            for track in list(group):
                process = self.children.get(track["name"])
                if process is None or process.poll() is None:
                    continue
                self.children.pop(track["name"], None)
                child = self.child_state(track)
                if (child and child.get("phase") == "merge-ready" and
                        child.get("fleet_parent_session_id") == self.state["loop"]["session_id"]):
                    track["status"] = "merge-ready"; track["pid"] = None; self.save()
                    if track["name"] not in self.state["merge_queue"]:
                        self.state["merge_queue"].append(track["name"])
                        self.record_track_event(track, "queued")
                        self.save()
                    progressed = True
                elif track["status"] in {"repairing", "running"}:
                    track["restart_count"] += 1
                    if self.args.max_child_restarts and track["restart_count"] > self.args.max_child_restarts:
                        raise RuntimeError(f"track {track['name']} 超過 child restart 上限")
                    time.sleep(min(2 ** min(track["restart_count"], 5), 30))
                    self.start_track(track)
            group = [t for t in group if t["status"] not in {"merged", "cleaned"}]
            if group and not progressed:
                time.sleep(0.25)
        return True

    def report(self):
        lines = ["# Parallel Run Report", "", f"- run: `{self.state['run_id']}`",
                 f"- integration ref: `{self.integration_ref}`",
                 f"- final SHA: `{self.state['expected_integration_sha']}`",
                 f"- generated: `{L.datetime.now().astimezone().isoformat(timespec='seconds')}`",
                 "", "## Phase history", ""]
        for entry in self.state.get("phase_history") or []:
            lines.append(f"- {entry.get('phase')}: {entry.get('started_at')} → {entry.get('ended_at') or 'open'} ({entry.get('duration_seconds')})")
        lines.extend(["", "## Merge transaction history", ""])
        for entry in self.state.get("merge_history") or []:
            lines.append(f"- {entry.get('at')} · {entry.get('track')} · {entry.get('stage')} · {str(entry.get('candidate_sha') or '')[:8]}")
        lines.extend(["", "## Tracks", ""])
        for track in self.state["tracks"]:
            diagnostics = track.get("diagnostics") or {}
            lines.extend([f"### `{track['name']}`", "",
                          f"- branch: `{track.get('branch_ref')}`",
                          f"- candidate: `{track.get('tip') or 'n/a'}`",
                          f"- status: `{track.get('status')}`",
                          f"- child workspace: `{track.get('child_workspace')}`",
                          f"- runtime port: `{track.get('port')}`",
                          f"- orders: `{json.dumps(self.state.get('order_map', {}).get(track['name'], {}), ensure_ascii=False)}`",
                          f"- rounds: {diagnostics.get('round', 0)}; completed: {diagnostics.get('completed', 0)}",
                          f"- validate rollbacks: {track['integration_validate_failures']}; restarts: {track['restart_count']}",
                          f"- last integration error: `{str(track.get('last_integration_error') or 'n/a')[:500].replace(chr(10), ' ')}`",
                          f"- issues: {len(diagnostics.get('issues') or [])}",
                          f"- evidence: `{track.get('evidence_path') or 'n/a'}`",
                          "- event history:"])
            for event in track.get("event_history") or []:
                lines.append("  - " + json.dumps(event, ensure_ascii=False, sort_keys=True))
            lines.append("")
        L.atomic_write_bytes(self.parent / "REPORT.md", ("\n".join(lines) + "\n").encode())

    def execute(self):
        L.ensure_real_directory(L.WORKSPACE_ROOT, "workspace root")
        if self.args.resume:
            if L.workspace_directory(self.parent, "fleet workspace") is None:
                raise RuntimeError(f"fleet workspace {self.name!r} 不存在，不能 resume")
            self.acquire_parent_run_lock()
            self.preflight()
        else:
            # Repo/ref/exact-worktree conflicts are proven before this name gains a
            # parent directory, so a rejected first launch cannot poison retries.
            with L.workspace_operation_lock(L.WORKSPACE_ROOT, self.name, blocking=False):
                if L.workspace_directory(self.parent, "fleet workspace") is not None:
                    raise RuntimeError(
                        f"workspace {self.name!r} 已存在；新 run 必須先刪除舊 workspace")
                self.pending_runtime_marker = L.configure_pending_runtime_identity(
                    L.WORKSPACE_ROOT, self.name, self.repo, uuid.uuid4().hex,
                    uuid.uuid4().hex)
                self.preflight()
                self.validate_tracked_inputs()
                self.acquire_integration_worktree_lock(announce=False)
                if L.workspace_directory(self.parent, "fleet workspace") is not None:
                    raise RuntimeError(f"workspace {self.name!r} 在 preflight 期間已建立")
                self.parent.mkdir()
                L.acquire_run_lock(self.parent / ".fleet.run.lock", f"fleet '{self.name}'")
        resuming = self.args.resume
        if resuming:
            self.load()
            self.apply_frozen_resume_config()
            self.validate_tracked_inputs()
            self.acquire_integration_worktree_lock()
            self.control_path.unlink(missing_ok=True)
            if self.state.get("phase") in {"stopped", "awaiting-approval", "failed"}:
                self.state["phase"] = self.state.get("resume_phase") or "exec"
                self.state["resume_phase"] = None
                self.state.pop("error", None)
            new_session = uuid.uuid4().hex
            sessions = self.state.setdefault("supervisor_session_history", [])
            sessions.append(new_session)
            if len(sessions) > 100:
                del sessions[:-100]
            self.state["loop"] = {"pid": os.getpid(), "session_id": new_session,
                                  "started_at": L.datetime.now().isoformat(timespec="seconds")}
            self.save()
            self.configure_runtime_identity()
            if not self.state.get("plan"):
                if not self.run_planning_handoff():
                    return
                # The planning loop owned (and removed) this workspace's markers
                # during handoff; restore the still-running fleet supervisor identity.
                self.configure_runtime_identity()
            else:
                if self.state.get("merge_tx"):
                    self.recover_merge_transaction()
                else:
                    self.assert_frozen_inputs()
                    self.assert_known_integration_tree(self.state["expected_integration_sha"])
                    ok, tail, _timed_out, mutated = self.validate_integration("resume baseline")
                    if not ok:
                        reason = "validator 修改現場" if mutated else "驗證失敗"
                        raise RuntimeError(f"resume baseline {reason}:\n{tail}")
                for track in self.state.get("tracks") or []:
                    if track.get("status") == "merged":
                        self.cleanup_track(track)
                planned = {task["track"] for task in self.state["plan"] if task["track"] != "@final"}
                existing = {track["name"] for track in self.state.get("tracks") or []}
                if planned - existing:
                    self.split_tracks(include_final=False)
        else:
            self.state = self.initial_state(uuid.uuid4().hex)
            self.save()
            if self.args.import_plan:
                self.freeze_import_plan()
                self.configure_runtime_identity()
                self.assert_known_integration_tree(git(self.repo, "rev-parse", self.integration_ref).stdout.strip())
                ok, tail, _timed_out, mutated = self.validate_integration("integration baseline")
                if not ok:
                    reason = "validator 修改現場" if mutated else "驗證失敗"
                    raise RuntimeError(f"integration baseline {reason}:\n{tail}")
            if not self.create_new():
                return
            self.configure_runtime_identity()
        if not self.supervise_group(final=False):
            return
        if any(task["track"] == "@final" for task in self.state["plan"]):
            self.split_tracks(include_final=True)
            if not self.supervise_group(final=True):
                return
        self.state["phase"] = "cleaning"; self.save()
        self.state["phase"] = "done"
        self.state["loop"]["pid"] = None
        self.save()
        self.report()
        self.log("全部軌道完成")
        L.notify(self.args.notify_cmd, "fleet_completed", self.name)


def main(argv=None):
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    try:
        args.track_env = validate_track_env(json.loads(args.track_env_json))
    except (json.JSONDecodeError, ValueError) as error:
        argument_parser.error(f"--track-env-json 不合法:{error}")
    if not 1 <= args.max_parallel <= 8:
        argument_parser.error("--max-parallel 必須是 1..8")
    for value, option in ((args.merge_threshold, "--merge-threshold"),
                          (args.done_threshold, "--done-threshold"),
                          (args.flag_threshold, "--flag-threshold"),
                          (args.red_limit, "--red-limit"),
                          (args.stall_limit, "--stall-limit")):
        if value < 1:
            argument_parser.error(f"{option} 必須 ≥ 1")
    if args.max_child_restarts < 0:
        argument_parser.error("--max-child-restarts 必須 ≥ 0")
    if not 0 <= args.track_port_base <= 65527:
        argument_parser.error("--track-port-base 必須是 0..65527（0=自動動態 port）")
    if args.expected_run_id and re.fullmatch(r"[0-9a-f]{32}", args.expected_run_id) is None:
        argument_parser.error("--expected-run-id 必須是 32 字元小寫 hex")
    for value, option, positive in ((args.round_timeout, "--round-timeout", False),
                                    (args.validate_timeout, "--validate-timeout", True),
                                    (args.agent_backoff_max, "--agent-backoff-max", False)):
        if not math.isfinite(value) or value < 0 or (positive and value == 0):
            argument_parser.error(f"{option} 必須是{' > 0' if positive else ' ≥ 0'} 的有限數字")
    fleet = Fleet(args)
    try:
        fleet.execute()
    except KeyboardInterrupt:
        fleet.stop_processes()
        fleet.clear_parent_loop_marker()
        if fleet.state:
            mark_fleet_interrupted(fleet.state)
            fleet.save()
        L.notify(args.notify_cmd, "fleet_stopped", fleet.name)
        raise
    except Exception as error:
        fleet.stop_processes()
        fleet.clear_parent_loop_marker()
        if fleet.state:
            failed_from = fleet.state.get("phase")
            resume_phase = recoverable_phase(fleet.state)
            message = str(error)[-8000:]
            fleet.state["resume_phase"] = resume_phase
            fleet.state["phase"] = "failed"
            fleet.state["error"] = message
            fleet.state["last_error"] = {
                "at": L.datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "phase": failed_from if failed_from in FLEET_PHASES else resume_phase,
                "message": message,
            }
            fleet.state.setdefault("loop", {})["pid"] = None
            fleet.save()
        L.notify(args.notify_cmd, "fleet_failed", fleet.name)
        raise
    finally:
        fleet.clear_pending_runtime_identity()
        L.remove_runtime_identity_markers()
        L.clear_runtime_identity_context()


if __name__ == "__main__":
    main()
