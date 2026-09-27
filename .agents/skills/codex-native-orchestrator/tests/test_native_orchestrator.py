import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import codex_native_orchestrator as controller  # noqa: E402


LEGACY_ROLES = {
    "root": {"model": "gpt-6-astra", "reasoning_effort": "medium", "sandbox_mode": "workspace-write"},
    "explorer": {"model": "gpt-6-luna", "reasoning_effort": "max", "sandbox_mode": "read-only"},
    "worker": {"model": "gpt-6-luna", "reasoning_effort": "max", "sandbox_mode": "workspace-write"},
    "tester": {"model": "gpt-6-luna", "reasoning_effort": "max", "sandbox_mode": "workspace-write"},
    "reviewer": {"model": "gpt-6-sol", "reasoning_effort": "xhigh", "sandbox_mode": "read-only"},
    "researcher": {"model": "gpt-6-luna", "reasoning_effort": "max", "sandbox_mode": "read-only"},
    "guardian": {"model": "gpt-6-sol", "reasoning_effort": "xhigh", "sandbox_mode": "read-only", "automatic": False, "requires_explicit_request": True},
}


def legacy_manifest():
    return {
        "schema_version": 1,
        "package_version": "2.4.2",
        "package_id": "astra-orchestrator",
        "skill": {"entry": ".agents/skills/astra-orchestrator/SKILL.md", "references": []},
        "roles": LEGACY_ROLES,
        "max_concurrency": 4,
        "runner_defaults": {
            "mode": "hybrid",
            "decision": "auto",
            "max_agents": 4,
            "triggers": [
                "explicit_dag_batch_resume_persistence_request",
                "three_or_more_dependent_nodes",
                "two_or_more_parallel_writers",
                "cross_turn_recovery",
            ],
            "multi_file_is_trigger": False,
            "overrides": ["runner on", "runner off", "root-only", "max agents N", "read-only"],
        },
        "paths": {
            "run_state_root": "~/.codex/astra-orchestrator/runs",
            "user_config": "~/.codex/config.toml",
            "user_agents_dir": "~/.codex/agents",
            "user_skill_dir": "~/.agents/skills/astra-orchestrator",
        },
        "timeouts": {"read_only": 1800, "writer": 3600, "guardian": 900},
        "managed_config_keys": [
            "agents.enabled",
            "agents.max_concurrent_threads_per_session",
            "agents.default_subagent_model",
            "agents.default_subagent_reasoning_effort",
        ],
        "installation_scope": "global",
        "config_values": {
            "agents.enabled": True,
            "agents.max_concurrent_threads_per_session": 4,
            "agents.default_subagent_model": "gpt-6-luna",
            "agents.default_subagent_reasoning_effort": "max",
        },
    }


