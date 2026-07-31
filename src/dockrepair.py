"""Command-line entry point for planning or executing Compose repairs.

main() is the starting point. It collects Docker state, calls planner.search(),
then either prints the returned plan or executes one action at a time.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from dockrepair_docker import collect_environment, compose_arguments, compose_command
from dockrepair_planner import fact, search


@dataclass(frozen=True)
class Limits:
    action_timeout: float = 180.0
    health_timeout: float = 30.0
    max_actions: int = 20


def _command(environment, action):
    if not action.arguments:
        return "Start the local Docker Engine"
    return compose_command(environment, *action.arguments)


def print_plan(environment, plan, goal):
    # Planning mode is read-only: show the commands without running them.
    print(f"Project: {environment.project_name}")
    print(f"Status: {plan.status}")
    print("Missing goal facts: " + (", ".join(sorted(goal - environment.facts)) or "none"))
    if environment.errors:
        print("Collection notes: " + "; ".join(environment.errors))

    print("\nProposed commands (nothing was executed):")
    if not plan.actions:
        print("  <none>")
    for number, action in enumerate(plan.actions, 1):
        label = "manual check" if action.manual else "proposed"
        print(f"  {number}. {action.name} [{label}, cost={action.cost}]")
        print(f"     {_command(environment, action)}")
    print(f"\nTotal cost: {plan.total_cost}; explored states: {plan.explored_states}")
    print(plan.message)


def _docker_daemon_ready():
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _start_docker_engine(timeout):
    # Choose the normal Docker launcher for the current operating system.
    if _docker_daemon_ready():
        return True, "Docker Engine is already reachable."

    launcher = None
    if os.name == "nt":
        desktop = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Docker/Docker/Docker Desktop.exe"
        launcher = [str(desktop)] if desktop.is_file() else None
    elif sys.platform == "darwin":
        desktop = Path("/Applications/Docker.app/Contents/MacOS/Docker")
        launcher = [str(desktop)] if desktop.is_file() else None
    elif shutil.which("systemctl"):
        launcher = ["systemctl", "start", "docker"]
    if not launcher:
        return False, "No supported Docker Engine launcher was found."

    try:
        process = subprocess.Popen(
            launcher, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if launcher[0] == "systemctl":
            process.wait(timeout=min(timeout, 30))
            if process.returncode:
                return False, f"systemctl start docker failed with exit code {process.returncode}."
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"Could not start Docker Engine: {error}"

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _docker_daemon_ready():
            return True, "Docker Engine became reachable."
        time.sleep(0.5)
    return False, f"Docker Engine did not become reachable within {timeout:g} seconds."


def _wait_for_facts(compose_file, wanted, timeout):
    # Poll fresh Docker state until reality contains the predicted facts.
    deadline = time.monotonic() + timeout
    environment = collect_environment(compose_file)
    while not wanted <= environment.facts and time.monotonic() < deadline:
        time.sleep(0.5)
        environment = collect_environment(compose_file)
    return wanted <= environment.facts, environment


def execute_action(environment, action, limits=Limits()):
    # Engine startup is represented as an action without Compose arguments.
    if "daemon_reachable" in action.adds and not action.arguments:
        succeeded, message = _start_docker_engine(limits.action_timeout)
        return succeeded, message, collect_environment(environment.compose_file)

    # A manual planner edge means observe health; it runs no mutating command.
    if action.manual:
        wanted = frozenset(item for item in action.adds if item.startswith("healthy:"))
        succeeded, observed = _wait_for_facts(environment.compose_file, wanted, limits.health_timeout)
        message = "Health check passed." if succeeded else f"Health did not converge within {limits.health_timeout:g} seconds."
        return succeeded, message, observed

    # Normal actions become real `docker compose` subprocesses here.
    try:
        result = subprocess.run(
            compose_arguments(environment, *action.arguments),
            cwd=Path(environment.compose_file).parent,
            capture_output=True, text=True, timeout=limits.action_timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return False, str(error), collect_environment(environment.compose_file)

    output = (result.stdout if result.returncode == 0 else result.stderr).strip()
    # Never trust predicted effects: inspect Docker again after the command.
    observed = collect_environment(environment.compose_file)
    pending = {
        item.split(":", 1)[1]
        for item in action.adds
        if item.startswith("health_pending:")
    }
    unhealthy = {name for name in pending if fact("unhealthy", name) in observed.facts}
    if result.returncode == 0 and unhealthy:
        wanted = frozenset(fact("healthy", name) for name in unhealthy)
        converged, observed = _wait_for_facts(environment.compose_file, wanted, limits.health_timeout)
        if not converged:
            note = f"Health did not converge within {limits.health_timeout:g} seconds."
            return False, f"{output}\n{note}".strip(), observed
    return result.returncode == 0, output, observed


def _effect_was_observed(action, environment):
    # Confirm that live state contains each effect promised by the action.
    for expected in action.adds:
        if expected.startswith("health_pending:"):
            name = expected.split(":", 1)[1]
            if not any(fact(kind, name) in environment.facts for kind in ("health_pending", "healthy")):
                return False
        elif expected not in environment.facts:
            return False
    return True


def execute_until_resolved(compose_file, limits=Limits()):
    # This is the main repair loop: plan, run one action, inspect, and replan.
    started = time.perf_counter()
    environment = collect_environment(compose_file)
    excluded = set()
    previous_mutation = None

    for step in range(1, limits.max_actions + 1):
        plan, goal = search(environment, frozenset(excluded))
        if goal <= environment.facts:
            elapsed = time.perf_counter() - started
            print(f"Resolved and verified in {elapsed:.3f} seconds after {step - 1} action(s).")
            return 0
        if not plan.actions:
            print(f"Repair stopped: {plan.message}")
            if environment.errors:
                print("Collection notes: " + "; ".join(environment.errors))
            return 2

        # Run only the first edge because the observed state may then change.
        action = plan.actions[0]
        print(f"{step}. Executing: {action.name}\n   {_command(environment, action)}")
        succeeded, output, environment = execute_action(environment, action, limits)
        if output:
            print(f"   {output}")

        if succeeded and not action.manual:
            previous_mutation = action.name
            if not _effect_was_observed(action, environment):
                succeeded = False
                print("   Predicted effects were not observed.")

        # Failed edges are excluded so search can choose a fallback path.
        if not succeeded:
            rejected = previous_mutation if action.manual else action.name
            if rejected:
                excluded.add(rejected)
                print(f"   Removing failed graph edge: {rejected}")
            print("   Action failed; replanning.")
        elif action.manual:
            previous_mutation = None

    print(f"Repair stopped after the limit of {limits.max_actions} actions.")
    return 2


def main():
    # CLI setup lives here; no Docker work happens while modules are imported.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-f", "--compose-file", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--action-timeout", type=float, default=180.0)
    parser.add_argument("--health-timeout", type=float, default=30.0)
    parser.add_argument("--max-actions", type=int, default=20)
    args = parser.parse_args()
    limits = Limits(args.action_timeout, args.health_timeout, args.max_actions)

    # Execution mode loops until healthy; default mode only prints a plan.
    if args.execute:
        return execute_until_resolved(args.compose_file, limits)
    environment = collect_environment(args.compose_file)
    plan, goal = search(environment)
    print_plan(environment, plan, goal)
    return 2 if plan.status in {"blocked", "unreachable"} else 0


if __name__ == "__main__":
    # `python -m dockrepair ...` starts here and immediately calls main().
    raise SystemExit(main())
