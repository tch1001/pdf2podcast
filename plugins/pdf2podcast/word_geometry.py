"""Match real speech-boundary events to printed words without inventing timing or geometry."""

from __future__ import annotations

from difflib import SequenceMatcher
import math
import re
from typing import Any
import unicodedata

import pymupdf

from .speech_text import pronunciation_text


def _tokens(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", pronunciation_text(text)).casefold()
    return re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)


def _seconds(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def aligned_words(
    document: Any, lines: dict[int, dict[str, Any]], line_ids: list[int],
    timings: list[dict[str, Any]], page_words: dict[int, list[Any]],
) -> list[dict[str, Any]]:
    """Use monotonic exact token matches; unmatched notation keeps paragraph-only highlighting."""
    if not timings:
        return []
    printed: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for line_id in line_ids:
        line = lines.get(line_id, {})
        number, box = line.get("page"), line.get("bbox")
        if type(number) is not int or not 1 <= number <= len(document) or not isinstance(box, list) or len(box) != 4:
            continue
        if not all(isinstance(value, (float, int)) and math.isfinite(value) for value in box):
            continue
        page = document[number - 1]
        if number not in page_words:
            page_words[number] = page.get_text("words", flags=pymupdf.TEXTFLAGS_TEXT)
        line_rect = pymupdf.Rect(box)
        for index, word in enumerate(page_words[number]):
            key = (number, index)
            rect = pymupdf.Rect(word[:4])
            if key in seen or rect.is_empty or (rect & line_rect).get_area() < rect.get_area() * .6:
                continue
            native = rect * ~page.transformation_matrix
            if native.is_empty or native.is_infinite:
                continue
            seen.add(key)
            printed.append({
                "text": str(word[4]), "line": line_id,
                "anchor": {"page": number, "rects": [[round(native.x0, 3), round(native.y0, 3),
                                                     round(native.width, 3), round(native.height, 3)]]},
            })
    source_tokens: list[str] = []
    source_owners: list[tuple[int, ...]] = []
    index = 0
    while index < len(printed):
        item = printed[index]
        text, owners = item["text"], (index,)
        if (text.endswith("-") and index + 1 < len(printed)
                and item["line"] != printed[index + 1]["line"]
                and printed[index + 1]["text"][:1].islower()):
            text = text[:-1] + printed[index + 1]["text"]
            owners = (index, index + 1)
            index += 1
        tokens = _tokens(text)
        source_tokens.extend(tokens)
        source_owners.extend([owners] * len(tokens))
        index += 1

    valid: list[dict[str, Any]] = []
    spoken_tokens: list[str] = []
    spoken_owners: list[int] = []
    for item in timings:
        if not isinstance(item, dict):
            continue
        start, end = _seconds(item.get("start")), _seconds(item.get("end"))
        text = item.get("text")
        if start is None or end is None or end <= start or not isinstance(text, str):
            continue
        tokens = _tokens(text)
        if not tokens:
            continue
        owner = len(valid)
        valid.append({"text": text, "start": round(start, 3), "end": round(end, 3), "tokens": len(tokens)})
        spoken_tokens.extend(tokens)
        spoken_owners.extend([owner] * len(tokens))
    matches: dict[int, list[tuple[int, ...]]] = {}
    for match in SequenceMatcher(None, source_tokens, spoken_tokens, autojunk=False).get_matching_blocks():
        for offset in range(match.size):
            owner = spoken_owners[match.b + offset]
            matches.setdefault(owner, []).append(source_owners[match.a + offset])
    result = []
    for index, item in enumerate(valid):
        matched = matches.get(index, [])
        if len(matched) != item["tokens"]:
            continue
        owners = sorted({owner for group in matched for owner in group})
        result.append({key: value for key, value in item.items() if key != "tokens"} | {
            "anchors": [printed[owner]["anchor"] for owner in owners],
        })
    return result
