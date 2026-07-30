"""Build and print a symbolic Docker Compose repair plan."""

from __future__ import annotations

import argparse
import heapq
import os
import shutil
import subprocess
import sys
import time
from itertools import count
from pathlib import Path

from dockrepair_data import Action, Environment, Plan, SearchNode
from dockrepair_docker import collect_environment, compose_arguments, compose_command


def fact(kind: str, service: str) -> str:
    """Create a readable symbolic fact such as ``running:web``."""

    return f"{kind}:{service}"


def build_goal(environment: Environment) -> frozenset[str]:
    """Compose defines what services should exist and be running."""

    goal = {"daemon_reachable", "compose_valid"}
    for service in environment.services.values():
        goal.update(
            {
                fact("container_exists", service.name),
                fact("config_current", service.name),
                fact("running", service.name),
            }
        )
        if service.needs_healthcheck:
            goal.add(fact("healthy", service.name))
    return frozenset(goal)


def dependency_facts(environment: Environment, service_name: str) -> set[str]:
    """Translate Compose ``depends_on`` rules into action requirements."""

    required = set()
    for dependency, condition in environment.services[service_name].dependencies:
        kind = "healthy" if condition == "service_healthy" else "running"
        required.add(fact(kind, dependency))
    return required


def build_actions(
    environment: Environment,
    excluded_actions: frozenset[str] = frozenset(),
) -> tuple[Action, ...]:
    """Create relevant alternatives, minus actions that failed during execution."""

    state = environment.facts
    if not environment.daemon_reachable:
        actions = (
            Action(
                name="Start Docker Engine",
                command="Start the local Docker Engine using the supported platform launcher",
                arguments=(),
                cost=1,
                requires=frozenset(),
                adds=frozenset({"daemon_reachable"}),
                manual=False,
            ),
        )
        return tuple(action for action in actions if action.name not in excluded_actions)

    actions = []

    def add_action(
        name: str,
        command_arguments: tuple[str, ...],
        cost: int,
        requires: set[str],
        adds: set[str],
        removes: set[str] | None = None,
        *,
        manual: bool = False,
    ) -> None:
        """Keep the action declarations below short and uniform."""

        actions.append(
            Action(
                name=name,
                command=compose_command(environment, *command_arguments),
                arguments=compose_arguments(environment, *command_arguments),
                cost=cost,
                requires=frozenset(requires),
                adds=frozenset(adds),
                removes=frozenset(removes or set()),
                manual=manual,
            )
        )

    services = sorted(environment.services.values(), key=lambda item: item.name)

    # Compose can reconcile several broken services and their dependency order in
    # one process. This creates a genuine alternative path through the graph:
    # one batch edge versus several lower-blast-radius service edges.
    batch_services = []
    for service in services:
        name = service.name
        if any(
            fact(kind, name) not in state
            for kind in ("container_exists", "config_current", "running")
        ):
            batch_services.append(service)

    if len(batch_services) >= 2:
        additions = set()
        removals = set()
        names = tuple(service.name for service in batch_services)
        for service in batch_services:
            additions.update(
                {
                    fact("container_exists", service.name),
                    fact("config_current", service.name),
                    fact("running", service.name),
                }
            )
            if service.needs_healthcheck:
                # `up --wait` observes health before it returns successfully.
                additions.add(fact("healthy", service.name))
            removals.update(
                {
                    fact("health_pending", service.name),
                    fact("unhealthy", service.name),
                }
            )
        add_action(
            "Reconcile services together: " + ", ".join(names),
            ("up", "-d", "--wait", *names),
            2 + len(names),
            {"daemon_reachable", "compose_valid"},
            additions,
            removals,
        )

    for service in services:
        name = service.name
        basic = {"daemon_reachable", "compose_valid"}
        dependencies = dependency_facts(environment, name)

        exists = fact("container_exists", name)
        current = fact("config_current", name)
        running = fact("running", name)
        healthy = fact("healthy", name)
        pending = fact("health_pending", name)
        unhealthy = fact("unhealthy", name)

        # Compose handles image pulling/building inside `up`; the planner only
        # decides which service to reconcile and in what order.
        transition = {exists, current, running}
        if service.needs_healthcheck:
            transition.add(pending)

        if exists not in state or current not in state:
            missing = exists not in state
            add_action(
                f"Reconcile {name}",
                ("up", "-d", "--no-deps", name),
                6 if missing else 7,
                basic | dependencies,
                transition,
                {healthy, unhealthy},
            )
        elif running not in state:
            additions = {running, pending} if service.needs_healthcheck else {running}
            add_action(
                f"Start {name}",
                ("start", name),
                2,
                basic | dependencies | {exists, current},
                additions,
                {healthy, unhealthy},
            )
            add_action(
                f"Recreate {name}",
                ("up", "-d", "--force-recreate", "--no-deps", name),
                8,
                basic | dependencies | {exists, current},
                transition,
                {healthy, unhealthy},
            )
        elif unhealthy in state:
            add_action(
                f"Restart {name}",
                ("restart", name),
                3,
                basic | dependencies | {exists, current, running, unhealthy},
                {running, pending},
                {healthy, unhealthy},
            )
            add_action(
                f"Recreate {name}",
                ("up", "-d", "--force-recreate", "--no-deps", name),
                8,
                basic | dependencies | {exists, current, running},
                {running, pending} if service.needs_healthcheck else {running},
                {healthy, unhealthy},
            )

        if service.needs_healthcheck and healthy not in state:
            add_action(
                f"Verify {name} health",
                ("ps", name),
                1,
                {"daemon_reachable", exists, current, running, pending},
                {healthy},
                {pending, unhealthy},
                manual=True,
            )

    return tuple(action for action in actions if action.name not in excluded_actions)


