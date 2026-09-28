"""Current native App steering producer/consumer contract.

Restoration keeps the original draft; native input alone contains file notes.
No collaboration mode is copied: the owner supplies its current native mode.
"""

from copy import deepcopy
from pathlib import PurePosixPath
from time import time
from uuid import uuid4

from connector.runtimes.codex.sdk.client import codex_turn_user_input_wire


def prepare_steer(request, state):
    message_id = request.client_message_id or str(uuid4())
    descriptors, images, files = [], [], []
    for attachment in request.attachments:
        if (
            not isinstance(attachment.path, str)
            or not PurePosixPath(attachment.path).is_absolute()
            or not isinstance(attachment.name, str)
            or not attachment.name
            or not isinstance(attachment.media_type, str)
        ):
            raise ValueError("unsupported steering attachment")
        descriptor = {
            "label": attachment.name,
            "path": attachment.path,
            "fsPath": attachment.path,
        }
        if attachment.is_image:
            descriptor["isImageAttachment"] = True
            images.append(
                {
                    "id": attachment.path,
                    "src": attachment.path,
                    "localPath": attachment.path,
                    "filename": attachment.name,
                }
            )
        else:
            files.append(deepcopy(descriptor))
        descriptors.append(descriptor)
    cwd = state.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ValueError("invalid observed steering cwd")
    roots = state.get("workspaceRoots")
    if roots is None:
        roots = [cwd] if cwd else []
    if not isinstance(roots, list) or not all(isinstance(root, str) for root in roots):
        raise ValueError("invalid observed steering workspace roots")
    return {
        "input": codex_turn_user_input_wire(request),
        "attachments": descriptors,
        "clientUserMessageId": message_id,
        "expectedTurnId": request.turn_id,
        "restoreMessage": {
            "id": message_id,
            "text": request.content,
            "createdAt": int(time() * 1000),
            "cwd": cwd,
            "context": {
                "prompt": request.content,
                "addedFiles": [],
                "fileAttachments": files,
                "ideContext": None,
                "imageAttachments": images,
                "workspaceRoots": deepcopy(roots),
            },
        },
    }


def confirmed_steer(result):
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("turnId"), str)
        or not result["turnId"].strip()
    ):
        raise ValueError(
            "Native steer acknowledgement is missing or malformed; outcome unknown"
        )
    return result


def validate_attachments(params):
    """Display descriptors cannot replace typed input on the AA owner path."""
    inputs = params.get("input")
    if not isinstance(inputs, list) or not all(
        isinstance(item, dict) for item in inputs
    ):
        raise TypeError("steer input must be typed array")
    attachments = params.get("attachments", [])
    if not isinstance(attachments, list):
        raise TypeError("unsupported steering attachments")
    for attachment in attachments:
        if not isinstance(attachment, dict) or not all(
            isinstance(attachment.get(key), str) and attachment[key]
            for key in ("label", "path", "fsPath")
        ):
            raise ValueError("unsupported steering attachment descriptor")
        path = attachment["fsPath"]
        if attachment.get("isImageAttachment"):
            present = any(
                item.get("type") == "localImage" and item.get("path") == path
                for item in inputs
            )
        else:
            present = any(
                item.get("type") == "text" and path in item.get("text", "")
                for item in inputs
            )
        if not present:
            raise ValueError("steering attachment missing from native input")
