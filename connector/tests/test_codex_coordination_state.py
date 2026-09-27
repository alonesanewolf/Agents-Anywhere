"""Synthetic canonical fixtures independent of SDK normalization."""

from copy import deepcopy

import pytest

from connector.runtimes.codex.coordination.state import (
    apply_patches,
    enumerate_turns,
    history_complete,
    read_context_matches,
)
from connector.runtimes.codex.coordination.wire import IpcError


def canonical():
    return {
        "id": "thread",
        "unknown": {"future": True},
        "turns": [{"turnId": "b", "items": [{"id": "live"}]}],
        "turnHistory": {
            "kind": "canonical",
            "history": {
                "generation": 9,
                "isComplete": True,
                "entitiesByKey": {
                    "entity-a": {
                        "turnId": "a",
                        "items": [{"id": "native", "future": 1}],
                    },
                    "entity-b": {"turnId": "b", "items": []},
                },
                "islands": [
                    {
                        "id": "island",
                        "entries": [
                            {"key": "order-a", "value": "entity-a"},
                            {"key": "order-b", "value": "entity-b"},
                        ],
                    }
                ],
            },
        },
    }


def test_canonical_enumeration_uses_value_and_reconciles_transient_turns():
    state = canonical()
    turns = enumerate_turns(state)
    assert turns == [
        {"turnId": "a", "items": [{"id": "native", "future": 1}]},
        {"turnId": "b", "items": [{"id": "live"}]},
    ]
    turns[0]["items"].clear()
    assert enumerate_turns(state)[0]["items"][0]["id"] == "native"
    assert history_complete(state)
    state["turnHistory"]["history"]["entitiesByKey"]["entity-a"]["itemsPagination"] = {
        "hasLoadedOldest": False
    }
    assert not history_complete(state)


def test_history_islands_partial_and_legacy_completeness():
    state = canonical()
    state["turnHistory"]["history"]["islands"].append({"id": "gap", "entries": []})
    assert not history_complete(state)
    assert not history_complete({"turns": []})
    assert history_complete(
        {"resumeState": "resumed", "turnsPagination": {"hasLoadedOldest": True}}
    )
    assert not history_complete(
        {
            "resumeState": "resumed",
            "turnsPagination": {"hasLoadedOldest": True, "source": "compact"},
        }
    )


def test_immer_patch_batch_preserves_unknowns_and_input_isolation():
    state = {"items": [{"id": "a"}, {"id": "c"}], "unknown": {"future": 1}}
    patches = [
        {"op": "add", "path": ["items", 1], "value": {"id": "b"}},
        {"op": "replace", "path": ["items", 2, "id"], "value": "d"},
        {"op": "remove", "path": ["items", 0]},
        {"op": "add", "path": ["metadata"], "value": {"raw": 7}},
    ]
    result = apply_patches(state, patches)
    assert result == {
        "items": [{"id": "b"}, {"id": "d"}],
        "unknown": {"future": 1},
        "metadata": {"raw": 7},
    }
    result["metadata"]["raw"] = 8
    assert patches[-1]["value"] == {"raw": 7}
    assert state["items"] == [{"id": "a"}, {"id": "c"}]
    assert apply_patches(
        state, [{"op": "replace", "path": [], "value": {"new": []}}]
    ) == {"new": []}


@pytest.mark.parametrize(
    "bad",
    [
        {"op": "replace", "path": ["missing"], "value": 1},
        {"op": "remove", "path": ["items", 4]},
        {"op": "add", "path": ["items", -1], "value": 1},
        {"op": "add", "path": ["items", True], "value": 1},
        {"op": "replace", "path": ["items", "length"], "value": 9},
        {"op": "move", "path": ["items", 0]},
        {"op": "remove", "path": []},
    ],
)
def test_bad_patch_rolls_back_whole_batch(bad):
    state = {"items": [1], "keep": 0}
    before = deepcopy(state)
    with pytest.raises(IpcError, match="invalid-patch"):
        apply_patches(state, [{"op": "replace", "path": ["keep"], "value": 3}, bad])
    assert state == before


def test_read_context_requires_trusted_host_and_complete_identity():
    local = {
        "executionHostKey": "local:verified",
        "identity": {"kind": "chatgpt", "accountId": "account", "userId": "user"},
    }
    assert read_context_matches(local, deepcopy(local))
    assert not read_context_matches(None, local)
    assert not read_context_matches(local, {**local, "executionHostKey": "other"})
    assert not read_context_matches(
        local,
        {
            **local,
            "identity": {"kind": "chatgpt", "accountId": "account", "userId": "other"},
        },
    )
    invalid = {"executionHostKey": "local:verified", "identity": {"kind": "chatgpt"}}
    assert not read_context_matches(invalid, invalid)
    storage = {
        "executionHostKey": "local:verified",
        "identity": {"kind": "execution-storage", "authMode": "api-key"},
    }
    assert read_context_matches(storage, storage)


def test_legacy_resume_defaults_complete_unless_compact_or_partial_items():
    assert history_complete({"resumeState": "resumed", "turns": []})
    assert not history_complete(
        {
            "resumeState": "resumed",
            "turnsPagination": {"hasLoadedOldest": True, "source": "compact"},
        }
    )
    assert not history_complete(
        {
            "resumeState": "resumed",
            "turns": [{"itemsPagination": {"hasLoadedOldest": False}}],
        }
    )
    value = canonical()
    value["turnsPagination"] = {"source": "compact"}
    assert not history_complete(value)
