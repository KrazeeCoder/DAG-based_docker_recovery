"""Read Docker state. Proposed repair commands are never executed here."""

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from dockrepair_data import Environment, Service


def compose_command(environment, *arguments):
    parts = compose_arguments(environment, *arguments)
    return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)


def compose_arguments(environment, *arguments):
    return ("docker", "compose", "-f", environment.compose_file, "-p", environment.project_name, *arguments)


def _run(arguments, cwd):
    # Keep all read-only Docker subprocess handling in one place.
    try:
        result = subprocess.run(arguments, cwd=cwd, capture_output=True, text=True, timeout=30)
        output = result.stdout.strip() if result.returncode == 0 else result.stderr.strip()
        return result.returncode == 0, output
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        return False, str(error)


def _service_hashes(text):
    return {
        parts[0]: parts[-1]
        for line in text.splitlines()
        if len(parts := line.split()) >= 2
    }


def _dependencies(service):
    return tuple(sorted(
        (name, settings.get("condition", "service_started"))
        for name, settings in service.get("depends_on", {}).items()
        if settings.get("required", True)
    ))


def _needs_healthcheck(service):
    healthcheck = service.get("healthcheck")
    if not healthcheck or healthcheck.get("disable"):
        return False
    return healthcheck.get("test") not in (None, "NONE", ["NONE"])


def _project_fallback(path):
    name = re.sub(r"[^a-z0-9_-]+", "-", path.parent.name.lower()).strip("-_")
    return name or "dockrepair"


def collect_environment(compose_file):
    # Phase 1: resolve and validate the requested Compose file.
    path = Path(compose_file).expanduser().resolve()
    fallback = _project_fallback(path)
    if not path.is_file():
        return Environment(str(path), fallback, {}, frozenset(), ("Compose file does not exist.",))

    cwd = path.parent
    compose = ["docker", "compose", "-f", str(path)]

    # Phase 2: ask Compose for its normalized services and dependencies.
    config_ok, config_text = _run([*compose, "config", "--format", "json"], cwd)
    if not config_ok:
        return Environment(str(path), fallback, {}, frozenset(), (config_text,))

    try:
        config = json.loads(config_text)
    except json.JSONDecodeError as error:
        return Environment(str(path), fallback, {}, frozenset(), (f"Invalid Compose JSON: {error}",))

    project_name = str(config.get("name") or fallback)
    hashes_ok, hashes_text = _run([*compose, "config", "--hash", "*"], cwd)
    hashes = _service_hashes(hashes_text) if hashes_ok else {}

    services = {
        name: Service(_needs_healthcheck(service), hashes.get(name), _dependencies(service))
        for name, service in sorted(config.get("services", {}).items())
    }

    # Phase 3: turn live Docker state into facts understood by the planner.
    facts = {"compose_valid"}
    errors = []
    daemon_ok, daemon_error = _run(["docker", "info", "--format", "{{.ServerVersion}}"], cwd)
    if not daemon_ok:
        return Environment(str(path), project_name, services, frozenset(facts), (daemon_error,))
    facts.add("daemon_reachable")

    ids_ok, ids_text = _run([*compose, "ps", "--all", "--quiet"], cwd)
    container_ids = ids_text.splitlines() if ids_ok and ids_text else []
    containers = []
    if container_ids:
        inspect_ok, inspect_text = _run(["docker", "inspect", *container_ids], cwd)
        if inspect_ok:
            containers = json.loads(inspect_text)
        else:
            errors.append(inspect_text)

    # Compose labels connect inspected containers back to service names.
    container_by_service = {}
    for container in containers:
        labels = container.get("Config", {}).get("Labels") or {}
        service_name = labels.get("com.docker.compose.service")
        if service_name:
            container_by_service[service_name] = container

    # Record existence, config, running, and health facts for each service.
    for name, service in services.items():
        container = container_by_service.get(name)
        if not container:
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

    return Environment(str(path), project_name, services, frozenset(facts), tuple(errors))
