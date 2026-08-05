from dockrepair_data import Plan
from dockrepair_planner import build_actions, build_goal

_DEP_PREFIXES = (
    "healthy:",
    "running:",
    "completed_successfully:",
    "endpoint_ready:",
    "health_pending:",
    "completion_pending:",
)


def _without_dependency_facts(requires):
    return frozenset(
        item for item in requires
        if not any(item.startswith(prefix) for prefix in _DEP_PREFIXES)
    )


def _is_naively_allowed(action, state):
    if action.identity[:1] == ("reconcile_batch",):
        return False
    return _without_dependency_facts(action.requires) <= state


def search_naive(environment, excluded=frozenset()):
    goal = build_goal(environment)
    initial = environment.facts
    if "compose_valid" not in initial:
        return Plan("blocked"), goal

    target = goal if environment.daemon_reachable else frozenset({"daemon_reachable"})
    if target <= initial:
        return Plan("already_healthy"), goal
    if environment.daemon_reachable and environment.blocked_reasons:
        return Plan("blocked"), goal

    candidates = [
        action for action in build_actions(environment, initial, excluded)
        if _is_naively_allowed(action, initial)
    ]
    if not candidates:
        return Plan("unreachable", explored_states=1), goal
    return Plan("plan_found", (candidates[0],), explored_states=1), goal
