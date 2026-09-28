"""Current initial context crosses the real resolver, SDK wire and peer boundary."""

import asyncio
from copy import deepcopy

import pytest
from codex_resume_fixture import RolloutNative
from test_codex_owner_absence_budget import THREAD, network

from connector.runtimes.codex.coordination.resume_settings import (
    CONTEXT_KNOWN_FIELDS,
    ResumeSettings,
)
from connector.runtimes.codex.coordination.wire import IpcError
from connector.runtimes.codex.sdk.runtime_client import CodexStartTurnRequest


def initial_native(tmp_path, *, tier="flex"):
    native = RolloutNative(
        tmp_path,
        settings={"reasoning_summary": "detailed"},
        native_defaults={"service_tier": tier},
        thread_fields={"historyMode": "paginated"},
    )
    meta = deepcopy(native.records[0])
    meta["payload"]["history_mode"] = "paginated"
    context = native.context()
    entries = context["payload"]["permission_profile"]["file_system"]["entries"]
    entries.extend(deepcopy([entries[4], entries[6]]))
    context["payload"]["file_system_sandbox_policy"]["entries"] = deepcopy(entries)
    # Native-shaped ordinals: metadata first, only full context at 6, complete at 13.
    native.records = [meta]
    native.records += [{"type": "event_msg", "payload": {"type": "task_started"}}] * 4
    native.records += [context]
    native.records += [{"type": "response_item", "payload": {"type": "message"}}] * 6
    native.records += [{"type": "event_msg", "payload": {"type": "task_complete"}}]
    native.save()
    return native


def omit_post_tier(native, *, response_tier=None, contradict_response=False):
    original = native.request

    async def request(method, params, **kwargs):
        result = await original(method, params, **kwargs)
        if method == "thread/resume":
            native.records[-1]["payload"]["thread_settings"].pop("service_tier", None)
            native.save()
            if contradict_response:
                result.root["serviceTier"] = response_tier
        return result

    native.request = request


@pytest.mark.parametrize("tier", [None, "flex", "priority"])
def test_initial_context_omits_unknown_tier_then_learns_validated_native_value(
    tmp_path, tier
):
    async def run():
        async with network(silent=False) as (_, caller, _, facade, _):
            native = initial_native(tmp_path, tier=tier)
            authority = ResumeSettings.resolve({"thread": native.thread()}, THREAD)
            assert authority.source_kind == "initial_context"
            assert "service_tier" not in authority.known_fields
            assert "serviceTier" not in authority.params()
            assert "serviceTier" not in authority.canonical_settings()
            facade.sdk = facade.operations.sdk = native.sdk
            await facade.start_turn(
                CodexStartTurnRequest(thread_id=THREAD, content="once")
            )
            resume = [p for m, p in native.calls if m == "thread/resume"]
            assert len(resume) == 1
            assert "serviceTier" not in resume[0]
            assert resume[0]["modelProvider"] == "test"
            assert resume[0]["config"]["model_reasoning_summary"] == "detailed"
            assert (
                resume[0]["config"]["sandbox_workspace_write"]["writable_roots"] == []
            )
            start = [p for m, p in native.calls if m == "turn/start"]
            assert len(start) == 1
            assert ("serviceTier" in start[0]) == (tier is not None)
            if tier is not None:
                assert start[0]["serviceTier"] == tier
            assert (
                start[0]["collaborationMode"]["settings"]["developer_instructions"]
                is None
            )
            assert (
                caller.get_state(THREAD)["latestThreadSettings"]["serviceTier"] == tier
            )
            assert caller.is_owner(THREAD)

    asyncio.run(run())


