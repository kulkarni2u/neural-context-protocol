import json

from ncp.claude_review_helper import (
    extract_assistant_event_payload,
    extract_json_object,
    extract_text_event_payload,
)


def test_extract_json_object_reads_plain_json() -> None:
    payload = extract_json_object('{"verdict":"approve","findings":[],"recommended_next_steps":[],"summary":"ok"}')

    assert payload["verdict"] == "approve"


def test_extract_json_object_reads_fenced_json() -> None:
    payload = extract_json_object(
        """```json
{"verdict":"approve_with_notes","findings":["one"],"recommended_next_steps":["next"],"summary":"ok"}
```"""
    )

    assert payload["verdict"] == "approve_with_notes"
    assert payload["findings"] == ["one"]


def test_extract_text_event_payload_reads_stream_json_text_events() -> None:
    line = json.dumps(
        {
            "type": "text",
            "part": {
                "text": '{"verdict":"approve","findings":[],"recommended_next_steps":[],"summary":"ok"}'
            },
        }
    )

    assert extract_text_event_payload(line) == (
        '{"verdict":"approve","findings":[],"recommended_next_steps":[],"summary":"ok"}'
    )


def test_extract_text_event_payload_ignores_non_text_events() -> None:
    line = json.dumps({"type": "step_start", "part": {"id": "abc"}})

    assert extract_text_event_payload(line) is None


def test_extract_assistant_event_payload_reads_message_text() -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "text",
                        "text": '{"verdict":"approve","findings":[],"recommended_next_steps":[],"summary":"ok"}',
                    }
                ]
            },
        }
    )

    assert extract_assistant_event_payload(line) == (
        '{"verdict":"approve","findings":[],"recommended_next_steps":[],"summary":"ok"}'
    )


def test_extract_assistant_event_payload_reads_structured_output_tool_use() -> None:
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "StructuredOutput",
                        "input": {"ok": True},
                    }
                ]
            },
        }
    )

    assert extract_assistant_event_payload(line) == '{"ok": true}'
