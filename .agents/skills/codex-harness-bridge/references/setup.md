# Setup and private configuration

Install and authenticate each selected CLI using its upstream documentation.
Create a private JSON config based on `config.example.json`; never commit it.
Config files must be mode `0600` on Unix. The `env_map` object maps a target
environment variable understood by the harness to the name of a source variable
in the invoking shell. It stores names only, not URLs or credentials. Set base
URLs and API keys in your shell or secret manager. Mapped values are redacted
from saved logs. The child receives only a small base environment (`PATH`,
`HOME`, locale, temp, and platform essentials), plus variables named in
`env_map`. Other parent variables, including database URLs, are not forwarded.

The child still needs the credentials required by its model provider. A model
tool may expose the child process environment to commands it runs; use a
restricted, task-specific credential when possible. Never put credentials in
arguments, the task brief, checked-in files, or run reports.

For Codex CLI, create a user-level provider/model configuration with exact model
IDs. The runner passes the role's selected model ID, a bounded sandbox, JSON
events, and disables multi-agent collaboration. Verify actual provider/model
events before trusting a run.

For Claude Code with a compatible endpoint, configure that endpoint and its
credential environment variables locally. This skill does not claim every
OpenAI-compatible endpoint works with Claude Code. The explicit tools list omits
the Agent tool; workspace-write mode uses `acceptEdits` and still needs a
configured permission policy. The CLI must support `--bare` so project and
user customizations are suppressed for this one-shot invocation.

Grok Build reads model route details from its user-level config. Use a custom
model entry for compatible providers and select a role model alias in the
private `model_ids` mapping. The runner selects its sandbox and disables
subagents.

OpenCode requires two existing primary agents, one for read-only and one for
workspace-write. Configure each to deny its `task` permission so the harness
cannot delegate again. The runner refuses to invoke OpenCode without both agent
names; a name alone does not prove its permissions, so inspect local agent
configuration and runtime identity.

Pi requires a model/provider configured in its user-level model catalog. The
runner selects the model, JSON mode, and explicit built-in tools; it disables
extensions, skills, and trust-gated project resources for that invocation.
`--print` makes the run non-interactive.

DeepSeek Harness is in developer preview. Install the headless profile, then
create six profiles: one per fixed model and mode. Each profile must set that
model as its default, keep filesystem/shell permissions within the requested
mode, and omit subagent/delegation tools. Add its profile name and the exact
configured model route under `deepseek_profiles`. Recheck the upstream CLI and
profile schema before use; a passing stub test is not evidence that a live
DeepSeek deployment is safe or compatible.

## Run one task

Keep the brief minimal and include the goal, permissions, owned paths, known
working-tree edits, constraints, and acceptance checks. Write the run directory
outside the target repository and use a fresh name. Example:

```sh
python3 /path/to/codex-harness-bridge/scripts/run_harness.py \
  --cwd /absolute/path/to/project \
  --brief /absolute/path/to/private-brief.md \
  --run-dir /absolute/path/to/private-runs/run-unique \
  --harness codex-cli --role worker --mode workspace-write \
  --owned-path src/component --owned-path tests/component \
  --config /absolute/path/to/private-config.json
```

`--preflight-only` checks the binary, config, brief, and adapter command without
starting the model process. DeepSeek Harness also requires
`--allow-experimental-dsh`. There are no automatic retries or fallback routes.

The runner serializes its own runs per workspace, captures redacted stdout and
stderr, parses runtime identity from recognized CLI event envelopes, snapshots
workspace and Git metadata, and reports changed paths outside the declared
ownership set. Existing links to targets outside the workspace block a run;
new links are reported as out-of-scope changes. Any workspace mutation during
a read-only run is reported as a violation. Path ownership is an audit boundary,
not an operating-system sandbox. For writes, use an isolated worktree and
independently inspect every resulting diff. Timeout, SIGINT, SIGTERM, and
SIGHUP stop the child process group and leave partial changes for review.

The shared report uses `status: partial` for a clean run awaiting the outer
Codex review, and `status: unverified` when runtime model or provider identity
is missing or conflicting. `bridge_status` gives the more specific runner
outcome. Neither status means the model/harness pair has passed a live smoke
test.
