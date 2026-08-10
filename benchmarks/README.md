# Benchmarks

Primary study: declared TCP dependency faults.

```bash
export PATH="/opt/homebrew/bin:$PATH"
# smoke
PYTHONPATH=src python3 benchmarks/dependency_bakeoff.py --fresh --repetitions 1 --seed 0
# paper protocol (symbolic)
PYTHONPATH=src python3 benchmarks/dependency_bakeoff.py --fresh --repetitions 10 --seed 3
# LLM companion (needs agy on PATH)
PYTHONPATH=src python3 benchmarks/dependency_bakeoff.py --agy-only --fresh --repetitions 10 --seed 3
```

Arms: `compose_up`, `reactive_restart`, `shallow`, `diagnostic`, optional `agy`.
Results: `benchmarks/results/*.json` (transcripts are gitignored).
Fixtures: `benchmarks/fixtures/` (robot cart slice under `robot_cart/`).

Lifecycle pilot (older, non-primary):

```bash
PYTHONPATH=src python3 benchmarks/bakeoff.py run --fresh --repetitions 1 --seed 3 --skip-codex
```
