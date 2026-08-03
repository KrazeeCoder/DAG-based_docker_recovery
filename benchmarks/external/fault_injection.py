"""Generic, manifest-driven fault injection for external Docker Compose apps.

Unlike the local fixtures in run_benchmark.py (which each have a hand-written
setup_* function), external apps are injected generically from a JSON
manifest so the same code works across LO2 light-oauth2, retail-store, and
robot-shop without writing per-app Python.

Manifest schema (one JSON file per scenario):
{
  "id": "robot-shop-stop-cart",
  "application": "robot-shop",
  "compose_file": "benchmarks/external/robot-shop/docker-compose.yaml",
  "fault": "stopped_service",
  "target": "cart",
  "expected_repairable": true
}

Supported "fault" values and their required extra manifest fields:
  stopped_service      -> target: str
  dependency_chain      -> target: list[str]
  missing_service       -> target: str
  network_disconnect    -> target: str, network: str
  outdated_config       -> old_compose_file: str  (bring this up first, then
                            the manifest's compose_file is the "desired" one)
  unhealthy             -> target: str, fault_exec: list[str]  (docker exec
                            argv run inside the target's container to break
                            its health check, e.g. ["rm", "-f", "/tmp/healthy"]
                            or ["pkill", "some-process"] -- this is
                            necessarily app-specific since "unhealthy" means
                            different things per app)
  missing_bind_mount    -> bind_path: str  (host path to rename away)
  port_conflict         -> port: int, protocol: str (default "tcp")
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _run(arguments, cwd=None, check=True):
    result = subprocess.run(
        arguments, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Command failed: {' '.join(arguments)}\n{detail}")
    return result


def _compose(compose_file, *arguments, check=True):
    return _run(["docker", "compose", "-f", str(compose_file), *arguments], check=check)


def load_manifest(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _container_id(compose_file, service):
    result = _compose(compose_file, "ps", "-q", service)
    return result.stdout.strip()


def bring_up_clean(manifest):
    """Untimed setup: bring the app up healthy before injecting the fault."""

    compose_file = manifest["compose_file"]
    _compose(compose_file, "down", "--remove-orphans", check=False)
    if manifest["fault"] == "outdated_config":
        old_file = manifest["old_compose_file"]
        _compose(old_file, "down", "--remove-orphans", check=False)
        _compose(old_file, "up", "-d", "--wait", "--wait-timeout", "60")
    else:
        _compose(compose_file, "up", "-d", "--wait", "--wait-timeout", "60")


def inject_fault(manifest):
    """Break exactly one thing, per the manifest's declared fault type."""

    compose_file = manifest["compose_file"]
    fault = manifest["fault"]

    if fault == "stopped_service":
        _compose(compose_file, "stop", manifest["target"])

    elif fault == "dependency_chain":
        _compose(compose_file, "stop", *manifest["target"])

    elif fault == "missing_service":
        _compose(compose_file, "rm", "-s", "-f", manifest["target"])

    elif fault == "network_disconnect":
        container_id = _container_id(compose_file, manifest["target"])
        if not container_id:
            raise RuntimeError(f"Could not find container for service {manifest['target']}")
        _run(["docker", "network", "disconnect", manifest["network"], container_id])

    elif fault == "outdated_config":
        # bring_up_clean already started the OLD compose file; nothing more
        # to do here -- the "fault" is simply that the desired (new) config
        # in manifest["compose_file"] has not been applied yet.
        pass

    elif fault == "unhealthy":
        container_id = _container_id(compose_file, manifest["target"])
        if not container_id:
            raise RuntimeError(f"Could not find container for service {manifest['target']}")
        _run(["docker", "exec", container_id, *manifest["fault_exec"]])

    elif fault == "missing_bind_mount":
        bind_path = Path(manifest["bind_path"])
        if bind_path.exists():
            bind_path.rename(bind_path.with_name(bind_path.name + ".hidden-for-fault"))

    elif fault == "port_conflict":
        protocol = manifest.get("protocol", "tcp")
        port = manifest["port"]
        _run([
            "docker", "run", "-d", "--name", f"fault-port-blocker-{port}",
            "-p", f"{port}:{port}/{protocol}", "busybox", "sleep", "3600",
        ])

    else:
        raise ValueError(f"Unknown fault type: {fault}")


def clean_up_fault_artifacts(manifest):
    """Undo anything injected outside the app's own containers (port blockers,
    renamed bind paths) so repeated trials start from a truly clean slate."""

    fault = manifest["fault"]
    if fault == "port_conflict":
        port = manifest["port"]
        _run(["docker", "rm", "-f", f"fault-port-blocker-{port}"], check=False)
    elif fault == "missing_bind_mount":
        bind_path = Path(manifest["bind_path"])
        hidden = bind_path.with_name(bind_path.name + ".hidden-for-fault")
        if hidden.exists() and not bind_path.exists():
            hidden.rename(bind_path)