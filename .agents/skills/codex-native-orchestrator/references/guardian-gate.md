# Automatic final Guardian gate

Every completed task handled by this skill runs one logical Guardian final
gate after successful verification and the Sol Reviewer pass. This is a fixed
policy for native and persistent-runner work, including read-only and
documentation tasks. No separate user request is required. A task means the
whole user assignment, not each agent or DAG node.

The Conductor is a separate mandatory startup phase that runs once before
overall planning. Do not start it again as part of this final gate.

New plans use `gate_mode=final`; omitted mode defaults to `final`, and `pre`
or `none` is rejected. `hard_risk` remains an independent risk classification
for authentication, permissions, irreversible migration, destructive changes,
public contract changes, and financial or regulatory logic. It does not change
the required final-gate timing. The Astra budget is one logical gate per task.

## Timing and invocation

Complete the work and its relevant checks, then complete a successful GPT-6.1
Sol Reviewer pass before the automatic final gate at the last meaningful
delivery point. Reviewer findings must be resolved and affected checks
refreshed first.
For read-only tasks, review the completed results and record unnecessary tests
as `not applicable`. Do not gate before Reviewer or report delivery as complete
without a valid gate result or an explicit user waiver.

For native tasks, record a clean repository baseline before starting work:

```sh
python3 scripts/codex_native_orchestrator.py --repo /absolute/path/to/repository \
  native-begin --run-id TASK_ID --objective 'Task objective'
```

`native-begin` starts the Conductor automatically; do not also spawn it
manually for the same task. When work begins outside a repository, start the
Conductor with native agent tools before planning.

If startup planning fails, retry `native-begin` with the same run ID and
objective before editing. Recovery requires the unchanged manifest and clean
recorded baseline; a valid planning result is reused. Native runs never enter
the ordinary DAG through `resume`. After work, finish with `native-gate` and
its completion receipt.

If the source is dirty or changes should stay uncommitted, use an isolated
evidence checkout containing the original state, record its baseline before
work, then preserve the final content there as an exact commit. This does not
change the user's repository history. Use that same evidence checkout below.

Record the completed assignment in a JSON receipt, then run:

```sh
python3 scripts/codex_native_orchestrator.py --repo /absolute/path/to/repository \
  native-gate /absolute/path/to/completion.json --run-id TASK_ID
```

The receipt contains the same `objective`, boolean `changes`, the completed
`result`, and a `test_evidence` array. Each check has exactly
`command`, `exit_code`, `output_hash`, and `artifact_hashes`; unnecessary checks
may be an empty array for read-only work. Optional `base_head` and
`delivery_commit` IDs must match the controller-recorded baseline and current
clean HEAD. The controller computes the actual canonical diff, tree, and
changed paths, rejects a false no-change declaration or failed checks, and
actually invokes Reviewer before Guardian. Receipt claims of Reviewer approval
cannot replace that invocation. The host session remains the controller, not a
writer node.
The controller rechecks the final repository snapshot after Reviewer and after
Guardian; changes during either check invalidate completion.

Keep the same task ID and byte-equivalent receipt for resume or retry. A changed
receipt after a gate starts cannot silently consume another gate. Before the
gate starts, Reviewer findings may be fixed and the same task's evidence
refreshed against the original baseline. `native-gate` records completed
work and never applies repository changes. If the gate is unavailable or does
not approve, only an explicit user waiver may use `--waiver REASON` with the
same receipt. A direct native `guardian` spawn alone does not establish the
required controller isolation or durable single-gate accounting.

The authoritative gate is a controller-isolated read-only process
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

- `Mode`: `final`;
- A deterministic final diff bound to base and delivery commit IDs, stable
  changed paths, and a canonical diff artifact and hash; for read-only work,
  completed task results and an explicit no-diff statement;
- `Key invariants`;
- `Test evidence`, including concrete test commands, exit statuses, and
  output or artifact paths/hashes, together with effective read-only runtime
  evidence and baseline evidence or `not applicable`;
- The successful Reviewer result; and
- `Residual risks`.

For tasks with changes, the canonical final diff is the exact, no-color,
no-external-diff, binary diff for the recorded base and delivery commits.
The final packet must carry that deterministic diff evidence and concrete
test evidence; a summary or a requested test plan is not a substitute.
Read-only tasks instead carry completed task results and record the absent
repository diff and any unnecessary tests as `not applicable`.

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
it also produces no valid verdict, mark the gate unavailable and stop. Never
retry again after any valid verdict.

## Failure and invalidation

An unavailable Guardian gate cannot be replaced by Reviewer approval. Stop
until the user gives an informed explicit waiver. Record the unavailable
result and preserve its evidence.

After a valid verdict, do not call Astra again automatically. A substantive
post-gate scope change invalidates the reviewed decision, trust boundary,
migration behavior, public contract, key invariant, or risk assumptions. For
hard-risk work, pause and ask the user to authorize a second gate or grant an
explicit human waiver. For other work, obtain a fresh Sol review and
state that the earlier Astra conclusion was invalidated.
