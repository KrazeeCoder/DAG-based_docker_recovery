# DockRepair: Safe Symbolic Planning for Docker Compose Repair

**Authors:** [Authors]  
**Affiliation:** [Institution]  
**Venue:** IEEE MIT Undergraduate Research Technology Conference (URTC) 2026

## Abstract

Ad-hoc `docker compose up` and LLM coding agents both repair broken Compose stacks, but the former misses adaptive faults and the latter is slow and unbounded. DockRepair is a small deterministic planner: it inspects live Compose state into symbolic facts, searches a deny-by-default action catalog with lexicographic uniform-cost search, and executes one action at a time with live re-inspection and replan. We evaluate four arms on eight opaque faults (seven synthetic fixtures plus a Robot Shop cart slice): blind `compose up --wait`, a dependency-blind naive planner, DockRepair, and Codex (`gpt-5.6-terra`). Blind `up` repairs only 5/8 cases. DockRepair, the naive planner, and Codex all reach 8/8. DockRepair matches that success at about one-quarter Codex wall-clock (12.0s vs 49.2s mean), uses fewer mutations than the naive arm on dependency chains, and cannot edit project files by construction. Constrained symbolic repair is a practical latency and safety layer beside unbounded LLM ops agents.

## I. Introduction

Docker Compose is the default local orchestration tool for multi-service applications. When stacks drift—stopped dependents, unhealthy healthchecks, config-hash mismatch—operators typically run `docker compose up` or hand-written scripts. Frontier coding agents can repair such environments, but they are non-deterministic, may edit project files, and offer weak guarantees about what they will touch.

DockRepair asks: can a deny-by-default symbolic planner with Compose dependency knowledge (1) beat blind `compose up` on adaptive faults, (2) match a frontier agent on success under opaque injection, and (3) win on wall-clock and mutation efficiency?

**Contributions.** (1) Inspect–plan–execute–replan over Compose facts with lexicographic costs prioritizing data risk. (2) A four-arm bakeoff with opaque fault injection. (3) Results on eight scenarios including one real-image Robot Shop cart slice.

We do **not** claim image-layer DAG recovery, Kubernetes support, or multi-replica repair.

## II. System

### A. Facts and goals

DockRepair reads normalized `docker compose config` plus live containers, networks, volumes, mounts, ports, and health into string facts (`running:api`, `healthy:database`, …). The goal is the fact set implied by the Compose file.

### B. Action catalog and safety

Operators include batch/individual reconcile, start/restart/recreate, resource create/connect, completion jobs, and observation edges. A deny-by-default validator admits only cataloged, project-scoped mutations—no deletes, foreign mutations, port eviction, or file edits. Missing binds, external resources, and foreign port conflicts are hard blockers.

### C. Search, cost, execute

Lexicographic UCS minimizes `(data-risk, destructiveness, disruption, actions)`. Execution runs only the first planned edge, recollects live state, verifies effects, and excludes failed edges so search can fall back (restart → recreate).

```
Compose + live Docker
        |
        v
  collect facts ----> UCS search ----> first action
        ^                                   |
        |                                   v
        +--------- re-inspect / exclude ----+
```

Figure 1. DockRepair control loop.

## III. Evaluation

### A. Protocol

Each trial: recreate an opaque broken environment (untimed), run one arm under wall-clock timing, verify the live DockRepair goal **and** unchanged Compose file hashes, then recreate the fault for the next arm. Arm order is randomized. Fault recipes are not written into tracked Compose YAML for adaptive cases; sabotage scripts live under `~/.dockrepair-bench/` and bind-mount at runtime. Codex receives only the Compose path and a no-file-edit safety prompt.

**Arms.** `compose_up` (`docker compose up -d --wait`); `naive` (same catalog, ignores `depends_on`-style preconditions, no batch reconcile); `planner` (DockRepair); `codex` (`codex exec --ephemeral`, model `gpt-5.6-terra`).

**Scenarios.** stopped-chain; partial-stop; missing-service; config-drift; unhealthy; recreate-fallback; flaky-start; robot-shop-stop-cart (public `robotshop/rs-cart:2.1.0` + Redis).

**Metrics.** Success (goal facts + hashes); wall-clock seconds; mutating actions (planner arms; observations excluded).

### B. Results

One repetition per scenario (seed 3); Colima Docker on macOS arm64. Summary: `paper/results_summary.json`.

