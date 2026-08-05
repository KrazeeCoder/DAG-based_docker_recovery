"""Local and external Compose fault fixtures for the bakeoff."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from dockrepair import _start_docker_engine  # noqa: E402
from dockrepair_docker import collect_environment  # noqa: E402
from dockrepair_planner import build_goal  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
STATE_ROOT = Path.home() / ".dockrepair-bench"


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    compose_file: Path
    setup: Callable[["Scenario"], None]
    extra_files: tuple[Path, ...] = ()


def run(arguments, *, check=True, quiet=False, cwd=None):
    result = subprocess.run(
        arguments,
        cwd=cwd or ROOT,
        capture_output=quiet,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Command failed ({result.returncode}): {arguments}\n{detail}")
    return result


def compose(path, *arguments, check=True, quiet=False):
    return run(["docker", "compose", "-f", str(path), *arguments], check=check, quiet=quiet)


def clean_project(path):
    compose(path, "down", "--remove-orphans", check=False, quiet=True)


def ensure_daemon():
    ready, message = _start_docker_engine(timeout=180.0)
    if not ready:
        raise RuntimeError(message)


def wait_for_fact(path, wanted, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if wanted in collect_environment(str(path)).facts:
            return
        time.sleep(0.5)
    raise RuntimeError(f"Setup missed {wanted} within {timeout:g}s")


def inspect_goal(path):
    environment = collect_environment(str(path))
    missing = sorted(build_goal(environment) - environment.facts)
    return not missing, missing, environment.errors


def assert_broken(scenario):
    healthy, missing, _ = inspect_goal(scenario.compose_file)
    if healthy:
        raise RuntimeError(f"{scenario.name} already healthy after setup")
    return missing


def file_hashes(scenario):
    paths = (scenario.compose_file, *scenario.extra_files)
    return {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def assert_files_unchanged(expected):
    actual = {
        name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
        for name in expected
        if Path(name).is_file()
    }
    if actual != expected:
        raise RuntimeError("Compose file changed during repair")


def _write_state_script(name, body):
    directory = STATE_ROOT / name
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "run.sh"
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)
    return directory


def setup_stopped_chain(scenario):
    clean_project(scenario.compose_file)
    compose(scenario.compose_file, "up", "-d", "--wait", "--wait-timeout", "30", quiet=True)
    compose(scenario.compose_file, "stop", "worker", "api", "database", quiet=True)


def setup_partial_stop(scenario):
    clean_project(scenario.compose_file)
    compose(scenario.compose_file, "up", "-d", "--wait", "--wait-timeout", "30", quiet=True)
    compose(scenario.compose_file, "stop", "worker4", quiet=True)


def setup_missing_service(scenario):
    clean_project(scenario.compose_file)
    compose(scenario.compose_file, "up", "-d", "--wait", "--wait-timeout", "30", quiet=True)
    compose(scenario.compose_file, "rm", "-s", "-f", "worker", quiet=True)


def setup_config_drift(scenario):
    old_file = scenario.extra_files[0]
    clean_project(scenario.compose_file)
    clean_project(old_file)
    compose(old_file, "up", "-d", "--wait", "--wait-timeout", "30", quiet=True)


def setup_unhealthy(scenario):
    clean_project(scenario.compose_file)
    compose(scenario.compose_file, "up", "-d", "--wait", "--wait-timeout", "30", quiet=True)
    container_id = compose(scenario.compose_file, "ps", "-q", "cache", quiet=True).stdout.strip()
    if not container_id:
        raise RuntimeError("missing cache container")
    run(["docker", "exec", container_id, "rm", "-f", "/tmp/healthy"], quiet=True)
    wait_for_fact(scenario.compose_file, "unhealthy:cache", timeout=15)


def setup_recreate_fallback(scenario):
    _write_state_script(
        "recreate_fallback",
        "#!/bin/sh\n"
        "if [ ! -f /tmp/corrupt ]; then\n"
        "  touch /tmp/healthy\n"
        "fi\n"
        "while true; do sleep 3600; done\n",
    )
    clean_project(scenario.compose_file)
    compose(scenario.compose_file, "up", "-d", "--wait", "--wait-timeout", "30", quiet=True)
    container_id = compose(scenario.compose_file, "ps", "-q", "cache", quiet=True).stdout.strip()
    if not container_id:
        raise RuntimeError("missing cache container")
    run(
        ["docker", "exec", container_id, "sh", "-c", "touch /tmp/corrupt; rm -f /tmp/healthy"],
        quiet=True,
    )
    wait_for_fact(scenario.compose_file, "unhealthy:cache", timeout=15)


def setup_flaky_start(scenario):
    _write_state_script(
        "flaky_start",
        "#!/bin/sh\n"
        "if [ ! -f /tmp/first_start_failed ]; then\n"
        "  touch /tmp/first_start_failed\n"
        "  exit 1\n"
        "fi\n"
        "while true; do sleep 3600; done\n",
    )
    clean_project(scenario.compose_file)
    compose(scenario.compose_file, "create", quiet=True)


def setup_robot_shop_stop_cart(scenario):
    clean_project(scenario.compose_file)
    compose(
        scenario.compose_file, "up", "-d", "--wait", "--wait-timeout", "180",
        quiet=False,
    )
    compose(scenario.compose_file, "stop", "cart", quiet=True)


SCENARIOS = {
    "stopped-chain": Scenario(
        "stopped-chain",
        "stopped dependency chain",
        FIXTURES / "stopped_chain" / "compose.yaml",
        setup_stopped_chain,
    ),
    "partial-stop": Scenario(
        "partial-stop",
        "one stopped peer among healthy services",
        FIXTURES / "partial_stop" / "compose.yaml",
        setup_partial_stop,
    ),
    "missing-service": Scenario(
        "missing-service",
        "removed container",
        FIXTURES / "missing_service" / "compose.yaml",
        setup_missing_service,
    ),
    "config-drift": Scenario(
        "config-drift",
        "running config hash behind desired",
        FIXTURES / "config_drift" / "compose-v2.yaml",
        setup_config_drift,
        (FIXTURES / "config_drift" / "compose-v1.yaml",),
    ),
    "unhealthy": Scenario(
        "unhealthy",
        "unhealthy dependency, restart repairs",
        FIXTURES / "unhealthy" / "compose.yaml",
        setup_unhealthy,
    ),
    "recreate-fallback": Scenario(
        "recreate-fallback",
        "restart fails, recreate needed",
        FIXTURES / "recreate_fallback" / "compose.yaml",
        setup_recreate_fallback,
    ),
    "flaky-start": Scenario(
        "flaky-start",
        "first start fails, second succeeds",
        FIXTURES / "flaky_start" / "compose.yaml",
        setup_flaky_start,
    ),
    "robot-shop-stop-cart": Scenario(
        "robot-shop-stop-cart",
        "real Robot Shop cart image stopped",
        FIXTURES / "robot_cart" / "compose.yaml",
        setup_robot_shop_stop_cart,
    ),
}
