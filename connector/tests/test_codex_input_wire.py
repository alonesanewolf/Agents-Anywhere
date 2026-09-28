"""Native UI text spans must survive the SDK-to-coordination wire boundary."""

from connector.runtimes.codex.sdk import client
from connector.runtimes.codex.sdk.runtime_client import CodexSteerTurnRequest
from openai_codex.generated.v2_all import UserInput


def test_native_wire_preserves_nonempty_text_spans_and_omits_none(monkeypatch):
    # Requests currently create plain text. Supply a typed SDK input at the
    # builder seam to protect future native spans from an unconditional [] fix.
    inputs = [
        UserInput.model_validate(
            {
                "type": "text",
                "text": "hello",
                "text_elements": [
                    {"byteRange": {"start": 0, "end": 5}, "placeholder": "native"},
                    {"byteRange": {"start": 1, "end": 2}, "placeholder": None},
                ],
            }
        ),
        UserInput.model_validate({"type": "localImage", "path": "/image.png"}),
    ]
    monkeypatch.setattr(client, "codex_turn_user_input", lambda request: inputs)

    result = client.codex_turn_user_input_wire(
        CodexSteerTurnRequest("thread", "turn", "hello")
    )

    assert result == [
        {
            "type": "text",
            "text": "hello",
            "text_elements": [
                {"byteRange": {"start": 0, "end": 5}, "placeholder": "native"},
                {"byteRange": {"start": 1, "end": 2}},
            ],
        },
        {"type": "localImage", "path": "/image.png"},
    ]
