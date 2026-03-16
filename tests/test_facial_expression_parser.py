import pytest

from core.facial_expression_parser import (
    parse_facial_expressions,
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
