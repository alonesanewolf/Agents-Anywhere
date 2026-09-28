"""Cold acquisition exercises real SDK serialization, never a preset resume result."""

import asyncio
from copy import deepcopy

import pytest
from codex_resume_fixture import RolloutNative
from test_codex_owner_absence_budget import THREAD, network

from connector.runtimes.codex.coordination.wire import IpcError
from connector.runtimes.codex.sdk.runtime_client import CodexStartTurnRequest


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


@pytest.mark.parametrize("nullable", [False, True], ids=["values", "explicit-nulls"])
def test_nonpermission_settings_are_applied_from_serialized_resume(tmp_path, nullable):
    async def run():
        async with network(silent=False) as (_, caller, _, facade, _):
            effort = None if nullable else "max"
            native = RolloutNative(
                tmp_path,
                settings={
                    "model": "gpt-example",
                    "model_provider_id": "openai",
                    "reasoning_effort": effort,
                    "reasoning_summary": None if nullable else "concise",
                    "service_tier": None if nullable else "priority",
                    "personality": None if nullable else "pragmatic",
                    "collaboration_mode": {
                        "mode": "plan",
                        "settings": {
                            "model": "gpt-example",
                            "reasoning_effort": effort,
                            "developer_instructions": "configured plan instructions",
                        },
                    },
                },
                native_defaults={
                    # Resume has no mode field: an independently matching native
                    # default is a condition of this modeled success, not proof
                    # that installed native resume preserves Plan or nulls.
                    "service_tier": None if nullable else "flex",
                    "collaboration_mode": {
                        "mode": "plan",
                        "developer_instructions": "configured plan instructions",
                    },
                },
            )
            facade.sdk = facade.operations.sdk = native.sdk
            await facade.start_turn(
                CodexStartTurnRequest(thread_id=THREAD, content="once")
            )
            wire = next(p for m, p in native.calls if m == "thread/resume")
            assert wire["model"] == "gpt-example"
            assert wire["modelProvider"] == "openai"
            assert wire["config"]["model_reasoning_effort"] == effort
            assert wire["config"]["model_reasoning_summary"] == (
                None if nullable else "concise"
            )
            if nullable:
                assert "serviceTier" not in wire
            else:
                assert wire["serviceTier"] == "priority"
            assert wire["personality"] == (None if nullable else "pragmatic")
            assert "collaborationMode" not in wire
            settings = caller.get_state(THREAD)["latestThreadSettings"]
            assert settings["model"] == "gpt-example"
            assert settings["modelProvider"] == "openai"
            assert settings["effort"] == effort
            assert settings["summary"] == (None if nullable else "concise")
            assert settings["serviceTier"] == (None if nullable else "priority")
            assert settings["personality"] == (None if nullable else "pragmatic")
            assert settings["collaborationMode"] == {
                "mode": "plan",
                "settings": {
                    "model": "gpt-example",
                    "reasoning_effort": effort,
                    "developer_instructions": "configured plan instructions",
                },
            }
            starts = [p for m, p in native.calls if m == "turn/start"]
            assert len(starts) == 1
            if nullable:
                assert "serviceTier" not in starts[0]
            else:
                assert starts[0]["serviceTier"] == "priority"

    asyncio.run(run())


@pytest.mark.parametrize("fault", ["omitted", "changed"])
@pytest.mark.parametrize(
    "path",
    [
        ("model",),
        ("modelProvider",),
        ("serviceTier",),
        ("personality",),
        ("config", "model_reasoning_effort"),
        ("config", "model_reasoning_summary"),
    ],
)
def test_nonpermission_wire_mismatch_fails_closed(tmp_path, path, fault):
    async def run():
        async with network(silent=False) as (_, caller, _, facade, _):
            native = RolloutNative(tmp_path)
            original = native.request

            async def request(method, params, **kwargs):
                params = deepcopy(params)
                if method == "thread/resume":
                    target = params["config"] if len(path) == 2 else params
                    if fault == "omitted":
                        target.pop(path[-1])
                    else:
                        target[path[-1]] = "different-native-value"
                return await original(method, params, **kwargs)

            native.request = request
            facade.sdk = facade.operations.sdk = native.sdk
            with pytest.raises(
                IpcError, match="codex_resume_effective_settings_mismatch"
            ):
                await facade.start_turn(
                    CodexStartTurnRequest(thread_id=THREAD, content="never")
                )
            assert len([p for m, p in native.calls if m == "thread/resume"]) == 1
            assert not [p for m, p in native.calls if m == "turn/start"]
            assert not caller.is_owner(THREAD) and not facade.acquiring

    asyncio.run(run())


