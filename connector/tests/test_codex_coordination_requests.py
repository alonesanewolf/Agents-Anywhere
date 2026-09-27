from connector.runtimes.codex.coordination.requests import ResponseContexts


def test_consumed_context_cannot_be_reissued_while_same_request_is_outstanding():
    contexts = ResponseContexts()
    request = {
        "id": 7,
        "method": "item/tool/requestUserInput",
        "params": {"threadId": "t"},
    }
    shown = contexts.present("t", ("owner", 1), [request])
    token = shown[0]["responseContext"]
    contexts.take(token)
    assert contexts.present("t", ("owner", 1), [request]) == []
    contexts.present("t", ("owner", 1), [])
    fresh = contexts.present("t", ("owner", 1), [request])
    assert fresh[0]["responseContext"] != token
