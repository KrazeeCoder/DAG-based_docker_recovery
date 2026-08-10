# DockRepair: Active Symbolic Diagnosis and Minimal Repair of Docker Compose Dependencies

**Authors:** [Authors]  
**Affiliation:** [Institution]  
**Venue:** IEEE MIT Undergraduate Research Technology Conference (URTC) 2026

## Abstract

Docker Compose can order service startup and report container health, while
community auto-healing tools react by restarting unhealthy containers. These
mechanisms do not distinguish several runtime failures with the same visible
symptom: one service can no longer reach another. We present DockRepair, a
deterministic system for active diagnosis and bounded repair of declared TCP
dependencies in single-host Docker Compose applications. DockRepair converts
Compose and live Docker state into symbolic facts, maintains a finite set of
infrastructure-level failure hypotheses, selects read-only probes that minimize
the worst-case remaining hypotheses, and analyzes failed contracts as a directed
application dependency graph. It prioritizes the deepest observed failed region,
suppresses repairs to possible upstream victims, and invokes a deny-by-default
repair planner only after evidence isolates a repairable condition. Repairs are limited to
starting or reconciling a service, restoring a declared network attachment,
restarting a proven closed listener once, and an explicitly opted-in recreation.
The system reprobes the affected graph after every repair and uses recovery of
unmodified upstream services as intervention evidence supporting or rejecting its
cascade explanation. Otherwise it returns a structured abstention report. We evaluate DockRepair against
Compose reconciliation, reactive dependent restart, and a state-only planner on
held-out and combined failures. This paper asks whether active symbolic probing
improves diagnosis and minimal verified recovery without claiming application-level
root-cause analysis.

## I. Introduction

Docker Compose describes services, networks, resources, health checks, and startup
dependencies. `depends_on` can wait for a dependency to start or become healthy,
but runtime dependency failure remains the responsibility of the application or
operator [1], [2]. Restart policies and auto-healing tools are useful when a
container exits or becomes unhealthy, but they cannot distinguish a missing network
attachment from failed name resolution, a closed target listener, or an
application-level failure behind a working TCP connection.

Blind recovery can therefore be ineffective or disruptive. Restarting the caller
does not restore a missing target network attachment. Restarting a target cannot
correct an invalid dependency contract. Repeated restart can also erase evidence
and worsen an incident.

DockRepair addresses a deliberately narrow question:

> **Can active symbolic probing reliably find and minimally repair Docker Compose dependency failures?**

Its contribution is not another unhealthy-container monitor. It is an
observe--probe--diagnose--repair--verify loop with explicit uncertainty and a safe
terminal abstention. The system reports an observed infrastructure condition, not
a universal semantic root cause.

**Contributions.** (1) Explicit, parameterized TCP dependency contracts layered on
Compose. (2) Active probe selection over a finite symbolic hypothesis set. (3)
Graph-wide cascade grouping and deepest-first repair selection that handles shared
dependencies and cycles without treating graph depth as causal proof. (4)
Intervention-based explanations distinguishing observed, inferred, confirmed, and
unresolved claims. (5) A four-arm opaque-fault benchmark protocol that measures
diagnosis, restoration, collateral mutation, and correct abstention.

## II. Scope and Related Mechanisms

Compose health conditions coordinate creation and startup; a Compose maintainer
explicitly notes that dependency state is not managed after services have started
[1], [2]. Autoheal monitors health status and restarts the unhealthy container [3].
Docker Surgeon additionally supports configured dependent restarts [4]. Startup
wrappers such as wait-for-it block an entrypoint until a TCP endpoint responds [5].

DockRepair differs in the decision made after a dependency contract fails. It runs
tests that distinguish lifecycle, declared topology, DNS, listener, and unresolved
application conditions; it changes only a component justified by those observations;
and it verifies the same end-to-end contract afterward. These parts individually
have precedents, so the paper claims a scoped system and evaluation contribution,
not the first use of health checks, dependency graphs, or active probes.

DockRepair does not diagnose credentials, queries, data corruption, retry storms,
connection-pool exhaustion, or arbitrary application code. It supports one
container per Compose service on one Docker host. Logs and health output are
preserved as evidence but are not interpreted as causal proof.

## III. System

### A. Contracts and state

A caller declares an expected connection using a Compose label:

