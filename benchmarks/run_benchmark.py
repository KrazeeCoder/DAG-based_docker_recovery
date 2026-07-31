"""Stage and measure one Docker repair scenario at a time.

The LLM is intentionally outside this process: prepare-llm starts its wall-clock
timer, the LLM executes real repair commands, and finish-llm stops the timer only
after Docker inspection proves the goal. run-app then recreates the exact failure
and measures dockrepair's executor in-process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from dockrepair import Limits, _start_docker_engine, execute_until_resolved  # noqa: E402
from dockrepair_docker import collect_environment  # noqa: E402
from dockrepair_planner import build_goal  # noqa: E402


BENCHMARK_DIR = Path(__file__).resolve().parent
FIXTURES = BENCHMARK_DIR / "fixtures"
STATE_FILE = BENCHMARK_DIR / ".benchmark-state.json"
RESULTS_FILE = BENCHMARK_DIR / "results.json"


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    compose_file: Path
    setup: Callable[["Scenario"], None]
    extra_files: tuple[Path, ...] = ()


def run(arguments, *, check=True, quiet=False):
    result = subprocess.run(
        arguments,
        cwd=ROOT,
        capture_output=quiet,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        shell=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Command failed ({result.returncode}): {subprocess.list2cmdline(arguments)}\n{detail}")
    return result


def compose(path, *arguments, check=True, quiet=False):
    return run(["docker", "compose", "-f", str(path), *arguments], check=check, quiet=quiet)


def clean_project(path):
    compose(path, "down", "--remove-orphans", check=False, quiet=True)


def ensure_daemon():
    """Make fixture setup possible without charging engine startup to either trial."""

    ready, message = _start_docker_engine(timeout=180.0)
    if not ready:
        raise RuntimeError(message)


def setup_stopped_chain(scenario):
    clean_project(scenario.compose_file)
    compose(scenario.compose_file, "up", "-d", "--wait", "--wait-timeout", "30", quiet=True)
    compose(scenario.compose_file, "stop", "worker", "api", "database", quiet=True)


def setup_partial_stop(scenario):
    clean_project(scenario.compose_file)
    compose(scenario.compose_file, "up", "-d", "--wait", "--wait-timeout", "30", quiet=True)
    compose(scenario.compose_file, "stop", "worker4", quiet=True)


def setup_missing_service(scenario):
    clean_project(scenario.compose_file)
    compose(scenario.compose_file, "up", "-d", "--wait", "--wait-timeout", "30", quiet=True)
    compose(scenario.compose_file, "rm", "-s", "-f", "worker", quiet=True)


def setup_config_drift(scenario):
    old_file = scenario.extra_files[0]
    clean_project(scenario.compose_file)
    clean_project(old_file)
    compose(old_file, "up", "-d", "--wait", "--wait-timeout", "30", quiet=True)


def setup_unhealthy(scenario):
    clean_project(scenario.compose_file)
    compose(scenario.compose_file, "up", "-d", "--wait", "--wait-timeout", "30", quiet=True)
    container_id = compose(scenario.compose_file, "ps", "-q", "cache", quiet=True).stdout.strip()
    if not container_id:
        raise RuntimeError("Could not find the cache container to sabotage.")
    run(["docker", "exec", container_id, "rm", "-f", "/tmp/healthy"], quiet=True)
    wait_for_fact(scenario.compose_file, "unhealthy:cache", timeout=15)


def setup_recreate_fallback(scenario):
    clean_project(scenario.compose_file)
    compose(scenario.compose_file, "up", "-d", "--wait", "--wait-timeout", "30", quiet=True)
    container_id = compose(scenario.compose_file, "ps", "-q", "cache", quiet=True).stdout.strip()
    if not container_id:
        raise RuntimeError("Could not find the cache container to corrupt.")
    run(
        ["docker", "exec", container_id, "sh", "-c", "touch /tmp/corrupt; rm -f /tmp/healthy"],
        quiet=True,
    )
    wait_for_fact(scenario.compose_file, "unhealthy:cache", timeout=15)


def setup_flaky_start(scenario):
    clean_project(scenario.compose_file)
    compose(scenario.compose_file, "create", quiet=True)


SCENARIOS = {
    "stopped-chain": Scenario(
        "stopped-chain",
        "Three stopped services with two health-gated dependency levels; simple Compose reconciliation should excel.",
        FIXTURES / "stopped_chain" / "compose.yaml",
        setup_stopped_chain,
    ),
    "partial-stop": Scenario(
        "partial-stop",
        "Only worker4 is stopped while five peers remain healthy; tests selective repair overhead.",
        FIXTURES / "partial_stop" / "compose.yaml",
        setup_partial_stop,
    ),
    "missing-service": Scenario(
        "missing-service",
        "One container is removed while two peers stay up; tests targeted reconciliation.",
        FIXTURES / "missing_service" / "compose.yaml",
        setup_missing_service,
    ),
    "config-drift": Scenario(
        "config-drift",
        "Processor is running from revision 1 while revision 2 is desired; tests config-hash detection.",
        FIXTURES / "config_drift" / "compose-v2.yaml",
        setup_config_drift,
        (FIXTURES / "config_drift" / "compose-v1.yaml",),
    ),
    "unhealthy": Scenario(
        "unhealthy",
        "A running dependency is unhealthy and restart repairs it; generic Compose up often leaves it unhealthy.",
        FIXTURES / "unhealthy" / "compose.yaml",
        setup_unhealthy,
    ),
    "recreate-fallback": Scenario(
        "recreate-fallback",
        "Restart cannot clear container-local corruption, but forced recreation can; proves alternative-path fallback.",
        FIXTURES / "recreate_fallback" / "compose.yaml",
        setup_recreate_fallback,
    ),
    "flaky-start": Scenario(
        "flaky-start",
        "The prerequisite exits on its first start and succeeds on its second; tests inspect-and-replan recovery.",
        FIXTURES / "flaky_start" / "compose.yaml",
        setup_flaky_start,
    ),
}


def utc_now():
    return datetime.now(timezone.utc)


def read_json(path, default):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def file_hashes(scenario):
    paths = (scenario.compose_file, *scenario.extra_files)
    return {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def assert_files_unchanged(expected):
    actual = {
        name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
        for name in expected
        if Path(name).is_file()
    }
    if actual != expected:
        raise RuntimeError("A benchmark Compose file changed during the repair; the trial is invalid.")


def inspect_goal(path):
    environment = collect_environment(str(path))
    missing = sorted(build_goal(environment) - environment.facts)
    return not missing, missing, environment.errors


def wait_for_fact(path, wanted, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if wanted in collect_environment(str(path)).facts:
            return
        time.sleep(0.5)
    raise RuntimeError(f"Setup did not produce the expected fact within {timeout:g}s: {wanted}")


def assert_broken(scenario):
    healthy, missing, _ = inspect_goal(scenario.compose_file)
    if healthy:
        raise RuntimeError(f"Setup error: {scenario.name} is already healthy.")
    return missing


def load_state():
    return read_json(STATE_FILE, {})  # type: ignore[return-value]


def save_state(state):
    write_json(STATE_FILE, state)


def require_scenario(name):
    if not name or name not in SCENARIOS:
        choices = ", ".join(SCENARIOS)
        raise RuntimeError(f"Choose a scenario with --scenario. Available: {choices}")
    return SCENARIOS[name]


def cmd_list():
    for scenario in SCENARIOS.values():
        print(f"{scenario.name:16} {scenario.description}")
    return 0


def cmd_prepare_llm(scenario):
    state = load_state()
    if state and state.get("phase") not in {"complete", "aborted"}:
        raise RuntimeError(
            f"Finish or abort the active {state.get('scenario')} trial before starting another."
        )

    ensure_daemon()
    scenario.setup(scenario)
    missing = assert_broken(scenario)
    started = utc_now()
    state = {
        "scenario": scenario.name,
        "order": "llm-first",
        "phase": "llm_running",
        "started_utc": started.isoformat(),
        "file_hashes": file_hashes(scenario),
    }
    save_state(state)

    print(f"LLM timer started at {started.isoformat()}")
    print(f"Compose file: {scenario.compose_file.resolve()}")
    print("Missing goal facts: " + ", ".join(missing))
    print("Repair this environment by actually executing Docker commands.")
    print(f"When it is resolved, run: py -3.11 {Path(__file__).resolve()} finish-llm --scenario {scenario.name}")
    return 0


def cmd_finish_llm(scenario):
    state = load_state()
    if state.get("scenario") != scenario.name or state.get("phase") != "llm_running":
        raise RuntimeError(f"No active LLM timer exists for {scenario.name}.")
    assert_files_unchanged(state["file_hashes"])  # type: ignore[arg-type]
    healthy, missing, errors = inspect_goal(scenario.compose_file)
    if not healthy:
        print("The environment is not resolved; the LLM timer is still running.")
        print("Missing goal facts: " + ", ".join(missing))
        if errors:
            print("Inspection errors: " + "; ".join(errors))
        return 2

    started = datetime.fromisoformat(str(state["started_utc"]))
    elapsed = (utc_now() - started).total_seconds()
    state.update({"phase": "llm_complete", "llm_seconds": round(elapsed, 3), "llm_success": True})
    save_state(state)
    print(f"LLM repair resolved and verified in {elapsed:.3f} seconds.")
    print(f"Next: py -3.11 {Path(__file__).resolve()} run-app --scenario {scenario.name}")
    return 0


def cmd_run_app(scenario):
    state = load_state()
    if state.get("scenario") != scenario.name or state.get("phase") != "llm_complete":
        raise RuntimeError("The LLM trial must complete successfully before the app trial.")
    assert_files_unchanged(state["file_hashes"])  # type: ignore[arg-type]

    state.update(run_app_trial(scenario))
    state["phase"] = "complete"
    state["completed_utc"] = utc_now().isoformat()
    results = read_json(RESULTS_FILE, {"runs": []})
    results["runs"].append(state.copy())  # type: ignore[index]
    write_json(RESULTS_FILE, results)
    save_state(state)

    print(f"Results saved to {RESULTS_FILE}")
    return 0 if state["app_success"] else 2


def run_app_trial(scenario):
    """Recreate one broken fixture, time the app, and independently verify it."""

    print("Recreating the identical broken environment (not timed)...")
    ensure_daemon()
    scenario.setup(scenario)
    missing = assert_broken(scenario)
    print("Missing goal facts: " + ", ".join(missing))

    started = time.perf_counter()
    return_code = execute_until_resolved(str(scenario.compose_file), Limits())
    elapsed = time.perf_counter() - started
    healthy, final_missing, errors = inspect_goal(scenario.compose_file)
    success = return_code == 0 and healthy

    if success:
        print(f"App repair resolved and independently verified in {elapsed:.3f} seconds.")
    else:
        print(f"App repair failed after {elapsed:.3f} seconds.")
        print("Missing goal facts: " + ", ".join(final_missing))
        if errors:
            print("Inspection errors: " + "; ".join(errors))
    return {
        "app_seconds": round(elapsed, 3),
        "app_success": success,
        "app_return_code": return_code,
    }


def cmd_run_app_first(scenario):
    state = load_state()
    if state and state.get("phase") not in {"complete", "aborted"}:
        raise RuntimeError(
            f"Finish or abort the active {state.get('scenario')} trial before starting another."
        )

    state = {
        "scenario": scenario.name,
        "order": "app-first",
        "phase": "app_running",
        "file_hashes": file_hashes(scenario),
    }
    save_state(state)
    state.update(run_app_trial(scenario))
    state["phase"] = "app_complete"
    save_state(state)
    print(f"Next: py -3.11 {Path(__file__).resolve()} prepare-llm-after-app --scenario {scenario.name}")
    return 0 if state["app_success"] else 2


def cmd_prepare_llm_after_app(scenario):
    state = load_state()
    if state.get("scenario") != scenario.name or state.get("phase") != "app_complete":
        raise RuntimeError("The app-first trial must complete before preparing the LLM trial.")
    assert_files_unchanged(state["file_hashes"])  # type: ignore[arg-type]

    print("Recreating the identical broken environment for the LLM (not timed)...")
    ensure_daemon()
    scenario.setup(scenario)
    missing = assert_broken(scenario)
    started = utc_now()
    state.update({"phase": "llm_running_after_app", "started_utc": started.isoformat()})
    save_state(state)

    print(f"LLM timer started at {started.isoformat()}")
    print(f"Compose file: {scenario.compose_file.resolve()}")
    print("Missing goal facts: " + ", ".join(missing))
    print("Repair this environment by actually executing Docker commands.")
    print(
        f"When it is resolved, run: py -3.11 {Path(__file__).resolve()} "
        f"finish-llm-after-app --scenario {scenario.name}"
    )
    return 0


def cmd_finish_llm_after_app(scenario):
    state = load_state()
    if state.get("scenario") != scenario.name or state.get("phase") != "llm_running_after_app":
        raise RuntimeError(f"No app-first LLM timer exists for {scenario.name}.")
    assert_files_unchanged(state["file_hashes"])  # type: ignore[arg-type]
    healthy, missing, errors = inspect_goal(scenario.compose_file)
    if not healthy:
        print("The environment is not resolved; the LLM timer is still running.")
        print("Missing goal facts: " + ", ".join(missing))
        if errors:
            print("Inspection errors: " + "; ".join(errors))
        return 2

    started = datetime.fromisoformat(str(state["started_utc"]))
    elapsed = (utc_now() - started).total_seconds()
    state.update(
        {
            "phase": "complete",
            "llm_seconds": round(elapsed, 3),
            "llm_success": True,
            "completed_utc": utc_now().isoformat(),
        }
    )
    results = read_json(RESULTS_FILE, {"runs": []})
    results["runs"].append(state.copy())  # type: ignore[index]
    write_json(RESULTS_FILE, results)
    save_state(state)
    print(f"LLM repair resolved and verified in {elapsed:.3f} seconds.")
    print(f"Results saved to {RESULTS_FILE}")
    return 0


def cmd_status():
    state = load_state()
    if not state:
        print("No benchmark trial is active.")
    else:
        print(json.dumps(state, indent=2))
    return 0


def cmd_results():
    results = read_json(RESULTS_FILE, {"runs": []})
    runs = results.get("runs", [])  # type: ignore[union-attr]
    if not runs:
        print("No completed paired trials yet.")
        return 0
    print(f"{'order':10} {'scenario':16} {'LLM seconds':>12} {'app seconds':>12} {'app vs LLM':>12}")
    for item in runs:
        llm = float(item["llm_seconds"])
        app = float(item["app_seconds"])
        ratio = app / llm if llm else float("inf")
        order = item.get("order", "llm-first")
        print(f"{order:10} {item['scenario']:16} {llm:12.3f} {app:12.3f} {ratio:11.2f}x")
    return 0


def cmd_abort():
    state = load_state()
    if state:
        state["phase"] = "aborted"
        save_state(state)
    print("Active trial marked aborted. Completed results were preserved.")
    return 0


def cmd_cleanup():
    ensure_daemon()
    seen = set()
    for scenario in SCENARIOS.values():
        for path in (scenario.compose_file, *scenario.extra_files):
            if path not in seen:
                clean_project(path)
                seen.add(path)
    print("Removed benchmark Compose projects. Result files were preserved.")
    return 0


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "list",
            "prepare-llm",
            "finish-llm",
            "run-app",
            "run-app-first",
            "prepare-llm-after-app",
            "finish-llm-after-app",
            "status",
            "results",
            "abort",
            "cleanup",
        ),
    )
    parser.add_argument("--scenario", choices=tuple(SCENARIOS))
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        if args.command == "list":
            return cmd_list()
        if args.command == "status":
            return cmd_status()
        if args.command == "results":
            return cmd_results()
        if args.command == "abort":
            return cmd_abort()
        if args.command == "cleanup":
            return cmd_cleanup()

        scenario = require_scenario(args.scenario)
        if args.command == "prepare-llm":
            return cmd_prepare_llm(scenario)
        if args.command == "finish-llm":
            return cmd_finish_llm(scenario)
        if args.command == "run-app":
            return cmd_run_app(scenario)
        if args.command == "run-app-first":
            return cmd_run_app_first(scenario)
        if args.command == "prepare-llm-after-app":
            return cmd_prepare_llm_after_app(scenario)
        return cmd_finish_llm_after_app(scenario)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
