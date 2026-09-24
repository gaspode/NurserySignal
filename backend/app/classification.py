"""Deterministic signal relevance rules shared by collectors and enrichment."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

CLASSIFICATION_RULE_VERSION = "planning-context-v2"

# These terms describe meaningful childcare context.  A bare "nursery" is
# intentionally not included: it is too ambiguous to override horticultural
# evidence on its own.
CHILDCARE_EVIDENCE_TERMS = (
    "children's nursery",
    "childrens nursery",
    "child nursery",
    "childcare",
    "child care",
    "early years",
    "preschool",
    "pre-school",
    "day nursery",
    "creche",
    "crèche",
    "montessori",
    "nursery places",
    "nursery children",
    "nursery manager",
    "nursery practitioner",
)

# Keep labels stable because they are retained in extracted_facts and are
# useful when a reviewer explains why a candidate was rejected.
HORTICULTURAL_EVIDENCE_PATTERNS = (
    ("community garden nursery", r"\bcommunity\s+garden\s+nurser(?:y|ies)\b"),
    ("garden nursery", r"\bgarden\s+nurser(?:y|ies)\b"),
    ("plant nursery", r"\bplant\s+nurser(?:y|ies)\b"),
    ("tree nursery", r"\btree\s+nurser(?:y|ies)\b"),
    ("horticultural nursery", r"\bhorticultural\s+nurser(?:y|ies)\b"),
    ("nursery stock", r"\bnursery\s+stock\b"),
    ("garden centre", r"\bgarden\s+centr(?:e|er)\b"),
    ("growing plants", r"\bgrowing\s+plants?\b"),
    ("propagation", r"\bpropagation\b"),
    ("seedlings", r"\bseedlings?\b"),
    ("saplings", r"\bsaplings?\b"),
    ("horticulture", r"\bhorticultur(?:e|al)\b"),
    ("gardening", r"\bgardening\b"),
    ("plants", r"\bplants?\b"),
    ("RHS", r"\brhs\b"),
)

_NEGATION_PREFIX = re.compile(
    r"\b(?:no|not|without|never)\b(?:\W+\w+){0,3}\W*$", re.IGNORECASE
)


@dataclass(frozen=True)
class SignalClassification:
    """The explainable, deterministic result of the relevance rules."""

    text: str
    childcare_terms: tuple[str, ...]
    horticultural_terms: tuple[str, ...]
    likely_false_positive: bool


def _flatten_text(value: Any) -> Iterable[str]:
    if value is None:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for nested in value.values():
            yield from _flatten_text(nested)
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            yield from _flatten_text(nested)
    else:
        yield str(value)


def combined_text(values: Iterable[Any]) -> str:
    """Combine all relevant source fields without logging or mutating them."""

    return " ".join(part.casefold() for value in values for part in _flatten_text(value))


def _has_meaningful_childcare(text: str, term: str) -> bool:
    for match in re.finditer(re.escape(term), text, re.IGNORECASE):
        # Phrases such as "no childcare opening" are explicitly negative
        # evidence and must not override a horticultural classification.
        if not _NEGATION_PREFIX.search(text[: match.start()]):
            return True
    return False


def classify_signal_text(values: Iterable[Any]) -> SignalClassification:
    text = combined_text(values)
    childcare_terms = tuple(
        term for term in CHILDCARE_EVIDENCE_TERMS if _has_meaningful_childcare(text, term)
    )
    horticultural_terms = tuple(
        label
        for label, pattern in HORTICULTURAL_EVIDENCE_PATTERNS
        if re.search(pattern, text, re.IGNORECASE)
    )
    return SignalClassification(
        text=text,
        childcare_terms=childcare_terms,
        horticultural_terms=horticultural_terms,
        likely_false_positive=bool(horticultural_terms) and not childcare_terms,
    )
