"""Bounded native rollout authority for cold resume; never a permission default."""

import hashlib
import json
import os
import stat
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from connector.runtime_protocol.errors import RuntimeProtocolError

from .wire import IpcError


class ResumeSettingsError(IpcError, RuntimeProtocolError):
    retryable = False

    def __init__(self, code):
        self.code = code
        detail = (
            "Native resume may have applied settings. No new turn was started."
            if "effective" in code or "connection" in code
            else "Native resume settings could not be verified. No resume or turn was started."
        )
        RuntimeError.__init__(self, f"{code}: {detail}")


# Fail closed on larger/unknown histories, rather than searching another file.
MAX_ROLLOUT_BYTES = 32 * 1024 * 1024
MAX_RECORD_BYTES = 1024 * 1024
MAX_RECORDS = 100_000

CONTEXT_FIELDS = {
    "model": "model",
    "approval_policy": "approval_policy",
    "approvals_reviewer": "approvals_reviewer",
    "permission_profile": "permission_profile",
    "cwd": "cwd",
    "workspace_roots": "runtime_workspace_roots",
    "effort": "reasoning_effort",
    "summary": "reasoning_summary",
    "personality": "personality",
    "collaboration_mode": "collaboration_mode",
    "disabled_plugin_ids": "disabled_plugin_ids",
}
CONTEXT_KNOWN_FIELDS = frozenset(CONTEXT_FIELDS.values()) | {"model_provider_id"}
SETTINGS_FIELDS = CONTEXT_KNOWN_FIELDS | {"service_tier"}


def unavailable():
    raise ResumeSettingsError("codex_resume_settings_unavailable")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def read_source(path):
    """No descriptor survives an await, error, or cancellation boundary."""
    try:
        if not hasattr(os, "O_NOFOLLOW"):
            unavailable()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or not 0 < before.st_size <= MAX_ROLLOUT_BYTES
            ):
                unavailable()
            data = stream.read(MAX_ROLLOUT_BYTES + 1)
            if fingerprint(before) != fingerprint(os.fstat(stream.fileno())):
                unavailable()
        if fingerprint(before) != fingerprint(os.stat(path, follow_symlinks=False)):
            unavailable()
        if len(data) != before.st_size or not data.endswith(b"\n"):
            unavailable()
        return data, fingerprint(before)
    except (OSError, ValueError):
        unavailable()


def records(data):
    lines = data.splitlines()
    if len(lines) > MAX_RECORDS:
        unavailable()
    for line in lines:
        if not line or len(line) > MAX_RECORD_BYTES:
            unavailable()
        try:
            value = json.loads(line)
            if not isinstance(value, dict) or not isinstance(
                value.get("payload"), dict
            ):
                unavailable()
            yield value
        except (ValueError, RecursionError):
            unavailable()


def normalized_profile(profile):
    result = deepcopy(profile)
    if not isinstance(result, dict):
        unavailable()
    if result.get("type") == "managed":
        fs = result.get("file_system")
        if not isinstance(fs, dict) or not isinstance(fs.get("entries"), list):
            unavailable()
        fs["entries"] = sorted({canonical(e) for e in fs["entries"]})
    return result


def policy(settings):
    """Recognize built-in legacy-equivalent profiles, retaining every rule."""
    cwd, roots = settings.get("cwd"), settings.get("runtime_workspace_roots")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        unavailable()
    if (
        not isinstance(roots, list)
        or not roots
        or roots[0] != cwd
        or any(not isinstance(p, str) or not Path(p).is_absolute() for p in roots)
        or len(set(roots)) != len(roots)
    ):
        unavailable()
    p = settings.get("permission_profile")
    if p == {"type": "disabled"}:
        return "danger-full-access", None
    if (
        not isinstance(p, dict)
        or set(p) != {"type", "file_system", "network"}
        or p["type"] != "managed"
        or p["network"] != "restricted"
    ):
        unavailable()
    fs = p["file_system"]
    if (
        not isinstance(fs, dict)
        or set(fs) != {"type", "entries"}
        or fs["type"] != "restricted"
        or not isinstance(fs["entries"], list)
    ):
        unavailable()

    def special(kind):
        return {"type": "special", "value": {"kind": kind}}

    expected = [{"path": special("root"), "access": "read"}]
    entries = {canonical(e) for e in fs["entries"]}
    if entries == {canonical(e) for e in expected} and p["network"] == "restricted":
        return "read-only", None
    for root in roots:
        expected.append({"path": {"type": "path", "path": root}, "access": "write"})
        for name in (".git", ".agents", ".codex"):
            expected.append(
                {
                    "path": {"type": "path", "path": str(Path(root) / name)},
                    "access": "read",
                    "missing_path_behavior": "skip",
                }
            )
    flags = {}
    for kind, key in (
        ("tmpdir", "exclude_tmpdir_env_var"),
        ("slash_tmp", "exclude_slash_tmp"),
    ):
        entry = {"path": special(kind), "access": "write"}
        flags[key] = canonical(entry) not in entries
        if not flags[key]:
            expected.append(entry)
    if entries != {canonical(e) for e in expected}:
        unavailable()
    return "workspace-write", {
        "writable_roots": roots[1:],
        "network_access": p["network"] == "enabled",
        **flags,
    }


