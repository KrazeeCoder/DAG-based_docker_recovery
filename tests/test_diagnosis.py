import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from dockrepair_data import (
    Container,
    DependencyContract,
    Diagnosis,
    Environment,
    IncidentReport,
    ProbeObservation,
    Service,
    Resource,
)
from dockrepair_diagnosis import (
    contract_fact,
    diagnose_contract,
    select_minimal_repair,
    write_report,
)
from dockrepair_docker import _dependency_contracts


BASE = frozenset({"compose_valid", "daemon_reachable"})


def container(name, *, running=True, networks=frozenset({"default"})):
    return Container(
        name, f"{name}-id", f"test-{name}-1", "running" if running else "exited",
        running, 0 if not running else None, False, 0, None, "", f"{name}-hash",
        networks, frozenset(), frozenset(),
    )


def environment(*, caller_networks=frozenset({"default"}), allow_recreate=False):
    services = {
        "api": Service(False, "api-hash", networks=("default",)),
        "database": Service(
            False, "database-hash", networks=("default",), allow_recreate=allow_recreate,
        ),
    }
    contract = DependencyContract("primary-db", "api", "database", 5432, 2, ("default",))
    containers = {
        "api": container("api", networks=caller_networks),
        "database": container("database"),
    }
    facts = BASE | frozenset({
        "container_exists:api", "config_current:api", "running:api",
        "container_exists:database", "config_current:database", "running:database",
        "network_exists:default", "network_connected:database:default",
    })
    if "default" in caller_networks:
        facts |= {"network_connected:api:default"}
    return Environment(
        "compose.yaml", "test", services, frozenset(facts), containers=containers,
        contracts=(contract,),
    ), contract


class ContractParsingTests(unittest.TestCase):
    def test_parameterized_tcp_contract_is_parsed(self):
        raw = {
            "api": {
                "networks": {"backend": {}},
                "labels": {"com.dockrepair.contract.primary-db": "tcp://database:5432"},
            },
            "database": {"networks": {"backend": {}}},
        }
        services = {
            "api": Service(False, "api", networks=("backend",)),
            "database": Service(False, "database", networks=("backend",)),
        }

        contracts, errors = _dependency_contracts(raw, services)

        self.assertEqual(errors, ())
        self.assertEqual(
            contracts,
            (DependencyContract("primary-db", "api", "database", 5432, 2, ("backend",)),),
        )

    def test_invalid_or_undeclared_targets_are_rejected(self):
        raw = {
            "api": {"labels": {
                "com.dockrepair.contract.bad-http": "http://database:80",
                "com.dockrepair.contract.ghost": "tcp://ghost:5432",
            }},
        }
        services = {"api": Service(False, "api", networks=("default",))}

        contracts, errors = _dependency_contracts(raw, services)

        self.assertEqual(contracts, ())
        self.assertEqual(len(errors), 2)