@pytest.mark.parametrize("omit_response", [False, True])
def test_initial_unknown_tier_learns_native_omitted_tier_as_null(
    tmp_path, omit_response
):
    async def run():
        async with network(silent=False) as (_, caller, _, facade, _):
            native = initial_native(tmp_path, tier=None)
            native.response_omit = ("serviceTier",) if omit_response else ()
            omit_post_tier(native)
            authority = ResumeSettings.resolve({"thread": native.thread()}, THREAD)
            assert authority.source_kind == "initial_context"
            assert "service_tier" not in authority.known_fields
            assert not authority.tier_wire_present
            assert "serviceTier" not in authority.params()
            facade.sdk = facade.operations.sdk = native.sdk
            await facade.start_turn(
                CodexStartTurnRequest(thread_id=THREAD, content="once")
            )
            resume = [p for m, p in native.calls if m == "thread/resume"]
            assert len(resume) == 1 and "serviceTier" not in resume[0]
            assert (
                "service_tier" not in native.records[-1]["payload"]["thread_settings"]
            )
            assert (
                caller.get_state(THREAD)["latestThreadSettings"]["serviceTier"] is None
            )
            start = [p for m, p in native.calls if m == "turn/start"]
            assert len(start) == 1 and "serviceTier" not in start[0]
            assert caller.is_owner(THREAD)

    asyncio.run(run())


@pytest.mark.parametrize("later_context", [False, True])
def test_newest_complete_snapshot_missing_tier_has_null_authority(
    tmp_path, later_context
):
    native = initial_native(tmp_path)
    native.settings["service_tier"] = "priority"
    native.records.append(native.applied())
    latest = native.applied()
    latest["payload"]["thread_settings"].pop("service_tier")
    native.records.append(latest)
    if later_context:
        native.records.append(native.context())
    native.save()
    authority = ResumeSettings.resolve({"thread": native.thread()}, THREAD)
    assert authority.source_kind == "settings_applied"
    assert authority.source_ordinal == 14
    assert not authority.tier_wire_present
    assert "service_tier" in authority.known_fields
    assert authority.settings["service_tier"] is None
    assert "serviceTier" not in authority.params()
    assert authority.canonical_settings()["serviceTier"] is None


def test_newest_missing_tier_cold_acquisition_preserves_known_none(tmp_path):
    async def run():
        async with network(silent=False) as (_, caller, _, facade, _):
            native = initial_native(tmp_path, tier="priority")
            latest = native.applied()
            latest["payload"]["thread_settings"].pop("service_tier")
            native.records.append(latest)
            native.save()
            native.native_defaults["service_tier"] = None
            facade.sdk = facade.operations.sdk = native.sdk
            await facade.start_turn(
                CodexStartTurnRequest(thread_id=THREAD, content="once")
            )
            resume = [p for m, p in native.calls if m == "thread/resume"]
            assert len(resume) == 1 and "serviceTier" not in resume[0]
            assert (
                caller.get_state(THREAD)["latestThreadSettings"]["serviceTier"] is None
            )
            start = [p for m, p in native.calls if m == "turn/start"]
            assert len(start) == 1 and "serviceTier" not in start[0]
            assert caller.is_owner(THREAD)

    asyncio.run(run())


@pytest.mark.parametrize("config_tier", ["default", "priority"])
def test_known_none_refuses_changed_native_config_tier_before_claim(
    tmp_path, config_tier
):
    async def run():
        async with network(silent=False) as (_, caller, _, facade, _):
            native = initial_native(tmp_path)
            latest = native.applied()
            latest["payload"]["thread_settings"].pop("service_tier")
            native.records.append(latest)
            native.save()
            native.native_defaults["service_tier"] = config_tier
            facade.sdk = facade.operations.sdk = native.sdk
            with pytest.raises(
                IpcError, match="codex_resume_effective_settings_mismatch"
            ):
                await facade.start_turn(
                    CodexStartTurnRequest(thread_id=THREAD, content="never")
                )
            resume = [p for m, p in native.calls if m == "thread/resume"]
            assert len(resume) == 1 and "serviceTier" not in resume[0]
            assert not [p for m, p in native.calls if m == "turn/start"]
            assert not caller.is_owner(THREAD) and not facade.acquiring

    asyncio.run(run())


