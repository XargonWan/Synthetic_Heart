"""Turn a debrief's "this is false" verdicts into stored retractions.

:mod:`core.soul.retractions` holds the store helpers and the prompt blocks; this
module is the write path. It is called by the situational-notes debrief, whose
single extraction call also reports ``retracted`` and ``lifted`` (one LLM call
per turn, not two).

Authority: a retraction from the trainer is ``active`` at once. One from anyone
else is stored ``pending`` and changes nothing until the trainer confirms it. An
active retraction then retires what is still standing on the false claim - the
matching situational notes, active goals and a pending growth proposal. Diary,
memories and scenes are history and are never touched; the prompt blocks tell
the model they record a mistake.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from core.logging_utils import log_debug, log_info, log_warning

from .models import RetractedClaim, now_utc, retracted_claim_id
from .retractions import claim_matching
from .situational import is_same_circumstance, subject_tokens

MAX_RETRACTIONS_PER_TURN = 4
_MAX_FIELD_CHARS = 300


@dataclass(slots=True)
class RetractionOutcome:
    stored: int = 0
    pending: int = 0
    lifted: int = 0
    retired_notes: int = 0
    abandoned_goals: int = 0
    discarded_proposal: bool = False


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())[:_MAX_FIELD_CHARS]


def parse_retractions(parsed: Any) -> list[dict[str, str]]:
    """Pull ``{"subject", "claim", "reason"}`` dicts out of the model's answer."""
    if not isinstance(parsed, dict):
        return []
    raw = parsed.get("retracted")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    items: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        subject = _clean(entry.get("subject"))
        claim = _clean(entry.get("claim")) or subject
        if not subject or not subject_tokens(subject):
            continue
        items.append(
            {"subject": subject, "claim": claim, "reason": _clean(entry.get("reason"))}
        )
    return items[:MAX_RETRACTIONS_PER_TURN]


def parse_lifted(parsed: Any) -> list[str]:
    """Pull the subjects of retractions the human has withdrawn."""
    if not isinstance(parsed, dict):
        return []
    raw = parsed.get("lifted")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    subjects: list[str] = []
    for item in raw:
        value = item.get("subject") if isinstance(item, dict) else item
        if isinstance(value, str) and value.strip():
            subjects.append(_clean(value))
    return subjects[:MAX_RETRACTIONS_PER_TURN]


def is_trainer_turn(context: Any, original_message: Any) -> bool:
    """Whether the human behind this turn is the trainer.

    Reads the flag the message queue already put on the context, then falls back
    to the interface registry. Anything undeterminable is NOT the trainer: the
    consequence is a ``pending`` retraction, never a silent unlearning.
    """
    if isinstance(context, dict) and context.get("is_trainer"):
        return True
    try:
        user = getattr(original_message, "from_user", None)
        user_id = getattr(user, "id", None)
        interface = (
            context.get("interface") if isinstance(context, dict) else None
        ) or getattr(original_message, "interface", None)
        if user_id is None or not interface:
            return False
        from core.interfaces_registry import get_interface_registry

        return bool(get_interface_registry().is_trainer(str(interface), user_id))
    except Exception as exc:
        log_debug(f"[retraction_flow] trainer check failed: {exc}")
        return False


def describe_sender(context: Any, original_message: Any) -> str:
    """A short label of who spoke, for the audit trail on a retraction."""
    user = getattr(original_message, "from_user", None)
    for attr in ("full_name", "username", "first_name", "id"):
        value = getattr(user, attr, None)
        if value:
            return _clean(value)[:80]
    return ""


def source_reference(context: Any, original_message: Any) -> str:
    path = ""
    if isinstance(context, dict):
        path = str(context.get("interface_path") or "")
    path = path or str(getattr(original_message, "interface_path", "") or "")
    return f"{path} @ {now_utc().isoformat(timespec='seconds')}".strip()


def _find_standing(
    existing: Iterable[RetractedClaim], subject: str
) -> RetractedClaim | None:
    """The stored claim that names the same thing as ``subject``, if any."""
    wanted = subject_tokens(subject)
    for claim in existing:
        if is_same_circumstance(wanted, subject_tokens(claim.subject)):
            return claim
    return None


