#!/usr/bin/env python3
"""Durable, fail-closed runtime for the Astra orchestrator.

The module intentionally uses only the Python 3.9 standard library.  The
public helpers are small enough to use from deterministic tests while the CLI
is the supported entry point for installed repositories.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime as _datetime
import errno
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = 1
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
SCHEMA_DIR = SKILL_DIR / "schemas"
MANIFEST_RELATIVE = Path(".agents/skills/codex-native-orchestrator/codex-native-orchestrator.json")
DEFAULT_CODEX_HOME = Path.home() / ".codex"
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DEPLOYMENT_ID_RE = re.compile(r"^deployment-[0-9a-f]{16}$")
ROLE_NAMES = {"explorer", "worker", "tester", "researcher", "reviewer", "guardian"}
TASK_ROLE_NAMES = ROLE_NAMES
RUNNER_TASK_ROLE_NAMES = TASK_ROLE_NAMES - {"guardian"}
READ_ONLY_ROLES = {"explorer", "researcher", "reviewer", "guardian"}
WRITER_ROLES = {"worker", "tester"}
GATE_MODES = {"none", "pre", "final"}
MAX_CONCURRENCY = 4

DEFAULT_ROLE_SPECS: Dict[str, Dict[str, Any]] = {
    "explorer": {"model": "gpt-6-luna", "reasoning_effort": "max", "read_only": True},
    "worker": {"model": "gpt-6-luna", "reasoning_effort": "max", "read_only": False},
    "tester": {"model": "gpt-6-sol", "reasoning_effort": "xhigh", "read_only": False},
    "researcher": {"model": "gpt-6-astra", "reasoning_effort": "medium", "read_only": True},
    "reviewer": {"model": "gpt-6-sol", "reasoning_effort": "xhigh", "read_only": True},
    "guardian": {"model": "gpt-6-astra", "reasoning_effort": "medium", "read_only": True},
}

EVENT_TYPES = {
    "run_created",
    "run_resumed",
    "run_cancelled",
    "run_failed",
    "run_needs_input",
    "run_completed",
    "run_applying",
    "run_applied",
    "run_cleaned",
    "task_ready",
    "task_started",
    "task_succeeded",
    "task_failed",
    "task_needs_input",
    "task_cancelled",
    "task_blocked",
    "task_recovered",
    "retry",
    "gate_started",
    "gate_completed",
    "gate_unavailable",
    "apply_started",
    "apply_task_succeeded",
    "apply_failed",
    "cleanup_started",
    "cleanup_task_succeeded",
    "cleanup_preserved",
    "deployment_started",
    "deployment_dry_run",
    "deployment_applied",
    "deployment_rolled_back",
    "deployment_failed",
    "deployment_file_applied",
    "deployment_recovery_required",
    "task_stale",
    "integration_created",
    "integration_commit_applied",
    "integration_finalized",
    "review_completed",
    "writer_model_completed",
    "writer_verified",
    "writer_staged",
    "writer_committed",
}

CODEX_EVENT_TYPES = {
    "thread.started",
    "turn.started",
    "turn.completed",
    "turn.failed",
    "turn.cancelled",
    "item.started",
    "item.updated",
    "item.completed",
    "response.completed",
    "response.failed",
    "message",
    "assistant_message",
    "result",
    "error",
}


class ControllerError(Exception):
    """Base class for expected, user-facing failures."""


class ValidationError(ControllerError):
    pass


class UnknownCodexEventError(ControllerError):
    pass


class LockError(ControllerError):
    pass


class RepoSafetyError(ControllerError):
    pass


class DeploymentError(ControllerError):
    pass


class WriterUncertainty(ControllerError):
    """The writer may have run; retrying could launch a duplicate."""


def utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def fsync_directory(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes, mode: Optional[int] = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None and path.exists():
        mode = file_mode(path)
    if mode is None:
        mode = 0o600
    fd, temp_name = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(str(temp_path), mode)
        os.replace(str(temp_path), str(path))
        fsync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()


def atomic_write_json(path: Path, value: Any, mode: Optional[int] = None) -> None:
    atomic_write_bytes(path, canonical_json(value) + b"\n", mode=mode)


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise ControllerError("cannot read JSON %s: %s" % (path, exc)) from exc


def resolve_codex_home(value: Optional[os.PathLike] = None) -> Path:
    raw = value if value is not None else os.environ.get("CODEX_HOME")
    if raw is None or str(raw).strip() == "":
        raw = DEFAULT_CODEX_HOME
    return Path(raw).expanduser().resolve()


def command_version(executable: str) -> str:
    path = shutil.which(executable)
    if not path:
        return "unavailable"
    try:
        result = subprocess.run([path, "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return (result.stdout or result.stderr).strip() or "unknown"


def normalize_relative_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("owned_paths entries must be strings")
    if not value or "\x00" in value:
        raise ValidationError("owned path is empty or contains NUL")
    if "\\" in value:
        raise ValidationError("owned path must use '/' separators: %r" % value)
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ValidationError("owned path must be relative: %r" % value)
    pieces = value.split("/")
    if any(piece == ".." for piece in pieces):
        raise ValidationError("owned path may not traverse parent directories: %r" % value)
    normalized = posix_norm(value)
    if normalized in ("", ".") or normalized.startswith("../"):
        raise ValidationError("owned path is unsafe: %r" % value)
    if normalized == ".git" or normalized.startswith(".git/"):
        raise ValidationError("owned path may not target Git metadata: %r" % value)
    return normalized


def posix_norm(value: str) -> str:
    parts = []
    for piece in value.split("/"):
        if piece in ("", "."):
            continue
        parts.append(piece)
    return "/".join(parts)


def normalize_owned_paths(values: Optional[Iterable[str]]) -> List[str]:
    if values is None:
        return []
    if isinstance(values, str) or not isinstance(values, (list, tuple, set)):
        raise ValidationError("owned_paths must be an array of relative paths")
    normalized = sorted({normalize_relative_path(value) for value in values})
    return normalized


def paths_overlap(first: str, second: str) -> bool:
    return first == second or first.startswith(second + "/") or second.startswith(first + "/")


def reject_overlapping_paths(paths: Iterable[str], label: str = "owned_paths") -> None:
    values = sorted(set(paths))
    for index, first in enumerate(values):
        for second in values[index + 1 :]:
            if paths_overlap(first, second):
                raise ValidationError("%s overlap after normalization: %r and %r" % (label, first, second))


def path_is_owned(path: str, owned_paths: Iterable[str]) -> bool:
    normalized = normalize_relative_path(path)
    return any(normalized == owned or normalized.startswith(owned + "/") for owned in owned_paths)


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _normalize_manifest(
    raw: Any,
    path: Path,
) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("source manifest must be a JSON object")
    roles = raw.get("roles")
    if not isinstance(roles, dict) or set(roles) != ROLE_NAMES:
        raise ValidationError("manifest must declare every fixed role exactly once")
    for name, fixed in DEFAULT_ROLE_SPECS.items():
        spec = roles[name]
        sandbox = "read-only" if fixed["read_only"] else "workspace-write"
        if not isinstance(spec, dict) or any(spec.get(field) != value for field, value in (
            ("model", fixed["model"]),
            ("reasoning_effort", fixed["reasoning_effort"]),
            ("sandbox_mode", sandbox),
        )):
            raise ValidationError("manifest role %s differs from the fixed topology" % name)
    max_concurrency = raw.get("max_concurrency", raw.get("max_concurrent_threads", 4))
    if not isinstance(max_concurrency, int) or isinstance(max_concurrency, bool) or max_concurrency < 1 or max_concurrency > MAX_CONCURRENCY:
        raise ValidationError("manifest max_concurrency must be an integer from 1 to %d" % MAX_CONCURRENCY)
    fixed_config = {
        "agents.enabled": True,
        "agents.max_concurrent_threads_per_session": max_concurrency,
        "agents.default_subagent_model": DEFAULT_ROLE_SPECS["worker"]["model"],
        "agents.default_subagent_reasoning_effort": DEFAULT_ROLE_SPECS["worker"]["reasoning_effort"],
    }
    if raw.get("config_values") != fixed_config or set(raw.get("managed_config_keys", [])) != set(fixed_config):
        raise ValidationError("manifest managed config differs from the fixed topology")
    if any(key in raw for key in ("config", "codex_config", "allowed_config_keys")):
        raise ValidationError("manifest config aliases are not allowed")
    deployment = raw.get("deployment", {})
    if isinstance(deployment, Mapping) and any(key in deployment for key in ("config", "config_values", "values", "allowed_config_keys", "managed_config_keys")):
        raise ValidationError("deployment config aliases are not allowed")
    result = dict(raw)
    result["roles"] = json.loads(json.dumps(roles))
    result["max_concurrency"] = max_concurrency
    result["manifest_path"] = str(path)
    return result


def load_manifest(repo: Path, required: bool = False) -> Dict[str, Any]:
    path = repo / MANIFEST_RELATIVE
    bundled_path = SKILL_DIR / "codex-native-orchestrator.json"
    if bundled_path.is_file() and read_json(bundled_path).get("installation_scope") == "global":
        path = bundled_path
    if not path.is_file():
        if required:
            raise ValidationError("source manifest is missing: %s" % path)
        raise ValidationError("fixed role manifest is missing: %s" % path)
    return _normalize_manifest(read_json(path), path)


def role_spec(manifest: Mapping[str, Any], role: str) -> Dict[str, Any]:
    if role not in ROLE_NAMES:
        raise ValidationError("unknown role: %s" % role)
    roles = manifest.get("roles", {})
    spec = dict(DEFAULT_ROLE_SPECS[role])
    if isinstance(roles, Mapping) and isinstance(roles.get(role), Mapping):
        spec.update(roles[role])
    return spec


def normalize_plan(plan: Mapping[str, Any], manifest: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    if not isinstance(plan, Mapping):
        raise ValidationError("plan must be a JSON object")
    schema_version = plan.get("schema_version", SCHEMA_VERSION)
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != SCHEMA_VERSION:
        raise ValidationError("plan schema_version must be the integer %d" % SCHEMA_VERSION)
    plan_id = plan.get("plan_id", plan.get("id"))
    if not isinstance(plan_id, str) or not RUN_ID_RE.match(plan_id):
        raise ValidationError("plan_id must be a short stable identifier")
    objective = plan.get("objective", plan.get("prompt", plan.get("description")))
    if not isinstance(objective, str) or not objective.strip():
        raise ValidationError("plan objective must be a non-empty string")
    gate_mode = plan.get("gate_mode", "none")
    if gate_mode not in GATE_MODES:
        raise ValidationError("gate_mode must be exactly one of: pre, final, none")
    hard_risk = plan.get("hard_risk", False)
    if not isinstance(hard_risk, bool):
        raise ValidationError("hard_risk must be a boolean")
    # Risk classification and guardian authorization are independent.  A plan
    # may be hard-risk while still using gate_mode=none when the user did not
    # explicitly request the optional guardian gate.  The hard-risk flag still
    # controls the fail-closed behavior when a gate is actually selected.
    hard_risk_reasons = plan.get("hard_risk_reasons", [])
    if isinstance(hard_risk_reasons, str):
        hard_risk_reasons = [hard_risk_reasons]
    if not isinstance(hard_risk_reasons, list) or any(not isinstance(item, str) or not item.strip() for item in hard_risk_reasons):
        raise ValidationError("hard_risk_reasons must be an array of non-empty strings")
    global_read_only = plan.get("read_only", False)
    if not isinstance(global_read_only, bool):
        raise ValidationError("read_only must be a boolean")
    raw_tasks = plan.get("tasks", plan.get("nodes"))
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValidationError("plan tasks must be a non-empty array")
    global_owned = normalize_owned_paths(plan.get("owned_paths", plan.get("ownedPaths", [])))
    reject_overlapping_paths(global_owned, "plan owned_paths")
    tasks: List[Dict[str, Any]] = []
    task_ids = set()
    for raw in raw_tasks:
        if not isinstance(raw, Mapping):
            raise ValidationError("each task must be an object")
        task_id = raw.get("task_id", raw.get("id"))
        if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
            raise ValidationError("task id must be a short stable identifier")
        if task_id in task_ids:
            raise ValidationError("duplicate task id: %s" % task_id)
        task_ids.add(task_id)
        role = raw.get("role", "worker")
        if role not in RUNNER_TASK_ROLE_NAMES:
            if role == "root":
                raise ValidationError("root is the controller and cannot be a plan task")
            if role == "guardian":
                raise ValidationError("guardian is reserved for the plan gate; use gate_mode")
            raise ValidationError("task %s has unknown role %r" % (task_id, role))
        deps = raw.get("depends_on", raw.get("dependencies", raw.get("requires", [])))
        if isinstance(deps, str):
            deps = [deps]
        if not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps):
            raise ValidationError("task %s dependencies must be an array of ids" % task_id)
        owned = normalize_owned_paths(raw.get("owned_paths", raw.get("ownedPaths", [])))
        read_only_default = role in READ_ONLY_ROLES or role == "tester"
        raw_read_only = raw.get("read_only", raw.get("readonly", read_only_default))
        raw_writes = raw.get("writes", not raw_read_only)
        if not isinstance(raw_read_only, bool):
            raise ValidationError("task %s read_only must be a boolean" % task_id)
        if not isinstance(raw_writes, bool):
            raise ValidationError("task %s writes must be a boolean" % task_id)
        read_only = True if global_read_only else raw_read_only
        writes = False if global_read_only else raw_writes
        if role in READ_ONLY_ROLES and (not read_only or writes):
            raise ValidationError("task %s role %s is read-only and cannot request writes" % (task_id, role))
        if role == "tester" and (not read_only or writes):
            raise ValidationError("task %s tester must be read-only in the persistent runner; use native dispatch for writable tests" % task_id)
        if read_only and writes:
            raise ValidationError("task %s cannot be both read_only and writes" % task_id)
        if not read_only and not writes:
            raise ValidationError("task %s must use a consistent read_only/writes pair" % task_id)
        if writes and not owned:
            raise ValidationError("writer task %s must declare owned_paths" % task_id)
        if global_owned:
            for path in owned:
                if not any(path == allowed or path.startswith(allowed + "/") for allowed in global_owned):
                    raise ValidationError("task %s path %r is outside plan owned_paths" % (task_id, path))
        task_objective = raw.get("objective", raw.get("prompt", raw.get("instruction", raw.get("task", ""))))
        if not isinstance(task_objective, str) or not task_objective.strip():
            raise ValidationError("task %s objective must be a non-empty string" % task_id)
        contract_lists = {}
        for field in ("scope", "non_goals", "acceptance_criteria", "context_refs"):
            values = _as_list(raw.get(field, []))
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValidationError("task %s %s must contain only non-empty strings" % (task_id, field))
            contract_lists[field] = values
        timeout_default = 1800 if read_only else 3600
        if manifest and isinstance(manifest.get("timeouts"), Mapping):
            timeout_default = manifest["timeouts"].get("read_only" if read_only else "writer", timeout_default)
        timeout = raw.get("timeout", timeout_default)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValidationError("task %s timeout must be a positive number" % task_id)
        retry_policy = raw.get("retry_policy", {"max_attempts": 2 if read_only else 1})
        if not isinstance(retry_policy, Mapping):
            raise ValidationError("task %s retry_policy must be an object" % task_id)
        max_attempts = retry_policy.get("max_attempts", 2 if read_only else 1)
        ceiling = 2 if read_only else 1
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1 or max_attempts > ceiling:
            raise ValidationError("task %s max_attempts must be from 1 to %d" % (task_id, ceiling))
        task = dict(raw)
        task.update({
            "task_id": task_id,
            "id": task_id,
            "role": role,
            "depends_on": sorted(set(deps)),
            "owned_paths": owned,
            "read_only": read_only,
            "writes": writes,
            "objective": task_objective,
            "scope": contract_lists["scope"],
            "non_goals": contract_lists["non_goals"],
            "acceptance_criteria": contract_lists["acceptance_criteria"],
            "context_refs": contract_lists["context_refs"],
            "write_mode": "read-only" if read_only else "workspace-write",
            "timeout": float(timeout),
            "retry_policy": {"max_attempts": max_attempts},
            "prompt": task_objective,
        })
        tasks.append(task)
    for task in tasks:
        for dep in task["depends_on"]:
            if dep not in task_ids:
                raise ValidationError("task %s depends on unknown task %s" % (task["task_id"], dep))
            if dep == task["task_id"]:
                raise ValidationError("task %s depends on itself" % task["task_id"])
    owned_pairs = []
    for task in tasks:
        for path in task["owned_paths"]:
            owned_pairs.append((path, task["task_id"]))
    for index, (first, first_task) in enumerate(owned_pairs):
        for second, second_task in owned_pairs[index + 1 :]:
            if first_task != second_task and paths_overlap(first, second):
                raise ValidationError("task owned_paths overlap after normalization: %s (%s) and %s (%s)" % (first, first_task, second, second_task))
    order = topological_order(tasks)
    critical_path_rank = critical_path_lengths(tasks, order)
    effective_manifest = manifest or {"max_concurrency": 4, "roles": DEFAULT_ROLE_SPECS}
    max_concurrency = plan.get("max_concurrency", effective_manifest.get("max_concurrency", 4))
    if not isinstance(max_concurrency, int) or isinstance(max_concurrency, bool) or max_concurrency < 1 or max_concurrency > MAX_CONCURRENCY:
        raise ValidationError("plan max_concurrency must be from 1 to %d" % MAX_CONCURRENCY)
    token_budget = plan.get("max_total_tokens")
    if token_budget is not None and (not isinstance(token_budget, int) or isinstance(token_budget, bool) or token_budget < 1):
        raise ValidationError("max_total_tokens must be a positive integer")
    contract_owner = plan.get("contract_owner")
    if contract_owner is not None:
        if contract_owner not in task_ids:
            raise ValidationError("contract_owner must name a task")
        owner_task = next(item for item in tasks if item["task_id"] == contract_owner)
        if not owner_task["writes"]:
            raise ValidationError("contract_owner must be a writer task")
    overrides = _as_list(plan.get("overrides", []))
    if any(not isinstance(value, str) or not value.strip() for value in overrides):
        raise ValidationError("overrides must be an array of non-empty strings")
    result = dict(plan)
    result.update({
        "schema_version": schema_version,
        "plan_id": plan_id,
        "objective": objective,
        "gate_mode": gate_mode,
        "hard_risk": hard_risk,
        "hard_risk_reasons": list(hard_risk_reasons),
        "read_only": global_read_only,
        "route": "hybrid",
        "overrides": list(overrides),
        "owned_paths": global_owned,
        "tasks": tasks,
        "max_concurrency": max_concurrency,
        "max_total_tokens": token_budget,
        "contract_owner": contract_owner,
        "topological_order": order,
        "critical_path_rank": critical_path_rank,
    })
    writer_ids = {task["task_id"] for task in tasks if task["writes"]}
    if gate_mode == "final" and writer_ids and not any(
        task["role"] == "tester"
        and task["read_only"]
        and writer_ids.issubset(set(_writer_ancestors(result, task["task_id"])))
        for task in tasks
    ):
        raise ValidationError("final Guardian gate requires a read-only tester downstream of every writer")
    return result


def topological_order(tasks: Sequence[Mapping[str, Any]]) -> List[str]:
    task_ids = [str(task.get("task_id", task.get("id"))) for task in tasks]
    dependencies = {task_id: set(task.get("depends_on", task.get("dependencies", []))) for task_id, task in zip(task_ids, tasks)}
    result: List[str] = []
    ready = sorted(task_id for task_id, deps in dependencies.items() if not deps)
    while ready:
        current = ready.pop(0)
        result.append(current)
        for task_id in sorted(dependencies):
            if current in dependencies[task_id]:
                dependencies[task_id].remove(current)
                if not dependencies[task_id]:
                    ready.append(task_id)
        ready.sort()
    if len(result) != len(task_ids):
        cyclic = sorted(task_id for task_id, deps in dependencies.items() if deps)
        raise ValidationError("plan dependency graph contains a cycle: %s" % ", ".join(cyclic))
    return result


def critical_path_lengths(tasks: Sequence[Mapping[str, Any]], order: Optional[Sequence[str]] = None) -> Dict[str, int]:
    ordered = list(order or topological_order(tasks))
    children: Dict[str, List[str]] = {task_id: [] for task_id in ordered}
    for task in tasks:
        task_id = str(task.get("task_id", task.get("id")))
        for dependency in task.get("depends_on", task.get("dependencies", [])):
            children[str(dependency)].append(task_id)
    ranks: Dict[str, int] = {}
    for task_id in reversed(ordered):
        ranks[task_id] = 1 + max((ranks[child] for child in children[task_id]), default=0)
    return ranks


def validate_plan(plan: Mapping[str, Any], manifest: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Validate and return the normalized plan."""
    return normalize_plan(plan, manifest=manifest)


