# Runner and recovery

The hybrid runner is a coordination mode selected from user controls, routing
triggers, and the host's tentative task structure. Its defaults are in the
manifest. After the host chooses the execution mode, the runner starts one
mandatory Conductor startup phase to independently assess the tentative plan.
It then schedules ordinary plan nodes in dependency waves, favors the critical
path, and never creates work solely to fill the four-thread concurrency cap.
Resume only from the saved state for the selected mode; do not carry startup
records across native and runner modes. If Conductor planning returns
`needs_input` or `failed`, do not schedule ordinary work until the issue is
resolved.

## Plan and state

Each new plan uses `gate_mode=final` (also the omitted-field default) and a boolean
`hard_risk`. The host session executes the agreed plan and is not a plan node;
ordinary task nodes use only `explorer`, `worker`, `tester`, `researcher`, or
`reviewer`. Conductor is a controller-managed startup phase, not a plan node.
Guardian remains a public named role for native compatibility, but the
persistent runner reserves it for the single controller-owned `gate_mode`
invocation.
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
path. The final Guardian gate remains required when using native dispatch.

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

The automatic `final` gate runs after successful verification and the GPT-6.1
Sol Reviewer pass for every task, including runs without writers. One logical
Astra gate is allowed per task. New plans cannot use `pre` or `none`; old run
records retain their original evidence and are stopped as stale after the
manifest changes, rather than silently resumed under the new policy.

## Timeouts, retry, and resume

Manifest timeout values are seconds: read-only nodes 1,800, writers 3,600,
and the guardian 900. A timeout preserves state for resume. A failed read-only
node may be retried once with the same structured input packet. Once a writer
has started, do not create a duplicate writer thread: resume the original
thread or move the node to `needs_input` while retaining its worktree.

Codex child processes inherit explicit proxy environment settings. On macOS,
enabled system HTTP or HTTPS proxies fill only proxy variables that are absent
from the environment; an explicitly empty variable is preserved. Invocation
stdout and stderr are retained with mode `0600` under the private
`astra-orchestrator/invocations` directory below `CODEX_HOME`, including partial output after a
timeout. State and error records expose the transcript paths without copying
the transcript contents into the journal.

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
