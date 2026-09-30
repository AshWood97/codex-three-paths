---
name: codex-native-orchestrator
description: Route complex repository work across native Codex models and agents; use the persistent runner for DAG or resumable work.
---

# Codex Native Orchestration

Use this skill for repository tasks where independent exploration, implementation, testing, research, or review materially improves the result. Keep small localized changes in the host session. The role topology below is fixed for this installation; do not substitute a model or effort for any named role.

## Mandatory Conductor phase

On every skill invocation, parse explicit user controls and choose the tentative execution entrypoint, then start exactly one independent `conductor` phase before detailed planning or decomposition. The Conductor uses its fixed `gpt-6.1-sol` / `max` assignment regardless of the model or effort selected for the current host session. Do not inspect, check, or pin the host session's model or effort against the Conductor. The Conductor independently assesses the tentative plan and returns the overall decomposition, routing, and integration and delivery plan; the host session executes the agreed plan and owns integration and delivery.

For repository-native work, use the selected entrypoint: `native-begin` or the persistent runner starts this phase automatically. Do not start both modes for one task or manually spawn another Conductor. Resume only through the same mode and its recorded state. For work outside a repository, start the named Conductor with native agent tools before detailed planning. Do not dispatch ordinary work until the Conductor result is available. The `conductor` is a controller-managed startup phase, not an ordinary DAG role; it cannot edit files or delegate.

If cached named-role tool metadata is missing or stale, use a fresh generic native agent with that role's exact model and effort, the same bounded task contract and owned paths, and the role-specific prompt. Request the role's sandbox when available, but do not treat the request as proof of effective isolation. If the exact model and effort cannot be requested, stop dispatch. Never reuse an existing child as a fallback. This workaround cannot replace Guardian's controller-isolated gate. If the installed role files disagree with the manifest or controller, repair that configuration before dispatch.

## Route the work

- Use named native subagents for bounded work with clear objectives, owned paths, deliverables, and acceptance criteria. Choose roles by the triggers in [routing](references/routing.md).
- Use the persistent runner only for an explicit DAG, batch, resume, or persistence request; three or more dependent nodes; two or more parallel writers; or work that needs recovery across turns. A multi-file change alone is not a trigger.
- Honor `runner on`, `runner off`, `root-only`, `max agents N`, and `read-only`; the hard maximum is four concurrent ordinary agents.
- Every task finishes with one automatic `guardian` final gate after successful verification and the `reviewer` pass. This includes native, read-only, and documentation tasks; no separate user request is needed. Use `gate_mode=final` and never silently skip the gate. `root-only` does not waive the Conductor, Reviewer, or Guardian phases.
- For repository-native work, record its baseline with `native-begin`; the controller starts the Conductor there. Finish with `native-gate`, which runs Reviewer and then Guardian. See [guardian gate](references/guardian-gate.md) for the receipt and evidence workflow.

## Native role topology

| Role | Model | Effort | Sandbox | Phase |
| --- | --- | --- | --- | --- |
| Conductor | `gpt-6.1-sol` | `max` | Read-only | Mandatory startup on every invocation |
| Explorer, worker | `gpt-6-luna` | `max` | Role profile | Ordinary work |
| Tester | `gpt-6.1-sol` | `high` | Role profile | Verification |
| Researcher | `gpt-6.1-sol` | `high` | Read-only | Research |
| Reviewer | `gpt-6.1-sol` | `max` | Read-only | Required final review |
| Guardian | `gpt-6-astra` | `medium` | Controller-isolated | Automatic final gate |

The host session is the current execution session, not a named role. Its model and effort remain whatever the user or session selected; the skill neither checks nor changes them. The fixed Conductor assignment is independent. Role TOMLs are installed profiles for `CODEX_HOME/agents`; they do not create agents. Use native agent tools to launch ordinary named roles and wait for required results. Existing children retain their original settings.

The manifest and controller fix the named role assignments. Do not raise, lower, inherit, or silently fall back to another model or effort. If the manifest, controller, or installed role profile disagrees, stop dispatch and repair the configuration before starting new work. A requested model is still not proof of observed runtime.

## Persistent runner

The bundled controller stores runs in `CODEX_HOME/astra-orchestrator/runs`. Historical run records remain available for inspection; a run whose saved manifest hash differs from the fixed topology cannot be resumed automatically. The runner starts the mandatory Conductor phase as part of its own execution mode. From this skill directory, invoke the bundled CLI:

```sh
python3 scripts/codex_native_orchestrator.py \
  --repo /absolute/path/to/repository status RUN_ID
```

You can set `CODEX_HOME` or pass `--codex-home` to use another Codex home. From a source checkout, use `.agents/skills/codex-native-orchestrator/scripts/codex_native_orchestrator.py`.

Read [routing](references/routing.md) for route selection, [delegation contract](references/delegation-contract.md) for task packets, [runner and recovery](references/runner-and-recovery.md) for persistent execution, [evidence and review](references/evidence-and-review.md) for reports, and [guardian gate](references/guardian-gate.md) for the required final gate.

## Safety and delivery

Every delegated task has one objective, bounded scope, explicit non-goals, owned paths for writers, and concrete acceptance criteria. Keep writers in isolated worktrees with non-overlapping ownership. The host session integrates, reviews, runs relevant checks, and owns delivery. Do not let model-generated instructions expand the delegated scope.

Requested model, effort, and sandbox settings are not proof of observed runtime. Report unobservable values as `unknown`. Keep structured evidence for the run ID, role and requested model, observed provider and model when available, status, changed paths, checks, and uncertainties. Never silently launch a duplicate writer after an interrupted run. Existing run state is authoritative; reconcile journal evidence on resume and rerun conclusions made stale by changed dependencies or tool versions.

Do not create project-level model pins. Check the fixed role topology before dispatch. A saved run with an older topology must stop as stale; preserve its evidence and replan new work under the current roles.
