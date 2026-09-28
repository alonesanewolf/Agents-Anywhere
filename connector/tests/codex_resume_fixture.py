"""Native-shaped persisted settings and low-level SDK dispatcher for tests."""

import json
from copy import deepcopy
from types import SimpleNamespace

from connector.runtimes.codex.sdk.client import CodexSdkClient


def profile(cwd):
    entries = [
        {"path": {"type": "special", "value": {"kind": "root"}}, "access": "read"},
        {"path": {"type": "path", "path": cwd}, "access": "write"},
    ]
    entries += [
        {"path": {"type": "special", "value": {"kind": kind}}, "access": "write"}
        for kind in ("slash_tmp", "tmpdir")
    ]
    entries += [
        {
            "path": {"type": "path", "path": f"{cwd}/{name}"},
            "access": "read",
            "missing_path_behavior": "skip",
        }
        for name in (".git", ".agents", ".codex")
    ]
    return {
        "type": "managed",
        "file_system": {"type": "restricted", "entries": entries},
        "network": "restricted",
    }


class RolloutNative:
    def __init__(self, tmp_path, thread_id="owner-absence-test"):
        self.thread_id, self.cwd = thread_id, str(tmp_path)
        self.path = tmp_path / "rollout.jsonl"
        self.settings = {
            "model": "gpt-6-luna",
            "model_provider_id": "test",
            "service_tier": "default",
            "approval_policy": "on-request",
            "approvals_reviewer": "user",
            "permission_profile": profile(self.cwd),
            "cwd": self.cwd,
            "runtime_workspace_roots": [self.cwd],
            "reasoning_effort": "low",
            "reasoning_summary": "auto",
            "personality": "pragmatic",
            "collaboration_mode": {
                "mode": "default",
                "settings": {
                    "model": "gpt-6-luna",
                    "reasoning_effort": "low",
                    "developer_instructions": None,
                },
            },
            "disabled_plugin_ids": [],
        }
        self.sandbox = {
            "type": "workspace-write",
            "network_access": False,
            "exclude_tmpdir_env_var": False,
            "exclude_slash_tmp": False,
        }
        self.records = [
            {"type": "session_meta", "payload": {"id": thread_id, "cwd": self.cwd}},
            self.applied(),
            {
                "type": "turn_context",
                "payload": {
                    "turn_id": "old",
                    "cwd": self.cwd,
                    "permission_profile": profile(self.cwd),
                    "sandbox_policy": self.sandbox,
                },
            },
        ]
        self.save()
        self.calls, self.post_mode = [], "append"
        self.sdk = object.__new__(CodexSdkClient)
        self.sdk._client = SimpleNamespace(_client=self)
        self.sdk._model_gateway = None
        self.sdk.native_generation = 1
        self.sdk._native_bridge = None

    def applied(self):
        return {
            "type": "event_msg",
            "payload": {
                "type": "thread_settings_applied",
                "thread_id": self.thread_id,
                "thread_settings": deepcopy(self.settings),
            },
        }

    def save(self):
        self.path.write_text("".join(json.dumps(r) + "\n" for r in self.records))

    def thread(self):
        return {
            "id": self.thread_id,
            "cwd": self.cwd,
            "path": str(self.path),
            "turns": [{"id": "old", "status": "completed", "items": []}],
            "status": {"type": "idle"},
        }

    async def request(self, method, params, **kwargs):
        self.calls.append((method, deepcopy(params)))
        if method == "thread/read":
            return SimpleNamespace(root={"thread": self.thread()})
        if method == "thread/resume":
            effective = deepcopy(self.settings)
            # Permission effects are reconstructed from the actual wire request
            # and independent defaults, not copied from historical authority.
            effective["approval_policy"] = params.get("approvalPolicy", "never")
            effective["approvals_reviewer"] = params.get("approvalsReviewer", "user")
            cwd = params.get("cwd", self.cwd)
            cfg = params.get("config", {}).get("sandbox_workspace_write", {})
            mode = params.get("sandbox", "danger-full-access")
            effective["cwd"] = cwd
            effective["runtime_workspace_roots"] = [cwd]
            if mode == "workspace-write":
                roots = [cwd, *cfg.get("writable_roots", [])]
                effective["runtime_workspace_roots"] = roots
                entries = [
                    {
                        "path": {"type": "special", "value": {"kind": "root"}},
                        "access": "read",
                    }
                ]
                for root in roots:
                    entries += profile(root)["file_system"]["entries"][1:2]
                    entries += profile(root)["file_system"]["entries"][4:]
                for kind, key in (
                    ("tmpdir", "exclude_tmpdir_env_var"),
                    ("slash_tmp", "exclude_slash_tmp"),
                ):
                    if not cfg.get(key, False):
                        entries.append(
                            {
                                "path": {"type": "special", "value": {"kind": kind}},
                                "access": "write",
                            }
                        )
                effective["permission_profile"] = {
                    "type": "managed",
                    "file_system": {"type": "restricted", "entries": entries},
                    "network": "enabled"
                    if cfg.get("network_access", False)
                    else "restricted",
                }
            elif mode == "read-only":
                effective["permission_profile"] = profile(cwd)
                effective["permission_profile"]["file_system"]["entries"] = profile(
                    cwd
                )["file_system"]["entries"][:1]
            else:
                effective["permission_profile"] = {"type": "disabled"}
            if self.post_mode == "mismatch":
                effective["permission_profile"] = {"type": "disabled"}
            if self.post_mode != "missing":
                self.records.append(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_settings_applied",
                            "thread_id": self.thread_id,
                            "thread_settings": effective,
                        },
                    }
                )
                self.save()
            sandbox = {"type": "dangerFullAccess"}
            if effective["permission_profile"]["type"] == "managed":
                if params.get("sandbox") == "read-only":
                    sandbox = {"type": "readOnly"}
                else:
                    cfg = params.get("config", {}).get("sandbox_workspace_write", {})
                    sandbox = {
                        "type": "workspaceWrite",
                        "writableRoots": cfg.get("writable_roots", []),
                        "networkAccess": cfg.get("network_access", False),
                        "excludeTmpdirEnvVar": cfg.get("exclude_tmpdir_env_var", False),
                        "excludeSlashTmp": cfg.get("exclude_slash_tmp", False),
                    }
            return SimpleNamespace(
                root={
                    "thread": self.thread(),
                    "model": effective["model"],
                    "modelProvider": effective["model_provider_id"],
                    "reasoningEffort": effective["reasoning_effort"],
                    "approvalPolicy": effective["approval_policy"],
                    "approvalsReviewer": effective["approvals_reviewer"],
                    "sandbox": sandbox,
                    "cwd": self.cwd,
                }
            )
        if method == "turn/start":
            return SimpleNamespace(
                root={"turn": {"id": "new", "status": "inProgress", "items": []}}
            )
        raise AssertionError(method)
