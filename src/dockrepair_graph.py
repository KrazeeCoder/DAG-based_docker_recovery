"""Dependency-graph grouping and intervention checks for contract failures."""

from __future__ import annotations

from collections import defaultdict, deque

from dockrepair_data import (
    CascadeGroup,
    Diagnosis,
    EdgeAssessment,
    Environment,
    GraphAnalysis,
    InterventionRecord,
)


def _strongly_connected_components(nodes, adjacency):
    """Return deterministic Tarjan SCCs for one failed-edge component."""

    index = 0
    indexes = {}
    lowlinks = {}
    stack = []
    on_stack = set()
    components = []

    def visit(node):
        nonlocal index
        indexes[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(adjacency.get(node, ())):
            if target not in indexes:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[target])
        if lowlinks[node] == indexes[node]:
            component = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            components.append(tuple(sorted(component)))

    for node in sorted(nodes):
        if node not in indexes:
            visit(node)
    return tuple(sorted(components))


def _weak_failed_components(edges):
    neighbors = defaultdict(set)
    by_node = defaultdict(set)
    for edge in edges:
        neighbors[edge.caller].add(edge.target)
        neighbors[edge.target].add(edge.caller)
        by_node[edge.caller].add(edge.contract_key)
        by_node[edge.target].add(edge.contract_key)

    edge_by_key = {edge.contract_key: edge for edge in edges}
    seen = set()
    groups = []
    for start in sorted(neighbors):
        if start in seen:
            continue
        queue = deque([start])
        seen.add(start)
        nodes = set()
        keys = set()
        while queue:
            node = queue.popleft()
            nodes.add(node)
            keys.update(by_node[node])
            for neighbor in sorted(neighbors[node]):
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        groups.append((tuple(sorted(nodes)), tuple(sorted(keys))))
    return tuple(sorted(groups, key=lambda item: item[1])), edge_by_key


def build_graph_analysis(environment: Environment, diagnoses: tuple[Diagnosis, ...]):
    diagnosis_by_key = {item.contract_key: item for item in diagnoses}
    edges = []
    for contract in sorted(environment.contracts, key=lambda item: item.key):
        diagnosis = diagnosis_by_key.get(contract.key)
        if diagnosis is None:
            continue
        edges.append(EdgeAssessment(
            contract.key,
            contract.caller,
            contract.target,
            diagnosis.locus,
            "healthy" if diagnosis.code == "CONTRACT_HEALTHY" else "failed",
            diagnosis.code,
            getattr(diagnosis, "certainty", "proven"),
            getattr(diagnosis, "repairable", diagnosis.code != "CONTRACT_HEALTHY"),
            getattr(diagnosis, "evidence", ()),
        ))

    failed = tuple(edge for edge in edges if edge.status == "failed")
    weak_groups, edge_by_key = _weak_failed_components(failed)
    groups = []
    inferred = []
    unresolved = []

    for number, (nodes, keys) in enumerate(weak_groups, start=1):
        group_edges = tuple(edge_by_key[key] for key in keys)
        adjacency = defaultdict(set)
        for edge in group_edges:
            adjacency[edge.caller].add(edge.target)
        components = _strongly_connected_components(nodes, adjacency)
        component_of = {
            service: component_index
            for component_index, component in enumerate(components)
            for service in component
        }
        outgoing = defaultdict(set)
        for edge in group_edges:
            source = component_of[edge.caller]
            target = component_of[edge.target]
            if source != target:
                outgoing[source].add(target)
        sinks = {
            component_index for component_index in range(len(components))
            if not outgoing.get(component_index)
        }
        deepest = tuple(sorted(
            edge.contract_key for edge in group_edges
            if component_of[edge.target] in sinks
        ))
        deepest_components = tuple(sorted(
            components[index] for index in sinks
            if any(
                component_of[edge.target] == index
                for edge in group_edges
                if edge.contract_key in deepest
            )
        ))
        upstream = tuple(sorted(set(keys) - set(deepest)))
        cyclic_components = {
            index for index, component in enumerate(components)
            if len(component) > 1
            or any(
                edge.caller == edge.target == component[0]
                for edge in group_edges
            )
        }
        # Only a cycle in a deepest sink makes current selection ambiguous.
        # An upstream cycle will become relevant after a deeper failure is repaired
        # and the graph is diagnosed again.
        cyclic = bool(cyclic_components & sinks)
        deepest_edges = [edge_by_key[key] for key in deepest]
        root_candidates = tuple(sorted({
            edge.locus for edge in deepest_edges if edge.repairable
        }))

        if cyclic and len(root_candidates) > 1:
            status = "cyclic_ambiguous"
            explanation = (
                "The failed dependency cycle has multiple plausible repair locations; "
                "graph position cannot select a causal root.",
            )
            unresolved.append(
                f"cascade-{number} contains a cycle with multiple plausible repairs: "
                + ", ".join(root_candidates)
            )
        elif root_candidates:
            status = "repairable"
            explanation = (
                "The listed candidates are in the deepest observed failed region; "
                "this is a repair priority, not proof of causation.",
            )
        elif any(edge.repairable for edge in group_edges if edge.contract_key in upstream):
            status = "blocked_by_deeper_failure"
            explanation = (
                "A deeper failed dependency has no safe repair, so upstream victim "
                "repairs are suppressed.",
            )
            unresolved.append(
                f"cascade-{number} has no safe repair for its deepest failed dependency"
            )
        else:
            status = "unrepairable"
            explanation = ("No safe repair is available for the deepest failed region.",)

        for key in upstream:
            deeper = ", ".join(deepest)
            inferred.append(
                f"{key} may be an upstream symptom of deeper failed contract(s): {deeper}."
            )
        groups.append(CascadeGroup(
            f"cascade-{number}",
            nodes,
            keys,
            deepest,
            deepest_components,
            root_candidates,
            upstream,
            cyclic,
            status,
            explanation,
        ))

    observed = tuple(
        f"{edge.caller} -> {edge.target}: {edge.diagnosis_code} [{edge.certainty}]."
        for edge in edges
    )
    return GraphAnalysis(
        tuple(edges), tuple(groups), (), observed, tuple(inferred), tuple(unresolved),
    )


