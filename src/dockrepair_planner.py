"""Build deterministic Docker repair operators and find the cheapest + best plan."""

import heapq
from dataclasses import replace
from itertools import count

from dockrepair_data import Action, Environment, Plan, RepairCost


BASE = frozenset({"daemon_reachable", "compose_valid"})
OBSERVE_COST = RepairCost(0, 0, 0, 1)
MUTATE_COST = RepairCost(0, 1, 0, 1)
RESTART_COST = RepairCost(0, 2, 1, 1)

DEPENDENCY_FACTS = {
    "service_started": "running",
    "service_healthy": "healthy",
    "service_completed_successfully": "completed_successfully",
}


def fact(kind, service):
    return f"{kind}:{service}"


def facts(service, *kinds):
    return frozenset(fact(kind, service) for kind in kinds)


def _network_fact(kind, service, network):
    return f"{kind}:{service}:{network}"


def _mount_fact(kind, service, target):
    return f"{kind}:{service}:{target}"


def _port_fact(kind, service, port):
    return f"{kind}:{service}:{port.key}"


def _network_connect_arguments(service_name, container_name, network_actual_name):
    """Restore Compose service DNS by attaching with the service-name alias."""
    return (
        "network", "connect",
        "--alias", service_name,
        network_actual_name, container_name,
    )


def _requirements(service):
    return frozenset(
        fact(DEPENDENCY_FACTS[condition], dependency)
        for dependency, condition in service.dependencies
        if condition in DEPENDENCY_FACTS
    )


def _service_goal(name, service):
    goal = set(facts(name, "container_exists", "config_current"))
    if service.completion_required:
        goal.add(fact("completed_successfully", name))
    else:
        goal.add(fact("running", name))
    if service.needs_healthcheck and not service.completion_required:
        goal.add(fact("healthy", name))
    for network in service.networks:
        goal.add(f"network_exists:{network}")
        goal.add(_network_fact("network_connected", name, network))
    for mount in service.mounts:
        if mount.kind == "bind":
            goal.add(_mount_fact("bind_exists", name, mount.target))
        elif mount.kind == "volume":
            goal.add(f"volume_exists:{mount.source}")
        goal.add(_mount_fact("mount_attached", name, mount.target))
    for port in service.ports:
        if port.published is not None:
            goal.add(_port_fact("port_bound", name, port))
    if service.readiness and not service.completion_required:
        goal.add(fact("endpoint_ready", name))
    return frozenset(goal)


def build_goal(environment):
    goal = set(BASE)
    for name, service in environment.services.items():
        goal.update(_service_goal(name, service))
    return frozenset(goal)


def _resource_requirements(environment, name, service):
    required = set(BASE | _requirements(service))
    for network in service.networks:
        resource = (environment.networks or {}).get(network)
        if resource and resource.external:
            required.add(f"network_exists:{network}")
    for mount in service.mounts:
        if mount.kind == "bind":
            required.add(_mount_fact("bind_exists", name, mount.target))
        elif mount.kind == "volume":
            resource = (environment.volumes or {}).get(mount.source)
            if resource and resource.external:
                required.add(f"volume_exists:{mount.source}")
    for port in service.ports:
        if port.published is not None:
            required.add(_port_fact("port_available", name, port))
    return frozenset(required)


def _transition_effects(environment, name, service):
    additions = set(facts(name, "container_exists", "config_current", "running"))
    removals = set(facts(
        name, "stopped", "unhealthy", "healthy", "health_pending", "oom_killed",
    ))
    if service.completion_required:
        additions.add(fact("completion_pending", name))
        removals.update(facts(name, "completed_successfully", "completion_failed"))
    elif service.needs_healthcheck:
        additions.add(fact("health_pending", name))
    if service.readiness and not service.completion_required:
        additions.add(fact("endpoint_ready", name))
        removals.add(fact("readiness_pending", name))
    for network in service.networks:
        resource = (environment.networks or {}).get(network)
        if not resource or not resource.external:
            additions.add(f"network_exists:{network}")
        additions.add(_network_fact("network_connected", name, network))
    for mount in service.mounts:
        if mount.kind == "volume":
            resource = (environment.volumes or {}).get(mount.source)
            if not resource or not resource.external:
                additions.add(f"volume_exists:{mount.source}")
        additions.add(_mount_fact("mount_attached", name, mount.target))
    for port in service.ports:
        if port.published is not None:
            additions.add(_port_fact("port_bound", name, port))
    return frozenset(additions), frozenset(removals)


