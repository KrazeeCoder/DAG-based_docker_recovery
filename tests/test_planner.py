import unittest
from unittest.mock import patch

from dockrepair import _effect_was_observed
from dockrepair_data import (
    Action,
    Container,
    Environment,
    Port,
    PublishedPort,
    ReadinessProbe,
    Resource,
    Service,
)
from dockrepair_docker import (
    _bindings_conflict,
    _port_matches,
    _probe_readiness,
    _published_ports,
    _readiness,
)
from dockrepair_planner import build_actions, search


BASE = frozenset({"compose_valid", "daemon_reachable"})


class AlternativePathTests(unittest.TestCase):
    def test_stopped_and_disconnected_service_requires_two_mutations(self):
        service = Service(True, "ui-hash", networks=("default",))
        container = Container(
            service="ui", container_id="abc", name="test-ui-1", status="exited",
            running=False, exit_code=0, oom_killed=False, restart_count=0,
            health=None, health_output="", config_hash="ui-hash",
            networks=frozenset(), mounts=frozenset(), published_ports=frozenset(),
        )
        environment = Environment(
            "compose.yaml", "test", {"ui": service},
            BASE | frozenset({
                "container_exists:ui", "config_current:ui", "stopped:ui",
                "network_exists:default",
            }),
            containers={"ui": container},
            networks={"default": Resource("default", "test_default")},
        )

        plan, _ = search(environment)

        self.assertEqual(
            [action.name for action in plan.actions],
            ["Start ui", "Verify ui health", "Connect ui to network default"],
        )
        self.assertEqual(sum(not action.manual for action in plan.actions), 2)

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


class ReadinessStateTests(unittest.TestCase):
    def test_configured_readiness_is_a_goal_and_observation_edge(self):
        service = Service(
            False, "web-hash", readiness=ReadinessProbe("http://localhost:8080/ready"),
        )
        state = BASE | frozenset({
            "container_exists:web", "config_current:web", "running:web",
            "readiness_pending:web",
        })
        environment = Environment("compose.yaml", "test", {"web": service}, state)

        plan, goal = search(environment)

        self.assertIn("endpoint_ready:web", goal)
        self.assertEqual([action.name for action in plan.actions], ["Verify web readiness"])

    def test_failed_observation_exposes_restart_fallback(self):
        service = Service(
            False, "web-hash", readiness=ReadinessProbe("tcp://localhost:8080"),
        )
        state = BASE | frozenset({
            "container_exists:web", "config_current:web", "running:web",
            "readiness_pending:web",
        })
        environment = Environment("compose.yaml", "test", {"web": service}, state)
        first_plan, _ = search(environment)
        rejected = frozenset({(state, first_plan.actions[0].key)})

        fallback, _ = search(environment, rejected)

        self.assertEqual([action.name for action in fallback.actions], ["Restart web for readiness"])
        self.assertIn("endpoint_ready:web", fallback.actions[0].adds)

    def test_readiness_observation_requires_healthy_dependencies(self):
        services = {
            "database": Service(True, "db-hash"),
            "web": Service(
                False, "web-hash", dependencies=(("database", "service_healthy"),),
                readiness=ReadinessProbe("http://localhost:8080/ready"),
            ),
        }
        state = BASE | frozenset({
            "container_exists:web", "config_current:web", "running:web",
            "readiness_pending:web",
        })
        environment = Environment("compose.yaml", "test", services, state)

        verify = next(
            action for action in build_actions(environment)
            if action.key == ("verify_readiness", "web")
        )

        self.assertIn("healthy:database", verify.requires)
        self.assertFalse(verify.is_allowed(state))

    def test_readiness_labels_are_parsed(self):
        probe = _readiness({"labels": {
            "com.dockrepair.readiness.url": "https://localhost:8443/ready",
            "com.dockrepair.readiness.statuses": "200,204,300-301",
            "com.dockrepair.readiness.timeout": "0.5",
        }})

        self.assertEqual(probe.url, "https://localhost:8443/ready")
        self.assertEqual(probe.statuses, frozenset({200, 204, 300, 301}))
        self.assertEqual(probe.timeout, 0.5)

    def test_tcp_readiness_uses_the_configured_timeout(self):
        probe = ReadinessProbe("tcp://localhost:6884", timeout=0.75)

        with patch("dockrepair_docker.socket.create_connection") as connect:
            self.assertTrue(_probe_readiness(probe))

        connect.assert_called_once_with(("localhost", 6884), timeout=0.75)


class ExactPortStateTests(unittest.TestCase):
    def test_inspection_preserves_target_host_port_protocol_and_host_ip(self):
        inspected = {
            "NetworkSettings": {"Ports": {
                "8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8888"}],
            }},
        }

        self.assertEqual(
            _published_ports(inspected),
            frozenset({PublishedPort(8080, 8888, "tcp", "127.0.0.1")}),
        )

    def test_specific_host_binding_requires_the_same_host_and_target(self):
        desired = Port(8080, 8888, "tcp", "127.0.0.1")

        self.assertTrue(_port_matches(desired, PublishedPort(8080, 8888, "tcp", "127.0.0.1")))
        self.assertFalse(_port_matches(desired, PublishedPort(8080, 8888, "tcp", "127.0.0.2")))
        self.assertFalse(_port_matches(desired, PublishedPort(9090, 8888, "tcp", "127.0.0.1")))

    def test_unspecified_host_accepts_docker_wildcard_binding(self):
        desired = Port(8080, 8888)

        self.assertTrue(_port_matches(desired, PublishedPort(8080, 8888, "tcp", "0.0.0.0")))
        self.assertFalse(_port_matches(desired, PublishedPort(8080, 8888, "tcp", "127.0.0.1")))

    def test_conflicts_respect_specific_host_addresses(self):
        first = Port(8080, 8888, "tcp", "127.0.0.1")

        self.assertFalse(_bindings_conflict(first, PublishedPort(8080, 8888, "tcp", "127.0.0.2")))
        self.assertTrue(_bindings_conflict(first, PublishedPort(8080, 8888, "tcp", "0.0.0.0")))


if __name__ == "__main__":
    unittest.main()
