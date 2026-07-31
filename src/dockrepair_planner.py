"""Create repair actions and find the cheapest path to a healthy state.

Start with search() at the bottom. The functions above it build the goal and
the possible actions that search() needs.
"""

import heapq
from itertools import count

from dockrepair_data import Action, Plan


# Every successful repair needs a valid Compose file and a reachable engine.
BASE = frozenset({"daemon_reachable", "compose_valid"})


def fact(kind, service):
    # Facts are strings such as "running:api" or "healthy:database".
    return f"{kind}:{service}"


def facts(service, *kinds):
    return frozenset(fact(kind, service) for kind in kinds)


def build_goal(environment):
    # Describe the fully repaired environment as a set of required facts.
    goal = set(BASE)
    for name, service in environment.services.items():
        goal.update(facts(name, "container_exists", "config_current", "running"))
        if service.needs_healthcheck:
            goal.add(fact("healthy", name))
    return frozenset(goal)


def _requirements(service):
    # Translate depends_on rules into facts that must already be true.
    return frozenset(
        fact("healthy" if condition == "service_healthy" else "running", dependency)
        for dependency, condition in service.dependencies
    )


def _batch_action(environment):
    # Compose can often reconcile several broken services in one command.
    broken = [
        name
        for name in sorted(environment.services)
        if not facts(name, "container_exists", "config_current", "running")
        <= environment.facts
    ]
    if len(broken) < 2:
        return None

    adds = set()
    for name in broken:
        adds.update(facts(name, "container_exists", "config_current", "running"))
        if environment.services[name].needs_healthcheck:
            adds.add(fact("healthy", name))

    return Action(
        "Reconcile services together: " + ", ".join(broken),
        ("up", "-d", "--wait", *broken),
        2 + len(broken), BASE, frozenset(adds),
    )


def _service_actions(name, service, state):
    # Name the facts used by this service's possible state transitions.
    exists, current, running, healthy, pending, unhealthy = (
        fact(kind, name)
        for kind in ("container_exists", "config_current", "running", "healthy", "health_pending", "unhealthy")
    )
    required = BASE | _requirements(service)
    transition = facts(name, "container_exists", "config_current", "running")
    if service.needs_healthcheck:
        transition |= {pending}
    actions = []

    # Missing or outdated containers need Compose reconciliation.
    if exists not in state or current not in state:
        actions.append(Action(
            f"Reconcile {name}", ("up", "-d", "--no-deps", name),
            6 if exists not in state else 7, required, transition,
        ))
    # A stopped container can be started cheaply or recreated as a fallback.
    elif running not in state:
        additions = facts(name, "running", "health_pending") if service.needs_healthcheck else frozenset({running})
        actions.extend((
            Action(f"Start {name}", ("start", name), 2, required | {exists, current}, additions),
            Action(
                f"Recreate {name}", ("up", "-d", "--force-recreate", "--no-deps", name),
                8, required | {exists, current}, transition,
            ),
        ))
    # Prefer restart for unhealthy containers; recreation costs more.
    elif unhealthy in state:
        additions = facts(name, "running", "health_pending") if service.needs_healthcheck else frozenset({running})
        actions.extend((
            Action(
                f"Restart {name}", ("restart", name), 3,
                required | {exists, current, running, unhealthy}, facts(name, "running", "health_pending"),
            ),
            Action(
                f"Recreate {name}", ("up", "-d", "--force-recreate", "--no-deps", name),
                8, required | {exists, current, running}, additions,
            ),
        ))

    # Health is observed after mutation instead of assumed by the planner.
    if service.needs_healthcheck and healthy not in state:
        actions.append(Action(
            f"Verify {name} health", ("ps", name), 1,
            BASE | {exists, current, running, pending}, frozenset({healthy}),
            manual=True,
        ))
    return actions


def build_actions(environment, excluded=frozenset()):
    # Build every repair transition currently available to the search.
    if not environment.daemon_reachable:
        actions = [Action("Start Docker Engine", (), 1, frozenset(), frozenset({"daemon_reachable"}))]
    else:
        actions = []
        batch = _batch_action(environment)
        if batch:
            actions.append(batch)
        for name, service in sorted(environment.services.items()):
            actions.extend(_service_actions(name, service, environment.facts))
    return tuple(action for action in actions if action.name not in excluded)


def search(environment, excluded=frozenset()):
    """Return the cheapest plan from the observed facts to the goal facts."""

    # The environment collector provides the starting facts; build the target.
    goal = build_goal(environment)
    initial = environment.facts
    if "compose_valid" not in initial:
        return Plan("blocked"), goal

    # If Docker is down, first return the small prerequisite startup plan.
    target = goal if environment.daemon_reachable else frozenset({"daemon_reachable"})
    if target <= initial:
        return Plan("already_healthy"), goal

    actions = build_actions(environment, excluded)

    # Queue entries are: total cost, tie-breaker, reached facts, chosen actions.
    order = count()
    frontier = [(0, next(order), initial, ())]
    best = {initial: 0}
    explored = 0

    while frontier:
        cost, _, state, path = heapq.heappop(frontier)
        # Skip an older queue entry when a cheaper path reached the same state.
        if cost != best.get(state):
            continue
        explored += 1
        if target <= state:
            status = "plan_found" if environment.daemon_reachable else "prerequisite_plan"
            return Plan(status, path, explored), goal

        # Expand the current state with every action whose requirements are met.
        for action in actions:
            if not action.is_allowed(state):
                continue
            next_state = action.apply(state)
            next_cost = cost + action.cost
            if next_state == state or next_cost >= best.get(next_state, float("inf")):
                continue
            # Remember only the cheapest known path to each symbolic state.
            best[next_state] = next_cost
            heapq.heappush(frontier, (next_cost, next(order), next_state, (*path, action)))

    return Plan("unreachable", explored_states=explored), goal
