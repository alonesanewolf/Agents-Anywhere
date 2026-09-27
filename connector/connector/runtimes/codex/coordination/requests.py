"""Exact request validation and opaque presentation-bound response contexts."""

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

    def clear(self, thread_id=None):
        self.tokens = {
            k: v
            for k, v in self.tokens.items()
            if thread_id is not None and v[0] != thread_id
        }

    def present(self, thread_id, source, requests):
        valid = []
        keep = {}
        for request in requests:
            binding = (
                thread_id,
                source,
                type(request["id"]).__name__,
                request["id"],
                request["method"],
            )
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
        self.clear(thread_id)
        self.tokens.update(keep)
        return valid

    def take(self, token):
        binding = self.tokens.pop(token, None)
        if binding is None:
            raise ValueError("stale response context")
        return binding
