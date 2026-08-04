"""Read Docker and Compose state without mutating it."""

import json
import os
import re
import shlex
import socket
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dockrepair_data import (
    Container,
    Environment,
    Mount,
    Port,
    PublishedPort,
    ReadinessProbe,
    Resource,
    Service,
    normalize_host_ip,
)


def compose_command(environment, *arguments):
    parts = compose_arguments(environment, *arguments)
    return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)


def compose_arguments(environment, *arguments):
    return ("docker", "compose", "-f", environment.compose_file, "-p", environment.project_name, *arguments)


def _run(arguments, cwd):
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
        for name, settings in (service.get("depends_on") or {}).items()
        if settings.get("required", True)
    ))


def _needs_healthcheck(service):
    healthcheck = service.get("healthcheck")
    if not healthcheck or healthcheck.get("disable"):
        return False
    return healthcheck.get("test") not in (None, "NONE", ["NONE"])


def _ports(service):
    result = []
    for value in service.get("ports") or ():
        if not isinstance(value, dict):
            continue
        try:
            target = int(value["target"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            published = int(value["published"]) if value.get("published") is not None else None
        except (TypeError, ValueError):
            published = None
        result.append(Port(
            target,
            published,
            str(value.get("protocol") or "tcp"),
            str(value.get("host_ip") or ""),
        ))
    return tuple(result)


def _labels(service):
    labels = service.get("labels") or {}
    if isinstance(labels, dict):
        return {str(key): str(value) for key, value in labels.items()}
    result = {}
    for label in labels:
        key, separator, value = str(label).partition("=")
        result[key] = value if separator else ""
    return result


def _readiness(service):
    labels = _labels(service)
    extension = service.get("x-dockrepair-readiness") or {}
    if isinstance(extension, str):
        extension = {"url": extension}
    url = str(extension.get("url") or labels.get("com.dockrepair.readiness.url") or "").strip()
    if not url:
        return None

    raw_statuses = extension.get("statuses", labels.get("com.dockrepair.readiness.statuses", "200-399"))
    statuses = set()
    values = raw_statuses if isinstance(raw_statuses, list) else str(raw_statuses).split(",")
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        try:
            if "-" in text:
                start, end = (int(part.strip()) for part in text.split("-", 1))
                if 100 <= start <= end <= 599:
                    statuses.update(range(start, end + 1))
            else:
                status = int(text)
                if 100 <= status <= 599:
                    statuses.add(status)
        except ValueError:
            continue
    if not statuses:
        statuses.update(range(200, 400))

    raw_timeout = extension.get("timeout", labels.get("com.dockrepair.readiness.timeout", 2))
    try:
        timeout = max(0.1, float(raw_timeout))
    except (TypeError, ValueError):
        timeout = 2.0
    return ReadinessProbe(url, frozenset(statuses), timeout)


def _bind_source_exists(source):
    if Path(source).expanduser().exists():
        return True
    # Docker Desktop resolves Linux-absolute bind sources inside its Linux VM.
    # Testing those paths with the Windows client filesystem incorrectly marks
    # standard mounts such as /proc, /sys, and /var/run as missing.
    return os.name == "nt" and source.startswith("/")


def _mounts(service):
    result = []
    for value in service.get("volumes") or ():
        if not isinstance(value, dict) or not value.get("target"):
            continue
        kind = str(value.get("type") or "volume")
        source = str(value.get("source") or "")
        exists = kind != "bind" or _bind_source_exists(source)
        result.append(Mount(source, str(value["target"]), kind, bool(value.get("read_only")), exists))
    return tuple(result)


def _logical_networks(service, available):
    configured = service.get("networks")
    if isinstance(configured, dict):
        return tuple(sorted(configured))
    if isinstance(configured, list):
        return tuple(sorted(str(item) for item in configured))
    return ("default",) if "default" in available else ()


def _resources(config, kind, project_name):
    result = {}
    for logical_name, settings in (config.get(kind) or {}).items():
        settings = settings or {}
        singular = kind[:-1]
        default_name = f"{project_name}_{logical_name}"
        result[logical_name] = Resource(
            logical_name,
            str(settings.get("name") or default_name),
            bool(settings.get("external")),
            str(settings.get("driver") or ""),
        )
    return result


def _project_fallback(path):
    name = re.sub(r"[^a-z0-9_-]+", "-", path.parent.name.lower()).strip("-_")
    return name or "dockrepair"


def _inspect(arguments, cwd):
    ok, text = _run(arguments, cwd)
    if not ok:
        return False, [], text
    try:
        return True, json.loads(text) if text else [], ""
    except json.JSONDecodeError as error:
        return False, [], f"Invalid Docker inspect JSON: {error}"


def _existing_resources(resources, kind, cwd, errors):
    existing = set()
    for logical_name, resource in resources.items():
        ok, _, error = _inspect(["docker", kind, "inspect", resource.actual_name], cwd)
        if ok:
            existing.add(logical_name)
        elif error and "No such" not in error and "not found" not in error.lower():
            errors.append(error)
    return frozenset(existing)


def _published_ports(container):
    result = set()
    for container_port, bindings in ((container.get("NetworkSettings") or {}).get("Ports") or {}).items():
        target_text, _, protocol = container_port.partition("/")
        try:
            target = int(target_text)
        except ValueError:
            continue
        for binding in bindings or ():
            if binding.get("HostPort"):
                try:
                    published = int(binding["HostPort"])
                except (TypeError, ValueError):
                    continue
                result.add(PublishedPort(target, published, protocol or "tcp", binding.get("HostIp") or ""))
    return frozenset(result)


def _ip_family(host):
    host = normalize_host_ip(host)
    if host == "*":
        return None
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def _host_scopes_conflict(first, second):
    first = normalize_host_ip(first)
    second = normalize_host_ip(second)
    if "*" in (first, second) or first == second:
        return True
    if first == "0.0.0.0":
        return _ip_family(second) == socket.AF_INET
    if second == "0.0.0.0":
        return _ip_family(first) == socket.AF_INET
    # Conservatively treat the IPv6 wildcard as dual-stack. Whether the host
    # permits a parallel IPv4 bind is platform-dependent.
    if "::" in (first, second):
        return True
    return False


def _bindings_conflict(desired, actual):
    return (
        desired.published == actual.published
        and desired.protocol.lower() == actual.protocol.lower()
        and _host_scopes_conflict(desired.host_ip, actual.host_ip)
    )


def _port_matches(desired, actual):
    desired_host = normalize_host_ip(desired.host_ip)
    actual_host = normalize_host_ip(actual.host_ip)
    host_matches = (
        actual_host in {"*", "0.0.0.0", "::"}
        if desired_host == "*"
        else desired_host == actual_host
    )
    return (
        desired.target == actual.target
        and desired.published == actual.published
        and desired.protocol.lower() == actual.protocol.lower()
        and host_matches
    )


def _probe_readiness(probe):
    parsed = urllib.parse.urlsplit(probe.url)
    try:
        if parsed.scheme == "tcp":
            if not parsed.hostname or parsed.port is None:
                return False
            with socket.create_connection((parsed.hostname, parsed.port), timeout=probe.timeout):
                return True
        if parsed.scheme not in {"http", "https"}:
            return False
        request = urllib.request.Request(probe.url, headers={"User-Agent": "dockrepair/0.1"})
        context = ssl._create_unverified_context() if parsed.scheme == "https" else None
        with urllib.request.urlopen(request, timeout=probe.timeout, context=context) as response:
            return response.status in probe.statuses
    except urllib.error.HTTPError as error:
        return error.code in probe.statuses
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _health(container):
    health = ((container.get("State") or {}).get("Health") or {})
    log = health.get("Log") or ()
    output = str(log[-1].get("Output") or "").strip() if log else ""
    return health.get("Status"), output


def _mount_matches(mount, actual, volumes):
    if mount.target != actual.get("Destination") or mount.kind != actual.get("Type"):
        return False
    if mount.kind == "bind":
        try:
            return Path(actual.get("Source") or "").resolve() == Path(mount.source).resolve()
        except OSError:
            return False
    if mount.kind == "volume":
        resource = volumes.get(mount.source)
        return bool(resource and actual.get("Name") == resource.actual_name)
    return True


def collect_environment(compose_file):
    path = Path(compose_file).expanduser().resolve()
    fallback = _project_fallback(path)
    if not path.is_file():
        return Environment(str(path), fallback, {}, frozenset(), ("Compose file does not exist.",))

    cwd = path.parent
    compose = ["docker", "compose", "-f", str(path)]
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
    hash_error = None if hashes_ok else hashes_text
    networks = _resources(config, "networks", project_name)
    volumes = _resources(config, "volumes", project_name)
    services = {
        name: Service(
            _needs_healthcheck(service),
            hashes.get(name),
            _dependencies(service),
            _logical_networks(service, networks),
            _mounts(service),
            _ports(service),
            _readiness(service),
        )
        for name, service in sorted((config.get("services") or {}).items())
    }

    facts = {"compose_valid"}
    errors = [hash_error] if hash_error else []
    blocked = []
    daemon_ok, daemon_error = _run(["docker", "info", "--format", "{{.ServerVersion}}"], cwd)
    if not daemon_ok:
        return Environment(
            str(path), project_name, services, frozenset(facts), (daemon_error,),
            {}, networks, volumes,
        )
    facts.add("daemon_reachable")

    existing_networks = _existing_resources(networks, "network", cwd, errors)
    existing_volumes = _existing_resources(volumes, "volume", cwd, errors)
    for name in existing_networks:
        facts.add(f"network_exists:{name}")
    for name in existing_volumes:
        facts.add(f"volume_exists:{name}")
    for name, resource in networks.items():
        if resource.external and name not in existing_networks:
            blocked.append(f"External network '{resource.actual_name}' does not exist.")
    for name, resource in volumes.items():
        if resource.external and name not in existing_volumes:
            blocked.append(f"External volume '{resource.actual_name}' does not exist.")

    ids_ok, ids_text = _run([*compose, "ps", "--all", "--quiet"], cwd)
    container_ids = ids_text.splitlines() if ids_ok and ids_text else []
    inspect_ok, inspected, inspect_error = _inspect(["docker", "inspect", *container_ids], cwd) if container_ids else (True, [], "")
    if not inspect_ok:
        errors.append(inspect_error)
        inspected = []

    raw_by_service = {}
    for container in inspected:
        labels = (container.get("Config") or {}).get("Labels") or {}
        service_name = labels.get("com.docker.compose.service")
        if service_name:
            raw_by_service[service_name] = container

    # Inspect all running containers only to determine whether desired host ports
    # are occupied by a different container.
    running_ok, running_text = _run(["docker", "ps", "--quiet"], cwd)
    running_ids = running_text.splitlines() if running_ok and running_text else []
    all_ok, all_running, all_error = _inspect(["docker", "inspect", *running_ids], cwd) if running_ids else (True, [], "")
    if not all_ok:
        errors.append(all_error)
        all_running = []
    occupied = []
    for container in all_running:
        container_id = str(container.get("Id") or "")
        for binding in _published_ports(container):
            occupied.append((binding, container_id))

    containers = {}
    conflicts = []
    reverse_networks = {resource.actual_name: name for name, resource in networks.items()}
    for name, service in services.items():
        for mount in service.mounts:
            if mount.kind == "bind" and not mount.source_exists:
                blocked.append(f"Bind source for {name}:{mount.target} does not exist: {mount.source}")
            elif mount.kind == "bind":
                facts.add(f"bind_exists:{name}:{mount.target}")

        raw = raw_by_service.get(name)
        container_id = str((raw or {}).get("Id") or "")
        for port in service.ports:
            if port.published is None:
                continue
            occupied_by = {
                owner
                for binding, owner in occupied
                if owner != container_id and _bindings_conflict(port, binding)
            }
            if occupied_by:
                conflict = f"port_conflict:{name}:{port.key}"
                conflicts.append(conflict)
                blocked.append(f"Host port {port.key} required by {name} is occupied by another container.")
            else:
                facts.add(f"port_available:{name}:{port.key}")

        if not raw:
            continue

        state = raw.get("State") or {}
        labels = (raw.get("Config") or {}).get("Labels") or {}
        health, health_output = _health(raw)
        attached_networks = frozenset(
            reverse_networks[actual]
            for actual in ((raw.get("NetworkSettings") or {}).get("Networks") or {})
            if actual in reverse_networks
        )
        actual_mounts = raw.get("Mounts") or ()
        attached_mounts = frozenset(
            mount.target
            for mount in service.mounts
            if any(_mount_matches(mount, actual, volumes) for actual in actual_mounts)
        )
        published = _published_ports(raw)
        containers[name] = Container(
            name,
            container_id,
            str(raw.get("Name") or "").lstrip("/"),
            str(state.get("Status") or "unknown"),
            bool(state.get("Running")),
            state.get("ExitCode"),
            bool(state.get("OOMKilled")),
            int(raw.get("RestartCount") or 0),
            health,
            health_output,
            labels.get("com.docker.compose.config-hash"),
            attached_networks,
            attached_mounts,
            published,
        )

        facts.add(f"container_exists:{name}")
        if hash_error is None and (
            not service.config_hash or containers[name].config_hash == service.config_hash
        ):
            facts.add(f"config_current:{name}")
        if containers[name].running:
            facts.add(f"running:{name}")
        else:
            facts.add(f"stopped:{name}")
        if containers[name].oom_killed:
            facts.add(f"oom_killed:{name}")
        if containers[name].running and health == "healthy":
            facts.add(f"healthy:{name}")
        elif containers[name].running and health == "unhealthy":
            facts.add(f"unhealthy:{name}")
        elif containers[name].running and service.needs_healthcheck:
            facts.add(f"health_pending:{name}")
        for network in attached_networks:
            facts.add(f"network_connected:{name}:{network}")
        for target in attached_mounts:
            facts.add(f"mount_attached:{name}:{target}")
        for port in service.ports:
            if port.published is None or any(_port_matches(port, binding) for binding in published):
                facts.add(f"port_bound:{name}:{port.key}")
        if containers[name].running and service.readiness:
            if _probe_readiness(service.readiness):
                facts.add(f"endpoint_ready:{name}")
            else:
                facts.add(f"readiness_pending:{name}")

    return Environment(
        str(path), project_name, services, frozenset(facts), tuple(dict.fromkeys(errors)),
        containers, networks, volumes, existing_networks, existing_volumes,
        tuple(sorted(conflicts)), tuple(dict.fromkeys(blocked)),
    )