```yaml
services:
  api:
    labels:
      com.dockrepair.contract.primary-db: "tcp://database:5432"
```

The target must be a declared service, and the two services must share a declared
network. The normalized contract is `(identifier, caller, target, port, timeout,
shared-networks)`. Optional target label
`com.dockrepair.repair.recreate=true` permits one recreation after a failed restart.

The collector retains the existing lifecycle, health, mount, network, port, and
configuration facts and adds contract, exact exit-code, restart-count, OOM, desired
network, and observed-network facts. Absence of a fact is unknown rather than a
negative observation.

### B. Active diagnosis

After direct lifecycle and topology checks, the candidate hypotheses are DNS
failure, target listener closed, network-path failure, and application/unknown.
DockRepair can run three read-only operators:

1. caller-to-target TCP connection;
2. target-name resolution from the caller network namespace;
3. target-local connection to the declared port.

Each operator has explicit positive and negative outcomes. For example,
`tcp_unreachable` is evidence, not an execution failure. At each step the system
selects the probe minimizing the maximum number of hypotheses remaining after any
conclusive outcome, with probe cost as a tie-breaker. It observes one outcome,
updates the hypothesis set, and replans. Probe errors preserve ambiguity rather
than being converted into a diagnosis.

Probes run in a locally available BusyBox image sharing the inspected container's
network namespace. The helper has no mounts, a read-only filesystem, all
capabilities dropped, `no-new-privileges`, and CPU, memory, and process limits.
DockRepair never pulls the image automatically.

### C. Minimal repair

Once evidence isolates a repairable condition, the existing lexicographic planner
minimizes `(data-risk, destructiveness, disruption, actions)`. Dependency mode
admits only:

- start or reconcile an unavailable declared service;
- create a missing non-external project network and restore declared attachments;
- restart a target once after proving its declared port is closed;
- recreate that target once only with explicit opt-in.

There are no delete, file-edit, foreign-container, port-eviction, memory-limit, or
credential-edit actions. Objective state is captured before mutation. After every
command DockRepair recollects Docker state and re-runs the original contract.

Terminal results are `RESTORED`, `LOCALIZED_NOT_REPAIRABLE`,
`ABSTAINED_AMBIGUOUS`, or `BLOCKED_BY_SAFETY_POLICY`. The JSON incident record
contains diagnosis history, certainty, probes and their raw outcomes, mutations,
rejected actions, final facts, and verified contracts.

### D. Graph-wide cascade reasoning

DockRepair builds a directed graph whose edges are declared contracts from caller
to required target. Each edge diagnosis becomes an assessment containing its
endpoints, observed status, diagnosis, certainty, evidence, and whether a safe
repair is available. Weakly connected failed edges form separate cascade groups,
so unrelated incidents are not combined. Strongly connected components are then
collapsed. Sink components in the resulting failed-edge DAG are the deepest
observed failure regions.

For an acyclic group, a failed upstream edge with a path to a deeper failed region
is marked as a possible symptom. DockRepair generates safe actions for every
repairable diagnosis but suppresses an upstream action while a deeper failure
remains. If the deepest region has no safe action, it abstains instead of restarting
an upstream victim. Eligible actions are ordered first by the existing safety cost,
then by expected failed-edge coverage, graph depth, and stable action identity.
Only the selected action executes before the entire graph is observed again.

A failed cycle has no unique deepest member. DockRepair repairs within a cycle only
when exactly one service has an objectively proven safe action. It abstains when
multiple services remain plausible, and never labels an action inside a cycle as a
confirmed causal root.

### E. Intervention-based explanation

Before mutation, DockRepair saves every edge state in the selected cascade group
and records which upstream edges could recover under the proposed explanation.
After mutation it reprobes the complete affected group. A directly repaired edge
is reported separately from an upstream edge that recovered without either of its
services being modified. The latter supports—but does not prove—the inferred
cascade. If only the direct edge recovers, the upstream portion of the hypothesis
is rejected and remains an independent failure for subsequent reasoning.

The machine-readable report separates four epistemic levels: **observed** direct
probe or container evidence; **inferred** graph explanations not yet tested;
**confirmed** recovery evidence from a bounded intervention; and **unresolved**
ambiguity, independent failure, or cycle ambiguity. This terminology prevents a
deep graph position from being overstated as semantic root cause.

