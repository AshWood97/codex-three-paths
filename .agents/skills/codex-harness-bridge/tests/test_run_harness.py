"""Contract tests for the portable six-harness bridge runner."""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_harness.py"
SPEC = importlib.util.spec_from_file_location("run_harness", SCRIPT)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


HARNESS_NAMES = (
    "codex-cli", "claude-code", "grok-build", "opencode", "pi", "deepseek-harness"
)
CANONICAL_MODEL = "gemini-3.8-flash-high"


STUB_SOURCE = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time

if "--help" in sys.argv:
    print("--json --sandbox --model --cd --config --print --output-format --permission-mode --tools --bare --cwd --no-subagents --pure --format --dir --agent --mode --no-session --no-extensions --no-skills --no-context-files --no-approve --profile")
    raise SystemExit(0)
args = sys.argv[1:]
if args and args[0] == "exec":
    required = {"--json", "--sandbox", "--model", "--cd"}
elif "--bare" in args:
    required = {"--print", "--output-format", "--permission-mode", "--tools"}
elif "--no-subagents" in args:
    required = {"--cwd", "--sandbox", "--model", "--output-format"}
elif "--pure" in args:
    required = {"run", "--format", "--dir", "--agent", "--model"}
elif "--no-context-files" in args:
    required = {"--print", "--mode", "--no-session", "--no-extensions", "--tools"}
else:
    required = {"--profile"}
prompt = args[args.index("-p") + 1] if "-p" in args else args[-1]
if not required.issubset(args) or "Codex Harness Bridge contract:" not in prompt:
    print("invalid adapter command", file=sys.stderr)
    raise SystemExit(64)
control_path = pathlib.Path(__file__).with_suffix(".control.json")
control = json.loads(control_path.read_text(encoding="utf-8")) if control_path.exists() else {}
def setting(name, default=None):
    return control.get(name, default)
if setting("STUB_NO_IDENTITY") == "1":
    event = {"type": "completed"}
else:
    event = {
        "type": "turn_started",
        "model": setting("STUB_MODEL", "gemini-3.8-flash-high"),
        "provider": setting("STUB_PROVIDER", "stub-provider"),
        "session_id": setting("STUB_SESSION", "stub-session"),
        "bridge_inner": os.environ.get("CODEX_HARNESS_BRIDGE_INNER"),
    }
print(json.dumps(event), flush=True)
if setting("STUB_EVENT_FAILURE"):
    print(json.dumps({"type": "turn.failed"}), flush=True)
if setting("STUB_READY"):
    pathlib.Path(setting("STUB_READY")).write_text("ready", encoding="utf-8")
if os.environ.get("BRIDGE_TOKEN"):
    print("credential=" + os.environ["BRIDGE_TOKEN"], flush=True)
if setting("STUB_WRITE"):
    target = pathlib.Path.cwd() / setting("STUB_WRITE")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("owned change\n", encoding="utf-8")
if setting("STUB_UNOWNED"):
    target = pathlib.Path.cwd() / setting("STUB_UNOWNED")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("unowned change\n", encoding="utf-8")
if setting("STUB_SLEEP"):
    time.sleep(float(setting("STUB_SLEEP")))
