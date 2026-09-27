# External provider setup

Codex loads custom provider definitions and model selection from user-level configuration. See the official [Codex advanced configuration](https://developers.openai.com/codex/config-advanced) and [configuration reference](https://developers.openai.com/codex/config-reference). This skill does not edit user configuration or project files.

1. Configure the provider in the user's Codex config (under `$CODEX_HOME`, which defaults to `~/.codex`) or a selected user-level profile. Add the provider table and model catalog setting shown in [the example](../examples/user-config.example.toml); merge the example keys instead of replacing existing config.
2. Replace the example provider ID, base URL, credential environment variable, and catalog path with values from the provider administrator. Set `model_catalog_json` to an absolute path to the catalog actually used by this session. The endpoint must speak the Responses API. Keep the credential only in the named environment variable or a provider-supported external auth command. Do not use `experimental_bearer_token` or hard-code credentials in config.
3. Ensure the catalog is the one actually loaded by the selected Codex session. It must define all three required model slugs, their supported reasoning efforts, and a positive `context_window`. A catalog entry records configuration metadata; it does not prove the endpoint supports the model.
   The checker reads `models[].slug`, `models[].context_window`, and `models[].supported_reasoning_levels[].effort`. It does not validate every version-specific Codex catalog field; use a catalog already accepted by the exact Codex client version and do not build one from this minimal field list.
4. Start a new Codex session with the external provider selected and Grok 4.7 as the root model. Existing sessions retain their original routing. If a profile or CLI override is used, pass that exact profile to preflight and inspect the effective runtime identity.
5. Run the read-only checker from the skill directory, passing observed identity from a trusted host/runtime source:

   ```sh
   python3 scripts/preflight.py \
     --role root \
     --observed-provider "$OBSERVED_PROVIDER" \
     --observed-model "$OBSERVED_MODEL" \
     --evidence-source host_runtime_metadata
   ```

   Add `--profile "$CODEX_PROFILE"` only when this session was started with that user-level profile. Omit `--session-id` if the host does not expose one. The checker emits a run ID if no `--run-id` is supplied. It never prints provider URLs, config/catalog paths, or credential values.

If the host cannot expose reliable current-session provider/model identity, this check cannot pass. Start the session through the configured external provider using a host that exposes that identity, then rerun the check. The emitted record is explicitly a preflight result; even a passed preflight reports `status: unverified` until provider telemetry confirms actual request routing. Do not treat a successful config check as proof that inference was routed correctly.

The preflight reads user configuration and catalog files only. It does not check network reachability, contact the provider, or prove how a remote API gateway routes a request. Verify actual route evidence from the Codex host/provider telemetry after a successful request. External provider usage may be billed by that provider.
