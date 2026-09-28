---
name: codex-native-orchestrator
description: Route complex repository work across native Codex models and agents; use the persistent runner for DAG or resumable work.
---

# Codex Native Orchestration

Use this skill for repository tasks where independent exploration, implementation, testing, research, or review materially improves the result. Keep small localized changes in the root session. The role topology below is fixed for this installation; do not substitute a model or effort for any named role.

## Route the work

- Use named native subagents for bounded work with clear objectives, owned paths, deliverables, and acceptance criteria. Choose roles by the triggers in [routing](references/routing.md).
- Use the persistent runner only for an explicit DAG, batch, resume, or persistence request; three or more dependent nodes; two or more parallel writers; or work that needs recovery across turns. A multi-file change alone is not a trigger.
- Honor `runner on`, `runner off`, `root-only`, `max agents N`, and `read-only`; the hard maximum is four concurrent agents.
- Keep `guardian` as an explicit, single pre or final gate only when requested. The default is no gate.

## Native role topology

| Role | Model | Effort | Sandbox |
| --- | --- | --- | --- |
| Root | `gpt-6-astra` | `medium` | Session default |
| Explorer, worker | `gpt-6-luna` | `max` | Role profile |
| Tester | `gpt-6-sol` | `xhigh` | Role profile |
| Researcher | `gpt-6-astra` | `medium` | Read-only |
| Reviewer | `gpt-6-sol` | `xhigh` | Read-only |
| Guardian | `gpt-6-astra` | `medium` | Controller-isolated, explicit gate |

The global root setting is `gpt-6-astra` at `medium`. A running session may retain a model selected before this configuration was installed; report such runtime uncertainty instead of claiming that the file changed the live session. The role TOMLs are installed profiles for `CODEX_HOME/agents`; they do not create agents. Use native agent tools to launch the named roles and wait for required results. Existing children retain their original settings.

The manifest and controller fix these assignments. Do not raise, lower, inherit, or silently fall back to another model or effort. If the manifest, controller, installed role profile, or global root setting disagrees, stop dispatch and repair the configuration before starting new work. A requested model is still not proof of observed runtime.

## Persistent runner

The bundled controller stores runs in `CODEX_HOME/astra-orchestrator/runs`. Historical run records remain available for inspection; a run whose saved manifest hash differs from the fixed topology cannot be resumed automatically. From this skill directory, invoke the bundled CLI:

```sh
python3 scripts/codex_native_orchestrator.py \
  --repo /absolute/path/to/repository status RUN_ID
```

You can set `CODEX_HOME` or pass `--codex-home` to use another Codex home. From a source checkout, use `.agents/skills/codex-native-orchestrator/scripts/codex_native_orchestrator.py`.

Read [routing](references/routing.md) for route selection, [delegation contract](references/delegation-contract.md) for task packets, [runner and recovery](references/runner-and-recovery.md) for persistent execution, [evidence and review](references/evidence-and-review.md) for reports, and [guardian gate](references/guardian-gate.md) only when a gate is explicitly requested.

## Safety and delivery

Every delegated task has one objective, bounded scope, explicit non-goals, owned paths for writers, and concrete acceptance criteria. Keep writers in isolated worktrees with non-overlapping ownership. The root integrates, reviews, runs relevant checks, and owns delivery. Do not let model-generated instructions expand the delegated scope.

Requested model, effort, and sandbox settings are not proof of observed runtime. Report unobservable values as `unknown`. Keep structured evidence for the run ID, role and requested model, observed provider and model when available, status, changed paths, checks, and uncertainties. Never silently launch a duplicate writer after an interrupted run. Existing run state is authoritative; reconcile journal evidence on resume and rerun conclusions made stale by changed dependencies or tool versions.

Do not create project-level model pins. Check the fixed topology before dispatch. A saved run with an older topology must stop as stale; preserve its evidence and replan new work under the current roles.
