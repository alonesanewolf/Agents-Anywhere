"""Owner operations through official SDK; no cross-owner fallback or retries."""

import asyncio
from collections import defaultdict
from contextvars import ContextVar
from copy import deepcopy

from .context import prepare_start, require_feature
from .history import hydrate
from .projection import active_turn, canonical_turn
from .requests import REQUEST_ROUTES, reply
from .state import enumerate_turns


class OwnerOperations:
    def __init__(self, sdk, peer, journal):
        self.sdk, self.peer, self.journal = sdk, peer, journal
        self.settings_locks = defaultdict(asyncio.Lock)
        self.mutation_locks = defaultdict(asyncio.Lock)
        self.queue_locks = defaultdict(asyncio.Lock)
        self.epoch = ContextVar("codex_owner_epoch", default=None)
        self.inflight = set()
        self.queue_head = ContextVar("codex_queue_head", default=None)

    def state(self, thread_id):
        if not self.peer.is_owner(thread_id):
            raise ValueError("not current conversation owner")
        expected = self.epoch.get()
        if expected is not None and expected != self.sdk.native_generation:
            raise ValueError("native connection generation changed")
        return self.peer.get_state(thread_id)

    async def call(self, thread_id, method, params):
        self.state(thread_id)
        expected_head = self.queue_head.get()
        if expected_head is not None and self.journal.queue(thread_id)[:1] != [
            expected_head
        ]:
            raise ValueError("queued head changed before native dispatch")
        task = asyncio.create_task(self.sdk.native_request(method, deepcopy(params)))
        self.inflight.add(task)
        task.add_done_callback(self.inflight.discard)
        task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        result = await asyncio.shield(task)
        self.state(thread_id)
        return result

    async def handle(self, method, params):
        thread_id = params["conversationId"]
        token = self.epoch.set(self.sdk.native_generation)
        try:
            self.state(thread_id)
            suffix = method.removeprefix("thread-follower-")
            if suffix in REQUEST_ROUTES:
                return await reply(self, thread_id, suffix, params)
            if suffix == "load-complete-history":
                return {"revision": await hydrate(self, thread_id)}
            if suffix == "set-queued-follow-ups-state":
                from .queue import accept_queue

                return await accept_queue(self, thread_id, params)
            if suffix in ("update-thread-settings", "update-daybreak"):
                from .controls import daybreak, settings

                async with self.settings_locks[thread_id]:
                    return await (
                        settings if suffix == "update-thread-settings" else daybreak
                    )(self, thread_id, params)
            async with self.settings_locks[thread_id], self.mutation_locks[thread_id]:
                self.state(thread_id)
                if suffix == "start-turn":
                    return {"result": await self.start(thread_id, params["turnStart"])}
                if suffix == "steer-turn":
                    return {"result": await self.steer(thread_id, params)}
                if suffix == "interrupt-turn":
                    from .controls import interrupt

                    return await interrupt(self, thread_id, params)
                if suffix == "compact-thread":
                    await self.call(
                        thread_id, "thread/compact/start", {"threadId": thread_id}
                    )
                    return {"ok": True}
                if suffix == "edit-last-user-turn":
                    from .controls import edit

                    return await edit(self, thread_id, params)
                raise ValueError(f"unsupported owner operation: {method}")
        finally:
            self.epoch.reset(token)

    async def mutation(self, thread_id, stage, method, params):
        await self.journal.stage(thread_id, stage)
        try:
            return await self.call(thread_id, method, params)
        except BaseException:
            await self.journal.stage(thread_id, "unknown", uncertainMethod=method)
            raise

    async def start(self, thread_id, turn_start, *, already_begun=False):
        state = self.state(thread_id)
        request, context = prepare_start(thread_id, turn_start, state, self.sdk)
        if active_turn(state) is not None and request.get("toolOutput") is None:
            raise ValueError("conversation has an active turn")
        passive = context.get("passiveContext") or {}
        items = deepcopy(context.get("responseItems") or [])
        if passive.get("key") != self.journal.passive_key(thread_id):
            items.extend(deepcopy(passive.get("items") or []))
        if items and active_turn(state) is not None:
            raise ValueError("response item injection requires an idle thread")
        if not already_begun:
            await self.journal.begin(
                thread_id,
                {
                    **request,
                    "context": context,
                    "generation": self.sdk.native_generation,
                },
            )
        if items:
            if active_turn(self.state(thread_id)) is not None:
                raise ValueError("response item injection requires an idle thread")
            await self.mutation(
                thread_id,
                "injecting",
                "thread/inject_items",
                {"threadId": thread_id, "items": items},
            )
            await self.journal.stage(thread_id, "injected")
            if passive.get("items"):
                await self.journal.set_passive_key(thread_id, passive.get("key"))
        result = await self.mutation(thread_id, "starting", "turn/start", request)
        if not isinstance(result.get("turn"), dict) or not result["turn"].get("id"):
            await self.journal.stage(thread_id, "unknown", uncertainMethod="turn/start")
            raise ValueError("native start returned no confirmed turn")
        await self.journal.stage(thread_id, "confirmed", result=result)
        state = self.state(thread_id)
        turn = canonical_turn(result["turn"], thread_id)
        turn["params"] = {
            **request,
            "attachments": deepcopy(context.get("attachments", [])),
        }
        if "localTurnMetadata" in context:
            turn["localMetadata"] = context["localTurnMetadata"]
        turns = enumerate_turns(state)
        old = next((t for t in turns if t.get("turnId") == turn["turnId"]), None)
        if old is None:
            turns.append(turn)
        else:
            # Native notifications may have already appended items before response.
            live_items = old.get("items", [])
            old.update(turn)
            if live_items:
                old["items"] = live_items
        state["turns"] = turns
        await self.peer.publish_state(thread_id, state)
        return result

    async def steer(self, thread_id, params):
        active = active_turn(self.state(thread_id))
        if active is None or active.get("turnId") is None:
            raise ValueError("no active native turn")
        if params.get("expectedTurnId") not in (None, active["turnId"]):
            raise ValueError("active turn mismatch")
        if params.get("toolOutput") is not None:
            require_feature(self.sdk, "toolOutput")
            result = await self.start(
                thread_id,
                {
                    "request": {
                        "threadId": thread_id,
                        "input": [],
                        "toolOutput": params["toolOutput"],
                        **{
                            k: deepcopy(params[k])
                            for k in ("additionalContext",)
                            if k in params
                        },
                    },
                    "context": {},
                },
            )
            return {"turnId": result["turn"]["id"]}
        if params.get("attachments"):
            raise ValueError(
                "unsupported steering attachment preparation; native input required"
            )
        if not isinstance(params.get("input"), list):
            raise TypeError("steer input must be typed array")
        request = {
            "threadId": thread_id,
            "expectedTurnId": active["turnId"],
            "input": deepcopy(params["input"]),
        }
        for key in ("clientUserMessageId", "additionalContext"):
            if key in params:
                request[key] = deepcopy(params[key])
        await self.journal.begin(
            thread_id,
            {**request, "restoreMessage": deepcopy(params.get("restoreMessage"))},
        )
        result = await self.mutation(thread_id, "steering", "turn/steer", request)
        await self.journal.stage(thread_id, "confirmed", result=result)
        return result