@pytest.mark.parametrize("tier,accepted", [(None, True), ("priority", False)])
def test_known_tier_compares_native_omission_as_null(tmp_path, tier, accepted):
    async def run():
        async with network(silent=False) as (_, caller, _, facade, _):
            native = RolloutNative(
                tmp_path,
                settings={"service_tier": tier},
                native_defaults={"service_tier": None if tier is None else "flex"},
            )
            omit_post_tier(native)
            facade.sdk = facade.operations.sdk = native.sdk
            if accepted:
                await facade.start_turn(
                    CodexStartTurnRequest(thread_id=THREAD, content="once")
                )
            else:
                with pytest.raises(
                    IpcError, match="codex_resume_effective_settings_mismatch"
                ):
                    await facade.start_turn(
                        CodexStartTurnRequest(thread_id=THREAD, content="never")
                    )
            resume = [p for m, p in native.calls if m == "thread/resume"]
            assert len(resume) == 1
            assert ("serviceTier" in resume[0]) == (tier is not None)
            if tier is not None:
                assert resume[0]["serviceTier"] == tier
            assert len([p for m, p in native.calls if m == "turn/start"]) == int(
                accepted
            )
            assert caller.is_owner(THREAD) == accepted

    asyncio.run(run())


def test_explicit_caller_null_survives_inherited_string_tier_on_real_start(tmp_path):
    async def run():
        async with network(silent=False) as (_, caller, _, facade, _):
            native = RolloutNative(tmp_path, settings={"service_tier": "priority"})
            facade.sdk = facade.operations.sdk = native.sdk
            await facade._acquire(THREAD)
            assert caller.is_owner(THREAD)
            await facade._route(
                THREAD,
                "start-turn",
                {
                    "turnStart": {
                        "request": {
                            "threadId": THREAD,
                            "input": [{"type": "text", "text": "explicit clear"}],
                            "serviceTier": None,
                        },
                        "context": {"inheritThreadSettings": True},
                    }
                },
            )
            resume = [p for m, p in native.calls if m == "thread/resume"]
            start = [p for m, p in native.calls if m == "turn/start"]
            assert len(resume) == len(start) == 1
            assert resume[0]["serviceTier"] == "priority"
            assert "serviceTier" in start[0] and start[0]["serviceTier"] is None

    asyncio.run(run())


def test_native_response_tier_cannot_override_omitted_snapshot(tmp_path):
    async def run():
        async with network(silent=False) as (_, caller, _, facade, _):
            native = initial_native(tmp_path, tier=None)
            omit_post_tier(native, response_tier="priority", contradict_response=True)
            facade.sdk = facade.operations.sdk = native.sdk
            with pytest.raises(
                IpcError, match="codex_resume_effective_settings_mismatch"
            ):
                await facade.start_turn(
                    CodexStartTurnRequest(thread_id=THREAD, content="never")
                )
            assert len([p for m, p in native.calls if m == "thread/resume"]) == 1
            assert not [p for m, p in native.calls if m == "turn/start"]
            assert not caller.is_owner(THREAD)

    asyncio.run(run())


@pytest.mark.parametrize("field", sorted(CONTEXT_KNOWN_FIELDS))
def test_newest_snapshot_missing_other_required_field_never_falls_back(tmp_path, field):
    native = initial_native(tmp_path)
    native.records.append(native.applied())
    latest = native.applied()
    latest["payload"]["thread_settings"].pop("service_tier")
    latest["payload"]["thread_settings"].pop(field)
    native.records.append(latest)
    native.save()
    with pytest.raises(IpcError, match="codex_resume_settings_unavailable"):
        ResumeSettings.resolve({"thread": native.thread()}, THREAD)


@pytest.mark.parametrize("field", sorted(CONTEXT_KNOWN_FIELDS))
def test_post_snapshot_missing_other_required_field_never_claims(tmp_path, field):
    async def run():
        async with network(silent=False) as (_, caller, _, facade, _):
            native = initial_native(tmp_path, tier=None)
            original = native.request

            async def request(method, params, **kwargs):
                result = await original(method, params, **kwargs)
                if method == "thread/resume":
                    settings = native.records[-1]["payload"]["thread_settings"]
                    settings.pop("service_tier", None)
                    settings.pop(field)
                    native.save()
                return result

            native.request = request
            facade.sdk = facade.operations.sdk = native.sdk
            with pytest.raises(
                IpcError, match="codex_resume_effective_settings_unavailable"
            ):
                await facade.start_turn(
                    CodexStartTurnRequest(thread_id=THREAD, content="never")
                )
            assert len([p for m, p in native.calls if m == "thread/resume"]) == 1
            assert not [p for m, p in native.calls if m == "turn/start"]
            assert not caller.is_owner(THREAD)

    asyncio.run(run())