def validate_settings(settings, cwd, known_fields=SETTINGS_FIELDS):
    if (
        not isinstance(settings, dict)
        or not known_fields <= settings.keys()
        or settings["cwd"] != cwd
    ):
        unavailable()
    if settings["approval_policy"] not in (
        "untrusted",
        "on-failure",
        "on-request",
        "never",
    ) or settings["approvals_reviewer"] not in (
        "user",
        "auto_review",
        "guardian_subagent",
    ):
        unavailable()
    if (
        not all(
            isinstance(settings[k], str) and settings[k]
            for k in ("model", "model_provider_id")
        )
        or settings["disabled_plugin_ids"] != []
    ):
        unavailable()
    mode = settings["collaboration_mode"]
    if (
        not isinstance(mode, dict)
        or set(mode) != {"mode", "settings"}
        or mode.get("mode") not in ("default", "plan")
        or not isinstance(mode.get("settings"), dict)
        or set(mode["settings"])
        != {"model", "reasoning_effort", "developer_instructions"}
        or mode["settings"].get("model") != settings["model"]
        or mode["settings"].get("reasoning_effort") != settings["reasoning_effort"]
        or not (
            mode["settings"]["developer_instructions"] is None
            or isinstance(mode["settings"]["developer_instructions"], str)
        )
    ):
        unavailable()
    for key in ("reasoning_effort", "reasoning_summary", "personality", "service_tier"):
        if (
            key in known_fields
            and settings[key] is not None
            and not isinstance(settings[key], str)
        ):
            unavailable()
    policy(settings)


def projection(settings, known_fields):
    """Compare represented settings, retaining exact mode instructions and rules."""
    result = {key: deepcopy(settings[key]) for key in known_fields}
    result["permission_profile"] = normalized_profile(result["permission_profile"])
    return result


def context_settings(context, cwd, provider):
    """Only this complete current format has initial settings authority."""
    if (
        not CONTEXT_FIELDS.keys() <= context.keys()
        or context.get("root_turn_id") != context.get("turn_id")
        or "service_tier" in context
    ):
        unavailable()
    settings = {
        target: deepcopy(context[source]) for source, target in CONTEXT_FIELDS.items()
    }
    settings["model_provider_id"] = provider
    validate_settings(settings, cwd, CONTEXT_KNOWN_FIELDS)
    mode, flags = policy(settings)
    legacy = deepcopy(context.get("sandbox_policy"))
    if isinstance(legacy, dict) and mode == "workspace-write":
        legacy.setdefault("writable_roots", [])
    if legacy != {"type": mode, **(flags or {})}:
        unavailable()
    profile = settings["permission_profile"]
    fs = context.get("file_system_sandbox_policy")
    if profile["type"] == "disabled":
        # Current native serializes explicit disabled + danger-full-access with
        # no separate filesystem field, rather than an inferred unrestricted one.
        if "file_system_sandbox_policy" in context:
            unavailable()
    else:
        if (
            not isinstance(fs, dict)
            or set(fs) != {"kind", "entries"}
            or fs["kind"] != profile["file_system"]["type"]
            or not isinstance(fs["entries"], list)
            or {canonical(e) for e in fs["entries"]}
            != {canonical(e) for e in profile["file_system"]["entries"]}
        ):
            unavailable()
    return settings