class FileLock:
    """Small O_EXCL lock with conservative same-host dead-process recovery."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fd: Optional[int] = None
        self.token: Optional[str] = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.token = uuid.uuid4().hex
        payload = {"pid": os.getpid(), "host": socket.gethostname(), "created_at": utc_now(), "token": self.token}
        for attempt in range(2):
            try:
                self.fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                break
            except FileExistsError as exc:
                try:
                    existing = read_json(self.path)
                    existing_pid = existing.get("pid")
                    same_host = existing.get("host") == socket.gethostname()
                    if not same_host or not isinstance(existing_pid, int) or existing_pid <= 0:
                        raise LockError("lock is already held or malformed: %s" % self.path) from exc
                    try:
                        os.kill(existing_pid, 0)
                    except ProcessLookupError:
                        self.path.unlink()
                        fsync_directory(self.path.parent)
                        if attempt == 0:
                            continue
                    except PermissionError:
                        pass
                except ControllerError as read_exc:
                    raise LockError("lock is already held and cannot be verified: %s" % self.path) from read_exc
                raise LockError("lock is already held: %s" % self.path) from exc
        if self.fd is None:
            raise LockError("could not acquire lock: %s" % self.path)
        try:
            os.write(self.fd, canonical_json(payload) + b"\n")
            os.fsync(self.fd)
            fsync_directory(self.path.parent)
        except Exception:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = None
            self.token = None
            with contextlib.suppress(OSError):
                self.path.unlink()
            raise

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
            try:
                current = read_json(self.path)
            except ControllerError:
                current = {}
            if current.get("token") == self.token:
                with contextlib.suppress(FileNotFoundError):
                    self.path.unlink()
            self.token = None
            fsync_directory(self.path.parent)

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


def _git(repo: Path, args: Sequence[str], check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(["git"] + list(args), cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        raise RepoSafetyError("git invocation failed: %s" % exc) from exc
    if check and result.returncode != 0:
        raise RepoSafetyError("git %s failed: %s" % (" ".join(args), (result.stderr or result.stdout).strip()))
    return result


def _git_with_input(repo: Path, args: Sequence[str], input_text: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=str(repo),
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise RepoSafetyError("git invocation failed: %s" % exc) from exc
    if check and result.returncode != 0:
        raise RepoSafetyError("git %s failed: %s" % (" ".join(args), (result.stderr or result.stdout).strip()))
    return result


def _git_bytes(
    repo: Path,
    args: Sequence[str],
    input_bytes: Optional[bytes] = None,
    check: bool = True,
    environment: Optional[Mapping[str, str]] = None,
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=str(repo),
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(environment) if environment is not None else None,
        )
    except OSError as exc:
        raise RepoSafetyError("git invocation failed: %s" % exc) from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="surrogateescape").strip()
        raise RepoSafetyError("git %s failed: %s" % (" ".join(args), detail))
    return result


def _trusted_git_environment() -> Dict[str, str]:
    environment = dict(os.environ)
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_EDITOR": "true",
        "GIT_SEQUENCE_EDITOR": "true",
        "GIT_MERGE_AUTOEDIT": "no",
    })
    return environment


def _trusted_git_args(args: Sequence[str]) -> List[str]:
    return [
        "-c", "core.hooksPath=%s" % os.devnull,
        "-c", "commit.gpgSign=false",
        "-c", "tag.gpgSign=false",
        "-c", "user.name=Astra Orchestrator",
        "-c", "user.email=codex-native-orchestrator@local.invalid",
    ] + list(args)


def _trusted_git(repo: Path, args: Sequence[str], check: bool = True) -> subprocess.CompletedProcess:
    raw = _git_bytes(repo, _trusted_git_args(args), check=check, environment=_trusted_git_environment())
    return subprocess.CompletedProcess(
        raw.args,
        raw.returncode,
        stdout=raw.stdout.decode("utf-8", errors="surrogateescape"),
        stderr=raw.stderr.decode("utf-8", errors="surrogateescape"),
    )


def _trusted_git_bytes(
    repo: Path,
    args: Sequence[str],
    input_bytes: Optional[bytes] = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    return _git_bytes(
        repo,
        _trusted_git_args(args),
        input_bytes=input_bytes,
        check=check,
        environment=_trusted_git_environment(),
    )


def _decode_nul_paths(value: bytes) -> List[str]:
    return [item.decode("utf-8", errors="surrogateescape") for item in value.split(b"\0") if item]


def _git_nul_paths(repo: Path, args: Sequence[str], trusted: bool = False) -> List[str]:
    runner = _trusted_git_bytes if trusted else _git_bytes
    return sorted(set(_decode_nul_paths(runner(repo, args).stdout)))


def _tree_fingerprint_records(root: Path) -> List[Dict[str, Any]]:
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise RepoSafetyError("Git metadata path is not a real directory: %s" % root)
    records: List[Dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RepoSafetyError("Git metadata contains a symlink: %s" % path)
        if path.is_file():
            records.append({
                "path": path.relative_to(root).as_posix(),
                "hash": sha256_file(path),
                "mode": file_mode(path),
            })
    return records


def git_protected_snapshot(repo: Path) -> Dict[str, Any]:
    snapshot = repo_snapshot(repo)
    git_dir = Path(snapshot["git_dir"])
    index_path = git_dir / "index"
    config_path = git_dir / "config"
    refs = _git_bytes(repo, ["for-each-ref", "--format=%(refname)%00%(objectname)%00"]).stdout
    index_tree = _trusted_git(repo, ["write-tree"]).stdout.strip()
    return {
        "head": snapshot["head"],
        "branch": snapshot["branch"],
        "index_tree": index_tree,
        "index_hash": sha256_file(index_path) if index_path.is_file() else None,
        "config_hash": sha256_file(config_path) if config_path.is_file() else None,
        "refs_hash": sha256_bytes(refs),
        "hooks_hash": sha256_json(_tree_fingerprint_records(git_dir / "hooks")),
    }


def _immutable_git_metadata(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: snapshot.get(key) for key in ("config_hash", "hooks_hash")}


def repo_snapshot(repo: Path) -> Dict[str, Any]:
    repo = Path(repo).resolve()
    root = _git(repo, ["rev-parse", "--show-toplevel"]).stdout.strip()
    git_dir = _git(repo, ["rev-parse", "--git-common-dir"]).stdout.strip()
    if not os.path.isabs(git_dir):
        git_dir = str((repo / git_dir).resolve())
    branch = _git(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
    branch_name = branch.stdout.strip() if branch.returncode == 0 else "HEAD"
    head = _git(repo, ["rev-parse", "HEAD"]).stdout.strip()
    status = _git(repo, ["status", "--porcelain=v1", "--untracked-files=all"]).stdout
    return {"repo_root": str(Path(root).resolve()), "git_dir": str(Path(git_dir).resolve()), "branch": branch_name, "head": head, "clean": status == "", "status": status}


def require_clean_repo(repo: Path) -> Dict[str, Any]:
    snapshot = repo_snapshot(repo)
    if not snapshot["clean"]:
        raise RepoSafetyError("source repository must be clean before orchestration: %s" % snapshot["status"].strip())
    return snapshot


def verify_repo(repo: Path, expected: Mapping[str, Any], require_clean: bool = True) -> Dict[str, Any]:
    actual = repo_snapshot(repo)
    for key in ("repo_root", "git_dir", "branch", "head"):
        if expected.get(key) is not None and actual.get(key) != expected.get(key):
            raise RepoSafetyError("repository %s changed: expected %s, got %s" % (key, expected.get(key), actual.get(key)))
    if require_clean and not actual["clean"]:
        raise RepoSafetyError("repository is not clean immediately before mutation: %s" % actual["status"].strip())
    return actual


def verify_no_untracked_apply_collisions(repo: Path, delivery_paths: Sequence[str]) -> Dict[str, Any]:
    delivery = sorted(normalize_relative_path(path) for path in delivery_paths)
    untracked = _git_nul_paths(repo, ["ls-files", "--others", "--exclude-standard", "-z"], trusted=True)
    ignored = _git_nul_paths(repo, ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"], trusted=True)
    local = sorted(set(untracked + ignored))
    collisions = sorted({
        local_path
        for local_path in local
        if any(paths_overlap(local_path, delivery_path) for delivery_path in delivery)
    })
    if collisions:
        raise RepoSafetyError("apply would overwrite untracked or ignored user content: %s" % ", ".join(collisions))
    return {"delivery_paths": delivery, "untracked": untracked, "ignored": ignored, "collisions": []}


def create_worktree(repo: Path, worktree_path: Path, branch: str, base_head: str) -> None:
    """Create an isolated Git checkout with private metadata.

    The compatibility name is retained because run state and older callers use
    the term worktree.  This is deliberately a non-local clone, not a linked
    `git worktree`: writer Git config, hooks, refs, and object writes must not
    share the source repository's metadata.
    """
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    if worktree_path.exists():
        raise RepoSafetyError("worktree path already exists: %s" % worktree_path)
    _trusted_git(repo, ["clone", "--quiet", "--no-local", "--no-hardlinks", "--no-checkout", str(repo), str(worktree_path)])
    _trusted_git(worktree_path, ["remote", "remove", "origin"])
    _trusted_git(worktree_path, ["config", "user.name", "Astra Orchestrator"])
    _trusted_git(worktree_path, ["config", "user.email", "codex-native-orchestrator@local.invalid"])
    _trusted_git(worktree_path, ["checkout", "-q", "-b", branch, base_head])


def remove_isolated_checkout(path: Path, allowed_root: Path) -> None:
    path = Path(path)
    allowed_root = Path(allowed_root).resolve()
    if path.is_symlink():
        raise RepoSafetyError("refusing to remove a symlink checkout: %s" % path)
    resolved = path.resolve()
    try:
        resolved.relative_to(allowed_root)
    except ValueError as exc:
        raise RepoSafetyError("checkout is outside the run worktree root: %s" % resolved) from exc
    if resolved == allowed_root or not resolved.is_dir():
        raise RepoSafetyError("refusing unsafe checkout removal: %s" % resolved)
    shutil.rmtree(str(resolved))
    fsync_directory(resolved.parent)


def worktree_snapshot(worktree: Path) -> Dict[str, Any]:
    snapshot = repo_snapshot(worktree)
    snapshot["path"] = str(Path(worktree).resolve())
    return snapshot


def changed_paths(worktree: Path, base_head: str) -> List[str]:
    # `--no-renames` exposes both endpoints of a move, preventing an
    # outside->inside rename from hiding an out-of-scope deletion.  NUL
    # delimiters preserve newlines and other unusual filename characters.
    return _git_nul_paths(worktree, ["diff", "--no-renames", "--name-only", "-z", base_head, "HEAD"], trusted=True)


def pending_writer_paths(worktree: Path, base_head: str) -> Dict[str, List[str]]:
    tracked = _git_nul_paths(worktree, ["diff", "--no-renames", "--name-only", "-z", base_head, "--"], trusted=True)
    untracked = _git_nul_paths(worktree, ["ls-files", "--others", "--exclude-standard", "-z"], trusted=True)
    ignored = _git_nul_paths(worktree, ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"], trusted=True)
    return {"changed": sorted(set(tracked + untracked)), "tracked": tracked, "untracked": untracked, "ignored": ignored}


def _path_has_symlink_component(root: Path, relative: str) -> bool:
    current = Path(root)
    for piece in PurePosixPath(relative).parts:
        current = current / piece
        if current.is_symlink():
            return True
    return False


def _path_is_gitlink(repo: Path, revision: str, relative: str) -> bool:
    raw = _git_bytes(repo, ["ls-tree", "-z", revision, "--", relative]).stdout
    for item in raw.split(b"\0"):
        if item.startswith(b"160000 "):
            return True
    return False


def validate_writer_path_set(worktree: Path, base_head: str, changed: Sequence[str], owned_paths: Sequence[str]) -> List[str]:
    if not changed:
        raise RepoSafetyError("writer produced no auditable file changes")
    normalized = [normalize_relative_path(path) for path in changed]
    if len(set(normalized)) != len(normalized):
        raise RepoSafetyError("writer changed path set contains duplicates")
    for path in normalized:
        if not path_is_owned(path, owned_paths):
            raise RepoSafetyError("writer changed path outside owned_paths: %s" % path)
        if _path_has_symlink_component(worktree, path):
            raise RepoSafetyError("writer path traverses a symlink: %s" % path)
        if _path_is_gitlink(worktree, base_head, path):
            raise RepoSafetyError("writer path targets a submodule: %s" % path)
    return sorted(normalized)


def writer_evidence(
    worktree: Path,
    base_head: str,
    owned_paths: Sequence[str],
    input_hash: Optional[str] = None,
    dependency_hash: Optional[str] = None,
    plan_hash: Optional[str] = None,
    manifest_hash: Optional[str] = None,
    tool_versions: Optional[Mapping[str, Any]] = None,
    model_output_hash: Optional[str] = None,
    staged_diff_hash: Optional[str] = None,
    baseline_metadata_hash: Optional[str] = None,
) -> Dict[str, Any]:
    snapshot = worktree_snapshot(worktree)
    if not snapshot["clean"]:
        raise WriterUncertainty("writer worktree has uncommitted changes; preserving it: %s" % snapshot["status"].strip())
    ignored = _git_nul_paths(worktree, ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"], trusted=True)
    if ignored:
        raise WriterUncertainty("writer worktree contains ignored side effects; preserving it: %s" % ", ".join(ignored))
    commit_count_text = _git(worktree, ["rev-list", "--count", "%s..HEAD" % base_head]).stdout.strip()
    try:
        commit_count = int(commit_count_text)
    except ValueError as exc:
        raise RepoSafetyError("could not determine writer commit count") from exc
    if snapshot["head"] == base_head or commit_count != 1:
        raise RepoSafetyError("writer must produce exactly one commit; observed %d" % commit_count)
    changed = changed_paths(worktree, base_head)
    if not changed:
        raise RepoSafetyError("writer commit contains no changed paths")
    normalized = validate_writer_path_set(worktree, base_head, changed, owned_paths)
    tree = _trusted_git(worktree, ["rev-parse", "HEAD^{tree}"]).stdout.strip()
    diff_bytes = _trusted_git_bytes(worktree, ["diff", "--no-ext-diff", "--no-color", "--binary", base_head, "HEAD"]).stdout
    actual_diff_hash = sha256_bytes(diff_bytes)
    if staged_diff_hash is not None and staged_diff_hash != actual_diff_hash:
        raise RepoSafetyError("writer committed diff does not match controller-staged evidence")
    evidence = {
        "repo_root": snapshot["repo_root"],
        "git_dir": snapshot["git_dir"],
        "branch": snapshot["branch"],
        "base_head": base_head,
        "head": snapshot["head"],
        "clean": snapshot["clean"],
        "changed_paths": normalized,
        "owned_paths": list(owned_paths),
        "commit_count": commit_count,
        "tree": tree,
        "committed_diff_hash": actual_diff_hash,
        "commit_owner": "controller" if model_output_hash is not None else "external",
        "model_output_hash": model_output_hash,
        "staged_diff_hash": staged_diff_hash,
        "baseline_metadata_hash": baseline_metadata_hash,
        "input_hash": input_hash,
        "dependency_hash": dependency_hash,
        "plan_hash": plan_hash,
        "manifest_hash": manifest_hash,
        "tool_versions": dict(tool_versions or {}),
    }
    evidence["hash"] = sha256_json(evidence)
    return evidence


class EventJournal:
    def __init__(self, path: Path, run_id: str):
        self.path = Path(path)
        self.run_id = run_id

    def read(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        events: List[Dict[str, Any]] = []
        previous_hash = ""
        expected_sequence = 1
        try:
            raw = self.path.read_bytes()
            if raw and not raw.endswith(b"\n"):
                raw = raw[: raw.rfind(b"\n") + 1] if b"\n" in raw else b""
            lines = raw.decode("utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise ControllerError("cannot read event journal: %s" % exc) from exc
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError as exc:
                raise ControllerError("event journal contains invalid JSON") from exc
            self._validate_event(event, self.run_id, expected_sequence, previous_hash)
            events.append(event)
            expected_sequence += 1
            previous_hash = event["event_hash"]
        return events

    @staticmethod
    def _validate_event(event: Any, run_id: str, sequence: int, previous_hash: str) -> None:
        if not isinstance(event, dict) or event.get("schema_version") != SCHEMA_VERSION:
            raise ControllerError("event journal contains an unknown event schema")
        required = ("event_id", "run_id", "sequence", "timestamp", "type", "payload", "prev_hash", "event_hash")
        if any(key not in event for key in required):
            raise ControllerError("event journal contains an incomplete event")
        if event["run_id"] != run_id or event["sequence"] != sequence or event["prev_hash"] != previous_hash:
            raise ControllerError("event journal sequence/hash chain is invalid")
        if event["type"] not in EVENT_TYPES:
            raise ControllerError("unknown event type in journal: %s" % event["type"])
        unsigned = dict(event)
        event_hash = unsigned.pop("event_hash")
        if sha256_json(unsigned) != event_hash:
            raise ControllerError("event journal hash chain is invalid")

    def append(self, event_type: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        if self.path.exists():
            raw = self.path.read_bytes()
            if raw and not raw.endswith(b"\n"):
                complete = raw[: raw.rfind(b"\n") + 1] if b"\n" in raw else b""
                atomic_write_bytes(self.path, complete, mode=0o600)
        events = self.read()
        if event_type not in EVENT_TYPES:
            raise ControllerError("cannot write unknown event type: %s" % event_type)
        previous_hash = events[-1]["event_hash"] if events else ""
        event: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "event_id": str(uuid.uuid4()),
            "run_id": self.run_id,
            "sequence": len(events) + 1,
            "timestamp": utc_now(),
            "type": event_type,
            "payload": dict(payload),
            "prev_hash": previous_hash,
        }
        event["event_hash"] = sha256_json(event)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as handle:
            handle.write(canonical_json(event) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(str(self.path), 0o600)
        fsync_directory(self.path.parent)
        return event


class RunStore:
    def __init__(self, codex_home: Path):
        # Keep the historical namespace so this package can read and resume
        # runs created by astra-orchestrator without moving durable state.
        self.root = resolve_codex_home(codex_home) / "astra-orchestrator"
        self.runs = self.root / "runs"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runs.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(str(self.root), 0o700)
        os.chmod(str(self.runs), 0o700)

    def run_path(self, run_id: str) -> Path:
        if not RUN_ID_RE.match(run_id):
            raise ValidationError("invalid run id")
        return self.runs / run_id

    def load(self, run_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        path = self.run_path(run_id)
        state = read_json(path / "state.json")
        plan = read_json(path / "plan.json")
        if not isinstance(state, dict) or not isinstance(plan, dict):
            raise ControllerError("run files must contain JSON objects")
        if state.get("schema_version") != SCHEMA_VERSION or plan.get("schema_version") != SCHEMA_VERSION:
            raise ControllerError("run uses an unsupported schema version")
        if sha256_json(plan) != state.get("plan_hash"):
            raise ControllerError("saved plan hash does not match run state")
        EventJournal(path / "events.jsonl", run_id).read()
        return state, plan

    def save_state(self, run_id: str, state: Mapping[str, Any]) -> None:
        path = self.run_path(run_id)
        atomic_write_json(path / "state.json", dict(state), mode=0o600)

    def create(self, plan: Mapping[str, Any], repo: Mapping[str, Any], manifest: Mapping[str, Any], run_id: Optional[str] = None) -> str:
        value = run_id or ("run-" + uuid.uuid4().hex[:16])
        path = self.run_path(value)
        if path.exists():
            raise ControllerError("run already exists: %s" % value)
        path.mkdir(parents=True, mode=0o700)
        (path / "results").mkdir(mode=0o700)
        (path / "inputs").mkdir(mode=0o700)
        (path / "evidence").mkdir(mode=0o700)
        (path / "worktrees").mkdir(mode=0o700)
        normalized = dict(plan)
        tasks = {}
        for task in normalized["tasks"]:
            tasks[task["task_id"]] = {
                "task_id": task["task_id"],
                "role": task["role"],
                "state": "pending",
                "status": "pending",
                "attempts": 0,
                "retry_count": 0,
                "read_only": task["read_only"],
                "writes": task["writes"],
                "owned_paths": task["owned_paths"],
                "worktree": None,
                "evidence": None,
                "error": None,
                "input_hash": None,
                "dependency_hash": None,
                "requested_runtime": None,
                "observed_runtime": None,
                "thread_id": None,
                "launch_released": False,
                "commit_broker": None,
            }
        manifest_payload = {k: v for k, v in manifest.items() if k not in {"manifest_path"}}
        state = {
            "schema_version": SCHEMA_VERSION,
            "run_id": value,
            "plan_id": normalized["plan_id"],
            "state": "planned",
            "status": "planned",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "repo": dict(repo),
            "plan_hash": sha256_json(normalized),
            "manifest_hash": sha256_json(manifest_payload),
            "tool_versions": {"python": sys.version.split()[0], "codex": command_version("codex"), "controller_schema": SCHEMA_VERSION},
            "gate_mode": normalized["gate_mode"],
            "hard_risk": normalized["hard_risk"],
            "tasks": tasks,
            "gate": None,
            "review": None,
            "integration": None,
            "usage": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "observed": False},
            "applied_tasks": [],
            "applied_commit": None,
            "error": None,
        }
        atomic_write_json(path / "plan.json", normalized, mode=0o600)
        atomic_write_json(path / "state.json", state, mode=0o600)
        EventJournal(path / "events.jsonl", value).append("run_created", {"plan_hash": state["plan_hash"], "manifest_hash": state["manifest_hash"], "gate_mode": state["gate_mode"]})
        return value

    def lock(self, run_id: str) -> FileLock:
        return FileLock(self.run_path(run_id) / "run.lock")


RUN_TRANSITIONS = {
    "planned": {"running", "needs_input", "cancelled", "failed"},
    "running": {"running", "needs_input", "completed", "failed", "cancelled"},
    "needs_input": {"running", "needs_input", "failed", "cancelled", "completed"},
    "completed": {"applying", "needs_input", "failed", "cancelled", "cleaned"},
    "applying": {"applying", "applied", "needs_input", "failed"},
    "applied": {"cleaned", "applied"},
    "failed": {"running", "cancelled", "failed"},
    "cancelled": {"cleaned", "cancelled"},
    "cleaned": {"cleaned"},
}

TASK_TRANSITIONS = {
    "pending": {"ready", "running", "blocked", "cancelled", "stale"},
    "ready": {"pending", "running", "needs_input", "blocked", "cancelled", "stale"},
    "running": {"pending", "succeeded", "failed", "needs_input", "blocked", "cancelled", "stale"},
    "succeeded": {"succeeded", "stale"},
    "failed": {"pending", "failed", "blocked", "cancelled", "stale"},
    "needs_input": {"pending", "needs_input", "succeeded", "blocked", "cancelled", "stale"},
    # Dependency-blocked nodes may become schedulable again after an explicit
    # resume successfully re-queues the failed/read-only ancestor.
    "blocked": {"pending", "blocked", "cancelled"},
    "cancelled": {"cancelled"},
    "stale": {"pending", "running", "needs_input", "blocked", "cancelled", "stale"},
}


def transition_run(store: RunStore, state: Dict[str, Any], new_state: str, event_type: str, payload: Optional[Mapping[str, Any]] = None) -> None:
    old = state["state"]
    if new_state not in RUN_TRANSITIONS.get(old, set()):
        raise ControllerError("invalid run state transition %s -> %s" % (old, new_state))
    details = dict(payload or {})
    if new_state in {"failed", "needs_input"}:
        reason = details.get("reason", details.get("error"))
        if reason:
            state["error"] = str(reason)
    elif new_state in {"running", "completed", "applied"}:
        state["error"] = None
    state["state"] = new_state
    state["status"] = new_state
    state["updated_at"] = utc_now()
    store.save_state(state["run_id"], state)
    EventJournal(store.run_path(state["run_id"]) / "events.jsonl", state["run_id"]).append(event_type, dict(details, from_state=old, to_state=new_state))


def transition_task(store: RunStore, state: Dict[str, Any], task_id: str, new_state: str, event_type: str, payload: Optional[Mapping[str, Any]] = None) -> None:
    task = state["tasks"][task_id]
    old = task["state"]
    if new_state not in TASK_TRANSITIONS.get(old, set()):
        raise ControllerError("invalid task state transition %s -> %s for %s" % (old, new_state, task_id))
    task["state"] = new_state
    task["status"] = new_state
    state["updated_at"] = utc_now()
    store.save_state(state["run_id"], state)
    EventJournal(store.run_path(state["run_id"]) / "events.jsonl", state["run_id"]).append(event_type, dict(payload or {}, task_id=task_id, from_state=old, to_state=new_state))


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("text", "content", "message", "error", "output", "result", "response"):
            if key in value:
                text = _extract_text(value[key])
                if text:
                    return text
    if isinstance(value, list):
        return "\n".join(text for text in (_extract_text(item) for item in value) if text)
    return ""


def parse_codex_events(stdout: str) -> Dict[str, Any]:
    events: List[Dict[str, Any]] = []
    final: Any = None
    thread_id = None
    usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    usage_observed = False
    observed_runtime = {"provider": "unknown", "model": "unknown", "reasoning_effort": "unknown", "sandbox_mode": "unknown"}
    if not isinstance(stdout, str):
        raise UnknownCodexEventError("Codex output is not text")
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError as exc:
            raise UnknownCodexEventError("Codex emitted non-JSON output") from exc
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise UnknownCodexEventError("Codex emitted an object without a type")
        if event["type"] not in CODEX_EVENT_TYPES:
            raise UnknownCodexEventError("unknown Codex JSON event type: %s" % event["type"])
        events.append(event)
        event_usage = event.get("usage")
        if isinstance(event_usage, Mapping):
            usage_observed = True
            for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
                value = event_usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    usage[key] = max(usage[key], value)
            total = event_usage.get("total_tokens")
            if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
                usage["total_tokens"] = max(usage["total_tokens"], total)
        for source_key, target_key in (("provider", "provider"), ("provider_id", "provider"), ("model", "model"), ("reasoning_effort", "reasoning_effort"), ("sandbox_mode", "sandbox_mode")):
            value = event.get(source_key)
            if isinstance(value, str) and value:
                observed_runtime[target_key] = value
        if event["type"] == "thread.started":
            thread_id = event.get("thread_id", event.get("id"))
        if event["type"] in {"result", "response.completed", "turn.completed"}:
            candidate = event.get("result", event.get("response", event.get("output")))
            if candidate is not None:
                final = candidate
        if event["type"] == "item.completed":
            item = event.get("item", {})
            if isinstance(item, Mapping) and item.get("type") in {"agent_message", "message"}:
                final = item.get("text", item.get("content", item))
        if event["type"] in {"message", "assistant_message"}:
            final = event.get("text", event.get("content", event))
    if not events:
        raise UnknownCodexEventError("Codex emitted no JSON events")
    failed = next((event for event in reversed(events) if event["type"] in {"error", "turn.failed", "response.failed", "turn.cancelled"}), None)
    if final is None:
        final = _extract_text(events[-1])
    if usage["total_tokens"] == 0:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return {
        "events": events,
        "thread_id": thread_id,
        "final": final,
        "failed_event": failed,
        "output_hash": sha256_bytes(stdout.encode("utf-8")),
        "usage": dict(usage, observed=usage_observed),
        "observed_runtime": observed_runtime,
    }


class CodexRunner:
    def __init__(self, codex_bin: str = "codex", schema_dir: Optional[Path] = None, invoke: Optional[Callable[..., Any]] = None, role_specs: Optional[Mapping[str, Mapping[str, Any]]] = None):
        self.codex_bin = codex_bin
        self.schema_dir = Path(schema_dir or SCHEMA_DIR)
        self._invoke_override = invoke
        self.role_specs = {name: dict(spec) for name, spec in (role_specs or DEFAULT_ROLE_SPECS).items()}

    def _call(self, prompt: str, role: str, read_only: bool, cwd: Path, output_schema: Path, timeout: Optional[float]) -> Dict[str, Any]:
        if self._invoke_override is not None:
            result = self._invoke_override(prompt=prompt, role=role, read_only=read_only, cwd=cwd, output_schema=output_schema, timeout=timeout)
            if not isinstance(result, dict):
                raise UnknownCodexEventError("fake Codex runner returned a non-object")
            return result
        spec = dict(DEFAULT_ROLE_SPECS[role])
        spec.update(self.role_specs.get(role, {}))
        sandbox = "read-only" if read_only else "workspace-write"
        command = [
            self.codex_bin,
            "--ask-for-approval", "never",
            "exec",
            "--strict-config",
            "--ephemeral",
            "--json",
            "--output-schema", str(output_schema),
            "--model", str(spec["model"]),
            "-c", 'model_reasoning_effort=%s' % json.dumps(str(spec["reasoning_effort"])),
            "--sandbox", sandbox,
        ]
        isolated = role == "guardian"
        if isolated:
            command.extend([
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--disable", "apps",
                "--disable", "browser_use",
                "--disable", "browser_use_external",
                "--disable", "browser_use_full_cdp_access",
                "--disable", "in_app_browser",
                "--disable", "hooks",
                "--disable", "plugins",
            ])
        command.append(prompt)
        home_context = tempfile.TemporaryDirectory(prefix="astra-guardian-home-") if isolated else contextlib.nullcontext(None)
        cwd_context = tempfile.TemporaryDirectory(prefix="astra-guardian-cwd-") if isolated else contextlib.nullcontext(None)
        auth_material_copied = False
        with home_context as temporary_directory, cwd_context as temporary_cwd_directory:
            environment = dict(os.environ)
            effective_cwd = Path(str(temporary_cwd_directory)) if isolated else Path(cwd)
            if isolated:
                temporary_home = Path(str(temporary_directory))
                os.chmod(str(temporary_home), 0o700)
                atomic_write_bytes(
                    temporary_home / "config.toml",
                    (
                        'approval_policy = "never"\n'
                        'sandbox_mode = "read-only"\n'
                        '[features]\n'
                        'apps = false\n'
                        'browser_use = false\n'
                        'browser_use_external = false\n'
                        'browser_use_full_cdp_access = false\n'
                        'in_app_browser = false\n'
                        'hooks = false\n'
                        'plugins = false\n'
                    ).encode("utf-8"),
                    mode=0o600,
                )
                source_auth = resolve_codex_home() / "auth.json"
                if source_auth.is_file() and not source_auth.is_symlink():
                    atomic_write_bytes(temporary_home / "auth.json", source_auth.read_bytes(), mode=0o600)
                    auth_material_copied = True
                environment["CODEX_HOME"] = str(temporary_home)
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(effective_cwd),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout,
                    env=environment,
                )
            except subprocess.TimeoutExpired as exc:
                error = "Codex invocation timed out"
                if read_only:
                    raise ControllerError(error) from exc
                raise WriterUncertainty(error) from exc
            except OSError as exc:
                error = "Codex invocation could not start: %s" % exc
                if read_only:
                    raise ControllerError(error) from exc
                raise WriterUncertainty(error) from exc
        parsed = parse_codex_events(completed.stdout)
        parsed["returncode"] = completed.returncode
        parsed["stderr"] = completed.stderr
        parsed["requested_runtime"] = {
            "model": str(spec["model"]),
            "reasoning_effort": str(spec["reasoning_effort"]),
            "sandbox_mode": sandbox,
            "approval_policy": "never",
            "ephemeral": True,
            "temporary_codex_home": isolated,
            "external_user_mcp_and_plugins_loaded": False if isolated else "unknown",
            "trusted_empty_cwd": isolated,
            "ignore_user_config": isolated,
            "ignore_rules": isolated,
            "skip_git_repo_check": isolated,
            "auth_material_copied": auth_material_copied if isolated else "unknown",
        }
        if parsed.get("failed_event") is not None:
            message = _extract_text(parsed["failed_event"]) or "Codex returned a terminal failure event"
            if read_only:
                raise ControllerError(message)
            raise WriterUncertainty(message)
        if completed.returncode != 0 and parsed.get("failed_event") is None:
            if read_only:
                raise ControllerError("Codex exited with status %s" % completed.returncode)
            raise WriterUncertainty("Codex exited with status %s" % completed.returncode)
        return parsed

    def invoke(self, prompt: str, role: str, read_only: bool, cwd: Path, output_schema: Optional[Path] = None, timeout: Optional[float] = None, max_attempts: Optional[int] = None) -> Dict[str, Any]:
        if role not in ROLE_NAMES:
            raise ValidationError("unknown runner role: %s" % role)
        if not isinstance(read_only, bool):
            raise ValidationError("runner read_only must be a boolean")
        supplied_spec = self.role_specs.get(role, {})
        for field in ("model", "reasoning_effort"):
            if supplied_spec.get(field, DEFAULT_ROLE_SPECS[role][field]) != DEFAULT_ROLE_SPECS[role][field]:
                raise ValidationError("runner role %s differs from the fixed %s" % (role, field))
        requested_spec = dict(DEFAULT_ROLE_SPECS[role])
        requested_spec.update(supplied_spec)
        if (requested_spec.get("read_only") is True or requested_spec.get("sandbox_mode") == "read-only") and not read_only:
            raise ValidationError("runner role %s cannot be promoted out of read-only mode" % role)
        schema = Path(output_schema or (self.schema_dir / ("gate.schema.json" if role == "guardian" else "result.schema.json")))
        attempts = max_attempts if max_attempts is not None else (2 if read_only else 1)
        ceiling = 2 if read_only else 1
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1 or attempts > ceiling:
            raise ValidationError("runner max_attempts must be from 1 to %d" % ceiling)
        errors: List[str] = []
        for attempt in range(attempts):
            try:
                result = self._call(prompt, role, read_only, cwd, schema, timeout)
            except (ControllerError, UnknownCodexEventError, WriterUncertainty, OSError, subprocess.SubprocessError) as exc:
                errors.append(str(exc))
                if not read_only:
                    raise WriterUncertainty("writer result is uncertain; duplicate launch is prohibited: %s" % exc) from exc
                if attempt + 1 >= attempts:
                    qualifier = " after one retry" if attempts == 2 else ""
                    raise ControllerError("read-only Codex invocation failed%s: %s" % (qualifier, errors[-1])) from exc
                continue
            expected_runtime = {
                "model": requested_spec["model"],
                "reasoning_effort": requested_spec["reasoning_effort"],
                "sandbox_mode": "read-only" if read_only else "workspace-write",
            }
            for runtime_kind in ("requested_runtime", "observed_runtime"):
                runtime = result.get(runtime_kind, {})
                if not isinstance(runtime, Mapping):
                    raise ValidationError("%s for %s is malformed" % (runtime_kind, role))
                for field, expected_value in expected_runtime.items():
                    actual = runtime.get(field, "unknown")
                    if actual not in {None, "", "unknown", expected_value}:
                        error = "%s for %s reports %s=%s; fixed value is %s" % (runtime_kind, role, field, actual, expected_value)
                        if not read_only:
                            raise WriterUncertainty(error)
                        raise ValidationError(error)
            result.setdefault("attempts", attempt + 1)
            return result
        raise ControllerError("Codex invocation failed: %s" % "; ".join(errors))


HybridRunner = CodexRunner


def parse_gate_response(result: Mapping[str, Any]) -> Dict[str, Any]:
    final = result.get("final", result.get("response", result))
    if isinstance(final, str):
        try:
            decoded = json.loads(final)
        except ValueError:
            decoded = None
        if isinstance(decoded, Mapping):
            final = decoded
    if not isinstance(final, Mapping):
        raise ValidationError("guardian response must be a structured object")
    data = dict(final)
    expected = {"Verdict", "Important findings", "Required changes", "Residual risks"}
    if set(data) != expected:
        raise ValidationError("guardian response fields do not match the strict contract")
    verdict = data["Verdict"]
    if not isinstance(verdict, str) or verdict not in {"approve", "revise", "block"}:
        raise ValidationError("guardian response has no valid Verdict")
    for field in ("Important findings", "Required changes", "Residual risks"):
        value = data[field]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValidationError("guardian response %s must be an array of strings" % field)
    return {
        "verdict": verdict,
        "important_findings": list(data["Important findings"]),
        "required_changes": list(data["Required changes"]),
        "residual_risks": list(data["Residual risks"]),
        "raw_hash": sha256_json(data),
    }


def _validate_test_evidence_item(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError("test evidence entry must be an object")
    expected = {"command", "exit_code", "output_hash", "artifact_hashes"}
    if set(value) != expected:
        raise ValidationError("test evidence fields do not match the strict contract")
    command = value["command"]
    exit_code = value["exit_code"]
    output_hash = value["output_hash"]
    artifacts = value["artifact_hashes"]
    if not isinstance(command, str) or not command.strip():
        raise ValidationError("test evidence command must be a non-empty string")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise ValidationError("test evidence exit_code must be an integer")
    if not isinstance(output_hash, str) or not re.match(r"^[0-9a-f]{64}$", output_hash):
        raise ValidationError("test evidence output_hash must be a SHA-256 digest")
    normalized_artifacts: Dict[str, str] = {}
    if isinstance(artifacts, Mapping):
        source_items = artifacts.items()
    elif isinstance(artifacts, list):
        source_items = []
        for item in artifacts:
            if not isinstance(item, Mapping) or set(item) != {"path", "sha256"}:
                raise ValidationError("test evidence artifact list entries must contain path and sha256")
            source_items.append((item["path"], item["sha256"]))
    else:
        raise ValidationError("test evidence artifact_hashes must be an object or typed array")
    for path, digest in source_items:
        normalized_path = normalize_relative_path(path)
        if normalized_path in normalized_artifacts:
            raise ValidationError("test evidence artifact paths must be unique")
        if not isinstance(digest, str) or not re.match(r"^[0-9a-f]{64}$", digest):
            raise ValidationError("test evidence artifact hash must be a SHA-256 digest")
        normalized_artifacts[normalized_path] = digest
    return {"command": command, "exit_code": exit_code, "output_hash": output_hash, "artifact_hashes": normalized_artifacts}


def validate_final_test_evidence(plan: Mapping[str, Any], state: Mapping[str, Any]) -> Any:
    writer_ids = {task["task_id"] for task in plan.get("tasks", []) if task.get("writes")}
    evidence = state.get("test_evidence")
    if not writer_ids:
        return evidence if isinstance(evidence, Mapping) else "not applicable"
    if not isinstance(evidence, Mapping) or not evidence:
        raise ValidationError("final writer gate requires concrete post-writer test evidence")
    tasks = _task_lookup(plan)
    qualified: Dict[str, Any] = {}
    for task_id, record in evidence.items():
        task = tasks.get(task_id)
        task_state = state.get("tasks", {}).get(task_id, {})
        if not task or task.get("role") != "tester" or not task.get("read_only") or task_state.get("state") != "succeeded":
            continue
        if not writer_ids.issubset(set(_writer_ancestors(plan, task_id))):
            continue
        entries = record.get("test_evidence") if isinstance(record, Mapping) else None
        if not isinstance(entries, list) or not entries:
            continue
        normalized = [_validate_test_evidence_item(item) for item in entries]
        if any(item["exit_code"] != 0 for item in normalized):
            continue
        qualified[task_id] = dict(record, test_evidence=normalized)
    if not qualified:
        raise ValidationError("final writer gate requires a successful read-only tester downstream of every writer")
    return qualified


def build_gate_packet(plan: Mapping[str, Any], state: Mapping[str, Any], mode: Optional[str] = None) -> Dict[str, Any]:
    selected_mode = mode or str(plan.get("gate_mode", "final"))
    if selected_mode not in {"pre", "final"}:
        raise ValidationError("guardian packet mode must be pre or final")
    packet = {
        "Mode": selected_mode,
        "Decision": plan.get("objective", ""),
        "Risk classification": {"hard_risk": bool(plan.get("hard_risk")), "hard_risk_reasons": plan.get("hard_risk_reasons", [])},
        "Key invariants": [
            "source repository identity, branch, HEAD, and clean state are verified immediately before mutation",
            "writer worktrees are isolated and owned paths do not overlap",
            "unknown Codex JSON events fail closed and writer uncertainty is never retried",
        ],
        "Test evidence": state.get("test_evidence", "not applicable"),
        "Controller isolation request": {
            "launcher": "codex --ask-for-approval never exec --strict-config --ephemeral --ignore-user-config --ignore-rules --skip-git-repo-check --sandbox read-only",
            "model": DEFAULT_ROLE_SPECS["guardian"]["model"],
            "reasoning_effort": DEFAULT_ROLE_SPECS["guardian"]["reasoning_effort"],
            "sandbox_mode": "read-only",
            "temporary_minimal_codex_home": True,
            "trusted_empty_cwd": True,
            "user_mcp_plugins_and_hooks_loaded": False,
        },
        "Observed runtime": "unknown until emitted by the Codex runtime; requested values are not execution proof",
        "Residual risks": state.get("error") or "none recorded",
    }
    if selected_mode == "pre":
        packet["Planned changes"] = [
            {"task_id": task["task_id"], "role": task["role"], "objective": task["objective"], "owned_paths": task["owned_paths"], "writes": task["writes"]}
            for task in plan.get("tasks", [])
        ]
    else:
        integration = state.get("integration")
        if integration:
            diff_path = Path(str(integration.get("diff_path", "")))
            if not diff_path.is_file() or sha256_file(diff_path) != integration.get("diff_hash"):
                raise ControllerError("final guardian packet requires a verified deterministic diff")
            packet["Final diff"] = diff_path.read_text(encoding="utf-8", errors="surrogateescape")
            packet["Final diff hash"] = integration["diff_hash"]
            required_integration = {"base_head", "delivery_commit", "tree", "changed_paths", "diff_hash", "evidence_hash"}
            if any(not integration.get(field) for field in required_integration):
                raise ControllerError("final guardian packet requires complete integration evidence")
            packet["Final integration evidence"] = {
                field: integration[field]
                for field in ("base_head", "delivery_commit", "tree", "changed_paths", "diff_hash", "evidence_hash", "writer_commits")
            }
        else:
            packet["Final diff"] = "read-only run; no repository diff"
            packet["Final diff hash"] = None
            packet["Final integration evidence"] = "not applicable"
        packet["Test evidence"] = (
            validate_final_test_evidence(plan, state)
            if plan.get("gate_mode") == "final"
            else state.get("test_evidence", "not applicable")
        )
        packet["Reviewer result"] = state.get("review") or "not applicable"
    return packet


def validate_guardian_runtime(result: Mapping[str, Any]) -> None:
    requested = result.get("requested_runtime")
    if not isinstance(requested, Mapping):
        raise ValidationError("guardian result has no controller runtime evidence")
    expected = {
        "model": DEFAULT_ROLE_SPECS["guardian"]["model"],
        "reasoning_effort": DEFAULT_ROLE_SPECS["guardian"]["reasoning_effort"],
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
    for key, value in expected.items():
        if requested.get(key) != value:
            raise ValidationError("guardian requested runtime mismatch for %s" % key)
    observed = result.get("observed_runtime", {})
    if not isinstance(observed, Mapping):
        raise ValidationError("guardian observed runtime evidence is malformed")
    comparisons = {
        "model": DEFAULT_ROLE_SPECS["guardian"]["model"],
        "reasoning_effort": DEFAULT_ROLE_SPECS["guardian"]["reasoning_effort"],
        "sandbox_mode": "read-only",
    }
    for key, expected_value in comparisons.items():
        actual = observed.get(key, "unknown")
        if actual not in {None, "", "unknown", expected_value}:
            raise ValidationError("guardian observed runtime mismatch for %s: %s" % (key, actual))


def run_guardian(store: RunStore, state: Dict[str, Any], plan: Mapping[str, Any], repo: Path, runner: CodexRunner, waiver: Optional[str] = None, mode: Optional[str] = None) -> Dict[str, Any]:
    selected_mode = mode or str(plan.get("gate_mode", "none"))
    if selected_mode not in {"pre", "final"}:
        raise ValidationError("guardian may run only for pre or final mode")
    run_path = store.run_path(state["run_id"])
    journal = EventJournal(run_path / "events.jsonl", state["run_id"])
    existing = state.get("gate")
    if existing is None:
        packet = build_gate_packet(plan, state, mode=selected_mode)
        packet_bytes = canonical_json(packet)
        packet_hash = sha256_bytes(packet_bytes)
        gate_id = "gate-" + uuid.uuid4().hex[:16]
        gate = {
            "schema_version": SCHEMA_VERSION,
            "gate_id": gate_id,
            "mode": selected_mode,
            "packet_hash": packet_hash,
            "retry_count": 0,
            "invocation_count": 0,
            "waiver": waiver,
            "status": "pending",
            "requested_runtime": {"model": DEFAULT_ROLE_SPECS["guardian"]["model"], "reasoning_effort": DEFAULT_ROLE_SPECS["guardian"]["reasoning_effort"], "sandbox_mode": "read-only", "approval_policy": "never", "ephemeral": True, "temporary_codex_home": True, "external_user_mcp_and_plugins_loaded": False, "trusted_empty_cwd": True, "ignore_user_config": True, "ignore_rules": True, "skip_git_repo_check": True},
            "observed_runtime": {"model": "unknown", "reasoning_effort": "unknown", "sandbox_mode": "unknown"},
        }
        state["gate"] = gate
        store.save_state(state["run_id"], state)
        atomic_write_bytes(run_path / "inputs" / "guardian.json", packet_bytes + b"\n", mode=0o600)
        journal.append("gate_started", {"gate_id": gate_id, "mode": selected_mode, "packet_hash": packet_hash})
    else:
        if not isinstance(existing, Mapping) or existing.get("status") != "pending":
            raise ControllerError("run already consumed its single logical Astra gate")
        gate = dict(existing)
        gate_id = str(gate.get("gate_id", ""))
        packet_path = run_path / "inputs" / "guardian.json"
        try:
            persisted = packet_path.read_bytes()
        except OSError as exc:
            raise ControllerError("pending guardian packet is unavailable") from exc
        packet_bytes = persisted[:-1] if persisted.endswith(b"\n") else persisted
        packet_hash = sha256_bytes(packet_bytes)
        if packet_hash != gate.get("packet_hash") or gate.get("mode") != selected_mode:
            raise ControllerError("pending guardian packet does not match durable gate state")
        try:
            persisted_packet = json.loads(packet_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ControllerError("pending guardian packet is malformed") from exc
        if canonical_json(persisted_packet) != packet_bytes:
            raise ControllerError("pending guardian packet is not canonical")
    prompt = packet_bytes.decode("utf-8")
    start_count = int(gate.get("invocation_count", 0))
    if start_count < 0 or start_count > 2:
        raise ControllerError("pending guardian invocation count is invalid")
    if start_count >= 2:
        gate["status"] = "unavailable"
        gate["retry_count"] = 1
        state["gate"] = gate
        store.save_state(state["run_id"], state)
        atomic_write_json(run_path / "results" / "guardian.json", {"gate": gate, "packet_hash": packet_hash}, mode=0o600)
        journal.append("gate_unavailable", {"gate_id": gate_id, "packet_hash": packet_hash, "retry_count": 1, "marker": "Astra gate unavailable", "recovered": True})
        return gate
    for invocation_index in range(start_count, 2):
        try:
            gate["invocation_count"] = invocation_index + 1
            gate["retry_count"] = max(0, invocation_index)
            state["gate"] = gate
            store.save_state(state["run_id"], state)
            result = runner.invoke(
                prompt=prompt,
                role="guardian",
                read_only=True,
                cwd=repo,
                output_schema=SCHEMA_DIR / "gate.schema.json",
                timeout=float(plan.get("guardian_timeout", 900)),
                max_attempts=1,
            )
            validate_guardian_runtime(result)
            parsed = parse_gate_response(result)
            gate.update({
                "status": parsed["verdict"],
                "retry_count": max(0, invocation_index),
                "response_hash": parsed["raw_hash"],
                "important_findings": parsed["important_findings"],
                "required_changes": parsed["required_changes"],
                "residual_risks": parsed["residual_risks"],
                "observed_runtime": result.get("observed_runtime", gate["observed_runtime"]),
            })
            state["gate"] = gate
            store.save_state(state["run_id"], state)
            atomic_write_json(run_path / "results" / "guardian.json", {"gate": gate, "packet_hash": packet_hash}, mode=0o600)
            journal.append("gate_completed", {"gate_id": gate_id, "packet_hash": packet_hash, "verdict": parsed["verdict"], "retry_count": max(0, invocation_index)})
            return gate
        except (ControllerError, ValueError, OSError, subprocess.SubprocessError) as exc:
            gate["retry_count"] = min(invocation_index, 1)
            gate["last_error"] = str(exc)
            state["gate"] = gate
            store.save_state(state["run_id"], state)
            if invocation_index == 1:
                gate["status"] = "unavailable"
                gate["retry_count"] = 1
                state["gate"] = gate
                store.save_state(state["run_id"], state)
                atomic_write_json(run_path / "results" / "guardian.json", {"gate": gate, "packet_hash": packet_hash}, mode=0o600)
                journal.append("gate_unavailable", {"gate_id": gate_id, "packet_hash": packet_hash, "retry_count": 1, "marker": "Astra gate unavailable"})
                return gate
    return gate


def _task_lookup(plan: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {task["task_id"]: task for task in plan["tasks"]}


def _dependency_hash(state: Mapping[str, Any], task: Mapping[str, Any]) -> str:
    values = {}
    for dependency in task.get("depends_on", []):
        dependency_state = state.get("tasks", {}).get(dependency, {})
        values[dependency] = {
            "state": dependency_state.get("state"),
            "evidence_hash": (dependency_state.get("evidence") or {}).get("hash"),
        }
    return sha256_json(values)


def _task_input_packet(plan: Mapping[str, Any], state: Mapping[str, Any], task: Mapping[str, Any], base_head: str) -> Dict[str, Any]:
    dependency_hash = _dependency_hash(state, task)
    packet = {
        "run_id": state["run_id"],
        "task_id": task["task_id"],
        "role": task["role"],
        "objective": task["objective"],
        "scope": task.get("scope", []),
        "non_goals": task.get("non_goals", []),
        "owned_paths": task.get("owned_paths", []),
        "acceptance_criteria": task.get("acceptance_criteria", []),
        "context_refs": task.get("context_refs", []),
        "write_mode": task["write_mode"],
        "base_head": base_head,
        "plan_hash": state["plan_hash"],
        "manifest_hash": state["manifest_hash"],
        "tool_versions": state.get("tool_versions", {}),
        "dependency_hash": dependency_hash,
        "dependency_evidence": {
            dependency: (state["tasks"][dependency].get("evidence") or {}).get("hash")
            for dependency in task.get("depends_on", [])
        },
        "requirements": [
            "Stay inside the supplied scope and do not delegate.",
            "Report requested runtime separately from observed runtime; use unknown for values that cannot be observed.",
            "Return the structured result required by the output schema.",
        ],
        "result_contract": {
            "schema_version": SCHEMA_VERSION,
            "run_id": state["run_id"],
            "task_id": task["task_id"],
            "role": task["role"],
            "outcome": "succeeded | failed | needs_input | cancelled",
            "summary": "concise outcome",
            "decisions": [],
            "modified_files": [],
            "commit_sha": "commit SHA or null",
            "commands": [],
            "tests": [],
            "test_evidence": [{"command": "exact command", "exit_code": 0, "output_hash": "SHA-256", "artifact_hashes": [{"path": "relative/path", "sha256": "SHA-256"}]}],
            "findings": [],
            "residual_risks": [],
            "follow_up_actions": [],
            "requested": {"model": "requested model", "effort": "requested effort", "sandbox": "requested sandbox"},
            "observed": {"model": "observed model or unknown", "effort": "observed effort or unknown", "sandbox": "observed sandbox or unknown"},
        },
    }
    if task["writes"]:
        packet["requirements"].extend([
            "Modify only owned_paths.",
            "Do not stage or commit. Git metadata is read-only to the model; return commit_sha as null.",
            "Report modified_files exactly; the trusted controller will verify, stage, and create the only commit.",
            "Remove ignored build artifacts before returning success; hidden side effects fail closed.",
        ])
    else:
        packet["requirements"].append("Remain read-only and do not modify the checkout.")
    return packet


def _decode_structured_final(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except ValueError:
        return value
    return decoded


_TASK_RESULT_LIST_FIELDS = (
    "decisions",
    "modified_files",
    "commands",
    "tests",
    "findings",
    "residual_risks",
    "follow_up_actions",
)


def validate_task_result(value: Any, run_id: str, task: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate model output independently of the remote output-schema layer."""
    if not isinstance(value, Mapping):
        raise ValidationError("task returned no structured result object")
    data = dict(value)
    required = {
        "schema_version", "run_id", "task_id", "role", "outcome", "summary",
        "decisions", "modified_files", "commit_sha", "commands", "tests", "test_evidence",
        "findings", "residual_risks", "follow_up_actions", "requested", "observed",
    }
    if set(data) != required:
        missing = sorted(required - set(data))
        extra = sorted(set(data) - required)
        raise ValidationError("task result fields do not match the contract; missing=%s extra=%s" % (missing, extra))
    if data["schema_version"] != SCHEMA_VERSION:
        raise ValidationError("task result schema_version does not match")
    identities = {"run_id": run_id, "task_id": task["task_id"], "role": task["role"]}
    for key, expected in identities.items():
        if data.get(key) != expected:
            raise ValidationError("task result %s mismatch: expected %r, got %r" % (key, expected, data.get(key)))
    if data.get("outcome") not in {"succeeded", "failed", "needs_input", "cancelled"}:
        raise ValidationError("task result has an invalid outcome")
    if not isinstance(data.get("summary"), str):
        raise ValidationError("task result summary must be a string")
    for field in _TASK_RESULT_LIST_FIELDS:
        items = data.get(field)
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise ValidationError("task result %s must be an array of strings" % field)
    test_evidence = data.get("test_evidence")
    if not isinstance(test_evidence, list):
        raise ValidationError("task result test_evidence must be an array")
    data["test_evidence"] = [_validate_test_evidence_item(item) for item in test_evidence]
    commit_sha = data.get("commit_sha")
    if commit_sha is not None and (not isinstance(commit_sha, str) or not re.match(r"^[0-9a-f]{40,64}$", commit_sha)):
        raise ValidationError("task result commit_sha must be null or a full hexadecimal commit id")
    normalized_files = [normalize_relative_path(path) for path in data["modified_files"]]
    if len(set(normalized_files)) != len(normalized_files):
        raise ValidationError("task result modified_files contains duplicates")
    data["modified_files"] = sorted(normalized_files)
    for field in ("requested", "observed"):
        runtime = data.get(field)
        if not isinstance(runtime, Mapping) or set(runtime) != {"model", "effort", "sandbox"}:
            raise ValidationError("task result %s runtime object is invalid" % field)
        if any(not isinstance(runtime[key], str) or not runtime[key] for key in runtime):
            raise ValidationError("task result %s runtime values must be non-empty strings" % field)
    return data


