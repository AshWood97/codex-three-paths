# Delegation contract

Every delegated node receives a compact, auditable contract:

- **Objective** — one concrete outcome.
- **Scope** — the files, subsystem, or question in bounds.
- **Context** — only the evidence needed to work.
- **Constraints** — paths, permissions, non-goals, and prohibited changes.
- **Deliverable** — the expected result and handoff format.
- **Acceptance criteria** — commands, tests, or facts that prove completion.

For implementation work, name the checks relevant to the changed code in the
writer's acceptance criteria and require exact commands and results. The root
reviews the integrated diff and reruns checks affected by integration. Apply
the role triggers in [routing](routing.md) for independent verification and
review.

The root owns architecture, decomposition, integration, conflict resolution,
and final verification. A subagent reports an architectural decision, new
dependency, public contract change, security-sensitive choice, overlapping
ownership, or unclear requirement instead of expanding its scope. Subagents do
not delegate to other subagents.

## Public roles

The five routine subagent roles are fixed; the guardian is controller-only:

| Role | Model / effort | Default purpose | Writes |
| --- | --- | --- | --- |
| `explorer` | GPT-6 Luna / max | map repository paths, symbols, tests, and constraints | no |
| `worker` | GPT-6 Luna / max | implement a bounded change | assigned paths only |
| `tester` | GPT-6 Sol / xhigh | reproduce and validate behavior | tests only when requested |
| `reviewer` | GPT-6 Sol / xhigh | independent material review | no |
| `researcher` | GPT-6 Astra / medium | verify current or versioned facts | no |
| `guardian` | GPT-6 Astra / medium | explicitly requested controller gate | no |

The global Root and all named roles have fixed model and effort assignments.
The manifest, controller, installed role files, and global root setting must
agree before new work is dispatched. Existing sessions may retain previously
selected models; record what can be observed and do not claim that a file
change switched a live session. Do not override a role model or effort per
task. A missing role is a dispatch error, not permission to substitute one.

The root owns the plan but is not a plan node. Persistent-runner task nodes may
use the five ordinary roles in the table above; `guardian` is invoked only by
the controller when the user explicitly requests a gate. It is not part of
the normal task path.

## Ownership and write modes

Each writer declares normalized relative `owned_paths`. Two writers may run in
parallel only when their paths do not overlap; a shared interface has one
explicit contract owner. Writers use separate Git worktrees and branches.
The current checkout is not modified by a runner until the root applies a
validated integration result.

Read-only nodes may inspect repository state and produce evidence, but may not
edit files, create commits, change configuration, or apply a run. A Tester in
read-only mode must select commands that do not modify the target state;
commands that generate snapshots, caches, databases, or artifacts in the
target checkout require a writable test environment. Test-file edits require
explicit owned paths, followed by a separate verification pass. Generated
test output belongs in a disposable native Tester environment;
do not route such required verification through the persistent runner. A
`read-only` user override converts every node to that mode and blocks writer
work even if the plan requested it.

## Handoff

Return a concise result containing the summary and decisions, modified paths
and commit SHA when applicable, commands and test evidence, findings and
residual risks, a `requested` model/effort/sandbox configuration, and an
`observed` model/effort/sandbox configuration. If runtime configuration cannot
be observed, record the literal value `unknown`; a requested value is not
proof of actual execution.