def scan(data, thread):
    latest = context = meta = None
    for index, record in enumerate(records(data)):
        kind, payload = record.get("type"), record["payload"]
        event = payload.get("type", "")
        if any(
            word in str(kind) + str(event)
            for word in ("compact", "rollback", "rolled_back", "revert", "fork")
        ):
            unavailable()
        if index == 0:
            meta = payload
            if (
                kind != "session_meta"
                or payload.get("id") != thread.get("id")
                or payload.get("cwd") != thread.get("cwd")
                or payload.get("history_base")
                or payload.get("history_mode") not in (None, "legacy", "paginated")
                or (
                    payload.get("history_mode") is not None
                    and thread.get("historyMode") is not None
                    and payload["history_mode"] != thread["historyMode"]
                )
                or any(
                    payload.get(k)
                    for k in ("forked_from_id", "forked_from", "parent_thread_id")
                )
            ):
                unavailable()
        elif kind == "session_meta":
            unavailable()
        if kind == "event_msg" and event == "thread_settings_applied":
            if payload.get("thread_id") != thread["id"]:
                unavailable()
            latest = (index, payload.get("thread_settings"))
        if kind == "turn_context":
            context = (index, payload)
    turns = thread.get("turns")
    if (
        context is None
        or not isinstance(turns, list)
        or not turns
        or not isinstance(turns[-1], dict)
        or not isinstance(turns[-1].get("id"), str)
        or not turns[-1]["id"]
    ):
        unavailable()
    ordinal, context = context
    if (
        context.get("turn_id") != turns[-1]["id"]
        or context.get("cwd") != thread["cwd"]
        or context.get("root_turn_id", context["turn_id"]) != context["turn_id"]
        or any(
            context.get(key)
            for key in ("history_base", "forked_from_id", "parent_thread_id")
        )
    ):
        unavailable()
    if latest is None:
        provider = meta.get("model_provider")
        if provider != thread.get("modelProvider"):
            unavailable()
        settings = context_settings(context, thread["cwd"], provider)
        return settings, "initial_context", ordinal, CONTEXT_KNOWN_FIELDS
    settings_ordinal, settings = latest
    validate_settings(settings, thread["cwd"])
    if ordinal > settings_ordinal:
        # A later execution context must corroborate, never replace, settings.
        corroboration = context_settings(
            context, thread["cwd"], settings["model_provider_id"]
        )
        if projection(corroboration, CONTEXT_KNOWN_FIELDS) != projection(
            settings, CONTEXT_KNOWN_FIELDS
        ):
            unavailable()
    return settings, "settings_applied", settings_ordinal, SETTINGS_FIELDS


