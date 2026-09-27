# Adapter contract

The runner starts one child process with an argument vector and a selected
working directory. It does not invoke a shell. Each adapter emits machine
readable JSON lines when the upstream CLI supports them. The parser extracts
provider, model, and session identifiers only from observed event fields; a
configured alias or a model's prose is not runtime evidence.

| Adapter | Executable and one-shot mode | Permission controls | Status |
| --- | --- | --- | --- |
| Codex CLI | `codex exec --json` | `--sandbox read-only` or `workspace-write`; disables Codex multi-agent tools | Supported CLI surface; provider/model still require user configuration |
| Claude Code | `claude --print --output-format stream-json` | `plan` or `acceptEdits`; explicit tool allowlist excludes Agent | Requires a compatible API endpoint and matching provider configuration for non-Anthropic models |
| Grok Build | `grok -p ... --output-format streaming-json` | `read-only` or `workspace` sandbox and `--no-subagents` | Supported CLI surface; custom model routes require user configuration |
| OpenCode | `opencode run --format json` | Requires configured primary agent per mode; agent must deny task delegation | Supported CLI surface; this skill fails preflight if agent names are missing |
| Pi | `pi --mode json` | explicit tool allowlist; disables extensions, skills, and project trust | Supported CLI surface; local custom providers must be configured |
| DeepSeek Harness | `dsh --profile <name> --json` | Requires a profile per model and mode with a narrow permission policy | Experimental developer preview; profile is responsible for model routing and sandbox policy |

## Fixed role map

The model intent is fixed by role: Grok 4.7 handles `root` and `complex`, Step
5 Preview handles `researcher` and `reviewer`, and Gemini 3.8 Flash High handles
`explorer`, `worker`, and `tester`. `model_ids` in private config maps those
canonical names to IDs configured in a particular harness. It never changes the
role map. The report includes both `requested_model` and `configured_model_id`.

Runtime identity is verified only when an adapter emits it in parseable JSON
events. If the process exits without observed provider or model fields, report
them as unknown. If an observed model conflicts with the configured route, stop
using the result and report a model mismatch.

## DeepSeek Harness status

DeepSeek Harness is explicitly experimental and requires the caller to pass
`--allow-experimental-dsh`. Its headless CLI supports a one-shot `--json` event
stream, but the documented command selects the model through the configured
profile rather than a per-run model flag. The private config therefore maps each
fixed model and execution mode to a profile and a configured model ID. The
profile must also disable nested delegation and set its filesystem/shell
permissions to the requested mode. A configured profile/model pair is not proof
of runtime identity; use observed event fields and keep identity unknown when
they are absent.

Upstream interfaces change. This package's tests use stub CLIs and prove the
adapter contract only; they are not live provider, auth, model, or version
compatibility tests.
