"""Owner operations through official SDK; no cross-owner fallback or retries."""

import asyncio
from collections import defaultdict
from contextvars import ContextVar
from copy import deepcopy

from openai_codex.errors import (
    InvalidParamsError,
    InvalidRequestError,
    MethodNotFoundError,
)

from connector.runtimes.codex.domain.activity import (
    activity,
    is_running,
    observe_activity,
)

from .context import prepare_start, require_feature
from .history import hydrate
from .projection import active_turn, canonical_turn
from .reducer import reduce_event
from .requests import REQUEST_ROUTES, reply
from .settings import merge_settings, observed_settings
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
        self.goal_epochs = defaultdict(int)
        self.goal_support = {}
        self.settings_epochs = defaultdict(lambda: defaultdict(int))

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
        if method == "thread/goal/get":
            # Passive hydration owns this read: timeout/disposal must propagate
            # cancellation and await the child's cooperative cleanup. Mutations
            # remain shielded because cancellation cannot resolve their outcome.
            result = await task
        else:
            result = await asyncio.shield(task)
        self.state(thread_id)
        return result

    def settings_observed(self, thread_id, values):
        for key in observed_settings(values):
            self.settings_epochs[thread_id][key] += 1

    def merge_confirmed_settings(self, thread_id, state, values, before):
        values = observed_settings(values)
        current = self.settings_epochs[thread_id]
        # Collaboration mode also embeds model/effort, so keep the newer group
        # intact if any of its observations changed while awaiting the ACK.
        if any(
            current[key] != before.get(key, 0)
            for key in ("model", "effort", "collaborationMode")
        ):
            values.pop("collaborationMode", None)
        merge_settings(
            state, {k: v for k, v in values.items() if current[k] == before.get(k, 0)}
        )

    async def observe_goal(self, thread_id, goal):
        """Commit once, then read current authority after transport publication yields."""
        from connector.runtimes.codex.turns.goals import validate_goal

        goal = validate_goal(goal, thread_id)
        state = self.state(thread_id)
        self.goal_support[thread_id] = True
        self.goal_epochs[thread_id] += 1
        state = reduce_event(
            state,
            {
                "method": "thread/goal/cleared"
                if goal is None
                else "thread/goal/updated",
                "params": {"threadId": thread_id, "goal": goal},
            },
        )
        await self.peer.publish_state(thread_id, state)
        return deepcopy(self.state(thread_id).get("threadGoal"))

    async def goal_request(self, thread_id, method, params):
        """Called within the owner's existing locks; never reacquire facade locks."""
        from connector.runtimes.codex.turns.goals import validate_goal

        epoch = self.goal_epochs[thread_id]
        result = await self.call(thread_id, method, params)
        if method == "thread/goal/clear":
            if result.get("cleared") is not True:
                return result
            goal = None
        else:
            if "goal" not in result:
                raise ValueError("Native goal was not observed")
            goal = validate_goal(result["goal"], thread_id)
            if method == "thread/goal/set" and goal is None:
                raise ValueError("Native setter returned no goal")
        if self.goal_epochs[thread_id] == epoch:
            await self.observe_goal(thread_id, goal)
        # A notification may arrive during publish_state's broadcast await. The
        # returned observation is read only after that boundary; native ACK data
        # must never repaint the pre-publication goal downstream.
        return {**result, "goal": deepcopy(self.state(thread_id).get("threadGoal"))}

    async def handle(self, method, params):
        thread_id = params["conversationId"]
        token = self.epoch.set(self.sdk.native_generation)
        try:
            self.state(thread_id)
            suffix = method.removeprefix("thread-follower-")
            if suffix in REQUEST_ROUTES:
                return await reply(self, thread_id, suffix, params)
            if suffix == "load-complete-history":
                # Edit holds this lock across revert, hydration and restart. Internal
                # edit hydration uses hydrate directly to avoid recursive acquisition.
                async with self.mutation_locks[thread_id]:
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

    async def mutation(self, thread_id, stage, method, params, *, activity_epoch=None):
        await self.journal.stage(thread_id, stage)
        try:
            if activity_epoch is not None:
                state = self.state(thread_id)
                activity_epoch["sequence"] = activity(state, enumerate_turns(state))[
                    "sequence"
                ]
            return await self.call(thread_id, method, params)
        except (InvalidParamsError, InvalidRequestError, MethodNotFoundError):
            record = self.journal.operation(thread_id)
            # Only protocol-level no-effect errors are safe to retry. Previously
            # successful phases remain durable and must not be repeated.
            partial = record.get("injectionConfirmed") or record.get("historyChanged")
            await self.journal.stage(
                thread_id, "rejected" if partial else "failed", rejectedMethod=method
            )
            raise
        except BaseException:
            await self.journal.stage(thread_id, "unknown", uncertainMethod=method)
            raise

    async def reconcile(self, thread_id):
        record = self.journal.operation(thread_id)
        if not record or record["stage"] in ("confirmed", "failed"):
            return False
        if (
            record["stage"] not in ("starting", "unknown")
            or record.get("uncertainMethod", "turn/start") != "turn/start"
        ):
            return False
        message_id = record["request"].get("clientUserMessageId")
        if not message_id:
            return False
        for turn in enumerate_turns(self.state(thread_id)):
            if not turn.get("turnId"):
                continue
            if turn.get("params", {}).get("clientUserMessageId") == message_id or any(
                item.get("type") == "userMessage" and item.get("clientId") == message_id
                for item in turn.get("items", [])
            ):
                await self.journal.stage(
                    thread_id,
                    "confirmed",
                    result={"turn": {"id": turn["turnId"]}},
                    reconciled=True,
                )
                return True
        return False

    async def start(self, thread_id, turn_start, *, already_begun=False):
        state = self.state(thread_id)
        record = self.journal.operation(thread_id) or {}
        recovering = record.get("stage") == "rejected"
        if recovering:
            turn_start = deepcopy(turn_start)
            previous = record["request"]
            turn_start.setdefault("request", {}).setdefault(
                "clientUserMessageId", previous["clientUserMessageId"]
            )
        request, context = prepare_start(thread_id, turn_start, state, self.sdk)
        if recovering and (
            record["request"].get("generation") != self.sdk.native_generation
            or context != record["request"].get("context")
            or any(
                request.get(key) != record["request"].get(key)
                for key in (
                    "threadId",
                    "input",
                    "clientUserMessageId",
                    "toolOutput",
                    "additionalContext",
                )
            )
        ):
            raise ValueError(
                "recovery requires the same input, context and native generation"
            )
        if (
            is_running(state, enumerate_turns(state))
            and request.get("toolOutput") is None
        ):
            raise ValueError("conversation has an active turn")
        passive = context.get("passiveContext") or {}
        items = deepcopy(context.get("responseItems") or [])
        if passive.get("key") != self.journal.passive_key(thread_id):
            items.extend(deepcopy(passive.get("items") or []))
        if items and is_running(state, enumerate_turns(state)):
            raise ValueError("response item injection requires an idle thread")
        prepared = {
            **request,
            "context": context,
            "generation": self.sdk.native_generation,
        }
        if already_begun or recovering:
            await self.journal.stage(
                thread_id, "prepared", request=prepared, restart=deepcopy(turn_start)
            )
        else:
            await self.journal.begin(thread_id, prepared)
        if items and not (recovering and record.get("injectionConfirmed")):
            if is_running(
                self.state(thread_id), enumerate_turns(self.state(thread_id))
            ):
                raise ValueError("response item injection requires an idle thread")
            await self.mutation(
                thread_id,
                "injecting",
                "thread/inject_items",
                {"threadId": thread_id, "items": items},
            )
            await self.journal.stage(thread_id, "injected", injectionConfirmed=True)
            if passive.get("items"):
                await self.journal.set_passive_key(thread_id, passive.get("key"))
        activity_before = {}
        settings_before = dict(self.settings_epochs[thread_id])
        result = await self.mutation(
            thread_id, "starting", "turn/start", request, activity_epoch=activity_before
        )
        if not isinstance(result.get("turn"), dict) or not result["turn"].get("id"):
            await self.journal.stage(thread_id, "unknown", uncertainMethod="turn/start")
            raise ValueError("native start returned no confirmed turn")
        await self.journal.stage(thread_id, "confirmed", result=result)
        state = self.state(thread_id)
        self.merge_confirmed_settings(thread_id, state, request, settings_before)
        turn = canonical_turn(result["turn"], thread_id)
        turn["params"] = {
            **request,
            "attachments": deepcopy(context.get("attachments", [])),
        }
        turn["turnStartContext"] = deepcopy(context)
        if "localTurnMetadata" in context:
            turn["localMetadata"] = context["localTurnMetadata"]
        turns = enumerate_turns(state)
        old = next((t for t in turns if t.get("turnId") == turn["turnId"]), None)
        if old is None:
            turns.append(turn)
        else:
            # Notifications observed while awaiting the response or fsync are newer.
            # Fill missing response fields, but only overwrite preparation metadata.
            for key, value in turn.items():
                old.setdefault(key, value)
            for key in ("params", "turnStartContext", "localMetadata"):
                if key in turn:
                    old[key] = turn[key]
        if (
            activity(state, turns)["sequence"] == activity_before["sequence"]
            and turn.get("status") == "inProgress"
        ):
            observe_activity(state, turns=turns, started=turn["turnId"])
        state["turns"] = turns
        await self.peer.publish_state(thread_id, state)
        return result

    async def steer(self, thread_id, params):
        if params.get("serviceTier") is not None:
            raise ValueError(
                "unsupported active steer serviceTier override; update thread settings first"
            )
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
        from .steering import confirmed_steer, validate_attachments

        validate_attachments(params)
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
        activity_before = {}
        result = await self.mutation(
            thread_id, "steering", "turn/steer", request, activity_epoch=activity_before
        )
        try:
            confirmed_steer(result)
        except ValueError:
            await self.journal.stage(thread_id, "unknown", uncertainMethod="turn/steer")
            raise
        await self.journal.stage(thread_id, "confirmed", result=result)
        state = self.state(thread_id)
        turns = enumerate_turns(state)
        if activity(state, turns)["sequence"] == activity_before["sequence"]:
            observe_activity(state, turns=turns, started=result["turnId"])
            state["aaAcknowledgedTurn"] = {
                "turnId": result["turnId"],
                "status": "inProgress",
            }
            await self.peer.publish_state(thread_id, state)
        return result