def _replacement_cost(state, name):
    running = fact("running", name) in state
    return RepairCost(1, 3, int(running), 1)


def _reconcile_cost(state, name):
    exists = fact("container_exists", name) in state
    current = fact("config_current", name) in state
    running = fact("running", name) in state
    if not exists or (current and not running):
        return MUTATE_COST
    if current and running:
        return RESTART_COST
    return _replacement_cost(state, name)


def _resource_actions(environment, state):
    actions = []
    used_networks = {
        network for service in environment.services.values() for network in service.networks
    }
    used_volumes = {
        mount.source
        for service in environment.services.values()
        for mount in service.mounts
        if mount.kind == "volume"
    }
    for name, resource in sorted((environment.networks or {}).items()):
        if name not in used_networks:
            continue
        exists = f"network_exists:{name}"
        if exists in state or resource.external:
            continue
        arguments = ["network", "create", "--label", f"com.docker.compose.project={environment.project_name}"]
        arguments.extend(("--label", f"com.docker.compose.network={name}"))
        if resource.driver:
            arguments.extend(("--driver", resource.driver))
        arguments.append(resource.actual_name)
        actions.append(Action(
            f"Create network {name}", tuple(arguments), MUTATE_COST, BASE, frozenset({exists}),
            identity=("create_network", name), executor="docker",
        ))
    for name, resource in sorted((environment.volumes or {}).items()):
        if name not in used_volumes:
            continue
        exists = f"volume_exists:{name}"
        if exists in state or resource.external:
            continue
        arguments = ["volume", "create", "--label", f"com.docker.compose.project={environment.project_name}"]
        arguments.extend(("--label", f"com.docker.compose.volume={name}"))
        if resource.driver:
            arguments.extend(("--driver", resource.driver))
        arguments.append(resource.actual_name)
        actions.append(Action(
            f"Create volume {name}", tuple(arguments), MUTATE_COST, BASE, frozenset({exists}),
            identity=("create_volume", name), executor="docker",
        ))
    return actions


def _batch_action(environment, state):
    broken = [
        name for name, service in sorted(environment.services.items())
        if not service.completion_required and not _service_goal(name, service) <= state
    ]
    if len(broken) < 2:
        return None

    required = set(BASE)
    additions = set()
    removals = set()
    aggregate_cost = RepairCost()
    for name in broken:
        service = environment.services[name]
        dependency_requirements = _requirements(service)
        completion_requirements = frozenset(
            item for item in dependency_requirements
            if item.startswith("completed_successfully:")
        )
        required.update(
            (_resource_requirements(environment, name, service) - dependency_requirements)
            | completion_requirements
        )
        transition_adds, transition_removes = _transition_effects(environment, name, service)
        additions.update(transition_adds)
        removals.update(transition_removes)
        aggregate_cost += _reconcile_cost(state, name)
        if service.needs_healthcheck:
            additions.discard(fact("health_pending", name))
            additions.add(fact("healthy", name))
    batch_cost = RepairCost(
        aggregate_cost.data_risk,
        aggregate_cost.destructiveness,
        aggregate_cost.disruption,
        1,
    )
    return Action(
        "Reconcile services together: " + ", ".join(broken),
        ("up", "-d", "--wait", *broken),
        batch_cost,
        frozenset(required),
        frozenset(additions),
        removes=frozenset(removals),
        identity=("reconcile_batch", *broken),
    )