def test_explicit_disabled_initial_profile_uses_native_absent_filesystem_shape(
    tmp_path,
):
    async def run():
        async with network(silent=False) as (_, caller, _, facade, _):
            native = initial_native(tmp_path)
            context = native.records[5]["payload"]
            context["permission_profile"] = {"type": "disabled"}
            context["sandbox_policy"] = {"type": "danger-full-access"}
            context.pop("file_system_sandbox_policy")
            native.save()
            facade.sdk = facade.operations.sdk = native.sdk
            await facade.start_turn(
                CodexStartTurnRequest(thread_id=THREAD, content="once")
            )
            resume = next(p for m, p in native.calls if m == "thread/resume")
            assert resume["sandbox"] == "danger-full-access"
            assert resume["approvalPolicy"] == "on-request"
            assert "sandbox_workspace_write" not in resume["config"]
            start = next(p for m, p in native.calls if m == "turn/start")
            assert start["sandboxPolicy"] == {"type": "dangerFullAccess"}
            assert caller.is_owner(THREAD)

    asyncio.run(run())


@pytest.mark.parametrize(
    "filesystem",
    [None, {"kind": "unrestricted"}, {"kind": "restricted", "entries": []}],
)
def test_disabled_context_rejects_invented_or_conflicting_filesystem_shape(
    tmp_path, filesystem
):
    native = initial_native(tmp_path)
    context = native.records[5]["payload"]
    context["permission_profile"] = {"type": "disabled"}
    context["sandbox_policy"] = {"type": "danger-full-access"}
    context["file_system_sandbox_policy"] = filesystem
    native.save()
    with pytest.raises(IpcError, match="codex_resume_settings_unavailable"):
        ResumeSettings.resolve({"thread": native.thread()}, THREAD)


@pytest.mark.parametrize(
    "field",
    [
        "workspace_roots",
        "permission_profile",
        "file_system_sandbox_policy",
        "sandbox_policy",
        "model",
        "approval_policy",
        "approvals_reviewer",
        "personality",
        "collaboration_mode",
        "disabled_plugin_ids",
        "effort",
        "summary",
        "mode-instructions",
        "mode-effort",
    ],
)
@pytest.mark.parametrize("after_settings", [False, True])
def test_newest_incomplete_context_never_borrows_older_authority(
    tmp_path, field, after_settings
):
    native = initial_native(tmp_path)
    context = deepcopy(native.records[5])
    if field.startswith("mode-"):
        context["payload"]["collaboration_mode"]["settings"].pop(
            "developer_instructions"
            if field == "mode-instructions"
            else "reasoning_effort"
        )
    else:
        context["payload"].pop(field)
    if after_settings:
        native.records.append(native.applied())
    native.records.append(context)
    native.save()
    with pytest.raises(IpcError, match="codex_resume_settings_unavailable"):
        ResumeSettings.resolve({"thread": native.thread()}, THREAD)


@pytest.mark.parametrize(
    "fault",
    [
        "meta-missing",
        "meta-conflict",
        "read-missing",
        "read-conflict",
        "root",
        "fs",
        "legacy",
        "rule",
        "plugins",
    ],
)
def test_initial_context_requires_matching_provider_lineage_and_full_policy(
    tmp_path, fault
):
    native = initial_native(tmp_path)
    context = native.records[5]["payload"]
    thread = native.thread()
    if fault == "meta-missing":
        native.records[0]["payload"].pop("model_provider")
    elif fault == "meta-conflict":
        native.records[0]["payload"]["model_provider"] = "other"
    elif fault == "read-missing":
        thread.pop("modelProvider")
    elif fault == "read-conflict":
        thread["modelProvider"] = "other"
    elif fault == "root":
        context["root_turn_id"] = "other"
    elif fault == "fs":
        context["file_system_sandbox_policy"]["entries"].pop(0)
    elif fault == "legacy":
        context["sandbox_policy"]["network_access"] = True
    elif fault == "rule":
        context["permission_profile"]["file_system"]["entries"].append(
            {"path": {"type": "path", "path": native.cwd + "/.git"}, "access": "write"}
        )
    else:
        context["disabled_plugin_ids"] = ["unknown"]
    native.save()
    with pytest.raises(IpcError, match="codex_resume_settings_unavailable"):
        ResumeSettings.resolve({"thread": thread}, THREAD)


