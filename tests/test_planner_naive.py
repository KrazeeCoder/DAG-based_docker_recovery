import unittest

from dockrepair_data import Environment, Service
from dockrepair_planner import search
from dockrepair_planner_naive import search_naive

BASE = frozenset({"compose_valid", "daemon_reachable"})


class NaivePlannerTests(unittest.TestCase):
    def test_naive_planner_does_not_use_batch_reconciliation(self):
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

        aware_plan, _ = search(environment)
        naive_plan, _ = search_naive(environment)

        self.assertEqual(
            [action.name for action in aware_plan.actions],
            ["Reconcile services together: api, database, worker"],
        )
        self.assertEqual(len(naive_plan.actions), 1)
        self.assertNotIn("Reconcile services together", naive_plan.actions[0].name)

    def test_naive_planner_can_propose_an_action_whose_precondition_is_unmet(self):
        services = {
            "api": Service(True, "api"),
            "worker": Service(False, "worker", (("api", "service_healthy"),)),
        }
        state = BASE | frozenset({
            "container_exists:api", "config_current:api",
            "container_exists:worker", "config_current:worker",
        })
        environment = Environment("compose.yaml", "test", services, state)
        naive_plan, _ = search_naive(environment)
        self.assertEqual(len(naive_plan.actions), 1)


if __name__ == "__main__":
    unittest.main()
