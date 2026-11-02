"""Protect real publication titles and reject the identifiers found in persisted jobs."""

from copy import deepcopy
import unittest

from pdf2podcast.titles import first_page_title, is_bad_title, select_document_title


INTERPLM = "INTERPLM: DISCOVERING INTERPRETABLE FEATURES IN PROTEIN LANGUAGE MODELS VIA SPARSE AUTOENCODERS"
SUBSPACE = "Is This the Subspace You Are Looking for? An Interpretability Illusion for Subspace Activation Patching"
RHD = "AlphaGenome-enabled analysis of non-coding regulatory variants underlying RHD expression with wet-lab validation"


def line(text, y, size=10, *, page=1):
    return {"text": text, "page": page, "font_size": size, "bbox": [70, y, 540, y + size]}


class PublicationTitles(unittest.TestCase):
    def test_identifiers_found_in_jobs_and_common_upload_names_are_not_titles(self):
        for bad in (
            "46534747", "2412.12101v1", "2311.17030v2", "s41586-024-07487-w",
            "s41586-025-10014-0", "NICITCv2.pdf", "contract-test.pdf", "2412.12101v1.pdf",
            "arXiv:2412.12101v1", "hep-th/9901001v2", "10.1038/s41586-024-07487-w",
            "10.1038_s41586-024-07487-w", "10.1038%2Fs41586-024-07487-w",
            "nihpp-2026.01.21.700828v2", "2025.02.06.636901v2.full",
            "550e8400-e29b-41d4-a716-446655440000", "pdf-6f48d56feee74a4a",
            "https://arxiv.org/pdf/2412.12101", "Untitled", "Microsoft Word - draft",
            "biology-15-01859", "input.pdf", "", None, 46534747,
        ):
            with self.subTest(bad=bad):
                self.assertTrue(is_bad_title(bad))

    def test_real_paper_names_acronyms_and_capitalization_survive(self):
        for title in (INTERPLM, SUBSPACE, RHD, "Is the cell really a machine?",
                      "Accurate structure prediction of biomolecular interactions with AlphaFold 3",
                      "Interpretable Missense Variant Effect Prediction via Sparse Feature Changes in ESM-2",
                      "ZeRO: Memory Optimizations Toward Training Trillion Parameter Models"):
            with self.subTest(title=title):
                self.assertFalse(is_bad_title(title))
                self.assertEqual(select_document_title(model_title=title, pdf_title="2412.12101v1"), title)

    def test_model_then_pdf_then_heading_then_existing_title_supply_evidence(self):
        self.assertEqual(select_document_title(model_title=INTERPLM, pdf_title=SUBSPACE), INTERPLM)
        self.assertEqual(select_document_title(model_title="46534747", pdf_title=SUBSPACE), SUBSPACE)
        self.assertEqual(select_document_title(pdf_title="46534747", first_page_title=RHD), RHD)
        self.assertEqual(select_document_title(current_title=INTERPLM, filename="2412.12101v1.pdf"), INTERPLM)
        self.assertEqual(select_document_title(filename="Invented Descriptive Title.pdf"), "Untitled document")
        self.assertEqual(select_document_title(pdf_title="NICITCv2", filename="NICITCv2.pdf"), "Untitled document")

    def test_metadata_filename_does_not_override_a_source_heading(self):
        self.assertEqual(select_document_title(pdf_title="2412.12101v1", first_page_title=INTERPLM), INTERPLM)
        self.assertEqual(select_document_title(pdf_title="literal-file-name", filename="literal-file-name.pdf",
                                               first_page_title=SUBSPACE), SUBSPACE)

    def test_split_title_blocks_beat_a_larger_vertical_arxiv_stamp(self):
        lines = [line("Is This the Subspace You Are Looking for? An Interpretability", 114, 17.18),
                 line("Illusion for Subspace Activation Patching", 136, 17.21),
                 line("Aleksandar Makelov", 177, 11.95), line("Abstract", 280, 12),
                 line("Scientific body prose. " * 30, 310), line("More body prose. " * 30, 700),
                 {"text": "arXiv:2311.17030v2 [cs.LG] 6 Dec 2023", "page": 1,
                  "font_size": 20, "bbox": [10, 190, 37, 575]}]
        original = deepcopy(lines)
        self.assertEqual(first_page_title(lines), SUBSPACE)
        self.assertEqual(lines, original)

    def test_body_prose_and_later_page_headings_are_not_guessed_as_titles(self):
        self.assertIsNone(first_page_title([line("Some ordinary prose to read.", 100),
                                           line("More ordinary prose to read.", 600),
                                           line("A different paper title", 50, 24, page=2)]))
        self.assertIsNone(first_page_title([]))


if __name__ == "__main__":
    unittest.main()
