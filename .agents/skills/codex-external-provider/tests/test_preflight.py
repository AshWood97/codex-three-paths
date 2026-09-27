from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
import preflight  # noqa: E402


PROVIDER_ID = "test_external_provider"
SECRET = "TEST_SECRET_MUST_NOT_LEAK"


def _catalog(*, missing: set[str] | None = None, wrong_effort: str | None = None) -> dict:
    required = {
        "grok-4.7": (240000, "xhigh"),
        "step-5-preview": (240000, "high"),
        "gemini-3.8-flash-high": (240000, "high"),
    }
    rows = []
    for slug, (context, effort) in required.items():
        if missing and slug in missing:
            continue
        levels = ["low", "medium", "high", "xhigh"]
        if wrong_effort == slug:
            levels = ["low", "medium", "high"]
        rows.append(
            {
                "slug": slug,
                "context_window": context,
                "supported_reasoning_levels": [{"effort": item} for item in levels],
            }
        )
    return {"models": rows}


def write_home(root: Path, *, provider_id: str = PROVIDER_ID, base_url: str = "https://provider.example.invalid/v1", model: str = "grok-4.7", wire_api: str = "responses", include_key: bool = True, static_token: str | None = None, catalog: dict | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "agents").mkdir()
    catalog_path = root / "private-catalog.json"
    catalog_path.write_text(json.dumps(catalog or _catalog()), encoding="utf-8")
    provider_lines = [
        f'model = "{model}"',
        f'model_provider = "{provider_id}"',
        f'model_catalog_json = "{catalog_path}"',
        "",
        f'[model_providers."{provider_id}"]',
        'name = "Test provider"',
        f'base_url = "{base_url}"',
        f'wire_api = "{wire_api}"',
    ]
    if include_key:
        provider_lines.append('env_key = "TEST_EXTERNAL_API_KEY"')
    if static_token is not None:
        provider_lines.append(f'experimental_bearer_token = "{static_token}"')
    (root / "config.toml").write_text("\n".join(provider_lines) + "\n", encoding="utf-8")


def snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    }


