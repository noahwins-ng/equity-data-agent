"""QNT-294 (AC2): the single discriminated-union answer payload.

Replaces the eight optional ``AgentState`` answer slots (``thesis`` /
``quick_fact`` / ``comparison`` / ``comparison_lean`` / ``conversational`` /
``focused`` / ``exploration``) with one ``answer`` field whose type is the union
of every answer shape. A single field holds exactly one payload, so the
"exactly one populated per run" contract that used to be convention-only
(``_assistant_surface`` was a seven-branch if-chain, and every consumer
re-implemented it) is now enforced by the type.

``project_answer`` is the single writer of the answer state: it derives the
matching legacy slot from the payload's concrete type and clears the rest, so no
node hand-assembles the multi-key dict and cross-population is structurally
impossible. The legacy slots remain as deprecated read-compat channels -- the
checkpointer persists them, the SSE citation/emit ladder and the eval scorers
still read them, and the followup path leans on a separately-hydrated
``thesis`` channel -- so consumers migrate to ``answer`` incrementally rather
than in one diff.
"""

from __future__ import annotations

from agent.comparison import ComparisonAnswer, LeanComparisonAnswer
from agent.conversational import ConversationalAnswer
from agent.exploration import ExplorationAnswer
from agent.focused import FocusedAnalysis
from agent.quick_fact import QuickFactAnswer
from agent.thesis import Thesis

# Discriminated by concrete type -- each shape is a distinct Pydantic model, so
# isinstance is the tag (a Pydantic ``Field(discriminator=...)`` would need a
# shared literal on every model, changing the JSON schema the LLM sees). Order
# is registry order, matching the SSE emit ladder.
AnswerPayload = (
    Thesis
    | QuickFactAnswer
    | ComparisonAnswer
    | LeanComparisonAnswer
    | ConversationalAnswer
    | FocusedAnalysis
    | ExplorationAnswer
)

# Concrete type -> legacy AgentState slot name. The slot keys are the deprecated
# read-compat channels; ``answer`` is the source of truth. followup reuses the
# QuickFactAnswer shape, so it maps to the ``quick_fact`` slot.
_SLOT_BY_TYPE: dict[type, str] = {
    Thesis: "thesis",
    QuickFactAnswer: "quick_fact",
    ComparisonAnswer: "comparison",
    LeanComparisonAnswer: "comparison_lean",
    ConversationalAnswer: "conversational",
    FocusedAnalysis: "focused",
    ExplorationAnswer: "exploration",
}
ANSWER_SLOTS: tuple[str, ...] = tuple(_SLOT_BY_TYPE.values())


def answer_slot(payload: object) -> str | None:
    """Legacy slot name for a payload's shape, or None for a missing/foreign payload.

    Accepts ``object`` (not just ``AnswerPayload``) so callers holding a
    ``BaseModel``-narrowed value can dispatch without a cast -- an unrecognized
    type simply maps to None.
    """
    return None if payload is None else _SLOT_BY_TYPE.get(type(payload))


def project_answer(payload: AnswerPayload | None) -> dict[str, object]:
    """State-write projection for a synthesized answer (QNT-294 AC2).

    Returns the ``answer`` field plus every legacy slot: all cleared to ``None``
    except the one matching ``payload``'s concrete type. This is the single
    place the legacy answer slots are written, so a node cannot populate two
    answer shapes at once. Replaces the old ``_empty_payload()`` + set-one-slot
    idiom. Does NOT carry ``confidence`` -- the caller adds it, since it is not
    part of the answer payload.
    """
    slot = answer_slot(payload)
    projected: dict[str, object] = {"answer": payload}
    for name in ANSWER_SLOTS:
        projected[name] = payload if name == slot else None
    return projected


__all__ = [
    "ANSWER_SLOTS",
    "AnswerPayload",
    "answer_slot",
    "project_answer",
]
