import json
import tempfile
import unittest
from pathlib import Path

from dockrepair_data import (
    Container,
    DependencyContract,
    Diagnosis,
    Environment,
    IncidentReport,
    Service,
)
from dockrepair_diagnosis import select_graph_repair, write_report
from dockrepair_graph import build_graph_analysis, finalize_intervention


def graph_environment(edge_specs):
    """Build a fully running symbolic environment from caller/target/id triples."""

    names = sorted({name for caller, target, _ in edge_specs for name in (caller, target)})
    services = {
        name: Service(False, f"{name}-hash", networks=("default",))
        for name in names
    }
    containers = {
        name: Container(
            name, f"{name}-id", f"test-{name}-1", "running", True, None,
            False, 0, None, "", f"{name}-hash", frozenset({"default"}),
            frozenset(), frozenset(),
        )
        for name in names
    }
    facts = {"compose_valid", "daemon_reachable", "network_exists:default"}
    for name in names:
        facts.update({
            f"container_exists:{name}", f"config_current:{name}",
            f"running:{name}", f"network_connected:{name}:default",
        })
    contracts = tuple(
        DependencyContract(identifier, caller, target, 1000, 2, ("default",))
        for caller, target, identifier in edge_specs
    )
    return Environment(
        "compose.yaml", "graph-test", services, frozenset(facts),
        containers=containers, contracts=contracts,
    )


def diagnosis(contract, *, code="TARGET_NOT_LISTENING", locus=None,
              repairable=True, certainty="proven"):
    return Diagnosis(
        contract.key, code, certainty, locus or contract.target, repairable,
        (f"synthetic evidence for {contract.key}",),
    )


class GraphAnalysisTests(unittest.TestCase):
    def test_three_service_chain_selects_only_deepest_service(self):
        environment = graph_environment((
            ("website", "api", "api"),
            ("api", "database", "database"),
        ))
        diagnoses = tuple(diagnosis(contract) for contract in environment.contracts)

        analysis = build_graph_analysis(environment, diagnoses)
        candidate = select_graph_repair(environment, diagnoses, set(), analysis)

        self.assertEqual(analysis.groups[0].deepest_contracts, ("api:database",))
        self.assertEqual(analysis.groups[0].upstream_symptoms, ("website:api",))
        self.assertEqual(candidate.action.key, ("restart", "database"))
        self.assertEqual(candidate.expected_contracts, ("api:database", "website:api"))

    def test_multiple_callers_share_one_deepest_repair(self):
        environment = graph_environment((
            ("api", "database", "db"),
            ("worker", "database", "db"),
        ))
        diagnoses = tuple(diagnosis(contract) for contract in environment.contracts)

        candidate = select_graph_repair(environment, diagnoses, set())

        self.assertEqual(candidate.action.key, ("restart", "database"))
        self.assertEqual(candidate.seed_contracts, ("api:db", "worker:db"))

    def test_diamond_propagates_expected_coverage_to_both_branches(self):
        environment = graph_environment((
            ("website", "api", "api"),
            ("website", "worker", "worker"),
            ("api", "database", "db"),
            ("worker", "database", "db"),
        ))
        diagnoses = tuple(diagnosis(contract) for contract in environment.contracts)

        candidate = select_graph_repair(environment, diagnoses, set())

        self.assertEqual(candidate.action.key, ("restart", "database"))
        self.assertEqual(set(candidate.expected_contracts), {
            "api:db", "worker:db", "website:api", "website:worker",
        })

    def test_unrelated_failures_form_separate_cascade_groups(self):
        environment = graph_environment((
            ("api", "database", "db"),
            ("mailer", "queue", "queue"),
        ))
        diagnoses = tuple(diagnosis(contract) for contract in environment.contracts)

        analysis = build_graph_analysis(environment, diagnoses)

        self.assertEqual(len(analysis.groups), 2)
        self.assertEqual(
            {group.contract_keys for group in analysis.groups},
            {("api:db",), ("mailer:queue",)},
        )

    def test_nonrepairable_deep_fault_suppresses_upstream_restart(self):
        environment = graph_environment((
            ("website", "api", "api"),
            ("api", "database", "db"),
        ))
        website_api, api_db = environment.contracts
        diagnoses = (
            diagnosis(website_api),
            diagnosis(
                api_db, code="DNS_FAILURE", repairable=False, certainty="localized",
            ),
        )

        analysis = build_graph_analysis(environment, diagnoses)
        candidate = select_graph_repair(environment, diagnoses, set(), analysis)

        self.assertIsNone(candidate)
        self.assertEqual(analysis.groups[0].status, "blocked_by_deeper_failure")

    def test_cycle_with_one_proven_repair_is_actionable_but_unconfirmed(self):
        environment = graph_environment((
            ("api", "worker", "worker"),
            ("worker", "api", "api"),
        ))
        api_worker, worker_api = environment.contracts
        diagnoses = (
            diagnosis(api_worker),
            diagnosis(
                worker_api, code="APPLICATION_OR_UNKNOWN", locus="worker",
                repairable=False, certainty="ambiguous",
            ),
        )

        analysis = build_graph_analysis(environment, diagnoses)
        candidate = select_graph_repair(environment, diagnoses, set(), analysis)

        self.assertTrue(analysis.groups[0].cyclic)
        self.assertEqual(candidate.action.key, ("restart", "worker"))
        self.assertTrue(candidate.cyclic)

    def test_cycle_with_multiple_plausible_services_abstains(self):
        environment = graph_environment((
            ("api", "worker", "worker"),
            ("worker", "api", "api"),
        ))
        diagnoses = tuple(diagnosis(contract) for contract in environment.contracts)

        analysis = build_graph_analysis(environment, diagnoses)

        self.assertEqual(analysis.groups[0].status, "cyclic_ambiguous")
        self.assertIsNone(select_graph_repair(environment, diagnoses, set(), analysis))

    def test_selection_is_stable_across_contract_declaration_order(self):
        specifications = (
            ("api", "database", "db"),
            ("mailer", "queue", "queue"),
        )
        selected = []
        for edge_specs in (specifications, tuple(reversed(specifications))):
            environment = graph_environment(edge_specs)
            diagnoses = tuple(diagnosis(contract) for contract in environment.contracts)
            selected.append(select_graph_repair(environment, diagnoses, set()).action.key)

        self.assertEqual(selected[0], selected[1])


