def test_native_projection_retains_unknown_fields_and_converts_units():
    from connector.runtimes.codex.coordination.projection import (
        native_to_state,
        state_to_native,
    )

    raw = {
        "id": "t",
        "createdAt": 12,
        "updatedAt": 13,
        "future": {"x": 1},
        "turns": [
            {
                "id": "physical",
                "startedAt": 12,
                "durationMs": 50,
                "status": "completed",
                "items": [
                    {
                        "id": "u",
                        "type": "userMessage",
                        "clientId": "client",
                        "content": [{"type": "text", "text": "hello", "future": 1}],
                    },
                    {"id": "a", "type": "agentMessage", "text": "ok", "future": True},
                ],
            }
        ],
    }
    state = native_to_state(raw, complete=True)
    assert state["createdAt"] == 12000
    assert state["turns"][0]["turnId"] == "physical"
    assert state["turns"][0]["params"]["clientUserMessageId"] == "client"
    restored = state_to_native(state)
    assert restored["createdAt"] == 12
    assert restored["future"] == {"x": 1}
    assert restored["turns"][0]["items"][1]["future"] is True
    assert restored["turns"][0]["durationMs"] == 50


def test_raw_plan_goal_request_and_delta_reducer_preserves_canonical_history():
    from connector.runtimes.codex.coordination.projection import (
        native_to_state,
        presentation,
        state_to_native,
    )
    from connector.runtimes.codex.coordination.reducer import reduce_event

    state = native_to_state(
        {"id": "t", "turns": [{"id": "turn", "status": "inProgress", "items": []}]},
        complete=False,
    )
    messages = [
        {
            "method": "item/started",
            "params": {
                "threadId": "t",
                "turnId": "turn",
                "item": {"id": "a", "type": "agentMessage", "text": "", "future": 2},
            },
        },
        {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "t",
                "turnId": "turn",
                "itemId": "a",
                "delta": "Hello",
            },
        },
        {
            "method": "turn/plan/updated",
            "params": {
                "threadId": "t",
                "turnId": "turn",
                "plan": [{"step": "one", "status": "inProgress"}],
            },
        },
        {
            "method": "turn/plan/updated",
            "params": {
                "threadId": "t",
                "turnId": "turn",
                "plan": [{"step": "one", "status": "completed"}],
            },
        },
        {
            "id": 7,
            "method": "item/tool/requestUserInput",
            "params": {"threadId": "t", "turnId": "turn", "questions": [], "future": 8},
        },
        {
            "method": "thread/goal/updated",
            "params": {
                "threadId": "t",
                "goal": {"objective": "test", "status": "complete", "tokensUsed": 9},
            },
        },
        {"method": "thread/goal/cleared", "params": {"threadId": "t"}},
    ]
    for message in messages:
        state = reduce_event(state, message)
    assert state["requests"][0]["id"] == 7
    assert state["requests"][0]["params"]["future"] == 8
    assert state_to_native(state)["turns"][0]["items"][0]["text"] == "Hello"
    assert len([x for x in state["turns"][0]["items"] if x["type"] == "todo-list"]) == 2
    shown = presentation(state)
    assert shown["threadGoal"] is None
    assert shown["completedThreadGoal"]["tokensUsed"] == 9
    assert shown["latestPlansByTurn"]["turn"]["id"] == "plan-progress:turn"
    assert shown["latestPlansByTurn"]["turn"]["plan"][0]["status"] == "completed"
    state = reduce_event(
        state,
        {
            "method": "serverRequest/resolved",
            "params": {"threadId": "t", "requestId": 7},
        },
    )
    assert state["requests"] == []


def test_partial_user_input_is_not_inferred_as_opening_message():
    from connector.runtimes.codex.coordination.projection import native_to_state

    state = native_to_state(
        {
            "id": "t",
            "turns": [
                {
                    "id": "turn",
                    "itemsPagination": {"hasLoadedOldest": False},
                    "items": [
                        {
                            "id": "later",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "steer"}],
                        }
                    ],
                }
            ],
        },
        complete=False,
    )
    assert state["turns"][0]["params"]["input"] == []
    assert state["turnsPagination"]["hasLoadedOldest"] is False


def test_summary_items_are_partial_and_paginated_native_history_is_canonical():
    from connector.runtimes.codex.coordination.projection import native_to_state
    from connector.runtimes.codex.coordination.state import history_complete

    state = native_to_state(
        {
            "id": "t",
            "historyMode": "paginated",
            "turns": [
                {
                    "id": "u",
                    "itemsView": "summary",
                    "items": [
                        {
                            "id": "tail",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "later"}],
                        }
                    ],
                }
            ],
        },
        complete=True,
    )
    assert state["turnHistory"]["kind"] == "canonical"
    assert not history_complete(state)
    assert state["turns"][0]["params"]["input"] == []


def test_completion_with_empty_items_does_not_erase_raw_deltas():
    from connector.runtimes.codex.coordination.projection import native_to_state
    from connector.runtimes.codex.coordination.reducer import reduce_event

    state = native_to_state(
        {
            "id": "t",
            "turns": [
                {
                    "id": "u",
                    "status": "inProgress",
                    "items": [
                        {"id": "a", "type": "agentMessage", "text": "live", "future": 5}
                    ],
                }
            ],
        }
    )
    completed = reduce_event(
        state,
        {
            "method": "turn/completed",
            "params": {
                "threadId": "t",
                "turn": {"id": "u", "status": "completed", "items": []},
            },
        },
    )
    assert completed["turns"][0]["items"][0]["text"] == "live"
