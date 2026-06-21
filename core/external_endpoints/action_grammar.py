"""GBNF grammar builder for the SyntH action-JSON protocol.

llama.cpp can constrain decoding to a `GBNF <https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md>`_
grammar. Feeding it a grammar for the action schema forces the model to emit
exactly one ``{"actions": [{"type": <known name>, "payload": {...}}], "meta"?: ...}``
object — no reasoning preamble, no malformed JSON, no invented/duplicated action
types, and no repeated trailing objects (the grammar completes after the first
object, so generation stops).

Used only by the openai_compat cortex path, opt-in via the endpoint's
``extra_config.force_action_grammar``. Other engines never see it.
"""

from __future__ import annotations


def _escape_gbnf_literal(value: str) -> str:
    """Escape a string for use inside a GBNF double-quoted terminal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def build_actions_gbnf(action_names: list[str]) -> str | None:
    """Return a GBNF grammar enforcing the action-JSON shape, or ``None``.

    The ``type`` field is constrained to the exact set of ``action_names`` so the
    model cannot invent, combine, or abbreviate an action name. ``payload`` is an
    arbitrary JSON object (per-action payload schemas are intentionally not
    encoded — that would be enormous and brittle). Returns ``None`` when there
    are no usable names so callers can skip attaching a grammar.
    """
    names = [n for n in dict.fromkeys(action_names) if n]
    if not names:
        return None

    # Each alternative matches the JSON string literal "name" — i.e. the GBNF
    # terminal "\"name\"".
    type_alts = " | ".join(
        '"\\"' + _escape_gbnf_literal(name) + '\\""' for name in names
    )

    return (
        'root        ::= ws "{" ws "\\"actions\\"" ws ":" ws actionarray metaopt ws "}" ws\n'
        'metaopt     ::= ( ws "," ws "\\"meta\\"" ws ":" ws value )?\n'
        'actionarray ::= "[" ws action ( ws "," ws action )* ws "]"\n'
        'action      ::= "{" ws "\\"type\\"" ws ":" ws actiontype ws "," ws "\\"payload\\"" ws ":" ws object ws "}"\n'
        "actiontype  ::= " + type_alts + "\n"
        "value       ::= object | array | string | number | boolnull\n"
        'boolnull    ::= ( "true" | "false" | "null" ) ws\n'
        'object      ::= "{" ws ( member ( ws "," ws member )* )? ws "}" ws\n'
        'member      ::= string ws ":" ws value\n'
        'array       ::= "[" ws ( value ( ws "," ws value )* )? ws "]" ws\n'
        'string      ::= "\\"" char* "\\"" ws\n'
        'char        ::= [^"\\\\] | "\\\\" ( ["\\\\/bfnrt] | "u" hex hex hex hex )\n'
        "hex         ::= [0-9a-fA-F]\n"
        'number      ::= "-"? int frac? exp? ws\n'
        'int         ::= "0" | [1-9] [0-9]*\n'
        'frac        ::= "." [0-9]+\n'
        "exp         ::= [eE] [-+]? [0-9]+\n"
        "ws          ::= [ \\t\\n]*\n"
    )
