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

```bash
export PATH="/opt/homebrew/bin:$PATH"
PYTHONPATH=src python3 benchmarks/dependency_bakeoff.py --repetitions 1 --fresh
PYTHONPATH=src python3 benchmarks/dependency_bakeoff.py --repetitions 10 --seed 3 --fresh
```

Optional LLM companion arm via `agy` (paper run used `gemini-3.6-flash-medium`):

```bash
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
# Smoke: one repair + one abstention scenario
PYTHONUNBUFFERED=1 PYTHONPATH=src python3 benchmarks/dependency_bakeoff.py \
  --agy-only --fresh --repetitions 1 --seed 0 \
  --scenario dependency-listener-closed \
  --scenario dependency-application \
  --agy-model gemini-3.6-flash-medium

# Paper companion protocol: 12 scenarios × 10 reps
PYTHONUNBUFFERED=1 PYTHONPATH=src python3 benchmarks/dependency_bakeoff.py \
  --agy-only --fresh --repetitions 10 --seed 3 \
  --agy-model gemini-3.6-flash-medium
```

Agy results write to `benchmarks/results/dependency_bakeoff_agy_results.json` with
programmatic `usage` (tokens), wall-clock, mutating command counts, and parsed
`status`/`diagnosis` JSON from the agent. Transcripts land in
`benchmarks/results/transcripts/`.

The first command is a smoke run. The second is the paper protocol. Symbolic
results are saved incrementally to `benchmarks/results/dependency_bakeoff_results.json`.
