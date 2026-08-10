from __future__ import annotations
import json
import math
import subprocess
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from dockrepair_data import (
    Action,
    DependencyContract,
    Diagnosis,
    IncidentReport,
    ProbeObservation,
    RepairCost,
)
from dockrepair_docker import collect_environment
from dockrepair_graph import (
    build_graph_analysis,
    expected_upstream_contracts,
    finalize_intervention,
)
from dockrepair_planner import BASE, build_actions, build_goal, fact, validate_action_safety


HYPOTHESES = frozenset({
    "DNS_FAILURE",
    "TARGET_NOT_LISTENING",
    "NETWORK_PATH_FAILURE",
    "APPLICATION_OR_UNKNOWN",
})

PROBE_PARTITIONS = {
    "tcp": {
        "tcp_reachable": frozenset({"APPLICATION_OR_UNKNOWN"}),
        "tcp_unreachable": frozenset({
            "DNS_FAILURE", "TARGET_NOT_LISTENING", "NETWORK_PATH_FAILURE",
        }),
    },
    "dns": {
        "dns_resolved": frozenset({
            "TARGET_NOT_LISTENING", "NETWORK_PATH_FAILURE", "APPLICATION_OR_UNKNOWN",
        }),
        "dns_unresolved": frozenset({"DNS_FAILURE"}),
    },
    "listener": {
        "listener_open": frozenset({
            "DNS_FAILURE", "NETWORK_PATH_FAILURE", "APPLICATION_OR_UNKNOWN",
        }),
        "listener_closed": frozenset({"TARGET_NOT_LISTENING"}),
    },
}

PROBE_COST = {"tcp": 1, "dns": 2, "listener": 3}


def contract_fact(kind, contract, suffix=""):
    value = f"{kind}:{contract.key}"
    return f"{value}:{suffix}" if suffix else value


