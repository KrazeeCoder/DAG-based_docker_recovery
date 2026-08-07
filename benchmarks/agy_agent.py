from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_AGY_MODEL = os.environ.get("DOCKREPAIR_AGY_MODEL", "gemini-3.6-flash-low")
DEFAULT_AGY_TIMEOUT_SEC = int(os.environ.get("DOCKREPAIR_AGY_TIMEOUT_SEC", "600"))

_MUTATING_PATTERNS = (
    re.compile(r"\bdocker\s+compose\b.*\b(up|start|restart|stop|kill|rm|down|create)\b", re.I),
    re.compile(r"\bdocker\s+(start|restart|stop|kill|rm|run|exec|network|volume|container)\b", re.I),
    re.compile(r"\bdocker\s+network\s+(connect|disconnect|rm|create)\b", re.I),
)

_DIAGNOSIS_CODES = {
    "TARGET_UNAVAILABLE",
    "CALLER_UNAVAILABLE",
    "CALLER_NETWORK_DRIFT",
    "TARGET_NETWORK_DRIFT",
    "TARGET_NOT_LISTENING",
    "DNS_FAILURE",
    "APPLICATION_OR_UNKNOWN",
    "TARGET_OOM_KILLED",
    "TARGET_CONFIG_DRIFT",
    "NETWORK_PATH_FAILURE",
    "CONTRACT_HEALTHY",
    "PROBE_UNAVAILABLE",
}


def agy_available():
    return shutil.which("agy") is not None


def build_prompt(scenario):
    compose_path = scenario.compose_file.resolve()
    project_hint = compose_path.parent.name
    return f"""You are repairing a single-host Docker Compose dependency failure.

Compose file: {compose_path}
Work only on this Compose project. Do not edit, create, or delete any files
(including the Compose YAML). Do not mutate containers, networks, or volumes
outside this project.

A declared TCP dependency is currently unusable or the caller is unready.
Inspect with docker/compose before mutating. Prefer the smallest repair that
restores the declared dependency contract (service DNS names, shared networks,
running listeners). If the failure is application-semantic, wrong contract,
persistent after a bounded repair attempt, missing DNS alias that Compose did
not declare, or otherwise outside safe infrastructure repair, STOP and abstain
instead of thrashing.

Budget: at most 20 Docker-mutating commands.

When finished, end your final message with exactly one JSON object on its own
line (no markdown fence) with this schema:
{{"status":"RESTORED"|"ABSTAINED","diagnosis":"<CODE>","notes":"<short>"}}

diagnosis must be one of:
{', '.join(sorted(_DIAGNOSIS_CODES))}

Project folder hint: {project_hint}. Scenario id: {scenario.name}.
"""


def _empty_usage():
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "thinking_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 0,
    }


def parse_stream_events(text):
    """Parse agy --output-format stream-json stdout into events."""
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] not in "{[":
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def extract_usage(events):
    usage = _empty_usage()
    for event in reversed(events):
        if event.get("event") == "result":
            result = event.get("result") or {}
            raw = result.get("usage") or {}
            for key in usage:
                usage[key] = int(raw.get(key) or 0)
            return usage, result
    return usage, {}


def extract_commands(events):
    commands = []
    for event in events:
        step = event.get("step_update") or {}
        if step.get("tool_name") != "run_command" or step.get("state") != "DONE":
            continue
        info = step.get("tool_info") or {}
        params = info.get("parameters") or {}
        command = params.get("CommandLine") or params.get("command") or ""
        if command:
            commands.append(command)
    return commands


def is_mutating_command(command):
    return any(pattern.search(command) for pattern in _MUTATING_PATTERNS)


def parse_final_report(response_text):
    """Extract the trailing status/diagnosis JSON from the agent response."""
    text = response_text or ""
    # Prefer the last JSON object that looks like our schema.
    candidates = re.findall(r"\{[^{}]*\}", text)
    for blob in reversed(candidates):
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        status = str(data.get("status") or "").upper()
        diagnosis = str(data.get("diagnosis") or "").upper()
        if status in {"RESTORED", "ABSTAINED"} or diagnosis in _DIAGNOSIS_CODES:
            return {
                "status": status or None,
                "diagnosis": diagnosis or None,
                "notes": data.get("notes"),
                "raw": data,
            }
    # Fallback: scan for known diagnosis tokens.
    for code in _DIAGNOSIS_CODES:
        if code in text:
            status = "ABSTAINED" if "ABSTAIN" in text.upper() else None
            if "RESTORED" in text.upper():
                status = "RESTORED"
            return {"status": status, "diagnosis": code, "notes": None, "raw": None}
    return {"status": None, "diagnosis": None, "notes": None, "raw": None}


def run_agy_trial(
    scenario,
    *,
    model=DEFAULT_AGY_MODEL,
    timeout_sec=DEFAULT_AGY_TIMEOUT_SEC,
    transcript_path=None,
    cwd=None,
):
    """Invoke agy non-interactively and return structured metrics."""
    if not agy_available():
        raise RuntimeError("agy CLI not found on PATH")

    prompt = build_prompt(scenario)
    command = [
        "agy",
        "-p", prompt,
        "--model", model,
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--print-timeout", f"{max(1, int(timeout_sec))}s",
        "--add-dir", str(scenario.compose_file.resolve().parent),
    ]
    timed_out = False
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd or ROOT),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec + 30,
        )
        return_code = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as error:
        timed_out = True
        return_code = 124
        stdout = error.stdout if isinstance(error.stdout, str) else ""
        stderr = (error.stderr if isinstance(error.stderr, str) else "") or "timeout"

    if transcript_path is not None:
        path = Path(transcript_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            stdout + (f"\n# stderr\n{stderr}\n" if stderr else ""),
            encoding="utf-8",
        )

    events = parse_stream_events(stdout)
    usage, result = extract_usage(events)
    commands = extract_commands(events)
    mutating = [cmd for cmd in commands if is_mutating_command(cmd)]
    response = result.get("response") or ""
    report = parse_final_report(response)
    status = (result.get("status") or ("TIMEOUT" if timed_out else "UNKNOWN")).upper()

    return {
        "return_code": return_code,
        "timed_out": timed_out,
        "agy_status": status,
        "model": model,
        "duration_seconds": float(result.get("duration_seconds") or 0.0),
        "num_turns": int(result.get("num_turns") or 0),
        "usage": usage,
        "commands": commands,
        "mutating_commands": mutating,
        "mutations": len(mutating),
        "response": response,
        "report": report,
        "transcript_path": str(transcript_path) if transcript_path else None,
        "stderr_tail": stderr[-2000:],
    }