def legacy_controller_manifest(raw, path):
    """Reproduce manifest expansion from the pre-migration controller."""
    defaults = {
        "root": {"model": "gpt-6-astra", "reasoning_effort": "medium", "read_only": False},
        "explorer": {"model": "gpt-6-luna", "reasoning_effort": "max", "read_only": True},
        "worker": {"model": "gpt-6-luna", "reasoning_effort": "max", "read_only": False},
        "tester": {"model": "gpt-6-luna", "reasoning_effort": "max", "read_only": False},
        "researcher": {"model": "gpt-6-luna", "reasoning_effort": "max", "read_only": True},
        "reviewer": {"model": "gpt-6-sol", "reasoning_effort": "xhigh", "read_only": True},
        "guardian": {"model": "gpt-6-sol", "reasoning_effort": "xhigh", "read_only": True},
    }
    roles = json.loads(json.dumps(defaults))
    for name, spec in raw.get("roles", {}).items():
        roles[name].update(spec)
    result = dict(raw)
    result["roles"] = roles
    result["max_concurrency"] = raw.get("max_concurrency", 4)
    result["manifest_path"] = str(path)
    return result


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
    def test_manifest_profiles_use_current_codex_effort_starts(self):
        manifest = controller.load_manifest(Path.cwd())
        self.assertEqual(manifest["package_id"], "codex-native-orchestrator")
        self.assertEqual(manifest["roles"]["root"]["reasoning_effort"], "low")
        for role in ("explorer", "worker", "tester", "researcher"):
            self.assertEqual(manifest["roles"][role]["reasoning_effort"], "high")
        self.assertEqual(manifest["roles"]["reviewer"]["reasoning_effort"], "medium")
        self.assertEqual(manifest["roles"]["guardian"]["reasoning_effort"], "xhigh")
        for profile in ("explorer", "worker", "tester", "researcher", "reviewer", "guardian"):
            text = (SCRIPT_DIR.parent / "roles" / (profile + ".toml")).read_text()
            self.assertIn('name = "%s"' % profile, text)
            self.assertIn('model_reasoning_effort = "%s"' % manifest["roles"][profile]["reasoning_effort"], text)

    def test_run_store_keeps_legacy_state_namespace_and_report_contract(self):
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

    def test_existing_run_resumes_with_matching_legacy_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            codex_home = home / "codex-home"
            repo = home / "repo"
            repo.mkdir()
            legacy_path = home / controller.LEGACY_MANIFEST_RELATIVE
            legacy_path.parent.mkdir(parents=True)
            raw_legacy = legacy_manifest()
            legacy_path.write_text(json.dumps(raw_legacy), encoding="utf-8")
            old_manifest = controller._normalize_manifest(raw_legacy, legacy_path)
            old_plan = make_plan(old_manifest, writer=True)
            store = controller.RunStore(codex_home)
            run_id = store.create(old_plan, {"head": "baseline"}, old_manifest, "existing-incomplete-run")
            state, _ = store.load(run_id)
            state["state"] = "needs_input"
            state["status"] = "needs_input"
            state["tasks"]["write-file"]["state"] = "needs_input"
            state["tasks"]["write-file"]["status"] = "needs_input"
            state["tasks"]["write-file"]["attempts"] = 1
            store.save_state(run_id, state)

            runner = controller.CodexRunner(invoke=lambda **_: self.fail("resume must not duplicate an interrupted writer"))
            orchestrator = controller.Orchestrator(repo, codex_home, runner=runner)
            with mock.patch.object(Path, "home", return_value=home):
                result = orchestrator.resume(run_id)

            self.assertEqual(orchestrator.manifest["package_id"], "astra-orchestrator")
            self.assertEqual(result["run_id"], run_id)
            self.assertEqual(result["state"], "needs_input")
            self.assertTrue(result["needs_input"])
            self.assertEqual(orchestrator.store.run_path(run_id).resolve(), (codex_home / "astra-orchestrator" / "runs" / run_id).resolve())

    def test_partial_legacy_role_manifest_uses_frozen_defaults_for_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            codex_home = home / "codex-home"
            repo = home / "repo"
            repo.mkdir()
            legacy_path = home / controller.LEGACY_MANIFEST_RELATIVE
            legacy_path.parent.mkdir(parents=True)
            raw_legacy = legacy_manifest()
            raw_legacy["roles"] = {"worker": {"sandbox_mode": "workspace-write"}}
            legacy_path.write_text(json.dumps(raw_legacy), encoding="utf-8")
            old_manifest = legacy_controller_manifest(raw_legacy, legacy_path)
            old_plan = make_plan(old_manifest, writer=True)
            store = controller.RunStore(codex_home)
            run_id = store.create(old_plan, {"head": "baseline"}, old_manifest, "partial-legacy-run")
            state, _ = store.load(run_id)
            state["state"] = "needs_input"
            state["status"] = "needs_input"
            state["tasks"]["write-file"]["state"] = "needs_input"
            state["tasks"]["write-file"]["status"] = "needs_input"
            state["tasks"]["write-file"]["attempts"] = 1
            store.save_state(run_id, state)

            runner = controller.CodexRunner(invoke=lambda **_: self.fail("resume must not duplicate an interrupted writer"))
            orchestrator = controller.Orchestrator(repo, codex_home, runner=runner)
            with mock.patch.object(Path, "home", return_value=home):
                result = orchestrator.resume(run_id)

            self.assertEqual(result["state"], "needs_input")
            self.assertEqual(orchestrator.manifest["roles"]["worker"]["reasoning_effort"], "max")
            self.assertEqual(orchestrator.manifest["roles"]["reviewer"]["reasoning_effort"], "xhigh")

    def test_run_with_unmatched_legacy_manifest_stops_as_stale(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            codex_home = home / "codex-home"
            repo = home / "repo"
            repo.mkdir()
            legacy_path = home / controller.LEGACY_MANIFEST_RELATIVE
            legacy_path.parent.mkdir(parents=True)
            raw_legacy = legacy_manifest()
            legacy_path.write_text(json.dumps(raw_legacy), encoding="utf-8")
            old_manifest = controller._normalize_manifest(raw_legacy, legacy_path)
            old_plan = make_plan(old_manifest, writer=True)
            store = controller.RunStore(codex_home)
            run_id = store.create(old_plan, {"head": "baseline"}, old_manifest, "changed-legacy-run")
            state, _ = store.load(run_id)
            state["state"] = "needs_input"
            state["status"] = "needs_input"
            state["tasks"]["write-file"]["state"] = "needs_input"
            state["tasks"]["write-file"]["status"] = "needs_input"
            store.save_state(run_id, state)
            raw_legacy["package_version"] = "different-contract"
            legacy_path.write_text(json.dumps(raw_legacy), encoding="utf-8")

            runner = controller.CodexRunner(invoke=lambda **_: self.fail("stale work must not be relaunched"))
            orchestrator = controller.Orchestrator(repo, codex_home, runner=runner)
            with mock.patch.object(Path, "home", return_value=home):
                result = orchestrator.resume(run_id)

            self.assertEqual(result["state"], "needs_input")
            self.assertIn("manifest changed", result["error"])

    def test_codex_events_report_provider_when_exposed(self):
        events = "\n".join(
            json.dumps(item)
            for item in (
                {"type": "turn.started", "provider": "openai-compatible", "model": "gpt-6-luna", "reasoning_effort": "high"},
                {"type": "turn.completed", "output": "done"},
            )
        )
        parsed = controller.parse_codex_events(events)
        self.assertEqual(parsed["observed_runtime"]["provider"], "openai-compatible")
        self.assertEqual(parsed["observed_runtime"]["model"], "gpt-6-luna")


if __name__ == "__main__":
    unittest.main()