def _run(arguments, cwd, timeout):
    try:
        result = subprocess.run(
            arguments,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return None, str(error)
    output = (result.stdout if result.returncode == 0 else result.stderr or result.stdout).strip()
    return result.returncode, output


def _probe_arguments(image, container_id, command):
    return [
        "docker", "run", "--rm",
        "--network", f"container:{container_id}",
        "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "32", "--memory", "32m", "--cpus", "0.25",
        image, *command,
    ]


def probe_image_available(image, cwd):
    code, output = _run(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"], cwd, 10,
    )
    return code == 0, output


def _observation(contract, probe, outcome, evidence):
    return ProbeObservation(
        contract.key,
        probe,
        outcome,
        frozenset({contract_fact(outcome, contract)}),
        evidence,
    )


def run_probe(environment, contract, probe, image="busybox:1.36.1", timeout=None):
    """Run one read-only probe; a negative result is a successful observation."""

    timeout = contract.timeout if timeout is None else float(timeout)
    if not math.isfinite(timeout):
        return _observation(contract, probe, "probe_error", "probe timeout must be finite")
    timeout = max(0.1, timeout)
    caller = (environment.containers or {}).get(contract.caller)
    target = (environment.containers or {}).get(contract.target)
    subject = caller if probe in {"dns", "tcp"} else target
    if not subject or not subject.running:
        return _observation(contract, probe, "probe_error", "probe subject is unavailable")

    if probe == "dns":
        command = ["nslookup", contract.target]
    elif probe == "tcp":
        command = ["nc", "-z", "-w", str(max(1, int(timeout + 0.999))), contract.target, str(contract.port)]
    elif probe == "listener":
        wait = str(max(1, int(timeout + 0.999)))
        command = [
            "sh", "-c",
            'for ip in 127.0.0.1 $(hostname -i); do '
            f'nc -z -w {wait} "$ip" {contract.port} && exit 0; '
            "done; exit 1",
        ]
    else:
        return _observation(contract, probe, "probe_error", f"unknown probe '{probe}'")

    code, output = _run(
        _probe_arguments(image, subject.container_id, command),
        Path(environment.compose_file).parent,
        timeout + 10,
    )
    evidence = output or f"{probe} command exited with code {code}"
    if code is None or code in {125, 126, 127}:
        return _observation(contract, probe, "probe_error", evidence)
    if probe == "dns":
        outcome = "dns_resolved" if code == 0 else "dns_unresolved"
    elif probe == "tcp":
        outcome = "tcp_reachable" if code == 0 else "tcp_unreachable"
    else:
        outcome = "listener_open" if code == 0 else "listener_closed"
    return _observation(contract, probe, outcome, evidence)


def _direct_diagnosis(environment, contract):
    caller = (environment.containers or {}).get(contract.caller)
    target = (environment.containers or {}).get(contract.target)
    if not caller or not caller.running:
        return Diagnosis(
            contract.key, "CALLER_UNAVAILABLE", "proven", contract.caller, True,
            ("caller container is missing or stopped",),
        )
    if not target or not target.running:
        detail = "target container is missing or stopped"
        if target and target.oom_killed:
            detail += f"; OOMKilled=true; exit_code={target.exit_code}"
            return Diagnosis(
                contract.key, "TARGET_OOM_KILLED", "proven", contract.target, True, (detail,),
            )
        return Diagnosis(
            contract.key, "TARGET_UNAVAILABLE", "proven", contract.target, True, (detail,),
        )
    if fact("config_current", contract.target) not in environment.facts:
        return Diagnosis(
            contract.key, "TARGET_CONFIG_DRIFT", "proven", contract.target, True,
            ("target container configuration differs from normalized Compose",),
        )
    for network in contract.shared_networks:
        if network not in caller.networks:
            return Diagnosis(
                contract.key, "CALLER_NETWORK_DRIFT", "proven", contract.caller, True,
                (f"caller is missing declared network '{network}'",),
            )
        if network not in target.networks:
            return Diagnosis(
                contract.key, "TARGET_NETWORK_DRIFT", "proven", contract.target, True,
                (f"target is missing declared network '{network}'",),
            )
    return None


def _choose_probe(hypotheses, completed):
    candidates = []
    for probe, outcomes in PROBE_PARTITIONS.items():
        if probe in completed:
            continue
        partitions = [hypotheses & possible for possible in outcomes.values()]
        useful = [part for part in partitions if part and part != hypotheses]
        if not useful:
            continue
        worst = max(len(part) for part in partitions if part)
        candidates.append((worst, PROBE_COST[probe], probe))
    return min(candidates)[2] if candidates else None


def _diagnosis_from_hypothesis(environment, contract, code, evidence):
    target_codes = {"DNS_FAILURE", "TARGET_NOT_LISTENING", "NETWORK_PATH_FAILURE"}
    locus = contract.target if code in target_codes else contract.caller
    repairable = code == "TARGET_NOT_LISTENING"
    certainty = "proven" if code in {"TARGET_NOT_LISTENING"} else "localized"
    if code == "APPLICATION_OR_UNKNOWN":
        certainty = "ambiguous"
    return Diagnosis(contract.key, code, certainty, locus, repairable, tuple(evidence))


def diagnose_contract(environment, contract, image="busybox:1.36.1", timeout=None):
    direct = _direct_diagnosis(environment, contract)
    if direct:
        return direct, (), frozenset()

    available, image_evidence = probe_image_available(
        image, Path(environment.compose_file).parent,
    )
    if not available:
        diagnosis = Diagnosis(
            contract.key, "PROBE_UNAVAILABLE", "ambiguous", contract.caller, False,
            (f"probe image '{image}' is not locally available", image_evidence),
        )
        return diagnosis, (), frozenset()

    hypotheses = HYPOTHESES
    completed = set()
    observations = []
    evidence = []
    observed_facts = set()
    while len(hypotheses) > 1:
        probe = _choose_probe(hypotheses, completed)
        if not probe:
            break
        observation = run_probe(environment, contract, probe, image, timeout)
        observations.append(observation)
        completed.add(probe)
        observed_facts.update(observation.facts)
        evidence.append(f"{probe}: {observation.outcome} ({observation.evidence})")
        if observation.outcome == "probe_error":
            continue
        possible = PROBE_PARTITIONS[probe].get(observation.outcome)
        if possible:
            hypotheses &= possible

    if len(hypotheses) == 1:
        code = next(iter(hypotheses))
        if code == "APPLICATION_OR_UNKNOWN" and any(
            observation.outcome == "tcp_reachable" for observation in observations
        ):
            caller_service = environment.services[contract.caller]
            readiness_ok = (
                not caller_service.readiness
                or fact("endpoint_ready", contract.caller) in environment.facts
            )
            health_ok = (
                not caller_service.needs_healthcheck
                or fact("healthy", contract.caller) in environment.facts
            )
            if readiness_ok and health_ok:
                code = "CONTRACT_HEALTHY"
                observed_facts.add(contract_fact("contract_satisfied", contract))
                diagnosis = Diagnosis(
                    contract.key, code, "proven", contract.target, False, tuple(evidence),
                )
                return diagnosis, tuple(observations), frozenset(observed_facts)
        return (
            _diagnosis_from_hypothesis(environment, contract, code, evidence),
            tuple(observations),
            frozenset(observed_facts),
        )

    diagnosis = Diagnosis(
        contract.key, "AMBIGUOUS_DEPENDENCY_FAILURE", "ambiguous", contract.caller, False,
        tuple(evidence) or ("available probes could not distinguish the remaining hypotheses",),
    )
    return diagnosis, tuple(observations), frozenset(observed_facts)


def diagnose_environment(environment, image="busybox:1.36.1", timeout=None):
    diagnoses = []
    observations = []
    facts = set()
    for contract in environment.contracts:
        diagnosis, probes, observed = diagnose_contract(environment, contract, image, timeout)
        diagnoses.append(diagnosis)
        observations.extend(probes)
        facts.update(observed)
    return tuple(diagnoses), tuple(observations), frozenset(facts)


def _catalog_action(environment, service_name, kinds, attempted=frozenset()):
    for action in build_actions(environment):
        if action.key[:1] not in {(kind,) for kind in kinds} or service_name not in action.key[1:]:
            continue
        if action.key in attempted:
            continue
        if not action.is_allowed(environment.facts):
            continue
        if action.key[:1] == ("recreate",) and not environment.services[service_name].allow_recreate:
            continue
        return action
    return None


def _constructed_action(environment, service_name, kind):
    if kind == "restart":
        action = Action(
            f"Restart {service_name} after listener diagnosis",
            ("restart", service_name),
            RepairCost(0, 2, 1, 1),
            BASE | {fact("container_exists", service_name), fact("running", service_name)},
            frozenset({fact("running", service_name)}),
            identity=("restart", service_name),
        )
    else:
        action = Action(
            f"Recreate {service_name} after persistent listener failure",
            ("up", "-d", "--force-recreate", "--no-deps", service_name),
            RepairCost(1, 3, 1, 1),
            BASE | {fact("container_exists", service_name), fact("running", service_name)},
            frozenset({fact("container_exists", service_name), fact("running", service_name)}),
            identity=("recreate", service_name),
        )
    safe, checks = validate_action_safety(environment, action)
    if not safe:
        return None
    return Action(
        action.name, action.arguments, action.cost, action.requires, action.adds,
        action.manual, action.removes, action.identity, action.executor, checks,
    )


def _action_for_diagnosis(environment, diagnosis, attempted):
    contract = next(
        item for item in environment.contracts if item.key == diagnosis.contract_key
    )
    if diagnosis.code in {
        "CALLER_UNAVAILABLE", "TARGET_UNAVAILABLE", "TARGET_CONFIG_DRIFT", "TARGET_OOM_KILLED",
    }:
        return _catalog_action(
            environment,
            diagnosis.locus,
            ("start", "reconcile", "recreate"),
            attempted,
        )
    if diagnosis.code in {"CALLER_NETWORK_DRIFT", "TARGET_NETWORK_DRIFT"}:
        for network in contract.shared_networks:
            if f"network_exists:{network}" in environment.facts:
                continue
            for candidate in build_actions(environment):
                if (
                    candidate.key == ("create_network", network)
                    and candidate.key not in attempted
                    and candidate.is_allowed(environment.facts)
                ):
                    return candidate
        return _catalog_action(
            environment, diagnosis.locus, ("connect_network",), attempted,
        )
    if diagnosis.code == "TARGET_NOT_LISTENING":
        restart_key = ("restart", contract.target)
        recreate_key = ("recreate", contract.target)
        if restart_key not in attempted:
            return _constructed_action(environment, contract.target, "restart")
        if environment.services[contract.target].allow_recreate and recreate_key not in attempted:
            return _constructed_action(environment, contract.target, "recreate")
    return None


@dataclass(frozen=True)
class GraphRepairCandidate:
    action: Action
    cascade_id: str
    service: str
    seed_contracts: tuple[str, ...]
    expected_contracts: tuple[str, ...]
    graph_depth: int
    cyclic: bool


def _upstream_depth(analysis, group, seeds):
    edges = {
        edge.contract_key: edge for edge in analysis.edges
        if edge.contract_key in group.contract_keys and edge.status == "failed"
    }
    distances = {key: 0 for key in seeds}
    frontier = list(sorted(seeds))
    while frontier:
        child_key = frontier.pop(0)
        child = edges.get(child_key)
        if child is None:
            continue
        for key, edge in sorted(edges.items()):
            if key not in distances and edge.target == child.caller:
                distances[key] = distances[child_key] + 1
                frontier.append(key)
    return max(distances.values(), default=0)


def graph_repair_candidates(environment, diagnoses, attempted, analysis=None):
    """Generate safe candidates only from deepest failed graph regions."""

    analysis = analysis or build_graph_analysis(environment, diagnoses)
    diagnosis_by_key = {item.contract_key: item for item in diagnoses}
    candidates = []
    for group in analysis.groups:
        eligible_keys = group.deepest_contracts
        if group.cyclic:
            proven_services = {
                diagnosis_by_key[key].locus
                for key in eligible_keys
                if key in diagnosis_by_key
                and getattr(diagnosis_by_key[key], "repairable", True)
                and getattr(diagnosis_by_key[key], "certainty", "proven") == "proven"
            }
            if len(proven_services) != 1:
                continue
            eligible_keys = tuple(
                key for key in eligible_keys
                if diagnosis_by_key.get(key)
                and diagnosis_by_key[key].locus in proven_services
                and getattr(diagnosis_by_key[key], "repairable", True)
                and getattr(diagnosis_by_key[key], "certainty", "proven") == "proven"
            )

        by_action = {}
        for key in eligible_keys:
            diagnosis = diagnosis_by_key.get(key)
            if not diagnosis or not getattr(diagnosis, "repairable", True):
                continue
            action = _action_for_diagnosis(environment, diagnosis, attempted)
            if action is None:
                continue
            stored = by_action.setdefault(
                action.key,
                {"action": action, "service": diagnosis.locus, "seeds": set()},
            )
            stored["seeds"].add(key)

        for stored in by_action.values():
            seeds = tuple(sorted(stored["seeds"]))
            expected = expected_upstream_contracts(analysis, group, seeds)
            candidates.append(GraphRepairCandidate(
                stored["action"],
                group.identifier,
                stored["service"],
                seeds,
                expected,
                _upstream_depth(analysis, group, seeds),
                group.cyclic,
            ))
    return tuple(candidates)


def select_graph_repair(environment, diagnoses, attempted, analysis=None):
    candidates = graph_repair_candidates(
        environment, diagnoses, attempted, analysis,
    )
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: (
        candidate.action.cost,
        -len(candidate.expected_contracts),
        -candidate.graph_depth,
        candidate.action.key,
    ))


