"""Supervisor-shaped launcher for real managed-worker subprocess tests."""

from __future__ import annotations

import argparse
import subprocess
import sys

from engine import parallel_child
from engine import platform_compat as compat


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task", required=True, type=int)
    parser.add_argument("--child-id", required=True)
    parser.add_argument("--supervisor-session", required=True)
    parser.add_argument("--supervisor-generation", required=True, type=int)
    parser.add_argument("--attempt", required=True, type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("payload", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    payload = list(args.payload)
    if payload[:1] == ["--"]:
        payload = payload[1:]
    guardian_argv = parallel_child.build_guardian_argv(
        sys.executable, args.run_dir, args.task, args.child_id, payload)
    process = subprocess.Popen(
        guardian_argv,
        stdin=subprocess.PIPE,
        **compat.popen_group_kwargs(),
    )
    try:
        if process.stdin is None:
            raise RuntimeError("guardian control pipe unavailable")
        if compat.attach_process_group(process) is not True:
            raise RuntimeError("guardian containment unavailable")
        ready = parallel_child.child_record(
            run_id=args.run_id,
            task=args.task,
            child_id=args.child_id,
            supervisor_session=args.supervisor_session,
            supervisor_generation=args.supervisor_generation,
            attempt=args.attempt,
            resume=args.resume,
            guardian_pid=process.pid,
            guardian_start_token=compat.process_start_token(process.pid),
            argv_hash=parallel_child.payload_argv_hash(payload),
            state="guardian_ready",
        )
        parallel_child.write_child_record(args.run_dir, ready)
        process.stdin.write(parallel_child.ACK_BYTE)
        process.stdin.flush()
        return int(compat.wait_process(process))
    finally:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
            process.stdin = None
        if process.poll() is None:
            try:
                compat.kill_process_group(process)
            except (OSError, ProcessLookupError, ValueError):
                process.kill()
            compat.wait_process(process)
        compat.close_process_group(process)


if __name__ == "__main__":
    raise SystemExit(main())
