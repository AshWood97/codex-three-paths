# Runner and recovery

The hybrid runner is an opt-in coordination mode selected by the routing
triggers and user overrides. Its defaults are in the manifest. It schedules
plan nodes in dependency waves, favors the critical path, and never creates
work solely to fill the four-thread concurrency cap.

## Plan and state

Each plan declares `gate_mode` (`pre`, `final`, or `none`) and a boolean
`hard_risk`. The root owns the plan and is not a plan node; ordinary task nodes
use only `explorer`, `worker`, `tester`, `researcher`, or `reviewer`. Guardian
remains a public named role for native compatibility, but the persistent runner
reserves it for the single controller-owned `gate_mode` invocation.
Each plan node declares `task_id`, `role`, `depends_on`, `owned_paths`,
`prompt`, `read_only`, and `writes`. Normalized execution nodes additionally
carry `objective`, `scope`, `non_goals`, `acceptance_criteria`, `write_mode`,
`context_refs`, `timeout`, and `retry_policy`. Normalized relative paths must
remain inside the repository and cannot overlap another writer.
Material writer plans include a `tester` node downstream of affected writers.
Tester records concrete commands and results before the controller's ordinary
integration review. A `final` Guardian gate additionally requires successful
read-only Tester evidence downstream of every writer.
Runner Tester nodes must use read-only checks. A `writes=true` Tester node is
integrated as a delivery writer. For required tests that generate output,
choose native dispatch so Tester evidence precedes Reviewer. If the user
explicitly requires this runner, stop and report the unsupported verification
path. Do not request a final Guardian gate when its read-only Tester evidence
cannot be produced.

Durable runs live below the manifest's `run_state_root` and contain the plan,
state, a JSONL event journal, per-node structured inputs/results/evidence, and
a report. Node states are fixed as:

`pending`, `ready`, `running`, `needs_input`, `succeeded`, `failed`,
`blocked`, `cancelled`, and `stale`.

State updates use a same-directory temporary file, flush and `fsync`, then
replace the prior state while holding the run lock. Events are filtered
structured records, not an implicit promise to retain full transcripts.
`state.json` is authoritative for scheduling and recovery; the event journal
supports audit and validation but does not override the saved state. State
durability does not make external work atomic: writer side effects, commits,
and deploy operations must still be verified and recovered conservatively.

No additional node state is valid.

## Writers and integration

The source repository must be clean before a writer DAG starts. Every writer
and the integration stage use an isolated, non-local Git checkout with
private metadata. The checkout does not retain an `origin` remote and does
not share the source repository's refs, config, hooks, or object writes.
Every writer uses an explicit isolated checkout and branch, leaves only its
verified owned-path changes in the working tree, and is checked for changes
outside its owned paths. The model process never stages or commits: the
controller owns the commit broker, verifies Git metadata and path evidence,
stages exact NUL-delimited paths, and creates one auditable controller-owned
commit bound to the input and output hashes.

That controller-owned writer commit is fetched only from the private writer
checkout into the private integration checkout, where it is verified and
cherry-picked.
Integration creates the delivery commit and records the deterministic final
diff as run evidence. Delivery refs are never force-overwritten. The current
checkout is changed only by an explicit, validated apply step after verifying
repository identity, base SHA, branch, clean state, evidence, and ownership;
only then does source fetch the exact delivery commit from private
integration and fast-forward it. No delivery commit is fetched into source
before apply.

If a source checkout is dirty, read-only work may continue but writer work is
blocked until the user commits or cleans it. Ambiguous or uncommitted writer
worktrees are preserved for inspection; the runner must not use broad
destructive Git cleanup.

The `pre` gate runs before any writer only when explicitly requested. The
`final` gate runs only after integration tests and the Sol reviewer pass.
`none` is the default and runs no guardian process; all three plan values
still allow at most one logical Astra gate when a user explicitly opts in.

## Timeouts, retry, and resume

Manifest timeout values are seconds: read-only nodes 1,800, writers 3,600,
and the guardian 900. A timeout preserves state for resume. A failed read-only
node may be retried once with the same structured input packet. Once a writer
has started, do not create a duplicate writer thread: resume the original
thread or move the node to `needs_input` while retaining its worktree.

On restart, acquire the run lock and validate the saved plan, `state.json`,
and complete journal records. `state.json` remains authoritative; resume
reconciles the ready set from successful dependency states and reconciles any
pending gate from its saved packet, hash, and status. A pending or
indeterminate gate is never treated as approval or silently duplicated.
Eligible read-only tasks may reuse their saved input packet within the retry
limit. An interrupted writer remains preserved and moves to `needs_input`
rather than being launched again. A changed base SHA, dependency result, plan
version, or commit makes dependent tests, review, and conclusions `stale`;
rerun them before apply. Cancellation must preserve enough state to report
what ran and what remains.

Token limits, when present, stop new scheduling at the limit and make a best
effort to interrupt active nodes. The runner does not promise zero overspend
under concurrency.