def _service_actions(environment, name, service, state):
    (
        exists, current, running, healthy, pending, unhealthy, stopped, ready,
        readiness_pending, completed, completion_pending, completion_failed,
    ) = (
        fact(kind, name)
        for kind in (
            "container_exists", "config_current", "running", "healthy",
            "health_pending", "unhealthy", "stopped", "endpoint_ready",
            "readiness_pending", "completed_successfully", "completion_pending",
            "completion_failed",
        )
    )
    required = _resource_requirements(environment, name, service)
    transition_adds, transition_removes = _transition_effects(environment, name, service)
    actions = []

    missing_networks = [
        network for network in service.networks
        if _network_fact("network_connected", name, network) not in state
    ]
    missing_mount = any(
        _mount_fact("mount_attached", name, mount.target) not in state
        for mount in service.mounts
    )
    missing_port = any(
        port.published is not None and _port_fact("port_bound", name, port) not in state
        for port in service.ports
    )

    if service.completion_required:
        resource_drift = bool(missing_networks or missing_mount or missing_port)
        needs_reconcile = exists not in state or current not in state or resource_drift
        if needs_reconcile:
            recreate = exists in state
            arguments = (
                ("up", "-d", "--force-recreate", "--no-deps", name)
                if recreate else ("up", "-d", "--no-deps", name)
            )
            actions.append(Action(
                f"{'Rerun' if recreate else 'Run'} completion job {name}",
                arguments,
                _replacement_cost(state, name) if recreate else MUTATE_COST,
                required | ({exists} if recreate else set()),
                transition_adds,
                removes=transition_removes,
                identity=(("rerun_completion" if recreate else "run_completion"), name),
            ))
        elif completed in state:
            return tuple(actions)
        elif completion_pending not in state and running not in state:
            actions.append(Action(
                f"Rerun completion job {name}",
                ("up", "-d", "--force-recreate", "--no-deps", name),
                _replacement_cost(state, name),
                required | {exists, current},
                transition_adds,
                removes=transition_removes,
                identity=("rerun_completion", name),
            ))

        if completion_pending in state or running in state or needs_reconcile:
            actions.append(Action(
                f"Verify {name} completed successfully", ("ps", name), OBSERVE_COST,
                BASE | {exists, current, running, completion_pending},
                frozenset({completed}), manual=True,
                removes=frozenset({running, completion_pending, completion_failed, stopped}),
                identity=("verify_completion", name), executor="observe",
            ))
        return tuple(actions)

    if exists not in state or current not in state or missing_mount or missing_port:
        actions.append(Action(
            f"Reconcile {name}", ("up", "-d", "--no-deps", name),
            _reconcile_cost(state, name),
            required,
            transition_adds,
            removes=transition_removes,
            identity=("reconcile", name),
        ))
    elif running not in state:
        additions = {running}
        if service.needs_healthcheck:
            additions.add(pending)
        if service.readiness:
            additions.add(ready)
        additions.update(
            _port_fact("port_bound", name, port)
            for port in service.ports if port.published is not None
        )
        actions.append(Action(
            f"Start {name}", ("start", name), MUTATE_COST,
            required | {exists, current}, frozenset(additions),
            removes=frozenset({stopped, unhealthy, healthy}),
            identity=("start", name),
        ))
        actions.append(Action(
            f"Recreate {name}", ("up", "-d", "--force-recreate", "--no-deps", name),
            _replacement_cost(state, name),
            required | {exists, current}, transition_adds,
            removes=transition_removes, identity=("recreate", name),
        ))
    elif unhealthy in state:
        additions = facts(name, "running", "health_pending")
        if service.readiness:
            additions |= {ready}
        actions.append(Action(
            f"Restart {name}", ("restart", name), RESTART_COST,
            required | {exists, current, running, unhealthy}, additions,
            removes=frozenset({unhealthy, healthy, stopped}), identity=("restart", name),
        ))
        actions.append(Action(
            f"Recreate {name}", ("up", "-d", "--force-recreate", "--no-deps", name),
            _replacement_cost(state, name),
            required | {exists, current, running}, transition_adds,
            removes=transition_removes, identity=("recreate", name),
        ))
    else:
        container = (environment.containers or {}).get(name)
        for network in missing_networks:
            resource = (environment.networks or {}).get(network)
            if not container or not resource:
                continue
            actions.append(Action(
                f"Connect {name} to network {network}",
                _network_connect_arguments(name, container.name, resource.actual_name),
                MUTATE_COST,
                BASE | {exists, f"network_exists:{network}"},
                frozenset({_network_fact("network_connected", name, network)}),
                identity=("connect_network", name, network), executor="docker",
            ))

        runtime_goal = _service_goal(name, service) - {ready}
        if service.readiness and ready not in state and runtime_goal <= state:
            restart_adds = {running, ready}
            restart_removes = {readiness_pending, stopped}
            if service.needs_healthcheck:
                restart_adds.add(pending)
                restart_removes.update({healthy, unhealthy})
            actions.append(Action(
                f"Restart {name} for readiness", ("restart", name), RESTART_COST,
                required | {exists, current, running}, frozenset(restart_adds),
                removes=frozenset(restart_removes),
                identity=("restart_readiness", name),
            ))
            actions.append(Action(
                f"Recreate {name}", ("up", "-d", "--force-recreate", "--no-deps", name),
                _replacement_cost(state, name),
                required | {exists, current, running}, transition_adds,
                removes=transition_removes, identity=("recreate", name),
            ))

    if service.needs_healthcheck and healthy not in state and pending in state:
        actions.append(Action(
            f"Verify {name} health", ("ps", name), OBSERVE_COST,
            BASE | {exists, current, running, pending}, frozenset({healthy}),
            manual=True, removes=frozenset({pending, unhealthy}),
            identity=("verify_health", name), executor="observe",
        ))
    if (
        service.readiness
        and ready not in state
        and readiness_pending in state
        and (_service_goal(name, service) - {ready}) <= state
    ):
        readiness_requires = required | (_service_goal(name, service) - {ready}) | {readiness_pending}
        actions.append(Action(
            f"Verify {name} readiness", (service.readiness.url,), OBSERVE_COST,
            readiness_requires, frozenset({ready}),
            manual=True, removes=frozenset({readiness_pending}),
            identity=("verify_readiness", name), executor="observe",
        ))
    return actions


