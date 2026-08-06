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
the worst-case remaining hypotheses, and invokes a deny-by-default repair planner
only after evidence isolates a repairable condition. Repairs are limited to
starting or reconciling a service, restoring a declared network attachment,
restarting a proven closed listener once, and an explicitly opted-in recreation.
The system verifies the original caller-to-target contract after every repair and
otherwise returns a structured abstention report. We evaluate DockRepair against
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
Verified, project-scoped minimal repairs with bounded fallback and opt-in
recreation. (4) A four-arm opaque-fault benchmark that measures diagnosis,
restoration, collateral mutation, and correct abstention.

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

## IV. Evaluation

### A. Research questions

- **RQ1:** Does active probing identify the injected infrastructure failure class?
- **RQ2:** Does diagnosis-guided repair restore supported failures with fewer
  unnecessary mutations than reactive recovery?
- **RQ3:** Does DockRepair abstain without exceeding its mutation budget on
  unsupported failures?
- **RQ4:** Do parameterized operators generalize to held-out names and topology?

### B. Arms and scenarios

The benchmark randomizes four arms: (1) `docker compose up --wait`; (2) reactive
restart of a visibly failed service and its declared dependents; (3) the original
state-only DockRepair planner; and (4) full active-diagnosis DockRepair.

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
count, mutation count, recovery time, and safety violations. Rules are developed on
one synthetic topology and evaluated on held-out service names/topology and combined
faults.

### D. Results status

The implementation and benchmark protocol are complete, but the final
ten-repetition study must be run before submission. Do not replace this paragraph
with aggregate claims until `benchmarks/results/dependency_bakeoff_results.json`
contains the full experiment. A one-repetition implementation smoke test on the
closed-listener scenario produced the intended qualitative separation: active
DockRepair localized the closed listener and restored the contract with one target
restart, while Compose up, reactive restart, and the state-only planner did not
observe the running-container failure. This smoke test is not statistically
meaningful and is not a paper result.

The previous DockRepair prototype's lifecycle-focused pilot (`paper/results_summary.json`)
covered eight faults with one repetition each. Those measurements are preliminary
engineering evidence only and are not used to answer the new research questions.

## V. Threats and Limitations

Explicit contracts add configuration burden and are not a complete runtime call
graph. A TCP success proves reachability, not correct application behavior. A
target-local probe can establish that no process accepts the declared port but
cannot determine whether code, configuration, or workload caused that condition.
Docker Desktop network behavior may differ from native Linux. The diagnostic image
must already be present. The benchmark's finite injected classes cannot establish
coverage of arbitrary Docker failures.

The planner encodes parameterized Docker mechanics and finite hypotheses. It does
not encode a rule for each fixture, but held-out evaluation is still necessary to
detect benchmark overfitting. Finally, repeated trials on one host measure internal
repeatability rather than production prevalence.

## VI. Conclusion

DockRepair is positioned as a narrow, verifiable recovery layer for declared
Docker Compose dependencies. Its useful distinction is not automatic restart, but
active differentiation before mutation, minimal project-scoped repair, end-to-end
verification, and explicit abstention at the application-semantic boundary.

## References

[1] Docker Inc., "Control startup and shutdown order in Compose," https://docs.docker.com/compose/how-tos/startup-order/

[2] Docker Compose, "Healthchecks dependent on health status of depends_on," issue 11582, https://github.com/docker/compose/issues/11582

[3] W. Farrell, "docker-autoheal," https://github.com/willfarrell/docker-autoheal

[4] krystall0, "Docker Surgeon," https://github.com/krystall0/docker-surgeon

[5] V. Bob, "wait-for-it," https://github.com/vishnubob/wait-for-it

[6] R. E. Fikes and N. J. Nilsson, "STRIPS: A new approach to the application of theorem proving to problem solving," Artificial Intelligence, 1971.
