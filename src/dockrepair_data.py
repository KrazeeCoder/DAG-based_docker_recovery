from dataclasses import dataclass


@dataclass(frozen=True)
class Service:
    # Static requirements read from the Compose configuration.
    needs_healthcheck: bool
    config_hash: str | None
    dependencies: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Environment:
    # One observed snapshot of Compose configuration and live Docker facts.
    compose_file: str
    project_name: str
    services: dict[str, Service]
    facts: frozenset[str]
    errors: tuple[str, ...] = ()

    @property
    def daemon_reachable(self):
        return "daemon_reachable" in self.facts


@dataclass(frozen=True)
class Action:
    # One graph edge: requirements, predicted effects, command, and cost.
    name: str
    arguments: tuple[str, ...]
    cost: int
    requires: frozenset[str]
    adds: frozenset[str]
    manual: bool = False

    def is_allowed(self, state):
        return self.requires <= state

    def apply(self, state):
        return state | self.adds


@dataclass(frozen=True)
class Plan:
    # The ordered actions chosen by the planner's cheapest-path search.
    status: str
    actions: tuple[Action, ...] = ()
    explored_states: int = 0

    @property
    def total_cost(self):
        return sum(action.cost for action in self.actions)

    @property
    def message(self):
        return {
            "blocked": "Compose configuration is invalid.",
            "already_healthy": "The goal is already satisfied.",
            "unreachable": "The action catalog cannot reach the goal.",
        }.get(self.status, "Predicted effects must be checked after each command.")
