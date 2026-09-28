"""Cold acquisition exercises real SDK serialization, never a preset resume result."""

import asyncio

import pytest
from codex_resume_fixture import RolloutNative
from connector.runtimes.codex.sdk.runtime_client import CodexStartTurnRequest
from test_codex_owner_absence_budget import THREAD, network


def test_cold_resume_uses_real_serializer_and_preserves_policy(tmp_path):
    async def run():
        async with network(silent=False) as (_, caller, _, facade, _):
            native = RolloutNative(tmp_path)
            facade.sdk = facade.operations.sdk = native.sdk
            result = await facade.start_turn(
                CodexStartTurnRequest(
                    thread_id=THREAD, content="once", client_message_id="one"
                )
            )
            assert result.turn_id == "new"
            resumes = [p for m, p in native.calls if m == "thread/resume"]
            assert len(resumes) == 1
            p = resumes[0]
            assert p["sandbox"] == "workspace-write"
            assert (
                p["approvalPolicy"] == "on-request" and p["approvalsReviewer"] == "user"
            )
            assert p["config"]["sandbox_workspace_write"] == {
                "writable_roots": [],
                "network_access": False,
                "exclude_tmpdir_env_var": False,
                "exclude_slash_tmp": False,
            }
            assert p["model"] == "gpt-6-luna" and p["modelProvider"] == "test"
            start = [p for m, p in native.calls if m == "turn/start"]
            assert len(start) == 1 and start[0]["effort"] == "low"
            assert start[0]["collaborationMode"]["mode"] == "default"
            assert caller.is_owner(THREAD)

    asyncio.run(run())


@pytest.mark.parametrize(
    "fault",
    [
        "history-base",
        "missing-settings",
        "missing-context",
        "wrong-id",
        "wrong-cwd",
        "wrong-head",
        "partial",
        "custom",
        "lineage",
        "changed",
        "replaced",
        "missing-post",
        "mismatch-post",
    ],
)
def test_refuses_unproven_authority_without_physical_turn(tmp_path, fault):
    async def run():
        from connector.runtimes.codex.coordination.wire import IpcError

        async with network(silent=False) as (router, caller, _, facade, _):
            n = RolloutNative(tmp_path)
            facade.sdk = facade.operations.sdk = n.sdk
            if fault == "history-base":
                n.records[0]["payload"]["history_base"] = {"thread_id": "other"}
            if fault == "missing-settings":
                n.records.pop(1)
            if fault == "missing-context":
                n.records.pop()
            if fault == "wrong-id":
                n.records[0]["payload"]["id"] = "other"
            if fault == "wrong-cwd":
                n.records[1]["payload"]["thread_settings"]["cwd"] = "/other"
            if fault == "wrong-head":
                n.records[-1]["payload"]["turn_id"] = "other"
            if fault == "custom":
                n.records[1]["payload"]["thread_settings"]["permission_profile"][
                    "file_system"
                ]["entries"].append(
                    {"path": {"type": "path", "path": "/custom"}, "access": "write"}
                )
            if fault == "lineage":
                n.records.append(
                    {"type": "event_msg", "payload": {"type": "thread_rolled_back"}}
                )
            n.save()
            if fault == "partial":
                n.path.write_bytes(n.path.read_bytes() + b"{")
            if fault in ("changed", "replaced"):

                async def change(count):
                    if count == 2:
                        if fault == "replaced":
                            n.path.unlink()
                        n.records.append(n.applied())
                        n.save()

                router.before_discovery = change
            if fault.endswith("-post"):
                n.post_mode = fault.removesuffix("-post")
            with pytest.raises(IpcError, match="codex_resume_"):
                await facade.start_turn(
                    CodexStartTurnRequest(thread_id=THREAD, content="once")
                )
            assert not [p for m, p in n.calls if m == "turn/start"]
            assert len([p for m, p in n.calls if m == "thread/resume"]) == int(
                fault.endswith("-post")
            )
            assert not caller.is_owner(THREAD) and not facade.acquiring

    asyncio.run(run())


def test_newer_settings_only_record_is_authority(tmp_path):
    async def run():
        async with network(silent=False) as (_, _, _, facade, _):
            n = RolloutNative(tmp_path)
            n.settings["approval_policy"] = "never"
            n.records.append(n.applied())
            n.save()
            facade.sdk = facade.operations.sdk = n.sdk
            await facade.start_turn(
                CodexStartTurnRequest(
                    thread_id=THREAD, content="once", model="gpt-6-sol"
                )
            )
            resume = next(p for m, p in n.calls if m == "thread/resume")
            start = next(p for m, p in n.calls if m == "turn/start")
            assert resume["approvalPolicy"] == "never"
            assert start["model"] == "gpt-6-sol"
            assert start["sandboxPolicy"]["type"] == "workspaceWrite"
            assert start["collaborationMode"]["settings"]["model"] == "gpt-6-sol"

    asyncio.run(run())


def test_owner_appearing_in_final_guard_prevents_resume(tmp_path):
    async def run():
        async with network(silent=False) as (router, caller, owner, facade, _):
            n = RolloutNative(tmp_path)
            facade.sdk = facade.operations.sdk = n.sdk

            async def claim(count):
                if count == 2:
                    await owner.claim(
                        THREAD, {"id": THREAD, "turns": [], "requests": []}
                    )

            router.before_discovery = claim
            await facade._acquire(THREAD)
            assert caller.is_follower(THREAD)
            assert not [p for m, p in n.calls if m == "thread/resume"]

    asyncio.run(run())