def select_minimal_repair(environment, diagnoses, attempted):
    """Backward-compatible wrapper around graph-aware repair selection."""

    candidate = select_graph_repair(environment, diagnoses, attempted)
    return candidate.action if candidate else None


def incident_status(
    environment, diagnoses, base_resolved, execute, repair_available=None,
):
    if environment.blocked_reasons:
        return "BLOCKED_BY_SAFETY_POLICY"
    if diagnoses and all(item.code == "CONTRACT_HEALTHY" for item in diagnoses) and base_resolved:
        return "RESTORED"
    if not execute and (
        any(item.repairable for item in diagnoses)
        if repair_available is None else repair_available
    ):
        return "REPAIR_AVAILABLE"
    if any(item.certainty == "ambiguous" for item in diagnoses):
        return "ABSTAINED_AMBIGUOUS"
    return "LOCALIZED_NOT_REPAIRABLE"


def write_report(report, path):
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            asdict(report),
            indent=2,
            sort_keys=True,
            default=lambda value: sorted(value) if isinstance(value, (set, frozenset)) else str(value),
        ) + "\n",
        encoding="utf-8",
    )


def print_incident_report(report):
    print("\n=== DEPENDENCY INCIDENT REPORT ===")
    print(f"Status: {report.status}")
    for diagnosis in report.diagnoses:
        print(
            f"- {diagnosis.contract_key}: {diagnosis.code} "
            f"[{diagnosis.certainty}], locus={diagnosis.locus}, repairable={diagnosis.repairable}"
        )
        for evidence in diagnosis.evidence:
            print(f"    evidence: {evidence}")
    print("Verified contracts: " + (", ".join(report.verified_contracts) or "none"))
    print("Mutations: " + (", ".join(report.mutations) or "none"))
    print("Services mutated: " + (", ".join(report.mutated_services) or "none"))
    print("Rejected actions: " + (", ".join(report.rejected_actions) or "none"))
    if report.graph and report.graph.groups:
        print("Cascade groups:")
        for group in report.graph.groups:
            print(
                f"- {group.identifier}: deepest={','.join(group.deepest_contracts)} "
                f"status={group.status} cyclic={group.cyclic}"
            )
    for heading, statements in (
        ("Observed", report.observed_explanations),
        ("Inferred", report.inferred_explanations),
        ("Confirmed", report.confirmed_explanations),
        ("Unresolved", report.unresolved_explanations),
    ):
        if statements:
            print(f"{heading}:")
            for statement in statements:
                print(f"- {statement}")
    if report.interventions:
        print("Interventions:")
        for intervention in report.interventions:
            print(
                f"- {intervention.action}: {intervention.causal_status}; "
                f"direct={','.join(intervention.directly_restored) or 'none'}; "
                f"indirect={','.join(intervention.indirectly_restored) or 'none'}"
            )


