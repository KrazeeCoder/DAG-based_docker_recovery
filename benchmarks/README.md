# Bakeoff

Four arms on opaque Compose faults: `compose_up`, `naive`, `planner`, `codex`.

```bash
export PATH="/opt/homebrew/bin:$PATH"
colima start
docker context use colima
PYTHONPATH=src python3 benchmarks/bakeoff.py list
PYTHONUNBUFFERED=1 PYTHONPATH=src python3 benchmarks/bakeoff.py run --fresh --repetitions 1 --seed 3
PYTHONPATH=src python3 benchmarks/bakeoff.py summary
```

`--skip-codex` for planner arms only. `--scenario unhealthy` for one case.

Adaptive fixtures mount sabotage scripts from `~/.dockrepair-bench/` (not in git).
Robot Shop cart slice: `benchmarks/fixtures/robot_cart/`.

Results: `benchmarks/results/` (gitignored). Paper: `paper/results_summary.json`.

## Dependency-failure study

The active-diagnosis study compares `compose_up`, a health-triggered
`reactive_restart`, the original `shallow` planner, and the full `diagnostic`
planner. Every scenario declares its injected diagnosis and whether verified
repair or safe abstention is expected.

```powershell
$env:PYTHONPATH = "src"
py -3.11 benchmarks/dependency_bakeoff.py --repetitions 1 --fresh
py -3.11 benchmarks/dependency_bakeoff.py --repetitions 10 --seed 3 --fresh
```

The first command is a smoke run. The second is the paper protocol. Results are
saved incrementally to `benchmarks/results/dependency_bakeoff_results.json`.
