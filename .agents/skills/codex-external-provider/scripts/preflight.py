#!/usr/bin/env python3
"""Read-only routing preflight for the codex-external-provider skill."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError as exc:  # pragma: no cover - runtime guard
    raise SystemExit("Python 3.11 or newer is required") from exc


BUILTIN_PROVIDERS = {"openai", "ollama", "lmstudio", "amazon-bedrock"}
MODEL_ROLES = {
    "root": ("grok-4.7", "xhigh"),
    "explorer": ("gemini-3.8-flash-high", "high"),
    "worker": ("gemini-3.8-flash-high", "high"),
    "tester": ("gemini-3.8-flash-high", "high"),
    "researcher": ("step-5-preview", "high"),
    "reviewer": ("step-5-preview", "high"),
    "worker_step": ("step-5-preview", "high"),
    "tester_step": ("step-5-preview", "high"),
    "analyst_grok": ("grok-4.7", "xhigh"),
    "worker_grok": ("grok-4.7", "xhigh"),
    "tester_grok": ("grok-4.7", "xhigh"),
    "reviewer_grok": ("grok-4.7", "xhigh"),
    "guardian": ("grok-4.7", "xhigh"),
}
EXIT_READY = 0
EXIT_BLOCKED = 2
SAFE_PROFILE = re.compile(r"^[A-Za-z0-9_-]+$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
SECRET_LIKE = re.compile(
    r"(?i)(?:bearer|api[_-]?key|(?:sk|xai)-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_-]{20,})"
)
SECRET_KEY_NAME = re.compile(r"(?i)(authorization|api[_-]?key|access[_-]?token|bearer|credential|secret)")


def report_identity(value: str | None) -> str | None:
    """Keep malformed or credential-like caller values out of JSON output."""
    if value is None or not SAFE_ID.fullmatch(value) or SECRET_LIKE.search(value):
        return None
    return value


class PreflightError(RuntimeError):
    """Safe reason for blocking the preflight."""


def _read_toml(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except FileNotFoundError as exc:
        raise PreflightError(f"missing {label}") from exc
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise PreflightError(f"invalid {label}") from exc
    if not isinstance(value, dict):
        raise PreflightError(f"invalid {label}")
    return value


def effective_config(codex_home: Path, profile: str | None) -> dict[str, Any]:
    config = _read_toml(codex_home / "config.toml", "user-level Codex config")
    if profile is None:
        return config
    if not SAFE_PROFILE.fullmatch(profile):
        raise PreflightError("invalid profile name")
    profile_config = _read_toml(codex_home / f"{profile}.config.toml", "selected profile config")
    # Profiles override selection and catalog settings. Provider definitions
    # are machine-local and remain sourced from config.toml.
    merged = dict(config)
    for key in ("model", "model_provider", "model_catalog_json", "model_reasoning_effort"):
        if key in profile_config:
            merged[key] = profile_config[key]
    return merged


def load_catalog(path_value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(path_value, str) or not path_value.strip():
        raise PreflightError("model_catalog_json is required")
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        raise PreflightError("model_catalog_json must be an absolute user-level path")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PreflightError("configured model catalog is missing") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreflightError("configured model catalog is invalid JSON") from exc
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise PreflightError("model catalog has no models list")
    catalog: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("slug"), str):
            continue
        slug = row["slug"]
        if slug in catalog:
            raise PreflightError("model catalog contains duplicate slugs")
        levels = row.get("supported_reasoning_levels")
        efforts = {
            item.get("effort")
            for item in levels
            if isinstance(item, dict) and isinstance(item.get("effort"), str)
        } if isinstance(levels, list) else set()
        context_window = row.get("context_window")
        catalog[slug] = {
            "efforts": efforts,
            "context_window": context_window
            if isinstance(context_window, int) and not isinstance(context_window, bool) and context_window > 0
            else None,
        }
    return catalog


def validate_provider(config: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, dict[str, Any]]]:
    provider_id = config.get("model_provider")
    if not isinstance(provider_id, str) or not SAFE_ID.fullmatch(provider_id) or SECRET_LIKE.search(provider_id):
        raise PreflightError("active model_provider is unknown")
    if provider_id in BUILTIN_PROVIDERS:
        raise PreflightError("active provider is built-in; an external custom provider is required")
    providers = config.get("model_providers")
    provider = providers.get(provider_id) if isinstance(providers, dict) else None
    if not isinstance(provider, dict):
        raise PreflightError("active custom provider is not configured")
    base_url = provider.get("base_url")
    if not isinstance(base_url, str):
        raise PreflightError("custom provider base_url is missing")
    try:
        parsed_url = urlsplit(base_url)
    except ValueError as exc:
        raise PreflightError("custom provider base_url is invalid") from exc
    if parsed_url.scheme != "https" or not parsed_url.netloc or parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
        raise PreflightError("custom provider base_url must be a credential-free HTTPS URL")
    if provider.get("wire_api", "responses") != "responses":
        raise PreflightError("custom provider must use the Responses API")
    if provider.get("experimental_bearer_token") is not None:
        raise PreflightError("static provider credentials are not allowed; use env_key or external auth")
    for field in ("http_headers", "query_params"):
        values = provider.get(field)
        if isinstance(values, dict) and any(SECRET_KEY_NAME.search(str(key)) for key in values):
            raise PreflightError("static credentials in headers or query parameters are not allowed")
    if provider.get("requires_openai_auth") is True:
        raise PreflightError("custom provider cannot use OpenAI authentication")
    env_key = provider.get("env_key")
    auth = provider.get("auth")
    has_env_key = isinstance(env_key, str) and bool(env_key.strip())
    has_auth_command = isinstance(auth, dict) and isinstance(auth.get("command"), str) and bool(auth["command"].strip())
    if has_env_key and has_auth_command:
        raise PreflightError("choose either env_key or external auth command")
    if has_env_key and (env_key not in os.environ or not os.environ.get(env_key)):
        raise PreflightError("provider credential environment variable is not present")
    if not has_env_key and not has_auth_command:
        raise PreflightError("provider must use env_key or external auth command")
    catalog = load_catalog(config.get("model_catalog_json"))
    for role, (slug, effort) in MODEL_ROLES.items():
        row = catalog.get(slug)
        if row is None:
            raise PreflightError(f"model catalog is missing required model for role {role}")
        if row["context_window"] is None:
            raise PreflightError(f"model catalog has no positive context window for role {role}")
        if effort not in row["efforts"]:
            raise PreflightError(f"model catalog does not support the required effort for role {role}")
    return provider_id, provider, catalog


def build_report(
    *,
    codex_home: Path,
    profile: str | None,
    role: str,
    observed_provider: str | None,
    observed_model: str | None,
    evidence_source: str | None,
    run_id: str | None,
    session_id: str | None,
) -> tuple[int, dict[str, Any]]:
    requested_model, requested_effort = MODEL_ROLES[role]
    generated_run_id = run_id or str(uuid.uuid4())
    if not SAFE_ID.fullmatch(generated_run_id) or SECRET_LIKE.search(generated_run_id):
        raise PreflightError("invalid run ID")
    if session_id is not None and (not SAFE_ID.fullmatch(session_id) or SECRET_LIKE.search(session_id)):
        raise PreflightError("invalid session ID")
    if evidence_source not in {None, "host_runtime_metadata"}:
        raise PreflightError("invalid runtime evidence source")
    if (observed_provider is None) != (observed_model is None):
        raise PreflightError("both observed provider and model are required")
    report: dict[str, Any] = {
        "schema_version": 1,
        "mode": "external_provider",
        "result_type": "preflight",
        "run_id": generated_run_id,
        "task_contract": {
            "objective": "Check user-level external provider configuration and compare it with supplied runtime identity.",
            "allowed_actions": ["read Codex user config, selected profile, and model catalog"],
            "owned_paths": [],
            "constraints": ["read-only", "never print or store credential values"],
            "acceptance": ["custom Responses provider configured", "required model catalog entries supported", "runtime identity matches requested role"],
        },
        "requested": {"role": role, "model": requested_model, "provider": None, "effort": requested_effort},
        "observed": {
            "provider": report_identity(observed_provider),
            "model": report_identity(observed_model),
            "session_ids": [session_id] if session_id is not None else [],
            "evidence_source": evidence_source,
        },
        "status": "unverified",
        "preflight_status": "blocked",
        "changed_paths": [],
        "checks": [],
        "uncertainties": [],
    }
    checks: list[dict[str, Any]] = report["checks"]
    if (observed_provider is not None and report_identity(observed_provider) is None) or (
        observed_model is not None and report_identity(observed_model) is None
    ):
        report["uncertainties"].append("unsafe or credential-like runtime identity was omitted from the report")

    try:
        config = effective_config(codex_home, profile)
        configured_model = config.get("model")
        if configured_model != MODEL_ROLES["root"][0]:
            raise PreflightError("effective user config must select Grok 4.7 as the root model")
        provider_id, _provider, _catalog = validate_provider(config)
        report["requested"]["provider"] = provider_id
        if observed_provider is None or observed_model is None:
            report["uncertainties"].append("trusted current-session provider/model identity was not supplied")
            raise PreflightError("runtime identity is unknown; fail closed")
        if evidence_source is None:
            report["uncertainties"].append("the source for runtime identity was not supplied")
            raise PreflightError("runtime evidence source is unknown; fail closed")
        if observed_provider != provider_id:
            raise PreflightError("observed runtime provider does not match the configured external provider")
        if observed_model != requested_model:
            raise PreflightError("observed runtime model does not match the requested role model")
    except PreflightError as exc:
        report["status"] = "blocked"
        report["reason"] = str(exc)
        checks.append({
            "command": f"python3 scripts/preflight.py --role {role} (read-only configuration and runtime identity checks)",
            "status": "failed",
            "exit_code": EXIT_BLOCKED,
        })
        return EXIT_BLOCKED, report

    checks.append({
        "command": f"python3 scripts/preflight.py --role {role} (read-only configuration and runtime identity checks)",
        "status": "passed",
        "exit_code": EXIT_READY,
    })
    report["preflight_status"] = "passed"
    report["uncertainties"].append(
        "preflight does not prove remote routing; verify provider telemetry for this request and every child run"
    )
    return EXIT_READY, report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only external-provider routing preflight")
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    parser.add_argument("--profile", help="selected user-level profile name, if any")
    parser.add_argument("--role", choices=tuple(MODEL_ROLES), default="root")
    parser.add_argument("--observed-provider", help="provider ID from trusted current runtime metadata")
    parser.add_argument("--observed-model", help="model slug from trusted current runtime metadata")
    parser.add_argument(
        "--evidence-source",
        choices=("host_runtime_metadata",),
        help="source for the supplied runtime identity",
    )
    parser.add_argument("--run-id", help="optional run identifier; generated when omitted")
    parser.add_argument("--session-id", help="optional runtime session identifier")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        code, report = build_report(
            codex_home=args.codex_home.expanduser(),
            profile=args.profile,
            role=args.role,
            observed_provider=args.observed_provider,
            observed_model=args.observed_model,
            evidence_source=args.evidence_source,
            run_id=args.run_id,
            session_id=args.session_id,
        )
    except PreflightError as exc:
        code = EXIT_BLOCKED
        report = {
            "schema_version": 1,
            "mode": "external_provider",
            "result_type": "preflight",
            "run_id": report_identity(args.run_id) or str(uuid.uuid4()),
            "task_contract": {
                "objective": "Check user-level external provider configuration and compare it with supplied runtime identity.",
                "allowed_actions": ["read Codex user config, selected profile, and model catalog"],
                "owned_paths": [],
                "constraints": ["read-only", "never print or store credential values"],
                "acceptance": ["custom Responses provider configured", "required model catalog entries supported", "runtime identity matches requested role"],
            },
            "requested": {
                "role": args.role,
                "model": MODEL_ROLES[args.role][0],
                "provider": None,
                "effort": MODEL_ROLES[args.role][1],
            },
            "observed": {
                "provider": report_identity(args.observed_provider),
                "model": report_identity(args.observed_model),
                "session_ids": [report_identity(args.session_id)] if report_identity(args.session_id) else [],
                "evidence_source": args.evidence_source,
            },
            "status": "blocked",
            "preflight_status": "blocked",
            "reason": str(exc),
            "changed_paths": [],
            "checks": [{"command": f"python3 scripts/preflight.py --role {args.role}", "status": "failed", "exit_code": EXIT_BLOCKED}],
            "uncertainties": [],
        }
    sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
