# Paired Docker repair benchmark

This harness runs one scenario at a time and enforces this order:

1. Recreate a broken Docker environment (setup is not timed).
2. Start the LLM wall-clock timer.
3. Let the LLM inspect and actually repair Docker.
4. Verify the live goal and stop the LLM timer.
5. Recreate the identical failure (not timed).
6. Run and time `dockrepair --execute`.
7. Independently verify the live goal and save the paired result.

The verifier requires the daemon, current Compose configuration, every declared
container, every running state, and every declared health check. Editing a
Compose fixture invalidates the trial.

## Run one paired trial

```powershell
py -3.11 .\benchmarks\run_benchmark.py list
py -3.11 .\benchmarks\run_benchmark.py prepare-llm --scenario unhealthy
```

Give the printed Compose path to the LLM and have it execute the repair. Plans or
suggested commands are not sufficient. Immediately after the LLM finishes:

```powershell
py -3.11 .\benchmarks\run_benchmark.py finish-llm --scenario unhealthy
py -3.11 .\benchmarks\run_benchmark.py run-app --scenario unhealthy
```

Repeat those three commands for the next scenario. View accumulated comparisons:

```powershell
py -3.11 .\benchmarks\run_benchmark.py results
```

To reverse the order and run the app before the LLM:

```powershell
py -3.11 .\benchmarks\run_benchmark.py run-app-first --scenario unhealthy
py -3.11 .\benchmarks\run_benchmark.py prepare-llm-after-app --scenario unhealthy
# Let the LLM execute and finish the repair, then immediately record it:
py -3.11 .\benchmarks\run_benchmark.py finish-llm-after-app --scenario unhealthy
```

Useful maintenance commands:

```powershell
py -3.11 .\benchmarks\run_benchmark.py status
py -3.11 .\benchmarks\run_benchmark.py abort
py -3.11 .\benchmarks\run_benchmark.py cleanup
```

`unhealthy`, `recreate-fallback`, and `flaky-start` are the key adaptive cases.
`recreate-fallback` makes the cheaper restart path fail verification so graph
search must remove that edge and select forced recreation. `flaky-start` makes
the cheapest batch edge fail, after which the planner uses dependency-aware
individual actions.
