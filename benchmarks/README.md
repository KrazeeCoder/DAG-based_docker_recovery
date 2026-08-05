# Bakeoff

Compare Codex, naive planner, and DockRepair on local Compose faults.

```bash
export PATH="/opt/homebrew/bin:$PATH"
colima start
docker context use colima
PYTHONPATH=src python3 benchmarks/bakeoff.py list
PYTHONPATH=src python3 benchmarks/bakeoff.py run --repetitions 1 --seed 2
PYTHONPATH=src python3 benchmarks/bakeoff.py summary
```

Planner-only: `PYTHONPATH=src python3 benchmarks/bakeoff.py run --skip-codex`

Results go under `benchmarks/results/`. Paper numbers: `paper/results_summary.json`.
