"""Link retained PDF geometry to existing recordings without regenerating text or audio."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading
from typing import Any

from .word_geometry import aligned_words

import pymupdf


_MAP_LOCK = threading.Lock()
_MAP_VERSION = 2


class ReadAlongUnavailable(RuntimeError):
    """Explain missing archival metadata without pretending a guessed location is exact."""


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ReadAlongUnavailable(f"Read-along metadata is missing or unreadable: {path.name}.") from error
    if not isinstance(value, dict):
        raise ReadAlongUnavailable(f"Read-along metadata has an unsupported format: {path.name}.")
    return value


def _time(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return round(float(value), 3)
    return None


def read_along_map(root: Path) -> dict[str, Any]:
    """Backfill a cached sidecar from immutable line ids and global manifest timings."""
    with _MAP_LOCK:
        names = ("input.pdf", "extracted.json", "content.json", "manifest.json")
        try:
            signature = [(name, (root / name).stat().st_size, (root / name).stat().st_mtime_ns) for name in names]
        except OSError as error:
            raise ReadAlongUnavailable(
                "This recording lacks its original PDF or paragraph-location metadata. "
                "Existing audio is unchanged; a location map cannot be recovered automatically."
            ) from error
        revision = hashlib.sha256(json.dumps([_MAP_VERSION, signature]).encode()).hexdigest()
        cache = root / "read-along.json"
        try:
            cached = json.loads(cache.read_text())
            if isinstance(cached, dict) and cached.get("revision") == revision:
                return cached
        except (OSError, ValueError):
            pass

        extracted = _load(root / "extracted.json")
        content = _load(root / "content.json")
        manifest = _load(root / "manifest.json")
        lines = {line["id"]: line for line in extracted.get("lines", []) if isinstance(line, dict) and "id" in line}
        sources = {
            section["paragraph_id"]: (section, chapter)
            for chapter in content.get("chapters", [])
            for section in chapter.get("sections", [])
            if section.get("paragraph_id")
        }
        paragraphs = []
        unmapped = 0
        page_words: dict[int, list[Any]] = {}
        with pymupdf.open(root / "input.pdf") as document:
            def anchors_for(line_ids: list[int]) -> list[dict[str, Any]]:
                anchors: dict[tuple[int, str], dict[str, Any]] = {}
                for line_id in line_ids:
                    line = lines.get(line_id, {})
                    page_number, bbox = line.get("page"), line.get("bbox")
                    if type(page_number) is not int or not 1 <= page_number <= len(document):
                        continue
                    if not isinstance(bbox, list) or len(bbox) != 4:
                        continue
                    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in bbox):
                        continue
                    page = document[page_number - 1]
                    rect = pymupdf.Rect(bbox) * ~page.transformation_matrix
                    if rect.is_empty or rect.is_infinite:
                        continue
                    key = (page_number, str(line.get("block", page_number)))
                    anchor = anchors.setdefault(key, {"page": page_number, "rects": []})
                    anchor["rects"].append([round(rect.x0, 3), round(rect.y0, 3),
                                            round(rect.width, 3), round(rect.height, 3)])
                return list(anchors.values())

            def heading_ids(source: dict[str, Any], chapter: dict[str, Any]) -> list[int]:
                ids = chapter.get("source_heading_line_ids") or []
                if ids:
                    return ids
                # Legacy headings can be recovered locally; this never transcribes old audio.
                body_ids = source.get("source_line_ids") or []
                text = chapter.get("source_heading")
                if not body_ids or not isinstance(text, str) or not text.strip():
                    return []
                normalize = lambda value: " ".join(value.split()).casefold()
                expected = normalize(text)
                first = min(body_ids)
                page = lines.get(first, {}).get("page", 1)
                candidates = sorted(number for number, line in lines.items()
                                    if number < first and page - 1 <= line.get("page", 0) <= page)
                for end in range(len(candidates) - 1, -1, -1):
                    for count in range(1, min(6, end + 1) + 1):
                        window = candidates[end - count + 1:end + 1]
                        if normalize(" ".join(lines[number].get("text", "") for number in window)) == expected:
                            return window
                return []

            for chapter in manifest.get("chapters", []):
                heading_added = False
                for section in chapter.get("sections", []):
                    paragraph_id = section.get("paragraph_id")
                    source, source_chapter = sources.get(paragraph_id, ({}, {}))
                    start = _time(section.get("source_start"))
                    if start is None:
                        start = next((_time(segment.get("start")) for segment in section.get("audio_segments", [])
                                      if segment.get("speaker") == "source" and _time(segment.get("start")) is not None), None)
                    if not paragraph_id or start is None:
                        unmapped += 1
                        continue
                    timings = section.get("word_timings") or []
                    if not heading_added:
                        segment = next((item for item in section.get("audio_segments", [])
                                        if item.get("kind") == "chapter_title"), None)
                        if segment:
                            ids = heading_ids(source, source_chapter)
                            heading_anchors = anchors_for(ids)
                            heading_start, heading_end = _time(segment.get("start")), _time(segment.get("end"))
                            if heading_anchors and heading_start is not None and heading_end is not None and heading_end > heading_start:
                                paragraphs.append({
                                    "id": "heading-" + str(paragraph_id), "kind": "heading",
                                    "title": chapter.get("title") or source_chapter.get("title") or "Section",
                                    "chapter": chapter.get("title") or "",
                                    "start": heading_start, "summary_start": None, "end": heading_end,
                                    "anchors": heading_anchors,
                                    "words": aligned_words(document, lines, ids,
                                        [word for word in timings if word.get("kind") == "chapter_title"], page_words),
                                })
                                heading_added = True
                    ids = source.get("source_line_ids") or []
                    anchors = anchors_for(ids)
                    if not anchors:
                        unmapped += 1
                        continue
                    paragraphs.append({
                        "id": paragraph_id, "kind": "paragraph",
                        "title": section.get("title") or "Paragraph",
                        "chapter": chapter.get("title") or "",
                        "start": start,
                        "summary_start": _time(section.get("summary_start")),
                        "end": _time(section.get("source_end")) or _time(section.get("end")),
                        "anchors": anchors,
                        "words": aligned_words(document, lines, ids,
                            [word for word in timings if word.get("speaker") == "source"], page_words),
                    })
            page_count = len(document)
        if not paragraphs:
            raise ReadAlongUnavailable(
                "No reliable paragraph locations are available for this recording. "
                "The PDF and existing audio have not been modified."
            )
        result = {
            # Keep the wire version stable: older installed readers reject version 2.
            # The cache revision still advances when optional mapping features change.
            "version": 1,
            "features": {"heading_anchors": True, "word_timing_version": 1},
            "revision": revision,
            "coordinate_space": "pdf_user_space",
            "job_id": root.name,
            "title": manifest.get("title", ""),
            "page_count": page_count,
            "paragraphs": paragraphs,
            "unmapped_paragraphs": unmapped,
            "pdf_path": f"/api/audio/jobs/{root.name}/pdf",
        }
        fd, temporary = tempfile.mkstemp(prefix=".read-along-", suffix=".json", dir=root)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(result, handle, ensure_ascii=False)
            os.replace(temporary, cache)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return result
