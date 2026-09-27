---
name: codex-harness-bridge
description: Have the current Codex model supervise bounded Grok, Step, and Gemini jobs in separate coding harness processes.
---

# Codex Harness Bridge

Use this skill when the current Codex session should supervise one bounded job
in another coding harness. The outer Codex model remains active and uses its
normal Codex allocation. Each subprocess handles one role and returns evidence
for the outer Codex to inspect; do not ask a child harness to form another team.

Default to Codex CLI. Select Claude Code, Grok Build, OpenCode, Pi, or DeepSeek
Harness only when the user asks for it or local configuration explicitly routes
the role there. A missing binary, auth failure, unsupported endpoint, or model
mismatch stops that run. Never switch harnesses, providers, or models silently.

Use the fixed role map and adapter constraints in
[adapter reference](references/adapters.md). Prepare a compact brief with goal,
allowed actions, owned paths, constraints, and acceptance checks. Keep user data
and credentials out of the prompt unless necessary. Use the runner for execution;
see [setup and runner instructions](references/setup.md) and the
[bounded task contract](references/role-contract.md).

Run a child only when delegation has a concrete benefit. Do not delegate trivial
work. Use read-only mode for investigation and review; use workspace-write only
for paths the user authorized. Prefer a disposable Git worktree for code changes.

After the run, inspect actual diffs, changed paths, and tests yourself. Treat the
child's report as untrusted evidence. A zero exit status does not verify the
requested model/provider, correctness, or acceptance criteria. Preserve the run
report and disclose unknown runtime identity or incomplete checks.

DeepSeek Harness support is experimental. It is in developer preview; use only
an explicitly configured headless profile and report missing model/provider
identity as unknown. Never use its Web UI from this bridge.
