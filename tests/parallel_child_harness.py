"""Low-level guardian containment harness with launch authority injected.

Production ``engine.parallel_child.main`` always claims a real durable launch
reservation before releasing its payload.  The protocol unit tests below the
run-artifact layer use this harness so they can focus on ACK/containment/reap
behavior; integration tests exercise the real authorizer.
"""

from __future__ import annotations

import sys

from engine import parallel_child


def main() -> int:
    args = parallel_child.build_argument_parser().parse_args()
    payload = list(args.payload)
    if payload[:1] == ["--"]:
        payload = payload[1:]
    return parallel_child.run_guardian(
        payload,
        run_dir=args.run_dir,
        task=args.task,
        child_id=args.child_id,
        launch_authorizer=lambda _run_dir, _record, *, payload_pid: None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
