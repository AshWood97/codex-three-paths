# Delegation contract

Every delegated node receives a compact, auditable contract:

- **Objective** — one concrete outcome.
- **Scope** — the files, subsystem, or question in bounds.
- **Context** — only the evidence needed to work.
- **Constraints** — paths, permissions, non-goals, and prohibited changes.
- **Deliverable** — the expected result and handoff format.
- **Acceptance criteria** — commands, tests, or facts that prove completion.

For implementation work, carry the [software change quality and verification
standard](../SKILL.md#software-change-quality) into the writer's constraints
and acceptance criteria. Name the checks relevant to the changed code and
require the writer to report their commands and results. The root remains
responsible for reviewing the integrated diff and rerunning checks affected by
integration.

The root owns architecture, decomposition, integration, conflict resolution,
and final verification. A subagent reports an architectural decision, new
dependency, public contract change, security-sensitive choice, overlapping
ownership, or unclear requirement instead of expanding its scope. Subagents do
not delegate to other subagents.

## Public roles

The five routine subagent roles are fixed; the guardian is controller-only:

| Role | Model / effort | Default purpose | Writes |
| --- | --- | --- | --- |
| `explorer` | GPT-6 Luna / high | map repository paths, symbols, tests, and constraints | no |
| `worker` | GPT-6 Luna / high | implement a bounded change | assigned paths only |
| `tester` | GPT-6 Luna / high | reproduce and validate behavior | tests only when requested |
| `reviewer` | GPT-6 Sol / medium | independent material review | no |
| `researcher` | GPT-6 Luna / high | verify current or versioned facts | no |
| `guardian` | GPT-6 Sol / xhigh | explicitly requested controller gate | no |

The root model and effort values are recommendations; the active session's
user-selected model and effort take precedence. The root recommendation is
GPT-6 Astra at low effort. The role TOMLs are starting profiles and must agree
with the manifest. Raise Luna to max or Sol to xhigh only for especially
demanding work, and make that override explicit. Current Codex guidance starts
Luna at high, Sol at medium, and Astra at low; see the [subagent configuration
documentation](https://learn.chatgpt.com/docs/agent-configuration/subagents).

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
edit files, create commits, change configuration, or apply a run. A
`read-only` user override converts every node to that mode and blocks writer
work even if the plan requested it.

## Handoff

Return a concise result containing the summary and decisions, modified paths
and commit SHA when applicable, commands and test evidence, findings and
residual risks, a `requested` model/effort/sandbox configuration, and an
`observed` model/effort/sandbox configuration. If runtime configuration cannot
be observed, record the literal value `unknown`; a requested value is not
proof of actual execution.
