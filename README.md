# DockRepair

Symbolic planner for single-host Docker Compose repair. Observe live Docker and
Compose state as facts, find a low-cost action sequence, and optionally execute
it with re-observation after every step. Dependency mode adds active probes,
declared TCP contracts, and abstention when a fault is outside the action
catalog.

Requires Python 3.11+ and a working Docker/Compose engine. No extra Python packages.

## Install

```bash
pip install -e .
# or
export PYTHONPATH=src
```

## Use

```bash
# Plan only
python3 -m dockrepair path/to/compose.yaml

# Plan and execute
python3 -m dockrepair path/to/compose.yaml --execute

# Declared dependency diagnosis / repair
python3 -m dockrepair path/to/compose.yaml --dependency
```

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

## Evaluation

Opaque dependency-fault bakeoff (Compose up, reactive restart, shallow planner,
full diagnosis):

```bash
PYTHONPATH=src python3 benchmarks/dependency_bakeoff.py --fresh --repetitions 10 --seed 3
```

Optional LLM arm (`agy`):

```bash
PYTHONPATH=src python3 benchmarks/dependency_bakeoff.py --agy-only --fresh --repetitions 10 --seed 3
```

Fixtures live under `benchmarks/fixtures/`. Aggregates used in the paper are under
`paper/`. See `benchmarks/README.md` for protocol details.

## Paper

MIT URTC manuscript and figures:

- PDF: `paper/submissions/DockRepair_MIT_URTC.pdf`
- Source notes: `paper/urtc_paper.md`
- Figures: `paper/figures/`
- Result summaries: `paper/results_summary.json`