def search(
    environment: Environment,
    excluded_actions: frozenset[str] = frozenset(),
) -> tuple[Plan, frozenset[str]]:
    """Find the lowest-cost path through the symbolic state graph."""

    goal = build_goal(environment)
    initial = environment.facts
    if "compose_valid" not in initial:
        return Plan("blocked", (), 0, 0, "Compose configuration is invalid."), goal

    # When Docker is stopped, plan only the prerequisite. A fresh snapshot is
    # required before any container actions can be planned safely.
    effective_goal = frozenset({"daemon_reachable"}) if not environment.daemon_reachable else goal
    if effective_goal <= initial:
        return Plan("already_healthy", (), 0, 0, "The goal is already satisfied."), goal

    start = SearchNode(initial, (), 0)
    frontier = []
    serial = count()  # Prevents heapq from comparing SearchNode objects on ties.
    heapq.heappush(frontier, (0, (), next(serial), start))

    # This is graph search, not a decision tree: different action sequences can
    # reach the same state. best_cost prevents exploring a worse duplicate.
    best_cost = {initial: 0}
    actions = build_actions(environment, excluded_actions)
    explored = 0

    while frontier and explored < 2_000:
        _, _, _, node = heapq.heappop(frontier)
        if node.cost != best_cost.get(node.state):
            continue
        explored += 1

        if effective_goal <= node.state:
            status = "prerequisite_plan" if not environment.daemon_reachable else "plan_found"
            return Plan(
                status,
                node.actions,
                node.cost,
                explored,
                "Predicted effects must be checked after each command.",
            ), goal

        for action in actions:
            if not action.is_allowed(node.state):
                continue

            next_state = action.apply(node.state)
            next_cost = node.cost + action.cost
            if next_state == node.state or next_cost >= best_cost.get(next_state, float("inf")):
                continue

            best_cost[next_state] = next_cost
            next_actions = (*node.actions, action)
            next_node = SearchNode(next_state, next_actions, next_cost)
            action_names = tuple(item.name for item in next_actions)
            heapq.heappush(frontier, (next_cost, action_names, next(serial), next_node))

    return Plan(
        "unreachable",
        (),
        0,
        explored,
        "The small action catalog cannot reach the goal.",
    ), goal


