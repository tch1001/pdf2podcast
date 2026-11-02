"""Remove bibliographic asides while preserving bracketed measurements and mathematical values."""

from __future__ import annotations

import re

_NUMERIC = re.compile(r"\[\s*[1-9]\d{0,3}(?:\s*[,;\-\u2013\u2014]\s*[1-9]\d{0,3})*\s*\]")
_PARENS = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]")
_CAPITAL = r"[A-Z\u00c0-\u00d6\u00d8-\u00de]"
_NAME = rf"(?:(?:van|von|de|del|der|da|dos|du|le)\s+){{0,2}}{_CAPITAL}[^\W\d_]*(?:['\u2019-][^\W\d_]+)*\.?"
_PERSON = rf"{_NAME}(?:\s+{_NAME}){{0,3}}"
_AUTHORS = rf"{_PERSON}(?:\s+et\s+al\.?|\s*(?:,|&|and)\s*{_PERSON})*"
_YEAR = r"(?:18|19|20)\d{2}[a-z]?"
_REFERENCE = rf"{_AUTHORS}\s*,?\s*{_YEAR}(?:\s*(?:,|and|[-\u2013])\s*{_YEAR})*(?:\s*,\s*pp?\.\s*\d+(?:[-\u2013]\d+)?)?"
_REFERENCE_LIST = re.compile(rf"{_REFERENCE}(?:\s*(?:;|,|and)\s*{_REFERENCE})*")
_MATH_CONTEXT = {
    "vector", "vectors", "matrix", "matrices", "array", "arrays", "interval",
    "intervals", "range", "ranges", "indices", "index", "values", "coordinates",
    "shape", "tensor", "dimension", "dimensions", "entry", "entries", "element",
    "elements", "axis", "axes",
}
_MONTHS = {
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
}


def _citation_only(value: str) -> bool:
    value = re.sub(
        r"^(?:(?:see(?:\s+also)?|e\.g\.|cf\.|for\s+example)\s*[,;:]?\s*)+",
        "", value.strip(), flags=re.IGNORECASE,
    )
    value = re.sub(r"(?:\.{3}|\u2026|;)\s*$", "", value).strip()
    value = re.sub(
        r",?\s+(?:for\s+details|for\s+a\s+review)\s*$", "", value,
        flags=re.IGNORECASE,
    )
    if value.split(",", 1)[0].strip().lower() in _MONTHS:
        return False
    return bool(_REFERENCE_LIST.fullmatch(value))


def without_citations(text: str) -> str:
    """Clean completed paragraphs, so citations split across PDF pages are handled together."""
    original = text

    def numeric(match: re.Match[str]) -> str:
        prefix = original[:match.start()]
        words = re.findall(r"[A-Za-z]+", prefix)
        if words and words[-1].lower() in _MATH_CONTEXT:
            return match.group(0)
        if prefix and not prefix[-1].isspace() and words and len(words[-1]) <= 2:
            return match.group(0)
        if prefix.rstrip().endswith(("=", "<", ">", "+", "\u2208")):
            return match.group(0)
        return ""

    text = _NUMERIC.sub(numeric, text)

    def parenthetical(match: re.Match[str]) -> str:
        value = match.group(1) if match.group(1) is not None else match.group(2)
        if _citation_only(value):
            return ""
        clauses = [part.strip() for part in value.split(";")]
        kept = [part for part in clauses if not _citation_only(part)]
        if len(kept) != len(clauses):
            return match.group(0)[0] + "; ".join(kept) + match.group(0)[-1] if kept else ""
        return match.group(0)

    text = _PARENS.sub(parenthetical, text)
    text = re.sub(
        rf"(?P<author>{_AUTHORS})\s*\(\s*{_YEAR}(?:\s*[,;\-\u2013]\s*{_YEAR})*\s*\)",
        lambda match: match.group("author"), text,
    )
    text = re.sub(r"\(\s*\)|\[\s*\]", "", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()
