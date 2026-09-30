from __future__ import annotations

import re
from typing import Any

PRIVATE_COMPANY = "PRIVATE_COMPANY"
PUBLIC_AUTHORITY = "PUBLIC_AUTHORITY"
UNKNOWN = "UNKNOWN"
OTHER = "OTHER"
ORGANISATION_TYPES = {PRIVATE_COMPANY, PUBLIC_AUTHORITY, UNKNOWN, OTHER}

_LOCAL_AUTHORITY_SUFFIX = re.compile(
    r"^(?:the\s+)?(?P<place>[a-z][a-z .&'’-]{1,100}?)\s+"
    r"(?:(?:county|city|metropolitan\s+borough|borough|district|unitary|parish|town|community)\s+)?"
    r"council$",
    re.IGNORECASE,
)
_LONDON_BOROUGH = re.compile(
    r"^(?:the\s+)?london\s+borough\s+of\s+(?P<place>[a-z][a-z .&'’-]{1,80})$",
    re.IGNORECASE,
)
_COUNCIL_OF = re.compile(
    r"^(?:the\s+)?council\s+of\s+(?:the\s+)?(?P<place>[a-z][a-z .&'’-]{1,100})$",
    re.IGNORECASE,
)
_AUTHORITY_SUFFIX = re.compile(
    r"^(?:the\s+)?[a-z][a-z .&'’-]{1,100}\s+"
    r"(?:combined|unitary|local)\s+authority$",
    re.IGNORECASE,
)
_PRIVATE_ENDING = re.compile(
    r"\b(?:limited|ltd|plc|llp|community\s+interest\s+company|cic)\.?$",
    re.IGNORECASE,
)
_AMBIGUOUS_COUNCIL_PREFIXES = {
    "arts",
    "business",
    "care",
    "design",
    "medical",
    "nursing",
    "research",
    "sports",
    "trade",
}


def normalized_organisation_type(value: Any) -> str:
    result = str(value or UNKNOWN).strip().upper()
    if result not in ORGANISATION_TYPES:
        raise ValueError("invalid organisation type")
    return result


def is_public_authority_name(value: Any) -> bool:
    """Conservatively recognise structurally named UK councils/local authorities."""
    name = " ".join(str(value or "").replace(",", " ").split()).strip(" .")
    if not name or len(name) > 140 or _PRIVATE_ENDING.search(name):
        return False
    if _LONDON_BOROUGH.fullmatch(name) or _COUNCIL_OF.fullmatch(name):
        return True
    if _AUTHORITY_SUFFIX.fullmatch(name):
        return True
    match = _LOCAL_AUTHORITY_SUFFIX.fullmatch(name)
    if not match:
        return False
    place_words = set(match.group("place").casefold().split())
    return not bool(place_words & _AMBIGUOUS_COUNCIL_PREFIXES)


def looks_like_public_authority(value: Any) -> bool:
    name = " ".join(str(value or "").split()).casefold()
    return bool(re.search(r"\b(?:council|borough|local authority|unitary authority)\b", name))


def public_authority_aliases(value: Any) -> tuple[str, ...]:
    """Return a small explainable alias set; this is not a fuzzy public-body resolver."""
    name = " ".join(str(value or "").split()).strip()
    if not is_public_authority_name(name):
        return ()
    aliases = {name}
    london = _LONDON_BOROUGH.fullmatch(name)
    council_of = _COUNCIL_OF.fullmatch(name)
    suffix = _LOCAL_AUTHORITY_SUFFIX.fullmatch(name)
    if london:
        aliases.add(f"{london.group('place')} Council")
    elif council_of:
        aliases.add(f"{council_of.group('place')} Council")
    elif suffix:
        place = suffix.group("place").strip()
        aliases.add(f"{place} Council")
        if re.search(r"\bCounty Council$", name, re.IGNORECASE):
            aliases.add(f"{place} CC")
    return tuple(sorted(aliases, key=str.casefold))
