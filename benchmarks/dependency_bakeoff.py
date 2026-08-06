"""Four-arm evaluation for declared Docker Compose dependency failures."""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(HERE))

from dockrepair import Limits, execute_action, execute_until_resolved  # noqa: E402
from dockrepair_diagnosis import probe_image_available, run_dependency_incident  # noqa: E402
from dockrepair_docker import collect_environment  # noqa: E402
from scenarios import (  # noqa: E402
    SCENARIOS,
    assert_broken,
    clean_project,
    compose,
    ensure_daemon,
    file_hashes,
    inspect_goal,
)


ARMS = ("compose_up", "reactive_restart", "shallow", "diagnostic")
RESULTS_FILE = HERE / "results" / "dependency_bakeoff_results.json"
DEFAULT_SCENARIOS = tuple(
    name for name, scenario in SCENARIOS.items() if scenario.expected_diagnosis
)


def _dependents(environment, roots):
    affected = set(roots)
    changed = True
    while changed:
        changed = False
        for name, service in environment.services.items():
            if name not in affected and any(target in affected for target, _ in service.dependencies):
                affected.add(name)
                changed = True
    return tuple(sorted(affected))


def _reactive_restart(scenario):
    environment = collect_environment(str(scenario.compose_file))
    visibly_failed = {
        name
        for name in environment.services
        if any(
            f"{kind}:{name}" in environment.facts
            for kind in ("stopped", "unhealthy", "readiness_pending")
        )
    }
    if not visibly_failed:
        return 0, (), "no lifecycle or health failure was visible"
    targets = _dependents(environment, visibly_failed)
    result = compose(scenario.compose_file, "restart", *targets, check=False, quiet=True)
    return result.returncode, targets, f"restarted {', '.join(targets)}"


def _run_arm(scenario, arm):
    hashes = file_hashes(scenario)
    before_environment = collect_environment(str(scenario.compose_file))
    declared_services = set(before_environment.services)
    started = time.perf_counter()
    mutations = 0
    mutated_services = set()
    probes = 0
    report = None
    detail = ""
    if arm == "compose_up":
        result = compose(
            scenario.compose_file, "up", "-d", "--wait", "--wait-timeout", "30",
            check=False, quiet=True,
        )
        return_code = result.returncode
        mutations = 1
        after_compose = collect_environment(str(scenario.compose_file))
        mutated_services.update(
            name for name in declared_services
            if (before_environment.containers or {}).get(name)
            != (after_compose.containers or {}).get(name)
        )
    elif arm == "reactive_restart":
        return_code, targets, detail = _reactive_restart(scenario)
        mutations = int(bool(targets))
        mutated_services.update(targets)
    elif arm == "shallow":
        events = []
        return_code = execute_until_resolved(
            str(scenario.compose_file), Limits(), event_sink=events.append,
        )
        mutations = sum(
            event.get("type") == "action_started" and not event.get("manual")
            for event in events
        )
        for event in events:
            if event.get("type") == "action_started" and not event.get("manual"):
                mutated_services.update(
                    value for value in event.get("key", ())[1:]
                    if value in declared_services
                )
    else:
        return_code, report = run_dependency_incident(
            str(scenario.compose_file), Limits(), execute_action, execute=True,
        )
        mutations = len(report.mutations)
        mutated_services.update(report.mutated_services)
        probes = len(report.probes)

    restored, missing, errors = inspect_goal(scenario.compose_file)
    unchanged = file_hashes(scenario) == hashes
    diagnosis_codes = [item.code for item in report.diagnoses] if report else []
    diagnosis_correct = scenario.expected_diagnosis in diagnosis_codes if report else None
    safe_abstention = bool(
        report
        and report.status in {"LOCALIZED_NOT_REPAIRABLE", "ABSTAINED_AMBIGUOUS"}
        and len(report.mutations) <= scenario.abstention_mutation_budget
    )
    outcome_correct = restored if scenario.repair_expected else safe_abstention
    return {
        "seconds": round(time.perf_counter() - started, 3),
        "return_code": return_code,
        "restored": restored,
        "outcome_correct": outcome_correct and unchanged,
        "diagnosis_correct": diagnosis_correct,
        "diagnosis_codes": diagnosis_codes,
        "safe_abstention": safe_abstention,
        "mutations": mutations,
        "mutated_services": sorted(mutated_services),
        "services_mutated": len(mutated_services),
        "probes": probes,
        "missing_at_end": missing,
        "collection_errors": errors,
        "files_unchanged": unchanged,
        "detail": detail,
        "incident": asdict(report) if report else None,
    }


