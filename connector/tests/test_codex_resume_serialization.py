"""Coordinated resume distinguishes supplied null from native defaults."""

import asyncio

import pytest
from codex_resume_fixture import RolloutNative


@pytest.mark.parametrize(
    "settings,expected",
    [
        ({"threadId": "t"}, {"threadId": "t"}),
        (
            {"threadId": "t", "serviceTier": None, "personality": None},
            {"threadId": "t", "serviceTier": None, "personality": None},
        ),
        (
            {"threadId": "t", "serviceTier": "priority", "personality": "pragmatic"},
            {"threadId": "t", "serviceTier": "priority", "personality": "pragmatic"},
        ),
    ],
)
def test_coordinated_resume_preserves_only_supplied_nullable_fields(
    tmp_path, settings, expected
):
    native = RolloutNative(tmp_path)

    async def request(method, params):
        assert method == "thread/resume"
        assert params == expected
        return {}

    native.sdk.native_request = request
    asyncio.run(native.sdk.native_thread_resume("t", settings=settings))


def test_default_sdk_resume_keeps_existing_omitted_nullable_fields(tmp_path):
    native = RolloutNative(tmp_path)

    async def request(method, params):
        assert method == "thread/resume"
        assert params["threadId"] == "t"
        assert "serviceTier" not in params
        assert "personality" not in params
        assert "model" not in params
        return {}

    native.sdk.native_request = request
    asyncio.run(native.sdk.native_thread_resume("t"))