def print_plan(environment: Environment, plan: Plan, goal: frozenset[str]) -> None:
    """Print only the facts and commands needed to understand the result."""

    missing = goal - environment.facts
    print(f"Project: {environment.project_name}")
    print(f"Status: {plan.status}")
    print("Missing goal facts: " + (", ".join(sorted(missing)) or "none"))

    if environment.errors:
        print("Collection notes: " + "; ".join(environment.errors))

    print("\nProposed commands (nothing was executed):")
    if not plan.actions:
        print("  <none>")
    for number, action in enumerate(plan.actions, start=1):
        label = "manual check" if action.manual else "proposed"
        print(f"  {number}. {action.name} [{label}, cost={action.cost}]")
        print(f"     {action.command}")

    print(f"\nTotal cost: {plan.total_cost}; explored states: {plan.explored_states}")
    print(plan.message)


def _docker_daemon_ready() -> bool:
    """Probe the engine without leaking expected startup errors to the console."""

    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _start_docker_engine(timeout: float) -> tuple[bool, str]:
    """Start the local Docker engine when a supported launcher is available."""

    if _docker_daemon_ready():
        return True, "Docker Engine is already reachable."

    launcher: list[str] | None = None
    if os.name == "nt":
        desktop = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Docker" / "Docker" / "Docker Desktop.exe"
        if desktop.is_file():
            launcher = [str(desktop)]
    elif sys.platform == "darwin":
        desktop = Path("/Applications/Docker.app/Contents/MacOS/Docker")
        if desktop.is_file():
            launcher = [str(desktop)]
    elif shutil.which("systemctl"):
        launcher = ["systemctl", "start", "docker"]

    if launcher is None:
        return False, "No supported Docker Engine launcher was found."

    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            launcher,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        if launcher[0] == "systemctl":
            process.wait(timeout=min(timeout, 30))
            if process.returncode != 0:
                return False, f"systemctl start docker failed with exit code {process.returncode}."
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"Could not start Docker Engine: {error}"

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _docker_daemon_ready():
            return True, "Docker Engine became reachable."
        time.sleep(0.5)
    return False, f"Docker Engine did not become reachable within {timeout:g} seconds."


def _wait_for_facts(compose_file: str, wanted: frozenset[str], timeout: float) -> tuple[bool, Environment]:
    """Wait until real inspection, rather than a predicted effect, proves facts."""

    deadline = time.monotonic() + timeout
    environment = collect_environment(compose_file)
    while not wanted <= environment.facts and time.monotonic() < deadline:
        time.sleep(0.5)
        environment = collect_environment(compose_file)
    return wanted <= environment.facts, environment


