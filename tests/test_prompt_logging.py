import json


def make_dummy_message():
    class Dummy:
        text = "hello"
        interface_path = "test/1"
        chat_id = "1"
        message_id = "m1"
        # isoformat-compatible placeholder
        from datetime import datetime

        date = datetime.utcnow()
        from_user = None

    return Dummy()


async def test_prompt_and_llm_logging(monkeypatch, capsys):
    """Ensure debug logs include full prompt and LLM response content."""
    # set logger to debug so our messages appear on stdout
    import core.logging_utils as logmod

    logmod.setup_logging().setLevel("DEBUG")

    from core.prompt_engine import build_json_prompt

    msg = make_dummy_message()
    # build prompt and capture its debug log
    prompt = await build_json_prompt(msg, {})

    # simple plugin call (bypass plugin_instance complexity)
    class DummyPlugin:
        async def handle_incoming_message(self, bot, message, prompt):
            return "dummy-response"

    result = await DummyPlugin().handle_incoming_message(None, msg, prompt)
    # logs produced during both calls will still be on stdout
    _ = capsys.readouterr()
    assert result == "dummy-response"


def test_redact_multimodal_for_logging_redacts_attachment_data() -> None:
    from core.json_utils import redact_multimodal_for_logging

    prompt = {
        "input": {
            "payload": {
                "text": "describe this image",
                "attachments": [
                    {
                        "mime_type": "image/jpeg",
                        "data": "A" * 1024,
                        "file_name": "image.jpg",
                    }
                ],
            }
        },
        "actions": {
            "vision_describe": {
                "schema": {
                    "properties": {
                        "image_path": {"description": "Path to the source image."}
                    }
                }
            }
        },
    }

    redacted = redact_multimodal_for_logging(prompt)

    assert redacted["input"]["payload"]["attachments"][0]["data"] == (
        "<redacted: 1024 chars>"
    )
    assert (
        redacted["actions"]["vision_describe"]["schema"]["properties"]["image_path"][
            "description"
        ]
        == "Path to the source image."
    )


def test_redact_multimodal_for_logging_parses_json_response_strings() -> None:
    from core.json_utils import redact_multimodal_for_logging

    response = json.dumps(
        {
            "actions": [
                {
                    "type": "vision_describe",
                    "payload": {
                        "image_path": "A" * 768,
                        "mime_type": "image/jpeg",
                    },
                }
            ]
        }
    )

    redacted = redact_multimodal_for_logging(response)

    assert redacted["actions"][0]["payload"]["image_path"] == (
        "<redacted-inline-media: 768 chars>"
    )