def _is_excluded(action, state, excluded):
    return (
        action.name in excluded
        or action.key in excluded
        or (state, action.key) in excluded
        or (
            action.key[:1] == ("reconcile_batch",)
            and any(isinstance(item, str) and item.startswith("Reconcile services together:") for item in excluded)
        )
    )


def validate_action_safety(environment, action):
    """Return deny-by-default safety validation and printable evidence."""

    if not action.identity:
        return False, ("DENY: action has no catalog identity",)
    kind = action.identity[0]
    allowed = {
        "start_engine", "create_network", "create_volume", "reconcile_batch",
        "reconcile", "start", "recreate", "restart", "connect_network",
        "restart_readiness", "verify_health", "verify_readiness",
        "run_completion", "rerun_completion", "verify_completion",
    }
    if kind not in allowed:
        return False, (f"DENY: unknown action identity '{kind}'",)

    evidence = ["catalog operator allowlisted", "no delete or file-edit operation"]
    if kind == "start_engine":
        if action.executor != "engine" or action.arguments:
            return False, ("DENY: engine action uses the wrong executor",)
        evidence.append("local engine startup only")
        return True, tuple(evidence)

    if kind.startswith("verify_"):
        if (
            action.executor != "observe" or not action.manual
            or len(action.identity) != 2
            or action.identity[1] not in environment.services
        ):
            return False, ("DENY: verification action is not observation-only",)
        service_name = action.identity[1]
        service = environment.services[service_name]
        expected = (
            (service.readiness.url,)
            if kind == "verify_readiness" and service.readiness
            else ("ps", service_name)
        )
        if action.arguments != expected:
            return False, ("DENY: verification target is not project-declared",)
        evidence.append("observation-only; no Docker mutation")
        return True, tuple(evidence)

    if kind in {"create_network", "create_volume"}:
        if action.executor != "docker" or len(action.identity) != 2:
            return False, ("DENY: resource creation is malformed",)
        resource_kind = "network" if kind == "create_network" else "volume"
        resources = environment.networks if resource_kind == "network" else environment.volumes
        resource = (resources or {}).get(action.identity[1])
        expected = [
            resource_kind, "create", "--label",
            f"com.docker.compose.project={environment.project_name}",
            "--label", f"com.docker.compose.{resource_kind}={action.identity[1]}",
        ]
        if resource and resource.driver:
            expected.extend(("--driver", resource.driver))
        if resource:
            expected.append(resource.actual_name)
        if not resource or resource.external or action.arguments != tuple(expected):
            return False, (f"DENY: {resource_kind} is undeclared, external, or mis-targeted",)
        evidence.extend((
            f"declared non-external {resource_kind}",
            "project ownership labels included",
        ))
        return True, tuple(evidence)

    if kind == "connect_network":
        if action.executor != "docker" or len(action.identity) != 3:
            return False, ("DENY: network attachment is malformed",)
        service_name, network_name = action.identity[1:]
        container = (environment.containers or {}).get(service_name)
        resource = (environment.networks or {}).get(network_name)
        if service_name not in environment.services or not container or not resource:
            return False, ("DENY: network attachment target is not project-declared",)
        if resource.external and f"network_exists:{network_name}" not in environment.facts:
            return False, ("DENY: missing external network cannot be created or attached",)
        expected = _network_connect_arguments(
            service_name, container.name, resource.actual_name,
        )
        if action.arguments != expected:
            return False, ("DENY: network attachment targets unexpected objects",)
        evidence.extend((
            "declared network",
            "observed project container only",
            "compose service DNS alias restored",
        ))
        return True, tuple(evidence)

    if action.executor != "compose":
        return False, ("DENY: service repair bypasses project-scoped Compose",)
    service_names = action.identity[1:]
    if not service_names or any(name not in environment.services for name in service_names):
        return False, ("DENY: service repair targets a foreign or unknown service",)
    expected_arguments = {
        "reconcile_batch": ("up", "-d", "--wait", *service_names),
        "reconcile": ("up", "-d", "--no-deps", *service_names),
        "start": ("start", *service_names),
        "recreate": ("up", "-d", "--force-recreate", "--no-deps", *service_names),
        "restart": ("restart", *service_names),
        "restart_readiness": ("restart", *service_names),
        "run_completion": ("up", "-d", "--no-deps", *service_names),
        "rerun_completion": ("up", "-d", "--force-recreate", "--no-deps", *service_names),
    }
    if action.arguments != expected_arguments.get(kind):
        return False, ("DENY: Compose arguments do not match the catalog operator",)
    evidence.extend(("project-scoped Compose invocation", "declared services only"))
    return True, tuple(evidence)


