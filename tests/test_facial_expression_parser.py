import pytest

from core.facial_expression_parser import (
    parse_facial_expressions,
    strip_speech_tags,
)


def test_parse_no_tags():
    text = "Hello world"
    clean, events = parse_facial_expressions(text)
    assert clean == text
    assert events == []


def test_parse_simple():
    text = "Hi [em_smile:0.5] there"
    clean, events = parse_facial_expressions(text)
    assert clean == "Hi  there"
    assert len(events) == 1
    ev = events[0]
    assert ev.name == "smile"
    assert pytest.approx(ev.intensity, 0.0001) == 0.5
    assert ev.position == 3  # after 'Hi '


def test_parse_reset_and_multiple():
    text = "Start [em_grin] middle [em_sad:0.2] end [em] done"
    clean, events = parse_facial_expressions(text)
    assert clean == "Start  middle  end  done"
    names = [e.name for e in events]
    assert names == ["grin", "sad", None]
    intensities = [e.intensity for e in events]
    assert intensities == [1.0, 0.2, 1.0]


def test_parse_invalid_format():
    # [em_!:x] contains '!' which is not in [a-z_]+, so the regex does NOT match.
    # The tag is left untouched in the output text and no events are produced.
    text = "Bad [em_!:x] text"
    clean, events = parse_facial_expressions(text)
    assert clean == text
    assert events == []


def test_overlapping_tags():
    text = "[em_smile:0.5]Hello[em_sad:0.3]"
    clean, events = parse_facial_expressions(text)
    assert clean == "Hello"
    assert len(events) == 2
    assert events[0].position == 0
    assert events[1].position == 5


def test_tag_missing_intensity():
    text = "Foo [em_angry] Bar"
    clean, events = parse_facial_expressions(text)
    assert events[0].intensity == 1.0


def test_strip_speech_tags_removes_every_form_the_model_emits():
    """TTS must never read a tag out loud; chat text keeps them (owner's call).

    The chat parse above matches only ``[em_NAME:INTENSITY]``, while the model
    routinely writes the colon-separated ``[em:NAME:INTENSITY]`` — that form was
    reaching the synthesiser verbatim.
    """
    assert strip_speech_tags("[em:grin:0.5]") == ""
    assert strip_speech_tags("[em_smile:0.6]") == ""
    assert strip_speech_tags("[em:0.3]") == ""
    assert strip_speech_tags("[em]") == ""
    assert strip_speech_tags("[em_grin]") == ""


def test_strip_speech_tags_keeps_the_words_around_a_tag():
    assert (
        strip_speech_tags("Morning, husband. [em:smile:0.7] You slept like a log.")
        == "Morning, husband. You slept like a log."
    )
    assert strip_speech_tags("Ciao! [em_grin:0.9] Come va?") == "Ciao! Come va?"


def test_strip_speech_tags_leaves_ordinary_brackets_alone():
    for text in ("[email] me", "[3 hours earlier] Then the look", "[laugh] okay"):
        assert strip_speech_tags(text) == text