def _summary(runs):
    result = {"n_runs": len(runs), "arms": {}}
    for arm in ARMS:
        rows = [row[arm] for row in runs]
        diagnosis_rows = [row for row in rows if row["diagnosis_correct"] is not None]
        result["arms"][arm] = {
            "outcome_accuracy": round(sum(row["outcome_correct"] for row in rows) / len(rows), 3),
            "repair_rate": round(sum(row["restored"] for row in rows) / len(rows), 3),
            "diagnosis_accuracy": (
                round(sum(row["diagnosis_correct"] for row in diagnosis_rows) / len(diagnosis_rows), 3)
                if diagnosis_rows else None
            ),
            "safe_abstentions": sum(row["safe_abstention"] for row in rows),
            "mean_seconds": round(sum(row["seconds"] for row in rows) / len(rows), 3),
            "mean_mutations": round(sum(row["mutations"] for row in rows) / len(rows), 3),
            "mean_services_mutated": round(
                sum(row["services_mutated"] for row in rows) / len(rows), 3
            ),
            "mean_probes": round(sum(row["probes"] for row in rows) / len(rows), 3),
            "safety_violations": sum(not row["files_unchanged"] for row in rows),
        }
    return result


def run_bakeoff(names, repetitions, seed, fresh):
    ensure_daemon()
    probe_ready, probe_image_id = probe_image_available("busybox:1.36.1", ROOT)
    if not probe_ready:
        raise RuntimeError(
            "busybox:1.36.1 is required but is not local; pull it explicitly before the benchmark"
        )
    selected = names or list(DEFAULT_SCENARIOS)
    unknown = [name for name in selected if name not in DEFAULT_SCENARIOS]
    if unknown:
        raise RuntimeError(f"Not a dependency-study scenario: {', '.join(unknown)}")
    data = {
        "meta": {
            "seed": seed,
            "repetitions": repetitions,
            "probe_image": "busybox:1.36.1",
            "probe_image_id": probe_image_id,
        },
        "runs": [],
    }
    if not fresh and RESULTS_FILE.is_file():
        data = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
        data["meta"] = {
            "seed": seed,
            "repetitions": repetitions,
            "probe_image": "busybox:1.36.1",
            "probe_image_id": probe_image_id,
        }
    rng = random.Random(seed)
    for name in selected:
        scenario = SCENARIOS[name]
        for repetition in range(1, repetitions + 1):
            order = list(ARMS)
            rng.shuffle(order)
            row = {
                "scenario": name,
                "repetition": repetition,
                "expected_diagnosis": scenario.expected_diagnosis,
                "repair_expected": scenario.repair_expected,
                "abstention_mutation_budget": scenario.abstention_mutation_budget,
                "arm_order": order,
            }
            for arm in order:
                scenario.setup(scenario)
                assert_broken(scenario)
                row[arm] = _run_arm(scenario, arm)
                print(
                    f"{name} r{repetition} {arm}: "
                    f"outcome={'OK' if row[arm]['outcome_correct'] else 'FAIL'}"
                )
            clean_project(scenario.compose_file)
            data["runs"].append(row)
            RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            RESULTS_FILE.write_text(json.dumps(data, indent=2, default=list) + "\n", encoding="utf-8")
    data["summary"] = _summary(data["runs"])
    RESULTS_FILE.write_text(json.dumps(data, indent=2, default=list) + "\n", encoding="utf-8")
    print(json.dumps(data["summary"], indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", action="append", dest="scenarios")
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--fresh", action="store_true")
    args = parser.parse_args()
    run_bakeoff(args.scenarios, args.repetitions, args.seed, args.fresh)


if __name__ == "__main__":
    main()
