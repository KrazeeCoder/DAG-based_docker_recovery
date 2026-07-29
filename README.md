# dockrepair

This is a deliberately small Docker Compose repair example. It reads the current
environment, searches for a low-cost sequence of symbolic actions, and can either
print the plan or execute it. Execution re-inspects Docker after every action and
finishes only when the goal is observed in the live environment.

## The entire implementation

- `src/dockrepair_data.py` contains the data classes.
- `src/dockrepair_docker.py` contains every Docker and Compose inspection call.
- `src/dockrepair.py` contains goals, actions, search, and command-line output.

The planner uses uniform-cost **graph search**. Symbolic states are graph nodes
and actions are edges. A `heapq` priority queue always selects the cheapest
unfinished node, while a dictionary prevents a more expensive path from
revisiting an equivalent state.

The model intentionally stays small. Missing or configuration-stale services
use one `docker compose up` reconciliation action; Compose itself handles image
pulling, building, and container creation.

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

The included live fixture has two services. `app` depends on a healthy
`prerequisite`. They are currently stopped, so the expected plan is:

```text
1. Start prerequisite
2. Verify prerequisite health
3. Start app
```

Action effects remain planner predictions. In execution mode the tool runs one
action, inspects Docker, and replans from the observed state. Health-check actions
poll live container state instead of assuming that a printed check succeeded.

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
