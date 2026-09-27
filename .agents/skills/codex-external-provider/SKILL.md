---
name: codex-external-provider
description: Orchestrate Grok, Step, and Gemini in a Codex session already routed to a verified external Responses API provider.
---

# Codex External Provider

Use this skill only when the user explicitly wants third-party API inference from the Codex session itself. The root session and every native subagent must use the same user-configured external Codex provider from session start. A model label or a provider entry in a config file alone does not prove that requests are routed there.

This mode can avoid Codex model usage only when runtime evidence confirms every model request used the external provider. The provider may charge separately. Codex features, tools, or host services may have separate usage and billing.

## Preflight

1. Confirm the current Codex host supports custom model providers and that this session was started with one. Provider settings belong in user-level configuration, not a repository `.codex/config.toml`.
2. Inspect the effective user config and selected profile. Require a non-built-in provider with a HTTPS base URL and `wire_api = "responses"`; credentials must come from an environment variable or an external auth command. Never place a key in config, arguments, logs, reports, or this repository.
3. Confirm the configured model catalog contains `grok-4.7` with `xhigh`, `step-5-preview` with `high`, and `gemini-3.8-flash-high` with `high`, plus positive context windows.
4. Obtain the current session's observed provider and model from trusted host runtime metadata. Run the checker in [preflight](scripts/preflight.py). It returns `blocked` if runtime identity is missing, is OpenAI, or does not match the configured provider and requested role model.
5. If the host does not expose trustworthy runtime identity, stop. Explain that this mode requires starting a new Codex session with the external provider selected, then rerun preflight. Do not infer identity from assistant text or silently switch providers.

See [provider setup](references/provider-setup.md) for safe user-level configuration and [role contract](references/role-contract.md) for delegation and evidence rules.

## Work and delegation

Keep the user's selected task boundaries. Use the root session for small, localized work. For work that benefits from delegation, split it into independent bounded tasks with explicit ownership, expected artifacts, and acceptance checks. Use only native Codex agents available in the current host; request the model and reasoning effort from the role map in the role contract.

For each child, verify provider and model from runtime metadata after it starts and again when its result is available. A missing or mismatched identity makes that result unverified: stop relying on it and report the routing failure. Do not retry through OpenAI, another provider, or another model automatically. A retry requires the user's explicit routing choice and a fresh preflight.

Keep at most three child runs active and two writers active, or fewer when the Codex host's configured concurrency limit is lower. Escalate a difficult task only to another role in the same provider/model registry; after two failed repair rounds, stop and report the remaining issue.

Review the actual changes and run the assigned checks before reporting completion. Record requested and observed provider/model, role, run or session ID when available, changed paths, checks, and uncertainties. Keep reports local; do not publish private provider IDs, URLs, session IDs, or credentials.

## Role model registry

The root uses Grok 4.7 (`grok-4.7`, `xhigh`). Use Gemini 3.8 Flash High (`gemini-3.8-flash-high`, `high`) for exploration, implementation, and test execution. Use Step 5 Preview (`step-5-preview`, `high`) for research and independent review. Grok 4.7 (`grok-4.7`, `xhigh`) is for explicitly requested review gates or escalation. Verify the exact model slug and provider for every role before accepting its work.

Do not claim a model/provider combination is verified based only on its name, model catalog, or configuration. A successful request routed through the session's observed external provider is required for that claim.
