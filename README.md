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

During execution, command failure or bounded health/readiness verification
failure removes that action edge for the exact observed state. The environment
is collected again and graph search finds the cheapest remaining path instead
of aborting the entire repair.

Published-port facts require the observed target port, published port, protocol,
and host-IP scope to match the Compose declaration. Wildcard and specific IPv4
or IPv6 bindings remain distinct, and conflict detection uses the same host
scope semantics.

## Optional application readiness

A service can declare an HTTP, HTTPS, or TCP readiness probe with Compose
labels. When configured, `endpoint_ready:<service>` becomes part of the
symbolic goal instead of treating a merely running container as sufficient:

```yaml
services:
  app:
    labels:
      com.dockrepair.readiness.url: "http://localhost:8080/ready"
      com.dockrepair.readiness.statuses: "200-399"
      com.dockrepair.readiness.timeout: "2"
```

Only the URL is required. HTTP status codes default to `200-399`; TCP probes use
a URL such as `tcp://localhost:6884`. The per-probe timeout bounds one attempt,
while `--health-timeout` bounds polling for both Docker health and application
readiness. A failed readiness observation makes restart and then recreation
available as fallback paths.

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
