"""Keep publication names faithful to source evidence instead of exposing upload identifiers."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import unquote


def _clean(value: Any) -> str:
    return " ".join(value.replace("\x00", "").split()) if isinstance(value, str) else ""


_IDENTIFIERS = re.compile(
    r"(?:arxiv[: _-]*)?(?:\d{4}[._]\d{4,5}|[a-z][a-z.\-]*/\d{7})(?:v\d+)?"
    r"|(?:doi[: _-]*)?10[._]\d{4,9}[/_:.-]\S+"
    r"|(?:nihpp[-_])?(?:19|20)\d{2}[._]\d{2}[._]\d{2}[._]\d+(?:v\d+)?(?:[._]full)?"
    r"|s\d{4,}-\d{2,4}-\d{3,}-[a-z0-9]+"
    r"|(?:[a-z]+-)?\d+(?:[-_.]\d+)+(?:[-_.][a-z])?"
    r"|(?:pdf-)?[a-f0-9]{16,}"
    r"|\{?[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\}?",
    re.IGNORECASE,
)
_PLACEHOLDERS = {
    "untitled", "untitled document", "unknown", "none", "null", "document", "paper",
    "input", "output", "main", "download", "manuscript", "article", "title", "no title",
}
_FURNITURE = re.compile(
    r"^(?:abstract|introduction|references|keywords|article|research article|review|letter)$"
    r"|^(?:arxiv\b|doi\b|https?://|www\.|journal\b|contents lists|published\b|accepted\b"
    r"|received\b|copyright\b|preprint\b|biorxiv\b|medrxiv\b|available\b|submitted\b)"
    r"|(?:@|all rights reserved|creative commons|cc-by|peer review|copyright holder)",
    re.IGNORECASE,
)


def is_bad_title(value: Any, *, filename: str | None = None) -> bool:
    """Reject clear identifiers and filename placeholders without recasing scientific names."""
    title = _clean(value)
    decoded = unquote(title)
    if not title or len(title) > 600 or len(title.split()) > 80:
        return True
    if title.casefold() in _PLACEHOLDERS or not any(char.isalpha() for char in title):
        return True
    if _IDENTIFIERS.fullmatch(decoded):
        return True
    if re.search(r"\.(?:pdf|docx?|tex|rtf|ps)(?:\s*\(\d+\))?$", decoded, re.IGNORECASE):
        return True
    if re.match(r"^(?:https?://|file:|[/\\]|[a-z]:[/\\]|Microsoft (?:Word|PowerPoint)\b)", decoded, re.IGNORECASE):
        return True
    if filename:
        basename = unquote(filename).replace("\\", "/").rsplit("/", 1)[-1]
        stem = re.sub(r"\.pdf$", "", basename, flags=re.IGNORECASE)
        if decoded.casefold() == basename.casefold():
            return True
        if decoded.casefold() == stem.casefold() and not re.search(r"\s", decoded):
            if re.search(r"[_/\\]|v\d+$", decoded, re.IGNORECASE) or "-" in decoded:
                return True
    return False


def select_document_title(
    *, model_title: Any = None, pdf_title: Any = None, first_page_title: Any = None,
    current_title: Any = None, filename: str | None = None, fallback: str = "Untitled document",
) -> str:
    """Prefer a full model-extracted title, then PDF evidence, never an inferred filename."""
    for candidate in (model_title, pdf_title, first_page_title, current_title):
        if not is_bad_title(candidate, filename=filename):
            return _clean(candidate)
    return fallback


def first_page_title(lines: Iterable[Mapping[str, Any]]) -> str | None:
    """Recover a prominent opening heading only when PDF typography provides clear evidence.

    Consume existing extracted lines, without reading a PDF or changing paragraph data.
    Restrict to physical page one and exclude vertical arXiv stamps and journal furniture.
    Ambiguous layouts are left for vision rather than guessed from prose or author lists.
    """
    page = [line for line in lines if line.get("page") == 1 and _clean(line.get("text"))]
    if not page:
        return None
    usable = [line for line in page if len(line.get("bbox", ())) == 4 and line.get("font_size", 0) > 0]
    if not usable:
        return None
    bottom = max(line["bbox"][3] for line in usable)
    sizes: Counter[float] = Counter()
    for line in usable:
        sizes[round(line["font_size"], 1)] += len(_clean(line["text"]))
    body_size = sizes.most_common(1)[0][0]
    candidates = [
        line for line in usable
        if line["bbox"][1] < bottom * 0.45
        and line["bbox"][3] - line["bbox"][1] <= 2.5 * line["font_size"]
        and not _FURNITURE.search(_clean(line["text"]))
        and not is_bad_title(line["text"])
        and line["font_size"] >= body_size * 1.12
    ]
    if not candidates:
        return None
    largest = max(line["font_size"] for line in candidates)
    prominent = sorted(
        (line for line in candidates if line["font_size"] >= largest * 0.95),
        key=lambda line: (line["bbox"][1], line["bbox"][0]),
    )
    selected = [prominent[0]]
    for line in prominent[1:]:
        previous = selected[-1]
        gap = line["bbox"][1] - previous["bbox"][3]
        if gap < -largest * 0.25 or gap > largest * 1.5:
            break
        selected.append(line)
    title = " ".join(_clean(line["text"]) for line in selected)
    if is_bad_title(title) or len(title.split()) < 3 or len(title.split()) > 40:
        return None
    return title
