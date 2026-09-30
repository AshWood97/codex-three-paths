import json
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import codex_native_orchestrator as controller  # noqa: E402


def planning_response():
    return {"final": {"outcome": "ready", "summary": "Bounded plan", "plan_steps": ["Inspect, implement, verify, review and gate"], "residual_risks": []}}


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
    tasks = [task]
    if writer:
        tasks.append({
            "task_id": "verify", "role": "tester", "objective": "Verify the bounded change",
            "depends_on": [task["task_id"]], "read_only": True, "writes": False,
        })
    return controller.validate_plan(
        {
            "schema_version": 1,
            "plan_id": "migration-resume",
            "objective": "Resume a persisted run",
            "tasks": tasks,
        },
        manifest=manifest,
    )


class NativePackageTests(unittest.TestCase):
    def make_local_repo(self, root):
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run([
            "git", "-c", "user.name=Test", "-c", "user.email=test@local",
            "commit", "--allow-empty", "-qm", "baseline",
        ], cwd=repo, check=True)
        return repo

    def test_conductor_fixed_read_only_role_is_excluded_from_dag(self):
        manifest = controller.load_manifest(Path.cwd())
        self.assertEqual(set(manifest["roles"]), {
            "conductor", "explorer", "worker", "tester", "researcher", "reviewer", "guardian",
        })
        self.assertEqual(manifest["roles"]["conductor"]["sandbox_mode"], "read-only")
        self.assertTrue(manifest["roles"]["conductor"]["automatic"])
        self.assertFalse(manifest["roles"]["conductor"]["requires_explicit_request"])
        with self.assertRaises(controller.ValidationError):
            controller.validate_plan({
                "plan_id": "illegal-conductor", "objective": "Plan work",
                "tasks": [{"task_id": "planning", "role": "conductor", "objective": "Plan work"}],
            }, manifest=manifest)
        runner = controller.CodexRunner(invoke=lambda **_: self.fail("writable Conductor must not launch"))
        with self.assertRaisesRegex(controller.ValidationError, "read-only"):
            runner.invoke("Plan work", role="conductor", read_only=False, cwd=Path.cwd())
        for field, bad_value in (("model", "gpt-6-luna"), ("reasoning_effort", "high")):
            with self.subTest(field=field):
                runner = controller.CodexRunner(
                    role_specs={"conductor": {field: bad_value}},
                    invoke=lambda **_: self.fail("drifted Conductor must not launch"),
                )
                with self.assertRaisesRegex(controller.ValidationError, "fixed"):
                    runner.invoke("Plan work", role="conductor", read_only=True, cwd=Path.cwd())

    def test_native_begin_plans_before_work_and_persists_bound_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_local_repo(root)
            runner = controller.CodexRunner(invoke=lambda **_: {})
            orchestrator = controller.Orchestrator(repo, root / "codex-home", runner)

            def invoke(**kwargs):
                self.assertEqual(kwargs["role"], "conductor")
                self.assertTrue(kwargs["read_only"])
                self.assertEqual(kwargs["output_schema"], controller.SCHEMA_DIR / "planning.schema.json")
                self.assertEqual(kwargs["max_attempts"], 1)
                packet = json.loads(kwargs["prompt"])
                self.assertEqual(packet["mode"], "native")
                self.assertEqual(packet["objective"], "Inspect code")
                self.assertEqual(packet["baseline"]["head"], controller.repo_snapshot(repo)["head"])
                state, _ = orchestrator.store.load("native-startup")
                self.assertEqual(state["conductor"]["status"], "running")
                self.assertTrue(all(task["state"] == "pending" for task in state["tasks"].values()))
                self.assertEqual(state["conductor"]["requested_runtime"], {
                    "model": "gpt-6.1-sol", "reasoning_effort": "max", "sandbox_mode": "read-only",
                })
                return planning_response()

            with mock.patch.object(orchestrator, "_require_installed_topology"), \
                 mock.patch.object(runner, "invoke", side_effect=invoke) as launch:
                result = orchestrator.native_begin("native-startup", "Inspect code")
                self.assertTrue(result["ok"])
                state, plan = orchestrator.store.load("native-startup")
                self.assertTrue(orchestrator._conductor_ready(state))
                self.assertTrue(orchestrator._ensure_conductor(state, plan))
                launch.assert_called_once()
            record = state["conductor"]
            self.assertEqual(record["plan_hash"], state["plan_hash"])
            self.assertEqual(record["manifest_hash"], state["manifest_hash"])
            run_path = orchestrator.store.run_path("native-startup")
            self.assertEqual(record["packet_hash"], controller.sha256_json(controller.read_json(run_path / "inputs" / "conductor.json")))
            self.assertEqual(controller.read_json(run_path / "results" / "conductor.json"), record)
            self.assertEqual(record["observed_runtime"], {})
            events = controller.EventJournal(run_path / "events.jsonl", "native-startup").read()
            self.assertEqual([item["type"] for item in events], ["run_created", "conductor_started", "conductor_completed"])

    def test_conductor_cache_requires_plan_manifest_and_packet_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            orchestrator = controller.Orchestrator(root, root / "codex-home")
            plan = make_plan(orchestrator.manifest, writer=False)
            run_id = orchestrator.store.create(plan, {"head": "baseline"}, orchestrator.manifest)
            state, plan = orchestrator.store.load(run_id)
            with mock.patch.object(orchestrator.runner, "invoke", return_value=planning_response()) as launch:
                self.assertTrue(orchestrator._ensure_conductor(state, plan))
                self.assertTrue(orchestrator._ensure_conductor(state, plan))
                launch.assert_called_once()
                for field in ("plan_hash", "manifest_hash", "packet_hash"):
                    original = state["conductor"][field]
                    state["conductor"][field] = "0" * 64
                    self.assertFalse(orchestrator._conductor_ready(state), field)
                    state["conductor"][field] = original
                packet_path = orchestrator.store.run_path(run_id) / "inputs" / "conductor.json"
                packet = controller.read_json(packet_path)
                packet["objective"] = "Unapproved objective"
                controller.atomic_write_json(packet_path, packet)
                self.assertFalse(orchestrator._conductor_ready(state))
                self.assertTrue(orchestrator._ensure_conductor(state, plan))
                self.assertEqual(launch.call_count, 2)
                self.assertEqual(controller.read_json(packet_path)["objective"], plan["objective"])
                packet_path.unlink()
                self.assertFalse(orchestrator._conductor_ready(state))

    def test_runner_plans_before_first_task_and_passes_conductor_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_local_repo(root)
            orchestrator = controller.Orchestrator(repo, root / "codex-home")
            plan = make_plan(orchestrator.manifest, writer=False)
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan))
            calls = []

            def invoke(**kwargs):
                calls.append(kwargs["role"])
                packet = json.loads(kwargs["prompt"])
                state, _ = orchestrator.store.load("runner-startup")
                if kwargs["role"] == "conductor":
                    self.assertEqual(packet["mode"], "runner")
                    self.assertEqual(packet["tasks"], plan["tasks"])
                    self.assertEqual(state["tasks"]["inspect"]["attempts"], 0)
                    self.assertFalse(state["tasks"]["inspect"]["launch_released"])
                    return planning_response()
                self.assertEqual(kwargs["role"], "explorer")
                self.assertTrue(orchestrator._conductor_ready(state))
                self.assertEqual(packet["conductor_plan"], planning_response()["final"])
                result = dict(packet["result_contract"], outcome="needs_input", summary="Need scope clarification", commit_sha=None)
                result["test_evidence"] = []
                result["requested"] = {"model": "gpt-6-luna", "effort": "max", "sandbox": "read-only"}
                result["observed"] = {"model": "unknown", "effort": "unknown", "sandbox": "unknown"}
                return {"final": result}

            with mock.patch.object(orchestrator, "_require_installed_topology"), \
                 mock.patch.object(orchestrator.runner, "invoke", side_effect=invoke):
                result = orchestrator.run(plan_path, run_id="runner-startup")
            self.assertEqual(calls, ["conductor", "explorer"])
            self.assertEqual(result["state"], "needs_input")
            state, _ = orchestrator.store.load("runner-startup")
            self.assertEqual(state["tasks"]["inspect"]["attempts"], 1)
            events = controller.EventJournal(orchestrator.store.run_path("runner-startup") / "events.jsonl", "runner-startup").read()
            event_names = [item["type"] for item in events]
            self.assertLess(event_names.index("conductor_completed"), event_names.index("task_started"))

    def test_unsuccessful_conductor_never_launches_runner_work(self):
        ready = planning_response()["final"]
        responses = {
            "missing-fields": {"final": {"outcome": "ready"}},
            "extra-fields": {"final": dict(ready, extra="untrusted")},
            "empty-steps": {"final": dict(ready, plan_steps=[])},
            "invalid-steps": {"final": dict(ready, plan_steps=[1])},
            "blank-summary": {"final": dict(ready, summary=" ")},
            "list-outcome": {"final": dict(ready, outcome=[])},
            "object-outcome": {"final": dict(ready, outcome={})},
            "needs-input": {"final": dict(ready, outcome="needs_input", plan_steps=[])},
            "failed": {"final": dict(ready, outcome="failed", plan_steps=[])},
            "unavailable": controller.ControllerError("Conductor is unavailable"),
        }
        for name, response in responses.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                repo = self.make_local_repo(root)
                orchestrator = controller.Orchestrator(repo, root / "codex-home")
                plan_path = root / "plan.json"
                plan_path.write_text(json.dumps(make_plan(orchestrator.manifest)))

                def invoke(**kwargs):
                    self.assertEqual(kwargs["role"], "conductor", "work must not start after failed planning")
                    if isinstance(response, Exception):
                        raise response
                    return response

                with mock.patch.object(orchestrator, "_require_installed_topology"), \
                     mock.patch.object(orchestrator.runner, "invoke", side_effect=invoke) as launch:
                    result = orchestrator.run(plan_path, run_id="blocked-planning")
                    launch.assert_called_once()
                self.assertEqual(result["state"], "needs_input")
                self.assertIsNone(result["gate"])
                state, _ = orchestrator.store.load("blocked-planning")
                self.assertFalse(orchestrator._conductor_ready(state))
                for task in state["tasks"].values():
                    self.assertEqual(task["state"], "pending")
                    self.assertEqual(task["attempts"], 0)
                    self.assertFalse(task["launch_released"])
                    self.assertIsNone(task["worktree"])
                self.assertEqual(list((orchestrator.store.run_path("blocked-planning") / "worktrees").iterdir()), [])

    def test_bare_ready_status_cannot_authorize_native_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_local_repo(root)
            orchestrator = controller.Orchestrator(repo, root / "codex-home")
            receipt_path = root / "completion.json"
            receipt_path.write_text(json.dumps({"objective": "Inspect code", "changes": False, "test_evidence": []}))
            with mock.patch.object(orchestrator, "_require_installed_topology"), \
                 mock.patch.object(orchestrator.runner, "invoke", return_value=planning_response()) as launch:
                orchestrator.native_begin("bare-ready", "Inspect code")
                state, _ = orchestrator.store.load("bare-ready")
                state["conductor"] = {"status": "ready"}
                orchestrator.store.save_state("bare-ready", state)
                with self.assertRaisesRegex(controller.ControllerError, "Conductor planning must complete"):
                    orchestrator.native_gate(receipt_path, "bare-ready")
                launch.assert_called_once()

    def test_failed_native_planning_cannot_resume_as_runner_dag(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_local_repo(root)
            orchestrator = controller.Orchestrator(repo, root / "codex-home")
            with mock.patch.object(orchestrator, "_require_installed_topology"), \
                 mock.patch.object(orchestrator.runner, "invoke", side_effect=controller.ControllerError("Planning unavailable")) as launch:
                result = orchestrator.native_begin("native-no-receipt", "Inspect code")
                self.assertFalse(result["ok"])
                state, _ = orchestrator.store.load("native-no-receipt")
                self.assertEqual(state["state"], "needs_input")
                self.assertTrue(state["repo"]["native_completion"])
                self.assertNotIn("native_completion_hash", state)
                with self.assertRaisesRegex(controller.ControllerError, "native runs cannot resume as a DAG"):
                    orchestrator.resume("native-no-receipt")
                launch.assert_called_once()
                self.assertEqual(launch.call_args.kwargs["role"], "conductor")
            after, _ = orchestrator.store.load("native-no-receipt")
            self.assertEqual(after, state)
            self.assertEqual(after["tasks"]["native-review"]["attempts"], 0)
            self.assertIsNone(after["review"])
            self.assertIsNone(after["gate"])

    def test_native_begin_retries_failed_planning_and_reuses_ready_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_local_repo(root)
            orchestrator = controller.Orchestrator(repo, root / "codex-home")
            blocked = {"final": dict(planning_response()["final"], outcome="needs_input", plan_steps=[])}
            with mock.patch.object(orchestrator, "_require_installed_topology"), \
                 mock.patch.object(orchestrator.runner, "invoke", side_effect=[blocked, planning_response()]) as launch:
                first = orchestrator.native_begin("native-planning-retry", "Inspect code")
                self.assertFalse(first["ok"])
                second = orchestrator.native_begin("native-planning-retry", "Inspect code")
                self.assertTrue(second["ok"])
                state, _ = orchestrator.store.load("native-planning-retry")
                self.assertEqual(state["state"], "running")
                self.assertIsNone(state["error"])
                self.assertTrue(orchestrator._conductor_ready(state))
                self.assertEqual(state["tasks"]["native-review"]["attempts"], 0)
                third = orchestrator.native_begin("native-planning-retry", "Inspect code")
                self.assertTrue(third["ok"])
                self.assertEqual(second["conductor"], third["conductor"])
                self.assertEqual(launch.call_count, 2)
                self.assertEqual([call.kwargs["role"] for call in launch.call_args_list], ["conductor", "conductor"])
            events = controller.EventJournal(orchestrator.store.run_path("native-planning-retry") / "events.jsonl", "native-planning-retry").read()
            event_names = [item["type"] for item in events]
            self.assertEqual(event_names.count("conductor_started"), 2)
            self.assertNotIn("task_started", event_names)
            self.assertNotIn("review_started", event_names)

    def test_native_begin_retry_clears_stale_error_and_preserves_failed_attempt_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_local_repo(root)
            orchestrator = controller.Orchestrator(repo, root / "codex-home")
            calls = []

            def invoke(**kwargs):
                self.assertEqual(kwargs["role"], "conductor")
                calls.append(kwargs["role"])
                state, _ = orchestrator.store.load("native-preserved-attempt")
                if len(calls) == 1:
                    error = controller.ControllerError("proxy unavailable")
                    error.evidence_paths = {"stdout": "/private/failed/stdout.jsonl", "stderr": "/private/failed/stderr.log"}
                    raise error
                self.assertEqual(state["state"], "running")
                self.assertIsNone(state["error"])
                self.assertEqual(state["conductor"]["status"], "running")
                self.assertEqual(state["tasks"]["native-review"]["attempts"], 0)
                self.assertEqual(state["conductor"]["requested_runtime"], {
                    "model": "gpt-6.1-sol", "reasoning_effort": "max", "sandbox_mode": "read-only",
                })
                return planning_response()

            with mock.patch.object(orchestrator, "_require_installed_topology"), \
                 mock.patch.object(orchestrator.runner, "invoke", side_effect=invoke):
                first = orchestrator.native_begin("native-preserved-attempt", "Inspect code")
                self.assertFalse(first["ok"])
                blocked, _ = orchestrator.store.load("native-preserved-attempt")
                baseline = dict(blocked["repo"])
                self.assertEqual(blocked["state"], "needs_input")
                self.assertEqual(blocked["conductor"]["status"], "unavailable")
                self.assertIn("evidence_paths", blocked["conductor"])
                second = orchestrator.native_begin("native-preserved-attempt", "Inspect code")
                self.assertTrue(second["ok"])
                ready, _ = orchestrator.store.load("native-preserved-attempt")

            self.assertEqual(ready["repo"], baseline)
            self.assertEqual(ready["manifest_hash"], blocked["manifest_hash"])
            self.assertTrue(orchestrator._manifest_matches_run(ready))
            events = controller.EventJournal(orchestrator.store.run_path("native-preserved-attempt") / "events.jsonl", "native-preserved-attempt").read()
            completed = [item["payload"] for item in events if item["type"] == "conductor_completed"]
            self.assertEqual([item["status"] for item in completed], ["unavailable", "ready"])
            self.assertEqual(completed[0]["evidence_paths"], blocked["conductor"]["evidence_paths"])
            self.assertEqual(calls, ["conductor", "conductor"])

    def test_native_begin_retry_rejects_changed_identity_or_started_work(self):
        for mutation in ("objective", "topology", "baseline", "dirty-baseline", "completion", "task-started", "task-attempted", "task-released", "non-native"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                repo = self.make_local_repo(root)
                orchestrator = controller.Orchestrator(repo, root / "codex-home")
                blocked = {"final": dict(planning_response()["final"], outcome="needs_input", plan_steps=[])}
                with mock.patch.object(orchestrator, "_require_installed_topology"), \
                     mock.patch.object(orchestrator.runner, "invoke", return_value=blocked) as launch:
                    orchestrator.native_begin("native-invalid-retry", "Inspect code")
                    state, _ = orchestrator.store.load("native-invalid-retry")
                    objective = "Inspect code"
                    if mutation == "objective":
                        objective = "Change code"
                    elif mutation == "topology":
                        state["manifest_hash"] = "0" * 64
                    elif mutation == "baseline":
                        subprocess.run([
                            "git", "-c", "user.name=Test", "-c", "user.email=test@local",
                            "commit", "--allow-empty", "-qm", "work already started",
                        ], cwd=repo, check=True)
                    elif mutation == "dirty-baseline":
                        (repo / "started.txt").write_text("Host work has started\n")
                    elif mutation == "completion":
                        state["native_completion_hash"] = "1" * 64
                    elif mutation == "task-started":
                        state["tasks"]["native-review"].update(state="running", status="running")
                    elif mutation == "task-attempted":
                        state["tasks"]["native-review"]["attempts"] = 1
                    elif mutation == "task-released":
                        state["tasks"]["native-review"]["launch_released"] = True
                    elif mutation == "non-native":
                        state["repo"].pop("native_completion")
                    orchestrator.store.save_state("native-invalid-retry", state)
                    with self.assertRaises(controller.ControllerError):
                        orchestrator.native_begin("native-invalid-retry", objective)
                    launch.assert_called_once()
                after, _ = orchestrator.store.load("native-invalid-retry")
                self.assertEqual(after["conductor"], state["conductor"])

    def test_native_begin_repository_drift_during_conductor_blocks_host_work(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.make_local_repo(root)
            orchestrator = controller.Orchestrator(repo, root / "codex-home")

            def invoke(**kwargs):
                self.assertEqual(kwargs["role"], "conductor")
                (repo / "concurrent.txt").write_text("Repository changed during planning\n")
                return planning_response()

            with mock.patch.object(orchestrator, "_require_installed_topology"), \
                 mock.patch.object(orchestrator.runner, "invoke", side_effect=invoke) as launch:
                with self.assertRaisesRegex(controller.RepoSafetyError, "baseline changed during Conductor"):
                    orchestrator.native_begin("native-planning-drift", "Inspect code")
                receipt_path = root / "completion.json"
                receipt_path.write_text(json.dumps({"objective": "Inspect code", "changes": False, "test_evidence": []}))
                with self.assertRaisesRegex(controller.ControllerError, "Conductor planning must complete"):
                    orchestrator.native_gate(receipt_path, "native-planning-drift")
                launch.assert_called_once()
            state, _ = orchestrator.store.load("native-planning-drift")
            self.assertEqual(state["state"], "needs_input")
            self.assertIn("baseline changed during Conductor", state["error"])
            self.assertEqual(state["conductor"]["status"], "stale")
            self.assertFalse(orchestrator._conductor_ready(state))
            self.assertEqual(state["tasks"]["native-review"]["attempts"], 0)
            self.assertIsNone(state["review"])
            self.assertIsNone(state["gate"])

    def test_manifest_profiles_use_configured_efforts(self):
        manifest = controller.load_manifest(Path.cwd())
        self.assertEqual(manifest["package_id"], "codex-native-orchestrator")
        self.assertNotIn("root", manifest["roles"])
        self.assertEqual(manifest["roles"]["conductor"]["model"], "gpt-6.1-sol")
        self.assertEqual(manifest["roles"]["conductor"]["reasoning_effort"], "max")
        self.assertNotIn("model", manifest["managed_config_keys"])
        self.assertNotIn("model_reasoning_effort", manifest["managed_config_keys"])
        for role in ("explorer", "worker"):
            self.assertEqual(manifest["roles"][role]["model"], "gpt-6-luna")
            self.assertEqual(manifest["roles"][role]["reasoning_effort"], "max")
        self.assertEqual(manifest["roles"]["tester"]["model"], "gpt-6.1-sol")
        self.assertEqual(manifest["roles"]["tester"]["reasoning_effort"], "high")
        self.assertEqual(manifest["roles"]["researcher"]["model"], "gpt-6.1-sol")
        self.assertEqual(manifest["roles"]["researcher"]["reasoning_effort"], "high")
        self.assertEqual(manifest["roles"]["reviewer"]["model"], "gpt-6.1-sol")
        self.assertEqual(manifest["roles"]["reviewer"]["reasoning_effort"], "max")
        self.assertEqual(manifest["roles"]["guardian"]["model"], "gpt-6-astra")
        self.assertEqual(manifest["roles"]["guardian"]["reasoning_effort"], "medium")
        self.assertTrue(manifest["roles"]["guardian"]["automatic"])
        self.assertFalse(manifest["roles"]["guardian"]["requires_explicit_request"])
        for profile in ("conductor", "explorer", "worker", "tester", "researcher", "reviewer", "guardian"):
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
            disabled_gate = json.loads(json.dumps(raw))
            disabled_gate["roles"]["guardian"]["automatic"] = False
            with self.assertRaisesRegex(controller.ValidationError, "automatic final gate"):
                controller._normalize_manifest(disabled_gate, path)
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

    def test_deployment_patch_preserves_selected_session_model(self):
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
        with self.assertRaisesRegex(controller.ValidationError, "gate_mode must be final"):
            controller.validate_plan(ordinary, manifest=manifest)
        plan["tasks"].append({
            "task_id": "verify", "role": "tester", "objective": "Verify changed behavior",
            "depends_on": ["change"], "read_only": True, "writes": False,
        })
        normalized = controller.validate_plan(plan, manifest=manifest)
        self.assertEqual(normalized["topological_order"], ["change", "verify"])

    def test_installed_session_choice_is_allowed_but_role_drift_blocks_dispatch(self):
        manifest = controller.load_manifest(Path.cwd())
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            repo = root / "repo"
            repo.mkdir()
            codex_home = root / "codex-home"
            agents = codex_home / "agents"
            agents.mkdir(parents=True)
            for role in ("conductor", "explorer", "worker", "tester", "researcher", "reviewer", "guardian"):
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

    def test_read_only_final_packet_allows_unneeded_tests(self):
        manifest = controller.load_manifest(Path.cwd())
        plan = controller.validate_plan({
            "plan_id": "docs-inspect", "objective": "Inspect documentation",
            "tasks": [{"task_id": "inspect", "role": "explorer", "objective": "Inspect documentation"}],
        }, manifest=manifest)
        packet = controller.build_gate_packet(plan, {"test_evidence": "not applicable"}, mode="final")
        self.assertEqual(packet["Test evidence"], "not applicable")
        self.assertEqual(plan["gate_mode"], "final")

    def test_new_plans_cannot_disable_or_move_the_final_gate(self):
        manifest = controller.load_manifest(Path.cwd())
        plan = {"plan_id": "automatic", "objective": "Inspect code", "tasks": [
            {"task_id": "inspect", "role": "explorer", "objective": "Inspect code"}
        ]}
        self.assertEqual(controller.validate_plan(plan, manifest)["gate_mode"], "final")
        for mode in ("none", "pre", None, True, ["final"]):
            with self.subTest(mode=mode), self.assertRaisesRegex(controller.ValidationError, "gate_mode must be final"):
                controller.validate_plan(dict(plan, gate_mode=mode), manifest)

    def test_read_only_completion_runs_reviewer_then_guardian_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            calls = []
            runner = controller.CodexRunner(invoke=lambda **_: {})
            orchestrator = controller.Orchestrator(root, root / "codex-home", runner=runner)
            plan = make_plan(orchestrator.manifest, writer=False)
            run_id = orchestrator.store.create(plan, {"head": "baseline"}, orchestrator.manifest)
            state, plan = orchestrator.store.load(run_id)
            state["state"] = "running"
            state["tasks"]["inspect"]["state"] = "succeeded"
            state["tasks"]["inspect"]["result_summary"] = {"summary": "Inspection completed"}

            def invoke(**kwargs):
                calls.append(kwargs["role"])
                if kwargs["role"] == "conductor":
                    return planning_response()
                packet = json.loads(kwargs["prompt"])
                self.assertEqual(packet["Task results"]["inspect"]["result"]["summary"], "Inspection completed")
                if kwargs["role"] == "guardian":
                    self.assertEqual(packet["Reviewer result"]["verdict"], "approve")
                return {"final": {"Verdict": "approve", "Important findings": [], "Required changes": [], "Residual risks": []}}

            with mock.patch.object(runner, "invoke", side_effect=invoke), \
                 mock.patch.object(orchestrator, "_mark_stale_evidence", return_value=False), \
                 mock.patch.object(controller, "validate_guardian_runtime"):
                orchestrator._execute_ready(run_id, state, plan, orchestrator.manifest)
                self.assertEqual(state["state"], "completed")
                self.assertEqual(calls, ["conductor", "reviewer", "guardian"])
                self.assertTrue(orchestrator._run_final_reviewer_review(state, plan))
                self.assertTrue(orchestrator._consume_guardian(state, plan))
                self.assertEqual(calls, ["conductor", "reviewer", "guardian"])

    def test_guardian_cannot_run_before_reviewer_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            orchestrator = controller.Orchestrator(root, root / "codex-home")
            plan = make_plan(orchestrator.manifest, writer=False)
            run_id = orchestrator.store.create(plan, {"head": "baseline"}, orchestrator.manifest)
            state, plan = orchestrator.store.load(run_id)
            for review in (None, {"verdict": "revise"}, {"verdict": "block"}):
                state["review"] = review
                with self.subTest(review=review), self.assertRaisesRegex(controller.ValidationError, "successful Reviewer"):
                    controller.run_guardian(orchestrator.store, state, plan, root, orchestrator.runner)
            state["review"] = {"verdict": "approve"}
            with self.assertRaisesRegex(controller.ValidationError, "all task results"):
                controller.run_guardian(orchestrator.store, state, plan, root, orchestrator.runner)
            with self.assertRaisesRegex(controller.ValidationError, "automatic final gate"):
                controller.run_guardian(orchestrator.store, state, plan, root, orchestrator.runner, mode="pre")

    def test_native_changed_task_records_real_diff_and_gates_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            for args in (["init", "-q"], ["config", "user.name", "Test"], ["config", "user.email", "test@local"]):
                subprocess.run(["git", *args], cwd=repo, check=True)
            (repo / "file.txt").write_text("before\n")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "before"], cwd=repo, check=True)
            base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            (repo / "file.txt").write_text("after\n")
            subprocess.run(["git", "commit", "-qam", "after"], cwd=repo, check=True)
            delivery = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            diff = subprocess.check_output(["git", "diff", "--no-ext-diff", "--no-color", "--binary", base, delivery], cwd=repo)
            receipt = {
                "objective": "Complete a native Root edit", "changes": True,
                "base_head": base, "delivery_commit": delivery,
                "review": {"verdict": "approve", "diff_hash": controller.sha256_bytes(diff)},
                "test_evidence": [{"command": "verify change", "exit_code": 0, "output_hash": "0" * 64, "artifact_hashes": {}}],
            }
            receipt_path = root / "completion.json"
            receipt_path.write_text(json.dumps(receipt))
            calls = []

            def invoke(**kwargs):
                calls.append(kwargs["role"])
                if kwargs["role"] == "conductor":
                    return planning_response()
                packet = json.loads(kwargs["prompt"])
                self.assertEqual(packet["Final diff"], diff.decode())
                self.assertEqual(packet["Final integration evidence"]["changed_paths"], ["file.txt"])
                if kwargs["role"] == "guardian":
                    self.assertEqual(packet["Reviewer result"]["verdict"], "approve")
                return {"final": {"Verdict": "approve", "Important findings": [], "Required changes": [], "Residual risks": []}}

            orchestrator = controller.Orchestrator(repo, root / "codex-home", controller.CodexRunner(invoke=invoke))
            with mock.patch.object(orchestrator, "_require_installed_topology"), mock.patch.object(controller, "validate_guardian_runtime"):
                with self.assertRaisesRegex(controller.ValidationError, "native-begin"):
                    orchestrator.native_gate(receipt_path, "not-started")
                subprocess.run(["git", "checkout", "-q", base], cwd=repo, check=True)
                orchestrator.native_begin("native-test", receipt["objective"])
                orchestrator.native_begin("wrong-base", receipt["objective"])
                orchestrator.native_begin("hidden-changes", receipt["objective"])
                subprocess.run(["git", "checkout", "-q", delivery], cwd=repo, check=True)
                first = orchestrator.native_gate(receipt_path, "native-test")
                second = orchestrator.native_gate(receipt_path, "native-test")
                self.assertEqual(first["state"], "completed")
                self.assertEqual(first["gate"]["gate_id"], second["gate"]["gate_id"])
                self.assertEqual(calls, ["conductor"] * 3 + ["reviewer", "guardian"])
                receipt["result"] = "Different work"
                receipt_path.write_text(json.dumps(receipt))
                with self.assertRaisesRegex(controller.ControllerError, "completion changed"):
                    orchestrator.native_gate(receipt_path, "native-test")
                receipt["base_head"] = "0" * 40
                receipt_path.write_text(json.dumps(receipt))
                with self.assertRaisesRegex(controller.ValidationError, "verified snapshot"):
                    orchestrator.native_gate(receipt_path, "wrong-base")
                receipt["base_head"] = base
                receipt["changes"] = False
                receipt["test_evidence"] = []
                receipt_path.write_text(json.dumps(receipt))
                with self.assertRaisesRegex(controller.ValidationError, "recorded baseline"):
                    orchestrator.native_gate(receipt_path, "hidden-changes")
                with self.assertRaisesRegex(controller.ControllerError, "cannot apply"):
                    orchestrator.apply("native-test")

    def test_native_receipt_cannot_self_attest_reviewer_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@local", "commit", "--allow-empty", "-qm", "baseline"], cwd=repo, check=True)
            receipt = {"objective": "Inspect code", "changes": False, "review": {"verdict": "approve"}, "test_evidence": []}
            receipt_path = root / "completion.json"
            try:
                receipt_path.write_text(json.dumps(receipt))
                calls = []
                def invoke(**kwargs):
                    calls.append(kwargs["role"])
                    if kwargs["role"] == "conductor":
                        return planning_response()
                    return {"final": {"Verdict": "revise", "Important findings": ["Fix required"], "Required changes": [], "Residual risks": []}}
                orchestrator = controller.Orchestrator(repo, root / "codex-home", controller.CodexRunner(invoke=invoke))
                with mock.patch.object(orchestrator, "_require_installed_topology"):
                    orchestrator.native_begin("native-revise", receipt["objective"])
                    result = orchestrator.native_gate(receipt_path, "native-revise")
                self.assertEqual(calls, ["conductor", "reviewer"])
                self.assertEqual(result["state"], "needs_input")
                self.assertIsNone(result["gate"])
                self.assertEqual(result["review"]["verdict"], "revise")
            finally:
                receipt_path.unlink(missing_ok=True)

    def test_native_drift_during_reviewer_or_guardian_prevents_completion(self):
        for mutate_role in ("reviewer", "guardian"):
            with self.subTest(mutate_role=mutate_role), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                repo = root / "repo"
                repo.mkdir()
                subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
                subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@local", "commit", "--allow-empty", "-qm", "baseline"], cwd=repo, check=True)
                receipt_path = root / "completion.json"
                receipt_path.write_text(json.dumps({"objective": "Inspect code", "changes": False, "test_evidence": []}))
                calls = []
                def invoke(**kwargs):
                    calls.append(kwargs["role"])
                    if kwargs["role"] == "conductor":
                        return planning_response()
                    if kwargs["role"] == mutate_role:
                        (repo / "concurrent.txt").write_text("Changed during review\n")
                    return {"final": {"Verdict": "approve", "Important findings": [], "Required changes": [], "Residual risks": []}}
                orchestrator = controller.Orchestrator(repo, root / "codex-home", controller.CodexRunner(invoke=invoke))
                with mock.patch.object(orchestrator, "_require_installed_topology"), mock.patch.object(controller, "validate_guardian_runtime"):
                    orchestrator.native_begin("native-drift", "Inspect code")
                    with self.assertRaisesRegex(controller.ControllerError, "during final checks"):
                        orchestrator.native_gate(receipt_path, "native-drift")
                state, _ = orchestrator.store.load("native-drift")
                self.assertEqual(state["state"], "needs_input")
                self.assertEqual(calls, ["conductor", "reviewer"] if mutate_role == "reviewer" else ["conductor", "reviewer", "guardian"])

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

    def test_codex_event_error_recovers_only_after_successful_terminal_event(self):
        recovered = "\n".join(json.dumps(item) for item in (
            {"type": "error", "message": "transient transport reset"},
            {"type": "assistant_message", "text": "working"},
            {"type": "turn.completed", "output": {"answer": "ready"}},
        ))
        parsed = controller.parse_codex_events(recovered)
        self.assertIsNone(parsed["failed_event"])
        self.assertEqual(parsed["final"], {"answer": "ready"})

        explicit_failure = "\n".join(json.dumps(item) for item in (
            {"type": "assistant_message", "text": "looks good"},
            {"type": "turn.failed", "message": "terminal failure"},
            {"type": "turn.completed", "output": "late text cannot erase failure"},
        ))
        parsed = controller.parse_codex_events(explicit_failure)
        self.assertEqual(parsed["failed_event"]["type"], "turn.failed")
        self.assertEqual(parsed["final"], "late text cannot erase failure")

        assistant_only = "\n".join(json.dumps(item) for item in (
            {"type": "error", "message": "request failed"},
            {"type": "assistant_message", "text": "fallback prose"},
        ))
        self.assertEqual(controller.parse_codex_events(assistant_only)["failed_event"]["type"], "error")
        with self.assertRaises(controller.UnknownCodexEventError):
            controller.parse_codex_events('{"type":"future.event"}')
        with self.assertRaises(controller.UnknownCodexEventError):
            controller.parse_codex_events("not json")

    def test_macos_system_proxy_fills_only_unset_proxy_environment_pairs(self):
        system = """<dictionary> {
  HTTPEnable : 1
  HTTPProxy : 127.0.0.1
  HTTPPort : 45678
  HTTPSEnable : 1
  HTTPSProxy : ::1
  HTTPSPort : 45679
}"""
        completed = subprocess.CompletedProcess(["scutil"], 0, system, "")
        with mock.patch.object(controller.subprocess, "run", return_value=completed) as scutil:
            child = controller.codex_child_environment({"PATH": "/bin"}, platform="darwin")
        scutil.assert_called_once_with(
            ["/usr/sbin/scutil", "--proxy"], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=2,
        )
        self.assertEqual(child["http_proxy"], "http://127.0.0.1:45678")
        self.assertEqual(child["HTTP_PROXY"], child["http_proxy"])
        self.assertEqual(child["https_proxy"], "http://[::1]:45679")
        self.assertEqual(child["HTTPS_PROXY"], child["https_proxy"])

        explicit = {"PATH": "/bin", "http_proxy": "", "HTTP_PROXY": "custom-http", "HTTPS_PROXY": "custom-https"}
        with mock.patch.object(controller.subprocess, "run", return_value=completed):
            child = controller.codex_child_environment(explicit, platform="darwin")
        self.assertEqual(child["http_proxy"], "")
        self.assertEqual(child["HTTP_PROXY"], "custom-http")
        self.assertEqual(child["HTTPS_PROXY"], "custom-https")
        all_proxy = {"PATH": "/bin", "ALL_PROXY": ""}
        with mock.patch.object(controller.subprocess, "run") as scutil:
            child = controller.codex_child_environment(all_proxy, platform="darwin")
        scutil.assert_not_called()
        self.assertEqual(child, all_proxy)

    def test_codex_runner_persists_private_transcripts_and_kills_timeout_descendants(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_home = root / "codex-home"
            fake = root / "fake-codex"
            success_events = "\n".join(json.dumps(item) for item in (
                {"type": "error", "message": "transient"},
                {"type": "turn.completed", "output": {"outcome": "ready"}},
            )) + "\n"
            fake.write_text("#!%s\nimport sys\nsys.stdout.write(%r)\nsys.stderr.write('private diagnostic\\n')\n" % (sys.executable, success_events))
            fake.chmod(0o700)
            runner = controller.CodexRunner(codex_bin=str(fake))
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
                parsed = runner.invoke("private prompt", "conductor", True, root, max_attempts=1)
            self.assertIsNone(parsed["failed_event"])
            self.assertEqual(parsed["final"], {"outcome": "ready"})
            paths = parsed["evidence_paths"]
            stdout_path = Path(paths["stdout"])
            stderr_path = Path(paths["stderr"])
            self.assertEqual(stdout_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(stderr_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(stdout_path.parent.stat().st_mode & 0o777, 0o700)
            self.assertIn('"type": "error"', stdout_path.read_text())
            self.assertIn("private diagnostic", stderr_path.read_text())

            marker = root / "descendant-finished"
            pid_path = root / "descendant.pid"
            child_code = "import time; time.sleep(1.5); open(%r, 'w').write('alive')" % str(marker)
            timeout_script = (
                "#!%s\nimport subprocess, sys, time\n"
                "sys.stdout.write('{\\\"type\\\":\\\"item.started\\\"}\\n'); sys.stdout.flush()\n"
                "sys.stderr.write('private timeout diagnostic\\n'); sys.stderr.flush()\n"
                "child = subprocess.Popen([sys.executable, '-c', %r])\n"
                "open(%r, 'w').write(str(child.pid))\n"
                "time.sleep(20)\n"
            ) % (sys.executable, child_code, str(pid_path))
            fake.write_text(timeout_script)
            fake.chmod(0o700)
            progress = io.StringIO()
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False), \
                 mock.patch.object(controller.sys, "stderr", progress):
                with self.assertRaises(controller.ControllerError) as raised:
                    runner.invoke("private prompt", "conductor", True, root, timeout=0.3, max_attempts=1)
            timeout_paths = raised.exception.evidence_paths
            partial_stdout = Path(timeout_paths["stdout"]).read_text()
            partial_stderr = Path(timeout_paths["stderr"]).read_text()
            self.assertIn("item.started", partial_stdout)
            self.assertIn("private timeout diagnostic", partial_stderr)
            self.assertEqual(Path(timeout_paths["stdout"]).stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(timeout_paths["stderr"]).stat().st_mode & 0o777, 0o600)
            self.assertNotIn("private timeout diagnostic", progress.getvalue())
            time.sleep(1.7)
            self.assertFalse(marker.exists(), "timed-out child process survived the process-group cleanup")

    def test_codex_runner_keyboard_interrupt_cleans_process_group_and_preserves_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_home = root / "codex-home"
            fake = root / "fake-codex"
            marker = root / "interrupted-descendant-finished"
            pid_path = root / "interrupted-descendant.pid"
            child_code = "import time; time.sleep(1.5); open(%r, 'w').write('alive')" % str(marker)
            fake.write_text((
                "#!%s\nimport subprocess, sys, time\n"
                "sys.stdout.write('{\\\"type\\\":\\\"item.started\\\"}\\n'); sys.stdout.flush()\n"
                "sys.stderr.write('private interrupt diagnostic\\n'); sys.stderr.flush()\n"
                "child = subprocess.Popen([sys.executable, '-c', %r])\n"
                "open(%r, 'w').write(str(child.pid))\n"
                "time.sleep(20)\n"
            ) % (sys.executable, child_code, str(pid_path)))
            fake.chmod(0o700)
            runner = controller.CodexRunner(codex_bin=str(fake))
            original_wait = controller.subprocess.Popen.wait
            waits = []

            def interrupt_after_child_starts(process, timeout=None):
                waits.append(process.pid)
                if len(waits) == 1:
                    deadline = time.monotonic() + 5
                    while not pid_path.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    if not pid_path.exists():
                        raise AssertionError("fake CLI did not start its descendant")
                    raise KeyboardInterrupt()
                return original_wait(process, timeout=timeout)

            with mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False), \
                 mock.patch.object(controller, "codex_child_environment", side_effect=lambda environment: dict(environment)), \
                 mock.patch.object(controller.subprocess.Popen, "wait", new=interrupt_after_child_starts):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    runner.invoke("private prompt", "conductor", True, root, timeout=30, max_attempts=2)

            self.assertEqual(len(waits), 3, "the interrupted read-only invocation must not retry")
            paths = raised.exception.evidence_paths
            partial_stdout = Path(paths["stdout"]).read_text()
            partial_stderr = Path(paths["stderr"]).read_text()
            self.assertIn("item.started", partial_stdout)
            self.assertIn("private interrupt diagnostic", partial_stderr)
            self.assertEqual(Path(paths["stdout"]).stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(paths["stderr"]).stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(list((codex_home / "astra-orchestrator" / "invocations").iterdir())), 1)
            time.sleep(1.7)
            self.assertFalse(marker.exists(), "interrupted child process survived the process-group cleanup")


if __name__ == "__main__":
    unittest.main()
