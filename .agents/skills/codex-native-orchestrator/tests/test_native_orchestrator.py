import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import codex_native_orchestrator as controller  # noqa: E402


def make_plan(manifest, writer=True):
    task = {
        "task_id": "write-file" if writer else "inspect",
        "role": "worker" if writer else "explorer",
        "objective": "Make or inspect a bounded repository change",
        "owned_paths": ["src"] if writer else [],
    }
    if not writer:
        task["read_only"] = True
        task["writes"] = False
    return controller.validate_plan(
        {
            "schema_version": 1,
            "plan_id": "migration-resume",
            "objective": "Resume a persisted run",
            "tasks": [task],
        },
        manifest=manifest,
    )


class NativePackageTests(unittest.TestCase):
    def test_manifest_profiles_use_configured_efforts(self):
        manifest = controller.load_manifest(Path.cwd())
        self.assertEqual(manifest["package_id"], "codex-native-orchestrator")
        self.assertNotIn("root", manifest["roles"])
        self.assertNotIn("model", manifest["managed_config_keys"])
        self.assertNotIn("model_reasoning_effort", manifest["managed_config_keys"])
        for role in ("explorer", "worker"):
            self.assertEqual(manifest["roles"][role]["model"], "gpt-6-luna")
            self.assertEqual(manifest["roles"][role]["reasoning_effort"], "max")
        self.assertEqual(manifest["roles"]["tester"]["model"], "gpt-6-sol")
        self.assertEqual(manifest["roles"]["tester"]["reasoning_effort"], "xhigh")
        self.assertEqual(manifest["roles"]["researcher"]["model"], "gpt-6-astra")
        self.assertEqual(manifest["roles"]["researcher"]["reasoning_effort"], "medium")
        self.assertEqual(manifest["roles"]["reviewer"]["model"], "gpt-6-sol")
        self.assertEqual(manifest["roles"]["reviewer"]["reasoning_effort"], "xhigh")
        self.assertEqual(manifest["roles"]["guardian"]["model"], "gpt-6-astra")
        self.assertEqual(manifest["roles"]["guardian"]["reasoning_effort"], "medium")
        for profile in ("explorer", "worker", "tester", "researcher", "reviewer", "guardian"):
            text = (SCRIPT_DIR.parent / "roles" / (profile + ".toml")).read_text()
            self.assertIn('name = "%s"' % profile, text)
            self.assertIn('model_reasoning_effort = "%s"' % manifest["roles"][profile]["reasoning_effort"], text)

    def test_guardian_runtime_requires_astra_medium(self):
        requested = {
            "model": "gpt-6-astra",
            "reasoning_effort": "medium",
            "sandbox_mode": "read-only",
            "approval_policy": "never",
            "ephemeral": True,
            "temporary_codex_home": True,
            "external_user_mcp_and_plugins_loaded": False,
            "trusted_empty_cwd": True,
            "ignore_user_config": True,
            "ignore_rules": True,
            "skip_git_repo_check": True,
        }
        result = {
            "requested_runtime": requested,
            "observed_runtime": {"model": "gpt-6-astra", "reasoning_effort": "medium", "sandbox_mode": "read-only"},
        }
        controller.validate_guardian_runtime(result)
        result["observed_runtime"] = {"model": "invalid-model", "reasoning_effort": "invalid", "sandbox_mode": "read-only"}
        with self.assertRaises(controller.ValidationError):
            controller.validate_guardian_runtime(result)

    def test_run_store_report_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            codex_home = Path(temp) / "codex-home"
            repo = Path(temp) / "repo"
            repo.mkdir()
            orchestrator = controller.Orchestrator(repo, codex_home, runner=controller.CodexRunner(invoke=lambda **_: {}))
            plan = make_plan(orchestrator.manifest, writer=False)
            run_id = orchestrator.store.create(plan, {"head": "baseline"}, orchestrator.manifest, "existing-run")
            report = orchestrator.report(run_id)

            self.assertEqual(orchestrator.store.run_path(run_id).resolve(), (codex_home / "astra-orchestrator" / "runs" / run_id).resolve())
            self.assertEqual(report["mode"], plan["route"])
            self.assertEqual(report["run_id"], run_id)
            self.assertEqual(report["status"], "planned")
            self.assertEqual(report["requested"][0]["role"], "explorer")
            self.assertEqual(report["requested"][0]["model"], "gpt-6-luna")
            self.assertEqual(report["observed"][0]["provider"], "unknown")
            self.assertIn("changed_paths", report)
            self.assertIn("checks", report)
            self.assertIn("uncertainties", report)

    def test_stale_run_cannot_resume_with_a_different_topology(self):
        with tempfile.TemporaryDirectory() as temp:
            codex_home = Path(temp) / "codex-home"
            repo = Path(temp) / "repo"
            repo.mkdir()
            runner = controller.CodexRunner(invoke=lambda **_: self.fail("stale work must not be relaunched"))
            orchestrator = controller.Orchestrator(repo, codex_home, runner=runner)
            plan = make_plan(orchestrator.manifest, writer=False)
            run_id = orchestrator.store.create(plan, {"head": "baseline"}, orchestrator.manifest, "stale-topology-run")
            state, _ = orchestrator.store.load(run_id)
            state["manifest_hash"] = "0" * 64
            orchestrator.store.save_state(run_id, state)

            result = orchestrator.resume(run_id)
            self.assertEqual(result["state"], "needs_input")
            self.assertIn("manifest changed", result["error"])

    def test_manifest_rejects_modified_or_missing_role_profiles(self):
        bundled = SCRIPT_DIR.parent / "codex-native-orchestrator.json"
        raw = json.loads(bundled.read_text())
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.json"
            wrong = json.loads(json.dumps(raw))
            wrong["roles"]["reviewer"]["model"] = "invalid-model"
            with self.assertRaises(controller.ValidationError):
                controller._normalize_manifest(wrong, path)
            missing = json.loads(json.dumps(raw))
            del missing["roles"]["guardian"]
            with self.assertRaises(controller.ValidationError):
                controller._normalize_manifest(missing, path)
            wrong_config = json.loads(json.dumps(raw))
            wrong_config["config_values"]["model"] = "invalid-model"
            with self.assertRaises(controller.ValidationError):
                controller._normalize_manifest(wrong_config, path)
            for extra in (
                {"config": {"model": "gpt-6-astra"}},
                {"allowed_config_keys": [*raw["managed_config_keys"], "model"]},
                {"deployment": {**raw["deployment"], "values": {"model": "gpt-6-astra"}}},
            ):
                with self.subTest(extra=extra):
                    injected = json.loads(json.dumps(raw))
                    injected.update(extra)
                    with self.assertRaisesRegex(controller.ValidationError, "config aliases"):
                        controller._normalize_manifest(injected, path)

    def test_deployment_patch_preserves_selected_root_model(self):
        manifest = controller.load_manifest(Path.cwd())
        original = b'model = "gpt-6-sol"\nmodel_reasoning_effort = "high"\n[agents]\nenabled = false\n'
        patched, _ = controller.patch_toml_bytes(
            original, manifest["config_values"], allowed_keys=manifest["managed_config_keys"]
        )
        self.assertIn(b'model = "gpt-6-sol"\n', patched)
        self.assertIn(b'model_reasoning_effort = "high"\n', patched)
        self.assertIn(b'enabled = true\n', patched)
        self.assertNotIn(b'model = "gpt-6-astra"', patched)

    def test_role_scanner_ignores_model_text_inside_multiline_instructions(self):
        with tempfile.TemporaryDirectory() as temp:
            role_file = Path(temp) / "explorer.toml"
            role_file.write_text('developer_instructions = """\nmodel = "gpt-6-luna"\nmodel_reasoning_effort = "max"\nsandbox_mode = "read-only"\n"""\n')
            self.assertEqual(controller._agent_file_spec(role_file), {})
            role_file.write_text('model = "gpt-6-luna"\nmodel_reasoning_effort = "max"\nsandbox_mode = "read-only"\ndeveloper_instructions = """\nmodel = "wrong"\n"""\n')
            self.assertEqual(controller._agent_file_spec(role_file), {
                "model": "gpt-6-luna", "model_reasoning_effort": "max", "sandbox_mode": "read-only"
            })

    def test_final_guardian_plan_requires_independent_post_writer_tester(self):
        manifest = controller.load_manifest(Path.cwd())
        writer = {"task_id": "change", "role": "worker", "objective": "Change runtime behavior", "owned_paths": ["src"]}
        plan = {"plan_id": "verify-change", "objective": "Change and verify behavior", "gate_mode": "final", "tasks": [writer]}
        with self.assertRaisesRegex(controller.ValidationError, "read-only tester downstream"):
            controller.validate_plan(plan, manifest=manifest)
        ordinary = dict(plan, gate_mode="none")
        controller.validate_plan(ordinary, manifest=manifest)
        plan["tasks"].append({
            "task_id": "verify", "role": "tester", "objective": "Verify changed behavior",
            "depends_on": ["change"], "read_only": True, "writes": False,
        })
        normalized = controller.validate_plan(plan, manifest=manifest)
        self.assertEqual(normalized["topological_order"], ["change", "verify"])

    def test_installed_root_choice_is_allowed_but_role_drift_blocks_dispatch(self):
        manifest = controller.load_manifest(Path.cwd())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            repo = root / "repo"
            repo.mkdir()
            codex_home = root / "codex-home"
            agents = codex_home / "agents"
            agents.mkdir(parents=True)
            for role in ("explorer", "worker", "tester", "researcher", "reviewer", "guardian"):
                (agents / (role + ".toml")).write_bytes((SCRIPT_DIR.parent / "roles" / (role + ".toml")).read_bytes())
            values = manifest["config_values"]
            config = 'model = "gpt-6-sol"\nmodel_reasoning_effort = "medium"\n[agents]\n'
            config += '\n'.join('%s = %s' % (key.removeprefix("agents."), json.dumps(value)) for key, value in values.items() if key.startswith("agents."))
            config_path = codex_home / "config.toml"
            config_path.write_text(config + '\n')
            orchestrator = controller.Orchestrator(repo, codex_home)
            with mock.patch.object(controller, "validate_codex_config_bytes", return_value={"ok": True}):
                orchestrator._require_installed_topology()
                home_skill = root / ".agents" / "skills" / "codex-native-orchestrator" / "SKILL.md"
                home_skill.parent.mkdir(parents=True)
                home_skill.write_text("global alias")
                with mock.patch.object(Path, "home", return_value=root):
                    orchestrator._require_installed_topology()
                home_skill.unlink()
                project_config = repo / ".codex" / "config.toml"
                project_config.parent.mkdir()
                project_config.write_text('model = "gpt-6-luna"\n')
                orchestrator._require_installed_topology()
                project_config.write_text('[agents]\ndefault_subagent_model = "gpt-6-sol"\n')
                with self.assertRaisesRegex(controller.ValidationError, "project_overrides"):
                    orchestrator._require_installed_topology()
                project_config.unlink()
                config_path.write_text((config + '\n').replace('model = "gpt-6-sol"', 'model = "gpt-6-luna"'))
                orchestrator._require_installed_topology()
                explorer_path = agents / "explorer.toml"
                explorer_path.write_text(explorer_path.read_text().replace('model = "gpt-6-luna"', 'model = "gpt-6-sol"'))
                with self.assertRaisesRegex(controller.ValidationError, "roles"):
                    orchestrator._require_installed_topology()

    def test_non_guardian_review_packet_allows_unneeded_tester(self):
        manifest = controller.load_manifest(Path.cwd())
        plan = controller.validate_plan({
            "plan_id": "docs-change", "objective": "Edit documentation", "gate_mode": "none",
            "tasks": [{"task_id": "edit", "role": "worker", "objective": "Edit documentation", "owned_paths": ["docs"]}],
        }, manifest=manifest)
        packet = controller.build_gate_packet(plan, {"test_evidence": "not applicable"}, mode="final")
        self.assertEqual(packet["Test evidence"], "not applicable")

    def test_runner_rejects_writable_tester(self):
        manifest = controller.load_manifest(Path.cwd())
        plan = {
            "plan_id": "writable-test", "objective": "Verify behavior",
            "tasks": [{"task_id": "test", "role": "tester", "objective": "Run tests that write output", "owned_paths": ["tmp"], "read_only": False, "writes": True}],
        }
        with self.assertRaisesRegex(controller.ValidationError, "use native dispatch"):
            controller.validate_plan(plan, manifest=manifest)

    def test_known_observed_model_mismatch_is_rejected(self):
        runner = controller.CodexRunner(invoke=lambda **_: {"observed_runtime": {"model": "invalid-model"}})
        with self.assertRaisesRegex(controller.ValidationError, "fixed value"):
            runner.invoke("review", role="reviewer", read_only=True, cwd=Path.cwd(), max_attempts=1)

    def test_unavailable_guardian_does_not_substitute_reviewer(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = controller.CodexRunner(invoke=lambda **_: self.fail("unexpected substitute role"))
            orchestrator = controller.Orchestrator(Path(temp), Path(temp) / "codex-home", runner=runner)
            state = {"run_id": "run-test", "state": "needs_input", "gate": {"status": "unavailable"}}
            with mock.patch.object(orchestrator.store, "save_state"):
                self.assertFalse(orchestrator._consume_guardian(state, {"gate_mode": "final"}))
            self.assertIn("explicit waiver", state["error"])

    def test_codex_events_report_provider_when_exposed(self):
        events = "\n".join(
            json.dumps(item)
            for item in (
                {"type": "turn.started", "provider": "openai-compatible", "model": "gpt-6-luna", "reasoning_effort": "max"},
                {"type": "turn.completed", "output": "done"},
            )
        )
        parsed = controller.parse_codex_events(events)
        self.assertEqual(parsed["observed_runtime"]["provider"], "openai-compatible")
        self.assertEqual(parsed["observed_runtime"]["model"], "gpt-6-luna")


if __name__ == "__main__":
    unittest.main()