class InterventionTests(unittest.TestCase):
    def setUp(self):
        self.environment = graph_environment((
            ("website", "api", "api"),
            ("api", "database", "database"),
        ))
        self.before_diagnoses = tuple(
            diagnosis(contract) for contract in self.environment.contracts
        )
        self.before = build_graph_analysis(self.environment, self.before_diagnoses)
        self.group = self.before.groups[0]
        self.candidate = select_graph_repair(
            self.environment, self.before_diagnoses, set(), self.before,
        )

    def intervention(self, after_diagnoses):
        return finalize_intervention(
            action=self.candidate.action,
            service=self.candidate.service,
            group=self.group,
            seed_contracts=self.candidate.seed_contracts,
            expected_contracts=self.candidate.expected_contracts,
            before_analysis=self.before,
            after_analysis=build_graph_analysis(self.environment, after_diagnoses),
            mutated_services={"database"},
        )

    def test_deep_repair_confirms_direct_and_indirect_recovery(self):
        after = tuple(
            diagnosis(
                contract, code="CONTRACT_HEALTHY", repairable=False,
            )
            for contract in self.environment.contracts
        )

        record = self.intervention(after)

        self.assertEqual(record.directly_restored, ("api:database",))
        self.assertEqual(record.indirectly_restored, ("website:api",))
        self.assertEqual(record.causal_status, "supported")

    def test_direct_only_recovery_rejects_upstream_part_of_hypothesis(self):
        website_api, api_database = self.environment.contracts
        after = (
            diagnosis(website_api),
            diagnosis(api_database, code="CONTRACT_HEALTHY", repairable=False),
        )

        after_analysis = build_graph_analysis(self.environment, after)
        record = self.intervention(after)

        self.assertEqual(record.directly_restored, ("api:database",))
        self.assertEqual(record.indirectly_restored, ())
        self.assertEqual(record.still_failed, ("website:api",))
        self.assertEqual(record.causal_status, "direct_only")
        remaining = select_graph_repair(
            self.environment, after, {("restart", "database")}, after_analysis,
        )
        self.assertEqual(remaining.action.key, ("restart", "api"))

    def test_graph_report_serializes_explanations_and_intervention(self):
        healthy = tuple(
            diagnosis(contract, code="CONTRACT_HEALTHY", repairable=False)
            for contract in self.environment.contracts
        )
        record = self.intervention(healthy)
        report = IncidentReport(
            "RESTORED", "graph-test", graph=self.before,
            interventions=(record,), observed_explanations=self.before.observed,
            inferred_explanations=self.before.inferred,
            confirmed_explanations=(record.conclusion,),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            write_report(report, path)
            data = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(data["graph"]["groups"][0]["deepest_contracts"], ["api:database"])
        self.assertEqual(data["interventions"][0]["causal_status"], "supported")
        self.assertTrue(data["confirmed_explanations"])


if __name__ == "__main__":
    unittest.main()