async def apply_retractions(
    repository: Any,
    items: list[dict[str, str]],
    *,
    trainer: bool,
    asserted_by: str = "",
    source_ref: str = "",
) -> RetractionOutcome:
    """Store the retractions of one turn and enforce the ones now in force."""
    outcome = RetractionOutcome()
    if not items:
        return outcome
    existing = await repository.list_retracted_claims()
    for item in items:
        standing = _find_standing(existing, item["subject"])
        if standing is not None and standing.status == "rejected" and not trainer:
            # The trainer refused this once; a stranger cannot re-open it.
            continue
        status = "active" if trainer else "pending"
        if standing is not None and standing.status == "active":
            # Already in force: nothing to learn, nothing to demote.
            continue
        claim = RetractedClaim(
            id=standing.id
            if standing is not None
            else retracted_claim_id(item["subject"], item["claim"]),
            subject=standing.subject if standing is not None else item["subject"],
            claim=standing.claim if standing is not None else item["claim"],
            reason=item["reason"] or (standing.reason if standing else ""),
            status=status,
            asserted_by=asserted_by,
            source_ref=source_ref,
        )
        try:
            await repository.upsert_retracted_claim(claim)
        except Exception as exc:
            log_warning(f"[retraction_flow] could not store a retraction: {exc}")
            continue
        if status == "active":
            outcome.stored += 1
            await enforce_claim(repository, claim, outcome)
        else:
            outcome.pending += 1
    return outcome


async def lift_retractions(
    repository: Any, subjects: list[str], *, trainer: bool
) -> int:
    """Withdraw retractions the trainer has taken back. Others cannot.

    An ``active`` retraction becomes ``lifted``. A ``pending`` one - someone
    else's, which the trainer now refuses - becomes ``rejected`` so a stranger
    cannot raise it again.
    """
    if not subjects or not trainer:
        return 0
    lifted = 0
    standing = await repository.list_retracted_claims(("active", "pending"))
    for subject in subjects:
        claim = _find_standing(standing, subject)
        if claim is None:
            continue
        try:
            await repository.set_retraction_status(
                claim.id, "rejected" if claim.status == "pending" else "lifted"
            )
            lifted += 1
        except Exception as exc:
            log_warning(f"[retraction_flow] could not lift {claim.id}: {exc}")
    return lifted


def _matches(claim: RetractedClaim) -> Callable[[str, str], bool]:
    def matcher(subject: str, text: str) -> bool:
        return claim_matching([claim], subject=subject, text=text) is not None

    return matcher


async def enforce_claim(
    repository: Any, claim: RetractedClaim, outcome: RetractionOutcome | None = None
) -> RetractionOutcome:
    """Retire the live rows that still stand on a claim now in force."""
    outcome = outcome or RetractionOutcome()
    matches = _matches(claim)

    try:
        for note in await repository.list_active_situational_notes(now=now_utc()):
            if matches(note.subject, f"{note.subject} {note.summary}"):
                await repository.resolve_situational_note(
                    note.id, new_status="resolved"
                )
                outcome.retired_notes += 1
    except Exception as exc:
        log_warning(f"[retraction_flow] could not retire notes: {exc}")

    try:
        from plugins.goals.goals import abandon_goals_matching

        outcome.abandoned_goals += await abandon_goals_matching(
            lambda text: matches("", text),
            note=f"Withdrawn: the trainer said this is false ({claim.subject}).",
        )
    except Exception as exc:
        log_debug(f"[retraction_flow] goals not updated: {exc}")

    try:
        from core.core_initializer import PLUGIN_REGISTRY

        for plugin in list(PLUGIN_REGISTRY.values()):
            discard = getattr(plugin, "discard_pending_proposal_matching", None)
            if callable(discard) and await discard([claim]):
                outcome.discarded_proposal = True
    except Exception as exc:
        log_debug(f"[retraction_flow] growth proposal not checked: {exc}")

    if outcome.retired_notes or outcome.abandoned_goals or outcome.discarded_proposal:
        log_info(
            f"[retraction_flow] '{claim.subject}' enforced: "
            f"{outcome.retired_notes} note(s), {outcome.abandoned_goals} goal(s), "
            f"proposal discarded={outcome.discarded_proposal}"
        )
    return outcome


# Operator decisions (WebUI): the status each action moves a claim to.
_DECISIONS = {
    "confirm": "active",
    "restore": "active",
    "reject": "rejected",
    "lift": "lifted",
}


async def decide_retraction(
    repository: Any, claim_id: str, action: str
) -> RetractedClaim | None:
    """Apply an operator's decision to one stored retraction.

    ``confirm`` (a pending one) and ``restore`` (a lifted or rejected one) put it
    in force and retire what still stands on it; ``reject`` refuses a pending
    one; ``lift`` withdraws one in force. Returns the updated claim, or ``None``
    when ``claim_id`` is unknown. An unknown ``action`` raises ``ValueError``.
    """
    new_status = _DECISIONS.get(action)
    if new_status is None:
        raise ValueError(f"unknown action: {action!r}")
    claim = next(
        (c for c in await repository.list_retracted_claims() if c.id == claim_id),
        None,
    )
    if claim is None:
        return None
    await repository.set_retraction_status(claim_id, new_status)
    claim.status = new_status
    if new_status == "active":
        await enforce_claim(repository, claim)
    return claim
