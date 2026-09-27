import pytest
from test_codex_coordination_operations import async_test

from connector.runtimes.codex.coordination.requests import ResponseContexts


def test_consumed_context_cannot_be_reissued_while_same_request_is_outstanding():
    contexts = ResponseContexts()
    request = {
        "id": 7,
        "method": "item/tool/requestUserInput",
        "params": {"threadId": "t"},
    }
    shown = contexts.present("t", ("owner", 1), [request])
    token = shown[0]["responseContext"]
    contexts.take(token)
    assert contexts.present("t", ("owner", 1), [request]) == []
    contexts.present("t", ("owner", 1), [])
    fresh = contexts.present("t", ("owner", 1), [request])
    assert fresh[0]["responseContext"] != token


def response_adapter(tmp_path, method, **request_params):
    from types import SimpleNamespace

    from test_codex_coordination_operations import Native, Owner

    from connector.core.json_kv import JsonKeyValueStore
    from connector.runtimes.codex.coordination.client import CoordinatedCodexClient

    peer, native = Owner(), Native()
    peer.get_owner = lambda thread: SimpleNamespace(client_id="owner")
    peer.get_revision = lambda thread: peer.revision
    peer.is_follower = lambda thread: not peer.owned
    peer.state["requests"] = [
        {"id": 7, "method": method, "params": {"threadId": "t", **request_params}}
    ]
    adapter = CoordinatedCodexClient(
        native,
        peer,
        kv_store=JsonKeyValueStore(tmp_path / "kv.json"),
        namespace="runtime",
    )
    source = adapter._snapshot("t")[-1]
    token = adapter.contexts.present("t", source, peer.state["requests"])[0][
        "responseContext"
    ]
    return adapter, native, peer, source, token


@pytest.mark.parametrize(
    "method, request_params, invalid, corrected",
    [
        ("item/commandExecution/requestApproval", {}, {}, {"decision": "decline"}),
        ("item/fileChange/requestApproval", {}, {}, {"decision": "decline"}),
        ("item/tool/requestUserInput", {}, {"answers": []}, {"answers": {}}),
        (
            "mcpServer/elicitation/request",
            {"mode": "vendor/auth"},
            {"action": "accept"},
            {"action": "decline"},
        ),
    ],
)
@async_test
async def test_predispatch_validation_keeps_same_response_context(
    tmp_path, method, request_params, invalid, corrected
):
    adapter, native, peer, source, token = response_adapter(
        tmp_path, method, **request_params
    )
    with pytest.raises((ValueError, KeyError)):
        await adapter.respond_to_request(token, invalid)
    assert native.calls == []
    shown = adapter.contexts.present("t", source, peer.state["requests"])
    assert shown[0]["responseContext"] == token
    await adapter.respond_to_request(token, corrected)
    assert len(native.calls) == 1
    assert native.calls[0][1]["result"] == corrected
    assert peer.state["requests"] == []


@pytest.mark.parametrize("remote", [False, True])
@async_test
async def test_response_context_single_dispatch_survives_unknown_result(
    tmp_path, remote
):
    import asyncio

    adapter, native, peer, source, token = response_adapter(
        tmp_path, "item/tool/requestUserInput"
    )
    entered, release = asyncio.Event(), asyncio.Event()
    dispatched = []

    async def dispatch(*args, **kwargs):
        dispatched.append((args, kwargs))
        entered.set()
        await release.wait()
        raise TimeoutError("unknown result")

    if remote:
        peer.owned = False
        peer.request_owner = dispatch
    else:
        native.respond_native_request = dispatch
    first = asyncio.create_task(adapter.respond_to_request(token, {"answers": {}}))
    await entered.wait()
    with pytest.raises(ValueError, match="stale"):
        await adapter.respond_to_request(token, {"answers": {}})
    release.set()
    with pytest.raises(TimeoutError):
        await first
    assert len(dispatched) == 1
    assert adapter.contexts.present("t", source, peer.state["requests"]) == []
    with pytest.raises(ValueError, match="stale"):
        await adapter.respond_to_request(token, {"answers": {}})
