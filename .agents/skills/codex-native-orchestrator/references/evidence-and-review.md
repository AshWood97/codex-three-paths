# Evidence and review

Every node result and delivery report must make the decision auditable without
requiring a full transcript. Record the plan version, repository identity,
base SHA, normalized owned paths, dependency result hashes, node status,
commit SHA when applicable, commands, tests, observed model/effort/sandbox,
findings, residual risks, and next action.

Requested configuration is not runtime evidence. This includes role metadata,
the requested read-only sandbox, and the guardian's temporary minimal
`CODEX_HOME`. If the model, effort, sandbox, or effective isolation cannot be
observed, record `unknown`; do not convert a TOML request, a launch command,
or a role description into proof of execution. Guardian packets additionally
record the serialized packet hash, gate mode, read-only isolation evidence,
validity, retry count, verdict, and any explicit waiver.

Each result record carries `requested` and `observed` objects with `model`,
`effort`, and `sandbox`. Use the literal value `unknown` for an unobservable
field in `observed`.

## Review order

The root reviews the integrated diff, ownership check, test output, and
dependency evidence before delivery. The Astra `reviewer` is an independent
read-only pass for material correctness, permission, data-integrity,
concurrency, compatibility, and missing-test risks. Resolve material findings
inside the owned scope and rerun focused validation. Do not describe a change
as Astra-approved after a `revise` verdict or after a post-gate scope change.

For a `final` gate, the packet must bind the deterministic final diff to the
recorded base and delivery commits, changed paths, and canonical diff hash.
It must also carry concrete test evidence: the exact commands, exit status,
and output or artifact path/hash for each relevant check. Requested isolation
settings never satisfy the observed-runtime portion of that evidence.

Evidence becomes `stale` whenever its base SHA, dependency results, plan
version, owned paths, or relevant commit changes. Stale tests and reviews do
not authorize apply; regenerate them from the current integration state.

## Delivery report

The final report names the selected route and overrides, nodes and dependency
waves, role configuration and observed runtime values, commits and changed
paths, commands and test results, review findings, guardian mode/verdict or
`none`, recovery actions, and residual risks. It includes the deterministic
final diff and concrete test evidence when a final gate is used. It must
distinguish `gate_mode=none` or an unavailable gate from an approval and must
state any validation that could not run.
