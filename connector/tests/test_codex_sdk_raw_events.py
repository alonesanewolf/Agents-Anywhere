import asyncio
import queue
import threading
from functools import wraps
from types import SimpleNamespace

import pytest

from connector.runtimes.codex.sdk.client import CodexSdkClient
from connector.runtimes.codex.sdk.server_requests import DeferredServerRequestReader


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


@async_test
async def test_raw_reader_preserves_order_and_does_not_block_on_server_request():
    messages = queue.Queue()
    observed, routed, written = [], [], []
    release = threading.Event()
    sync = SimpleNamespace(
        _read_message=messages.get,
        _coerce_notification=lambda method, params: (method, params),
        _write_message=written.append,
        _router=SimpleNamespace(
            route_notification=routed.append,
            route_response=routed.append,
            fail_all=lambda error: None,
        ),
    )

    def respond(message):
        release.wait(2)
        return {"answers": {"q": {"answers": ["value"]}}, "extra": message["id"]}

    reader = DeferredServerRequestReader(
        sync, raw_tap=observed.append, server_request=respond
    )
    runner = asyncio.create_task(asyncio.to_thread(reader.run))
    messages.put(
        {"id": 7, "method": "item/tool/requestUserInput", "params": {"threadId": "t"}}
    )
    messages.put(
        {"method": "item/plan/delta", "params": {"delta": "hi", "future": True}}
    )
    messages.put({"id": "native-response", "result": {}})
    for _ in range(200):
        if len(routed) == 2:
            break
        await asyncio.sleep(0.001)
    assert len(routed) == 2
    assert [message["method"] for message in observed] == [
        "item/tool/requestUserInput",
        "item/plan/delta",
    ]
    assert observed[1]["params"]["future"] is True
    assert written == []
    release.set()
    for _ in range(200):
        if written:
            break
        await asyncio.sleep(0.001)
    assert written[0] == {
        "id": 7,
        "result": {"answers": {"q": {"answers": ["value"]}}, "extra": 7},
    }
    messages.put(None)
    await runner


@async_test
async def test_native_extension_retains_unknown_request_response_and_runtime_version():
    seen = []

    async def request(method, params, *, response_model):
        seen.append((method, params))
        return response_model.model_validate(
            {"future": {"nested": 1}, "turn": {"id": "physical"}}
        )

    sdk = CodexSdkClient(
        SimpleNamespace(
            _client=SimpleNamespace(
                request=request, _sync=SimpleNamespace(_runtime_version="0.155.1")
            )
        )
    )
    result = await sdk.native_request(
        "turn/start", {"threadId": "t", "futureInput": {"a": 2}}
    )
    assert result == {"future": {"nested": 1}, "turn": {"id": "physical"}}
    assert seen == [("turn/start", {"threadId": "t", "futureInput": {"a": 2}})]
    assert sdk.native_runtime_info()["version"] == "0.155.1"


@async_test
async def test_native_requests_keep_exact_ids_generation_and_reject_reuse():
    from connector.runtimes.codex.sdk.native_events import NativeEventBridge

    seen = []

    async def handler(message, generation):
        seen.append((message, generation))

    bridge = NativeEventBridge(handler, generation=4)
    bridge.start()
    requests = [
        {
            "id": value,
            "method": "item/tool/requestUserInput",
            "params": {"threadId": "t"},
        }
        for value in (7, "7")
    ]
    for request in requests:
        bridge.raw_tap(request)
    await asyncio.sleep(0)
    waiters = [
        asyncio.create_task(bridge.wait_response(request)) for request in requests
    ]
    await bridge.respond(
        7,
        {"answers": {}},
        generation=4,
        thread_id="t",
        method="item/tool/requestUserInput",
    )
    with pytest.raises(ValueError, match="stale"):
        await bridge.respond(
            7, {}, generation=3, thread_id="t", method="item/tool/requestUserInput"
        )
    await bridge.respond(
        "7",
        {"answers": {"q": {"answers": ["x"]}}},
        generation=4,
        thread_id="t",
        method="item/tool/requestUserInput",
    )
    assert await waiters[0] == {"answers": {}}
    assert await waiters[1] == {"answers": {"q": {"answers": ["x"]}}}
    assert [entry[0]["id"] for entry in seen] == [7, "7"]
    await bridge.close()