@pytest.mark.parametrize("later_context", [False, True])
def test_later_settings_only_authority_keeps_tier_and_own_provider(
    tmp_path, later_context
):
    native = initial_native(tmp_path)
    native.settings.update(
        service_tier=None, model_provider_id="new-provider", approval_policy="never"
    )
    native.records.append(native.applied())
    if later_context:
        native.records.append(native.context())
    native.save()
    authority = ResumeSettings.resolve({"thread": native.thread()}, THREAD)
    assert authority.source_kind == "settings_applied"
    assert "service_tier" in authority.known_fields
    assert "serviceTier" not in authority.params()
    assert authority.params()["modelProvider"] == "new-provider"
    assert authority.params()["approvalPolicy"] == "never"


def test_complete_settings_after_sparse_older_context_is_still_authority(tmp_path):
    native = initial_native(tmp_path)
    native.records[5]["payload"] = {"turn_id": "old", "cwd": native.cwd}
    native.records.append(native.applied())
    native.save()
    authority = ResumeSettings.resolve({"thread": native.thread()}, THREAD)
    assert authority.params()["serviceTier"] == "default"
    assert authority.params()["sandbox"] == "workspace-write"


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "gpt-6-sol"),
        ("effort", "high"),
        ("summary", "concise"),
        ("personality", "friendly"),
        ("approvals_reviewer", "auto_review"),
        ("mode", "plan"),
        ("instructions", "custom"),
    ],
)
def test_later_complete_context_cannot_replace_known_settings(tmp_path, field, value):
    native = initial_native(tmp_path)
    native.records.append(native.applied())
    context = native.context()
    payload = context["payload"]
    mode = payload["collaboration_mode"]
    if field == "mode":
        mode["mode"] = value
    elif field == "instructions":
        mode["settings"]["developer_instructions"] = value
    else:
        payload[field] = value
        if field in {"model", "effort"}:
            mode["settings"]["model" if field == "model" else "reasoning_effort"] = (
                value
            )
    native.records.append(context)
    native.save()
    with pytest.raises(IpcError, match="codex_resume_settings_unavailable"):
        ResumeSettings.resolve({"thread": native.thread()}, THREAD)


@pytest.mark.parametrize(
    "fault",
    ["partial-settings", "null-settings", "custom-settings", "conflicting-context"],
)
def test_newest_unsupported_or_conflicting_evidence_blocks_older_fallback(
    tmp_path, fault
):
    native = initial_native(tmp_path)
    native.records.append(native.applied())
    if fault == "conflicting-context":
        context = native.context()
        context["payload"]["approval_policy"] = "never"
        native.records.append(context)
    else:
        record = native.applied()
        if fault == "null-settings":
            record["payload"]["thread_settings"] = None
        elif fault == "partial-settings":
            record["payload"]["thread_settings"].pop("approval_policy")
        else:
            record["payload"]["thread_settings"]["disabled_plugin_ids"] = ["other"]
        native.records.append(record)
    native.save()
    with pytest.raises(IpcError, match="codex_resume_settings_unavailable"):
        ResumeSettings.resolve({"thread": native.thread()}, THREAD)


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "other"),
        ("model_provider_id", "other"),
        ("reasoning_effort", "high"),
        ("reasoning_summary", "concise"),
        ("personality", "friendly"),
        ("approval_policy", "never"),
        ("approvals_reviewer", "auto_review"),
        ("permission_profile", {"type": "disabled"}),
        ("cwd", "/other"),
        ("runtime_workspace_roots", ["/other"]),
        ("disabled_plugin_ids", ["other"]),
        ("mode", "plan"),
        ("instructions", "unrelated custom native instructions"),
    ],
)
def test_initial_resume_validates_every_known_field_before_claim_or_turn(
    tmp_path, field, value
):
    async def run():
        async with network(silent=False) as (_, caller, _, facade, _):
            native = initial_native(tmp_path)
            original = native.request

            async def request(method, params, **kwargs):
                result = await original(method, params, **kwargs)
                if method == "thread/resume":
                    observed = native.records[-1]["payload"]["thread_settings"]
                    if field == "mode":
                        observed["collaboration_mode"]["mode"] = value
                    elif field == "instructions":
                        observed["collaboration_mode"]["settings"][
                            "developer_instructions"
                        ] = value
                    else:
                        observed[field] = value
                    native.save()
                return result

            native.request = request
            facade.sdk = facade.operations.sdk = native.sdk
            with pytest.raises(
                IpcError, match="codex_resume_effective_settings_"
            ) as caught:
                await facade.start_turn(
                    CodexStartTurnRequest(thread_id=THREAD, content="never")
                )
            assert not caught.value.retryable
            assert "may have applied" in str(caught.value)
            assert len([p for m, p in native.calls if m == "thread/resume"]) == 1
            assert not [p for m, p in native.calls if m == "turn/start"]
            assert not caller.is_owner(THREAD) and not facade.acquiring

    asyncio.run(run())


