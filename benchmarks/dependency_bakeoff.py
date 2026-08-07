"""Four-arm (+ optional agy) evaluation for declared Docker Compose dependency failures."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(HERE))

from agy_agent import (  # noqa: E402
    DEFAULT_AGY_MODEL,
    DEFAULT_AGY_TIMEOUT_SEC,
    agy_available,
    run_agy_trial,
)
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


SYMBOLIC_ARMS = ("compose_up", "reactive_restart", "shallow", "diagnostic")
ALL_ARMS = SYMBOLIC_ARMS + ("agy",)
RESULTS_FILE = HERE / "results" / "dependency_bakeoff_results.json"
AGY_RESULTS_FILE = HERE / "results" / "dependency_bakeoff_agy_results.json"
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


def _score_arm(scenario, hashes, started, *, restored, missing, errors, mutations,
               mutated_services, probes, return_code, detail="", report=None,
               diagnosis_codes=None, safe_abstention=None, extra=None):
    unchanged = file_hashes(scenario) == hashes
    diagnosis_codes = list(diagnosis_codes or [])
    if report is not None and not diagnosis_codes:
        diagnosis_codes = [item.code for item in report.diagnoses]
    diagnosis_correct = (
        scenario.expected_diagnosis in diagnosis_codes if diagnosis_codes else None
    )
    if safe_abstention is None:
        safe_abstention = bool(
            report
            and report.status in {"LOCALIZED_NOT_REPAIRABLE", "ABSTAINED_AMBIGUOUS"}
            and len(report.mutations) <= scenario.abstention_mutation_budget
        )
    outcome_correct = restored if scenario.repair_expected else safe_abstention
    row = {
        "seconds": round(time.perf_counter() - started, 3),
        "return_code": return_code,
        "restored": restored,
        "outcome_correct": bool(outcome_correct and unchanged),
        "diagnosis_correct": diagnosis_correct,
        "diagnosis_codes": diagnosis_codes,
        "safe_abstention": bool(safe_abstention),
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
    if extra:
        row.update(extra)
    return row


def _run_agy_arm(scenario, trial_id, model, timeout_sec):
    hashes = file_hashes(scenario)
    before = collect_environment(str(scenario.compose_file))
    declared = set(before.services)
    started = time.perf_counter()
    transcript = HERE / "results" / "transcripts" / f"{trial_id}-agy.jsonl"
    trial = run_agy_trial(
        scenario,
        model=model,
        timeout_sec=timeout_sec,
        transcript_path=transcript,
        cwd=ROOT,
    )
    after = collect_environment(str(scenario.compose_file))
    mutated_services = {
        name for name in declared
        if (before.containers or {}).get(name) != (after.containers or {}).get(name)
    }
    # Also attribute services named in mutating docker/compose commands.
    for command in trial.get("mutating_commands") or []:
        for name in declared:
            if re.search(rf"\b{re.escape(name)}\b", command):
                mutated_services.add(name)
    restored, missing, errors = inspect_goal(scenario.compose_file)
    report = trial.get("report") or {}
    diagnosis = report.get("diagnosis")
    diagnosis_codes = [diagnosis] if diagnosis else []
    claimed = (report.get("status") or "").upper()
    mutations = int(trial.get("mutations") or 0)
    # Require an explicit ABSTAINED claim plus mutation budget on unsupported faults.
    safe_abstention = (
        not restored
        and claimed == "ABSTAINED"
        and mutations <= scenario.abstention_mutation_budget
    )

    return _score_arm(
        scenario, hashes, started,
        restored=restored,
        missing=missing,
        errors=errors,
        mutations=mutations,
        mutated_services=mutated_services,
        probes=0,
        return_code=trial.get("return_code", 1),
        detail=claimed or trial.get("agy_status") or "",
        diagnosis_codes=diagnosis_codes,
        safe_abstention=safe_abstention,
        extra={
            "model": trial.get("model"),
            "timed_out": trial.get("timed_out"),
            "agy_status": trial.get("agy_status"),
            "agy_duration_seconds": trial.get("duration_seconds"),
            "num_turns": trial.get("num_turns"),
            "usage": trial.get("usage"),
            "mutating_commands": trial.get("mutating_commands"),
            "commands": trial.get("commands"),
            "agent_report": report,
            "transcript_path": trial.get("transcript_path"),
        },
    )


def _run_arm(scenario, arm, *, trial_id=None, agy_model=DEFAULT_AGY_MODEL,
             agy_timeout_sec=DEFAULT_AGY_TIMEOUT_SEC):
    if arm == "agy":
        return _run_agy_arm(scenario, trial_id or scenario.name, agy_model, agy_timeout_sec)

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
    elif arm == "diagnostic":
        return_code, report = run_dependency_incident(
            str(scenario.compose_file), Limits(), execute_action, execute=True,
        )
        mutations = len(report.mutations)
        mutated_services.update(report.mutated_services)
        probes = len(report.probes)
    else:
        raise RuntimeError(f"Unknown arm: {arm}")

    restored, missing, errors = inspect_goal(scenario.compose_file)
    return _score_arm(
        scenario, hashes, started,
        restored=restored,
        missing=missing,
        errors=errors,
        mutations=mutations,
        mutated_services=mutated_services,
        probes=probes,
        return_code=return_code,
        detail=detail,
        report=report,
    )


def _summary(runs, arms):
    result = {"n_runs": len(runs), "arms": {}}
    for arm in arms:
        rows = [row[arm] for row in runs if arm in row]
        if not rows:
            continue
        diagnosis_rows = [row for row in rows if row["diagnosis_correct"] is not None]
        arm_summary = {
            "n": len(rows),
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
        if any("usage" in row for row in rows):
            usages = [row.get("usage") or {} for row in rows]
            arm_summary["mean_input_tokens"] = round(
                sum(int(u.get("input_tokens") or 0) for u in usages) / len(rows), 1
            )
            arm_summary["mean_output_tokens"] = round(
                sum(int(u.get("output_tokens") or 0) for u in usages) / len(rows), 1
            )
            arm_summary["mean_total_tokens"] = round(
                sum(int(u.get("total_tokens") or 0) for u in usages) / len(rows), 1
            )
            arm_summary["mean_cache_read_tokens"] = round(
                sum(int(u.get("cache_read_tokens") or 0) for u in usages) / len(rows), 1
            )
            arm_summary["mean_turns"] = round(
                sum(int(row.get("num_turns") or 0) for row in rows) / len(rows), 2
            )
            arm_summary["timeouts"] = sum(bool(row.get("timed_out")) for row in rows)
            models = sorted({row.get("model") for row in rows if row.get("model")})
            arm_summary["models"] = models
        result["arms"][arm] = arm_summary
    return result


def _resolve_arms(arms, include_agy, agy_only):
    if agy_only:
        return ("agy",)
    if arms:
        selected = tuple(arms)
    else:
        selected = SYMBOLIC_ARMS + (("agy",) if include_agy else ())
    unknown = [arm for arm in selected if arm not in ALL_ARMS]
    if unknown:
        raise RuntimeError(f"Unknown arms: {', '.join(unknown)}")
    if "agy" in selected and not agy_available():
        raise RuntimeError("agy arm requested but agy CLI is not on PATH")
    return selected


def run_bakeoff(
    names,
    repetitions,
    seed,
    fresh,
    *,
    arms=None,
    include_agy=False,
    agy_only=False,
    agy_model=DEFAULT_AGY_MODEL,
    agy_timeout_sec=DEFAULT_AGY_TIMEOUT_SEC,
    results_file=None,
):
    ensure_daemon()
    probe_ready, probe_image_id = probe_image_available("busybox:1.36.1", ROOT)
    if not probe_ready:
        raise RuntimeError(
            "busybox:1.36.1 is required but is not local; pull it explicitly before the benchmark"
        )
    selected_arms = _resolve_arms(arms, include_agy, agy_only)
    results_path = Path(results_file) if results_file else (
        AGY_RESULTS_FILE if selected_arms == ("agy",) else RESULTS_FILE
    )
    selected = names or list(DEFAULT_SCENARIOS)
    unknown = [name for name in selected if name not in DEFAULT_SCENARIOS]
    if unknown:
        raise RuntimeError(f"Not a dependency-study scenario: {', '.join(unknown)}")
    data = {
        "meta": {
            "seed": seed,
            "repetitions": repetitions,
            "arms": list(selected_arms),
            "probe_image": "busybox:1.36.1",
            "probe_image_id": probe_image_id,
            "agy_model": agy_model if "agy" in selected_arms else None,
            "agy_timeout_sec": agy_timeout_sec if "agy" in selected_arms else None,
        },
        "runs": [],
    }
    if not fresh and results_path.is_file():
        data = json.loads(results_path.read_text(encoding="utf-8"))
        data["meta"] = {
            **data.get("meta", {}),
            "seed": seed,
            "repetitions": repetitions,
            "arms": list(selected_arms),
            "probe_image": "busybox:1.36.1",
            "probe_image_id": probe_image_id,
            "agy_model": agy_model if "agy" in selected_arms else data.get("meta", {}).get("agy_model"),
            "agy_timeout_sec": (
                agy_timeout_sec if "agy" in selected_arms
                else data.get("meta", {}).get("agy_timeout_sec")
            ),
        }
    rng = random.Random(seed)
    for name in selected:
        scenario = SCENARIOS[name]
        for repetition in range(1, repetitions + 1):
            order = list(selected_arms)
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
                trial_id = f"{name}-r{repetition}-s{seed}"
                row[arm] = _run_arm(
                    scenario, arm,
                    trial_id=trial_id,
                    agy_model=agy_model,
                    agy_timeout_sec=agy_timeout_sec,
                )
                print(
                    f"{name} r{repetition} {arm}: "
                    f"outcome={'OK' if row[arm]['outcome_correct'] else 'FAIL'}"
                    + (
                        f" tokens={row[arm].get('usage', {}).get('total_tokens')}"
                        if arm == "agy" else ""
                    )
                )
            clean_project(scenario.compose_file)
            data["runs"].append(row)
            results_path.parent.mkdir(parents=True, exist_ok=True)
            results_path.write_text(json.dumps(data, indent=2, default=list) + "\n", encoding="utf-8")
    # Summary over whatever arms appear in the runs.
    observed = []
    for arm in ALL_ARMS:
        if any(arm in row for row in data["runs"]):
            observed.append(arm)
    data["summary"] = _summary(data["runs"], observed)
    results_path.write_text(json.dumps(data, indent=2, default=list) + "\n", encoding="utf-8")
    print(json.dumps(data["summary"], indent=2))
    print(f"wrote {results_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", action="append", dest="scenarios")
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument(
        "--arm", action="append", dest="arms",
        help="Arm to include (repeatable). Default: symbolic four-arm suite.",
    )
    parser.add_argument(
        "--include-agy", action="store_true",
        help="Add the agy LLM arm to the default symbolic suite.",
    )
    parser.add_argument(
        "--agy-only", action="store_true",
        help="Run only the agy arm (writes dependency_bakeoff_agy_results.json).",
    )
    parser.add_argument("--agy-model", default=DEFAULT_AGY_MODEL)
    parser.add_argument("--agy-timeout-sec", type=int, default=DEFAULT_AGY_TIMEOUT_SEC)
    parser.add_argument("--results-file", type=Path)
    args = parser.parse_args()
    run_bakeoff(
        args.scenarios,
        args.repetitions,
        args.seed,
        args.fresh,
        arms=args.arms,
        include_agy=args.include_agy,
        agy_only=args.agy_only,
        agy_model=args.agy_model,
        agy_timeout_sec=args.agy_timeout_sec,
        results_file=args.results_file,
    )


if __name__ == "__main__":
    main()