@dataclass
class ResumeSettings:
    path: str
    thread: dict
    data: bytes
    stamp: tuple
    settings: dict
    source_kind: str
    source_ordinal: int
    known_fields: frozenset[str]

    @classmethod
    def resolve(cls, raw, thread_id):
        if not isinstance(raw, dict) or not isinstance(raw.get("thread"), dict):
            unavailable()
        thread = raw["thread"]
        path = thread.get("path")
        if (
            thread.get("id") != thread_id
            or not isinstance(path, str)
            or not Path(path).is_absolute()
            or thread.get("forkedFromId")
            or thread.get("parentThreadId")
            or thread.get("historyBase")
            or thread.get("historyMode") not in (None, "legacy", "paginated")
        ):
            unavailable()
        data, stamp = read_source(path)
        try:
            settings, source_kind, source_ordinal, known_fields = scan(data, thread)
        except (TypeError, ValueError, AttributeError, KeyError, RecursionError):
            unavailable()
        return cls(
            path,
            thread,
            data,
            stamp,
            settings,
            source_kind,
            source_ordinal,
            known_fields,
        )

    def revalidate(self):
        data, stamp = read_source(self.path)
        if (
            stamp != self.stamp
            or hashlib.sha256(data).digest() != hashlib.sha256(self.data).digest()
        ):
            raise ResumeSettingsError("codex_resume_settings_changed")

    def params(self):
        s = self.settings
        mode, flags = policy(s)
        config = {
            "model_reasoning_effort": s["reasoning_effort"],
            "model_reasoning_summary": s["reasoning_summary"],
        }
        if flags is not None:
            config["sandbox_workspace_write"] = flags
        params = {
            "threadId": self.thread["id"],
            "cwd": s["cwd"],
            "model": s["model"],
            "modelProvider": s["model_provider_id"],
            "sandbox": mode,
            "approvalPolicy": s["approval_policy"],
            "approvalsReviewer": s["approvals_reviewer"],
            "personality": s["personality"],
            "config": config,
        }
        if "service_tier" in self.known_fields:
            params["serviceTier"] = s["service_tier"]
        return params

    def validate_result(self, result):
        try:
            self._validate_result(result)
        except (
            IpcError,
            ValueError,
            TypeError,
            AttributeError,
            KeyError,
            RecursionError,
        ) as exc:
            if isinstance(exc, ResumeSettingsError) and exc.code.startswith(
                "codex_resume_effective_"
            ):
                raise
            raise ResumeSettingsError(
                "codex_resume_effective_settings_unavailable"
            ) from exc

    def _validate_result(self, result):
        # Native returns legacy sandbox today; require the newly persisted full
        # effective profile. No waiting/retry or unbounded detached reader.
        try:
            data, stamp = read_source(self.path)
        except IpcError as exc:
            raise ResumeSettingsError(
                "codex_resume_effective_settings_unavailable"
            ) from exc
        if (
            stamp[:2] != self.stamp[:2]
            or not data.startswith(self.data)
            or len(data) == len(self.data)
        ):
            raise ResumeSettingsError("codex_resume_effective_settings_unavailable")
        observed = None
        for record in records(data[len(self.data) :]):
            p = record["payload"]
            if (
                record.get("type") != "event_msg"
                or p.get("type") != "thread_settings_applied"
                or p.get("thread_id") != self.thread["id"]
            ):
                raise ResumeSettingsError("codex_resume_effective_settings_unavailable")
            observed = p.get("thread_settings")
        try:
            validate_settings(observed, self.thread["cwd"])
        except IpcError as exc:
            raise ResumeSettingsError(
                "codex_resume_effective_settings_unavailable"
            ) from exc
        expected = projection(self.settings, self.known_fields)
        actual = projection(observed, self.known_fields)
        if (
            actual != expected
            or result.get("thread", {}).get("id") != self.thread["id"]
            or result.get("approvalPolicy") != expected["approval_policy"]
            or result.get("approvalsReviewer") != expected["approvals_reviewer"]
            or result.get("sandbox") != self.canonical_settings()["sandboxPolicy"]
            or any(
                result[key] != observed[source]
                for key, source in (
                    ("model", "model"),
                    ("modelProvider", "model_provider_id"),
                    ("reasoningEffort", "reasoning_effort"),
                    ("cwd", "cwd"),
                    ("serviceTier", "service_tier"),
                    ("personality", "personality"),
                )
                if key in result
            )
        ):
            raise ResumeSettingsError("codex_resume_effective_settings_mismatch")
        # Only fresh validated native evidence may fill the initial unknown tier.
        self.settings = {**self.settings, "service_tier": observed["service_tier"]}
        self.known_fields = SETTINGS_FIELDS

    def canonical_settings(self):
        s = self.settings
        mode, flags = policy(s)
        sandbox = {
            "type": {
                "workspace-write": "workspaceWrite",
                "read-only": "readOnly",
                "danger-full-access": "dangerFullAccess",
            }[mode]
        }
        if flags:
            sandbox.update(
                {
                    "writableRoots": flags["writable_roots"],
                    "networkAccess": flags["network_access"],
                    "excludeTmpdirEnvVar": flags["exclude_tmpdir_env_var"],
                    "excludeSlashTmp": flags["exclude_slash_tmp"],
                }
            )
        result = {
            "model": s["model"],
            "modelProvider": s["model_provider_id"],
            "effort": s["reasoning_effort"],
            "summary": s["reasoning_summary"],
            "personality": s["personality"],
            "collaborationMode": s["collaboration_mode"],
            "approvalPolicy": s["approval_policy"],
            "approvalsReviewer": s["approvals_reviewer"],
            "sandboxPolicy": sandbox,
            "runtimeWorkspaceRoots": s["runtime_workspace_roots"],
            "cwd": s["cwd"],
        }
        if "service_tier" in self.known_fields:
            result["serviceTier"] = s["service_tier"]
        return result