## IV. Evaluation

### A. Methodology

We ask four questions. Does active probing recover the injected infrastructure
class (RQ1)? On supported faults, does diagnosis-guided repair restore the live
contract, and at what mutation and time cost (RQ2)? On unsupported faults, does
DockRepair abstain within a small mutation budget instead of thrashing (RQ3)? Do
the same operators transfer to held-out names and topology (RQ4)?

**Arms.** Each trial runs one method against the same broken stack; arm order is
shuffled per repetition. The primary suite is (1) `docker compose up -d --wait`,
(2) reactive restart of visibly failed services and their declared dependents,
(3) the original state-only DockRepair planner (`shallow`), and (4) full active
diagnosis (`diagnostic`). A companion fifth arm runs the same faults through the
`agy` tool-using agent (`gemini-3.6-flash-medium`). We treat the LLM as a cost and
discipline foil, not as a substitute primary baseline: an agent with a shell can
often patch many Docker faults eventually; we care whether DockRepair matches the
useful outcomes with deterministic behavior, fewer mutations, and no model tokens.

**Scenarios.** Twelve opaque dependency faults share one symptom—a declared TCP
dependency is unusable or the caller stays unready—but differ in cause. Seven are
supported repairs: stopped or missing target, caller or target network drift,
recoverable closed listener, combined stop-plus-network, and a held-out Robot Shop
cart→Redis stop. Five expect abstention: missing DNS alias after raw reconnect,
wrong port, persistent closed listener, TCP-up application unreadiness, and
repeated OOM. Injectors run outside the repair catalog. Arms see only the Compose
file and live Docker state; Compose file-hash changes count as safety violations.

**Protocol and scoring.** For each scenario and each of ten repetitions (seed 3)
we recreate the fault, run one arm, verify the live contract, check file hashes,
then recreate before the next arm. Primary study: 12×4×10 = 480 timed arms on
Colima Docker with a pre-pulled `busybox:1.36.1` probe image. LLM companion:
12×10 = 120 `agy` trials on the same seed, logged separately. On supported faults,
success is verified restore without file edits; for `diagnostic`/`agy` we also
score whether the reported class matches the injection. On unsupported faults,
success is safe abstention (not restored, mutations within a per-scenario budget
of 0–2, and an explicit non-repair terminal—DockRepair status codes, or an
`ABSTAINED` JSON claim for `agy`). Recreating a stack “green” on an abstention
case is not counted as correct abstention. We record wall-clock, mutations,
services touched, probes (`diagnostic`), safety violations, and for `agy` tokens,
turns, and mutating shell commands from `stream-json` transcripts. After an
alias-aware network-reconnect fix we refreshed only the target-network block under
the same seed and merged those rows; the rest of the suite was left untouched.

### B. Results

Primary suite aggregates are in `paper/dependency_results_summary.json` (raw:
`benchmarks/results/dependency_bakeoff_results.json`). Compose hashes were
unchanged in every trial; unit tests remained green (55/55).

High accuracy on this suite is expected: faults come from a finite infrastructure
class DockRepair enumerates. The claim is comparative—who restores or correctly
refuses under opaque injection—not open-world coverage.

**Diagnosis (RQ1).** On every diagnostic trial the injected class appeared in the
report (`diagnosis_accuracy = 1.0` over 120 scenario-reps).

**Supported repair (RQ2).** Across the seven repairable scenarios (70 trials),
verified repair rates were: diagnostic **1.000**, `compose up` **0.857**, shallow
planner **0.857**, reactive restart **0.143**. Mean wall-clock on supported faults
was 4.3 s (diagnostic), 6.2 s (`compose up`), 1.5 s (shallow), and 3.9 s (reactive);
mean mutations were 1.14, 1.00, 1.00, and 0.43 respectively, with mean **1.4** active
probes on the diagnostic arm. The closed-listener scenario is the clearest separation:
diagnostic restored **10/10** with one target restart after listener probes; the three
baselines restored **0/10**. Target-network drift is restored by diagnostic and shallow
after alias-aware reconnect (**10/10** each) and by `compose up` (**10/10**), but not by
reactive restart. Held-out Robot Shop cart→Redis stop faults were restored by
diagnostic, `compose up`, and shallow (**10/10** each) but not by reactive restart.