def execute_action(
    environment: Environment,
    action: Action,
    timeout: float,
    health_timeout: float,
) -> tuple[bool, str, Environment]:
    """Execute one planned action and return a freshly observed environment."""

    if "daemon_reachable" in action.adds and not action.arguments:
        succeeded, message = _start_docker_engine(timeout)
        return succeeded, message, collect_environment(environment.compose_file)

    health_facts = frozenset(item for item in action.adds if item.startswith("healthy:"))
    if action.manual and health_facts:
        succeeded, observed = _wait_for_facts(environment.compose_file, health_facts, health_timeout)
        message = (
            "Health check passed."
            if succeeded
            else f"Health did not converge within {health_timeout:g} seconds."
        )
        return succeeded, message, observed

    try:
        result = subprocess.run(
            action.arguments,
            cwd=Path(environment.compose_file).parent,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return False, str(error), collect_environment(environment.compose_file)

    output = (result.stdout if result.returncode == 0 else result.stderr).strip()
    observed = collect_environment(environment.compose_file)
    pending_services = {
        item.split(":", 1)[1]
        for item in action.adds
        if item.startswith("health_pending:")
    }
    unhealthy_services = {
        name for name in pending_services if fact("unhealthy", name) in observed.facts
    }
    if result.returncode == 0 and unhealthy_services:
        wanted = frozenset(fact("healthy", name) for name in unhealthy_services)
        converged, observed = _wait_for_facts(environment.compose_file, wanted, health_timeout)
        if not converged:
            note = f"Health did not converge within {health_timeout:g} seconds."
            output = f"{output}\n{note}".strip()
            return False, output, observed
    return result.returncode == 0, output, observed


def _action_services(action: Action) -> frozenset[str]:
    """Return services whose runtime state an action predicts it will change."""

    kinds = {"container_exists", "config_current", "running", "health_pending", "healthy"}
    return frozenset(
        name
        for item in action.adds
        if ":" in item
        for kind, name in (item.split(":", 1),)
        if kind in kinds
    )


def _effect_was_observed(action: Action, environment: Environment) -> bool:
    """Check predicted core effects while accepting any terminal health state."""

    for expected in action.adds:
        if expected.startswith("health_pending:"):
            name = expected.split(":", 1)[1]
            if not any(
                fact(kind, name) in environment.facts
                for kind in ("health_pending", "healthy")
            ):
                return False
        elif expected not in environment.facts:
            return False
    return True


def execute_until_resolved(
    compose_file: str,
    timeout: float,
    max_actions: int,
    health_timeout: float = 30.0,
) -> int:
    """Execute, verify, remove failed graph edges, and search for another path."""

    started = time.perf_counter()
    environment = collect_environment(compose_file)
    excluded_actions: set[str] = set()
    ineffective_attempts: dict[str, int] = {}
    last_mutation_by_service: dict[str, str] = {}

    for step in range(1, max_actions + 1):
        plan, goal = search(environment, frozenset(excluded_actions))
        if goal <= environment.facts:
            elapsed = time.perf_counter() - started
            print(f"Resolved and verified in {elapsed:.3f} seconds after {step - 1} action(s).")
            return 0
        if plan.status in {"blocked", "unreachable"} or not plan.actions:
            print(f"Repair stopped: {plan.message}")
            if environment.errors:
                print("Collection notes: " + "; ".join(environment.errors))
            return 2

        action = plan.actions[0]
        print(f"{step}. Executing: {action.name}")
        print(f"   {action.command}")
        succeeded, output, environment = execute_action(
            environment,
            action,
            timeout,
            health_timeout,
        )
        if output:
            print(f"   {output}")

        services = _action_services(action)
        if succeeded and not action.manual:
            for service in services:
                last_mutation_by_service[service] = action.name

            if not _effect_was_observed(action, environment):
                attempts = ineffective_attempts.get(action.name, 0) + 1
                ineffective_attempts[action.name] = attempts
                print(f"   Predicted effects were not observed (attempt {attempts}/2).")
                if attempts >= 2:
                    excluded_actions.add(action.name)
                    print(f"   Removing failed graph edge: {action.name}")

        if not succeeded:
            if action.manual:
                predecessors = {
                    last_mutation_by_service[service]
                    for service in services
                    if service in last_mutation_by_service
                }
                excluded_actions.update(predecessors)
                if predecessors:
                    print("   Verification rejected: " + ", ".join(sorted(predecessors)))
            else:
                excluded_actions.add(action.name)
            print("   Action failed; replanning without the rejected graph edge.")
            continue

        if action.manual:
            for service in services:
                last_mutation_by_service.pop(service, None)

    print(f"Repair stopped after the safety limit of {max_actions} actions.")
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Plan or execute a Docker Compose environment repair."
    )
    parser.add_argument("-f", "--compose-file", required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually execute actions, inspect after each one, and stop only when the goal is verified.",
    )
    parser.add_argument(
        "--action-timeout",
        type=float,
        default=180.0,
        help="Seconds allowed for each command, engine startup, or health wait (default: 180).",
    )
    parser.add_argument(
        "--health-timeout",
        type=float,
        default=30.0,
        help="Seconds allowed for a health transition before trying an alternative (default: 30).",
    )
    parser.add_argument("--max-actions", type=int, default=20)
    args = parser.parse_args()

    if args.execute:
        return execute_until_resolved(
            args.compose_file,
            args.action_timeout,
            args.max_actions,
            args.health_timeout,
        )

    environment = collect_environment(args.compose_file)
    plan, goal = search(environment)
    print_plan(environment, plan, goal)
    return 2 if plan.status in {"blocked", "unreachable"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
