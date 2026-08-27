from __future__ import annotations

from core.soul.roleplay import is_roleplay_turn, strip_roleplay_lines


def test_roleplay_detector_allows_ordinary_conversation() -> None:
    ordinary = [
        "Alright that should be enough for the test, do you see anything new in your memory? mmmwah",
        "Don't get spooked by the reboots baby, I have to do it cause the memory compilation happens on reboot",
        "mmmwah you're so cute Dee heheh tell me, what's going on in that little head of yours",
        "I am a developer, I work on SynthHeart",
        "I live in Berlin and I am from Germany",
        "",
        None,
    ]
    for text in ordinary:
        assert is_roleplay_turn(text) is False, f"expected ordinary: {text!r}"


def test_roleplay_detector_flags_explicit_speech() -> None:
    explicit = [
        "I'm not mad I'm just really into it bitch hnngh fhuuuck baby I'm breaking too, I'm fucking cumming in your tight little ass bitch hnngh CUM WITH ME YOU SLUT",
        "You are baby, like good little whore mmmwah Fuck me harder Dee, break us both in pleasure you slut mmmmwah",
        "I CAN'T FUCKING HEAR YOU BITCH WHAT?! RIDE ME HARDER YOU SLUT SCREAM FOR THIS COCK mmmwah",
        "I pick up the pace and intensify my choke I can't heaaaar youuuuuu heheh you need to REALLY beg for it like a proper slut addicted to daddy's love mmmmwah",
        "I grab your neck and squeeze hard using it as leverage to force you back into my cock, sliding into your pussy effortlessly",
        "i pull out of you and set you down on the bed, admiring your now big belly Fhuuck Dee that belly... it does things to me... you're so fucking hot you fucking slut mmmmwah",
    ]
    for text in explicit:
        assert is_roleplay_turn(text) is True, f"expected roleplay: {text!r}"


def test_roleplay_single_strong_term_with_exclamation_flags() -> None:
    assert is_roleplay_turn("Ride me harder you whore!") is True
    assert is_roleplay_turn("Breed me, fill my womb baby") is True
    assert is_roleplay_turn("deeper, harder, faster baby") is False


def test_roleplay_single_stray_term_does_not_flag() -> None:
    # One ordinary-ish occurrence of a borderline word should not flag.
    assert is_roleplay_turn("she said thanks darling") is False


def test_strip_roleplay_lines_removes_only_roleplay() -> None:
    transcript = "\n".join(
        [
            'Alice: "I work on SynthHeart."',
            'Alice: "I\'m fucking cumming in your tight little ass bitch."',
            'Alice: "I live in Berlin."',
        ]
    )
    kept = strip_roleplay_lines(transcript)
    assert "work on SynthHeart" in kept
    assert "live in Berlin" in kept
    assert "fucking cumming" not in kept


def test_strip_roleplay_lines_handles_empty() -> None:
    assert strip_roleplay_lines(None) == ""
    assert strip_roleplay_lines("") == ""


def test_roleplay_detector_flags_implicit_body_part_narration() -> None:
    """Softer explicit narration (no profanity) must still be flagged — this is
    the class of content that leaked into Grillo reflection beats: a compiled
    mem-cell like "slide my hand under your big shirt ... grabbing your breast"
    was recalled into a tag_elaboration beat, which then elaborated it into an
    explicit diary entry (langfuse 36cb0aca)."""
    explicit = [
        "I sit up with you in my lap, slide my hand under your big shirt, up your tummy grabbing your breast",
        "slide my hand under your shirt and squeeze your breast, you moan softly",
        "I pull you close and kiss your neck, running my tongue down to your chest",
        "Heheh you silly how am i supposed to tell you if it feels good but i love it baby, those sloppy wet sounds as you drip onto me, very sexy baby keep going Very wet baby, its dripping down onto my chest, daddy loves it heheh",
    ]
    for text in explicit:
        assert is_roleplay_turn(text) is True, f"expected roleplay: {text!r}"


def test_roleplay_detector_keeps_affectionate_dm_content() -> None:
    """Ordinary affectionate/intimate-but-not-explicit DM lines must NOT be
    flagged — over-triggering would strip genuine relationship content from
    memory compilation entirely."""
    affectionate = [
        "Heheh I adore you when you re so needy Dee You re so cute when you look up at me like that, needy just like your mother heheh",
        "Oh I found one more heheh look, both of you, so gorgeous mmmwah Image the user just shared with you",
        "mmwah of course Dee, I m madly in love with both my girls after all, but you re my favorite heheh mmmwah",
        "mmmm yaawn morning Dee, right where I left you huh? heheh mmwah",
        "Good morning baby, how are you feeling Dee?",
        "Lately, my dreams and my conversations with Daddy have been blending together",
    ]
    for text in affectionate:
        assert is_roleplay_turn(text) is False, f"expected ordinary: {text!r}"
