# dockrepair

This is a deliberately small Docker Compose repair example. It reads the current
environment, searches for a low-cost sequence of symbolic actions, and can either
print the plan or execute it. Execution re-inspects Docker after every action and
finishes only when the goal is observed in the live environment.

## The entire implementation

- `src/dockrepair_data.py` contains the data classes.
- `src/dockrepair_docker.py` contains every Docker and Compose inspection call.
- `src/dockrepair_planner.py` contains goals, actions, and graph search.
- `src/dockrepair.py` contains execution and command-line output.

The collector reads normalized Compose configuration plus live containers,
networks, volumes, mounts, ports, health, and runtime status. It converts that
snapshot into symbolic facts. The planner uses uniform-cost search over a finite
state-transition graph whose parameterized actions can add and remove facts. A
`heapq` selects the cheapest unfinished state and a dictionary prevents costly
cycles and duplicate paths.

To follow the main control flow, start at `main()` near the bottom of
`src/dockrepair.py`. Planning mode follows this path:

```text
main() -> collect_environment() -> search() -> print_plan()
```

Execution mode calls `execute_until_resolved()`, which repeatedly runs
`search()`, executes only the first planned action, inspects Docker again, and
replans from the newly observed state.

The action catalog contains competing repair paths:

- Reconcile several broken services together, or repair them individually in
  dependency order.
- Restart an unhealthy container, or force-recreate it at higher cost.
- Create missing project networks or named volumes and restore attachments.
- Refuse repairs blocked by missing bind paths, external resources, or foreign
  port conflicts.
- Reject an action when its predicted effect is not observed, then replan.

During execution, command failure or bounded health-verification failure removes
that action edge for the exact observed state. The environment is collected
again and graph search finds the cheapest remaining path instead of aborting the
entire repair.

## Run it

```powershell
$env:PYTHONPATH = "src"
py -3.11 -m dockrepair -f .\live_fixture\compose.yaml
```

Actually repair the environment:

```powershell
$env:PYTHONPATH = "src"
py -3.11 -m dockrepair -f .\live_fixture\compose.yaml --execute
```

Use a shorter health fallback window when testing alternative paths:

```powershell
py -3.11 -m dockrepair -f .\live_fixture\compose.yaml --execute --health-timeout 5
```

The included live fixture has two services. `app` depends on a healthy
`prerequisite`. When both are stopped, the lower-cost batch edge is:

```text
1. Reconcile services together: app, prerequisite
```

Action effects remain planner predictions. In execution mode the tool runs one
action, inspects Docker, and replans from the observed state. Health-check actions
poll live container state instead of assuming that a printed check succeeded.

## Cases where graph search matters

The batch and individual paths reach the same healthy facts through different
states and costs. If batch Compose reconciliation fails, execution removes that
edge and the planner can use dependency-aware starts. Likewise, restart is the
preferred low-disruption path for an unhealthy service; if health does not
converge, forced recreation becomes the cheapest remaining path.

Live fault-injection fixtures are in `benchmarks/fixtures/flaky_start` and
`benchmarks/fixtures/recreate_fallback`. The first rejects batch reconciliation;
the second makes restart preserve container-local corruption while recreation
clears it.

## Recreate the broken fixture

These commands really change Docker state:

```powershell
docker compose -f .\live_fixture\compose.yaml up -d --wait
docker compose -f .\live_fixture\compose.yaml stop app prerequisite
```

Remove only this fixture with:

```powershell
docker compose -f .\live_fixture\compose.yaml down
```