@pytest.mark.parametrize(
    "field", ["service_tier", "personality", "mode", "instructions"]
)
def test_incompatible_native_defaults_cannot_echo_historical_authority(tmp_path, field):
    async def run():
        async with network(silent=False) as (_, caller, _, facade, _):
            native = RolloutNative(tmp_path)
            if field in {"service_tier", "personality"}:
                native.settings[field] = None
                if field == "personality":
                    original = native.request

                    async def request(method, params, **kwargs):
                        if method == "thread/resume":
                            params = deepcopy(params)
                            params.pop("personality")
                        return await original(method, params, **kwargs)

                    native.request = request
            elif field == "mode":
                native.settings["collaboration_mode"]["mode"] = "plan"
            else:
                native.settings["collaboration_mode"]["settings"][
                    "developer_instructions"
                ] = "old instructions"
            native.records[1] = native.applied()
            native.records[2] = native.context()
            native.save()
            facade.sdk = facade.operations.sdk = native.sdk
            with pytest.raises(
                IpcError, match="codex_resume_effective_settings_mismatch"
            ):
                await facade.start_turn(
                    CodexStartTurnRequest(thread_id=THREAD, content="never")
                )
            assert len([p for m, p in native.calls if m == "thread/resume"]) == 1
            assert not [p for m, p in native.calls if m == "turn/start"]
            assert not caller.is_owner(THREAD) and not facade.acquiring

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
                # Missing settings cannot authorize an incomplete legacy context.
                n.records[-1]["payload"].pop("workspace_roots")
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


def restricted_roots(native):
    """Native authority with nondefault tmp exclusions and an additional root."""
    extra = native.cwd + "/additional"
    native.settings["runtime_workspace_roots"] = [native.cwd, extra]
    entries = native.settings["permission_profile"]["file_system"]["entries"]
    entries[:] = [
        e
        for e in entries
        if e["path"].get("value", {}).get("kind") not in ("tmpdir", "slash_tmp")
    ]
    entries.append({"path": {"type": "path", "path": extra}, "access": "write"})
    entries.extend(
        {
            "path": {"type": "path", "path": extra + "/" + name},
            "access": "read",
            "missing_path_behavior": "skip",
        }
        for name in (".git", ".agents", ".codex")
    )
    native.records[1] = native.applied()
    native.sandbox.update(
        writable_roots=[extra], exclude_tmpdir_env_var=True, exclude_slash_tmp=True
    )
    native.records[-1] = native.context()
    native.save()
    return extra


