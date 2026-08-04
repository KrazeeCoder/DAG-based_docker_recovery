import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from dockrepair import (
    _effect_was_observed,
    _print_action_certificate,
    _print_resolution_certificate,
    execute_until_resolved,
    print_plan,
)
from dockrepair_data import (
    Action,
    Container,
    Environment,
    Port,
    PublishedPort,
    ReadinessProbe,
    RepairCost,
    Resource,
    Service,
)
from dockrepair_docker import (
    _bindings_conflict,
    _completion_facts,
    _desired_replicas,
    _port_matches,
    _probe_readiness,
    _published_ports,
    _readiness,
    _replica_blockers,
)
from dockrepair_planner import (
    _requirements,
    build_actions,
    build_goal,
    search,
    validate_action_safety,
)


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
            RepairCost(0, 2, 1, 1),
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
        self.assertEqual(plan.total_cost, RepairCost(0, 3, 0, 1))

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


class ComposeCompletionTests(unittest.TestCase):
    @staticmethod
    def container(running, exit_code):
        return Container(
            service="migrate", container_id="job", name="test-migrate-1",
            status="running" if running else "exited", running=running,
            exit_code=exit_code, oom_killed=False, restart_count=0,
            health=None, health_output="", config_hash="job-hash",
            networks=frozenset(), mounts=frozenset(), published_ports=frozenset(),
        )

    def test_dependency_conditions_map_to_distinct_facts(self):
        service = Service(False, "app", dependencies=(
            ("cache", "service_started"),
            ("database", "service_healthy"),
            ("migrate", "service_completed_successfully"),
        ))

        self.assertEqual(_requirements(service), frozenset({
            "running:cache", "healthy:database", "completed_successfully:migrate",
        }))

    def test_unknown_dependency_condition_blocks_search(self):
        service = Service(False, "app", dependencies=(("db", "mystery_condition"),))
        environment = Environment("compose.yaml", "test", {"app": service}, BASE)

        plan, _ = search(environment)

        self.assertEqual(plan.status, "blocked")

    def test_completion_goal_replaces_running_health_and_readiness(self):
        job = Service(
            True, "job-hash", readiness=ReadinessProbe("tcp://localhost:1234"),
            completion_required=True,
        )
        goal = build_goal(Environment("compose.yaml", "test", {"migrate": job}, BASE))

        self.assertIn("completed_successfully:migrate", goal)
        self.assertNotIn("running:migrate", goal)
        self.assertNotIn("healthy:migrate", goal)
        self.assertNotIn("endpoint_ready:migrate", goal)

    def test_completion_runtime_facts_distinguish_pending_success_and_failure(self):
        job = Service(False, "job-hash", completion_required=True)

        self.assertEqual(
            _completion_facts("migrate", job, self.container(True, None)),
            frozenset({"completion_pending:migrate"}),
        )
        self.assertEqual(
            _completion_facts("migrate", job, self.container(False, 0)),
            frozenset({"completed_successfully:migrate"}),
        )
        self.assertEqual(
            _completion_facts("migrate", job, self.container(False, 7)),
            frozenset({"completion_failed:migrate"}),
        )

    def test_completion_job_unlocks_dependent_only_after_observation(self):
        services = {
            "migrate": Service(False, "job-hash", completion_required=True),
            "web": Service(
                False, "web-hash",
                dependencies=(("migrate", "service_completed_successfully"),),
            ),
        }
        environment = Environment("compose.yaml", "test", services, BASE)

        plan, _ = search(environment)

        self.assertEqual([action.name for action in plan.actions], [
            "Run completion job migrate",
            "Verify migrate completed successfully",
            "Reconcile web",
        ])

    def test_completion_job_is_not_in_batch_reconciliation(self):
        services = {
            "migrate": Service(False, "job-hash", completion_required=True),
            "api": Service(
                False, "api-hash",
                dependencies=(("migrate", "service_completed_successfully"),),
            ),
            "worker": Service(False, "worker-hash"),
        }
        environment = Environment("compose.yaml", "test", services, BASE)

        plan, _ = search(environment)

        self.assertEqual([action.name for action in plan.actions], [
            "Run completion job migrate",
            "Verify migrate completed successfully",
            "Reconcile services together: api, worker",
        ])

    def test_fast_completion_satisfies_pending_and_running_predictions(self):
        action = Action(
            "Run completion job migrate", ("up", "-d", "migrate"),
            RepairCost(0, 1, 0, 1), BASE,
            frozenset({
                "container_exists:migrate", "config_current:migrate",
                "running:migrate", "completion_pending:migrate",
            }),
            removes=frozenset({"completed_successfully:migrate"}),
            identity=("run_completion", "migrate"),
        )
        observed = Environment(
            "compose.yaml", "test", {},
            BASE | frozenset({
                "container_exists:migrate", "config_current:migrate",
                "stopped:migrate", "completed_successfully:migrate",
            }),
        )

        self.assertTrue(_effect_was_observed(action, observed))


