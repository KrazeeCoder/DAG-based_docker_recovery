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
