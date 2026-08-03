"""Randomized-order, three-way benchmark: Codex vs naive planner vs
dependency-aware planner, across local fixtures AND external-app manifests.

Per scenario, arm order is randomized (not fixed LLM-first) to control for
ordering effects. Codex now runs automatically via `codex exec` instead of
waiting on a human operator, and cannot see fault-injection scripts, scenario
labels, historical repairs, or the planner's own proposed plan.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent / "external"))

from dockrepair import Limits, execute_until_resolved  # noqa: E402
from dockrepair_docker import collect_environment  # noqa: E402
from dockrepair_planner import build_goal, search  # noqa: E402
from dockrepair_planner_naive import search_naive  # noqa: E402
from fault_injection import (  # noqa: E402
    bring_up_clean, clean_up_fault_artifacts, inject_fault, load_manifest,
)

EXTERNAL_DIR = Path(__file__).resolve().parent / "external"
MANIFEST_DIR = EXTERNAL_DIR / "manifests"
RESULTS_FILE = EXTERNAL_DIR / "results" / "results.json"

# Fixed model string: change this once here, not per-run, so every trial in
# a sweep used the exact same Codex configuration.
CODEX_MODEL = "gpt-5.1-codex"  # <-- set to whatever fixed model you're benchmarking


def utc_now():
    return datetime.now(timezone.utc)


def read_json(path, default):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def all_manifests():
    return sorted(MANIFEST_DIR.glob("*.json"))


def inspect_goal(compose_file):
    environment = collect_environment(str(compose_file))
    missing = sorted(build_goal(environment) - environment.facts)
    return not missing, missing, environment.errors


def run_codex_trial(manifest):
    """Run Codex autonomously against the live broken environment and time it."""

    compose_file = manifest["compose_file"]
    started = time.perf_counter()

    prompt = (
        f"Repair the Docker Compose environment at {compose_file}. "
        "Do not edit project files. "
        "Inspect Docker before acting and after every mutation. "
        "Stop when every declared service is current, running, and healthy. "
        "Use at most 20 Docker-mutating commands."
    )

    result = subprocess.run(
        [
            "codex", "exec",
            "--ephemeral",
            "--json",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox", "danger-full-access",
            "--model", CODEX_MODEL,
            prompt,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1200,
    )
    elapsed = time.perf_counter() - started

    transcript_path = (
        EXTERNAL_DIR / "results" / f"{manifest['id']}-codex-transcript.jsonl"
    )
    transcript_path.write_text(result.stdout, encoding="utf-8")

    healthy, missing, errors = inspect_goal(compose_file)
    return {
        "seconds": round(elapsed, 3),
        "success": healthy,
        "return_code": result.returncode,
        "transcript_path": str(transcript_path),
        "missing_at_end": missing,
    }


def run_planner_trial(compose_file, search_fn, label):
    started = time.perf_counter()
    return_code = execute_until_resolved(str(compose_file), Limits(), search_fn=search_fn)
    elapsed = time.perf_counter() - started
    healthy, missing, errors = inspect_goal(compose_file)
    success = return_code == 0 and healthy
    return {
        "seconds": round(elapsed, 3),
        "success": success,
        "return_code": return_code,
        "missing_at_end": missing,
    }


ARM_RUNNERS = {
    "codex": lambda manifest: run_codex_trial(manifest),
    "naive": lambda manifest: run_planner_trial(manifest["compose_file"], search_naive, "naive"),
    "planner": lambda manifest: run_planner_trial(manifest["compose_file"], search, "planner"),
}


def run_one_repetition(manifest, arm_order):
    """Run all three arms for one scenario, in the given randomized order.

    Each arm gets the identical fault recreated fresh immediately before its
    own timed window -- no arm gets a head start or a stale environment left
    over from a previous arm.
    """

    row = {
        "id": manifest["id"],
        "application": manifest["application"],
        "fault": manifest["fault"],
        "expected_repairable": manifest["expected_repairable"],
        "arm_order": list(arm_order),
        "run_utc": utc_now().isoformat(),
    }

    for arm in arm_order:
        print(f"  [{manifest['id']}] recreating fault for arm: {arm}")
        bring_up_clean(manifest)
        inject_fault(manifest)
        result = ARM_RUNNERS[arm](manifest)
        clean_up_fault_artifacts(manifest)
        row[arm] = result
        status = "OK" if result["success"] else "FAILED"
        print(f"  [{manifest['id']}] {arm}: {status} in {result['seconds']:.2f}s")

    return row


def cmd_run(scenario_id, repetitions, seed):
    rng = random.Random(seed)
    manifests = (
        [load_manifest(MANIFEST_DIR / f"{scenario_id}.json")]
        if scenario_id else
        [load_manifest(path) for path in all_manifests()]
    )

    results = read_json(RESULTS_FILE, {"runs": []})
    for manifest in manifests:
        for repetition in range(1, repetitions + 1):
            arm_order = list(ARM_RUNNERS)
            rng.shuffle(arm_order)
            print(f"=== {manifest['id']} repetition {repetition}/{repetitions} order={arm_order} ===")
            row = run_one_repetition(manifest, arm_order)
            row["repetition"] = repetition
            results["runs"].append(row)
            write_json(RESULTS_FILE, results)  # save after every repetition, not just at the end

    print(f"\nSaved to {RESULTS_FILE}")
    return 0


def cmd_list():
    for path in all_manifests():
        manifest = load_manifest(path)
        print(f"{manifest['id']:32} {manifest['application']:16} {manifest['fault']}")
    return 0


def cmd_results():
    results = read_json(RESULTS_FILE, {"runs": []})
    runs = results.get("runs", [])
    if not runs:
        print("No results yet.")
        return 0
    print(f"{'id':32} {'rep':>4} {'codex s':>9} {'naive s':>9} {'planner s':>10} {'codex ok':>9} {'naive ok':>9} {'planner ok':>11}")
    for row in runs:
        print(
            f"{row['id']:32} {row['repetition']:>4} "
            f"{row['codex']['seconds']:>9.2f} {row['naive']['seconds']:>9.2f} {row['planner']['seconds']:>10.2f} "
            f"{'yes' if row['codex']['success'] else 'no':>9} "
            f"{'yes' if row['naive']['success'] else 'no':>9} "
            f"{'yes' if row['planner']['success'] else 'no':>11}"
        )
    return 0


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("list", "run", "results"))
    parser.add_argument("--scenario", help="Manifest id to run; omit to run every manifest.")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--seed", type=int, default=0, help="Random seed for arm ordering, for reproducibility.")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.command == "list":
        return cmd_list()
    if args.command == "results":
        return cmd_results()
    return cmd_run(args.scenario, args.repetitions, args.seed)


if __name__ == "__main__":
    raise SystemExit(main())