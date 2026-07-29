"""Read Docker state. Proposed repair commands are never executed here."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from dockrepair_data import Environment, Service


def compose_command(environment: Environment, *arguments: str) -> str:
    """Build a printable command string, not an executable process."""

    parts = compose_arguments(environment, *arguments)
    return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)


def compose_arguments(environment: Environment, *arguments: str) -> tuple[str, ...]:
    """Build a shell-free argument list for an executable Compose action."""

    return ("docker", "compose", "-f", environment.compose_file, "-p", environment.project_name, *arguments)


def _run(arguments: list[str], cwd: Path) -> tuple[bool, str]:
    """Run one of the fixed read-only commands used by collect_environment."""

    try:
        result = subprocess.run(
            arguments,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            shell=False,
        )
        output = result.stdout.strip() if result.returncode == 0 else result.stderr.strip()
        return result.returncode == 0, output
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return False, str(error)


def _service_hashes(text: str) -> dict[str, str]:
    return {
        parts[0]: parts[-1]
        for line in text.splitlines()
        if len(parts := line.split()) >= 2
    }


def _dependencies(service: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    dependencies = []
    raw_dependencies = service.get("depends_on", {})
    if not isinstance(raw_dependencies, dict):
        return ()
    for name, settings in raw_dependencies.items():
        condition = settings.get("condition", "service_started") if isinstance(settings, dict) else "service_started"
        required = settings.get("required", True) if isinstance(settings, dict) else True
        if required:
            dependencies.append((str(name), str(condition)))
    return tuple(sorted(dependencies))


def _needs_healthcheck(service: dict[str, Any]) -> bool:
    healthcheck = service.get("healthcheck")
    if not isinstance(healthcheck, dict) or healthcheck.get("disable") is True:
        return False
    return healthcheck.get("test") not in (None, "NONE", ["NONE"])


def _project_fallback(path: Path) -> str:
    name = re.sub(r"[^a-z0-9_-]+", "-", path.parent.name.lower()).strip("-_")
    return name or "dockrepair"


def collect_environment(compose_file: str) -> Environment:
    """Take one compact snapshot using only read-only Docker commands."""

    path = Path(compose_file).expanduser().resolve()
    fallback = _project_fallback(path)
    if not path.is_file():
        return Environment(str(path), fallback, {}, frozenset(), False, ("Compose file does not exist.",))

    cwd = path.parent
    compose = ["docker", "compose", "-f", str(path)]

    config_ok, config_text = _run([*compose, "config", "--format", "json"], cwd)
    if not config_ok:
        return Environment(str(path), fallback, {}, frozenset(), False, (config_text,))

    try:
        config = json.loads(config_text)
    except json.JSONDecodeError as error:
        return Environment(str(path), fallback, {}, frozenset(), False, (f"Invalid Compose JSON: {error}",))

    project_name = str(config.get("name") or fallback)
    hashes_ok, hashes_text = _run([*compose, "config", "--hash", "*"], cwd)
    hashes = _service_hashes(hashes_text) if hashes_ok else {}

    services = {}
    for name, raw_service in sorted(config.get("services", {}).items()):
        service = raw_service if isinstance(raw_service, dict) else {}
        services[name] = Service(
            name=name,
            needs_healthcheck=_needs_healthcheck(service),
            config_hash=hashes.get(name),
            dependencies=_dependencies(service),
        )

    facts = {"compose_valid"}
    errors = []
    daemon_ok, daemon_error = _run(["docker", "info", "--format", "{{.ServerVersion}}"], cwd)
    if not daemon_ok:
        return Environment(str(path), project_name, services, frozenset(facts), False, (daemon_error,))
    facts.add("daemon_reachable")

    # Ask Compose only for IDs, then inspect every container in one batch.
    ids_ok, ids_text = _run([*compose, "ps", "--all", "--quiet"], cwd)
    container_ids = ids_text.splitlines() if ids_ok and ids_text else []
    containers = []
    if container_ids:
        inspect_ok, inspect_text = _run(["docker", "inspect", *container_ids], cwd)
        if inspect_ok:
            containers = json.loads(inspect_text)
        else:
            errors.append(inspect_text)

    # Map each inspected container back to its Compose service label.
    container_by_service = {}
    for container in containers:
        labels = container.get("Config", {}).get("Labels") or {}
        service_name = labels.get("com.docker.compose.service")
        if service_name:
            container_by_service[service_name] = container

    for name, service in services.items():
        container = container_by_service.get(name)
        exists = container is not None
        if not exists:
            continue

        facts.add(f"container_exists:{name}")
        labels = container.get("Config", {}).get("Labels") or {}
        actual_hash = labels.get("com.docker.compose.config-hash")
        if not service.config_hash or actual_hash == service.config_hash:
            facts.add(f"config_current:{name}")

        state = container.get("State", {})
        running = bool(state.get("Running"))
        if running:
            facts.add(f"running:{name}")

        health = (state.get("Health") or {}).get("Status")
        if running and health == "healthy":
            facts.add(f"healthy:{name}")
        elif running and health == "unhealthy":
            facts.add(f"unhealthy:{name}")
        elif running and service.needs_healthcheck:
            facts.add(f"health_pending:{name}")

    return Environment(str(path), project_name, services, frozenset(facts), True, tuple(errors))