def run_dependency_incident(
    compose_file,
    limits,
    execute_action_fn,
    *,
    execute=False,
    probe_image="busybox:1.36.1",
    probe_timeout=None,
    report_path=None,
):
    """Diagnose contracts, execute only justified repairs, then verify end to end."""

    attempted = set()
    rejected = []
    mutations = []
    mutated_services = set()
    all_probes = []
    diagnosis_history = []
    seen_diagnoses = set()
    evidence = []
    interventions = []
    observed_explanations = []
    inferred_explanations = []
    confirmed_explanations = []
    unresolved_explanations = []
    selected_candidates = []
    pending_intervention = None
    environment = collect_environment(compose_file)
    final_diagnoses = ()
    final_analysis = build_graph_analysis(environment, final_diagnoses)

    def remember(collection, values):
        for value in values:
            if value not in collection:
                collection.append(value)

    for step in range(limits.max_actions + 1):
        final_diagnoses, probes, probe_facts = diagnose_environment(
            environment, probe_image, probe_timeout,
        )
        environment = replace(environment, facts=environment.facts | probe_facts)
        for diagnosis in final_diagnoses:
            key = (diagnosis.contract_key, diagnosis.code)
            if key not in seen_diagnoses:
                seen_diagnoses.add(key)
                diagnosis_history.append(diagnosis)
        all_probes.extend(probes)
        final_analysis = build_graph_analysis(environment, final_diagnoses)
        remember(observed_explanations, final_analysis.observed)
        remember(inferred_explanations, final_analysis.inferred)
        remember(unresolved_explanations, final_analysis.unresolved)

        if pending_intervention is not None:
            intervention = finalize_intervention(
                action=pending_intervention["candidate"].action,
                service=pending_intervention["candidate"].service,
                group=pending_intervention["group"],
                seed_contracts=pending_intervention["candidate"].seed_contracts,
                expected_contracts=pending_intervention["candidate"].expected_contracts,
                before_analysis=pending_intervention["analysis"],
                after_analysis=final_analysis,
                mutated_services=pending_intervention["mutated_services"],
            )
            interventions.append(intervention)
            if intervention.causal_status in {"supported", "partially_supported"}:
                remember(confirmed_explanations, (intervention.conclusion,))
            else:
                remember(unresolved_explanations, (intervention.conclusion,))
            pending_intervention = None
        candidate = select_graph_repair(
            environment, final_diagnoses, attempted, final_analysis,
        )
        # Dependency mode verifies declared contracts. Unrelated stack drift is
        # still reported by the ordinary planner but is not mutated here.
        base_resolved = True
        status = incident_status(
            environment, final_diagnoses, base_resolved, execute,
            repair_available=candidate is not None,
        )
        if status in {"RESTORED", "BLOCKED_BY_SAFETY_POLICY"}:
            break

        if step >= limits.max_actions:
            evidence.append(f"mutation limit {limits.max_actions} reached")
            break

        if not execute or candidate is None:
            break
        action = candidate.action
        selected_candidates.append(
            f"{candidate.cascade_id}:{'/'.join(action.key)}"
        )
        selected_group = next(
            group for group in final_analysis.groups
            if group.identifier == candidate.cascade_id
        )

        # Preserve objective state before mutation; health output is evidence, never parsed as cause.
        for diagnosis in final_diagnoses:
            container = (environment.containers or {}).get(diagnosis.locus)
            if container:
                evidence.append(
                    f"before {action.name}: {diagnosis.locus} status={container.status} "
                    f"exit={container.exit_code} oom={container.oom_killed} restarts={container.restart_count} "
                    f"health={container.health}"
                )
                if container.health_output:
                    evidence.append(
                        f"health output for {diagnosis.locus}: {container.health_output[:500]}"
                    )
        attempted.add(action.key)
        action_mutated_services = {
            value for value in action.key[1:] if value in environment.services
        }
        pending_intervention = {
            "candidate": candidate,
            "group": selected_group,
            "analysis": final_analysis,
            "mutated_services": frozenset(action_mutated_services),
        }
        succeeded, output, environment = execute_action_fn(environment, action, limits)
        if not action.manual:
            mutations.append(action.name)
            mutated_services.update(action_mutated_services)
        if output:
            evidence.append(f"{action.name}: {output}")
        if not succeeded:
            rejected.append(action.name)
        # Avoid diagnosing a transient container-registration state immediately
        # after start/restart as a DNS fault. The next loop still uses a fresh snapshot.
        time.sleep(0.5)
        environment = collect_environment(compose_file)

    base_resolved = True
    final_candidate = select_graph_repair(
        environment, final_diagnoses, attempted, final_analysis,
    )
    status = incident_status(
        environment, final_diagnoses, base_resolved, execute,
        repair_available=final_candidate is not None,
    )
    if status != "BLOCKED_BY_SAFETY_POLICY" and execute and any(
        item.repairable for item in final_diagnoses
    ) and final_candidate is None:
        status = "LOCALIZED_NOT_REPAIRABLE"
    missing_stack_facts = sorted(build_goal(environment) - environment.facts)
    if missing_stack_facts:
        evidence.append(
            "unrepaired stack facts outside the dependency action selected: "
            + ", ".join(missing_stack_facts)
        )
    final_analysis = replace(
        final_analysis, selected_candidates=tuple(selected_candidates),
    )
    report = IncidentReport(
        status=status,
        project=environment.project_name,
        diagnoses=tuple(diagnosis_history),
        probes=tuple(all_probes),
        mutations=tuple(mutations),
        rejected_actions=tuple(rejected),
        verified_contracts=tuple(
            item.contract_key for item in final_diagnoses
            if item.code == "CONTRACT_HEALTHY"
        ),
        evidence=tuple(evidence),
        observed_facts=tuple(sorted(environment.facts)),
        mutated_services=tuple(sorted(mutated_services)),
        graph=final_analysis,
        interventions=tuple(interventions),
        observed_explanations=tuple(observed_explanations),
        inferred_explanations=tuple(inferred_explanations),
        confirmed_explanations=tuple(confirmed_explanations),
        unresolved_explanations=tuple(unresolved_explanations),
    )
    print_incident_report(report)
    if report_path:
        write_report(report, report_path)
    return (0 if status == "RESTORED" else 2), report
