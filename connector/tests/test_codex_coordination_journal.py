import asyncio

import pytest

from connector.core.json_kv import JsonKeyValueStore


def test_uncertain_submission_survives_restart_and_blocks_duplicate(tmp_path):
    from connector.runtimes.codex.coordination.journal import CoordinationJournal

    async def run():
        store = JsonKeyValueStore(tmp_path / "kv.json")
        journal = CoordinationJournal(store, "runtime")
        await journal.begin(
            "t", {"clientUserMessageId": "m", "input": [{"type": "image", "future": 1}]}
        )
        await journal.stage("t", "injecting")
        restarted = CoordinationJournal(JsonKeyValueStore(store.path), "runtime")
        assert restarted.operation("t")["stage"] == "injecting"
        with pytest.raises(ValueError, match="unconfirmed"):
            await restarted.begin("t", {"clientUserMessageId": "m"})
        await restarted.stage("t", "confirmed", result={"turn": {"id": "one"}})
        await restarted.begin("t", {"clientUserMessageId": "next"})
        assert restarted.operation("t")["request"]["clientUserMessageId"] == "next"

    asyncio.run(run())


def test_queue_persistence_failure_does_not_claim_accepted_state(tmp_path):
    from connector.runtimes.codex.coordination.journal import CoordinationJournal

    class BrokenStore(JsonKeyValueStore):
        def set(self, key, value):
            raise OSError("disk full")

    async def run():
        journal = CoordinationJournal(BrokenStore(tmp_path / "kv.json"), "runtime")
        with pytest.raises(OSError, match="disk full"):
            await journal.replace_queue(
                "t", [{"id": "m", "text": "hello", "future": [1]}]
            )
        assert journal.queue("t") == []

    asyncio.run(run())