def test_scan_caps_and_symlink_are_not_authority(tmp_path, monkeypatch):
    from connector.runtimes.codex.coordination import resume_settings as module
    from connector.runtimes.codex.coordination.wire import IpcError

    n = RolloutNative(tmp_path)
    monkeypatch.setattr(module, "MAX_ROLLOUT_BYTES", 4)
    with pytest.raises(IpcError):
        module.ResumeSettings.resolve({"thread": n.thread()}, THREAD)
    monkeypatch.setattr(module, "MAX_ROLLOUT_BYTES", 100000)
    monkeypatch.setattr(module, "MAX_RECORDS", 2)
    with pytest.raises(IpcError):
        module.ResumeSettings.resolve({"thread": n.thread()}, THREAD)
    monkeypatch.setattr(module, "MAX_RECORDS", 1000)
    monkeypatch.setattr(module, "MAX_RECORD_BYTES", 5)
    with pytest.raises(IpcError):
        module.ResumeSettings.resolve({"thread": n.thread()}, THREAD)
    link = tmp_path / "link"
    link.symlink_to(n.path)
    with pytest.raises(IpcError):
        module.ResumeSettings.resolve(
            {"thread": {**n.thread(), "path": str(link)}}, THREAD
        )


@pytest.mark.parametrize("phase", ["thread/read", "thread/resume"])
def test_native_disconnect_cannot_publish_stale_acquisition(tmp_path, phase):
    async def run():
        from connector.runtimes.codex.coordination.wire import IpcError

        async with network(silent=False) as (_, caller, _, facade, _):
            n = RolloutNative(tmp_path)
            facade.sdk = facade.operations.sdk = n.sdk
            original = n.request

            async def request(method, params, **kwargs):
                value = await original(method, params, **kwargs)
                if method == phase:
                    await facade._native_event(
                        {"method": "native/disconnected", "params": {}}, 1
                    )
                return value

            n.request = request
            with pytest.raises(IpcError, match="codex_resume_"):
                await facade.start_turn(
                    CodexStartTurnRequest(thread_id=THREAD, content="once")
                )
            assert not caller.is_owner(THREAD) and not facade.acquiring
            assert not [p for m, p in n.calls if m == "turn/start"]

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["thread/read", "thread/resume"])
def test_cancelled_acquisition_releases_speculation(tmp_path, phase):
    async def run():
        async with network(silent=False) as (_, caller, _, facade, _):
            n = RolloutNative(tmp_path)
            facade.sdk = facade.operations.sdk = n.sdk
            original = n.request
            entered = asyncio.Event()

            async def request(method, params, **kwargs):
                if method == phase:
                    entered.set()
                    await asyncio.Event().wait()
                return await original(method, params, **kwargs)

            n.request = request
            task = asyncio.create_task(
                facade.start_turn(
                    CodexStartTurnRequest(thread_id=THREAD, content="once")
                )
            )
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not caller.is_owner(THREAD) and not facade.acquiring
            assert not facade.locks[THREAD].locked()

    asyncio.run(run())


def test_complete_permission_choice_is_distinct_from_inherited_resume(tmp_path):
    async def run():
        async with network(silent=False) as (_, _, _, facade, _):
            n = RolloutNative(tmp_path)
            facade.sdk = facade.operations.sdk = n.sdk
            await facade.start_turn(
                CodexStartTurnRequest(
                    thread_id=THREAD,
                    content="once",
                    approval_policy="never",
                    approvals_reviewer="user",
                    sandbox="danger-full-access",
                )
            )
            resume = next(p for m, p in n.calls if m == "thread/resume")
            start = next(p for m, p in n.calls if m == "turn/start")
            assert resume["sandbox"] == "workspace-write"
            assert start["sandboxPolicy"] == {"type": "dangerFullAccess"}
            assert start["approvalPolicy"] == "never"

    asyncio.run(run())


def test_settings_only_override_acquires_latest_disabled_policy_without_turn(tmp_path):
    async def run():
        async with network(silent=False) as (_, caller, _, facade, _):
            n = RolloutNative(tmp_path)
            n.settings["permission_profile"] = {"type": "disabled"}
            n.records.append(n.applied())
            n.save()
            original = n.request

            async def request(method, params, **kwargs):
                if method == "thread/settings/update":
                    from types import SimpleNamespace

                    n.calls.append((method, params))
                    return SimpleNamespace(root={"applied": True})
                return await original(method, params, **kwargs)

            n.request = request
            facade.sdk = facade.operations.sdk = n.sdk
            result = await facade.owner_operation(
                THREAD,
                "thread-follower-update-thread-settings",
                {
                    "threadSettings": {
                        "approvalPolicy": "on-request",
                        "approvalsReviewer": "user",
                        "sandboxPolicy": {
                            "type": "workspaceWrite",
                            "writableRoots": [],
                            "networkAccess": False,
                            "excludeTmpdirEnvVar": False,
                            "excludeSlashTmp": False,
                        },
                    }
                },
            )
            assert result["applied"] and caller.is_owner(THREAD)
            assert (
                next(p for m, p in n.calls if m == "thread/resume")["sandbox"]
                == "danger-full-access"
            )
            assert not [p for m, p in n.calls if m == "turn/start"]
            assert (
                next(p for m, p in n.calls if m == "thread/settings/update")[
                    "sandboxPolicy"
                ]["type"]
                == "workspaceWrite"
            )

    asyncio.run(run())
