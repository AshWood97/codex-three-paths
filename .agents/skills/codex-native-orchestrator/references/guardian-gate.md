# Optional guardian gate

The plan records `gate_mode` as exactly `pre`, `final`, or `none` and records
whether the risk is `hard_risk`. Risk classification is independent from
guardian authorization: `hard_risk` may be true with `gate_mode=none` when the
user has not requested an extra gate. The root Astra already performs ordinary
planning and risk analysis. The default is `none`; the guardian is an explicit
opt-in compatibility mechanism, not an automatic second opinion.
The Astra budget is exactly zero or one logical gate per task.

## Classification

If the user explicitly requests a gate, choose `pre` when the task makes or
changes a decision involving any of:

- authentication or authorization;
- permissions, privilege, isolation, or another trust boundary;
- irreversible data or schema migration;
- a persistent data or storage format;
- destructive operations;
- a breaking public API, protocol, or schema change; or
- financial or regulatory logic.

Choose `final`, only when `pre` does not apply, for any of the following
explicitly requested checks:

- high-impact concurrency, transaction, recovery, or consistency behavior;
- cross-service changes with a high blast radius;
- conflicting conclusions from agents that the root cannot resolve with
  evidence;
- a root cause that remains materially uncertain;
- test failures whose cause remains unexplained; or
- work the user explicitly marks as release-critical.

Choose `none` for ordinary work and whenever the user has not requested an
extra gate. In `none`, do not call the guardian. If an explicit request could
fit both modes, choose `pre`; never schedule both.

When a user explicitly opts into a hard-risk gate, an unavailable or blocking
gate cannot be silently downgraded. A retry of malformed output is part of the
same logical gate, not a second gate.

## Timing and invocation

For an explicitly requested `pre`, finish enough read-only exploration to state the decision and
invariants, then gate before any implementation edit, migration, destructive
action, or writer. For `final`, complete the implementation and integration
tests, then complete the Sol reviewer pass before the gate at the last
meaningful delivery point. `none` has no guardian invocation.

The authoritative gate is an explicit controller-isolated read-only process
using a structured output contract. The controller launches the guardian with
a temporary minimal `CODEX_HOME` dedicated to that invocation. `guardian.toml`
is advisory compatibility metadata for role discovery and native callers; its
`sandbox_mode`, the requested read-only launch, and the temporary minimal
`CODEX_HOME` are not evidence that the runtime was actually isolated. Before
accepting a gate result, the controller records how effective read-only
permissions were established and records observed runtime values separately
from requested values. If model, effort, sandbox, or effective isolation
cannot be observed, the value is `unknown`.

Send only this compact packet:

- `Mode`: `pre` or `final`;
- `Decision` for `pre`, or for `final` a deterministic final diff bound to the
  base and delivery commit IDs, stable changed paths, and a canonical diff
  artifact and hash;
- `Key invariants`;
- `Test evidence`, including concrete test commands, exit statuses, and
  output or artifact paths/hashes, together with effective read-only runtime
  evidence and baseline evidence or `not applicable`; and
- `Residual risks`.

The canonical final diff is the exact, no-color, no-external-diff, binary
diff for the recorded base and delivery commits. A final packet must carry
that deterministic diff evidence and concrete test evidence; a summary or a
requested test plan is not a substitute.

The guardian must remain read-only, must not delegate, and must not expand
scope. A valid response contains exactly these required sections and a
`Verdict` whose value is exactly `approve`, `revise`, or `block`:

- `Verdict`;
- `Important findings`;
- `Required changes`; and
- `Residual risks`.

Timeout, availability, transport, startup, missing-section, unknown-verdict,
or otherwise malformed output is not a valid verdict. A no-valid-verdict
result may be retried exactly once with the exact same serialized gate packet,
byte-for-byte unchanged. That retry is part of the same one logical gate. If
it also produces no valid verdict, apply the fallback rules immediately and
never retry again after any valid verdict.

## Failure and invalidation

For non-hard work where the user explicitly requested a gate, a failed gate may
fall back to the normal Astra `reviewer`, and the report must contain the exact
marker `Astra gate unavailable`.
A successful fallback is not Astra approval. For a security or
trust boundary, irreversible migration, financial or regulatory logic, or
destructive operation, do not silently downgrade: stop until Astra succeeds
or the user gives an informed explicit waiver.

After a valid verdict, do not call Astra again automatically. A substantive
post-gate scope change invalidates the reviewed decision, trust boundary,
migration behavior, public contract, key invariant, or risk assumptions. For
hard-risk work, pause and ask the user to authorize a second gate or grant an
explicit human waiver. For other work, obtain a fresh Sol review and
state that the earlier Astra conclusion was invalidated.
