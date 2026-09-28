"""Normalize observed native settings without supplying application defaults."""

from copy import deepcopy

SETTINGS_FIELDS = {
    "model",
    "modelProvider",
    "effort",
    "collaborationMode",
    "approvalPolicy",
    "approvalsReviewer",
    "sandboxPolicy",
    "activePermissionProfile",
    "runtimeWorkspaceRoots",
    "permissions",
    "cwd",
    "serviceTier",
}


def observed_settings(value):
    values = {k: deepcopy(v) for k, v in value.items() if k in SETTINGS_FIELDS}
    for source, target in (("reasoningEffort", "effort"), ("sandbox", "sandboxPolicy")):
        if source in value and target not in values:
            values[target] = deepcopy(value[source])
    mode = values.get("collaborationMode")
    if isinstance(mode, dict):
        nested = mode.get("settings") or {}
        for source, target in (("model", "model"), ("reasoning_effort", "effort")):
            if source in nested and target not in values:
                values[target] = deepcopy(nested[source])
    return values


def merge_settings(state, settings):
    settings = {**deepcopy(settings), **observed_settings(settings)}
    if not settings:
        return
    state["latestThreadSettings"] = {
        **(state.get("latestThreadSettings") or {}),
        **deepcopy(settings),
    }
    for key, target in (
        ("model", "latestModel"),
        ("effort", "latestReasoningEffort"),
        ("collaborationMode", "latestCollaborationMode"),
    ):
        if key in settings:
            state[target] = deepcopy(settings[key])
    mode = state.get("latestCollaborationMode")
    if isinstance(mode, dict):
        nested = mode.setdefault("settings", {})
        for key, target in (("model", "model"), ("effort", "reasoning_effort")):
            if key in settings:
                nested[target] = deepcopy(settings[key])
        state["latestThreadSettings"]["collaborationMode"] = deepcopy(mode)
    for key in ("cwd", "modelProvider"):
        if key in settings:
            state[key] = deepcopy(settings[key])
    permissions = {
        k: deepcopy(v)
        for k, v in settings.items()
        if k
        in {
            "sandboxPolicy",
            "approvalPolicy",
            "approvalsReviewer",
            "activePermissionProfile",
            "runtimeWorkspaceRoots",
        }
    }
    if permissions:
        state["currentPermissions"] = {
            **(state.get("currentPermissions") or {}),
            **permissions,
        }