class ActiveDiagnosisTests(unittest.TestCase):
    @staticmethod
    def observation(contract, probe, outcome):
        return ProbeObservation(
            contract.key, probe, outcome,
            frozenset({contract_fact(outcome, contract)}), outcome,
        )

    def test_active_probes_isolate_closed_target_listener(self):
        env, contract = environment()
        outcomes = {
            "tcp": self.observation(contract, "tcp", "tcp_unreachable"),
            "dns": self.observation(contract, "dns", "dns_resolved"),
            "listener": self.observation(contract, "listener", "listener_closed"),
        }
        called = []

        def fake_probe(_environment, _contract, probe, _image, _timeout):
            called.append(probe)
            return outcomes[probe]

        with patch("dockrepair_diagnosis.probe_image_available", return_value=(True, "image")), patch(
            "dockrepair_diagnosis.run_probe", side_effect=fake_probe,
        ):
            diagnosis, probes, facts = diagnose_contract(env, contract)

        self.assertEqual(called, ["tcp", "dns", "listener"])
        self.assertEqual(diagnosis.code, "TARGET_NOT_LISTENING")
        self.assertTrue(diagnosis.repairable)
        self.assertIn(contract_fact("listener_closed", contract), facts)
        self.assertEqual(len(probes), 3)

    def test_reachable_tcp_contract_is_verified_in_one_probe(self):
        env, contract = environment()
        observation = self.observation(contract, "tcp", "tcp_reachable")
        with patch("dockrepair_diagnosis.probe_image_available", return_value=(True, "image")), patch(
            "dockrepair_diagnosis.run_probe", return_value=observation,
        ):
            diagnosis, probes, facts = diagnose_contract(env, contract)

        self.assertEqual(diagnosis.code, "CONTRACT_HEALTHY")
        self.assertEqual([item.probe for item in probes], ["tcp"])
        self.assertIn(contract_fact("contract_satisfied", contract), facts)

    def test_declared_network_drift_is_proven_without_a_probe(self):
        env, contract = environment(caller_networks=frozenset())

        diagnosis, probes, _ = diagnose_contract(env, contract)

        self.assertEqual(diagnosis.code, "CALLER_NETWORK_DRIFT")
        self.assertEqual(diagnosis.certainty, "proven")
        self.assertEqual(probes, ())

    def test_oom_kill_is_reported_as_objective_evidence(self):
        env, contract = environment()
        failed = replace(
            env.containers["database"], running=False, status="exited",
            exit_code=137, oom_killed=True,
        )
        env = replace(
            env,
            containers={**env.containers, "database": failed},
            facts=(env.facts - {"running:database"}) | {"stopped:database", "oom_killed:database"},
        )

        diagnosis, probes, _ = diagnose_contract(env, contract)

        self.assertEqual(diagnosis.code, "TARGET_OOM_KILLED")
        self.assertIn("exit_code=137", diagnosis.evidence[0])
        self.assertEqual(probes, ())

    def test_working_tcp_with_failed_caller_health_abstains_at_semantic_boundary(self):
        env, contract = environment()
        env = replace(
            env,
            services={**env.services, "api": replace(env.services["api"], needs_healthcheck=True)},
        )
        observation = self.observation(contract, "tcp", "tcp_reachable")
        with patch("dockrepair_diagnosis.probe_image_available", return_value=(True, "image")), patch(
            "dockrepair_diagnosis.run_probe", return_value=observation,
        ):
            diagnosis, _, _ = diagnose_contract(env, contract)

        self.assertEqual(diagnosis.code, "APPLICATION_OR_UNKNOWN")
        self.assertEqual(diagnosis.certainty, "ambiguous")
        self.assertFalse(diagnosis.repairable)

    def test_listener_recreate_requires_explicit_opt_in(self):
        diagnosis = None
        for allowed in (False, True):
            env, contract = environment(allow_recreate=allowed)
            diagnosis = type("D", (), {
                "contract_key": contract.key,
                "code": "TARGET_NOT_LISTENING",
                "locus": contract.target,
            })()
            action = select_minimal_repair(env, (diagnosis,), {("restart", "database")})
            if allowed:
                self.assertEqual(action.key, ("recreate", "database"))
            else:
                self.assertIsNone(action)

    def test_missing_declared_network_is_created_before_attachment(self):
        env, contract = environment(caller_networks=frozenset())
        env = replace(
            env,
            facts=frozenset(item for item in env.facts if item != "network_exists:default"),
            networks={"default": Resource("default", "test_default")},
        )
        diagnosis = Diagnosis(
            contract.key, "CALLER_NETWORK_DRIFT", "proven", "api", True,
        )

        action = select_minimal_repair(env, (diagnosis,), set())

        self.assertEqual(action.key, ("create_network", "default"))


class ReportTests(unittest.TestCase):
    def test_report_is_machine_readable_json(self):
        report = IncidentReport("RESTORED", "test", verified_contracts=("api:db",))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "incident.json"
            write_report(report, target)
            data = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(data["status"], "RESTORED")
        self.assertEqual(data["verified_contracts"], ["api:db"])


if __name__ == "__main__":
    unittest.main()
