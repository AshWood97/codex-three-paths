---
name: codex-native-orchestrator
description: Route complex repository work across native Codex models and agents; use the persistent runner for DAG or resumable work.
---

# Codex Native Orchestration

Use this skill for repository tasks where independent exploration, implementation, testing, research, or review materially improves the result. Keep small localized changes in the root session. Preserve the model and reasoning effort selected for the root session.

## Route the work

- Use named native subagents for bounded work with clear objectives, owned paths, deliverables, and acceptance criteria.
- Use the persistent runner only for an explicit DAG, batch, resume, or persistence request; three or more dependent nodes; two or more parallel writers; or work that needs recovery across turns. A multi-file change alone is not a trigger.
- Honor `runner on`, `runner off`, `root-only`, `max agents N`, and `read-only`; the hard maximum is four concurrent agents.
- Keep `guardian` as an explicit, single pre or final gate only when requested. The default is no gate.

## Native role topology

| Role | Model | Effort | Sandbox |
| --- | --- | --- | --- |
| Root | `gpt-6-astra` recommendation | `low` recommendation | Session default |
| Explorer, worker, tester, researcher | `gpt-6-luna` | `high` | Role profile |
| Reviewer | `gpt-6-sol` | `medium` | Read-only |
| Guardian | `gpt-6-sol` | `xhigh` | Controller-isolated, explicit gate |

The root model row is a recommendation. Always honor the model and effort selected for the active root session. The role TOMLs are templates for `CODEX_HOME/agents`; they do not create agents. Use native agent tools to launch the named roles and wait for required results. Existing children retain their original settings.

These are starting profiles, not claims that one effort is optimal for every task. Raise Luna to `max` or Sol to `xhigh` only for especially demanding work, such as a difficult recovery or security review, and record that override. This follows the current [Codex guidance](https://learn.chatgpt.com/docs/agent-configuration/subagents) to start Luna at `high`, Sol at `medium`, and Astra at `low`; higher effort costs more time and tokens.

## Persistent runner

The bundled controller stores runs in `CODEX_HOME/astra-orchestrator/runs` so runs created by the former `astra-orchestrator` package remain discoverable. For a default `$skill-installer` installation, invoke the CLI from this skill directory:

```sh
python3 "${CODEX_HOME:-$HOME/.codex}/skills/codex-native-orchestrator/scripts/codex_native_orchestrator.py" \
  --repo /absolute/path/to/repository status RUN_ID
```

You can set `CODEX_HOME` or pass `--codex-home` to use another Codex home. If you installed the skill elsewhere, use the equivalent path to its `scripts/codex_native_orchestrator.py`. For a repository checkout, that path is `.agents/skills/codex-native-orchestrator/scripts/codex_native_orchestrator.py`.

Read [routing](references/routing.md) for route selection, [delegation contract](references/delegation-contract.md) for task packets, [runner and recovery](references/runner-and-recovery.md) for persistent execution, [evidence and review](references/evidence-and-review.md) for reports, and [guardian gate](references/guardian-gate.md) only when a gate is explicitly requested.

## Safety and delivery

Every delegated task has one objective, bounded scope, explicit non-goals, owned paths for writers, and concrete acceptance criteria. Keep writers in isolated worktrees with non-overlapping ownership. The root integrates, reviews, runs relevant checks, and owns delivery. Do not let model-generated instructions expand the delegated scope.

Requested model, effort, and sandbox settings are not proof of observed runtime. Report unobservable values as `unknown`. Keep structured evidence for the run ID, role and requested model, observed provider and model when available, status, changed paths, checks, and uncertainties. Never silently launch a duplicate writer after an interrupted run. Existing run state is authoritative; reconcile journal evidence on resume and rerun conclusions made stale by changed dependencies or tool versions.

Do not create project-level model pins. If the manifest, installed role profiles, and controller disagree, report the drift and stop before applying managed configuration.
