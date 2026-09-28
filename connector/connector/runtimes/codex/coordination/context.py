"""Validate native preparation before mutations; retain input rather than flattening."""

import os
from copy import deepcopy
from uuid import uuid4

from packaging.version import InvalidVersion, Version

# Current installed IDE feature gates, independently from Python SDK version.
MINIMUM = {
    "daybreak": "0.154.0a6",
    "reviewer": "0.153.0a4",
    "toolOutput": "0.151.0a4",
    "turnTrigger": "0.150.0a10",
    "serviceTierForTurn": "0.150.0a10",
    "revert": "0.148.0a13",
}
UNSUPPORTED_CONTEXT = {
    "commentAttachments",
    "mcpAppModelContextAttachments",
    "usePermissionSelection",
    "writingBlockContextPrepared",
}
CONTEXT_FIELDS = {
    "localTurnMetadata",
    "messageThreadId",
    "attachments",
    "useAppServerPermissionDefault",
    "inheritThreadSettings",
    "threadStartKind",
    "responseItems",
    "passiveContext",
} | UNSUPPORTED_CONTEXT
REQUEST_FIELDS = {
    "threadId",
    "input",
    "cwd",
    "model",
    "effort",
    "summary",
    "approvalPolicy",
    "approvalsReviewer",
    "sandboxPolicy",
    "serviceTier",
    "serviceTierForTurn",
    "outputSchema",
    "clientUserMessageId",
    "turnTrigger",
    "toolOutput",
    "personality",
    "collaborationMode",
    "additionalContext",
    "responsesapiClientMetadata",
    "permissions",
    "runtimeWorkspaceRoots",
}


def require_feature(sdk, feature):
    raw = sdk.native_runtime_info().get("version")
    try:
        version = Version(raw.replace("-alpha.", "a")) if raw else None
    except InvalidVersion:
        version = None
    if (
        version is None
        or version == Version("0.0.0")
        or version < Version(MINIMUM[feature])
    ):
        raise ValueError(
            f"unsupported native version for {feature}: {raw or 'unknown'}"
        )


def validate_context(context):
    if not isinstance(context, dict):
        raise TypeError("invalid turn context")
    for field, value in context.items():
        if value not in (None, False, [], {}) and (
            field not in CONTEXT_FIELDS or field in UNSUPPORTED_CONTEXT
        ):
            raise ValueError(f"unsupported IDE context preparation: {field}")


def prepare_start(thread_id, turn_start, state, sdk):
    request = deepcopy(turn_start.get("request", {}))
    context = deepcopy(turn_start.get("context") or {})
    validate_context(context)
    if request.get("threadId") != thread_id:
        raise ValueError("turn threadId does not match conversationId")
    unknown = set(request) - REQUEST_FIELDS
    if unknown:
        raise ValueError(
            f"unsupported native request fields: {', '.join(sorted(unknown))}"
        )
    if not isinstance(request.get("input", []), list):
        raise TypeError("native input must be a typed array")
    for item in request.get("input", []):
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise TypeError("invalid typed native input")
    request.setdefault("input", [])
    if not request.get("clientUserMessageId"):
        request["clientUserMessageId"] = str(uuid4())
    if context.get("inheritThreadSettings"):
        settings = state.get("latestThreadSettings", {})
        for key, value in settings.items():
            target = "sandboxPolicy" if key == "sandbox" else key
            # A native no-override tier is known in state, but inherited null
            # on turn/start would actively select standard routing.
            if target == "serviceTier" and value is None:
                continue
            if context.get("useAppServerPermissionDefault") and target in {
                "approvalPolicy",
                "approvalsReviewer",
                "sandboxPolicy",
                "permissions",
                "runtimeWorkspaceRoots",
            }:
                continue
            if target in REQUEST_FIELDS and target not in request:
                request[target] = deepcopy(value)
        mode = request.get("collaborationMode")
        if isinstance(mode, dict) and isinstance(mode.get("settings"), dict):
            for source, target in (("model", "model"), ("effort", "reasoning_effort")):
                if source in request:
                    mode["settings"][target] = deepcopy(request[source])
    for field in ("toolOutput", "turnTrigger", "serviceTierForTurn"):
        if request.get(field) is not None:
            require_feature(sdk, field)
    for attachment in context.get("attachments", []):
        native_input = attachment_input(attachment)
        if native_input not in request["input"]:
            request["input"].append(native_input)
    return request, context


def attachment_input(attachment):
    if not isinstance(attachment, dict):
        raise TypeError("invalid attachment")
    if isinstance(attachment.get("input"), dict) and isinstance(
        attachment["input"].get("type"), str
    ):
        return deepcopy(attachment["input"])
    path = attachment.get("path")
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError(
            "unsupported IDE attachment preparation; absolute local path or native input required"
        )
    media = attachment.get("mediaType", attachment.get("mimeType", ""))
    if isinstance(media, str) and media.startswith("image/"):
        return {"type": "localImage", "path": path}
    name = attachment.get("name") or os.path.basename(path)
    return {"type": "text", "text": f"[Attached file: {name} at {path}]"}


def queue_start(thread_id, message):
    context = deepcopy(message.get("context") or {})
    if context.get("untrustedAppMessage") is not None or any(
        item.get("untrusted") is True
        for item in context.get("mcpAppModelContextAttachments", [])
    ):
        raise ValueError("App input requires confirmation before legacy delivery")
    supported = {
        "prompt",
        "messageThreadId",
        "turnTrigger",
        "workspaceRoots",
        "collaborationMode",
        "input",
        "addedFiles",
        "fileAttachments",
        "ideContext",
        "imageAttachments",
    }
    for key, value in context.items():
        if value not in (None, False, [], {}) and key not in supported:
            raise ValueError(f"unsupported queued context preparation: {key}")
    for key in ("addedFiles", "ideContext"):
        if context.get(key):
            raise ValueError(
                f"unsupported queued editor preparation: {key}; native input required"
            )
    request = {
        "threadId": thread_id,
        "input": deepcopy(
            context.get(
                "input",
                [
                    {
                        "type": "text",
                        "text": context.get("prompt", message.get("text", "")),
                    }
                ],
            )
        ),
        "clientUserMessageId": message["id"],
    }
    for attachment in [
        *context.get("fileAttachments", []),
        *context.get("imageAttachments", []),
    ]:
        native_input = attachment_input(attachment)
        if native_input not in request["input"]:
            request["input"].append(native_input)
    if message.get("cwd") is not None:
        request["cwd"] = message["cwd"]
    for key in ("turnTrigger", "collaborationMode"):
        if context.get(key) is not None:
            request[key] = context[key]
    if context.get("workspaceRoots"):
        request["runtimeWorkspaceRoots"] = context["workspaceRoots"]
    if message.get("responsesapiClientMetadata") is not None:
        request["responsesapiClientMetadata"] = deepcopy(
            message["responsesapiClientMetadata"]
        )
    return {
        "request": request,
        "context": {
            "inheritThreadSettings": True,
            "messageThreadId": context.get("messageThreadId"),
        },
    }
