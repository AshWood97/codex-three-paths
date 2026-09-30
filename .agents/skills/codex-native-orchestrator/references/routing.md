# Routing

## Mandatory startup

Every skill invocation starts exactly one independent Conductor phase before
detailed planning or decomposition. First parse explicit user controls and
choose the tentative execution entrypoint; the selected controller entrypoint
starts Conductor and passes it the host's tentative task plan. The Conductor is
fixed at `gpt-6.1-sol` / `max`, independent of the host session's selected
model and effort; never check or pin the host session against that assignment.
It independently assesses the tentative plan and returns the overall
decomposition, routing, and integration and delivery plan. The host session
executes the agreed plan and owns integration and delivery.

For repository-native work, use the selected entrypoint: `native-begin` for
native execution or the persistent runner for runner execution. Each starts
Conductor automatically. Do not start both modes for one task, manually spawn
a second Conductor, or carry a startup record across modes. Resume only through
the same mode and its saved state. If startup cannot produce a ready plan, do
not start work. For work outside a repository, launch the named Conductor with
native agent tools before detailed planning. The Conductor does not edit files
or delegate; it is a controller-managed phase rather than an ordinary work
node.

If cached named-role tool metadata is missing or stale, use a fresh generic
native agent with the role's exact model and effort, the same bounded task
contract and owned paths, and the role-specific prompt. Request that role's
sandbox when available, but treat effective isolation as unknown unless
observed. If the exact model or effort cannot be requested, stop dispatch.
Never reuse an existing child as a fallback. This workaround cannot replace
Guardian's controller-isolated gate. If the installed role files disagree
with the manifest or controller, repair that configuration before dispatch.

## Default route

After the Conductor returns its plan, follow its work decomposition within the
selected execution mode. Use the native named-role path for ordinary bounded
tasks. The host session may handle a known, localized,
low-risk edit when it can inspect the affected code and verify the outcome
directly. File count alone does not trigger delegation. Delegate when the user
requests agents or the task meets a role trigger below.

| Role | Dispatch trigger | Result required |
| --- | --- | --- |
| `conductor` | Every skill invocation, before overall planning | Overall decomposition, route, integration, and delivery plan |
| `explorer` | Entry point, call path, dependency, affected component, or test location is uncertain | Repository map with exact paths and unresolved questions |
| `researcher` | A decision depends on current or versioned API behavior, upstream source, standards, or compatibility facts outside the repository | Primary source, version assumption, and uncertainty |
| `worker` | Implementation scope, owned paths, and acceptance criteria are clear | Bounded change, changed paths, and checks |
| `tester` | Runtime behavior or a bug fix changes; a dependency, public interface, data format, permission, concurrency path, or multiple writer integration is affected | Independent reproduction or focused verification with commands, results, and gaps |
| `reviewer` | Every completed task, after appropriate verification | Findings against the final diff and test evidence |
| `guardian` | Every task, after successful verification and Reviewer review | One isolated, read-only final verdict |

The host session reviews every change. Tester verification and Reviewer review
are separate; neither substitutes for the other. Guardian is an additional
final gate and never replaces normal testing or review. For a material change,
use Tester after implementation and Reviewer after the final integrated
result. If Reviewer finds a material defect, fix it, rerun affected checks,
then refresh the review. For documentation or formatting changes with no
behavior impact, host-session verification can supply the relevant check
evidence; the final Reviewer and Guardian passes are still required.

For material persistent-runner changes, plan a Tester node downstream of the
affected writer nodes. Use read-only verification where the commands permit
it. If required tests need writable output, use native dispatch with a
disposable Tester environment, then review the final change and test evidence.
Do not mark a runner Tester node `writes=true` merely for generated test
output: the runner integrates every writer's owned paths into delivery. If
the user explicitly requires the runner for such a plan, stop and report that
the runner cannot bind writable test evidence before its final review. A final
Guardian gate requires successful read-only Tester evidence downstream of
every writer.

For ordinary delegated work, make an actual native
`multi_agent_v1__spawn_agent` call (also known as `spawn_agent` on some hosts),
choose a fixed named role, retain the returned id, and wait for the required
result. A role TOML is only a profile; it is not a spawn. `root-only` suppresses
ordinary implementation delegation and the runner; it does not waive the
mandatory Conductor, Reviewer, or Guardian phases. An explicit user waiver may
suppress Reviewer or Guardian and must be recorded as a waiver, never as an
approval. Do not create parallel work merely because a change touches more
than one file; that rule controls unnecessary parallelism, not the review and
gate requirements.

Use the hybrid persistent runner when at least one of these conditions holds:

1. The user explicitly requests a DAG, batch, resume, persistence, or a
   persistent run.
2. The plan contains at least three dependent nodes.
3. The plan contains at least two writers that can run in parallel.
4. The task needs recovery across separate host-session turns.

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
- `root-only`: keep ordinary work in the host session and do not start the
  runner; this does not waive the Conductor, Reviewer, or Guardian phases.
- `max agents N`: request a cap on concurrent ordinary agent tasks; clamp it
  to the manifest maximum of four and to at least one when execution is
  enabled.
- `read-only`: make the whole run read-only, including writers and apply
  operations.

The most recent explicit value wins when the same control is repeated.
`read-only` is a capability ceiling: a Tester may run checks only when those
checks do not modify the target state. `root-only` takes precedence over
`runner on`. `runner off` keeps required native verification but serializes
work that would otherwise use parallel writer nodes. A lower agent cap changes
concurrency, not required verification stages. The host session records the
route and overrides in the report.

## Routing order

1. Parse the user's explicit controls, choose a tentative execution mode, and
   start its mandatory Conductor phase exactly once before detailed planning.
2. Apply the controls and Conductor's decomposition and route recommendation
   within the selected mode; use `gate_mode=final` for every task. No additional
   user request is needed, and `pre` or `none` is invalid for new plans.
3. Select among the five ordinary roles from the table and order dependencies:
   exploration or research before implementation, appropriate verification
   after work, Reviewer after the final result, and automatic Guardian last.
   Run one Guardian per completed task, not one per DAG node.
4. Match the detailed dependencies and verification requirements to the
   selected mode; if it cannot support them, stop before dispatch.
5. For delegated work, create bounded contracts and non-overlapping ownership
   before starting writers. Preserve the host session's responsibility for
   executing the agreed plan, integration, and delivery.

The runner is a coordination mode, not a new role. Its task nodes use only the
five ordinary roles and their fixed model assignments. The selected controller
entrypoint starts Conductor before detailed planning and invokes the
controller-only Guardian automatically after Reviewer. Startup records are
resumed only in the same execution mode.
