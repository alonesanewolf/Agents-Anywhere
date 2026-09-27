"""Runtime-scoped durable write-ahead records; never replay uncertain mutations."""

import asyncio
import os
from copy import deepcopy


class CoordinationJournal:
    def __init__(self, store, namespace):
        if store is None or not namespace:
            raise ValueError("runtime-scoped KV and namespace required")
        self.store = store
        self.key = f"codex.coordination:{namespace}"
        self.document = deepcopy(
            dict(
                store.get(self.key)
                or {"operations": {}, "queues": {}, "passiveKeys": {}}
            )
        )
        self.lock = asyncio.Lock()
        self.failed = False

    def operation(self, thread_id):
        return deepcopy(self.document["operations"].get(thread_id))

    def queue(self, thread_id):
        return deepcopy(self.document["queues"].get(thread_id, []))

    def passive_key(self, thread_id):
        return self.document["passiveKeys"].get(thread_id)

    async def _change(self, update):
        async with self.lock:
            if self.failed:
                raise OSError("coordination persistence requires recovery")
            document = deepcopy(self.document)
            update(document)
            try:
                task = asyncio.create_task(asyncio.to_thread(self._persist, document))
                await asyncio.shield(task)
            except BaseException:
                self.failed = True
                raise
            self.document = document

    def _persist(self, document):
        self.store.set(self.key, document)
        # Fail explicitly on filesystems/OSes that cannot guarantee durable rename.
        descriptor = os.open(self.store.path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def begin(self, thread_id, request):
        def update(document):
            existing = document["operations"].get(thread_id)
            if existing and existing["stage"] not in ("confirmed", "failed"):
                raise ValueError("unconfirmed native operation requires reconciliation")
            document["operations"][thread_id] = {
                "stage": "prepared",
                "request": deepcopy(request),
            }

        await self._change(update)

    async def stage(self, thread_id, stage, **fields):
        def update(document):
            document["operations"][thread_id].update(stage=stage, **deepcopy(fields))

        await self._change(update)

    async def replace_queue(self, thread_id, messages):
        def update(document):
            if messages:
                document["queues"][thread_id] = deepcopy(messages)
            else:
                document["queues"].pop(thread_id, None)

        await self._change(update)

    async def set_passive_key(self, thread_id, key):
        await self._change(
            lambda document: document["passiveKeys"].__setitem__(thread_id, key)
        )
