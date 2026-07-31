import unittest

from dockrepair import _effect_was_observed
from dockrepair_data import Action, Environment, Service
from dockrepair_planner import search


BASE = frozenset({"compose_valid", "daemon_reachable"})


class AlternativePathTests(unittest.TestCase):
    def test_persistently_unhealthy_is_not_a_successful_transition(self):
        action = Action(
            "Restart cache",
            ("restart", "cache"),
            3,
            frozenset(),
            frozenset({"running:cache", "health_pending:cache"}),
        )
        environment = Environment(
            "compose.yaml",
            "test",
            {"cache": Service(True, "cache")},
            BASE | frozenset({"running:cache", "unhealthy:cache"}),
        )

        self.assertFalse(_effect_was_observed(action, environment))

    def test_batch_edge_beats_sequential_dependency_path(self):
        services = {
            "database": Service(True, "db"),
            "api": Service(True, "api", (("database", "service_healthy"),)),
            "worker": Service(False, "worker", (("api", "service_healthy"),)),
        }
        stopped_facts = BASE | frozenset(
            fact
            for name in services
            for fact in (f"container_exists:{name}", f"config_current:{name}")
        )
        environment = Environment("compose.yaml", "test", services, stopped_facts)

        plan, _ = search(environment)

        self.assertEqual(
            [action.name for action in plan.actions],
            ["Reconcile services together: api, database, worker"],
        )
        self.assertEqual(plan.total_cost, 5)

    def test_rejected_batch_edge_falls_back_to_dependency_order(self):
        services = {
            "database": Service(True, "db"),
            "api": Service(True, "api", (("database", "service_healthy"),)),
            "worker": Service(False, "worker", (("api", "service_healthy"),)),
        }
        stopped_facts = BASE | frozenset(
            fact
            for name in services
            for fact in (f"container_exists:{name}", f"config_current:{name}")
        )
        environment = Environment("compose.yaml", "test", services, stopped_facts)

        plan, _ = search(
            environment,
            frozenset({"Reconcile services together: api, database, worker"}),
        )

        self.assertEqual(
            [action.name for action in plan.actions],
            [
                "Start database",
                "Verify database health",
                "Start api",
                "Verify api health",
                "Start worker",
            ],
        )

    def test_restart_is_cheaper_than_recreation(self):
        services = {"cache": Service(True, "cache")}
        environment = Environment(
            "compose.yaml",
            "test",
            services,
            BASE
            | frozenset(
                {
                    "container_exists:cache",
                    "config_current:cache",
                    "running:cache",
                    "unhealthy:cache",
                }
            ),
        )

        cheap_plan, _ = search(environment)
        fallback_plan, _ = search(environment, frozenset({"Restart cache"}))

        self.assertEqual([action.name for action in cheap_plan.actions], ["Restart cache", "Verify cache health"])
        self.assertEqual([action.name for action in fallback_plan.actions], ["Recreate cache", "Verify cache health"])
        self.assertLess(cheap_plan.total_cost, fallback_plan.total_cost)


if __name__ == "__main__":
    unittest.main()
