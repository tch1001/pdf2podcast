"""Keep title improvements independent of paragraph contents and saved model work."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import pymupdf

from pdf2podcast.vision_paragraphs import (
    LAYOUT_RECOVERY_PROMPT, VISION_PIPELINE, _LEGACY_VISION_PROMPT,
    _annotate_page, _assemble, extract_visual_document,
)


TITLE = "InterPLM: Discovering Interpretable Features in Protein Language Models via Sparse Autoencoders"


def source_line(number, text, *, page=1):
    return {"id": number, "page": page, "text": text, "reading_text": text,
            "bbox": [50, number * 25, 500, number * 25 + 10], "font_size": 10}


def group(kind, ids):
    return {"kind": kind, "line_ids": ids, "topic": "Protein language models",
            "continues_previous": False, "level": 1}


class VisualPublicationTitles(unittest.TestCase):
    def test_visual_title_replaces_filename_without_changing_paragraphs(self):
        lines = [source_line(1, "InterPLM: Discovering Interpretable Features"),
                 source_line(2, "in Protein Language Models via Sparse Autoencoders"),
                 source_line(3, "Protein language models learn rich representations of proteins.")]
        plans = [{"groups": [group("document_title", [1]), group("document_title", [2]),
                              group("paragraph", [3])]}]
        document = {"title": "2412.12101v1", "lines": lines, "omitted": []}
        original_lines = deepcopy(lines)
        chapters, omissions = _assemble(document, plans)
        self.assertEqual(document["title"], TITLE)
        self.assertEqual(lines, original_lines)
        section = chapters[0]["sections"][0]
        self.assertEqual(section["raw_source_text"], lines[2]["text"])
        self.assertEqual(section["source_line_ids"], [3])
        self.assertEqual(section["page_start"], 1)
        self.assertEqual(section["page_end"], 1)
        good_document = {"title": TITLE, "lines": deepcopy(lines), "omitted": []}
        expected, expected_omissions = _assemble(good_document, deepcopy(plans))
        self.assertEqual(chapters, expected)
        self.assertEqual(omissions, expected_omissions)

    def test_identifier_and_later_page_title_do_not_replace_good_metadata(self):
        lines = [source_line(1, "2412.12101v1"), source_line(2, "Readable scientific prose continues here."),
                 source_line(3, "A different supplementary paper title", page=2)]
        document = {"title": TITLE, "lines": lines}
        plans = [{"groups": [group("document_title", [1]), group("paragraph", [2]),
                              group("document_title", [3])]}]
        _assemble(document, plans)
        self.assertEqual(document["title"], TITLE)

    def test_pdf_metadata_identifier_yields_to_printed_title_before_model_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.pdf"
            with pymupdf.open() as pdf:
                page = pdf.new_page()
                page.insert_text((50, 80), "InterPLM: Discovering Interpretable Features", fontsize=17)
                page.insert_text((50, 102), "in Protein Language Models via Sparse Autoencoders", fontsize=17)
                for y in range(170, 700, 20):
                    page.insert_text((50, y), "Protein language models learn rich representations from protein sequences.", fontsize=10)
                pdf.set_metadata({"title": "2412.12101v1"})
                pdf.save(path)
            document = extract_visual_document(path, "2412.12101v1.pdf")
            self.assertEqual(document["title"], TITLE)
            self.assertEqual(document["pdf_title"], "2412.12101v1")
            self.assertEqual(document["page_count"], 1)

    def test_title_prompt_change_reuses_both_existing_paragraph_cache_formats(self):
        lines = [source_line(1, "Readable scientific prose continues here.")]
        groups = [group("paragraph", [1])]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "visual-paragraphs").mkdir()
            for options in ({}, {"output_format": LAYOUT_RECOVERY_PROMPT}):
                base = {"version": VISION_PIPELINE, "prompt": _LEGACY_VISION_PROMPT,
                        "model": "test-model", "lines": lines, **options}
                fingerprint = hashlib.sha256(json.dumps(base, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
                (root / "visual-paragraphs" / "00001.json").write_text(
                    json.dumps({"fingerprint": fingerprint, "groups": groups}))
                def no_model_call(_):
                    self.fail("Changing title guidance must not rerun checked paragraph extraction")
                self.assertEqual(_annotate_page(root, 1, lines, {}, "test-model", no_model_call), groups)


if __name__ == "__main__":
    unittest.main()