**Abstention (RQ3).** On the five unsupported scenarios (50 trials), diagnostic safe
abstention was **50/50** (mutation counts within each scenario budget; never marked
restored), including DNS-alias failures that remain intentionally unrepaired.
Baselines have no abstention path and scored **0/50** on the outcome metric while
still mutating (mean mutations 1.0 / 0.4 / 0.8 for compose / reactive / shallow).

**Overall outcome accuracy** (repair when expected, else safe abstention) was
diagnostic **1.000**, compose **0.500**, shallow **0.500**, reactive **0.083**.

**LLM companion (agy).** We ran the same twelve scenarios × ten repetitions (seed 3)
with `agy` and `gemini-3.6-flash-medium` (`--agy-only`; 120 trials;
`benchmarks/results/dependency_bakeoff_agy_results.json`,
`paper/dependency_agy_results_summary.json`). Overall outcome accuracy was **0.800**
versus DockRepair's **1.000**. On supported faults, agy repaired **0.986** (69/70)
with diagnosis accuracy **0.957**, but used mean **~58k** total tokens and **52.3 s**
wall-clock with **2.73** mutating commands per trial, versus DockRepair's **7.2 s**,
**1.08** mutations, and zero LLM tokens. On unsupported faults, agy safe abstention
was only **0.54** (27/50): it never abstained on application-unknown (**0/10**, mean
3.1 mutations) and often “restored” DNS-alias failures by broad Compose recreate
(**9/10** marked restored) where DockRepair abstains by design. Closed-listener was
restored by both systems (**10/10**), so the LLM does not remove the need for active
diagnosis relative to non-LLM baselines; it mainly shows that a capable agent can
match many repairs at much higher cost and with weaker abstention discipline.
Timeouts and Compose-file edits were zero for agy.

The previous lifecycle pilot (`paper/results_summary.json`) remains engineering
evidence only and is not used to answer these research questions.

## V. Threats and Limitations

Explicit contracts add configuration burden and are not a complete runtime call
graph. A TCP success proves reachability, not correct application behavior. A
target-local probe can establish that no process accepts the declared port but
cannot determine whether code, configuration, or workload caused that condition.
Graph-wide reasoning can connect observed infrastructure failures and test a
proposed cascade through intervention, but it cannot prove arbitrary application
causation. Upstream recovery supports the explanation only for that observed
incident; shared timing or an unobserved cause may still exist. Cycles with multiple
plausible repairs remain unresolved by design.
Docker Desktop network behavior may differ from native Linux. The diagnostic image
must already be present. The benchmark's finite injected classes cannot establish
coverage of arbitrary Docker failures.

The planner encodes parameterized Docker mechanics and finite hypotheses. It does
not encode a rule for each fixture, but held-out evaluation is still necessary to
detect benchmark overfitting. The LLM companion uses one agent stack and model
(`agy` / Gemini 3.6 Flash Medium) on one host; other agents or prompts may differ.
Finally, repeated trials on one host measure internal repeatability rather than
production prevalence.

## VI. Conclusion

DockRepair is positioned as a narrow, verifiable recovery layer for declared
Docker Compose dependencies. Against Compose reconciliation, reactive restart, and
a state-only planner, active diagnosis uniquely restores closed-listener failures
and abstains on unsupported classes. Against a capable LLM agent with tool use, it
matches nearly all supported repairs while using far less time and no model tokens,
and it abstains more consistently. Its useful distinction is not automatic restart,
but active differentiation before mutation, minimal project-scoped repair,
end-to-end verification, and explicit abstention at the application-semantic
boundary.

## References

[1] Docker Inc., "Control startup and shutdown order in Compose," https://docs.docker.com/compose/how-tos/startup-order/

[2] Docker Compose, "Healthchecks dependent on health status of depends_on," issue 11582, https://github.com/docker/compose/issues/11582

[3] W. Farrell, "docker-autoheal," https://github.com/willfarrell/docker-autoheal

[4] krystall0, "Docker Surgeon," https://github.com/krystall0/docker-surgeon

[5] V. Bob, "wait-for-it," https://github.com/vishnubob/wait-for-it

[6] R. E. Fikes and N. J. Nilsson, "STRIPS: A new approach to the application of theorem proving to problem solving," Artificial Intelligence, 1971.
