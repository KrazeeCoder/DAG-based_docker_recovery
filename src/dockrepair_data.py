from dataclasses import dataclass
from ipaddress import ip_address


def normalize_host_ip(value):
    value = str(value or "").strip().strip("[]")
    if not value:
        return "*"
    try:
        return ip_address(value).compressed
    except ValueError:
        return value.lower()


def port_binding_key(host_ip, published, protocol):
    host = normalize_host_ip(host_ip)
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{published}/{str(protocol).lower()}"


@dataclass(frozen=True)
class Port:
    target: int
    published: int | None = None
    protocol: str = "tcp"
    host_ip: str = ""

    @property
    def key(self):
        return port_binding_key(self.host_ip, self.published, self.protocol)


@dataclass(frozen=True)
class PublishedPort:
    target: int
    published: int
    protocol: str = "tcp"
    host_ip: str = ""

    @property
    def key(self):
        return port_binding_key(self.host_ip, self.published, self.protocol)


@dataclass(frozen=True)
class ReadinessProbe:
    url: str
    statuses: frozenset[int] = frozenset(range(200, 400))
    timeout: float = 2.0


@dataclass(frozen=True)
class Mount:
    source: str
    target: str
    kind: str
    read_only: bool = False
    source_exists: bool = True


@dataclass(frozen=True)
class Resource:
    logical_name: str
    actual_name: str
    external: bool = False
    driver: str = ""


@dataclass(frozen=True)
class Service:
    # Static requirements read from the Compose configuration.
    needs_healthcheck: bool
    config_hash: str | None
    dependencies: tuple[tuple[str, str], ...] = ()
    networks: tuple[str, ...] = ()
    mounts: tuple[Mount, ...] = ()
    ports: tuple[Port, ...] = ()
    readiness: ReadinessProbe | None = None
    completion_required: bool = False
    desired_replicas: int = 1


@dataclass(frozen=True)
class Container:
    service: str
    container_id: str
    name: str
    status: str
    running: bool
    exit_code: int | None
    oom_killed: bool
    restart_count: int
    health: str | None
    health_output: str
    config_hash: str | None
    networks: frozenset[str]
    mounts: frozenset[str]
    published_ports: frozenset[PublishedPort]


@dataclass(frozen=True)
class Environment:
    # Desired Compose configuration plus one observed Docker snapshot.
    compose_file: str
    project_name: str
    services: dict[str, Service]
    facts: frozenset[str]
    errors: tuple[str, ...] = ()
    containers: dict[str, Container] | None = None
    networks: dict[str, Resource] | None = None
    volumes: dict[str, Resource] | None = None
    existing_networks: frozenset[str] = frozenset()
    existing_volumes: frozenset[str] = frozenset()
    port_conflicts: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()

    @property
    def daemon_reachable(self):
        return "daemon_reachable" in self.facts


@dataclass(frozen=True, order=True)
class RepairCost:
    """Lexicographic repair policy, ordered from highest to lowest priority."""

    data_risk: int = 0
    destructiveness: int = 0
    disruption: int = 0
    actions: int = 0

    def __add__(self, other):
        if not isinstance(other, RepairCost):
            return NotImplemented
        return RepairCost(
            self.data_risk + other.data_risk,
            self.destructiveness + other.destructiveness,
            self.disruption + other.disruption,
            self.actions + other.actions,
        )

    def __str__(self):
        return (
            f"(data-risk={self.data_risk}, destructiveness={self.destructiveness}, "
            f"disruption={self.disruption}, actions={self.actions})"
        )


@dataclass(frozen=True)
class Action:
    # One graph edge: requirements, add/delete effects, command, and cost.
    name: str
    arguments: tuple[str, ...]
    cost: RepairCost
    requires: frozenset[str]
    adds: frozenset[str]
    manual: bool = False
    removes: frozenset[str] = frozenset()
    identity: tuple[str, ...] = ()
    executor: str = "compose"
    safety_checks: tuple[str, ...] = ()

    def is_allowed(self, state):
        return self.requires <= state

    def apply(self, state):
        return (state - self.removes) | self.adds

    @property
    def key(self):
        return self.identity or (self.executor, *self.arguments)


@dataclass(frozen=True)
class Plan:
    # The ordered actions chosen by the planner's cheapest-path search.
    status: str
    actions: tuple[Action, ...] = ()
    explored_states: int = 0

    @property
    def total_cost(self):
        total = RepairCost()
        for action in self.actions:
            total += action.cost
        return total

    @property
    def message(self):
        return {
            "blocked": "The observed environment has a condition outside the safe action catalog.",
            "already_healthy": "The goal is already satisfied.",
            "unreachable": "The action catalog cannot reach the goal.",
        }.get(self.status, "Predicted effects must be checked after each command.")