@pytest.mark.parametrize(
    "choice",
    [
        {"sandbox": "workspace-write"},
        {"sandbox": "workspace-write", "approval_policy": "on-request"},
        {"sandbox": "workspace-write", "approvals_reviewer": "user"},
        {"approval_policy": "on-request"},
        {"approvals_reviewer": "user"},
        {"approval_policy": "on-request", "approvals_reviewer": "user"},
    ],
)
def test_partial_permission_choice_is_rejected_before_acquisition(tmp_path, choice):
    async def run():
        from connector.runtime_protocol import RuntimeInvalidRequestError

        async with network(silent=False) as (router, caller, _, facade, _):
            n = RolloutNative(tmp_path)
            restricted_roots(n)
            facade.sdk = facade.operations.sdk = n.sdk
            with pytest.raises(
                RuntimeInvalidRequestError, match="incomplete permission choice"
            ):
                await facade.start_turn(
                    CodexStartTurnRequest(thread_id=THREAD, content="once", **choice)
                )
            assert n.calls == [] and router.discovery_count == 0
            assert not caller.is_owner(THREAD)

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["claim", "pending"])
@pytest.mark.parametrize("ending", ["cancel", "error"])
def test_failed_claim_publication_releases_partial_owner_and_reacquires(
    tmp_path, ending, phase
):
    async def run():
        async with network(silent=False) as (router, caller, _, facade, _):
            n = RolloutNative(tmp_path)
            facade.sdk = facade.operations.sdk = n.sdk
            entered = asyncio.Event()
            snapshot = caller._snapshot
            publications = 0
            error = RuntimeError("publication failed")

            async def publish(thread_id):
                nonlocal publications
                publications += 1
                if phase == "pending" and publications == 1:
                    await snapshot(thread_id)
                    await facade._native_event(
                        {
                            "method": "thread/status/changed",
                            "params": {
                                "threadId": thread_id,
                                "status": {"type": "idle"},
                            },
                        },
                        1,
                    )
                    return
                assert caller.is_owner(thread_id)
                entered.set()
                if ending == "error":
                    raise error
                await asyncio.Event().wait()

            caller._snapshot = publish
            task = asyncio.create_task(
                facade.start_turn(
                    CodexStartTurnRequest(thread_id=THREAD, content="once")
                )
            )
            await asyncio.wait_for(entered.wait(), 1)
            if ending == "cancel":
                task.cancel()
            with pytest.raises(
                asyncio.CancelledError if ending == "cancel" else RuntimeError
            ) as caught:
                await task
            if ending == "error":
                assert caught.value is error
            assert (
                not caller.is_owner(THREAD)
                and THREAD not in facade.owned
                and not facade.acquiring
            )
            assert not [p for m, p in n.calls if m == "turn/start"]
            caller._snapshot = snapshot
            await facade._native_event(
                {"method": "native/disconnected", "params": {}}, 1
            )
            assert not caller.is_owner(THREAD)
            before = router.discovery_count
            await facade._acquire(THREAD)
            assert router.discovery_count == before + 2
            assert len([p for m, p in n.calls if m == "thread/resume"]) == 2
            assert caller.is_owner(THREAD) and THREAD in facade.owned

    asyncio.run(run())


@pytest.mark.parametrize(
    "choice",
    [
        {},
        {"model": "gpt-6-sol"},
        {
            "approval_policy": "on-request",
            "approvals_reviewer": "user",
            "sandbox": "workspace-write",
        },
        {"approval_policy": "never", "sandbox": "danger-full-access"},
    ],
)
def test_nondefault_resume_policy_and_complete_choice_wire(tmp_path, choice):
    async def run():
        async with network(silent=False) as (_, _, _, facade, _):
            n = RolloutNative(tmp_path)
            extra = restricted_roots(n)
            facade.sdk = facade.operations.sdk = n.sdk
            await facade.start_turn(
                CodexStartTurnRequest(thread_id=THREAD, content="once", **choice)
            )
            resume = next(p for m, p in n.calls if m == "thread/resume")
            start = next(p for m, p in n.calls if m == "turn/start")
            assert len([p for m, p in n.calls if m == "thread/resume"]) == 1
            assert len([p for m, p in n.calls if m == "turn/start"]) == 1
            assert resume["config"]["sandbox_workspace_write"] == {
                "writable_roots": [extra],
                "network_access": False,
                "exclude_tmpdir_env_var": True,
                "exclude_slash_tmp": True,
            }
            if choice.get("sandbox") == "danger-full-access":
                assert start["sandboxPolicy"] == {"type": "dangerFullAccess"}
            elif choice.get("sandbox") == "workspace-write":
                assert start["sandboxPolicy"] == {
                    "type": "workspaceWrite",
                    "writableRoots": [],
                    "networkAccess": False,
                    "excludeTmpdirEnvVar": False,
                    "excludeSlashTmp": False,
                }
            else:
                assert start["sandboxPolicy"] == {
                    "type": "workspaceWrite",
                    "writableRoots": [extra],
                    "networkAccess": False,
                    "excludeTmpdirEnvVar": True,
                    "excludeSlashTmp": True,
                }

    asyncio.run(run())