@pytest.mark.parametrize("fault", ["missing-tier", "invalid-tier", "response-tier"])
def test_unknown_tier_requires_valid_fresh_snapshot_and_matching_response(
    tmp_path, fault
):
    async def run():
        async with network(silent=False) as (_, caller, _, facade, _):
            native = initial_native(tmp_path)
            original = native.request

            async def request(method, params, **kwargs):
                result = await original(method, params, **kwargs)
                if method == "thread/resume":
                    observed = native.records[-1]["payload"]["thread_settings"]
                    if fault == "missing-tier":
                        observed.pop("service_tier")
                    elif fault == "invalid-tier":
                        observed["service_tier"] = {"unknown": True}
                    else:
                        result.root["serviceTier"] = "different"
                    native.save()
                return result

            native.request = request
            facade.sdk = facade.operations.sdk = native.sdk
            with pytest.raises(IpcError, match="codex_resume_effective_settings_"):
                await facade.start_turn(
                    CodexStartTurnRequest(thread_id=THREAD, content="never")
                )
            assert len([p for m, p in native.calls if m == "thread/resume"]) == 1
            assert not [p for m, p in native.calls if m == "turn/start"]
            assert not caller.is_owner(THREAD)

    asyncio.run(run())


@pytest.mark.parametrize(
    "fault", ["missing", "prefix-changed", "replaced", "interleaved"]
)
def test_initial_post_snapshot_must_be_immediate_append_on_same_source(tmp_path, fault):
    async def run():
        async with network(silent=False) as (_, caller, _, facade, _):
            native = initial_native(tmp_path)
            if fault == "missing":
                native.post_mode = "missing"
            original = native.request

            async def request(method, params, **kwargs):
                result = await original(method, params, **kwargs)
                if method == "thread/resume":
                    if fault == "prefix-changed":
                        native.records[1]["payload"]["type"] = "different"
                    elif fault == "replaced":
                        replacement = native.path.with_suffix(".replacement")
                        replacement.write_bytes(native.path.read_bytes())
                        replacement.replace(native.path)
                    elif fault == "interleaved":
                        native.records.append(
                            {"type": "event_msg", "payload": {"type": "task_started"}}
                        )
                    if fault in {"prefix-changed", "interleaved"}:
                        native.save()
                return result

            native.request = request
            facade.sdk = facade.operations.sdk = native.sdk
            with pytest.raises(
                IpcError, match="codex_resume_effective_settings_unavailable"
            ):
                await facade.start_turn(
                    CodexStartTurnRequest(thread_id=THREAD, content="never")
                )
            assert len([p for m, p in native.calls if m == "thread/resume"]) == 1
            assert not [p for m, p in native.calls if m == "turn/start"]
            assert not caller.is_owner(THREAD)

    asyncio.run(run())
