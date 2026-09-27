"""Lossless, I/O-free helpers for the IDE's canonical conversation state."""

from copy import deepcopy
from typing import Any

from .wire import IpcError


def enumerate_turns(state: dict) -> list[dict]:
    """Enumerate canonical island order and overlay transient turns by native id.

    Return independent objects; unknown turn/item fields remain intact. Missing
    entities are skipped here and treated as incomplete by history_complete.
    """
    turns = []
    history = state.get("turnHistory", {})
    if history.get("kind") == "canonical":
        data = history.get("history", {})
        entities = data.get("entitiesByKey", {})
        for island in data.get("islands", []):
            for entry in island.get("entries", []):
                turn = entities.get(entry.get("value"))
                if isinstance(turn, dict):
                    turns.append(turn)
    positions = {
        turn.get("turnId"): i for i, turn in enumerate(turns) if turn.get("turnId")
    }
    for turn in state.get("turns", []):
        key = turn.get("turnId")
        if key in positions:
            turns[positions[key]] = {**turns[positions[key]], **turn}
        else:
            if key:
                positions[key] = len(turns)
            turns.append(turn)
    return deepcopy(turns)


def history_complete(state: dict) -> bool:
    if state.get("turnsPagination", {}).get("source") == "compact":
        return False
    if any(
        turn.get("itemsPagination", {}).get("hasLoadedOldest") is False
        for turn in enumerate_turns(state)
    ):
        return False
    history = state.get("turnHistory", {})
    if history.get("kind") == "canonical":
        data = history.get("history", {})
        islands = data.get("islands", [])
        if data.get("isComplete") is not True or len(islands) != 1:
            return False
        entities = data.get("entitiesByKey", {})
        for entry in islands[0].get("entries", []):
            turn = entities.get(entry.get("value"))
            if (
                not isinstance(turn, dict)
                or turn.get("itemsPagination", {}).get("hasLoadedOldest") is False
            ):
                return False
        return all(
            turn.get("itemsPagination", {}).get("hasLoadedOldest") is not False
            for turn in enumerate_turns(state)
        )
    return (
        state.get("resumeState") == "resumed"
        and state.get("turnsPagination", {}).get("hasLoadedOldest", True) is True
    )


def apply_patches(state: dict, patches: list[dict]) -> dict:
    """Apply Immer array-path add/remove/replace as one isolated transaction.

    Invalid paths never partially mutate the caller's state. Array indices are
    integers (including insertion at length); object keys must be strings.
    """
    result = deepcopy(state)
    try:
        if not isinstance(patches, list):
            raise TypeError
        for patch in patches:
            op, path = patch["op"], patch["path"]
            if op not in {"add", "remove", "replace"} or not isinstance(path, list):
                raise ValueError
            if not path:
                if op == "remove" or not isinstance(patch["value"], dict):
                    raise ValueError
                result = deepcopy(patch["value"])
                continue
            parent: Any = result
            for segment in path[:-1]:
                _validate_segment(parent, segment)
                parent = parent[segment]
            key = path[-1]
            _validate_segment(parent, key, insert=op == "add")
            if op == "remove":
                del parent[key]
            elif isinstance(parent, list) and op == "add":
                parent.insert(key, deepcopy(patch["value"]))
            else:
                parent[key] = deepcopy(patch["value"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise IpcError("invalid-patch") from exc
    return result


def _validate_segment(parent, key, *, insert=False):
    if isinstance(parent, list):
        if type(key) is not int or key < 0 or key >= len(parent) + int(insert):
            raise ValueError
    elif isinstance(parent, dict):
        if not isinstance(key, str) or (not insert and key not in parent):
            raise ValueError
    else:
        raise TypeError


def read_context_matches(local: dict | None, remote: dict | None) -> bool:
    """Compare only trusted local account/host facts, never infer from remote."""
    if not isinstance(local, dict) or not isinstance(remote, dict):
        return False
    host = local.get("executionHostKey")
    if not isinstance(host, str) or not host or host != remote.get("executionHostKey"):
        return False
    identity, other = local.get("identity"), remote.get("identity")
    if not isinstance(identity, dict) or not isinstance(other, dict):
        return False
    kind = identity.get("kind")
    keys = {"chatgpt": ("accountId", "userId"), "execution-storage": ("authMode",)}.get(
        kind
    )
    return (
        bool(keys)
        and other.get("kind") == kind
        and all(
            isinstance(identity.get(key), str)
            and bool(identity[key])
            and identity[key] == other.get(key)
            for key in keys
        )
    )