def test_cancelled_claim_does_not_release_replacement_owner(tmp_path):
    async def run():
        async with network(silent=False) as (_, caller, _, facade, _):
            n = RolloutNative(tmp_path)
            facade.sdk = facade.operations.sdk = n.sdk
            entered = asyncio.Event()
            snapshot = caller._snapshot

            async def suspended(thread_id):
                entered.set()
                await asyncio.Event().wait()

            caller._snapshot = suspended
            task = asyncio.create_task(facade._acquire(THREAD))
            await asyncio.wait_for(entered.wait(), 1)
            old = caller.ownership_token(THREAD)
            state = {"id": THREAD, "turns": [], "requests": [], "replacement": True}
            caller._snapshot = snapshot
            await caller.claim(THREAD, state)
            replacement = caller.ownership_token(THREAD)
            assert replacement is not old
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert caller.ownership_token(THREAD) is replacement
            assert caller.get_state(THREAD) == state
            assert THREAD not in facade.owned
            assert not facade.acquiring
            assert not [p for m, p in n.calls if m == "turn/start"]

    asyncio.run(run())


@pytest.mark.parametrize("ending", ["cancel", "error"])
def test_superseded_initial_claim_never_adopts_or_observes_replacement(
    tmp_path, ending
):
    async def run():
        async with network(silent=False) as (_, caller, _, facade, _):
            native = RolloutNative(tmp_path)
            facade.sdk = facade.operations.sdk = native.sdk
            entered, resume, pending = asyncio.Event(), asyncio.Event(), asyncio.Event()
            snapshot = caller._snapshot
            error = RuntimeError("pending publication failed")

            async def initial(thread_id):
                await snapshot(thread_id)
                entered.set()
                await resume.wait()

            async def second(thread_id):
                await snapshot(thread_id)
                pending.set()
                if ending == "error":
                    raise error
                await asyncio.Event().wait()

            caller._snapshot = initial
            task = asyncio.create_task(
                facade.start_turn(
                    CodexStartTurnRequest(thread_id=THREAD, content="once")
                )
            )
            waiter = asyncio.create_task(pending.wait())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                original = caller.ownership_token(THREAD)
                caller._snapshot = snapshot
                state = {
                    "id": THREAD,
                    "turns": [],
                    "requests": [],
                    "status": {"type": "active"},
                    "replacement": True,
                }
                revision = await caller.claim(THREAD, state)
                replacement = caller.ownership_token(THREAD)
                assert replacement is not original
                for method, params in [
                    ("thread/status/changed", {"status": {"type": "idle"}}),
                    ("thread/settings/changed", {"settings": {"model": "queued"}}),
                ]:
                    await facade._native_event(
                        {"method": method, "params": {"threadId": THREAD, **params}}, 1
                    )
                epochs = deepcopy(facade.operations.settings_epochs)
                caller._snapshot = second
                resume.set()
                # A correct implementation rejects before second publication;
                # the old implementation reaches it and is cancelled/fails there.
                done, _ = await asyncio.wait(
                    {task, waiter}, timeout=1, return_when=asyncio.FIRST_COMPLETED
                )
                assert done
                if pending.is_set() and ending == "cancel":
                    task.cancel()
                outcome = (await asyncio.gather(task, return_exceptions=True))[0]
                assert caller.ownership_token(THREAD) is replacement
                assert caller.get_state(THREAD) == state
                assert caller.get_revision(THREAD) == revision
                assert not pending.is_set()
                assert facade.operations.settings_epochs == epochs
                assert THREAD not in facade.owned and not facade.acquiring
                assert (
                    isinstance(outcome, IpcError) and outcome.code == "claim-superseded"
                )
                assert len([p for m, p in native.calls if m == "thread/resume"]) == 1
                assert not [p for m, p in native.calls if m == "turn/start"]
            finally:
                task.cancel()
                waiter.cancel()
                await asyncio.gather(task, waiter, return_exceptions=True)
                caller._snapshot = snapshot

    asyncio.run(run())