**Table I. Aggregate (n=8).**

| Arm | Success | Mean wall-clock (s) | Mean mutating actions | Compose edits |
|-----|---------|---------------------|-----------------------|---------------|
| compose_up | 5/8 (62.5%) | 3.16 | 1.0 | 0 |
| naive | 8/8 (100%) | 12.23 | 1.50 | 0 |
| planner | 8/8 (100%) | 12.01 | 1.38 | 0 |
| codex | 8/8 (100%) | 49.22 | — | 0 |

**Table II. Success and wall-clock by scenario.**

| Scenario | up | naive | planner | codex |
|----------|----|-------|---------|-------|
| stopped-chain | Y 4.0s | Y 6.2s | Y 4.8s | Y 43.6s |
| partial-stop | Y 0.8s | Y 1.3s | Y 1.1s | Y 27.5s |
| missing-service | Y 0.8s | Y 1.1s | Y 1.3s | Y 35.6s |
| config-drift | Y 11.8s | Y 11.5s | Y 11.4s | Y 42.7s |
| unhealthy | N 0.6s | Y 12.9s | Y 12.4s | Y 54.6s |
| recreate-fallback | N 0.7s | Y 54.6s | Y 54.7s | Y 76.7s |
| flaky-start | N 0.7s | Y 4.3s | Y 4.5s | Y 78.9s |
| robot-shop-stop-cart | Y 5.8s | Y 5.9s | Y 5.9s | Y 34.2s |

**Table III. Mutating actions (planner arms).**

| Scenario | planner | naive |
|----------|---------|-------|
| stopped-chain | 1 | 2 |
| partial-stop | 1 | 1 |
| missing-service | 1 | 1 |
| config-drift | 1 | 1 |
| unhealthy | 1 | 1 |
| recreate-fallback | 2 | 2 |
| flaky-start | 3 | 3 |
| robot-shop-stop-cart | 1 | 1 |

### C. Findings

**Blind `up` is not enough.** It fails unhealthy, recreate-fallback, and flaky-start—the adaptive cases that need restart/recreate fallback or inspect-and-replan.

**Latency vs Codex.** DockRepair matches 8/8 success at 12.0s mean vs 49.2s (~4.1×). Every scenario is faster under DockRepair than under Codex.

**Dependency ablation.** On stopped-chain, DockRepair uses one batch reconcile (1 mutation) vs 2 for naive. Overall mean mutations 1.38 vs 1.50.

**Safety.** Planner arms cannot edit files. Codex left Compose hashes unchanged on all eight trials under the safety prompt, but remains unbounded outside that prompt.

## IV. Related Work

Compose reconciliation (`up --wait`) handles many stopped/missing cases but not the adaptive failures above. Classical planning (STRIPS/UCS) motivates the fact/action encoding. LLM ops agents are strong but weakly bounded; we treat Codex as that baseline under matched constraints. Full-stack sample apps (Robot Shop, retail-store, light-oauth2) remain useful for broader future suites.

## V. Limitations

Single-replica Compose only. Hard blockers refused. Costs are policy, not latency. Goal oracle is DockRepair's fact model. n=1. Adaptive sabotage scripts live on the host bind-mount (not in tracked YAML), but a sufficiently thorough agent could still inspect mounted files. Robot Shop case is a cart+Redis slice, not the full multi-service demo (shipping was unhealthy on our arm64 VM). LLM mutation counts omitted (unreliable from transcripts).

## VI. Conclusion

DockRepair shows that deny-by-default symbolic Compose repair with dependency-aware UCS and live replan beats blind `compose up` on adaptive faults, matches a frontier coding agent on opaque scenarios, and cuts wall-clock by about 4× while bounding the mutation surface. Dependency knowledge trims mutations on multi-service chains versus a same-catalog naive baseline.

## Acknowledgments

[BWSI / mentors / compute acknowledgments as appropriate.]

## References

[1] Docker Inc., “Docker Compose specification,” https://docs.docker.com/compose/  
[2] R. E. Fikes and N. J. Nilsson, “STRIPS: A new approach to the application of theorem proving to problem solving,” Artificial Intelligence, 1971.  
[3] OpenAI, “Codex CLI,” https://github.com/openai/codex  
[4] Instana, “Stan's Robot Shop,” https://github.com/instana/robot-shop  
[5] AWS Samples, “Retail Store Sample App,” https://github.com/aws-containers/retail-store-sample-app  
