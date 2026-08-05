# DockRepair: Safe Symbolic Planning for Docker Compose Repair

**Authors:** [Authors]  
**Affiliation:** [Institution]  
**Venue:** IEEE MIT Undergraduate Research Technology Conference (URTC) 2026

## Abstract

Broken Docker Compose environments are usually repaired by ad-hoc shell commands or by large language model (LLM) coding agents that can mutate arbitrary state. We present DockRepair, a small deterministic planner that inspects live Compose state into symbolic facts, searches a deny-by-default action catalog with lexicographic uniform-cost search, and executes one action at a time with live re-inspection and replan. On seven local Compose fault scenarios we compare DockRepair against (i) a dependency-blind planner that uses the same action catalog and (ii) OpenAI Codex (`codex exec`, model `gpt-5.6-terra`) under an identical no-file-edit safety prompt. All three arms repaired every scenario. DockRepair matched Codex success at roughly one-quarter the mean wall-clock time, used fewer mutating actions than the naive baseline on dependency chains, and never edited Compose files by construction. We argue that constrained symbolic repair is a practical, reproducible complement to unbounded LLM ops agents when latency, auditability, and safety matter.

## I. Introduction

Docker Compose is the default local orchestration tool for multi-service applications. When stacks drift—stopped dependents, unhealthy healthchecks, config-hash mismatch—operators typically run `docker compose up` or hand-written scripts. Frontier coding agents can repair such environments, but they are non-deterministic, may edit project files, and offer weak guarantees about what they will touch.

DockRepair asks a narrower question: can a *deny-by-default symbolic planner* with Compose dependency knowledge match LLM repair success on common live faults while improving wall-clock cost, mutation count, and safety bounds?

**Contributions.** (1) A complete inspect–plan–execute–replan loop over Compose facts with lexicographic costs prioritizing data risk. (2) An automated bakeoff harness comparing DockRepair, a naive same-catalog baseline, and Codex. (3) Empirical results on seven synthetic but realistic fault classes.

We do **not** claim image-layer DAG recovery, Kubernetes support, or multi-replica repair. The domain is single-replica Compose service-state repair.

## II. System

### A. Facts and goals

DockRepair reads normalized `docker compose config` plus live containers, networks, volumes, mounts, ports, and health. State is a set of string facts such as `running:api`, `healthy:database`, `network_connected:app:default`, and `completed_successfully:migrate`. The goal is the fact set implied by the Compose file (running/healthy/current services, declared ports, optional readiness probes).

### B. Action catalog and safety

Candidate edges are parameterized operators: batch or individual reconcile, start/restart/recreate, create network/volume, connect network, run/rerun completion jobs, and observation edges (`verify_health`, `verify_readiness`, `verify_completion`). A deny-by-default validator admits only cataloged, project-scoped mutations. The catalog contains no volume deletion, foreign-container mutation, foreign-port eviction, or file edit. Missing binds, missing external resources, and foreign port conflicts are hard blockers.

### C. Search and cost

Search is lexicographic uniform-cost search over reachable fact sets. Cost is the vector `(data-risk, destructiveness, disruption, actions)`. Lower data risk always beats fewer actions. These are policy priorities, not latency estimates.

### D. Execute and replan

Execution runs only the first planned edge, recollects live state, and verifies predicted effects. Failed or unverified edges are excluded for that observed state so search can fall back (e.g., restart → recreate). The loop ends when the live goal is observed or the action budget is exhausted.

```
Compose + live Docker
        |
        v
  collect facts ----> UCS search ----> first action
        ^                                   |
        |                                   v
        +--------- re-inspect / exclude ----+
```

Figure 1. DockRepair control loop: observe, plan, mutate once, replan.

## III. Evaluation

### A. Protocol

Each trial: (1) recreate a broken fixture (untimed), (2) run one arm under wall-clock timing, (3) independently verify the live goal, (4) recreate the identical fault for the next arm. Arm order is randomized per repetition. Codex receives only the Compose path and a safety prompt forbidding file edits and foreign mutations; it cannot see fault scripts, scenario labels, or DockRepair's plan. Compose file hashes are checked after the Codex arm.

**Arms.** `planner` (DockRepair), `naive` (same catalog; ignores `depends_on`-style preconditions and never proposes batch reconcile), `codex` (`codex exec --ephemeral`, model `gpt-5.6-terra`).

**Scenarios.** unhealthy; recreate-fallback; flaky-start; stopped-chain; partial-stop; missing-service; config-drift.

