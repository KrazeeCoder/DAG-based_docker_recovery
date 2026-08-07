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

### A. Research questions

- **RQ1:** Does active probing identify the injected infrastructure failure class?
- **RQ2:** Does diagnosis-guided repair restore supported failures with fewer
  unnecessary mutations than reactive recovery?
- **RQ3:** Does DockRepair abstain without exceeding its mutation budget on
  unsupported failures?
- **RQ4:** Do parameterized operators generalize to held-out names and topology?

### B. Arms and scenarios

The benchmark randomizes four primary arms: (1) `docker compose up --wait`; (2)
reactive restart of a visibly failed service and its declared dependents; (3) the
original state-only DockRepair planner; and (4) full active-diagnosis DockRepair.
An optional fifth companion arm runs the same opaque faults through an `agy` LLM
agent with tool use, reporting tokens, wall-clock, and mutating commands for
cost/latency contrast rather than as a substitute primary baseline.

All scenarios expose the same high-level condition: a declared TCP dependency is
not usable or the caller remains unready. Repairable cases are stopped and missing
targets, caller and target network drift, a recoverable closed listener, a combined
stop-plus-network fault, and a held-out Robot Shop cart-to-Redis failure.
Unsupported cases are missing DNS alias, wrong declared port, persistent closed
listener, working TCP with failed application readiness, and repeated OOM
termination. Fault injection remains outside production action rules.

Each trial recreates an opaque fault, runs one arm, verifies the live contract and
unchanged project files, then recreates the fault for the next arm. The paper
protocol uses ten randomized repetitions per scenario. Ground truth is the injected
fault class; the system sees only the Compose file and live environment.

### C. Metrics

Primary metrics are fault-class accuracy, verified repair rate on supported faults,
correct abstention on unsupported faults, unnecessary restarts/recreations, probe
count, mutation count, recovery time, and safety violations. The LLM companion
additionally reports input/output/total tokens, turns, and mutating shell commands
parsed from agent transcripts. Rules are developed on one synthetic topology and
evaluated on held-out service names/topology and combined faults.

### D. Results

We ran the locked protocol locally on Colima Docker: twelve scenarios, four
symbolic arms, ten repetitions, seed 3, for 480 timed arms. After fixing Compose
DNS alias restoration on network reconnect, we refreshed only the
`dependency-target-network` block (same seed and repetition count) and merged it
into the full result set. Raw trials are in
`benchmarks/results/dependency_bakeoff_results.json`; aggregates are in
`paper/dependency_results_summary.json`. Compose file hashes were unchanged in every
trial (zero safety violations). Unit tests remained green (55/55).

High accuracy on this suite is expected: faults are drawn from a finite,
infrastructure-level hypothesis class that DockRepair enumerates explicitly. The
scientific claim is therefore comparative, not absolute: which mechanisms restore
or correctly refuse under opaque injection. Perfect in-scope accuracy without
baseline contrast would be weak evidence; the contrasts below are the result.

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
