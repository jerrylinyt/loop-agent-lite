import unittest

from engine import dashboard
from engine import status


RUN_ID = "a" * 32


def parent_projection():
    return {
        "name": "parent",
        "workspace_kind": "fleet-parent",
        "fleet_run_id": RUN_ID,
        "phase": "done",
        "parallel_phase": "done",
        "parallel_tracks": [{
            "name": "backend",
            "child_workspace": "parent--backend",
            "status": "cleaned",
        }],
        "issues": 0,
        "unread_issues": 0,
        "plan_len": 1,
        "completed": 1,
    }


def child_projection(name="parent--backend", track="backend", **updates):
    child = {
        "name": name,
        "workspace_kind": "fleet-child",
        "fleet_parent": "parent",
        "fleet_run_id": RUN_ID,
        "track": track,
        "phase": "exec",
        "issues": 0,
        "unread_issues": 0,
        "plan_len": 0,
        "completed": 0,
    }
    child.update(updates)
    return child


class TestAuthoritativeChildRegistrationProjection(unittest.TestCase):
    def test_health_deduplicates_only_exact_registered_child(self):
        projection = dashboard.fleet_health_projection([
            parent_projection(), child_projection(running=True),
        ])
        self.assertEqual(projection["workspace_count"], 1)
        self.assertEqual(projection["running"], 0)

    def test_same_run_unregistered_child_keeps_running_error_and_attention(self):
        orphan = child_projection(
            "parent--ui", "ui", running=True, error="unregistered child",
            issues=1, unread_issues=1,
        )
        projection = dashboard.fleet_health_projection([parent_projection(), orphan])
        self.assertEqual(projection["workspace_count"], 2)
        self.assertEqual(projection["running"], 1)
        self.assertEqual(projection["error_count"], 1)
        self.assertEqual(projection["attention"], 1)
        self.assertEqual(projection["issues"], 1)
        self.assertEqual(projection["status"], "error")

    def test_registered_track_with_mismatched_child_workspace_stays_visible(self):
        replacement = child_projection(
            "parent--replacement", "backend", running=True, issues=1, unread_issues=1,
        )
        projection = dashboard.fleet_health_projection(
            [parent_projection(), replacement])
        self.assertEqual(projection["workspace_count"], 2)
        self.assertEqual(projection["running"], 1)
        self.assertEqual(projection["attention"], 1)

    def test_status_summary_uses_same_exact_registration_contract(self):
        registered = child_projection(
            running=True, issues=9, unread_issues=9, agent_failure_streak=9)
        orphan = child_projection(
            "parent--ui", "ui", running=True, issues=1, unread_issues=1,
            agent_failure_streak=2)
        broken_orphan = {
            "name": "parent--replacement",
            "workspace_kind": "fleet-child",
            "fleet_parent": "parent",
            "fleet_run_id": RUN_ID,
            "track": "backend",
            "error": "unreadable replacement child",
        }
        summary = status.summarize_status(
            [parent_projection(), registered, orphan, broken_orphan])
        self.assertEqual(summary["workspace_count"], 4)
        self.assertEqual(summary["valid_count"], 3)
        self.assertEqual(summary["error_count"], 1)
        self.assertEqual(summary["running"], 1)
        self.assertEqual(summary["attention"], 1)
        self.assertEqual(summary["issues"], 1)
        self.assertEqual(summary["unread_issues"], 1)
        self.assertEqual(summary["agent_failures"], 2)
        self.assertEqual(summary["tasks_total"], 1)


if __name__ == "__main__":
    unittest.main()
