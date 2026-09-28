"""Execute validated AA command grammar once, without slash-prompt fallback."""

import json
import re
from dataclasses import dataclass

from openai_codex.errors import (
    InvalidParamsError,
    InvalidRequestError,
    MethodNotFoundError,
)

from connector.runtime_protocol import RuntimeCommandResult
from connector.runtimes.codex.domain.commands import (
    ALIASES,
    OPERATIONS,
    command_facts,
    list_codex_commands,
)
from connector.runtimes.codex.turns.coordination_controls import (
    execute_coordination_control,
)
from connector.runtimes.codex.turns.goals import (
    hydrate_goal,
    parse_goal,
    publish_goal,
    validate_goal,
)


def command_input(command, raw, args):
    if not isinstance(command, str) or not re.fullmatch(
        r"/?[A-Za-z][A-Za-z0-9_-]*", command
    ):
        raise ValueError("Invalid command name")
    name = command.removeprefix("/")
    if any(not isinstance(arg, str) for arg in args):
        raise ValueError("Command arguments must be strings")
    if raw is not None:
        if not isinstance(raw, str) or len(raw) > 4096:
            raise ValueError("Command raw input must contain at most 4096 characters")
        match = re.fullmatch(r"\s*/([A-Za-z][A-Za-z0-9_-]*)(?:\s+([\s\S]*))?", raw)
        if not match or match[1] != name:
            raise ValueError("Raw command must match the requested command")
        text = match[2] or ""
    else:
        if len(args) > 1:
            raise ValueError("Provide one free-form argument or exact raw input")
        text = args[0] if args else ""
    return ALIASES.get(name, name), text


def parse_payload(name, text, thread_id):
    if name == "goal":
        return parse_goal(text)
    if name == "review":
        target, _, argument = text.strip().partition(" ")
        if target in {"", "uncommitted"} and not argument:
            return {"type": "uncommittedChanges"}
        if target in {"branch", "commit", "custom"} and argument.strip():
            return {
                "type": {
                    "branch": "baseBranch",
                    "commit": "commit",
                    "custom": "custom",
                }[target],
                {"branch": "branch", "commit": "sha", "custom": "instructions"}[
                    target
                ]: argument,
            }
        raise ValueError("Review requires uncommitted, branch, commit or custom target")
    if name == "plan":
        if text.strip() not in {"", "on", "off"}:
            raise ValueError("Plan accepts on or off")
        return "default" if text.strip() == "off" else "plan"
    if name in {"settings", "daybreak", "edit", "queue"}:
        payload = json.loads(text)
        if not isinstance(payload, dict) or not payload:
            raise ValueError("A native JSON object payload is required")
        if set(payload) & {
            "threadId",
            "conversationId",
            "hostId",
            "requestId",
            "responseContext",
        }:
            raise ValueError("Command cannot override authority identity")
        if name == "settings" and (
            not isinstance(payload.get("threadSettings"), dict)
            or not payload["threadSettings"]
            or "threadId" in payload["threadSettings"]
        ):
            raise ValueError(
                "Settings requires threadSettings without identity overrides"
            )
        if name == "daybreak" and type(payload.get("daybreakEnabled")) is not bool:
            raise ValueError("daybreakEnabled must be boolean")
        if name == "edit" and (
            not isinstance(payload.get("turnId"), str)
            or not payload["turnId"]
            or not isinstance(payload.get("message"), str)
        ):
            raise ValueError("Edit requires turnId and message")
        if name == "queue" and (
            not isinstance(payload.get("state"), dict)
            or set(payload["state"]) != {thread_id}
            or not isinstance(payload["state"][thread_id], list)
        ):
            raise ValueError("Queue state must contain only the current thread's list")
        return payload
    if text.strip():
        raise ValueError("This command takes no arguments")
    return {}


