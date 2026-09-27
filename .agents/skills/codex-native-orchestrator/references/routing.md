# Routing

The repository manifest is the machine-readable source of truth for topology,
runner defaults, paths, timeouts, and managed configuration keys. Do not infer
models or limits from a user's global configuration when the manifest and
named role files are available.

## Default route

Use the native named-role path for an ordinary bounded task. First classify it
as `root-only` or `delegated`: root-only is reserved for genuinely small,
localized work with no material independent context; delegation is required
when the user asks for agents or when exploration, independent workstreams,
cross-component debugging, separate verification, current-fact research, or
independent review materially improves the result.

For a delegated request, the root must make an actual native
`multi_agent_v1__spawn_agent` call (also known as `spawn_agent` on some
hosts), choose a fixed named role, retain the returned id, and wait for the
required result. A role TOML is only a profile; it is not a spawn. Do not
silently replace a required child with root-thread work. `root-only` and an
explicit user request to stay in the root are the only ordinary suppressors.
Do not create parallel work merely because a change touches more than one
file; that rule controls unnecessary parallelism, not the delegation gate.

Use the hybrid persistent runner when at least one of these conditions holds:

1. The user explicitly requests a DAG, batch, resume, persistence, or a
   persistent run.
2. The plan contains at least three dependent nodes.
3. The plan contains at least two writers that can run in parallel.
4. The task needs recovery across separate root turns.

“Multi-file” alone is not a trigger. A runner that is forced off uses the
native path unless a safety rule requires stopping for user input; a runner
that is forced on still validates the plan and safety constraints before
starting work.

## User overrides

Recognize these explicit controls:

- `runner on` or `runner off`: force or suppress persistent-runner routing.
- `root-only`: do not delegate and do not start the runner.
- `max agents N`: request a cap; clamp it to the manifest maximum of four and
  to at least one when execution is enabled.
- `read-only`: make the whole run read-only, including writers and apply
  operations.

The most recent explicit value wins when the same control is repeated.
`root-only` suppresses delegation, `read-only` is a capability ceiling, and a
requested agent count never exceeds four. The root records the selected route
and overrides in the run report.

## Routing order

1. Parse explicit controls and establish the read-only ceiling.
2. Use `gate_mode=none` by default. Set `pre` or `final` only when the user
   explicitly requests an Astra gate/guardian review or an external policy
   requires one; the root Astra already performs ordinary risk analysis.
3. Decide native versus hybrid routing from the trigger rules and overrides.
4. For delegated work, create bounded contracts and non-overlapping ownership
   before starting writers.
5. Preserve the root's responsibility for integration, review, and delivery.

The runner is a coordination mode, not a new role. It must use the five
ordinary roles and the model assignments in the manifest. The controller-only
guardian is not scheduled unless an explicit gate mode is present.
