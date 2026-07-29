"""Plain data objects. No Docker calls or planning logic live here."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Service:
    name: str
    needs_healthcheck: bool
    config_hash: str | None
    dependencies: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Environment:
    compose_file: str
    project_name: str
    services: dict[str, Service]
    facts: frozenset[str]
    daemon_reachable: bool
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class Action:
    name: str
    command: str
    arguments: tuple[str, ...]
    cost: int
    requires: frozenset[str]
    adds: frozenset[str]
    removes: frozenset[str] = frozenset()
    manual: bool = False

    def is_allowed(self, state: frozenset[str]) -> bool:
        return self.requires <= state

    def apply(self, state: frozenset[str]) -> frozenset[str]:
        return (state - self.removes) | self.adds


@dataclass(frozen=True)
class SearchNode:
    """One node in the state graph explored by uniform-cost search."""

    state: frozenset[str]
    actions: tuple[Action, ...]
    cost: int


@dataclass(frozen=True)
class Plan:
    status: str
    actions: tuple[Action, ...]
    total_cost: int
    explored_states: int
    message: str
