"""Active, bounded diagnosis and minimal repair for declared TCP dependencies."""

from __future__ import annotations

import json
import math
import subprocess
import time
from dataclasses import asdict, replace
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


def select_minimal_repair(environment, diagnoses, attempted):
    for diagnosis in diagnoses:
        contract = next(item for item in environment.contracts if item.key == diagnosis.contract_key)
        if diagnosis.code in {
            "CALLER_UNAVAILABLE", "TARGET_UNAVAILABLE", "TARGET_CONFIG_DRIFT", "TARGET_OOM_KILLED",
        }:
            action = _catalog_action(
                environment,
                diagnosis.locus,
                ("start", "reconcile", "recreate"),
                attempted,
            )
            if action:
                return action
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
            action = _catalog_action(environment, diagnosis.locus, ("connect_network",), attempted)
            if action:
                return action
        if diagnosis.code == "TARGET_NOT_LISTENING":
            restart_key = ("restart", contract.target)
            recreate_key = ("recreate", contract.target)
            if restart_key not in attempted:
                return _constructed_action(environment, contract.target, "restart")
            if environment.services[contract.target].allow_recreate and recreate_key not in attempted:
                return _constructed_action(environment, contract.target, "recreate")
    return None


def incident_status(environment, diagnoses, base_resolved, execute):
    if environment.blocked_reasons:
        return "BLOCKED_BY_SAFETY_POLICY"
    if diagnoses and all(item.code == "CONTRACT_HEALTHY" for item in diagnoses) and base_resolved:
        return "RESTORED"
    if not execute and any(item.repairable for item in diagnoses):
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
    environment = collect_environment(compose_file)
    final_diagnoses = ()

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
        # Dependency mode verifies declared contracts. Unrelated stack drift is
        # still reported by the ordinary planner but is not mutated here.
        base_resolved = True
        status = incident_status(environment, final_diagnoses, base_resolved, execute)
        if status in {"RESTORED", "BLOCKED_BY_SAFETY_POLICY"}:
            break

        if step >= limits.max_actions:
            evidence.append(f"mutation limit {limits.max_actions} reached")
            break

        action = select_minimal_repair(environment, final_diagnoses, attempted)
        if not execute or action is None:
            break

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
        succeeded, output, environment = execute_action_fn(environment, action, limits)
        if not action.manual:
            mutations.append(action.name)
            mutated_services.update(
                value for value in action.key[1:] if value in environment.services
            )
        if output:
            evidence.append(f"{action.name}: {output}")
        if not succeeded:
            rejected.append(action.name)
        # Avoid diagnosing a transient container-registration state immediately
        # after start/restart as a DNS fault. The next loop still uses a fresh snapshot.
        time.sleep(0.5)
        environment = collect_environment(compose_file)

    base_resolved = True
    status = incident_status(environment, final_diagnoses, base_resolved, execute)
    if status != "BLOCKED_BY_SAFETY_POLICY" and execute and any(
        item.repairable for item in final_diagnoses
    ) and not select_minimal_repair(
        environment, final_diagnoses, attempted,
    ):
        status = "LOCALIZED_NOT_REPAIRABLE"
    missing_stack_facts = sorted(build_goal(environment) - environment.facts)
    if missing_stack_facts:
        evidence.append(
            "unrepaired stack facts outside the dependency action selected: "
            + ", ".join(missing_stack_facts)
        )
    report = IncidentReport(
        status,
        environment.project_name,
        tuple(diagnosis_history),
        tuple(all_probes),
        tuple(mutations),
        tuple(rejected),
        tuple(item.contract_key for item in final_diagnoses if item.code == "CONTRACT_HEALTHY"),
        tuple(evidence),
        tuple(sorted(environment.facts)),
        tuple(sorted(mutated_services)),
    )
    print_incident_report(report)
    if report_path:
        write_report(report, report_path)
    return (0 if status == "RESTORED" else 2), report