raise SystemExit(int(setting("STUB_EXIT", "0")))
'''


class BridgeRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.runs = self.base / "runs"
        self.runs.mkdir()
        self.brief = self.base / "brief.md"
        self.brief.write_text("Update one bounded component and report checks.\n", encoding="utf-8")
        self.stub = self.base / "stub-cli"
        self.stub.write_text(STUB_SOURCE, encoding="utf-8")
        self.stub.chmod(self.stub.stat().st_mode | stat.S_IXUSR)
        self.config_path = self.base / "private-config.json"
        self.config = self._config()
        self._write_config()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _config(self) -> dict[str, Any]:
        config: dict[str, Any] = {
            "version": 1,
            "binaries": {name: str(self.stub) for name in HARNESS_NAMES},
            "model_ids": {name: {CANONICAL_MODEL: CANONICAL_MODEL} for name in HARNESS_NAMES},
            "env_map": {name: {"BRIDGE_TOKEN": "STUB_SECRET"} for name in HARNESS_NAMES},
            "opencode_agents": {"read-only": "bridge-review", "workspace-write": "bridge-worker"},
            "deepseek_profiles": {
                CANONICAL_MODEL: {
                    mode: {"profile": f"bridge-{mode}", "model_id": CANONICAL_MODEL}
                    for mode in ("read-only", "workspace-write")
                }
            },
        }
        return config

    def _write_config(self) -> None:
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.config_path.chmod(0o600)

    def _request(self, harness: str = "codex-cli", **overrides: Any) -> runner.Request:
        values: dict[str, Any] = {
            "cwd": self.workspace,
            "brief": self.brief,
            "run_dir": self.runs / f"run-{len(list(self.runs.iterdir()))}",
            "harness": harness,
            "role": "worker",
            "mode": "workspace-write",
            "owned": ("owned",),
            "timeout": 10,
            "config_path": self.config_path,
            "allow_experimental_dsh": harness == "deepseek-harness",
        }
        values.update(overrides)
        return runner.Request(**values)

    def _environment(self, **extra: str) -> dict[str, str]:
        self.stub.with_suffix(".control.json").write_text(json.dumps(extra), encoding="utf-8")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "STUB_SECRET": "private-credential-value-123",
        }
        if runner.INNER_RUN_ENV in extra:
            env[runner.INNER_RUN_ENV] = extra[runner.INNER_RUN_ENV]
        return env

    def test_all_six_adapters_run_with_one_role_and_common_evidence(self) -> None:
        for harness in HARNESS_NAMES:
            with self.subTest(harness=harness):
                report = runner.execute(
                    self._request(harness),
                    self._environment(STUB_WRITE=f"owned/{harness}.txt"),
                )
                self.assertEqual(report["status"], "partial")
                self.assertEqual(report["bridge_status"], "completed-awaiting-review")
                self.assertEqual(report["requested"]["harness"], harness)
                self.assertEqual(report["requested"]["model"], CANONICAL_MODEL)
                self.assertEqual(report["observed"]["model"], CANONICAL_MODEL)
                self.assertEqual(report["observed"]["provider"], "stub-provider")
                self.assertIn("stub-session", report["observed"]["session_ids"])
                self.assertEqual(report["changed_paths"], [f"owned/{harness}.txt"])
                self.assertTrue(report["run_id"])
                self.assertTrue(Path(report["report_path"]).is_file())

    def test_adapter_commands_have_explicit_single_process_boundaries(self) -> None:
        cfg = self.config
        for harness in HARNESS_NAMES:
            with self.subTest(harness=harness):
                argv = runner.build_argv(
                    harness, "/tmp/cli", self.workspace, "brief", "worker", "workspace-write", cfg
                )
                self.assertEqual(argv[0], "/tmp/cli")
                self.assertIn("brief", argv)
                if harness == "codex-cli":
                    self.assertIn("features.multi_agent=false", argv)
                elif harness == "claude-code":
                    self.assertIn("--bare", argv)
                    self.assertIn("--tools", argv)
                    self.assertNotIn("Agent", argv[argv.index("--tools") + 1])
                elif harness == "grok-build":
                    self.assertIn("--no-subagents", argv)
                elif harness == "opencode":
                    self.assertIn("bridge-worker", argv)
                elif harness == "pi":
                    self.assertIn("--print", argv)
                    self.assertIn("--no-context-files", argv)
                    self.assertIn("--no-extensions", argv)
                else:
                    self.assertEqual(argv[argv.index("--profile") + 1], "bridge-workspace-write")

    def test_role_model_mapping_is_fixed_across_harnesses(self) -> None:
        self.assertEqual(runner.ROLE_MODELS, {
            "root": "grok-4.7", "complex": "grok-4.7",
            "researcher": "step-5-preview", "reviewer": "step-5-preview",
            "explorer": CANONICAL_MODEL, "worker": CANONICAL_MODEL, "tester": CANONICAL_MODEL,
        })

    def test_wrapped_prompt_keeps_the_task_inside_the_declared_boundary(self) -> None:
        prompt = runner._wrap_task_prompt("Implement this.", "worker", "workspace-write", ("src/a",))
        self.assertIn("Do not delegate", prompt)
        self.assertIn("write only within the owned paths: src/a", prompt)
        self.assertIn("Implement this.", prompt)
        self.assertIn("remain in force", prompt)

    def test_child_instructions_are_single_role_and_secret_is_redacted(self) -> None:
        report = runner.execute(self._request(), self._environment())
        log_text = (Path(report["report_path"]).parent / "stdout.log").read_text(encoding="utf-8")
        self.assertIn("[REDACTED]", log_text)
        self.assertNotIn("private-credential-value-123", log_text)
        self.assertIn("One-shot child process", " ".join(item["command"] for item in report["checks"]))
        self.assertIn('"bridge_inner": "1"', log_text)

    def test_secret_split_across_pipe_reads_is_redacted(self) -> None:
        class ChunkedPipe:
            def __init__(self) -> None:
                self.chunks = iter((b"credential=private-", b"credential-value-123\n"))

            def read(self, _size: int) -> bytes:
                return next(self.chunks, b"")

            def close(self) -> None:
                pass

        output = self.base / "split-secret.log"
        errors: list[str] = []
        redactor = runner.Redactor(["private-credential-value-123"])
        runner._run_stream(ChunkedPipe(), output, redactor, None, errors, "stdout")
        logged = output.read_text(encoding="utf-8")
        self.assertNotIn("private-credential-value-123", logged)
        self.assertIn("[REDACTED]", logged)

    def test_unmapped_database_url_is_not_forwarded(self) -> None:
        child, redactor = runner.child_environment(
            "codex-cli", self.config,
            {"PATH": "/usr/bin:/bin", "STUB_SECRET": "private-credential-value-123",
             "DATABASE_URL": "postgres://user:password@db.invalid/data"},
        )
        self.assertNotIn("DATABASE_URL", child)
        self.assertEqual(child["BRIDGE_TOKEN"], "private-credential-value-123")
        self.assertNotIn("private-credential-value-123", redactor.text("private-credential-value-123"))

    def test_tool_payload_cannot_spoof_runtime_identity(self) -> None:
        identity = runner.EventIdentity("codex-cli")
        identity.consume(json.dumps({"type": "item.completed", "item": {
            "model": CANONICAL_MODEL, "provider": "forged-provider"}}))
        identity.consume(json.dumps({"model": CANONICAL_MODEL, "provider": "forged-provider"}))
        self.assertIsNone(identity.report()["observed_model"])
        self.assertIsNone(identity.report()["observed_provider"])
        identity.consume(json.dumps({"type": "thread.started", "model": CANONICAL_MODEL,
                                     "provider": "trusted-event"}))
        self.assertEqual(identity.report()["observed_provider"], "trusted-event")

    def test_shared_report_fields_are_present(self) -> None:
        report = runner.execute(self._request(), self._environment())
        required = {"schema_version", "mode", "run_id", "task_contract", "requested", "observed",
                    "status", "changed_paths", "checks", "uncertainties"}
        self.assertTrue(required.issubset(report))
        self.assertEqual(report["mode"], "harness")
        self.assertEqual(report["schema_version"], 1)
        self.assertTrue(all({"command", "status"}.issubset(check) for check in report["checks"]))

    def test_out_of_scope_changes_are_detected(self) -> None:
        report = runner.execute(
            self._request(), self._environment(STUB_UNOWNED="elsewhere/result.txt")
        )
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["bridge_status"], "out-of-scope-changes")
        self.assertEqual(report["changed_paths"], ["elsewhere/result.txt"])

    def test_any_workspace_write_during_read_only_run_is_detected(self) -> None:
        report = runner.execute(
            self._request(mode="read-only"),
            self._environment(STUB_WRITE="owned/result.txt"),
        )
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["bridge_status"], "read-only-violation")

    def test_git_metadata_write_during_read_only_run_is_detected(self) -> None:
        metadata = self.workspace / ".git"
        metadata.mkdir()
        (metadata / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        report = runner.execute(self._request(mode="read-only"),
                                self._environment(STUB_WRITE=".git/HEAD"))
        self.assertEqual(report["status"], "failed")
        self.assertIn(".git/HEAD", report["changed_paths"])

    def test_external_symlink_blocks_execution(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        (self.workspace / "owned").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(runner.BridgeError, "symlink outside"):
            runner.execute(self._request(), self._environment())

    def test_nonzero_exit_is_reported_as_failure(self) -> None:
        report = runner.execute(self._request(), self._environment(STUB_EXIT="7"))
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["exit_code"], 7)

    def test_failure_event_is_not_accepted_when_process_exits_zero(self) -> None:
        report = runner.execute(self._request(), self._environment(STUB_EVENT_FAILURE="1"))
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failure_events"], ["turn.failed"])

    def test_timeout_stops_the_child_and_records_partial_result(self) -> None:
        report = runner.execute(
            self._request(timeout=1),
            self._environment(STUB_WRITE="owned/partial.txt", STUB_SLEEP="20"),
        )
        self.assertEqual(report["status"], "cancelled")
        self.assertEqual(report["bridge_status"], "timed-out")
        self.assertEqual(report["changed_paths"], ["owned/partial.txt"])

    def test_keyboard_interrupt_cancels_child_and_reports_cancelled(self) -> None:
        class FakePipe:
            def __init__(self) -> None:
                self.pending = b'{"model":"gemini-3.8-flash-high","provider":"stub"}\n'

            def read(self, _size: int) -> bytes:
                chunk, self.pending = self.pending, b""
                return chunk

            def close(self) -> None:
                pass

        class FakeProcess:
            pid = 999999999

            def __init__(self) -> None:
                self.stdout = FakePipe()
                self.stderr = FakePipe()
                self.returncode: int | None = None
                self.wait_calls = 0

            def wait(self, timeout: float | None = None) -> int:
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise KeyboardInterrupt
                return self.returncode or 0

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.returncode = -2

            def kill(self) -> None:
                self.returncode = -9

        fake_process = FakeProcess()
        with mock.patch.object(runner, "inspect_cli", return_value={"claude_bare": False}), \
             mock.patch.object(runner.subprocess, "Popen", return_value=fake_process):
            report = runner.execute(self._request(), self._environment())
        self.assertEqual(report["status"], "cancelled")
        self.assertEqual(report["exit_code"], -2)

    @unittest.skipIf(os.name == "nt", "POSIX signal handling test")
    def test_sigterm_cancels_child_and_persists_report(self) -> None:
        ready = self.base / "child-ready"
        self._environment(STUB_SLEEP="20", STUB_READY=str(ready))
        request = self._request()
        argv = [sys.executable, str(SCRIPT), "--cwd", str(request.cwd),
                "--brief", str(request.brief), "--run-dir", str(request.run_dir),
                "--harness", request.harness, "--role", request.role,
                "--mode", request.mode, "--owned-path", "owned",
                "--config", str(request.config_path)]
        env = dict(os.environ)
        env["STUB_SECRET"] = "private-credential-value-123"
        process = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 8
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(ready.exists(), "stub child never started")
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=20)
        self.assertNotEqual(process.returncode, 0)
        report = json.loads((request.run_dir / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "cancelled")

    def test_launch_error_is_reported_and_does_not_hold_workspace_lock(self) -> None:
        broken = self.base / "broken-cli"
        broken.write_text("#!/not/a/real/interpreter\n", encoding="utf-8")
        broken.chmod(broken.stat().st_mode | stat.S_IXUSR)
        self.config["binaries"]["codex-cli"] = str(broken)
        self._write_config()
        with self.assertRaisesRegex(runner.BridgeError, "could not inspect"):
            runner.execute(self._request(), self._environment())

        # A subsequent run proves the failed Popen path released its workspace lock.
        self.config["binaries"]["codex-cli"] = str(self.stub)
        self._write_config()
        second = runner.execute(self._request(), self._environment())
        self.assertEqual(second["status"], "partial")

    def test_report_write_failure_cannot_return_a_success_status(self) -> None:
        with mock.patch.object(runner, "_write_json", side_effect=OSError("unwritable")):
            report = runner.execute(self._request(), self._environment())
        self.assertEqual(report["status"], "failed")
        self.assertIsNone(report["report_path"])
        self.assertFalse(report["report_written"])

    def test_deepseek_requires_explicit_opt_in_and_unknown_identity_is_unverified(self) -> None:
        with self.assertRaises(runner.BridgeError):
            runner.execute(self._request("deepseek-harness", allow_experimental_dsh=False), self._environment())
        report = runner.execute(
            self._request("deepseek-harness"), self._environment(STUB_NO_IDENTITY="1")
        )
        self.assertEqual(report["status"], "unverified")
        self.assertEqual(report["bridge_status"], "identity-unverified")
        self.assertIsNone(report["observed"]["model"])
        self.assertIsNone(report["observed"]["provider"])

    def test_missing_opencode_agent_fails_preflight_without_fallback(self) -> None:
        self.config["opencode_agents"] = {"read-only": "bridge-review"}
        self._write_config()
        with self.assertRaisesRegex(runner.BridgeError, "requires configured primary agents"):
            runner.execute(self._request("opencode", preflight_only=True), self._environment())

    def test_recursion_guard_rejects_an_inner_bridge_invocation(self) -> None:
        with self.assertRaisesRegex(runner.BridgeError, "recursive"):
            runner.execute(self._request(), self._environment(CODEX_HARNESS_BRIDGE_INNER="1"))

    def test_model_mismatch_is_not_accepted_as_completed(self) -> None:
        report = runner.execute(
            self._request(), self._environment(STUB_MODEL="different-model")
        )
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["bridge_status"], "model-mismatch")

    def test_owned_path_validation_rejects_escape_and_workspace_root(self) -> None:
        for value in ("../outside", "/absolute", "."):
            with self.subTest(value=value), self.assertRaises(runner.BridgeError):
                runner.validate_owned_paths([value])

    def test_workspace_lock_rejects_parallel_bridge_writer(self) -> None:
        with runner.workspace_lock(self.workspace):
            with self.assertRaisesRegex(runner.BridgeError, "already active"):
                with runner.workspace_lock(self.workspace):
                    pass

    def test_workspace_lock_unifies_repository_root_and_subdirectory(self) -> None:
        repo = self.base / "repo"
        child = repo / "child"
        (repo / ".git").mkdir(parents=True)
        child.mkdir()
        with runner.workspace_lock(repo):
            with self.assertRaisesRegex(runner.BridgeError, "already active"):
                with runner.workspace_lock(child):
                    pass

    def test_brief_must_be_a_regular_non_symlink_file(self) -> None:
        alias = self.base / "brief-link.md"
        alias.symlink_to(self.brief)
        with self.assertRaisesRegex(runner.BridgeError, "non-symlink"):
            runner.execute(self._request(brief=alias, preflight_only=True), self._environment())

    def test_config_must_be_private_and_never_accepts_secret_values_as_env_map(self) -> None:
        self.config_path.chmod(0o644)
        with self.assertRaisesRegex(runner.BridgeError, "permissions"):
            runner.load_config(self.config_path)
        self.config["env_map"]["codex-cli"] = {"API_KEY": "literal-secret-value"}
        self._write_config()
        with self.assertRaisesRegex(runner.BridgeError, "environment variable names"):
            runner.load_config(self.config_path)


if __name__ == "__main__":
    unittest.main()