@pytest.mark.parametrize("replacement_kind", ["peer", "facade"])
@pytest.mark.parametrize("ending", ["cancel", "error", "return"])
def test_superseded_pending_claim_preserves_replacement_and_its_bookkeeping(
    tmp_path, ending, replacement_kind
):
    async def run():
        async with network(silent=False) as (_, caller, _, facade, _):
            native = RolloutNative(tmp_path)
            facade.sdk = facade.operations.sdk = native.sdk
            entered, resume = asyncio.Event(), asyncio.Event()
            snapshot = caller._snapshot
            error = RuntimeError("pending publication failed")
            publications = 0

            async def publish(thread_id):
                nonlocal publications
                publications += 1
                await snapshot(thread_id)
                if publications == 1:
                    await facade._native_event(
                        {
                            "method": "thread/status/changed",
                            "params": {
                                "threadId": thread_id,
                                "status": {"type": "idle"},
                            },
                        },
                        1,
                    )
                    return
                entered.set()
                await resume.wait()
                if ending == "error":
                    raise error

            caller._snapshot = publish
            task = asyncio.create_task(
                facade.start_turn(
                    CodexStartTurnRequest(thread_id=THREAD, content="once")
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), 1)
                original = caller.ownership_token(THREAD)
                assert THREAD in facade.owned
                caller._snapshot = snapshot
                state = {"id": THREAD, "turns": [], "requests": [], "replacement": True}
                if replacement_kind == "facade":
                    facade.acquiring[THREAD] = []
                    await facade._claim_observed(THREAD, state)
                else:
                    await caller.claim(THREAD, state)
                replacement = caller.ownership_token(THREAD)
                revision = caller.get_revision(THREAD)
                assert replacement is not original
                if ending == "cancel":
                    task.cancel()
                else:
                    resume.set()
                outcome = (await asyncio.gather(task, return_exceptions=True))[0]
                assert caller.ownership_token(THREAD) is replacement
                assert caller.get_state(THREAD) == state
                assert caller.get_revision(THREAD) == revision
                assert (THREAD in facade.owned) == (replacement_kind == "facade")
                assert not facade.acquiring
                if ending == "cancel":
                    assert isinstance(outcome, asyncio.CancelledError)
                elif ending == "error":
                    assert outcome is error
                else:
                    assert (
                        isinstance(outcome, IpcError)
                        and outcome.code == "claim-superseded"
                    )
                assert len([p for m, p in native.calls if m == "thread/resume"]) == 1
                assert not [p for m, p in native.calls if m == "turn/start"]
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                caller._snapshot = snapshot

    asyncio.run(run())


@pytest.mark.parametrize("field", ["approval_policy", "approvals_reviewer", "sandbox"])
def test_invalid_complete_permission_choice_is_rejected_before_acquisition(
    tmp_path, field
):
    async def run():
        from connector.runtime_protocol import RuntimeInvalidRequestError

        async with network(silent=False) as (router, _, _, facade, _):
            n = RolloutNative(tmp_path)
            facade.sdk = facade.operations.sdk = n.sdk
            choice = {
                "approval_policy": "on-request",
                "approvals_reviewer": "user",
                "sandbox": "workspace-write",
            }
            choice[field] = ""
            with pytest.raises(
                RuntimeInvalidRequestError, match="unsupported permission choice"
            ):
                await facade.start_turn(
                    CodexStartTurnRequest(thread_id=THREAD, content="once", **choice)
                )
            assert n.calls == [] and router.discovery_count == 0

    asyncio.run(run())
