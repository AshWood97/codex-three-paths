# Routing

The installed manifest declares the fixed topology, runner defaults, paths,
timeouts, and managed configuration keys. The controller rejects a manifest
whose role model, effort, or sandbox differs from the fixed assignments. Check
the installed role files and global root setting before native dispatch.

## Default route

Use the native named-role path for an ordinary bounded task. Root works alone
on a known, localized, low-risk edit when it can inspect the affected code and
verify the outcome directly. File count alone does not trigger delegation.
Delegate when the user requests agents or the task meets a role trigger below.

| Role | Dispatch trigger | Result required |
| --- | --- | --- |
| `explorer` | Entry point, call path, dependency, affected component, or test location is uncertain | Repository map with exact paths and unresolved questions |
| `researcher` | A decision depends on current or versioned API behavior, upstream source, standards, or compatibility facts outside the repository | Primary source, version assumption, and uncertainty |
| `worker` | Implementation scope, owned paths, and acceptance criteria are clear | Bounded change, changed paths, and checks |
| `tester` | Runtime behavior or a bug fix changes; a dependency, public interface, data format, permission, concurrency path, or multiple writer integration is affected | Independent reproduction or focused verification with commands, results, and gaps |
| `reviewer` | The same material changes need an independent final review, or a public contract, security boundary, data integrity rule, recovery path, or multiwriter integration changes | Findings against the final diff and test evidence |
| `guardian` | User explicitly requests one extra pre or final gate | One isolated, read-only verdict |

The root still reviews every change. Tester verification and Reviewer review
are separate; neither substitutes for the other. Guardian is an extra gate and
never replaces normal testing or review. For a material change, use Tester
after implementation and Reviewer after the final integrated result. If
Reviewer finds a material defect, fix it, rerun affected checks, then refresh
the review. For documentation or formatting changes with no behavior impact,
Root verification is sufficient unless the user requests independent work.
For material persistent-runner changes, plan a Tester node downstream of the
affected writer nodes. Use read-only verification where the commands permit
it. If required tests need writable output, use native dispatch with a
disposable Tester environment, then review the final change and test evidence.
Do not mark a runner Tester node `writes=true` merely for generated test
output: the runner integrates every writer's owned paths into delivery. If
the user explicitly requires the persistent runner for such a plan, stop and
report that the runner cannot bind writable test evidence before its final
review. A final Guardian gate requires successful read-only Tester evidence
downstream of every writer.

For a delegated request, the root must make an actual native
`multi_agent_v1__spawn_agent` call (also known as `spawn_agent` on some
hosts), choose a fixed named role, retain the returned id, and wait for the
required result. A role TOML is only a profile; it is not a spawn. Do not
silently replace a required child with root-thread work. `root-only` and an
explicit user request to stay in the root suppress delegation; the root then
performs the applicable checks and reports that no independent pass occurred.
Do not create parallel work merely because a change touches more than one
file; that rule controls unnecessary parallelism, not the delegation gate.

Use the hybrid persistent runner when at least one of these conditions holds:

1. The user explicitly requests a DAG, batch, resume, persistence, or a
   persistent run.
2. The plan contains at least three dependent nodes.
3. The plan contains at least two writers that can run in parallel.
4. The task needs recovery across separate root turns.

These triggers apply only when all required verification can run in the
runner's read-only Tester nodes. Otherwise use native dispatch, or stop when
the user explicitly requires the runner.

“Multi-file” alone is not a trigger. A runner that is forced off uses the
native path unless a safety rule requires stopping for user input; a runner
that is forced on still validates the plan and safety constraints before
starting work.

## User overrides

Recognize these explicit controls:

- `runner on` or `runner off`: force or suppress persistent-runner routing.
- `root-only`: do not delegate and do not start the runner.
- `max agents N`: request a cap on concurrent child tasks; clamp it to the
  manifest maximum of four and to at least one when execution is enabled.
- `read-only`: make the whole run read-only, including writers and apply
  operations.

The most recent explicit value wins when the same control is repeated.
`read-only` is a capability ceiling: a Tester may run checks only when those
checks do not modify the target state. `root-only` takes precedence over
`runner on`. `runner off` keeps required native verification but serializes
work that would otherwise use parallel writer nodes. A lower agent cap changes
concurrency, not the required verification stages. The root records the route
and overrides in the report.

## Routing order

1. Parse explicit controls and establish the read-only ceiling.
2. Use `gate_mode=none` by default. Set `pre` or `final` only when the user
   explicitly requests a Guardian gate. Choose `pre` for a decision needed
   before implementation and `final` for review of completed evidence.
3. Select roles from the table and order dependencies: exploration or research
   before implementation, Tester after affected behavior, Reviewer after the
   final diff, and an explicitly requested final Guardian last.
4. Decide native versus hybrid routing from the trigger rules and overrides.
5. For delegated work, create bounded contracts and non-overlapping ownership
   before starting writers. Preserve the root's responsibility for integration
   and delivery.

The runner is a coordination mode, not a new role. It must use the five
ordinary roles and the fixed model assignments. The controller-only Guardian
is not scheduled unless the user explicitly requested a gate. No model or
effort substitution is allowed when a role is unavailable; report the issue.
