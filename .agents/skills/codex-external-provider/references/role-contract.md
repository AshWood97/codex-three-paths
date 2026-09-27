# Role and evidence contract

## Role assignments

| Role | Requested model | Effort | Access |
| --- | --- | --- | --- |
| `root` | `grok-4.7` | `xhigh` | Orchestration, integration, final review |
| `explorer`, `worker`, `tester` | `gemini-3.8-flash-high` | `high` | Read-only exploration; scoped implementation; assigned tests |
| `researcher`, `reviewer` | `step-5-preview` | `high` | Read-only research and independent review |
| `worker_step`, `tester_step` | `step-5-preview` | `high` | Explicit escalation for assigned writing/testing work |
| `analyst_grok`, `worker_grok`, `tester_grok`, `reviewer_grok`, `guardian` | `grok-4.7` | `xhigh` | Escalation; guardian only on explicit request |

Use only roles the current Codex host actually exposes. Do not create or imply a role merely because it appears in this table. Keep each delegation to one objective with file ownership and verifiable acceptance criteria. Avoid overlapping writers. A read-only role cannot write. Do not allow a child to delegate further unless the host and user explicitly authorize that topology.

Keep at most three child runs active and two writers active, or fewer when the host's configured limit is lower. Escalate within the registry after a failed attempt; after two failed repair rounds, stop and report rather than repeating the same route.

## Runtime identity

Before using a child result, inspect its authoritative runtime metadata. Record the requested role/model/provider and the observed model/provider. The provider must equal the root session's configured custom provider; the model must equal the role's requested slug. If the host omits either field, record it as unknown and do not count the result as verified. A child's self-report is not runtime evidence.

Run `scripts/preflight.py --role ROLE --observed-provider ID --observed-model SLUG --evidence-source host_runtime_metadata` for each role where host metadata is available. The command checks the user config/catalog and compares the supplied runtime identity; it does not independently retrieve host metadata. Its output is a preflight record, not proof of remote routing, and successful preflights remain `unverified` until provider telemetry is checked. If the identity cannot be obtained, fail closed.

## Run evidence

Keep a local record for each delegated run with:

- `mode`: `codex-external-provider`;
- `run_id` and `session_id` when exposed by the host;
- requested role, model, and provider;
- observed provider and model, or `unknown`;
- status (`verified`, `blocked`, or `unverified`);
- changed paths, checks and results, and uncertainties.

Do not include credentials, provider URLs, private config paths, or session identifiers in a public report. Never claim Codex model usage was avoided unless every root and child inference request has verifiable external-provider routing evidence. Even then, state that external service charges may apply.