**Metrics.** Success (goal facts observed), wall-clock seconds, mutating action count (planner arms), confirmed Compose edits (hash check).

### B. Results

One repetition per scenario (seed 2); Colima Docker Engine on macOS arm64. Full machine-readable summary: `paper/results_summary.json`.

**Table I. Aggregate (n=7 scenarios).**

| Arm | Success | Mean wall-clock (s) | Mean mutating actions | Confirmed Compose edits |
|-----|---------|---------------------|-----------------------|-------------------------|
| planner | 7/7 (100%) | 13.16 | 1.86 | 0 |
| naive | 7/7 (100%) | 12.73 | 2.29 | 0 |
| codex | 7/7 (100%) | 49.11 | — (LLM) | 0 |

**Table II. Wall-clock seconds by scenario.**

| Scenario | planner | naive | codex |
|----------|---------|-------|-------|
| unhealthy | 12.66 | 12.25 | 66.94 |
| recreate-fallback | 56.44 | 53.65 | 73.36 |
| flaky-start | 4.42 | 4.20 | 52.23 |
| stopped-chain | 4.77 | 5.56 | 38.33 |
| partial-stop | 1.19 | 1.00 | 22.91 |
| missing-service | 1.38 | 1.23 | 42.54 |
| config-drift | 11.23 | 11.19 | 47.47 |

**Table III. Mutating actions (planner arms).**

| Scenario | planner | naive |
|----------|---------|-------|
| unhealthy | 2 | 2 |
| recreate-fallback | 3 | 4 |
| flaky-start | 4 | 4 |
| stopped-chain | 1 | 3 |
| partial-stop | 1 | 1 |
| missing-service | 1 | 1 |
| config-drift | 1 | 1 |

### C. Findings

**Latency vs Codex.** DockRepair matched Codex success with mean wall-clock 13.2s vs 49.1s (~3.7×). Every scenario was faster under DockRepair than under Codex.

**Dependency ablation.** On `stopped-chain`, DockRepair used one batch reconcile (1 mutation) while the naive arm needed 3 single-service mutations. On `recreate-fallback`, both arms fell back from restart to recreate; naive spent one extra failed mutation (4 vs 3).

**Safety.** Planner arms cannot edit files. Codex repaired under the no-edit prompt and left Compose hashes unchanged on all seven trials; success therefore did not require file mutation, but Codex remains unbounded outside the prompt.

**Tied success.** These synthetic fixtures do not separate the arms on reliability. The measured advantages are latency, mutation efficiency on dependency-aware cases, and safety-by-construction.

## IV. Related Work

Compose itself provides reconciliation (`up --wait`) but does not search alternative repair paths under a safety cost policy. Classical AI planning (STRIPS/PDDL, UCS/A*) motivates our fact/action encoding. Recent LLM ops / coding agents demonstrate strong repair ability with weak formal bounds; our bakeoff treats Codex as a strong but unbounded baseline under matched task constraints. Self-healing Kubernetes operators address a richer control plane outside this paper's Compose scope.

## V. Limitations

DockRepair supports exactly one container per Compose service. Multi-replica and Swarm/Kubernetes are out of scope. Hard blockers are refused rather than repaired. Costs are policy, not latency. Fixtures are synthetic busybox stacks; external multi-service apps (e.g., Robot Shop) remain future work. We report a single repetition per scenario under a conference deadline; Codex variance across seeds is not characterized. LLM mutation counts from transcripts are unreliable, so Table I omits them for Codex.

## VI. Conclusion

DockRepair shows that a small deny-by-default symbolic planner with dependency-aware UCS and live replan can match a frontier coding agent on common Compose faults while cutting wall-clock time and bounding the mutation surface. Dependency knowledge reduces mutations on multi-service chains relative to a same-catalog naive baseline. Constrained symbolic repair is a practical safety and latency layer beside LLM ops agents.

## Acknowledgments

[BWSI / mentors / compute acknowledgments as appropriate.]

## References

[1] Docker Inc., “Docker Compose specification,” https://docs.docker.com/compose/  
[2] R. E. Fikes and N. J. Nilsson, “STRIPS: A new approach to the application of theorem proving to problem solving,” Artificial Intelligence, 1971.  
[3] OpenAI, “Codex CLI,” https://github.com/openai/codex  
[4] Instana, “Stan's Robot Shop,” https://github.com/instana/robot-shop  
[5] AWS Samples, “Retail Store Sample App,” https://github.com/aws-containers/retail-store-sample-app  