def expected_upstream_contracts(analysis: GraphAnalysis, group: CascadeGroup, seeds):
    """Return seed edges and failed callers transitively affected above them."""

    group_edges = {
        edge.contract_key: edge for edge in analysis.edges
        if edge.contract_key in group.contract_keys and edge.status == "failed"
    }
    selected = set(seeds)
    frontier_callers = {
        group_edges[key].caller for key in selected if key in group_edges
    }
    changed = True
    while changed:
        changed = False
        for key, edge in group_edges.items():
            if key not in selected and edge.target in frontier_callers:
                selected.add(key)
                frontier_callers.add(edge.caller)
                changed = True
    return tuple(sorted(selected))


def group_for_contract(analysis: GraphAnalysis, contract_key: str):
    return next(
        (group for group in analysis.groups if contract_key in group.contract_keys),
        None,
    )


def finalize_intervention(
    *, action, service, group, seed_contracts, expected_contracts,
    before_analysis, after_analysis, mutated_services,
):
    before_by_key = {edge.contract_key: edge for edge in before_analysis.edges}
    after_by_key = {edge.contract_key: edge for edge in after_analysis.edges}
    relevant_keys = tuple(sorted(set(expected_contracts)))
    # Preserve the complete group snapshot even when the intervention predicts
    # recovery for only one branch of a larger connected incident.
    group_keys = tuple(sorted(set(group.contract_keys)))
    before = tuple(before_by_key[key] for key in group_keys if key in before_by_key)
    after = tuple(after_by_key[key] for key in group_keys if key in after_by_key)

    restored = {
        key for key in relevant_keys
        if key in before_by_key and key in after_by_key
        and before_by_key[key].status == "failed"
        and after_by_key[key].status == "healthy"
    }
    direct = tuple(sorted(restored & set(seed_contracts)))
    indirect = tuple(sorted(
        key for key in restored - set(seed_contracts)
        if before_by_key[key].caller not in mutated_services
        and before_by_key[key].target not in mutated_services
    ))
    still_failed = tuple(sorted(
        key for key in relevant_keys
        if key in after_by_key and after_by_key[key].status == "failed"
    ))

    if group.cyclic:
        causal_status = "cycle_unresolved"
        conclusion = (
            "The intervention changed a cyclic failure region, but it does not "
            "establish a causal root inside the cycle."
        )
    elif indirect and not still_failed:
        causal_status = "supported"
        conclusion = (
            f"Repairing {service} restored upstream contracts without modifying their "
            "services; this supports, but does not prove, the cascade explanation."
        )
    elif indirect:
        causal_status = "partially_supported"
        conclusion = (
            f"Repairing {service} restored some upstream contracts, but part of the "
            "predicted cascade remains unresolved."
        )
    elif direct:
        causal_status = "direct_only"
        conclusion = (
            f"Repairing {service} restored its directly associated contract, but did "
            "not confirm the predicted upstream cascade."
        )
    else:
        causal_status = "not_supported"
        conclusion = (
            f"Repairing {service} did not restore the predicted contracts; the "
            "cascade explanation is not supported by this intervention."
        )
    return InterventionRecord(
        action.name,
        action.key,
        service,
        group.identifier,
        tuple(sorted(seed_contracts)),
        relevant_keys,
        before,
        after,
        direct,
        indirect,
        still_failed,
        causal_status,
        conclusion,
    )
