from core.text_utils import looks_like_mojibake, try_recover_mojibake


def test_detects_mojibake_for_flag_emoji():
    proper = "🇬🇧 English: Loehnert"
    # create mojibake by decoding UTF-8 bytes as latin-1
    mojibake = proper.encode("utf-8").decode("latin-1")
    assert looks_like_mojibake(mojibake) is True
    recovered = try_recover_mojibake(mojibake)
    assert recovered == proper


def test_no_false_positive_on_normal_ascii():
    text = "Hello, this is normal ASCII text."
    assert looks_like_mojibake(text) is False
    assert try_recover_mojibake(text) == text


def test_recovery_handles_windows1252_variants():
    proper = "Café — résumé"
    mojibake = proper.encode("utf-8").decode("latin-1")
    assert looks_like_mojibake(mojibake) is True
    recovered = try_recover_mojibake(mojibake)
    assert recovered == proper
