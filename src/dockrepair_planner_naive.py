"""The non-dependency-aware baseline planner.

This module intentionally does NOT know about service dependencies
(`depends_on` conditions) or resource-existence preconditions (a network or
volume must exist before something attaches to it). It proposes the first
action whose only requirement is the daemon/compose-valid baseline, executes
it against real Docker, and lets `execute_until_resolved`'s existing
re-inspect / exclude-on-failure / replan loop discover the correct ordering
the hard way -- by trying an action, having it fail against the live
daemon, excluding that (state, action) pair, and trying the next one.

This is the fair "no dependency information" comparison arm: it uses the
exact same action catalog, costs, executors, and execution/verification
loop as `dockrepair_planner.search`. The ONLY difference is that ordering
constraints are discovered through real failure instead of being known in
advance from `requires`.
"""

from dockrepair_data import Plan
from dockrepair_planner import BASE, build_actions, build_goal


def _is_naively_allowed(action):
    # A naive planner only knows the universal baseline (daemon reachable,
    # compose valid). It does NOT know about depends_on conditions or
    # resource-existence preconditions -- those were folded into
    # `action.requires` by the dependency-aware builder, so here we simply
    # refuse to consult anything beyond BASE when deciding legality.
    return action.requires <= BASE


def search_naive(environment, excluded=frozenset()):
    """Propose exactly one action per call, in fixed catalog order.

    No search, no cost minimization, no dependency lookahead. This mirrors
    a planner that does not have an explicit dependency graph: it picks the
    first plausible-looking action and lets the real world tell it whether
    that was legal, via `execute_until_resolved`'s existing failure/replan
    machinery.
    """

    goal = build_goal(environment)
    initial = environment.facts
    if "compose_valid" not in initial:
        return Plan("blocked"), goal

    target = goal if environment.daemon_reachable else frozenset({"daemon_reachable"})
    if target <= initial:
        return Plan("already_healthy"), goal
    if environment.daemon_reachable and environment.blocked_reasons:
        return Plan("blocked"), goal

    candidates = build_actions(environment, initial, excluded)
    # Only actions whose requirements do not exceed BASE are ones a
    # dependency-blind planner could legally attempt without foreknowledge.
    # Everything else (batch reconciliation, network-gated connects, etc.)
    # requires dependency knowledge to even propose safely, so a truly
    # naive planner is restricted to the single-service "try it and see"
    # actions -- reconcile/start/restart/recreate one container at a time.
    naive_candidates = [action for action in candidates if _is_naively_allowed(action)]

    if not naive_candidates:
        return Plan("unreachable", explored_states=1), goal

    # Fixed catalog order (already sorted by service name upstream) stands
    # in for "no prioritization by dependency knowledge." We do not sort by
    # cost here on purpose -- that would smuggle back in some of the
    # planning intelligence we are trying to withhold from this baseline.
    chosen = naive_candidates[0]
    return Plan("plan_found", (chosen,), explored_states=1), goal