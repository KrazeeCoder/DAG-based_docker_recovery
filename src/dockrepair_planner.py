"""Build deterministic Docker repair operators and search for a cheapest plan."""

import heapq
from itertools import count

from dockrepair_data import Action, Environment, Plan


BASE = frozenset({"daemon_reachable", "compose_valid"})


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


def _requirements(service):
    return frozenset(
        fact("healthy" if condition == "service_healthy" else "running", dependency)
        for dependency, condition in service.dependencies
    )


def _service_goal(name, service):
    goal = set(facts(name, "container_exists", "config_current", "running"))
    if service.needs_healthcheck:
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
    if service.readiness:
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
    removals = set(facts(name, "stopped", "unhealthy", "healthy", "health_pending", "oom_killed"))
    if service.needs_healthcheck:
        additions.add(fact("health_pending", name))
    if service.readiness:
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
            f"Create network {name}", tuple(arguments), 2, BASE, frozenset({exists}),
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
            f"Create volume {name}", tuple(arguments), 2, BASE, frozenset({exists}),
            identity=("create_volume", name), executor="docker",
        ))
    return actions


def _batch_action(environment, state):
    broken = [
        name for name, service in sorted(environment.services.items())
        if not _service_goal(name, service) <= state
    ]
    if len(broken) < 2:
        return None

    required = set(BASE)
    additions = set()
    removals = set()
    for name in broken:
        service = environment.services[name]
        required.update(_resource_requirements(environment, name, service) - _requirements(service))
        transition_adds, transition_removes = _transition_effects(environment, name, service)
        additions.update(transition_adds)
        removals.update(transition_removes)
        if service.needs_healthcheck:
            additions.discard(fact("health_pending", name))
            additions.add(fact("healthy", name))
    return Action(
        "Reconcile services together: " + ", ".join(broken),
        ("up", "-d", "--wait", *broken),
        2 + len(broken),
        frozenset(required),
        frozenset(additions),
        removes=frozenset(removals),
        identity=("reconcile_batch", *broken),
    )


def _service_actions(environment, name, service, state):
    exists, current, running, healthy, pending, unhealthy, stopped, ready, readiness_pending = (
        fact(kind, name)
        for kind in (
            "container_exists", "config_current", "running", "healthy",
            "health_pending", "unhealthy", "stopped", "endpoint_ready",
            "readiness_pending",
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

    if exists not in state or current not in state or missing_mount or missing_port:
        actions.append(Action(
            f"Reconcile {name}", ("up", "-d", "--no-deps", name),
            6 if exists not in state else 7,
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
            f"Start {name}", ("start", name), 2,
            required | {exists, current}, frozenset(additions),
            removes=frozenset({stopped, unhealthy, healthy}),
            identity=("start", name),
        ))
        actions.append(Action(
            f"Recreate {name}", ("up", "-d", "--force-recreate", "--no-deps", name), 8,
            required | {exists, current}, transition_adds,
            removes=transition_removes, identity=("recreate", name),
        ))
    elif unhealthy in state:
        additions = facts(name, "running", "health_pending")
        if service.readiness:
            additions |= {ready}
        actions.append(Action(
            f"Restart {name}", ("restart", name), 3,
            required | {exists, current, running, unhealthy}, additions,
            removes=frozenset({unhealthy, healthy, stopped}), identity=("restart", name),
        ))
        actions.append(Action(
            f"Recreate {name}", ("up", "-d", "--force-recreate", "--no-deps", name), 8,
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
                ("network", "connect", resource.actual_name, container.name),
                2,
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
                f"Restart {name} for readiness", ("restart", name), 4,
                required | {exists, current, running}, frozenset(restart_adds),
                removes=frozenset(restart_removes),
                identity=("restart_readiness", name),
            ))
            actions.append(Action(
                f"Recreate {name}", ("up", "-d", "--force-recreate", "--no-deps", name), 8,
                required | {exists, current, running}, transition_adds,
                removes=transition_removes, identity=("recreate", name),
            ))

    if service.needs_healthcheck and healthy not in state and pending in state:
        actions.append(Action(
            f"Verify {name} health", ("ps", name), 1,
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
            f"Verify {name} readiness", (service.readiness.url,), 1,
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


def build_actions(environment, state=None, excluded=frozenset()):
    state = environment.facts if state is None else state
    if "daemon_reachable" not in state:
        actions = [Action(
            "Start Docker Engine", (), 1, frozenset(), frozenset({"daemon_reachable"}),
            identity=("start_engine",), executor="engine",
        )]
    else:
        actions = _resource_actions(environment, state)
        batch = _batch_action(environment, state)
        if batch:
            actions.append(batch)
        for name, service in sorted(environment.services.items()):
            actions.extend(_service_actions(environment, name, service, state))
    return tuple(action for action in actions if not _is_excluded(action, state, excluded))


def search(environment, excluded=frozenset()):
    """Return the cheapest symbolic plan from observed facts to goal facts."""

    goal = build_goal(environment)
    initial = environment.facts
    if "compose_valid" not in initial:
        return Plan("blocked"), goal

    target = goal if environment.daemon_reachable else frozenset({"daemon_reachable"})
    if target <= initial:
        return Plan("already_healthy"), goal
    if environment.daemon_reachable and environment.blocked_reasons:
        return Plan("blocked"), goal

    order = count()
    frontier = [(0, next(order), initial, ())]
    best = {initial: 0}
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
            if next_state == state or next_cost >= best.get(next_state, float("inf")):
                continue
            best[next_state] = next_cost
            heapq.heappush(frontier, (next_cost, next(order), next_state, (*path, action)))

    return Plan("unreachable", explored_states=explored), goal