@dataclass
class CodexCommandController:
    client: object
    states: object
    ensure_started: object
    publish_history: object = None

    def facts(self, session_id, thread_id):
        return command_facts(self.client, thread_id, self.states.get(session_id))

    def catalog(self, session_id, thread_id, query=None, limit=50):
        return list_codex_commands(
            thread_id,
            self.client is not None,
            query,
            limit,
            facts=self.facts(session_id, thread_id),
        )

    async def hydrate(self, session_id, thread_id):
        if not thread_id or self.client is None:
            return
        await self.ensure_started()
        try:
            await hydrate_goal(self.client, self.states, session_id, thread_id)
        except Exception:  # noqa: BLE001 - optional read cannot claim an unobserved clear
            return

    async def execute_command(
        self, session_id, command, external_session_id=None, raw=None, args=()
    ):
        requested = command.removeprefix("/")
        try:
            name, text = command_input(command, raw, args)
            payload = parse_payload(name, text, external_session_id)
        except (ValueError, TypeError):
            return RuntimeCommandResult(
                command=requested,
                ok=False,
                code="invalid_command",
                message="Invalid command arguments; consult the command hint.",
            )
        descriptor = next(
            (c for c in self.catalog(session_id, external_session_id) if c.id == name),
            None,
        )
        if descriptor is None:
            return RuntimeCommandResult(
                command=requested,
                ok=False,
                code="unknown_command",
                message="Unknown Codex command.",
            )
        if not descriptor.enabled:
            return RuntimeCommandResult(
                command=requested,
                ok=False,
                code="command_unavailable",
                message=descriptor.disabled_reason,
            )
        if descriptor.metadata["ui"]["kind"] == "selector":
            return RuntimeCommandResult(
                command=requested,
                ok=False,
                code="command_requires_selector",
                message="Use the session selection control.",
            )
        if name == "goal" and payload[0] != "status":
            action = descriptor.metadata["goalActions"][payload[0]]
            if not action["enabled"]:
                return RuntimeCommandResult(
                    command=requested,
                    ok=False,
                    code="command_unavailable",
                    message=action["disabledReason"],
                )
        if name == "plan":
            state = self.states.get(session_id)
            settings = state.metadata.get("codexSettings", {}) if state else {}
            model = settings.get("latestModel")
            if not isinstance(model, str) or not model:
                return RuntimeCommandResult(
                    command=requested,
                    ok=False,
                    code="command_unavailable",
                    message="native_model_unknown",
                )
            previous_mode = (
                settings.get("latestThreadSettings", {}).get("collaborationMode") or {}
            )
            mode_settings = dict(previous_mode.get("settings", {}))
            mode_settings["developer_instructions"] = None
            mode_settings["model"] = model
            if "latestReasoningEffort" in settings:
                mode_settings["reasoning_effort"] = settings["latestReasoningEffort"]
            payload = {
                "threadSettings": {
                    "collaborationMode": {"mode": payload, "settings": mode_settings}
                }
            }
        await self.ensure_started()
        try:
            result, execution = await self.dispatch(
                session_id, external_session_id, name, payload
            )
            if not isinstance(result, dict):
                raise TypeError("Invalid native result")
            if result.get("ok") is False or result.get("applied") is False:
                return RuntimeCommandResult(
                    command=requested,
                    ok=False,
                    code="command_error",
                    message="Native command was not applied.",
                    result={**result, "executionState": "completed"},
                )
            return RuntimeCommandResult(
                command=requested, result={**result, "executionState": execution}
            )
        except (InvalidParamsError, InvalidRequestError, MethodNotFoundError):
            return RuntimeCommandResult(
                command=requested,
                ok=False,
                code="command_rejected",
                message="Native backend rejected this operation.",
                result={"executionState": "completed"},
            )
        except Exception:  # noqa: BLE001 - every post-dispatch ambiguity is non-retrying
            return RuntimeCommandResult(
                command=requested,
                ok=False,
                code="command_outcome_unknown",
                message="Command outcome is unknown. Refresh before taking further action.",
                result={"executionState": "unknown", "retryable": False},
            )

    async def dispatch(self, session_id, thread_id, name, payload):
        if name == "status":
            state = self.states.get(session_id)
            return {
                "status": state.status if state else "unknown",
                "externalSessionId": thread_id,
                "metadata": dict(state.metadata) if state else {},
                "capabilities": self.facts(session_id, thread_id),
            }, "completed"
        if name == "goal":
            action, fields = payload
            if action == "status":
                await hydrate_goal(self.client, self.states, session_id, thread_id)
                state = self.states.get(session_id)
                if not state or not state.metadata.get("codexPresentation"):
                    return {
                        "ok": False,
                        "reason": "native_goal_unobserved",
                    }, "completed"
                return {
                    "codexPresentation": dict(
                        state.metadata.get("codexPresentation", {})
                    )
                    if state
                    else {}
                }, "completed"
            method = "thread/goal/clear" if action == "clear" else "thread/goal/set"
            result = await self.client.native_request(
                method, {"threadId": thread_id, **fields}
            )
            if action == "clear" and result.get("cleared") is not True:
                return {**result, "ok": False}, "completed"
            if self.facts(session_id, thread_id).get("goalObservationOwned"):
                result = {**result, "goal": await self.client.project_goal(thread_id)}
            else:
                goal = validate_goal(result.get("goal"), thread_id)
                if action != "clear" and goal is None:
                    raise ValueError("Native setter returned no goal")
                await self.client.observe_goal(thread_id, goal)
                await publish_goal(self.states, session_id, thread_id, goal)
            return result, "accepted" if fields.get(
                "status"
            ) == "active" else "completed"
        if name == "review":
            result = await self.client.native_request(
                "review/start",
                {"threadId": thread_id, "target": payload, "delivery": "inline"},
            )
            if (
                not isinstance(result.get("reviewThreadId"), str)
                or not result["reviewThreadId"]
                or not isinstance(result.get("turn"), dict)
                or not result["turn"].get("id")
            ):
                raise ValueError("Review acknowledgement is missing its physical turn")
            return result, "accepted"
        operation = "update-thread-settings" if name == "plan" else OPERATIONS[name]
        facts = self.facts(session_id, thread_id)
        if name == "compact" and not facts.get("coordinated"):
            result = await self.client.compact_thread(thread_id)
            return dict(result.payload), "accepted"
        if name == "history" and facts.get("role") == "owner":
            await self.client.reconcile_thread(thread_id)
        result = await execute_coordination_control(
            self.client, thread_id, operation, payload
        )
        key = "applied" if operation == "update-thread-settings" else "ok"
        if name != "history" and type(result.get(key)) is not bool:
            raise ValueError("Native control acknowledgement is incomplete")
        if name == "history" and self.publish_history:
            if self.client.has_canonical_authority(thread_id):
                await self.client.refresh_state(thread_id, force=True)
            else:
                await self.publish_history(session_id, thread_id, result["state"])
        return result, "accepted" if name in {
            "compact",
            "edit",
            "queue",
        } else "completed"