def _merge_usage(state: Dict[str, Any], result: Mapping[str, Any]) -> None:
    observed = result.get("usage")
    if not isinstance(observed, Mapping):
        return
    target = state.setdefault("usage", {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "observed": False})
    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
        value = observed.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            target[key] = int(target.get(key, 0)) + value
    target["observed"] = bool(target.get("observed")) or bool(observed.get("observed"))


def _writer_ancestors(plan: Mapping[str, Any], task_id: str) -> List[str]:
    tasks = _task_lookup(plan)
    found = set()
    stack = list(tasks[task_id].get("depends_on", []))
    while stack:
        current = stack.pop()
        if current in found:
            continue
        found.add(current)
        stack.extend(tasks[current].get("depends_on", []))
    return sorted(item for item in found if tasks[item].get("writes"))


class Orchestrator:
    def __init__(self, repo: Path, codex_home: Optional[Path] = None, runner: Optional[CodexRunner] = None):
        self.repo = Path(repo).resolve()
        self.codex_home = resolve_codex_home(codex_home)
        self.manifest = load_manifest(self.repo, required=False)
        self.store = RunStore(self.codex_home)
        self.runner = runner or CodexRunner(role_specs=self.manifest.get("roles"))

    def _manifest_matches_run(self, state: Mapping[str, Any]) -> bool:
        current_hash = sha256_json({key: value for key, value in self.manifest.items() if key != "manifest_path"})
        return current_hash == state.get("manifest_hash")

    def _require_installed_topology(self) -> None:
        report = global_doctor(self.repo, self.codex_home, self.manifest)
        checks = report["checks"]
        drift = [name for name in ("roles", "config", "project_overrides") if not checks[name]["ok"]]
        if drift:
            raise ValidationError("installed fixed topology differs from the manifest: %s" % ", ".join(drift))

    def validate(self, plan_path: Path) -> Dict[str, Any]:
        plan = read_json(Path(plan_path).resolve())
        return validate_plan(plan, manifest=self.manifest)

    def run(self, plan_path: Path, run_id: Optional[str] = None, dry_run: bool = False) -> Dict[str, Any]:
        normalized = validate_plan(read_json(Path(plan_path).resolve()), manifest=self.manifest)
        if dry_run:
            return {"ok": True, "dry_run": True, "plan": normalized, "plan_hash": sha256_json(normalized)}
        self._require_installed_topology()
        snapshot = repo_snapshot(self.repo)
        has_writers = any(task["writes"] for task in normalized["tasks"])
        if has_writers and not snapshot["clean"]:
            raise RepoSafetyError("writer DAG requires a clean source repository: %s" % snapshot["status"].strip())
        value = self.store.create(normalized, snapshot, self.manifest, run_id=run_id)
        state, plan = self.store.load(value)
        with self.store.lock(value):
            transition_run(self.store, state, "running", "run_resumed", {"reason": "execution started"})
            if plan["gate_mode"] == "pre":
                if not self._consume_guardian(state, plan):
                    return {"ok": True, "run_id": value, "state": state["state"], "gate": state.get("gate"), "tasks": state["tasks"]}
            self._execute_ready(value, state, plan, self.manifest)
        state, plan = self.store.load(value)
        return {"ok": True, "run_id": value, "state": state["state"], "gate": state.get("gate"), "tasks": state["tasks"]}

    def _consume_guardian(self, state: Dict[str, Any], plan: Mapping[str, Any]) -> bool:
        existing = state.get("gate")
        cwd = Path((state.get("integration") or {}).get("path", self.repo))
        if isinstance(existing, Mapping) and existing.get("status") in {"approve", "waived"}:
            return True
        if isinstance(existing, Mapping) and existing.get("status") not in {"pending", "unavailable"}:
            state["error"] = "single Astra gate already consumed with status %s" % existing.get("status", "unknown")
            self.store.save_state(state["run_id"], state)
            if state["state"] != "needs_input":
                transition_run(self.store, state, "needs_input", "run_needs_input", {"reason": state["error"]})
            return False
        if isinstance(existing, Mapping) and existing.get("status") == "unavailable":
            gate = dict(existing)
        else:
            gate = run_guardian(self.store, state, plan, cwd, self.runner, mode=str(plan["gate_mode"]))
        if gate["status"] == "approve":
            return True
        if gate["status"] == "unavailable":
            state["error"] = "Astra gate unavailable; an explicit waiver is required to continue"
        else:
            state["error"] = "guardian verdict %s" % gate["status"]
        self.store.save_state(state["run_id"], state)
        if state["state"] != "needs_input":
            transition_run(self.store, state, "needs_input", "run_needs_input", {"reason": state["error"]})
        return False

    def _ensure_integration(self, state: Dict[str, Any]) -> Dict[str, Any]:
        if state.get("integration"):
            return state["integration"]
        verify_repo(self.repo, state["repo"], require_clean=True)
        run_id = state["run_id"]
        path = self.store.run_path(run_id) / "worktrees" / "integration"
        branch = "astra/%s/integration" % re.sub(r"[^A-Za-z0-9.-]", "-", run_id)[:48]
        create_worktree(self.repo, path, branch, state["repo"]["head"])
        state["integration"] = {
            "path": str(path),
            "branch": branch,
            "backend": "isolated-clone",
            "base_head": state["repo"]["head"],
            "head": state["repo"]["head"],
            "writer_commits": [],
            "delivery_commit": None,
            "verified": False,
        }
        self.store.save_state(run_id, state)
        EventJournal(self.store.run_path(run_id) / "events.jsonl", run_id).append("integration_created", {"path": str(path), "branch": branch, "base_head": state["repo"]["head"]})
        return state["integration"]

    def _prepare_task(self, state: Dict[str, Any], plan: Mapping[str, Any], task: Mapping[str, Any]) -> Dict[str, Any]:
        run_id = state["run_id"]
        run_path = self.store.run_path(run_id)
        task_id = task["task_id"]
        task_state = state["tasks"][task_id]
        worktree = None
        if task["writes"]:
            integration = self._ensure_integration(state)
            verify_repo(self.repo, state["repo"], require_clean=True)
            base_head = integration["head"]
            branch = "astra/%s/%s" % (
                re.sub(r"[^A-Za-z0-9.-]", "-", run_id)[:32],
                re.sub(r"[^A-Za-z0-9.-]", "-", task_id)[:32],
            )
            worktree = run_path / "worktrees" / task_id
            create_worktree(self.repo, worktree, branch, base_head)
            baseline_metadata = git_protected_snapshot(worktree)
            task_state["worktree"] = {
                "path": str(worktree),
                "branch": branch,
                "backend": "isolated-clone",
                "base_head": base_head,
                "thread_id": None,
                "baseline_metadata": baseline_metadata,
                "baseline_metadata_hash": sha256_json(baseline_metadata),
            }
            cwd = worktree
        else:
            integration = state.get("integration")
            cwd = Path(integration["path"]) if integration else self.repo
            base_head = integration["head"] if integration else state["repo"]["head"]
        packet = _task_input_packet(plan, state, task, base_head)
        input_hash = sha256_json(packet)
        task_state["input_hash"] = input_hash
        task_state["dependency_hash"] = packet["dependency_hash"]
        atomic_write_json(run_path / "inputs" / (task_id + ".json"), packet, mode=0o600)
        self.store.save_state(run_id, state)
        return {
            "task": task,
            "cwd": cwd,
            "base_head": base_head,
            "packet": packet,
            "input_hash": input_hash,
            "baseline_metadata": (task_state.get("worktree") or {}).get("baseline_metadata"),
        }

    def _run_task_process(self, state_snapshot: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
        task = context["task"]
        role = task["role"]
        role_config = role_spec(self.manifest, role)
        requested = {
            "model": role_config["model"],
            "reasoning_effort": role_config["reasoning_effort"],
            "sandbox_mode": "read-only" if task["read_only"] else "workspace-write",
        }
        result = self.runner.invoke(
            prompt=canonical_json(context["packet"]).decode("utf-8"),
            role=role,
            read_only=task["read_only"],
            cwd=Path(context["cwd"]),
            output_schema=SCHEMA_DIR / "result.schema.json",
            timeout=float(task["timeout"]),
            # Node retries are owned by the durable scheduler so every real
            # process launch is reflected in state.json and events.jsonl.
            max_attempts=1,
        )
        output_hash = result.get("output_hash", sha256_json(result))
        structured = validate_task_result(
            _decode_structured_final(result.get("final", result)),
            str(state_snapshot["run_id"]),
            task,
        )
        result_record = {
            "schema_version": SCHEMA_VERSION,
            "run_id": state_snapshot["run_id"],
            "task_id": task["task_id"],
            "role": role,
            "outcome": structured["outcome"],
            "attempts": result.get("attempts", 1),
            "output_hash": output_hash,
            "result": structured,
            "requested_runtime": result.get("requested_runtime", requested),
            "observed_runtime": result.get("observed_runtime", {"model": "unknown", "reasoning_effort": "unknown", "sandbox_mode": "unknown"}),
            "usage": result.get("usage", {"observed": False}),
            "input_hash": context["input_hash"],
            "dependency_hash": context["packet"]["dependency_hash"],
            "base_head": context["base_head"],
        }
        if structured["outcome"] != "succeeded":
            return {"record": result_record, "evidence": None, "raw": result}
        if task["writes"]:
            if structured["commit_sha"] is not None:
                raise ValidationError("writer model must not create or report a commit")
            baseline_metadata = context.get("baseline_metadata")
            if not isinstance(baseline_metadata, Mapping):
                raise ValidationError("writer baseline Git metadata is missing")
            current_metadata = git_protected_snapshot(Path(context["cwd"]))
            if current_metadata != baseline_metadata:
                raise WriterUncertainty("writer Git metadata changed inside the model sandbox")
            pending = pending_writer_paths(Path(context["cwd"]), str(context["base_head"]))
            if pending["ignored"]:
                raise WriterUncertainty("writer left ignored side effects: %s" % ", ".join(pending["ignored"]))
            changed = validate_writer_path_set(Path(context["cwd"]), str(context["base_head"]), pending["changed"], task["owned_paths"])
            reported_paths = sorted(normalize_relative_path(path) for path in structured["modified_files"])
            if reported_paths != changed:
                raise ValidationError("writer result modified_files do not match verified working-tree changes")
            evidence = None
        else:
            if structured["commit_sha"] is not None or structured["modified_files"]:
                raise ValidationError("read-only task reported modified files or a commit")
            evidence = {
                "read_only": True,
                "input_hash": context["input_hash"],
                "dependency_hash": context["packet"]["dependency_hash"],
                "base_head": context["base_head"],
                "plan_hash": state_snapshot["plan_hash"],
                "manifest_hash": state_snapshot["manifest_hash"],
                "tool_versions": state_snapshot.get("tool_versions", {}),
                "output_hash": output_hash,
                "requested_runtime": result_record["requested_runtime"],
                "observed_runtime": result_record["observed_runtime"],
            }
            evidence["hash"] = sha256_json(evidence)
        return {"record": result_record, "evidence": evidence, "raw": result}

    def _writer_commit_message(self, state: Mapping[str, Any], task: Mapping[str, Any], record: Mapping[str, Any]) -> str:
        return (
            "Astra orchestrator writer %s/%s\n\n"
            "Astra-Input-Hash: %s\n"
            "Astra-Output-Hash: %s\n"
        ) % (state["run_id"], task["task_id"], record["input_hash"], record["output_hash"])

    def _initialize_writer_broker(
        self,
        state: Dict[str, Any],
        task: Mapping[str, Any],
        record: Mapping[str, Any],
    ) -> Dict[str, Any]:
        task_state = state["tasks"][task["task_id"]]
        worktree_info = task_state.get("worktree")
        if not isinstance(worktree_info, Mapping):
            raise WriterUncertainty("writer checkout metadata is missing")
        structured = validate_task_result(record.get("result"), state["run_id"], task)
        if record.get("outcome") != "succeeded" or structured["outcome"] != "succeeded":
            raise WriterUncertainty("controller commit broker requires a successful model result")
        if structured["commit_sha"] is not None:
            raise WriterUncertainty("writer model attempted to own the Git commit")
        baseline = worktree_info.get("baseline_metadata")
        if not isinstance(baseline, Mapping):
            raise WriterUncertainty("writer baseline metadata is missing")
        broker = {
            "phase": "model_succeeded",
            "base_head": worktree_info.get("base_head"),
            "input_hash": record.get("input_hash"),
            "dependency_hash": record.get("dependency_hash"),
            "model_output_hash": record.get("output_hash"),
            "reported_paths": sorted(normalize_relative_path(path) for path in structured["modified_files"]),
            "baseline_metadata": dict(baseline),
            "baseline_metadata_hash": sha256_json(baseline),
            "commit_message": self._writer_commit_message(state, task, record),
        }
        task_state["commit_broker"] = broker
        self.store.save_state(state["run_id"], state)
        EventJournal(self.store.run_path(state["run_id"]) / "events.jsonl", state["run_id"]).append(
            "writer_model_completed",
            {"task_id": task["task_id"], "output_hash": record.get("output_hash")},
        )
        return broker

    def _load_writer_result(self, state: Mapping[str, Any], task: Mapping[str, Any]) -> Dict[str, Any]:
        path = self.store.run_path(state["run_id"]) / "results" / (task["task_id"] + ".json")
        record = read_json(path)
        if not isinstance(record, Mapping):
            raise WriterUncertainty("persisted writer result is malformed")
        required = {"run_id": state["run_id"], "task_id": task["task_id"], "role": task["role"], "outcome": "succeeded"}
        for key, expected in required.items():
            if record.get(key) != expected:
                raise WriterUncertainty("persisted writer result identity is invalid")
        validate_task_result(record.get("result"), state["run_id"], task)
        return dict(record)

    def _verify_writer_metadata_before_commit(self, worktree: Path, broker: Mapping[str, Any]) -> Dict[str, Any]:
        current = git_protected_snapshot(worktree)
        baseline = broker["baseline_metadata"]
        if _immutable_git_metadata(current) != _immutable_git_metadata(baseline):
            raise WriterUncertainty("writer changed protected Git configuration or hooks")
        if current["branch"] != baseline["branch"]:
            raise WriterUncertainty("writer branch changed before controller commit")
        return current

    def _verify_staged_writer(
        self,
        worktree: Path,
        base_head: str,
        expected_paths: Sequence[str],
    ) -> Dict[str, Any]:
        staged = _git_nul_paths(worktree, ["diff", "--cached", "--no-renames", "--name-only", "-z", base_head, "--"], trusted=True)
        if staged != sorted(expected_paths):
            raise WriterUncertainty("controller-staged paths do not match verified writer paths")
        unstaged = _git_nul_paths(worktree, ["diff", "--no-renames", "--name-only", "-z", "--"], trusted=True)
        untracked = _git_nul_paths(worktree, ["ls-files", "--others", "--exclude-standard", "-z"], trusted=True)
        ignored = _git_nul_paths(worktree, ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"], trusted=True)
        if unstaged or untracked or ignored:
            raise WriterUncertainty("writer checkout has uncommitted or ignored side effects after controller staging")
        tree = _trusted_git(worktree, ["write-tree"]).stdout.strip()
        diff_bytes = _trusted_git_bytes(worktree, ["diff", "--cached", "--no-ext-diff", "--no-color", "--binary", base_head, "--"]).stdout
        return {"tree": tree, "diff_hash": sha256_bytes(diff_bytes), "changed_paths": staged}

    def _commit_writer(
        self,
        state: Dict[str, Any],
        task: Mapping[str, Any],
        record: Mapping[str, Any],
    ) -> Dict[str, Any]:
        task_id = task["task_id"]
        task_state = state["tasks"][task_id]
        broker = task_state.get("commit_broker")
        if not isinstance(broker, dict):
            broker = self._initialize_writer_broker(state, task, record)
        worktree_info = task_state.get("worktree") or {}
        worktree = Path(str(worktree_info.get("path", "")))
        if not worktree.is_dir():
            raise WriterUncertainty("writer checkout is unavailable for controller commit")
        base_head = str(broker.get("base_head", ""))
        if not re.match(r"^[0-9a-f]{40,64}$", base_head):
            raise WriterUncertainty("writer controller base commit is invalid")
        current = self._verify_writer_metadata_before_commit(worktree, broker)
        phase = broker.get("phase")
        if phase not in {"model_succeeded", "verified", "staged", "committed"}:
            raise WriterUncertainty("writer controller commit phase is invalid")

        if current["head"] != base_head:
            if phase not in {"staged", "committed"}:
                raise WriterUncertainty("writer HEAD changed before the controller-owned commit phase")
            parent = _trusted_git(worktree, ["rev-parse", "HEAD^"]).stdout.strip()
            message = _trusted_git(worktree, ["log", "-1", "--format=%B", "HEAD"]).stdout.rstrip("\n")
            if parent != base_head or message != str(broker["commit_message"]).rstrip("\n"):
                raise WriterUncertainty("writer commit is not the expected controller-owned commit")
        else:
            if current["refs_hash"] != broker["baseline_metadata"]["refs_hash"]:
                raise WriterUncertainty("writer refs changed before controller commit")
            if phase == "model_succeeded":
                if current["index_tree"] != broker["baseline_metadata"]["index_tree"] or current["index_hash"] != broker["baseline_metadata"]["index_hash"]:
                    raise WriterUncertainty("writer index changed inside the model sandbox")
                pending = pending_writer_paths(worktree, base_head)
                if pending["ignored"]:
                    raise WriterUncertainty("writer left ignored side effects: %s" % ", ".join(pending["ignored"]))
                changed = validate_writer_path_set(worktree, base_head, pending["changed"], task["owned_paths"])
                if changed != broker["reported_paths"]:
                    raise WriterUncertainty("writer-reported paths do not match controller evidence")
                broker.update({"phase": "verified", "changed_paths": changed})
                self.store.save_state(state["run_id"], state)
                EventJournal(self.store.run_path(state["run_id"]) / "events.jsonl", state["run_id"]).append(
                    "writer_verified", {"task_id": task_id, "changed_paths": changed}
                )
                phase = "verified"
                current = self._verify_writer_metadata_before_commit(worktree, broker)
            if phase == "verified":
                expected_paths = list(broker["changed_paths"])
                if current["index_tree"] == broker["baseline_metadata"]["index_tree"]:
                    pathspec = b"".join(path.encode("utf-8", errors="surrogateescape") + b"\0" for path in expected_paths)
                    _trusted_git_bytes(
                        worktree,
                        ["--literal-pathspecs", "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul"],
                        input_bytes=pathspec,
                    )
                staged = self._verify_staged_writer(worktree, base_head, expected_paths)
                broker.update({"phase": "staged", "staged_tree": staged["tree"], "staged_diff_hash": staged["diff_hash"]})
                self.store.save_state(state["run_id"], state)
                EventJournal(self.store.run_path(state["run_id"]) / "events.jsonl", state["run_id"]).append(
                    "writer_staged", {"task_id": task_id, "tree": staged["tree"], "diff_hash": staged["diff_hash"]}
                )
                phase = "staged"
            if phase == "staged":
                staged = self._verify_staged_writer(worktree, base_head, broker["changed_paths"])
                if staged["tree"] != broker.get("staged_tree") or staged["diff_hash"] != broker.get("staged_diff_hash"):
                    raise WriterUncertainty("staged writer evidence changed before commit")
                commit = _trusted_git_bytes(
                    worktree,
                    ["commit-tree", staged["tree"], "-p", base_head],
                    input_bytes=str(broker["commit_message"]).encode("utf-8"),
                ).stdout.decode("ascii").strip()
                if not re.match(r"^[0-9a-f]{40,64}$", commit):
                    raise WriterUncertainty("controller commit-tree returned an invalid commit")
                _trusted_git(worktree, ["update-ref", "HEAD", commit, base_head])

        evidence = writer_evidence(
            worktree,
            base_head,
            task["owned_paths"],
            input_hash=record.get("input_hash"),
            dependency_hash=record.get("dependency_hash"),
            plan_hash=state["plan_hash"],
            manifest_hash=state["manifest_hash"],
            tool_versions=state.get("tool_versions", {}),
            model_output_hash=record.get("output_hash"),
            staged_diff_hash=broker.get("staged_diff_hash"),
            baseline_metadata_hash=broker.get("baseline_metadata_hash"),
        )
        if evidence["changed_paths"] != broker.get("changed_paths"):
            raise WriterUncertainty("controller commit paths changed after commit")
        broker.update({"phase": "committed", "commit_sha": evidence["head"], "evidence_hash": evidence["hash"]})
        task_state["commit_broker"] = broker
        self.store.save_state(state["run_id"], state)
        EventJournal(self.store.run_path(state["run_id"]) / "events.jsonl", state["run_id"]).append(
            "writer_committed", {"task_id": task_id, "commit_sha": evidence["head"], "evidence_hash": evidence["hash"]}
        )
        return evidence

    def _recover_writer_commit(self, state: Dict[str, Any], plan: Mapping[str, Any], task: Mapping[str, Any]) -> bool:
        task_id = task["task_id"]
        task_state = state["tasks"][task_id]
        try:
            record = self._load_writer_result(state, task)
            if not isinstance(task_state.get("commit_broker"), Mapping):
                self._initialize_writer_broker(state, task, record)
            evidence = self._commit_writer(state, task, record)
            atomic_write_json(self.store.run_path(state["run_id"]) / "evidence" / (task_id + ".json"), evidence, mode=0o600)
            integrated = any(item.get("task_id") == task_id for item in (state.get("integration") or {}).get("writer_commits", []))
            if not integrated:
                self._integrate_writer(state, task, evidence)
            task_state["evidence"] = evidence
            task_state["error"] = None
            if task_state["state"] != "succeeded":
                transition_task(self.store, state, task_id, "succeeded", "task_recovered", {"reason": "controller-owned commit reconciled", "evidence_hash": evidence["hash"]})
            return True
        except (ControllerError, OSError, ValueError) as exc:
            task_state["error"] = "writer controller-commit recovery requires input: %s" % exc
            if task_state["state"] != "needs_input":
                transition_task(self.store, state, task_id, "needs_input", "task_recovered", {"reason": task_state["error"], "worktree_preserved": True})
            else:
                self.store.save_state(state["run_id"], state)
            return False

    def _integrate_writer(self, state: Dict[str, Any], task: Mapping[str, Any], evidence: Mapping[str, Any]) -> None:
        integration = state.get("integration")
        if not integration:
            raise RepoSafetyError("writer result has no integration worktree")
        path = Path(integration["path"])
        snapshot = worktree_snapshot(path)
        if not snapshot["clean"] or snapshot["head"] != integration["head"]:
            raise WriterUncertainty("integration worktree is ambiguous; preserving it")
        worktree = Path(state["tasks"][task["task_id"]]["worktree"]["path"])
        actual = writer_evidence(
            worktree,
            evidence["base_head"],
            task["owned_paths"],
            input_hash=evidence.get("input_hash"),
            dependency_hash=evidence.get("dependency_hash"),
            plan_hash=state["plan_hash"],
            manifest_hash=state["manifest_hash"],
            tool_versions=state.get("tool_versions", {}),
            model_output_hash=evidence.get("model_output_hash"),
            staged_diff_hash=evidence.get("staged_diff_hash"),
            baseline_metadata_hash=evidence.get("baseline_metadata_hash"),
        )
        if actual["hash"] != evidence.get("hash"):
            raise RepoSafetyError("writer evidence changed before integration: %s" % task["task_id"])
        try:
            _trusted_git(path, ["fetch", "--quiet", "--no-tags", str(worktree), evidence["head"]])
            fetched_head = _trusted_git(path, ["rev-parse", "FETCH_HEAD"]).stdout.strip()
            if fetched_head != evidence["head"]:
                raise RepoSafetyError("fetched writer commit does not match evidence: %s" % task["task_id"])
            _trusted_git(path, ["cherry-pick", "--no-gpg-sign", fetched_head])
        except RepoSafetyError as exc:
            raise WriterUncertainty("integration conflict; worktrees preserved: %s" % exc) from exc
        after = worktree_snapshot(path)
        integration["head"] = after["head"]
        integration["writer_commits"].append({"task_id": task["task_id"], "source_commit": evidence["head"], "integrated_commit": after["head"], "evidence_hash": evidence["hash"]})
        integration["verified"] = False
        self.store.save_state(state["run_id"], state)
        EventJournal(self.store.run_path(state["run_id"]) / "events.jsonl", state["run_id"]).append("integration_commit_applied", {"task_id": task["task_id"], "source_commit": evidence["head"], "integration_head": after["head"]})

    def _mark_stale_evidence(self, state: Dict[str, Any], plan: Mapping[str, Any]) -> bool:
        changed = False
        tasks = _task_lookup(plan)
        for task_id in plan["topological_order"]:
            task_state = state["tasks"][task_id]
            if task_state["state"] != "succeeded":
                continue
            task = tasks[task_id]
            evidence = task_state.get("evidence") or {}
            current_dependency_hash = _dependency_hash(state, task)
            if evidence.get("dependency_hash") != current_dependency_hash:
                transition_task(self.store, state, task_id, "stale", "task_stale", {"reason": "dependency evidence changed"})
                changed = True
        return changed

    def _block_unrunnable_tasks(self, state: Dict[str, Any], plan: Mapping[str, Any]) -> None:
        """Propagate a terminal dependency state through the remaining DAG."""
        tasks = _task_lookup(plan)
        terminal_dependency = {"failed", "needs_input", "blocked", "cancelled", "stale"}
        # topological_order guarantees that a newly blocked parent is observed
        # by every transitive child later in this same pass.
        for task_id in plan["topological_order"]:
            task_state = state["tasks"][task_id]
            if task_state["state"] != "pending":
                continue
            dependency_states = [state["tasks"][dep]["state"] for dep in tasks[task_id]["depends_on"]]
            if any(value in terminal_dependency for value in dependency_states):
                transition_task(
                    self.store,
                    state,
                    task_id,
                    "blocked",
                    "task_blocked",
                    {"reason": "dependency did not succeed"},
                )

    def _finalize_integration(self, state: Dict[str, Any], plan: Mapping[str, Any]) -> None:
        integration = state.get("integration")
        if not integration or integration.get("delivery_commit"):
            return
        path = Path(integration["path"])
        snapshot = worktree_snapshot(path)
        if not snapshot["clean"] or snapshot["head"] != integration["head"]:
            raise WriterUncertainty("integration worktree changed before finalization")
        base = integration["base_head"]
        changed = _git_nul_paths(path, ["diff", "--no-renames", "--name-only", "-z", base, integration["head"]], trusted=True)
        writer_paths = [owned for task in plan["tasks"] if task["writes"] for owned in task["owned_paths"]]
        for changed_path in changed:
            if not path_is_owned(changed_path, writer_paths):
                raise RepoSafetyError("integration changed path outside all writer ownership: %s" % changed_path)
        tree = _trusted_git(path, ["rev-parse", "%s^{tree}" % integration["head"]]).stdout.strip()
        message = "Astra orchestrator integration %s\n" % state["run_id"]
        delivery = _trusted_git_bytes(path, ["commit-tree", tree, "-p", base], input_bytes=message.encode("utf-8")).stdout.decode("ascii").strip()
        if not re.match(r"^[0-9a-f]{40,64}$", delivery):
            raise RepoSafetyError("git commit-tree returned an invalid commit id")
        diff = _trusted_git_bytes(path, ["diff", "--no-ext-diff", "--no-color", "--binary", base, integration["head"]]).stdout
        diff_path = self.store.run_path(state["run_id"]) / "evidence" / "final.diff"
        atomic_write_bytes(diff_path, diff, mode=0o600)
        integration.update({
            "delivery_commit": delivery,
            "tree": tree,
            "changed_paths": sorted(changed),
            "diff_path": str(diff_path),
            "diff_hash": sha256_file(diff_path),
            "verified": True,
            "evidence_hash": sha256_json({"base_head": base, "delivery_commit": delivery, "tree": tree, "changed_paths": sorted(changed), "diff_hash": sha256_file(diff_path), "writer_commits": integration["writer_commits"], "plan_hash": state["plan_hash"], "manifest_hash": state["manifest_hash"], "tool_versions": state.get("tool_versions", {})}),
        })
        self.store.save_state(state["run_id"], state)
        EventJournal(self.store.run_path(state["run_id"]) / "events.jsonl", state["run_id"]).append("integration_finalized", {"delivery_commit": delivery, "evidence_hash": integration["evidence_hash"]})

    def _run_final_reviewer_review(self, state: Dict[str, Any], plan: Mapping[str, Any]) -> bool:
        if not state.get("integration"):
            return True
        packet = build_gate_packet(plan, state, mode="final")
        packet.pop("Controller isolation request", None)
        packet["Review kind"] = "ordinary independent integration review"
        packet["Integration"] = state["integration"]
        packet_hash = sha256_json(packet)
        existing = state.get("review")
        if isinstance(existing, Mapping) and existing.get("packet_hash") == packet_hash:
            return existing.get("verdict") == "approve"
        result = self.runner.invoke(
            prompt=canonical_json(packet).decode("utf-8"),
            role="reviewer",
            read_only=True,
            cwd=Path(state["integration"]["path"]),
            output_schema=SCHEMA_DIR / "gate.schema.json",
            timeout=float(plan.get("reviewer_timeout", 1800)),
            max_attempts=2,
        )
        parsed = parse_gate_response(result)
        review = {
            "kind": "integration",
            "packet_hash": packet_hash,
            "verdict": parsed["verdict"],
            "important_findings": parsed["important_findings"],
            "required_changes": parsed["required_changes"],
            "residual_risks": parsed["residual_risks"],
            "integration_evidence_hash": state["integration"]["evidence_hash"],
            "requested_runtime": {"model": DEFAULT_ROLE_SPECS["reviewer"]["model"], "reasoning_effort": DEFAULT_ROLE_SPECS["reviewer"]["reasoning_effort"], "sandbox_mode": "read-only"},
            "observed_runtime": result.get("observed_runtime", {"model": "unknown", "reasoning_effort": "unknown", "sandbox_mode": "unknown"}),
        }
        state["review"] = review
        self.store.save_state(state["run_id"], state)
        atomic_write_json(self.store.run_path(state["run_id"]) / "results" / "sol-review.json", review, mode=0o600)
        EventJournal(self.store.run_path(state["run_id"]) / "events.jsonl", state["run_id"]).append("review_completed", {"kind": "integration", "packet_hash": packet_hash, "verdict": parsed["verdict"]})
        return parsed["verdict"] == "approve"

    def _execute_ready(self, run_id: str, state: Dict[str, Any], plan: Dict[str, Any], manifest: Mapping[str, Any]) -> None:
        tasks = _task_lookup(plan)
        run_path = self.store.run_path(run_id)
        while state["state"] == "running":
            if self._mark_stale_evidence(state, plan):
                for task_id in plan["topological_order"]:
                    task_state = state["tasks"][task_id]
                    if task_state["state"] == "stale" and not task_state["writes"]:
                        transition_task(self.store, state, task_id, "pending", "task_recovered", {"reason": "rerun stale read-only evidence"})
                    elif task_state["state"] == "stale":
                        task_state["error"] = "writer evidence became stale; automatic relaunch is prohibited"
                        transition_task(self.store, state, task_id, "needs_input", "task_needs_input", {"reason": task_state["error"]})
                        transition_run(self.store, state, "needs_input", "run_needs_input", {"task_id": task_id})
                        return
            self._block_unrunnable_tasks(state, plan)
            ready = [
                task_id for task_id in plan["topological_order"]
                if state["tasks"][task_id]["state"] == "pending"
                and all(state["tasks"][dep]["state"] == "succeeded" for dep in tasks[task_id]["depends_on"])
            ]
            position = {task_id: index for index, task_id in enumerate(plan["topological_order"])}
            ready.sort(key=lambda task_id: (-int(plan.get("critical_path_rank", {}).get(task_id, 1)), position[task_id]))
            if not ready:
                break
            budget = plan.get("max_total_tokens")
            if budget is not None and state.get("usage", {}).get("total_tokens", 0) >= budget:
                transition_run(self.store, state, "needs_input", "run_needs_input", {"reason": "token budget reached before scheduling a new wave", "budget": budget})
                return
            wave = ready[: int(plan["max_concurrency"])]
            contexts: Dict[str, Dict[str, Any]] = {}
            try:
                for task_id in wave:
                    transition_task(self.store, state, task_id, "ready", "task_ready", {})
                    contexts[task_id] = self._prepare_task(state, plan, tasks[task_id])
            except (ControllerError, OSError) as exc:
                for prepared_id in wave:
                    prepared_state = state["tasks"][prepared_id]
                    if prepared_state["state"] != "ready":
                        continue
                    try:
                        worktree_info = prepared_state.get("worktree")
                        if worktree_info:
                            path = Path(worktree_info["path"])
                            baseline = worktree_info.get("baseline_metadata")
                            if not isinstance(baseline, Mapping) or git_protected_snapshot(path) != baseline or not worktree_snapshot(path)["clean"]:
                                raise WriterUncertainty("prepared checkout changed before process submission")
                            remove_isolated_checkout(path, run_path / "worktrees")
                            prepared_state["worktree"] = None
                        transition_task(self.store, state, prepared_id, "pending", "task_recovered", {"reason": "wave preparation failed before process submission"})
                    except (ControllerError, OSError) as cleanup_exc:
                        prepared_state["error"] = str(cleanup_exc)
                        transition_task(self.store, state, prepared_id, "needs_input", "task_recovered", {"reason": str(cleanup_exc), "worktree_preserved": True})
                state["error"] = str(exc)
                self.store.save_state(run_id, state)
                transition_run(self.store, state, "needs_input", "run_needs_input", {"reason": str(exc)})
                return
            outcomes: Dict[str, Any] = {}
            release = threading.Event()
            cancel_before_start = threading.Event()

            def run_after_release(snapshot: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
                release.wait()
                if cancel_before_start.is_set():
                    raise ControllerError("task launch cancelled before model process started")
                return self._run_task_process(snapshot, context)

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(wave))
            future_map: Dict[Any, str] = {}
            try:
                state_snapshot = json.loads(json.dumps(state))
                for task_id in wave:
                    future_map[executor.submit(run_after_release, state_snapshot, contexts[task_id])] = task_id
                for task_id in wave:
                    transition_task(self.store, state, task_id, "running", "task_started", {"role": tasks[task_id]["role"], "input_hash": contexts[task_id]["input_hash"], "submitted": True})
                    state["tasks"][task_id]["attempts"] += 1
                    state["tasks"][task_id]["launch_released"] = True
                self.store.save_state(run_id, state)
                release.set()
                for future in concurrent.futures.as_completed(future_map):
                    task_id = future_map[future]
                    try:
                        outcomes[task_id] = ("ok", future.result())
                    except Exception as exc:  # normalized below; worker futures must not mutate state
                        outcomes[task_id] = ("error", exc)
            except Exception as exc:
                cancel_before_start.set()
                release.set()
                for future in future_map:
                    future.cancel()
                executor.shutdown(wait=True)
                state["error"] = "task submission failed: %s" % exc
                self.store.save_state(run_id, state)
                transition_run(self.store, state, "needs_input", "run_needs_input", {"reason": state["error"]})
                return
            finally:
                release.set()
                executor.shutdown(wait=True)
            wave_failed = False
            wave_needs_input = False
            for task_id in wave:
                task = tasks[task_id]
                task_state = state["tasks"][task_id]
                outcome, value = outcomes[task_id]
                if outcome == "ok":
                    record = value["record"]
                    evidence = value["evidence"]
                    atomic_write_json(run_path / "results" / (task_id + ".json"), record, mode=0o600)
                    _merge_usage(state, value["raw"])
                    task_state["requested_runtime"] = record["requested_runtime"]
                    task_state["observed_runtime"] = record["observed_runtime"]
                    task_state["thread_id"] = value["raw"].get("thread_id")
                    task_state["result_summary"] = {
                        key: record["result"].get(key)
                        for key in ("summary", "commands", "tests", "test_evidence", "findings", "residual_risks", "follow_up_actions")
                    }
                    if task_state.get("worktree") is not None:
                        task_state["worktree"]["thread_id"] = task_state["thread_id"]
                    task_state["attempts"] = max(task_state["attempts"], int(record.get("attempts", 1)))
                    if record["outcome"] != "succeeded":
                        reason = "task reported %s: %s" % (record["outcome"], record["result"].get("summary", ""))
                        task_state["error"] = reason
                        if task["writes"] or record["outcome"] == "needs_input":
                            transition_task(self.store, state, task_id, "needs_input", "task_needs_input", {"reason": reason, "reported_outcome": record["outcome"], "worktree_preserved": task["writes"]})
                            wave_needs_input = True
                        elif record["outcome"] == "cancelled":
                            transition_task(self.store, state, task_id, "cancelled", "task_cancelled", {"reason": reason, "reported_outcome": record["outcome"]})
                            wave_needs_input = True
                        else:
                            transition_task(self.store, state, task_id, "failed", "task_failed", {"reason": reason, "reported_outcome": record["outcome"]})
                            wave_failed = True
                        continue
                    try:
                        if task["writes"]:
                            self._initialize_writer_broker(state, task, record)
                            evidence = self._commit_writer(state, task, record)
                            record["controller_commit_sha"] = evidence["head"]
                            atomic_write_json(run_path / "results" / (task_id + ".json"), record, mode=0o600)
                            self._integrate_writer(state, task, evidence)
                    except (ControllerError, OSError) as exc:
                        task_state["error"] = str(exc)
                        transition_task(self.store, state, task_id, "needs_input", "task_needs_input", {"error": str(exc), "preserved_worktree": True})
                        wave_needs_input = True
                        continue
                    atomic_write_json(run_path / "evidence" / (task_id + ".json"), evidence, mode=0o600)
                    task_state["evidence"] = evidence
                    task_state["error"] = None
                    transition_task(self.store, state, task_id, "succeeded", "task_succeeded", {"result_hash": record["output_hash"], "evidence_hash": evidence["hash"]})
                else:
                    exc = value
                    task_state["error"] = str(exc)
                    uncertain = isinstance(exc, WriterUncertainty) or task["writes"]
                    result_record = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "task_id": task_id, "role": task["role"], "outcome": "needs_input" if uncertain else "failed", "attempts": task_state["attempts"], "error": str(exc)}
                    atomic_write_json(run_path / "results" / (task_id + ".json"), result_record, mode=0o600)
                    if uncertain:
                        transition_task(self.store, state, task_id, "needs_input", "task_needs_input", {"error": str(exc), "duplicate_launch_prohibited": task["writes"]})
                        wave_needs_input = True
                    else:
                        max_attempts = int(task["retry_policy"]["max_attempts"])
                        if task_state["attempts"] < max_attempts:
                            task_state["retry_count"] = task_state["attempts"]
                            transition_task(
                                self.store,
                                state,
                                task_id,
                                "pending",
                                "retry",
                                {
                                    "error": str(exc),
                                    "attempt": task_state["attempts"],
                                    "max_attempts": max_attempts,
                                    "same_input_hash": task_state.get("input_hash"),
                                },
                            )
                        else:
                            transition_task(self.store, state, task_id, "failed", "task_failed", {"error": str(exc), "attempts": task_state["attempts"]})
                            wave_failed = True
            if wave_needs_input:
                self._block_unrunnable_tasks(state, plan)
                transition_run(self.store, state, "needs_input", "run_needs_input", {"reason": "one or more nodes need input"})
                return
            if wave_failed:
                self._block_unrunnable_tasks(state, plan)
                transition_run(self.store, state, "failed", "run_failed", {"reason": "one or more read-only nodes failed"})
                return
        if any(task_state["state"] in {"pending", "ready", "running", "stale"} for task_state in state["tasks"].values()):
            return
        if any(task_state["state"] in {"failed", "needs_input", "blocked", "cancelled"} for task_state in state["tasks"].values()):
            if state["state"] == "running":
                transition_run(self.store, state, "needs_input", "run_needs_input", {"reason": "not all tasks succeeded"})
            return
        try:
            self._finalize_integration(state, plan)
            state["test_evidence"] = {
                task_id: {
                    "summary": (task_state.get("result_summary") or {}).get("summary"),
                    "commands": (task_state.get("result_summary") or {}).get("commands", []),
                    "tests": (task_state.get("result_summary") or {}).get("tests", []),
                    "test_evidence": (task_state.get("result_summary") or {}).get("test_evidence", []),
                    "residual_risks": (task_state.get("result_summary") or {}).get("residual_risks", []),
                    "evidence_hash": (task_state.get("evidence") or {}).get("hash"),
                }
                for task_id, task_state in state["tasks"].items()
                if tasks[task_id]["role"] == "tester"
            } or "not applicable"
            self.store.save_state(run_id, state)
            if not self._run_final_reviewer_review(state, plan):
                state["error"] = "independent integration review requires revision"
                self.store.save_state(run_id, state)
                transition_run(self.store, state, "needs_input", "run_needs_input", {"reason": state["error"]})
                return
            if plan["gate_mode"] == "final" and not self._consume_guardian(state, plan):
                return
        except (ControllerError, OSError) as exc:
            state["error"] = str(exc)
            self.store.save_state(run_id, state)
            transition_run(self.store, state, "needs_input", "run_needs_input", {"reason": str(exc)})
            return
        transition_run(self.store, state, "completed", "run_completed", {"integration_commit": (state.get("integration") or {}).get("delivery_commit"), "gate_mode": plan["gate_mode"]})

    def resume(self, run_id: str, waiver: Optional[str] = None) -> Dict[str, Any]:
        with self.store.lock(run_id):
            state, plan = self.store.load(run_id)
            if state["state"] in {"completed", "applied", "cleaned", "cancelled"}:
                return {"ok": True, "run_id": run_id, "state": state["state"], "gate": state.get("gate"), "tasks": state["tasks"]}
            manifest_matches = self._manifest_matches_run(state)
            current_tools = {"python": sys.version.split()[0], "codex": command_version("codex"), "controller_schema": SCHEMA_VERSION}
            drift_reason = None
            if not manifest_matches:
                drift_reason = "manifest changed"
            elif current_tools != state.get("tool_versions"):
                drift_reason = "tool versions changed"
            if drift_reason:
                for task_id, task_state in state["tasks"].items():
                    if task_state["state"] == "succeeded":
                        transition_task(self.store, state, task_id, "stale", "task_stale", {"reason": drift_reason})
                if isinstance(state.get("gate"), dict):
                    state["gate"]["invalidated"] = drift_reason + " after gate"
                state["error"] = "%s; evidence is stale and no second Astra gate will be called automatically" % drift_reason
                self.store.save_state(run_id, state)
                if state["state"] != "needs_input":
                    transition_run(self.store, state, "needs_input", "run_needs_input", {"reason": state["error"]})
                return {"ok": True, "run_id": run_id, "state": state["state"], "needs_input": True, "error": state["error"]}
            self._require_installed_topology()
            if waiver is not None:
                if not isinstance(waiver, str) or not waiver.strip():
                    raise ValidationError("guardian waiver must be non-empty")
                if plan["gate_mode"] == "none":
                    raise ValidationError("a guardian waiver is not applicable to gate_mode none")
                if state.get("gate") is None:
                    state["gate"] = {"schema_version": SCHEMA_VERSION, "gate_id": "waiver-" + uuid.uuid4().hex[:16], "mode": plan["gate_mode"], "packet_hash": "", "retry_count": 0, "invocation_count": 0, "status": "waived", "waiver": waiver}
                else:
                    state["gate"]["status"] = "waived"
                    state["gate"]["waiver"] = waiver
                self.store.save_state(run_id, state)
            task_specs = _task_lookup(plan)
            for task_id, task_state in state["tasks"].items():
                max_attempts = int(task_specs[task_id]["retry_policy"]["max_attempts"])
                if task_state["state"] == "ready":
                    if task_state["writes"]:
                        worktree_info = task_state.get("worktree")
                        expected_path = self.store.run_path(run_id) / "worktrees" / task_id
                        if worktree_info:
                            path = Path(worktree_info["path"])
                            try:
                                snapshot = worktree_snapshot(path)
                            except ControllerError as exc:
                                task_state["error"] = "prepared writer checkout is invalid: %s" % exc
                                transition_task(self.store, state, task_id, "needs_input", "task_recovered", {"reason": task_state["error"], "worktree_preserved": True})
                                continue
                            baseline = worktree_info.get("baseline_metadata")
                            if not snapshot["clean"] or snapshot["head"] != worktree_info.get("base_head") or not isinstance(baseline, Mapping) or git_protected_snapshot(path) != baseline:
                                task_state["error"] = "prepared writer checkout changed before launch"
                                transition_task(self.store, state, task_id, "needs_input", "task_recovered", {"reason": task_state["error"], "worktree_preserved": True})
                                continue
                            if worktree_info.get("backend") == "isolated-clone":
                                remove_isolated_checkout(path, expected_path.parent)
                            else:
                                _git(self.repo, ["worktree", "remove", str(path)])
                        elif expected_path.exists():
                            remove_isolated_checkout(expected_path, expected_path.parent)
                        task_state["worktree"] = None
                    transition_task(self.store, state, task_id, "pending", "task_recovered", {"reason": "prepared task had not launched"})
                elif task_state["state"] == "running":
                    if task_state["writes"]:
                        result_path = self.store.run_path(run_id) / "results" / (task_id + ".json")
                        has_success_result = False
                        if result_path.is_file():
                            with contextlib.suppress(ControllerError):
                                candidate = read_json(result_path)
                                has_success_result = isinstance(candidate, Mapping) and candidate.get("outcome") == "succeeded"
                        if isinstance(task_state.get("commit_broker"), Mapping) or has_success_result:
                            self._recover_writer_commit(state, plan, task_specs[task_id])
                        else:
                            task_state["error"] = "writer process state is ambiguous; duplicate launch prohibited"
                            transition_task(self.store, state, task_id, "needs_input", "task_recovered", {"duplicate_launch_prohibited": True, "worktree_preserved": True})
                    elif task_state["attempts"] >= max_attempts:
                        task_state["error"] = "interrupted read-only node exhausted its retry budget"
                        transition_task(self.store, state, task_id, "failed", "task_failed", {"reason": task_state["error"], "attempts": task_state["attempts"]})
                    else:
                        task_state["error"] = "interrupted read-only node will reuse its saved input packet"
                        transition_task(self.store, state, task_id, "pending", "task_recovered", {"duplicate_launch_prohibited": False})
                elif task_state["state"] == "needs_input" and task_state["writes"]:
                    result_path = self.store.run_path(run_id) / "results" / (task_id + ".json")
                    has_success_result = False
                    if result_path.is_file():
                        with contextlib.suppress(ControllerError):
                            candidate = read_json(result_path)
                            has_success_result = isinstance(candidate, Mapping) and candidate.get("outcome") == "succeeded"
                    if isinstance(task_state.get("commit_broker"), Mapping) or has_success_result:
                        self._recover_writer_commit(state, plan, task_specs[task_id])
                elif task_state["state"] in {"failed", "needs_input"} and not task_state["writes"] and task_state["attempts"] < max_attempts:
                    transition_task(self.store, state, task_id, "pending", "task_recovered", {"reason": "explicit resume of read-only node"})
                elif task_state["state"] == "stale" and not task_state["writes"]:
                    transition_task(self.store, state, task_id, "pending", "task_recovered", {"reason": "explicit rerun of stale read-only evidence"})
            # All controller-created `blocked` states mean that a dependency
            # did not succeed.  Revive them in topological order only when the
            # dependency chain has itself been made runnable again.
            for task_id in plan["topological_order"]:
                task_state = state["tasks"][task_id]
                if task_state["state"] != "blocked":
                    continue
                dependency_states = [state["tasks"][dep]["state"] for dep in task_specs[task_id]["depends_on"]]
                if all(value in {"pending", "succeeded"} for value in dependency_states):
                    transition_task(self.store, state, task_id, "pending", "task_recovered", {"reason": "dependencies were re-queued"})
            if state["state"] in {"running", "needs_input", "failed", "planned"}:
                if any(task["state"] == "needs_input" and task["writes"] for task in state["tasks"].values()):
                    return {"ok": True, "run_id": run_id, "state": state["state"], "needs_input": True}
                if state["state"] != "running":
                    transition_run(self.store, state, "running", "run_resumed", {})
                if plan["gate_mode"] == "pre" and not self._consume_guardian(state, plan):
                    return {"ok": True, "run_id": run_id, "state": state["state"], "gate": state.get("gate"), "tasks": state["tasks"]}
                self._execute_ready(run_id, state, plan, self.manifest)
        state, _ = self.store.load(run_id)
        return {"ok": True, "run_id": run_id, "state": state["state"], "gate": state.get("gate"), "tasks": state["tasks"]}

    def status(self, run_id: str) -> Dict[str, Any]:
        state, _ = self.store.load(run_id)
        return state

    def cancel(self, run_id: str, reason: str = "cancelled by user") -> Dict[str, Any]:
        with self.store.lock(run_id):
            state, _ = self.store.load(run_id)
            if state["state"] in {"cancelled", "cleaned"}:
                return state
            for task_id, task_state in state["tasks"].items():
                if task_state["state"] in {"pending", "ready", "running", "stale", "blocked"}:
                    transition_task(self.store, state, task_id, "cancelled", "task_cancelled", {"reason": reason})
            transition_run(self.store, state, "cancelled", "run_cancelled", {"reason": reason})
        return self.status(run_id)

    def report(self, run_id: str, markdown: bool = False) -> Any:
        state, plan = self.store.load(run_id)
        events = EventJournal(self.store.run_path(run_id) / "events.jsonl", run_id).read()
        manifest_matches = self._manifest_matches_run(state)
        task_specs = _task_lookup(plan)
        requested = []
        observed = []
        checks = []
        uncertainties = []
        changed = set((state.get("integration") or {}).get("changed_paths", []))
        for task_id in plan["topological_order"]:
            task_spec = task_specs[task_id]
            task_state = state["tasks"].get(task_id, {})
            requested_runtime = task_state.get("requested_runtime") or {}
            if not requested_runtime:
                role = role_spec(self.manifest, task_spec["role"]) if manifest_matches else {}
                requested_runtime = {
                    "model": role.get("model", "unknown"),
                    "reasoning_effort": role.get("reasoning_effort", "unknown"),
                    "sandbox_mode": "read-only" if task_spec["read_only"] else "workspace-write",
                }
            observed_runtime = task_state.get("observed_runtime") or {}
            requested.append({
                "task_id": task_id,
                "role": task_spec["role"],
                "model": requested_runtime.get("model", "unknown"),
                "effort": requested_runtime.get("reasoning_effort", "unknown"),
                "sandbox": requested_runtime.get("sandbox_mode", "unknown"),
            })
            observed.append({
                "task_id": task_id,
                "provider": observed_runtime.get("provider", "unknown"),
                "model": observed_runtime.get("model", "unknown"),
                "effort": observed_runtime.get("reasoning_effort", "unknown"),
                "sandbox": observed_runtime.get("sandbox_mode", "unknown"),
            })
            evidence = task_state.get("evidence") or {}
            changed.update(evidence.get("changed_paths", []))
            summary = task_state.get("result_summary") or {}
            checks.append({
                "task_id": task_id,
                "status": task_state.get("state", "unknown"),
                "commands": summary.get("commands", []),
                "tests": summary.get("tests", []),
                "evidence": summary.get("test_evidence", []),
            })
            uncertainties.extend(summary.get("residual_risks", []))
            if observed[-1]["provider"] == "unknown" or observed[-1]["model"] == "unknown":
                uncertainties.append("Observed provider or model is unknown for task %s" % task_id)
        if state.get("error"):
            uncertainties.append(str(state["error"]))
        if not manifest_matches:
            uncertainties.append("Saved run manifest is unavailable or has changed")
        route = plan.get("route", "hybrid")
        report = {
            "schema_version": SCHEMA_VERSION,
            "mode": route,
            "run_id": run_id,
            "plan_id": plan["plan_id"],
            "route": route,
            "overrides": plan.get("overrides", []),
            "status": state["state"],
            "state": state["state"],
            "requested": requested,
            "observed": observed,
            "changed_paths": sorted(changed),
            "checks": checks,
            "uncertainties": uncertainties,
            "tasks": state["tasks"],
            "gate": state.get("gate"),
            "review": state.get("review"),
            "integration": state.get("integration"),
            "usage": state.get("usage"),
            "event_count": len(events),
            "plan_hash": state["plan_hash"],
            "manifest_hash": state.get("manifest_hash"),
            "residual_risk": state.get("error"),
        }
        if not markdown:
            return report
        lines = ["# Codex Native run %s" % run_id, "", "- Status: %s" % state["state"], "- Plan: %s" % plan["plan_id"], "- Mode: %s" % route, "- Overrides: %s" % (", ".join(plan.get("overrides", [])) or "none"), "- Gate mode: %s" % plan["gate_mode"], "- Events: %d" % len(events), "- Integration commit: %s" % ((state.get("integration") or {}).get("delivery_commit") or "none"), "- Uncertainties: %s" % ("; ".join(uncertainties) or "none"), "", "| Task | Role | Status | Requested model | Observed provider / model | Checks |", "| --- | --- | --- | --- | --- | --- |"]
        requested_by_id = {item["task_id"]: item for item in requested}
        observed_by_id = {item["task_id"]: item for item in observed}
        checks_by_id = {item["task_id"]: item for item in checks}
        for task_id in plan["topological_order"]:
            request = requested_by_id[task_id]
            runtime = observed_by_id[task_id]
            task_checks = checks_by_id[task_id]
            check_names = task_checks["commands"] + task_checks["tests"]
            lines.append("| %s | %s | %s | %s (%s) | %s / %s | %s |" % (task_id, request["role"], task_checks["status"], request["model"], request["effort"], runtime["provider"], runtime["model"], "; ".join(check_names) or "none"))
        rendered = "\n".join(lines) + "\n"
        atomic_write_bytes(self.store.run_path(run_id) / "report.md", rendered.encode("utf-8"), mode=0o600)
        return rendered

    def apply(self, run_id: str, waiver: Optional[str] = None) -> Dict[str, Any]:
        with self.store.lock(run_id):
            state, plan = self.store.load(run_id)
            if not self._manifest_matches_run(state):
                raise RepoSafetyError("manifest changed after validation; evidence is stale")
            if waiver:
                if not isinstance(waiver, str) or not waiver.strip():
                    raise ValidationError("guardian waiver must be non-empty")
                if plan["gate_mode"] == "none":
                    raise ValidationError("a guardian waiver is not applicable to gate_mode none")
                if state.get("gate") is None:
                    state["gate"] = {"schema_version": SCHEMA_VERSION, "gate_id": "waiver-" + uuid.uuid4().hex[:16], "mode": plan["gate_mode"], "packet_hash": "", "retry_count": 0, "invocation_count": 0, "status": "waived", "waiver": waiver}
                else:
                    state["gate"]["waiver"] = waiver
                    state["gate"]["status"] = "waived"
                self.store.save_state(run_id, state)
            gate = state.get("gate") or {}
            if plan["gate_mode"] != "none" and gate.get("status") not in {"approve", "waived"}:
                raise ControllerError("apply requires a valid guardian result or explicit waiver")
            if gate.get("status") == "waived" and state["state"] == "needs_input":
                if any(task.get("state") != "succeeded" for task in state.get("tasks", {}).values()):
                    raise ControllerError("guardian waiver cannot bypass an incomplete task")
                transition_run(self.store, state, "running", "run_resumed", {"reason": "explicit guardian waiver"})
                self._execute_ready(run_id, state, plan, self.manifest)
            if state["state"] not in {"completed", "applying", "applied"}:
                raise ControllerError("run is not ready to apply: %s" % state["state"])
            if state["state"] == "applied":
                return state
            integration = state.get("integration")
            if not integration:
                transition_run(self.store, state, "applying", "apply_started", {"waiver": gate.get("waiver"), "read_only_run": True})
                transition_run(self.store, state, "applied", "run_applied", {"applied_tasks": []})
                return state
            if not integration.get("verified") or not integration.get("delivery_commit") or not integration.get("evidence_hash"):
                raise RepoSafetyError("integration evidence is incomplete")
            reviewer_review = state.get("review") or {}
            if reviewer_review.get("verdict") != "approve" or reviewer_review.get("integration_evidence_hash") != integration.get("evidence_hash"):
                raise RepoSafetyError("Reviewer result is absent or stale for the integration evidence")
            if plan["gate_mode"] == "final" and gate.get("status") == "approve":
                current_gate_hash = sha256_json(build_gate_packet(plan, state, mode="final"))
                if current_gate_hash != gate.get("packet_hash"):
                    raise RepoSafetyError("final Astra conclusion is stale; a second gate or explicit waiver is required")
            current_tools = {"python": sys.version.split()[0], "codex": command_version("codex"), "controller_schema": SCHEMA_VERSION}
            if current_tools != state.get("tool_versions"):
                raise RepoSafetyError("tool versions changed after validation; evidence is stale")
            integration_snapshot = worktree_snapshot(Path(integration["path"]))
            if not integration_snapshot["clean"] or integration_snapshot["head"] != integration["head"]:
                raise RepoSafetyError("integration worktree changed after validation")
            integration_path = Path(integration["path"])
            delivery_parent = _git(integration_path, ["rev-parse", "%s^" % integration["delivery_commit"]]).stdout.strip()
            if delivery_parent != integration["base_head"]:
                raise RepoSafetyError("delivery commit is not based on the recorded source HEAD")
            for task in plan["tasks"]:
                if not task["writes"]:
                    continue
                evidence = state["tasks"][task["task_id"]].get("evidence")
                if not evidence:
                    raise RepoSafetyError("writer evidence is missing: %s" % task["task_id"])
                actual = writer_evidence(
                    Path(state["tasks"][task["task_id"]]["worktree"]["path"]),
                    evidence["base_head"],
                    task["owned_paths"],
                    input_hash=evidence.get("input_hash"),
                    dependency_hash=evidence.get("dependency_hash"),
                    plan_hash=state["plan_hash"],
                    manifest_hash=state["manifest_hash"],
                    tool_versions=state.get("tool_versions", {}),
                    model_output_hash=evidence.get("model_output_hash"),
                    staged_diff_hash=evidence.get("staged_diff_hash"),
                    baseline_metadata_hash=evidence.get("baseline_metadata_hash"),
                )
                if actual["hash"] != evidence.get("hash"):
                    raise RepoSafetyError("writer evidence changed: %s" % task["task_id"])
            actual_source = repo_snapshot(self.repo)
            if state["state"] == "applying" and actual_source["head"] == integration["delivery_commit"] and actual_source["clean"]:
                state["applied_commit"] = actual_source["head"]
                state["repo_after_apply"] = actual_source
                transition_run(self.store, state, "applied", "run_applied", {"applied_tasks": [task["task_id"] for task in plan["tasks"] if task["writes"]], "reconciled": True})
                return state
            verify_repo(self.repo, state["repo"], require_clean=True)
            verify_no_untracked_apply_collisions(self.repo, integration.get("changed_paths", []))
            local_filters = _trusted_git(
                self.repo,
                ["config", "--local", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$"],
                check=False,
            )
            if local_filters.returncode == 0 and local_filters.stdout.strip():
                raise RepoSafetyError("apply refuses repository-configured external Git filters")
            if state["state"] != "applying":
                transition_run(self.store, state, "applying", "apply_started", {"waiver": gate.get("waiver"), "delivery_commit": integration["delivery_commit"]})
            try:
                _trusted_git(self.repo, ["fetch", "--quiet", "--no-tags", str(integration_path), integration["delivery_commit"]])
                fetched_delivery = _trusted_git(self.repo, ["rev-parse", "FETCH_HEAD"]).stdout.strip()
                if fetched_delivery != integration["delivery_commit"]:
                    raise RepoSafetyError("fetched delivery commit does not match integration evidence")
                _trusted_git(self.repo, ["merge", "--ff-only", fetched_delivery])
            except RepoSafetyError as exc:
                state["error"] = str(exc)
                self.store.save_state(run_id, state)
                EventJournal(self.store.run_path(run_id) / "events.jsonl", run_id).append("apply_failed", {"error": str(exc), "delivery_commit": integration["delivery_commit"]})
                raise
            after = repo_snapshot(self.repo)
            if not after["clean"] or after["head"] != integration["delivery_commit"]:
                raise RepoSafetyError("post-apply repository verification failed")
            state["applied_tasks"] = [task["task_id"] for task in plan["tasks"] if task["writes"]]
            state["applied_commit"] = after["head"]
            state["repo_after_apply"] = after
            self.store.save_state(run_id, state)
            EventJournal(self.store.run_path(run_id) / "events.jsonl", run_id).append("apply_task_succeeded", {"kind": "integration", "head": after["head"]})
            transition_run(self.store, state, "applied", "run_applied", {"applied_tasks": state["applied_tasks"], "delivery_commit": after["head"]})
        return self.status(run_id)

    def cleanup(self, run_id: str) -> Dict[str, Any]:
        with self.store.lock(run_id):
            state, plan = self.store.load(run_id)
            if state["state"] not in {"applied", "completed"}:
                raise ControllerError("cleanup is limited to completed or applied runs; current state is %s" % state["state"])
            journal = EventJournal(self.store.run_path(run_id) / "events.jsonl", run_id)
            journal.append("cleanup_started", {})
            candidates = []
            allowed_root = self.store.run_path(run_id) / "worktrees"
            for task_id, task_state in state["tasks"].items():
                worktree_info = task_state.get("worktree")
                if not worktree_info:
                    continue
                path = Path(worktree_info["path"])
                if not path.exists():
                    continue
                candidates.append((task_id, path, worktree_info.get("backend", "linked-worktree")))
            integration = state.get("integration")
            if integration and Path(integration["path"]).exists():
                candidates.append(("integration", Path(integration["path"]), integration.get("backend", "linked-worktree")))
            dirty = []
            for label, path, _backend in candidates:
                try:
                    if not worktree_snapshot(path)["clean"]:
                        dirty.append(label)
                except ControllerError:
                    dirty.append(label)
            if dirty:
                for label in dirty:
                    journal.append("cleanup_preserved", {"task_id": label, "reason": "worktree has unsaved or ambiguous changes"})
                state["cleanup_preserved"] = dirty
                self.store.save_state(run_id, state)
                return state
            for label, path, backend in candidates:
                if backend == "isolated-clone":
                    remove_isolated_checkout(path, allowed_root)
                else:
                    _git(self.repo, ["worktree", "remove", str(path)])
                journal.append("cleanup_task_succeeded", {"task_id": label})
            transition_run(self.store, state, "cleaned", "run_cleaned", {})
        return self.status(run_id)


_TOML_KEY_PART = r'''(?:[A-Za-z0-9_-]+|"(?:\\.|[^"\\])*"|'(?:''|[^'])*')'''


def _normalize_toml_key_path(path: str) -> str:
    """Normalize bare, quoted, and dotted TOML key paths for comparisons."""
    parts: List[str] = []
    start = 0
    quote: Optional[str] = None
    escaped = False
    for index, char in enumerate(path):
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = None
            continue
        if quote == "'":
            if char == "'":
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == ".":
            part = path[start:index].strip()
            if not part:
                raise DeploymentError("invalid TOML key path: %s" % path)
            parts.append(part)
            start = index + 1
    part = path[start:].strip()
    if not part:
        raise DeploymentError("invalid TOML key path: %s" % path)
    parts.append(part)

    normalized: List[str] = []
    for part in parts:
        if part.startswith('"') and part.endswith('"'):
            try:
                normalized.append(json.loads(part))
            except (TypeError, ValueError) as exc:
                raise DeploymentError("invalid TOML quoted key: %s" % part) from exc
        elif part.startswith("'") and part.endswith("'"):
            normalized.append(part[1:-1].replace("''", "'"))
        else:
            normalized.append(part)
    return ".".join(normalized)


def _toml_key(path: str, table: str) -> str:
    normalized_path = _normalize_toml_key_path(path)
    normalized_table = _normalize_toml_key_path(table) if table else ""
    return (normalized_table + "." if normalized_table else "") + normalized_path


_TOML_ASSIGNMENT = re.compile(
    r"^(?P<indent>\s*)(?P<key>" + _TOML_KEY_PART + r"(?:\s*\.\s*" + _TOML_KEY_PART + r")*)(?P<between>\s*=\s*)(?P<value>.*?)(?P<newline>\r?\n)?$"
)
_TOML_TABLE = re.compile(r"^\s*\[(?!\[)(?P<table>[^\]\r\n]+)\]\s*(?:#.*)?(?:\r?\n)?$")


def _toml_value_without_comment(value: str) -> str:
    """Remove an inline TOML comment without treating '#' in a string as one."""
    quote: Optional[str] = None
    escaped = False
    for index, char in enumerate(value):
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = None
            continue
        if quote == "'":
            if char == "'":
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "#":
            return value[:index].rstrip()
    return value.rstrip()


def _scan_toml_value_continuation(
    value: str,
    multiline: Optional[str] = None,
    square_depth: int = 0,
    curly_depth: int = 0,
) -> Tuple[Optional[str], int, int]:
    """Track multiline strings and collection continuations conservatively.

    This is intentionally not a TOML parser.  It only prevents assignment-like
    text inside an unrelated multiline value from being mistaken for a real
    key while the complete document remains subject to Codex strict parsing.
    """
    index = 0
    quote: Optional[str] = None
    while index < len(value):
        if multiline is not None:
            closing = value.find(multiline, index)
            while closing >= 0 and multiline == '"""':
                slash_count = 0
                cursor = closing - 1
                while cursor >= 0 and value[cursor] == "\\":
                    slash_count += 1
                    cursor -= 1
                if slash_count % 2 == 0:
                    break
                closing = value.find(multiline, closing + 3)
            if closing < 0:
                return multiline, square_depth, curly_depth
            index = closing + 3
            multiline = None
            continue
        if quote is not None:
            char = value[index]
            if char == quote:
                if quote == "'":
                    quote = None
                else:
                    slash_count = 0
                    cursor = index - 1
                    while cursor >= 0 and value[cursor] == "\\":
                        slash_count += 1
                        cursor -= 1
                    if slash_count % 2 == 0:
                        quote = None
            index += 1
            continue
        if value.startswith("'''", index) or value.startswith('"""', index):
            multiline = value[index : index + 3]
            index += 3
            continue
        char = value[index]
        if char == "#":
            break
        if char in {"'", '"'}:
            quote = char
        elif char == "[":
            square_depth += 1
        elif char == "]":
            square_depth = max(0, square_depth - 1)
        elif char == "{":
            curly_depth += 1
        elif char == "}":
            curly_depth = max(0, curly_depth - 1)
        index += 1
    return multiline, square_depth, curly_depth


def _collect_managed_toml_scalars(text: str, wanted: Iterable[str]) -> Dict[str, str]:
    """Collect managed scalar assignments at real TOML table boundaries."""
    wanted_keys = set(wanted)
    observed: Dict[str, str] = {}
    table = ""
    multiline: Optional[str] = None
    square_depth = 0
    curly_depth = 0
    for line in text.splitlines(keepends=True):
        if multiline is not None or square_depth or curly_depth:
            multiline, square_depth, curly_depth = _scan_toml_value_continuation(
                line,
                multiline=multiline,
                square_depth=square_depth,
                curly_depth=curly_depth,
            )
            continue
        if line.lstrip().startswith("[["):
            table = "__unmanaged_array__"
            continue
        match_table = _TOML_TABLE.match(line)
        if match_table:
            table = _normalize_toml_key_path(match_table.group("table").strip())
            continue
        match = _TOML_ASSIGNMENT.match(line)
        if not match:
            continue
        key = _toml_key(match.group("key"), table)
        if key in wanted_keys:
            if key in observed:
                raise DeploymentError("duplicate TOML key: %s" % key)
            observed[key] = _toml_value_without_comment(match.group("value").strip())
        value_text = match.group("value").strip()
        multiline, square_depth, curly_depth = _scan_toml_value_continuation(value_text)
    return observed


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise DeploymentError("unsupported TOML deployment value: %r" % (value,))


def config_values_from_manifest(manifest: Mapping[str, Any]) -> Dict[str, Any]:
    if manifest.get("package_id") == "codex-native-orchestrator" and manifest.get("installation_scope") == "global":
        return dict(manifest["config_values"])
    deployment = manifest.get("deployment", {})
    candidates = []
    for key in ("config", "config_values", "codex_config"):
        if isinstance(manifest.get(key), Mapping):
            candidates.append(manifest[key])
    if isinstance(deployment, Mapping):
        for key in ("config", "config_values", "values"):
            if isinstance(deployment.get(key), Mapping):
                candidates.append(deployment[key])
    result: Dict[str, Any] = {}
    for candidate in candidates:
        def visit(prefix: str, value: Any) -> None:
            if isinstance(value, Mapping):
                for key, child in value.items():
                    visit(_toml_key(str(key), prefix), child)
            else:
                result[prefix] = value
        for key, value in candidate.items():
            visit(str(key), value)
    if not result:
        roles = manifest.get("roles", {})
        worker_spec = roles.get("worker", {}) if isinstance(roles, Mapping) else {}
        result = {
            "agents.enabled": True,
            "agents.max_concurrent_threads_per_session": manifest.get("max_concurrency", 4),
            "agents.default_subagent_model": worker_spec.get("model", DEFAULT_ROLE_SPECS["worker"]["model"]),
            "agents.default_subagent_reasoning_effort": worker_spec.get("reasoning_effort", DEFAULT_ROLE_SPECS["worker"]["reasoning_effort"]),
        }
    return result


def config_path_from_manifest(manifest: Mapping[str, Any]) -> str:
    deployment = manifest.get("deployment", {})
    if isinstance(deployment, Mapping) and isinstance(deployment.get("config_path"), str):
        return deployment["config_path"]
    value = manifest.get("config_path", ".codex/config.toml")
    if not isinstance(value, str):
        raise DeploymentError("manifest config_path must be a string")
    return value


def patch_toml_bytes(original: bytes, values: Mapping[str, Any], allowed_keys: Optional[Iterable[str]] = None) -> Tuple[bytes, Dict[str, Any]]:
    """Patch only simple TOML assignments while preserving all other bytes."""
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeploymentError("config is not UTF-8 TOML") from exc
    allowed = set(allowed_keys or values.keys())
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise DeploymentError("manifest requests disallowed config keys: %s" % ", ".join(unknown))
    lines = text.splitlines(keepends=True)
    seen: Dict[str, int] = {}
    table = ""
    replacements: Dict[int, str] = {}
    table_ranges: Dict[str, Tuple[int, int]] = {}
    seen_tables = set()
    table_starts: List[int] = []
    current_table_start = 0
    multiline: Optional[str] = None
    square_depth = 0
    curly_depth = 0
    for index, line in enumerate(lines):
        if multiline is not None or square_depth or curly_depth:
            multiline, square_depth, curly_depth = _scan_toml_value_continuation(
                line,
                multiline=multiline,
                square_depth=square_depth,
                curly_depth=curly_depth,
            )
            continue
        if line.lstrip().startswith("[["):
            if re.match(r"^\s*\[\[\s*agents(?:\.|\s*\]\])", line):
                raise DeploymentError("array-of-tables cannot define managed agents keys")
            if table:
                table_ranges[table] = (current_table_start, index)
            table = "__unmanaged_array__"
            current_table_start = index
            table_starts.append(index)
            continue
        match_table = _TOML_TABLE.match(line)
        if match_table:
            if table:
                table_ranges[table] = (current_table_start, index)
            table = _normalize_toml_key_path(match_table.group("table").strip())
            if table == "agents" and table in seen_tables:
                raise DeploymentError("duplicate TOML table: %s" % table)
            seen_tables.add(table)
            current_table_start = index
            table_starts.append(index)
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _TOML_ASSIGNMENT.match(line)
        if not match:
            # Unmanaged TOML may use dotted/quoted keys or multiline values.
            # Preserve it byte-for-byte; the patched full document is validated
            # by Codex before any deployment write occurs.
            continue
        key = _toml_key(match.group("key"), table)
        if key in seen and key in values:
            raise DeploymentError("duplicate TOML key: %s" % key)
        seen[key] = index
        value_text = match.group("value").strip()
        if value_text.startswith("{") or value_text.startswith("[") or value_text.startswith("'''") or value_text.startswith('"""'):
            if key in values:
                raise DeploymentError("unsupported TOML value for deployed key: %s" % key)
        if key in values:
            newline = match.group("newline") or ("\n" if text.endswith("\n") else "")
            comment = ""
            # Preserve a trailing comment without trying to parse arbitrary TOML.
            quote = None
            for pos, char in enumerate(value_text):
                if char in "'\"":
                    quote = None if quote == char else (char if quote is None else quote)
                elif char == "#" and quote is None:
                    comment = value_text[pos:].rstrip()
                    break
            suffix = (" " + comment) if comment else ""
            replacements[index] = match.group("indent") + match.group("key") + match.group("between") + _toml_scalar(values[key]) + suffix + newline
        multiline, square_depth, curly_depth = _scan_toml_value_continuation(value_text)
    if table:
        table_ranges[table] = (current_table_start, len(lines))
    for index, replacement in replacements.items():
        lines[index] = replacement
    missing = sorted(set(values) - set(seen))
    if missing:
        top_level = [key for key in missing if "." not in key]
        agent_keys = [key.split(".", 1)[1] for key in missing if key.startswith("agents.") and key.count(".") == 1]
        other = [key for key in missing if key not in top_level and key not in ["agents." + key for key in agent_keys]]
        if other:
            raise DeploymentError("cannot safely add unsupported TOML tables: %s" % ", ".join(other))
        insertions: Dict[int, str] = {}

        def add_insertion(index: int, content: str) -> None:
            if index == len(lines) and lines and not lines[-1].endswith(("\n", "\r")):
                content = "\n" + content
            insertions[index] = insertions.get(index, "") + content

        if top_level:
            insert_at = min(table_starts) if table_starts else len(lines)
            add_insertion(insert_at, "".join(key + " = " + _toml_scalar(values[key]) + "\n" for key in top_level))
        if agent_keys:
            if "agents" in table_ranges:
                end = table_ranges["agents"][1]
                add_insertion(end, "".join(key + " = " + _toml_scalar(values["agents." + key]) + "\n" for key in agent_keys))
            else:
                add_insertion(
                    len(lines),
                    "[agents]\n" + "".join(key + " = " + _toml_scalar(values["agents." + key]) + "\n" for key in agent_keys),
                )
        rebuilt: List[str] = []
        for index, line in enumerate(lines):
            if index in insertions:
                rebuilt.append(insertions[index])
            rebuilt.append(line)
        if len(lines) in insertions:
            rebuilt.append(insertions[len(lines)])
        lines = rebuilt
    patched = "".join(lines).encode("utf-8")
    expected_scalars = {key: _toml_scalar(value) for key, value in values.items()}
    observed_scalars = _collect_managed_toml_scalars(patched.decode("utf-8"), values)
    mismatched = sorted(key for key, expected in expected_scalars.items() if observed_scalars.get(key) != expected)
    if mismatched:
        raise DeploymentError("patched config does not contain expected managed values: %s" % ", ".join(mismatched))
    return patched, {"changed": patched != original, "keys": sorted(values), "missing_added": missing, "before_hash": sha256_bytes(original), "after_hash": sha256_bytes(patched)}


def validate_codex_config_bytes(config_bytes: bytes, codex_bin: Optional[str] = None) -> Dict[str, Any]:
    executable = codex_bin or shutil.which("codex")
    if not executable:
        raise DeploymentError("Codex executable is required for strict config validation")
    with tempfile.TemporaryDirectory(prefix="astra-config-check-") as directory:
        temporary_home = Path(directory)
        atomic_write_bytes(temporary_home / "config.toml", config_bytes, mode=0o600)
        try:
            completed = subprocess.run(
                # Help and feature-list commands either skip config loading or
                # reject --strict-config in the baseline CLI.  Starting the
                # stdio app server and immediately sending EOF traverses the
                # real strict loader without launching a model session.
                [executable, "app-server", "--strict-config", "--listen", "stdio://"],
                cwd=str(temporary_home),
                input="",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                env=dict(os.environ, CODEX_HOME=str(temporary_home)),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DeploymentError("strict config validation could not run: %s" % exc) from exc
        if completed.returncode != 0:
            raise DeploymentError("strict config validation failed: %s" % ((completed.stderr or completed.stdout).strip()[-2000:]))
        return {"ok": True, "codex": executable}


class DeploymentManager:
    """Transactional installer for user config, named agents, and the skill tree."""

    def __init__(self, repo: Path, codex_home: Path, agents_home: Optional[Path] = None, skill_home: Optional[Path] = None):
        self.repo = Path(repo).resolve()
        self.codex_home = resolve_codex_home(codex_home)
        self.agents_home_override = Path(agents_home).expanduser().resolve() if agents_home is not None else None
        self.skill_home_override = Path(skill_home).expanduser().resolve() if skill_home is not None else None
        # Deployment recovery records share the old package data root so a
        # pre-migration rollback remains available after installing this skill.
        self.root = self.codex_home / "astra-orchestrator" / "deployments"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(str(self.root), 0o700)
        self.lock_path = self.root.parent / "deploy.lock"
        self.journal = EventJournal(self.root / "events.jsonl", "deployment")

    def _latest(self) -> Dict[str, Any]:
        records = sorted(
            (path for path in self.root.glob("*/state.json") if DEPLOYMENT_ID_RE.match(path.parent.name)),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not records:
            raise DeploymentError("no deployment record exists")
        return read_json(records[0])

    def _unresolved(self) -> Optional[Dict[str, Any]]:
        for path in sorted(
            (item for item in self.root.glob("*/state.json") if DEPLOYMENT_ID_RE.match(item.parent.name)),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            state = read_json(path)
            if state.get("state") in {"preparing", "backed_up", "applying", "failed", "rolling_back"}:
                return state
        return None

    def _deployment_dir(self, deployment_id: str) -> Path:
        if not isinstance(deployment_id, str) or not DEPLOYMENT_ID_RE.match(deployment_id):
            raise DeploymentError("invalid deployment id")
        directory = self.root / deployment_id
        if directory.parent != self.root or directory.is_symlink():
            raise DeploymentError("deployment record escapes the deployment root")
        return directory

    def _load_deployment_state(self, deployment_id: str) -> Tuple[Dict[str, Any], Path]:
        deployment_dir = self._deployment_dir(deployment_id)
        state_path = deployment_dir / "state.json"
        if state_path.is_symlink() or not state_path.is_file():
            raise DeploymentError("deployment state is missing or unsafe: %s" % state_path)
        state = read_json(state_path)
        if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
            raise DeploymentError("deployment state has an unsupported schema")
        if state.get("deployment_id") != deployment_id or state.get("codex_home") != str(self.codex_home):
            raise DeploymentError("deployment state identity does not match the requested installation")
        manifest_snapshot = state.get("manifest_snapshot")
        legacy_package = isinstance(manifest_snapshot, Mapping) and manifest_snapshot.get("package_id") == "astra-orchestrator"
        files = state.get("files")
        if not isinstance(files, list):
            raise DeploymentError("deployment state files must be an array")
        recorded_targets = state.get("target_roots")
        if recorded_targets is None:
            # Legacy deployment records predate self-contained target roots.
            # Recover them from the manager's fixed installation roots without
            # consulting the mutable source manifest.
            targets = {
                "config": self.codex_home / "config.toml",
                "agents": self.agents_home_override or (self.codex_home / "agents"),
                "skill": self.skill_home_override or (Path.home() / ".agents" / "skills" / ("astra-orchestrator" if legacy_package else "codex-native-orchestrator")),
            }
        else:
            if not isinstance(recorded_targets, Mapping) or set(recorded_targets) != {"config", "agents", "skill"}:
                raise DeploymentError("deployment target roots are malformed")
            targets = {}
            for key, value in recorded_targets.items():
                if not isinstance(value, str) or not os.path.isabs(value) or os.path.abspath(value) != value:
                    raise DeploymentError("deployment target root is not a normalized absolute path")
                targets[key] = Path(value)
            if targets["config"] != self.codex_home / "config.toml":
                raise DeploymentError("deployment config root does not match CODEX_HOME")
            expected_agents = self.agents_home_override or (self.codex_home / "agents")
            expected_skill = self.skill_home_override or (Path.home() / ".agents" / "skills" / ("astra-orchestrator" if legacy_package else "codex-native-orchestrator"))
            if targets["agents"] != expected_agents or targets["skill"] != expected_skill:
                raise DeploymentError("deployment target roots do not match the requested installation")
        if manifest_snapshot is not None:
            if not isinstance(manifest_snapshot, Mapping) or sha256_json(manifest_snapshot) != state.get("manifest_hash"):
                raise DeploymentError("deployment manifest snapshot is invalid")
        roots = {"agent": targets["agents"], "skill": targets["skill"]}
        backup_root = deployment_dir / "backups"
        for record in files:
            if not isinstance(record, dict) or record.get("kind") not in {"config", "agent", "skill"}:
                raise DeploymentError("deployment state contains an invalid file record")
            target_value = record.get("target")
            if not isinstance(target_value, str) or not os.path.isabs(target_value):
                raise DeploymentError("deployment state contains a non-absolute target")
            target = Path(os.path.abspath(target_value))
            if str(target) != target_value:
                raise DeploymentError("deployment state target is not normalized: %s" % target_value)
            if record["kind"] == "config":
                if target != targets["config"]:
                    raise DeploymentError("deployment config target escaped its managed path")
            else:
                root = roots[record["kind"]]
                try:
                    relative = target.relative_to(root)
                except ValueError as exc:
                    raise DeploymentError("deployment target escaped its managed root: %s" % target) from exc
                if not relative.parts:
                    raise DeploymentError("deployment target cannot be the managed root itself")
            self._reject_symlink(target)
            backup_value = record.get("backup")
            if backup_value is not None:
                if not isinstance(backup_value, str) or not os.path.isabs(backup_value):
                    raise DeploymentError("deployment backup path is invalid")
                backup = Path(os.path.abspath(backup_value))
                try:
                    backup.relative_to(backup_root)
                except ValueError as exc:
                    raise DeploymentError("deployment backup escaped its record directory") from exc
                self._reject_symlink(backup)
        return state, state_path

    @staticmethod
    def _resolve_declared_path(value: Any, relative_root: Path) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise DeploymentError("deployment path must be a non-empty string")
        expanded = Path(value).expanduser()
        lexical = expanded if expanded.is_absolute() else relative_root / expanded
        # Keep the lexical path so _reject_symlink can inspect every existing
        # component instead of silently following a link during resolve().
        return Path(os.path.abspath(str(lexical)))

    @staticmethod
    def _reject_symlink(path: Path) -> None:
        current = path
        while True:
            if current.is_symlink():
                raise DeploymentError("deployment refuses symlink target: %s" % current)
            if current == current.parent:
                break
            current = current.parent

    def _targets(self, manifest: Mapping[str, Any]) -> Dict[str, Path]:
        deployment = manifest.get("deployment") if isinstance(manifest.get("deployment"), Mapping) else {}
        paths = manifest.get("paths") if isinstance(manifest.get("paths"), Mapping) else {}
        config_value = deployment.get("config_path", "config.toml")
        agents_value = deployment.get("agents_home", paths.get("user_agents_dir", "agents"))
        skill_value = deployment.get("skill_home", paths.get("user_skill_dir", "~/.agents/skills/codex-native-orchestrator"))
        config_path = self._resolve_declared_path(config_value, self.codex_home)
        expected_config = self.codex_home / "config.toml"
        if config_path != expected_config:
            raise DeploymentError("user config target must be CODEX_HOME/config.toml: %s" % config_path)
        declared_agents = self._resolve_declared_path(agents_value, self.codex_home)
        declared_skill = self._resolve_declared_path(skill_value, self.codex_home)
        expected_agents = self.codex_home / "agents"
        expected_skill = Path.home() / ".agents" / "skills" / "codex-native-orchestrator"
        if self.agents_home_override is None and declared_agents != expected_agents:
            raise DeploymentError("manifest agents target must be CODEX_HOME/agents: %s" % declared_agents)
        if self.skill_home_override is None and declared_skill != expected_skill:
            raise DeploymentError("manifest skill target must be ~/.agents/skills/codex-native-orchestrator: %s" % declared_skill)
        agents_path = self.agents_home_override or expected_agents
        skill_path = self.skill_home_override or expected_skill
        forbidden = {Path("/"), Path.home(), self.repo, self.codex_home}
        if agents_path in forbidden or skill_path in forbidden:
            raise DeploymentError("deployment directory target is too broad")
        return {"config": config_path, "agents": agents_path, "skill": skill_path}

    def _desired_files(self, manifest: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Path]]:
        targets = self._targets(manifest)
        deployment = manifest.get("deployment") if isinstance(manifest.get("deployment"), Mapping) else {}
        allowed = manifest.get("allowed_config_keys", manifest.get("managed_config_keys"))
        if allowed is None:
            allowed = deployment.get("allowed_config_keys", deployment.get("managed_config_keys"))
        values = config_values_from_manifest(manifest)
        config_path = targets["config"]
        self._reject_symlink(config_path)
        original = config_path.read_bytes() if config_path.exists() else b""
        if config_path.exists() and not config_path.is_file():
            raise DeploymentError("user config target is not a regular file: %s" % config_path)
        patched, patch_info = patch_toml_bytes(original, values, allowed_keys=allowed)
        if manifest.get("package_id") in {"astra-orchestrator", "codex-native-orchestrator"}:
            patch_info["strict_config"] = validate_codex_config_bytes(patched)
        desired: List[Dict[str, Any]] = [{
            "kind": "config",
            "source": None,
            "target": config_path,
            "data": patched,
            "after_mode": file_mode(config_path) if config_path.exists() else 0o600,
        }]
        agents_source_value = deployment.get("agents_source", (manifest.get("paths") or {}).get("agents_dir", ".agents/skills/codex-native-orchestrator/agents"))
        skill_source_value = deployment.get("skill_source", (manifest.get("paths") or {}).get("skill_dir", ".agents/skills/codex-native-orchestrator"))
        agents_source = self.repo / normalize_relative_path(str(agents_source_value))
        skill_source = self.repo / normalize_relative_path(str(skill_source_value))
        for kind, source_root, target_root in (("agent", agents_source, targets["agents"]), ("skill", skill_source, targets["skill"])):
            if not source_root.is_dir():
                raise DeploymentError("deployment source directory is missing: %s" % source_root)
            for source in sorted(path for path in source_root.rglob("*") if path.is_file()):
                if source.is_symlink():
                    raise DeploymentError("deployment refuses symlink source: %s" % source)
                relative = source.relative_to(source_root)
                target = target_root / relative
                self._reject_symlink(target)
                desired.append({"kind": kind, "source": source, "target": target, "data": source.read_bytes(), "after_mode": file_mode(source)})
        for item in desired:
            item["after_hash"] = sha256_bytes(item["data"])
        return desired, patch_info, targets

    @staticmethod
    def _target_snapshot(path: Path) -> Dict[str, Any]:
        if path.is_symlink():
            raise DeploymentError("deployment refuses symlink target: %s" % path)
        if not path.exists():
            return {"exists": False, "hash": None, "mode": None}
        if not path.is_file():
            raise DeploymentError("deployment target is not a regular file: %s" % path)
        return {"exists": True, "hash": sha256_file(path), "mode": file_mode(path)}

    @staticmethod
    def _matches(path: Path, exists: bool, digest: Optional[str], mode: Optional[int]) -> bool:
        if not exists:
            return not path.exists() and not path.is_symlink()
        return path.is_file() and not path.is_symlink() and sha256_file(path) == digest and file_mode(path) == mode

    def deploy(self, dry_run: bool = False) -> Dict[str, Any]:
        # Planning is inside the same lock as backup and apply so a concurrent
        # deploy cannot create a stale patched config document.
        with FileLock(self.lock_path):
            return self._deploy_with_lock_held(dry_run=dry_run)

    def _deploy_with_lock_held(self, dry_run: bool = False) -> Dict[str, Any]:
        manifest = load_manifest(self.repo, required=True)
        if manifest.get("installation_scope") == "global":
            raise DeploymentError("Already globally installed; update the global package in place. Do not deploy project copies.")
        desired, patch_info, targets = self._desired_files(manifest)
        component_changes = []
        planned_snapshots: Dict[str, Dict[str, Any]] = {}
        for item in desired:
            before = self._target_snapshot(item["target"])
            planned_snapshots[str(item["target"])] = before
            component_changes.append({"kind": item["kind"], "target": str(item["target"]), "changed": not self._matches(item["target"], True, item["after_hash"], item["after_mode"]) if before["exists"] else True, "before_hash": before["hash"], "after_hash": item["after_hash"]})
        config_snapshot = planned_snapshots[str(targets["config"])]
        if config_snapshot["exists"]:
            if config_snapshot["hash"] != patch_info["before_hash"]:
                raise DeploymentError("user config changed while deployment was being planned")
        elif patch_info["before_hash"] != sha256_bytes(b""):
            raise DeploymentError("user config existence changed while deployment was being planned")
        result = {
            "ok": True,
            "dry_run": dry_run,
            "config_path": str(targets["config"]),
            "agents_path": str(targets["agents"]),
            "skill_path": str(targets["skill"]),
            "patch": patch_info,
            "components": component_changes,
        }
        with contextlib.nullcontext():
            if dry_run:
                self.journal.append("deployment_dry_run", {"config_path": str(targets["config"]), "patch": patch_info, "changed_files": sum(1 for item in component_changes if item["changed"])})
                return result
            unresolved = self._unresolved()
            if unresolved is not None:
                self.journal.append("deployment_recovery_required", {"deployment_id": unresolved.get("deployment_id"), "state": unresolved.get("state")})
                raise DeploymentError("deployment %s requires rollback recovery before another apply" % unresolved.get("deployment_id"))
            deployment_id = "deployment-" + uuid.uuid4().hex[:16]
            deployment_dir = self.root / deployment_id
            backup_dir = deployment_dir / "backups"
            backup_dir.mkdir(parents=True, mode=0o700)
            state: Dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "deployment_id": deployment_id,
                "repo_root": str(self.repo),
                "codex_home": str(self.codex_home),
                "state": "preparing",
                "created_at": utc_now(),
                "manifest_snapshot": {k: v for k, v in manifest.items() if k != "manifest_path"},
                "manifest_hash": sha256_json({k: v for k, v in manifest.items() if k != "manifest_path"}),
                "target_roots": {name: str(path) for name, path in targets.items()},
                "files": [],
            }
            atomic_write_json(deployment_dir / "state.json", state, mode=0o600)
            self.journal.append("deployment_started", {"deployment_id": deployment_id, "file_count": len(desired)})
            try:
                for index, item in enumerate(desired):
                    target = item["target"]
                    before = self._target_snapshot(target)
                    if before != planned_snapshots[str(target)]:
                        raise DeploymentError("deployment target changed during locked planning: %s" % target)
                    changed = not (before["exists"] and before["hash"] == item["after_hash"] and before["mode"] == item["after_mode"])
                    backup = None
                    if changed and before["exists"]:
                        backup = backup_dir / ("%04d-%s" % (index, target.name))
                        atomic_write_bytes(backup, target.read_bytes(), mode=int(before["mode"]))
                        if sha256_file(backup) != before["hash"] or file_mode(backup) != before["mode"]:
                            raise DeploymentError("deployment backup verification failed: %s" % target)
                    record = {
                        "kind": item["kind"],
                        "source": str(item["source"]) if item["source"] else None,
                        "target": str(target),
                        "changed": changed,
                        "before_exists": before["exists"],
                        "before_hash": before["hash"],
                        "before_mode": before["mode"],
                        "backup": str(backup) if backup else None,
                        "after_hash": item["after_hash"],
                        "after_mode": item["after_mode"],
                        "applied": False,
                        "rolled_back": False,
                    }
                    state["files"].append(record)
                    atomic_write_json(deployment_dir / "state.json", state, mode=0o600)
                state["state"] = "backed_up"
                atomic_write_json(deployment_dir / "state.json", state, mode=0o600)
                for index, item in enumerate(desired):
                    record = state["files"][index]
                    if not record["changed"]:
                        continue
                    target = Path(record["target"])
                    if not self._matches(target, record["before_exists"], record["before_hash"], record["before_mode"]):
                        raise DeploymentError("deployment target changed after backup: %s" % target)
                    state["state"] = "applying"
                    state["current_file"] = str(target)
                    atomic_write_json(deployment_dir / "state.json", state, mode=0o600)
                    atomic_write_bytes(target, item["data"], mode=int(record["after_mode"]))
                    if not self._matches(target, True, record["after_hash"], record["after_mode"]):
                        raise DeploymentError("deployment post-write verification failed: %s" % target)
                    record["applied"] = True
                    atomic_write_json(deployment_dir / "state.json", state, mode=0o600)
                    self.journal.append("deployment_file_applied", {"deployment_id": deployment_id, "target": str(target), "after_hash": record["after_hash"]})
                for record in state["files"]:
                    target = Path(record["target"])
                    if not self._matches(target, True, record["after_hash"], record["after_mode"]):
                        raise DeploymentError("deployment final verification detected a concurrent edit: %s" % target)
                state["state"] = "applied"
                state.pop("current_file", None)
                state["applied_at"] = utc_now()
                atomic_write_json(deployment_dir / "state.json", state, mode=0o600)
                self.journal.append("deployment_applied", {"deployment_id": deployment_id, "changed_files": sum(1 for item in state["files"] if item["changed"])})
            except Exception as exc:
                state["state"] = "failed"
                state["error"] = str(exc)
                atomic_write_json(deployment_dir / "state.json", state, mode=0o600)
                self.journal.append("deployment_failed", {"deployment_id": deployment_id, "error": str(exc), "rollback_required": any(item.get("applied") for item in state["files"])})
                raise
            result.update({"deployment_id": deployment_id, "state": state["state"], "changed_files": sum(1 for item in state["files"] if item["changed"])})
            return result

    def rollback(self, deployment_id: Optional[str] = None) -> Dict[str, Any]:
        with FileLock(self.lock_path):
            selected = deployment_id or self._latest()["deployment_id"]
            state, state_path = self._load_deployment_state(selected)
            if state.get("state") not in {"applied", "applying", "failed", "backed_up", "preparing", "rolling_back"}:
                raise DeploymentError("deployment cannot be rolled back from state %s" % state.get("state"))
            observed_after: Dict[str, bool] = {}
            for item in state.get("files", []):
                if not item.get("changed"):
                    continue
                path = Path(item["target"])
                is_after = self._matches(path, True, item["after_hash"], item["after_mode"])
                is_before = self._matches(path, item["before_exists"], item["before_hash"], item["before_mode"])
                if item.get("rolled_back"):
                    if not is_before:
                        raise DeploymentError("rollback recovery refused: restored file changed: %s" % path)
                    observed_after[str(path)] = False
                    continue
                if item.get("applied") and not is_after:
                    raise DeploymentError("rollback refused: deployed file changed after deployment: %s" % path)
                if not item.get("applied") and not is_after and not is_before:
                    raise DeploymentError("rollback refused: target changed during interrupted deployment: %s" % path)
                observed_after[str(path)] = is_after
                if item["before_exists"]:
                    backup = Path(item["backup"])
                    if not backup.is_file() or sha256_file(backup) != item["before_hash"] or file_mode(backup) != item["before_mode"]:
                        raise DeploymentError("rollback backup verification failed: %s" % backup)
            state["state"] = "rolling_back"
            atomic_write_json(state_path, state, mode=0o600)
            for item in reversed(state.get("files", [])):
                if not item.get("changed") or not observed_after.get(item["target"], False):
                    continue
                path = Path(item["target"])
                if item["before_exists"]:
                    backup = Path(item["backup"])
                    atomic_write_bytes(path, backup.read_bytes(), mode=int(item["before_mode"]))
                    if not self._matches(path, True, item["before_hash"], item["before_mode"]):
                        raise DeploymentError("rollback post-write verification failed: %s" % path)
                else:
                    path.unlink()
                    fsync_directory(path.parent)
                    if path.exists() or path.is_symlink():
                        raise DeploymentError("rollback could not remove newly deployed file: %s" % path)
                item["rolled_back"] = True
                atomic_write_json(state_path, state, mode=0o600)
            state["state"] = "rolled_back"
            state["rolled_back_at"] = utc_now()
            atomic_write_json(state_path, state, mode=0o600)
            self.journal.append("deployment_rolled_back", {"deployment_id": state["deployment_id"]})
            return state


def _tree_fingerprint(root: Path) -> Optional[str]:
    if not root.is_dir():
        return None
    records = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.is_symlink():
            raise DeploymentError("fingerprint refuses symlink: %s" % path)
        records.append({"path": path.relative_to(root).as_posix(), "hash": sha256_file(path), "mode": file_mode(path)})
    return sha256_json(records)


def _managed_tree_fingerprint(source_root: Path, target_root: Path) -> Optional[str]:
    if not source_root.is_dir() or not target_root.is_dir():
        return None
    records = []
    for source in sorted(item for item in source_root.rglob("*") if item.is_file()):
        relative = source.relative_to(source_root)
        target = target_root / relative
        if not target.is_file() or target.is_symlink():
            return None
        records.append({"path": relative.as_posix(), "hash": sha256_file(target), "mode": file_mode(target)})
    return sha256_json(records)


def _agent_file_spec(path: Path) -> Dict[str, str]:
    if not path.is_file():
        return {}
    fields = ("model", "model_reasoning_effort", "sandbox_mode")
    try:
        observed = _collect_managed_toml_scalars(path.read_text(encoding="utf-8"), fields)
        result = {field: json.loads(value) for field, value in observed.items()}
    except (DeploymentError, UnicodeError, ValueError):
        return {}
    return {field: value for field, value in result.items() if isinstance(value, str)}


def global_doctor(repo: Path, codex_home: Path, manifest: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate the sole installed package without requiring per-project copies."""
    checks: Dict[str, Any] = {}
    checks["manifest"] = {"ok": True, "path": manifest["manifest_path"], "package_version": manifest.get("package_version")}
    roles = {}
    for name in sorted(TASK_ROLE_NAMES):
        spec = role_spec(manifest, name)
        expected = {"model": spec["model"], "model_reasoning_effort": spec["reasoning_effort"], "sandbox_mode": spec["sandbox_mode"]}
        path = codex_home / "agents" / (name + ".toml")
        actual = _agent_file_spec(path)
        roles[name] = {"ok": actual == expected, "path": str(path), "expected": expected, "actual": actual}
    checks["roles"] = {"ok": all(v["ok"] for v in roles.values()), "roles": roles}
    conflicts = []
    managed_keys = set(manifest["managed_config_keys"])
    for directory in (repo, *repo.parents):
        if directory == Path.home():
            continue
        local_skill = directory / MANIFEST_RELATIVE.parent / "SKILL.md"
        if local_skill.is_file() and local_skill.resolve() != (SKILL_DIR / "SKILL.md").resolve():
            conflicts.append(str(local_skill))
        local_config = directory / ".codex" / "config.toml"
        if local_config.is_file() and local_config.resolve() != (codex_home / "config.toml").resolve():
            try:
                local_values = _collect_managed_toml_scalars(local_config.read_text(encoding="utf-8"), managed_keys)
                if local_values:
                    conflicts.append(str(local_config) + " (managed keys: " + ", ".join(sorted(local_values)) + ")")
            except (ControllerError, OSError, UnicodeError) as exc:
                conflicts.append(str(local_config) + " (cannot inspect: " + str(exc) + ")")
        for name in sorted(TASK_ROLE_NAMES):
            local_role = directory / ".codex" / "agents" / (name + ".toml")
            if local_role.is_file() and local_role.resolve() != (codex_home / "agents" / (name + ".toml")).resolve():
                conflicts.append(str(local_role))
    checks["project_overrides"] = {"ok": not conflicts, "conflicts": conflicts}
    try:
        config_bytes = (codex_home / "config.toml").read_bytes()
        validate_codex_config_bytes(config_bytes)
        expected_values = config_values_from_manifest(manifest)
        expected = {key: _toml_scalar(value) for key, value in expected_values.items()}
        actual = _collect_managed_toml_scalars(config_bytes.decode("utf-8"), expected)
        checks["config"] = {"ok": actual == expected, "expected": expected, "actual": actual}
    except (ControllerError, OSError) as exc:
        checks["config"] = {"ok": False, "error": str(exc)}
    return {"ok": all(v["ok"] for v in checks.values()), "scope": "global", "checks": checks}


def doctor(repo: Path, codex_home: Path) -> Dict[str, Any]:
    repo = Path(repo).resolve()
    codex_home = resolve_codex_home(codex_home)
    installed_manifest = load_manifest(repo, required=False)
    if installed_manifest.get("installation_scope") == "global":
        return global_doctor(repo, codex_home, installed_manifest)
    checks: Dict[str, Any] = {}
    topology: Dict[str, Any] = {}
    checks["python"] = {"ok": sys.version_info >= (3, 9), "version": sys.version.split()[0], "minimum": "3.9"}
    try:
        manifest = load_manifest(repo, required=True)
        topology = {
            name: {
                "model": role_spec(manifest, name)["model"],
                "reasoning_effort": role_spec(manifest, name)["reasoning_effort"],
                "sandbox_mode": role_spec(manifest, name).get("sandbox_mode", "read-only" if name in READ_ONLY_ROLES else "workspace-write"),
            }
            for name in sorted(ROLE_NAMES)
        }
        checks["manifest"] = {"ok": True, "path": str(repo / MANIFEST_RELATIVE), "package_version": manifest.get("package_version"), "hash": sha256_file(repo / MANIFEST_RELATIVE)}
    except ControllerError as exc:
        manifest = {"roles": DEFAULT_ROLE_SPECS, "max_concurrency": 4, "managed_config_keys": []}
        checks["manifest"] = {"ok": False, "error": str(exc)}
    schema_names = ("plan.schema.json", "state.schema.json", "event.schema.json", "result.schema.json", "gate.schema.json")
    schema_errors = []
    for name in schema_names:
        path = SCHEMA_DIR / name
        try:
            value = read_json(path)
            if not isinstance(value, dict):
                raise ControllerError("schema is not an object")
        except ControllerError as exc:
            schema_errors.append("%s: %s" % (name, exc))
    checks["schemas"] = {"ok": not schema_errors, "path": str(SCHEMA_DIR), "errors": schema_errors}
    source_roles = {}
    role_drift = []
    for name in sorted(TASK_ROLE_NAMES):
        path = repo / ".codex" / "agents" / (name + ".toml")
        actual = _agent_file_spec(path)
        expected = topology.get(name, {})
        expected_file = {"model": expected.get("model"), "model_reasoning_effort": expected.get("reasoning_effort"), "sandbox_mode": expected.get("sandbox_mode")}
        ok = actual == expected_file
        source_roles[name] = {"ok": ok, "path": str(path), "expected": expected_file, "actual": actual}
        if not ok:
            role_drift.append(name)
    checks["source_roles"] = {"ok": not role_drift, "roles": source_roles, "drift": role_drift}
    codex_bin = shutil.which("codex")
    codex_check: Dict[str, Any] = {"ok": codex_bin is not None, "path": codex_bin}
    if codex_bin:
        try:
            version = subprocess.run([codex_bin, "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
            try:
                config_path = codex_home / "config.toml"
                validate_codex_config_bytes(config_path.read_bytes() if config_path.is_file() else b"", codex_bin=codex_bin)
                strict_ok = True
                strict_error = ""
            except DeploymentError as exc:
                strict_ok = False
                strict_error = str(exc)
            codex_check.update({"version": (version.stdout or version.stderr).strip(), "strict_config_ok": strict_ok, "strict_config_error": strict_error})
            codex_check["ok"] = version.returncode == 0 and strict_ok
        except (OSError, subprocess.SubprocessError) as exc:
            codex_check.update({"ok": False, "error": str(exc)})
    checks["codex"] = codex_check
    if codex_bin:
        try:
            app_server = subprocess.run([codex_bin, "app-server", "--help"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
            checks["app_server"] = {"ok": True, "available": app_server.returncode == 0, "selected_backend": "cli", "correctness_depends_on_app_server": False}
        except (OSError, subprocess.SubprocessError) as exc:
            checks["app_server"] = {"ok": True, "available": False, "selected_backend": "cli", "correctness_depends_on_app_server": False, "probe_error": str(exc)}
    else:
        checks["app_server"] = {"ok": True, "available": False, "selected_backend": "cli", "correctness_depends_on_app_server": False}
    try:
        snapshot = repo_snapshot(repo)
        checks["git"] = {"ok": True, "snapshot": snapshot, "writer_ready": snapshot["clean"]}
    except ControllerError as exc:
        checks["git"] = {"ok": False, "error": str(exc), "writer_ready": False}
    state_root = codex_home / "astra-orchestrator" / "runs"
    state_parent = state_root if state_root.exists() else state_root.parent
    checks["state"] = {"ok": state_parent.exists() and os.access(str(state_parent), os.W_OK), "path": str(state_root), "exists": state_root.exists(), "mode": oct(file_mode(state_root)) if state_root.exists() else None}
    try:
        manager = DeploymentManager(repo, codex_home)
        targets = manager._targets(manifest)
        desired, patch_info, _ = manager._desired_files(manifest)
        installed_agents = {}
        installed_drift = []
        for name in sorted(TASK_ROLE_NAMES):
            source = repo / ".codex" / "agents" / (name + ".toml")
            target = targets["agents"] / (name + ".toml")
            ok = source.is_file() and target.is_file() and sha256_file(source) == sha256_file(target) and file_mode(source) == file_mode(target)
            installed_agents[name] = {"ok": ok, "path": str(target)}
            if not ok:
                installed_drift.append(name)
        source_skill = repo / MANIFEST_RELATIVE.parent
        source_skill_hash = _tree_fingerprint(source_skill)
        installed_skill_hash = _managed_tree_fingerprint(source_skill, targets["skill"])
        config_target = targets["config"]
        config_ok = config_target.is_file() and not patch_info["changed"]
        checks["installed"] = {
            "ok": config_ok and not installed_drift and source_skill_hash == installed_skill_hash,
            "config": {"ok": config_ok, "path": str(config_target), "managed_patch": patch_info},
            "agents": installed_agents,
            "agent_drift": installed_drift,
            "skill": {"ok": source_skill_hash == installed_skill_hash, "path": str(targets["skill"]), "source_hash": source_skill_hash, "installed_hash": installed_skill_hash},
            "desired_file_count": len(desired),
        }
    except (ControllerError, OSError) as exc:
        checks["installed"] = {"ok": False, "error": str(exc)}
    catalog_candidates = [codex_home / "models_cache.json", codex_home / "cockpit-model-catalog.json", codex_home / "cockpit-model-catalog.json.bak"]
    catalog_path = next((path for path in catalog_candidates if path.is_file()), catalog_candidates[0])
    if catalog_path.is_file():
        try:
            catalog = read_json(catalog_path)
            models = catalog.get("models", []) if isinstance(catalog, Mapping) else []
            by_name = {}
            for item in models:
                if not isinstance(item, Mapping):
                    continue
                name = item.get("slug", item.get("model", item.get("id")))
                efforts = []
                for level in item.get("supported_reasoning_levels", item.get("reasoning_levels", [])):
                    if isinstance(level, str):
                        efforts.append(level)
                    elif isinstance(level, Mapping):
                        value = level.get("effort", level.get("level", level.get("name")))
                        if isinstance(value, str):
                            efforts.append(value)
                if isinstance(name, str):
                    by_name[name] = set(efforts)
            capability = {}
            for name, spec in topology.items():
                present = spec["model"] in by_name and spec["reasoning_effort"] in by_name[spec["model"]]
                capability[name] = {"model": spec["model"], "reasoning_effort": spec["reasoning_effort"], "present": present}
            checks["models"] = {"ok": all(item["present"] for item in capability.values()), "catalog": str(catalog_path), "roles": capability}
        except ControllerError as exc:
            checks["models"] = {"ok": False, "catalog": str(catalog_path), "error": str(exc)}
    else:
        checks["models"] = {"ok": False, "catalog": str(catalog_path), "error": "local model catalog is missing"}
    return {"ok": all(value.get("ok", False) for value in checks.values()), "topology": topology, "checks": checks}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-native-orchestrator")
    parser.add_argument("--repo", default=None, help="repository root (defaults to current directory)")
    parser.add_argument("--codex-home", default=None, help="CODEX_HOME override")
    parser.add_argument("--json", action="store_true", help="emit JSON where applicable")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("doctor"); p.add_argument("--repo", default=argparse.SUPPRESS); p.add_argument("--codex-home", default=argparse.SUPPRESS)
    p = sub.add_parser("validate-plan"); p.add_argument("plan"); p.add_argument("--repo", default=argparse.SUPPRESS); p.add_argument("--codex-home", default=argparse.SUPPRESS)
    p = sub.add_parser("run"); p.add_argument("plan"); p.add_argument("--run-id", default=None); p.add_argument("--dry-run", action="store_true"); p.add_argument("--repo", default=argparse.SUPPRESS); p.add_argument("--codex-home", default=argparse.SUPPRESS)
    p = sub.add_parser("status"); p.add_argument("run_id"); p.add_argument("--repo", default=argparse.SUPPRESS); p.add_argument("--codex-home", default=argparse.SUPPRESS)
    p = sub.add_parser("resume"); p.add_argument("run_id"); p.add_argument("--waiver", default=None); p.add_argument("--repo", default=argparse.SUPPRESS); p.add_argument("--codex-home", default=argparse.SUPPRESS)
    p = sub.add_parser("cancel"); p.add_argument("run_id"); p.add_argument("--reason", default="cancelled by user"); p.add_argument("--repo", default=argparse.SUPPRESS); p.add_argument("--codex-home", default=argparse.SUPPRESS)
    p = sub.add_parser("report"); p.add_argument("run_id"); p.add_argument("--format", choices=("json", "markdown"), default="json"); p.add_argument("--repo", default=argparse.SUPPRESS); p.add_argument("--codex-home", default=argparse.SUPPRESS)
    p = sub.add_parser("apply"); p.add_argument("run_id"); p.add_argument("--waiver", default=None); p.add_argument("--repo", default=argparse.SUPPRESS); p.add_argument("--codex-home", default=argparse.SUPPRESS)
    p = sub.add_parser("cleanup"); p.add_argument("run_id"); p.add_argument("--repo", default=argparse.SUPPRESS); p.add_argument("--codex-home", default=argparse.SUPPRESS)
    p = sub.add_parser("deploy"); mode = p.add_mutually_exclusive_group(); mode.add_argument("--dry-run", action="store_true"); mode.add_argument("--apply", action="store_true"); p.add_argument("--repo", default=argparse.SUPPRESS); p.add_argument("--codex-home", default=argparse.SUPPRESS)
    p = sub.add_parser("rollback"); p.add_argument("deployment_id", nargs="?"); p.add_argument("--repo", default=argparse.SUPPRESS); p.add_argument("--codex-home", default=argparse.SUPPRESS)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo_value = getattr(args, "repo", None) or os.getcwd()
    codex_home = resolve_codex_home(getattr(args, "codex_home", None))
    repo = Path(repo_value).expanduser().resolve()
    try:
        if args.command == "doctor":
            result = doctor(repo, codex_home)
        elif args.command == "validate-plan":
            result = {"ok": True, "plan": Orchestrator(repo, codex_home).validate(Path(args.plan))}
        elif args.command == "run":
            result = Orchestrator(repo, codex_home).run(Path(args.plan), run_id=args.run_id, dry_run=args.dry_run)
        elif args.command == "status":
            result = Orchestrator(repo, codex_home).status(args.run_id)
        elif args.command == "resume":
            result = Orchestrator(repo, codex_home).resume(args.run_id, waiver=args.waiver)
        elif args.command == "cancel":
            result = Orchestrator(repo, codex_home).cancel(args.run_id, args.reason)
        elif args.command == "report":
            result = Orchestrator(repo, codex_home).report(args.run_id, markdown=args.format == "markdown")
        elif args.command == "apply":
            result = Orchestrator(repo, codex_home).apply(args.run_id, waiver=args.waiver)
        elif args.command == "cleanup":
            result = Orchestrator(repo, codex_home).cleanup(args.run_id)
        elif args.command == "deploy":
            result = DeploymentManager(repo, codex_home).deploy(dry_run=not args.apply)
        elif args.command == "rollback":
            result = DeploymentManager(repo, codex_home).rollback(args.deployment_id)
        else:
            parser.error("unknown command")
            return 2
        if isinstance(result, str):
            print(result, end="" if result.endswith("\n") else "\n")
        else:
            print(json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False))
        return 0
    except (ControllerError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
