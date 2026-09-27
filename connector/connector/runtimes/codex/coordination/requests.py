"""Exact request validation and opaque presentation-bound response contexts."""

from collections.abc import Mapping
from copy import deepcopy
from secrets import token_urlsafe

from .reducer import exact_id

REQUEST_ROUTES = {
    "command-approval-decision": "item/commandExecution/requestApproval",
    "file-approval-decision": "item/fileChange/requestApproval",
    "permissions-request-approval-response": "item/permissions/requestApproval",
    "submit-user-input": "item/tool/requestUserInput",
    "submit-mcp-server-elicitation-response": "mcpServer/elicitation/request",
}


def validate_response(request, result):
    """Pure validation, usable before the facade commits its one-shot token."""
    if not isinstance(result, Mapping):
        raise TypeError("native response requires a mapping")
    method = request["method"]
    if (
        method
        in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval")
        and "decision" not in result
    ):
        raise ValueError("approval response requires decision")
    if method == "item/tool/requestUserInput" and not isinstance(
        result.get("answers"), dict
    ):
        raise ValueError("question response requires answers mapping")
    if method == "mcpServer/elicitation/request":
        content = request["params"]
        if result.get("action") not in ("accept", "decline", "cancel"):
            raise ValueError("invalid elicitation action")
        if result["action"] == "accept" and (
            content.get("mode") not in ("form", "url")
            or "connector" in str(content.get("_meta", {})).lower()
        ):
            raise ValueError("unsupported special MCP authentication or verification")


async def reply(operations, thread_id, suffix, params):
    method = REQUEST_ROUTES[suffix]
    state = operations.state(thread_id)
    request = next(
        (
            r
            for r in state.get("requests", [])
            if exact_id(r.get("id"), params.get("requestId"))
            and r.get("method") == method
            and r.get("params", {}).get("threadId") == thread_id
        ),
        None,
    )
    if request is None:
        raise ValueError("no exact outstanding native request")
    result = (
        {"decision": deepcopy(params["decision"])}
        if suffix in ("command-approval-decision", "file-approval-decision")
        else deepcopy(params["response"])
    )
    validate_response(request, result)
    operations.state(thread_id)
    await operations.sdk.respond_native_request(
        request["id"],
        result,
        generation=operations.sdk.native_generation,
        thread_id=thread_id,
        method=method,
    )
    current = operations.state(thread_id)
    current["requests"] = [
        r
        for r in current.get("requests", [])
        if not (exact_id(r.get("id"), request["id"]) and r.get("method") == method)
    ]
    await operations.peer.publish_state(thread_id, current)
    return {"ok": True}


class ResponseContexts:
    def __init__(self):
        self.tokens = {}
        self.consumed = set()

    def clear(self, thread_id=None):
        self.consumed = {
            binding
            for binding in self.consumed
            if thread_id is not None and binding[0] != thread_id
        }
        self.tokens = {
            k: v
            for k, v in self.tokens.items()
            if thread_id is not None and v[0] != thread_id
        }

    def present(self, thread_id, source, requests):
        valid = []
        keep = {}
        current = set()
        for request in requests:
            binding = (
                thread_id,
                source,
                type(request["id"]).__name__,
                request["id"],
                request["method"],
            )
            current.add(binding)
            if binding in self.consumed:
                continue
            token = next(
                (key for key, value in self.tokens.items() if value == binding), None
            ) or token_urlsafe(24)
            keep[token] = binding
            valid.append(
                {
                    "threadId": thread_id,
                    "requestId": request["id"],
                    "method": request["method"],
                    "responseContext": token,
                }
            )
        self.tokens = {
            key: value for key, value in self.tokens.items() if value[0] != thread_id
        }
        self.consumed = {
            binding
            for binding in self.consumed
            if binding[0] != thread_id or binding in current
        }
        self.tokens.update(keep)
        return valid

    def peek(self, token):
        binding = self.tokens.get(token)
        if binding is None:
            raise ValueError("stale response context")
        return binding

    def take(self, token):
        binding = self.tokens.pop(token, None)
        if binding is None:
            raise ValueError("stale response context")
        self.consumed.add(binding)
        return binding

    def new_request(self, thread_id, request_id, method):
        def matches(binding):
            return (
                binding[0] == thread_id
                and exact_id(binding[3], request_id)
                and binding[4] == method
            )

        self.tokens = {
            key: binding for key, binding in self.tokens.items() if not matches(binding)
        }
        self.consumed = {binding for binding in self.consumed if not matches(binding)}
