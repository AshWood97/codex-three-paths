# Delegation contract

Every delegated node receives a compact, auditable contract:

- **Objective** — one concrete outcome.
- **Scope** — the files, subsystem, or question in bounds.
- **Context** — only the evidence needed to work.
- **Constraints** — paths, permissions, non-goals, and prohibited changes.
- **Deliverable** — the expected result and handoff format.
- **Acceptance criteria** — commands, tests, or facts that prove completion.

For implementation work, name the checks relevant to the changed code in the
writer's acceptance criteria and require exact commands and results. The host
session reviews the integrated diff and reruns checks affected by integration.
Apply the role triggers in [routing](routing.md) for independent verification
and review.

The mandatory startup Conductor proposes the architecture, decomposition,
routing, integration, and delivery plan. The host session executes the agreed
plan, resolves conflicts, and owns final integration, verification, and
delivery. A subagent reports an architectural decision, new dependency, public
contract change, security-sensitive choice, overlapping ownership, or unclear
requirement instead of expanding its scope. Subagents do not delegate to other
subagents.

## Public roles

Seven named roles are fixed. The five ordinary roles may be used for bounded
work; the Conductor is a mandatory controller-managed startup phase and
Guardian is the mandatory controller-managed final phase:

| Role | Model / effort | Default purpose | Writes |
| --- | --- | --- | --- |
| `conductor` | GPT-6.1 Sol / max | mandatory overall decomposition, route, integration, and delivery plan | no |
| `explorer` | GPT-6 Luna / max | map repository paths, symbols, tests, and constraints | no |
| `worker` | GPT-6 Luna / max | implement a bounded change | assigned paths only |
| `tester` | GPT-6.1 Sol / high | reproduce and validate behavior | tests only when requested |
| `reviewer` | GPT-6.1 Sol / max | independent material review | no |
| `researcher` | GPT-6.1 Sol / high | verify current or versioned facts | no |
| `guardian` | GPT-6 Astra / medium | automatic final controller gate after Reviewer | no |

The host session uses the model and effort selected by the user or session;
they are independent of the fixed Conductor assignment and must not be checked
or pinned against it. Named roles have fixed model and effort assignments.
The manifest, controller, and installed role files must agree before new work
is dispatched. Record what can be observed and do not claim that a file change
switched a live session. Do not override a named role model or effort per task.
If cached named-role tool metadata is missing or stale, use a fresh generic
native agent with the role's exact model and effort, the same bounded task
contract and owned paths, and the role-specific prompt. Request that role's
sandbox when available; record effective isolation as unknown unless observed.
If the exact model or effort cannot be requested, stop. Never reuse an existing
child as fallback. This workaround cannot replace Guardian's
controller-isolated gate. If the installed role files disagree with the
manifest or controller, repair that configuration before dispatch.

The Conductor is not an ordinary plan node. Persistent-runner task nodes may
use only the five ordinary roles in the table above; the selected controller
entrypoint starts one Conductor phase before detailed planning and invokes one
`guardian` after successful verification and Reviewer review for every task.
Neither is an ordinary DAG node. Choose the execution mode before startup and
use only that mode's record for resume; do not carry a Conductor result across
native and runner modes.

## Ownership and write modes

Each writer declares normalized relative `owned_paths`. Two writers may run in
parallel only when their paths do not overlap; a shared interface has one
explicit contract owner. Writers use separate Git worktrees and branches.
The current checkout is not modified by a runner until the host session
applies a validated integration result.

Read-only nodes may inspect repository state and produce evidence, but may not
edit files, create commits, change configuration, or apply a run. A Tester in
read-only mode must select commands that do not modify the target state;
commands that generate snapshots, caches, databases, or artifacts in the
target checkout require a writable test environment. Test-file edits require
explicit owned paths, followed by a separate verification pass. Generated
test output belongs in a disposable native Tester environment; do not route
such required verification through the persistent runner. A `read-only` user
override converts every node to that mode and blocks writer work even if the
plan requested it.

## Handoff

Return a concise result containing the summary and decisions, modified paths
and commit SHA when applicable, commands and test evidence, findings and
residual risks, a `requested` model/effort/sandbox configuration, and an
`observed` model/effort/sandbox configuration. If runtime configuration cannot
be observed, record the literal value `unknown`; a requested value is not
proof of actual execution.