def build_actions(environment, state=None, excluded=frozenset()):
    state = environment.facts if state is None else state
    if "daemon_reachable" not in state:
        actions = [Action(
            "Start Docker Engine", (), MUTATE_COST, frozenset(), frozenset({"daemon_reachable"}),
            identity=("start_engine",), executor="engine",
        )]
    else:
        actions = _resource_actions(environment, state)
        batch = _batch_action(environment, state)
        if batch:
            actions.append(batch)
        for name, service in sorted(environment.services.items()):
            actions.extend(_service_actions(environment, name, service, state))
    validated = []
    for action in actions:
        if _is_excluded(action, state, excluded):
            continue
        safe, checks = validate_action_safety(environment, action)
        if safe:
            validated.append(replace(action, safety_checks=checks))
    return tuple(validated)


def search(environment, excluded=frozenset()):
    """Return the cheapest symbolic plan from observed facts to goal facts."""

    goal = build_goal(environment)
    initial = environment.facts
    if "compose_valid" not in initial:
        return Plan("blocked"), goal
    if any(
        condition not in DEPENDENCY_FACTS
        for service in environment.services.values()
        for _, condition in service.dependencies
    ):
        return Plan("blocked"), goal

    target = goal if environment.daemon_reachable else frozenset({"daemon_reachable"})
    if target <= initial:
        return Plan("already_healthy"), goal
    if environment.daemon_reachable and environment.blocked_reasons:
        return Plan("blocked"), goal

    order = count()
    zero = RepairCost()
    frontier = [(zero, next(order), initial, ())]
    best = {initial: zero}
    explored = 0
    while frontier:
        cost, _, state, path = heapq.heappop(frontier)
        if cost != best.get(state):
            continue
        explored += 1
        if target <= state:
            status = "plan_found" if environment.daemon_reachable else "prerequisite_plan"
            return Plan(status, path, explored), goal

        for action in build_actions(environment, state, excluded):
            if not action.is_allowed(state):
                continue
            next_state = action.apply(state)
            next_cost = cost + action.cost
            previous_cost = best.get(next_state)
            if next_state == state or (previous_cost is not None and next_cost >= previous_cost):
                continue
            best[next_state] = next_cost
            heapq.heappush(frontier, (next_cost, next(order), next_state, (*path, action)))

    return Plan("unreachable", explored_states=explored), goal