class PreflightTests(unittest.TestCase):
    def invoke(self, home: Path, *extra: str):
        stdout, stderr = io.StringIO(), io.StringIO()
        args = [
            "--codex-home",
            str(home),
            "--observed-provider",
            PROVIDER_ID,
            "--observed-model",
            "grok-4.7",
            "--evidence-source",
            "host_runtime_metadata",
            *extra,
        ]
        with patch.dict(os.environ, {"TEST_EXTERNAL_API_KEY": SECRET}), redirect_stdout(stdout), redirect_stderr(stderr):
            code = preflight.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_ready_report_is_read_only_and_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            write_home(home)
            before = snapshot(home)
            code, out, err = self.invoke(home, "--session-id", "session-test")
            report = json.loads(out)
            self.assertEqual(code, preflight.EXIT_READY, out + err)
            self.assertEqual(report["schema_version"], 1)
            self.assertEqual(report["mode"], "external_provider")
            self.assertEqual(report["requested"], {"effort": "xhigh", "model": "grok-4.7", "provider": PROVIDER_ID, "role": "root"})
            self.assertEqual(report["observed"]["provider"], PROVIDER_ID)
            self.assertEqual(report["observed"]["model"], "grok-4.7")
            self.assertEqual(report["observed"]["session_ids"], ["session-test"])
            self.assertEqual(report["observed"]["evidence_source"], "host_runtime_metadata")
            self.assertRegex(report["run_id"], r"^[0-9a-f-]{36}$")
            self.assertEqual(report["status"], "unverified")
            self.assertEqual(report["preflight_status"], "passed")
            self.assertEqual(report["changed_paths"], [])
            self.assertEqual(report["checks"][0]["status"], "passed")
            self.assertNotIn(SECRET, out + err)
            self.assertNotIn(str(home), out + err)
            self.assertNotIn("provider.example", out + err)
            self.assertEqual(snapshot(home), before)

    def test_missing_runtime_identity_blocks_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            write_home(home)
            before = snapshot(home)
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {"TEST_EXTERNAL_API_KEY": SECRET}), redirect_stdout(stdout), redirect_stderr(stderr):
                code = preflight.main(["--codex-home", str(home)])
            report = json.loads(stdout.getvalue())
            self.assertEqual(code, preflight.EXIT_BLOCKED)
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(report["observed"]["provider"], None)
            self.assertEqual(report["observed"]["model"], None)
            self.assertIn("unknown", report["reason"])
            self.assertEqual(snapshot(home), before)

    def test_openai_and_unknown_provider_block_without_mutation(self):
        cases = [
            ("openai", "active provider is built-in"),
            ("unconfigured_provider", "active custom provider is not configured"),
        ]
        for provider_id, message in cases:
            with self.subTest(provider=provider_id), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                if provider_id == "unconfigured_provider":
                    write_home(home)
                    config_path = home / "config.toml"
                    config_path.write_text(
                        config_path.read_text(encoding="utf-8").replace(
                            f'model_provider = "{PROVIDER_ID}"',
                            f'model_provider = "{provider_id}"',
                        ),
                        encoding="utf-8",
                    )
                else:
                    write_home(home, provider_id=provider_id)
                before = snapshot(home)
                code, out, err = self.invoke(home)
                self.assertEqual(code, preflight.EXIT_BLOCKED, out + err)
                report = json.loads(out)
                self.assertIn(message, report["reason"])
                self.assertEqual(snapshot(home), before)

    def test_runtime_provider_and_model_mismatch_block(self):
        cases = [
            ("another_provider", "grok-4.7"),
            (PROVIDER_ID, "gpt-6-astra"),
        ]
        for observed_provider, observed_model in cases:
            with self.subTest(observed=(observed_provider, observed_model)), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                write_home(home)
                with patch.dict(os.environ, {"TEST_EXTERNAL_API_KEY": SECRET}):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = preflight.main([
                            "--codex-home", str(home),
                            "--observed-provider", observed_provider,
                            "--observed-model", observed_model,
                            "--evidence-source", "host_runtime_metadata",
                        ])
                self.assertEqual(code, preflight.EXIT_BLOCKED)
                self.assertEqual(json.loads(stdout.getvalue())["status"], "blocked")

    def test_provider_config_and_catalog_fail_closed(self):
        cases = [
            {"wire_api": "chat"},
            {"base_url": "http://provider.example.invalid/v1"},
            {"include_key": False},
            {"catalog": _catalog(missing={"step-5-preview"})},
            {"catalog": _catalog(wrong_effort="grok-4.7")},
            {"model": "gpt-6-astra"},
            {"static_token": SECRET},
        ]
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                write_home(home, **case)
                before = snapshot(home)
                code, out, err = self.invoke(home)
                self.assertEqual(code, preflight.EXIT_BLOCKED, out + err)
                self.assertNotIn(SECRET, out + err)
                self.assertEqual(snapshot(home), before)

    def test_profile_selection_is_respected(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            write_home(home)
            (home / "external.config.toml").write_text(
                'model = "grok-4.7"\nmodel_provider = "other_provider"\n', encoding="utf-8"
            )
            with patch.dict(os.environ, {"TEST_EXTERNAL_API_KEY": SECRET}):
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = preflight.main([
                        "--codex-home", str(home), "--profile", "external",
                        "--observed-provider", PROVIDER_ID, "--observed-model", "grok-4.7",
                        "--evidence-source", "host_runtime_metadata",
                    ])
            report = json.loads(stdout.getvalue())
            self.assertEqual(code, preflight.EXIT_BLOCKED)
            self.assertIn("active custom provider is not configured", report["reason"])

    def test_role_model_must_match_observed_child(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            write_home(home)
            with patch.dict(os.environ, {"TEST_EXTERNAL_API_KEY": SECRET}):
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = preflight.main([
                        "--codex-home", str(home), "--role", "researcher",
                        "--observed-provider", PROVIDER_ID, "--observed-model", "step-5-preview",
                        "--evidence-source", "host_runtime_metadata",
                    ])
            report = json.loads(stdout.getvalue())
            self.assertEqual(code, preflight.EXIT_READY, stdout.getvalue() + stderr.getvalue())
            self.assertEqual(report["requested"]["role"], "researcher")
            self.assertEqual(report["requested"]["model"], "step-5-preview")
            self.assertEqual(report["status"], "unverified")

    def test_every_role_uses_a_supported_registered_model(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            write_home(home)
            for role, (model, _effort) in preflight.MODEL_ROLES.items():
                with self.subTest(role=role), patch.dict(os.environ, {"TEST_EXTERNAL_API_KEY": SECRET}):
                    code, report = preflight.build_report(
                        codex_home=home,
                        profile=None,
                        role=role,
                        observed_provider=PROVIDER_ID,
                        observed_model=model,
                        evidence_source="host_runtime_metadata",
                        run_id=None,
                        session_id=None,
                    )
                    self.assertEqual(code, preflight.EXIT_READY)
                    self.assertEqual(report["requested"]["model"], model)
                    self.assertEqual(report["preflight_status"], "passed")

    def test_external_auth_command_is_accepted_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            write_home(home, include_key=False)
            config_path = home / "config.toml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8")
                + f'\n[model_providers."{PROVIDER_ID}".auth]\ncommand = "credential-helper"\n',
                encoding="utf-8",
            )
            before = snapshot(home)
            code, out, err = self.invoke(home)
            self.assertEqual(code, preflight.EXIT_READY, out + err)
            self.assertEqual(json.loads(out)["preflight_status"], "passed")
            self.assertNotIn(SECRET, out + err)
            self.assertEqual(snapshot(home), before)

    def test_static_auth_header_is_blocked_and_not_echoed(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            write_home(home)
            config_path = home / "config.toml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8")
                + f'http_headers = {{ Authorization = "Bearer {SECRET}" }}\n',
                encoding="utf-8",
            )
            before = snapshot(home)
            code, out, err = self.invoke(home)
            self.assertEqual(code, preflight.EXIT_BLOCKED)
            self.assertIn("static credentials", json.loads(out)["reason"])
            self.assertNotIn(SECRET, out + err)
            self.assertEqual(snapshot(home), before)

    def test_public_skill_metadata_and_routing_contract(self):
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
        self.assertIsNotNone(match)
        metadata = match.group(1)
        self.assertIn("\nname: codex-external-provider\n", "\n" + metadata + "\n")
        description = re.search(r"(?m)^description: (.+)$", metadata)
        self.assertIsNotNone(description)
        self.assertLessEqual(len(description.group(1)), 1024)
        self.assertNotIn("<", description.group(1))
        self.assertNotIn(">", description.group(1))
        interface = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn('display_name: "Codex External Provider"', interface)
        self.assertNotIn("allow_implicit_invocation: false", interface)
        self.assertIn("after two failed repair rounds", text.lower())
        self.assertIn("two writers active", text)
        example = tomllib.loads((SKILL_ROOT / "examples" / "user-config.example.toml").read_text(encoding="utf-8"))
        self.assertEqual(example["model_provider"], "your_external_provider")
        self.assertEqual(example["model_providers"]["your_external_provider"]["wire_api"], "responses")

    def test_runtime_identity_without_source_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            write_home(home)
            with patch.dict(os.environ, {"TEST_EXTERNAL_API_KEY": SECRET}):
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = preflight.main([
                        "--codex-home", str(home),
                        "--observed-provider", PROVIDER_ID,
                        "--observed-model", "grok-4.7",
                    ])
            report = json.loads(stdout.getvalue())
            self.assertEqual(code, preflight.EXIT_BLOCKED)
            self.assertIn("evidence source is unknown", report["reason"])


if __name__ == "__main__":
    unittest.main()
