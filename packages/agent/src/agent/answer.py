"""QNT-294 / QNT-307: the single discriminated-union answer payload.

Replaces the seven optional ``AgentState`` answer slots (``thesis`` /
``quick_fact`` / ``comparison`` / ``comparison_lean`` / ``conversational`` /
``focused`` / ``exploration``) with one ``answer`` field whose type is the union
of every answer shape. A single field holds exactly one payload, so the
"exactly one populated per run" contract that used to be convention-only
(``_assistant_surface`` was a seven-branch if-chain, and every consumer
re-implemented it) is now enforced by the type.

``project_answer`` is the single writer of the answer state: it writes the one
``answer`` field, so no node hand-assembles a multi-key dict and cross-population
is structurally impossible. QNT-307 retired the seven legacy read-compat slots
that QNT-294 kept while consumers migrated: every reader now reads ``answer``,
the followup path reads a dedicated ``prior_answer`` channel (see
``agent.nodes.classify``), and old SqliteSaver checkpoints that still carry the
seven-key shape hydrate harmlessly (unknown channels are ignored; a missing
``answer`` reads as None).

``answer_slot`` maps a payload's concrete type to its SSE event name (the wire
protocol still names cards ``thesis`` / ``quick_fact`` / ...), so the SSE emit
ladder can derive the event name from the union without a discriminator field.
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

# QNT-324: the analytical answer shapes a followup can point back at. classify
# snapshots one of these as ``prior_answer`` at the turn boundary so a followup
# ("which looks stronger?" after a comparison, "so what's the takeaway?" after an
# exploration) reasons over the card the user is pointing at. A prior
# ``ConversationalAnswer`` (chit-chat) or ``QuickFactAnswer`` (a followup's own
# compact card, not a full analysis) is deliberately NOT carried -- only a full
# analytical card is substrate for the next turn.
ANALYTICAL_ANSWER_TYPES: tuple[type, ...] = (
    Thesis,
    ComparisonAnswer,
    LeanComparisonAnswer,
    FocusedAnalysis,
    ExplorationAnswer,
)

# Concrete type -> SSE event name (the wire protocol still names cards
# ``thesis`` / ``quick_fact`` / ...). ``answer`` is the single source of truth;
# this map lets the SSE emit ladder derive the event name from the union without
# a discriminator field. followup reuses the QuickFactAnswer shape, so it maps to
# the ``quick_fact`` event.
_EVENT_BY_TYPE: dict[type, str] = {
    Thesis: "thesis",
    QuickFactAnswer: "quick_fact",
    ComparisonAnswer: "comparison",
    LeanComparisonAnswer: "comparison_lean",
    ConversationalAnswer: "conversational",
    FocusedAnalysis: "focused",
    ExplorationAnswer: "exploration",
}


def answer_slot(payload: object) -> str | None:
    """SSE event name for a payload's shape, or None for a missing/foreign payload.

    Accepts ``object`` (not just ``AnswerPayload``) so callers holding a
    ``BaseModel``-narrowed value can dispatch without a cast -- an unrecognized
    type simply maps to None.
    """
    return None if payload is None else _EVENT_BY_TYPE.get(type(payload))


def project_answer(payload: AnswerPayload | None) -> dict[str, object]:
    """State-write projection for a synthesized answer (QNT-294 / QNT-307).

    Returns the single ``answer`` field. QNT-307 dropped the seven legacy slot
    keys this used to also write; ``answer`` is now the sole write channel, so a
    node cannot populate two answer shapes at once (the union enforces it).
    Does NOT carry ``confidence`` -- the caller adds it, since it is not part of
    the answer payload.
    """
    return {"answer": payload}


__all__ = [
    "ANALYTICAL_ANSWER_TYPES",
    "AnswerPayload",
    "answer_slot",
    "project_answer",
]