class ReplicaSafetyTests(unittest.TestCase):
    def test_scale_and_deploy_replicas_are_parsed(self):
        self.assertEqual(_desired_replicas({"scale": 3}), 3)
        self.assertEqual(_desired_replicas({"deploy": {"replicas": 2}}), 2)
        self.assertEqual(_desired_replicas({}), 1)

    def test_declared_and_runtime_replicas_are_blocked(self):
        declared = {"web": Service(False, "web", desired_replicas=2)}
        runtime = {"web": Service(False, "web")}

        self.assertIn("declares 2 replicas", _replica_blockers(declared)[0])
        self.assertIn("has 2 containers", _replica_blockers(runtime, {"web": [{}, {}]})[0])


class SafetyCostTests(unittest.TestCase):
    def test_costs_are_componentwise_and_lexicographic(self):
        safe_long = RepairCost(0, 5, 5, 10)
        risky_short = RepairCost(1, 0, 0, 1)

        self.assertLess(safe_long, risky_short)
        self.assertEqual(
            RepairCost(0, 1, 0, 1) + RepairCost(1, 3, 1, 1),
            RepairCost(1, 4, 1, 2),
        )

    def test_safety_validator_denies_unknown_and_foreign_actions(self):
        environment = Environment(
            "compose.yaml", "test", {"web": Service(False, "web")}, BASE,
            networks={"shared": Resource("shared", "shared", external=True)},
        )
        unknown = Action(
            "Delete everything", ("down",), RepairCost(), BASE, frozenset(),
            identity=("delete_everything",),
        )
        foreign = Action(
            "Start foreign", ("start", "foreign"), RepairCost(), BASE,
            frozenset(), identity=("start", "foreign"),
        )
        disguised = Action(
            "Start web", ("start", "foreign"), RepairCost(), BASE,
            frozenset(), identity=("start", "web"),
        )
        external = Action(
            "Create shared", ("network", "create", "shared"), RepairCost(),
            BASE, frozenset(), identity=("create_network", "shared"),
            executor="docker",
        )

        self.assertFalse(validate_action_safety(environment, unknown)[0])
        self.assertFalse(validate_action_safety(environment, foreign)[0])
        self.assertFalse(validate_action_safety(environment, disguised)[0])
        self.assertFalse(validate_action_safety(environment, external)[0])

    def test_generated_actions_include_safety_evidence(self):
        service = Service(False, "web")
        state = BASE | frozenset({"container_exists:web", "config_current:web", "stopped:web"})
        environment = Environment("compose.yaml", "test", {"web": service}, state)

        actions = build_actions(environment)

        self.assertTrue(actions)
        self.assertTrue(all(action.safety_checks for action in actions))


class CertificateTests(unittest.TestCase):
    def test_planning_and_action_certificates_are_terminal_text(self):
        service = Service(False, "web")
        state = BASE | frozenset({"container_exists:web", "config_current:web", "stopped:web"})
        environment = Environment("compose.yaml", "test", {"web": service}, state)
        plan, goal = search(environment)
        output = StringIO()

        with redirect_stdout(output):
            print_plan(environment, plan, goal)
            _print_action_certificate(plan.actions[0], state)

        text = output.getvalue()
        self.assertIn("PLAN CERTIFICATE", text)
        self.assertIn("Objective order: data-risk -> destructiveness -> disruption -> actions", text)
        self.assertIn("requires:", text)
        self.assertIn("safety:", text)
        self.assertIn("ACTION CERTIFICATE", text)

    def test_execution_prints_action_and_verified_resolution_certificates(self):
        service = Service(False, "web")
        initial_state = BASE | frozenset({
            "container_exists:web", "config_current:web", "stopped:web",
        })
        final_state = BASE | frozenset({
            "container_exists:web", "config_current:web", "running:web",
        })
        initial = Environment("compose.yaml", "test", {"web": service}, initial_state)
        resolved = Environment("compose.yaml", "test", {"web": service}, final_state)
        output = StringIO()

        with (
            patch("dockrepair.collect_environment", return_value=initial),
            patch("dockrepair.execute_action", return_value=(True, "", resolved)),
            redirect_stdout(output),
        ):
            return_code = execute_until_resolved("compose.yaml")

        text = output.getvalue()
        self.assertEqual(return_code, 0)
        self.assertIn("ACTION CERTIFICATE", text)
        self.assertIn("RESOLUTION CERTIFICATE", text)
        self.assertIn("Status: VERIFIED", text)
        self.assertIn("Accumulated cost: (data-risk=0, destructiveness=1", text)

    def test_resolution_certificate_reports_verified_and_incomplete_states(self):
        service = Service(False, "web")
        goal = BASE | frozenset({"container_exists:web", "config_current:web", "running:web"})
        resolved = Environment("compose.yaml", "test", {"web": service}, goal)
        broken = Environment("compose.yaml", "test", {"web": service}, BASE)
        output = StringIO()

        with redirect_stdout(output):
            _print_resolution_certificate(
                resolved, goal, [(('start', 'web'), True)],
                RepairCost(0, 1, 0, 1), True,
            )
            _print_resolution_certificate(
                broken, goal, [], RepairCost(), False, "blocked",
            )

        text = output.getvalue()
        self.assertIn("Status: VERIFIED", text)
        self.assertIn("Status: INCOMPLETE", text)
        self.assertIn("container_exists:web", text)


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
