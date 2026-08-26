from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest

import fitz
from PIL import Image

from support import load_pipeline


def png_payload(size: tuple[int, int], color: str) -> bytes:
    image = Image.new("RGB", size, color)
    payload = io.BytesIO()
    image.save(payload, format="PNG")
    return payload.getvalue()


class LayoutHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pipeline = load_pipeline(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_embedded_large_facsimile_moves_to_image_variant(self):
        source = self.root / "source.pdf"
        with fitz.open() as document:
            facsimile_page = document.new_page(width=300, height=400)
            facsimile_page.insert_image(
                facsimile_page.rect,
                stream=png_payload((1200, 1600), "white"),
            )
            facsimile_page.insert_image(
                fitz.Rect(35, 40, 265, 350),
                stream=png_payload((600, 900), "gray"),
            )

            ordinary_page = document.new_page(width=300, height=400)
            ordinary_page.insert_image(
                ordinary_page.rect,
                stream=png_payload((1200, 1600), "white"),
            )
            ordinary_page.insert_image(
                fitz.Rect(20, 20, 40, 40),
                stream=png_payload((64, 64), "black"),
            )
            document.save(source)

        page_results = {
            0: {"cells": [{"category": "Text", "text": "bad facsimile OCR"}]},
            1: {"cells": [{"category": "Text", "text": "ordinary page text"}]},
        }
        updated = self.pipeline.degrade_source_facsimile_pages_to_images(
            source,
            page_results,
            self.root / "work",
        )

        self.assertTrue(updated[0]["source_facsimile_detected"])
        self.assertEqual(updated[0]["image_fallback_kind"], "source_facsimile")
        self.assertEqual(updated[0]["source_facsimile_large_raster_count"], 2)
        self.assertEqual(updated[0]["cells"], [])
        self.assertTrue(Path(updated[0]["image_fallback_path"]).is_file())
        self.assertNotIn("image_fallback_path", updated[1])

    def test_auxiliary_color_logo_moves_page_to_image_variant(self):
        source = self.root / "color-logo.pdf"
        with fitz.open() as document:
            page = document.new_page(width=300, height=400)
            page.insert_image(
                page.rect,
                stream=png_payload((1200, 1600), "white"),
            )
            page.insert_image(
                fitz.Rect(115, 35, 185, 105),
                stream=png_payload((70, 70), "red"),
            )
            document.save(source)

        updated = self.pipeline.degrade_source_facsimile_pages_to_images(
            source,
            {
                0: {
                    "cells": [
                        {
                            "category": "Text",
                            "text": "Publisher title without its colored logo",
                            "bbox": [40, 130, 260, 180],
                            "__bbox_units": "pdf",
                        }
                    ]
                }
            },
            self.root / "work",
        )

        self.assertEqual(updated[0]["source_facsimile_large_raster_count"], 1)
        self.assertEqual(updated[0]["source_auxiliary_color_raster_count"], 1)
        self.assertTrue(updated[0]["image_fallback_page"])

    def test_overlapping_duplicate_tail_is_merged_but_margin_number_is_kept(self):
        shared_tail = "Wenn sie das Grab und das Kreuz drüber,"
        blocks = [
            {
                "left": 70.0,
                "top": 250.0,
                "width": 300.0,
                "height": 305.0,
                "category": "Text",
                "text": "Vorheriger Absatz. " + shared_tail,
                "source_text": "Vorheriger Absatz. " + shared_tail,
                "order": 1,
            },
            {
                "left": 390.0,
                "top": 535.0,
                "width": 12.0,
                "height": 15.0,
                "category": "Text",
                "text": "40",
                "source_text": "40",
                "order": 2,
            },
            {
                "left": 130.0,
                "top": 540.0,
                "width": 180.0,
                "height": 25.0,
                "category": "Text",
                "text": shared_tail + " im Blitze sehen!",
                "source_text": shared_tail + " im Blitze sehen!",
                "order": 3,
            },
        ]

        merged = self.pipeline.merge_overlapping_duplicate_ocr_blocks(blocks)
        self.assertEqual(len(merged), 2)
        body = next(block for block in merged if block["text"] != "40")
        self.assertEqual(body["text"].count(shared_tail), 1)
        self.assertIn("im Blitze sehen!", body["text"])
        self.assertTrue(body["duplicate_ocr_blocks_merged"])
        self.assertTrue(any(block["text"] == "40" for block in merged))

    def test_nonduplicate_overlapping_blocks_remain_separate(self):
        blocks = [
            {
                "left": 60.0,
                "top": 100.0,
                "width": 200.0,
                "height": 80.0,
                "category": "Text",
                "text": "A sufficiently long first paragraph with unique content.",
                "source_text": "A sufficiently long first paragraph with unique content.",
                "order": 1,
            },
            {
                "left": 70.0,
                "top": 160.0,
                "width": 200.0,
                "height": 80.0,
                "category": "Text",
                "text": "A different long paragraph that happens to overlap geometrically.",
                "source_text": "A different long paragraph that happens to overlap geometrically.",
                "order": 2,
            },
        ]
        self.assertEqual(
            len(self.pipeline.merge_overlapping_duplicate_ocr_blocks(blocks)),
            2,
        )

    def test_fuzzy_duplicate_join_does_not_leave_doubled_punctuation(self):
        base = "A sentence ending with Die Schmeichelei'n, mein Herr, will ich verzeihn."
        addition = "Die Schmeichelei'n, mein Herr, will ich verzeihn, Ihr wißt es."
        merged = self.pipeline.append_text_with_overlap(base, addition)
        self.assertIn("verzeihn, Ihr", merged)
        self.assertNotIn(". ,", merged)

    def test_dense_numeric_reference_list_is_marked_as_complex_layout(self):
        text = "\n".join(f"{index}.1 Variant {index}" for index in range(1, 31))
        result = {
            "cells": [
                {
                    "category": "Text",
                    "text": text,
                    "bbox": [25.0, 40.0, 275.0, 360.0],
                    "__bbox_units": "pdf",
                }
            ]
        }

        reasons = self.pipeline.complex_layout_fallback_reasons(
            result, 300.0, 400.0
        )
        self.assertIn("flattened_reference_list", {reason["kind"] for reason in reasons})

        ordinary_poem = {
            "cells": [
                {
                    "category": "Text",
                    "text": "\n".join(
                        f"A distinct poetic line {'a' * (index + 1)}"
                        for index in range(30)
                    ),
                    "bbox": [55.0, 40.0, 245.0, 360.0],
                    "__bbox_units": "pdf",
                }
            ]
        }
        self.assertEqual(
            self.pipeline.complex_layout_fallback_reasons(
                ordinary_poem, 300.0, 400.0
            ),
            [],
        )

    def test_contents_heading_requires_source_layout_fallback(self):
        result = {
            "cells": [
                {
                    "category": "Page-header",
                    "text": "Inhalt",
                    "bbox": [130.0, 20.0, 170.0, 35.0],
                    "__bbox_units": "pdf",
                },
                {
                    "category": "Text",
                    "text": "Chapter one Chapter two",
                    "bbox": [30.0, 60.0, 270.0, 360.0],
                    "__bbox_units": "pdf",
                },
            ]
        }
        reasons = self.pipeline.complex_layout_fallback_reasons(
            result, 300.0, 400.0
        )
        self.assertIn("contents_page_topology", {reason["kind"] for reason in reasons})

    def test_severe_source_text_loss_moves_page_to_image_variant(self):
        source = self.root / "source-text-loss.pdf"
        with fitz.open() as document:
            page = document.new_page(width=300, height=400)
            page.insert_textbox(
                fitz.Rect(30, 30, 270, 370),
                "Complete source paragraph with many words and references. " * 12,
                fontsize=9,
            )
            document.save(source)
        updated = self.pipeline.degrade_complex_layout_pages_to_images(
            source,
            {
                0: {
                    "cells": [
                        {
                            "category": "Text",
                            "text": "59",
                            "bbox": [30, 50, 270, 350],
                            "__bbox_units": "pdf",
                        }
                    ]
                }
            },
            self.root / "work",
        )
        kinds = {
            reason["kind"] for reason in updated[0]["complex_layout_fallback_reasons"]
        }
        self.assertIn("severe_source_text_loss", kinds)
        self.assertTrue(updated[0]["image_fallback_page"])

    def test_unrepresented_source_graphic_in_ocr_gap_requires_fallback(self):
        source = self.root / "source-graphic-gap.pdf"
        with fitz.open() as document:
            page = document.new_page(width=300, height=400)
            page.insert_text((35, 45), "Top source paragraph", fontsize=10)
            page.insert_text((35, 365), "Bottom source paragraph", fontsize=10)
            for x in range(55, 251, 25):
                page.draw_line((x, 100), (x, 300), color=(0, 0, 0), width=2)
            for y in range(100, 301, 25):
                page.draw_line((55, y), (250, y), color=(0, 0, 0), width=2)
            document.save(source)
        result = {
            "cells": [
                {
                    "category": "Text",
                    "text": "Top source paragraph",
                    "bbox": [30, 25, 270, 55],
                    "__bbox_units": "pdf",
                },
                {
                    "category": "Text",
                    "text": "Bottom source paragraph",
                    "bbox": [30, 340, 270, 375],
                    "__bbox_units": "pdf",
                },
            ]
        }
        updated = self.pipeline.degrade_complex_layout_pages_to_images(
            source, {0: result}, self.root / "work"
        )
        kinds = {
            reason["kind"] for reason in updated[0]["complex_layout_fallback_reasons"]
        }
        self.assertIn("unrepresented_source_graphic", kinds)

    def test_extreme_monotonic_number_expansion_is_not_treated_as_a_real_list(self):
        hallucination = "Reference pages " + "–".join(str(value) for value in range(291, 391))
        self.assertEqual(
            self.pipeline.longest_monotonic_number_run(hallucination),
            100,
        )
        self.assertLess(
            self.pipeline.longest_monotonic_number_run(
                "Entries 291 292 328 330–335 340–345 348 437"
            ),
            64,
        )

    def test_fuzzy_overlapping_edge_line_keeps_short_corrected_twin(self):
        blocks = [
            {
                "left": 100.0,
                "top": 100.0,
                "width": 140.0,
                "height": 25.0,
                "category": "Text",
                "text": "Alles würd' ich wagen,",
                "source_text": "Alles würd' ich wagen,",
                "order": 1,
            },
            {
                "left": 100.0,
                "top": 115.0,
                "width": 220.0,
                "height": 100.0,
                "category": "Text",
                "text": "Aries Wind Ten Wagen,\nMit Wind und Welle mich schlagen,\nUnd es siegte der Muth,",
                "source_text": "Aries Wind Ten Wagen,\nMit Wind und Welle mich schlagen,\nUnd es siegte der Muth,",
                "order": 2,
            },
        ]

        repaired = self.pipeline.repair_overlapping_fuzzy_duplicate_ocr_lines(blocks)
        text = "\n".join(block["text"] for block in repaired)
        self.assertIn("Alles würd' ich wagen,", text)
        self.assertNotIn("Aries Wind Ten Wagen", text)
        self.assertEqual(
            sum(block.get("fuzzy_duplicate_ocr_lines_removed", 0) for block in repaired),
            1,
        )

    def test_fuzzy_suffix_duplicate_handles_superscript_and_punctuation_variants(self):
        blocks = [
            {
                "left": 50.0,
                "top": 100.0,
                "width": 240.0,
                "height": 100.0,
                "category": "Text",
                "text": (
                    "A long explanatory paragraph. "
                    "Erstveröffentlichung: MEGA¹ I/1.2. S. 175–176."
                ),
                "source_text": "",
                "order": 1,
            },
            {
                "left": 55.0,
                "top": 190.0,
                "width": 180.0,
                "height": 20.0,
                "category": "Text",
                "text": "Erstveröffentlichung: MEGA 1 I/1,2, S. 175–176.",
                "source_text": "",
                "order": 2,
            },
        ]

        repaired = self.pipeline.repair_overlapping_fuzzy_duplicate_ocr_lines(blocks)
        combined = "\n".join(block["text"] for block in repaired)
        self.assertEqual(combined.count("Erstveröffentlichung"), 1)
        self.assertIn("A long explanatory paragraph.", combined)

    def test_section_heading_duplicate_is_removed_from_following_text_block(self):
        blocks = [
            {
                "left": 120.0,
                "top": 100.0,
                "width": 120.0,
                "height": 18.0,
                "category": "Section-header",
                "text": "Lebe wohl, Streiter!",
                "source_text": "Lebe wohl, Streiter!",
                "order": 1,
            },
            {
                "left": 100.0,
                "top": 110.0,
                "width": 180.0,
                "height": 85.0,
                "category": "Text",
                "text": "Lebe wohl, Streiter!\nErzähle den Geistern des Himmels,\nDie nie gestorben sind,",
                "source_text": "",
                "order": 2,
            },
        ]

        repaired = self.pipeline.repair_overlapping_fuzzy_duplicate_ocr_lines(blocks)
        combined = "\n".join(block["text"] for block in repaired)
        self.assertEqual(combined.count("Lebe wohl, Streiter!"), 1)
        self.assertIn("Erzähle den Geistern", combined)

    def test_small_adjacent_overlap_is_trimmed_without_dropping_either_block(self):
        blocks = [
            {
                "left": 50.0,
                "top": 100.0,
                "width": 250.0,
                "height": 75.0,
                "category": "Text",
                "text": "Several distinct lines\nthat must remain in the earlier block.",
                "order": 1,
            },
            {
                "left": 52.0,
                "top": 171.0,
                "width": 220.0,
                "height": 18.0,
                "category": "Text",
                "text": "A distinct following line that must also remain.",
                "order": 2,
            },
        ]

        repaired = self.pipeline.trim_small_adjacent_text_block_overlaps(blocks)
        self.assertEqual(len(repaired), 2)
        self.assertEqual(repaired[0]["height"], 71.0)
        self.assertTrue(repaired[0]["adjacent_text_overlap_trimmed"])

    def test_overlapping_margin_references_get_their_own_column(self):
        blocks = [
            {
                "left": 55.0,
                "top": 550.0,
                "width": 42.0,
                "height": 48.0,
                "category": "Text",
                "text": "70.6-9\n70.12-16\n(912.1-6)",
                "source_text": "",
                "order": 1,
            },
            {
                "left": 56.0,
                "top": 550.0,
                "width": 230.0,
                "height": 18.0,
                "category": "Text",
                "text": "70.6—9 Diese Arbeit hat Marx nicht ausgeführt.",
                "source_text": "",
                "order": 2,
            },
            {
                "left": 56.0,
                "top": 570.0,
                "width": 310.0,
                "height": 18.0,
                "category": "Text",
                "text": "70.12–16 Vgl. Hefte zur epikureischen Philosophie.",
                "source_text": "",
                "order": 3,
            },
        ]

        repaired = self.pipeline.repair_overlapping_margin_reference_blocks(
            blocks, 450.0
        )
        bodies = [block for block in repaired if block.get("margin_reference_block_repaired")]
        self.assertEqual(len(bodies), 2)
        self.assertTrue(all(block["left"] > 100.0 for block in bodies))
        self.assertEqual(
            {block["text"] for block in bodies},
            {
                "Diese Arbeit hat Marx nicht ausgeführt.",
                "Vgl. Hefte zur epikureischen Philosophie.",
            },
        )

    def test_partial_reference_prefix_captured_in_body_is_removed(self):
        self.assertEqual(
            self.pipeline.strip_captured_reference_prefix(
                "5.25 Explanatory note", {"125.25"}
            ),
            "Explanatory note",
        )
        self.assertEqual(
            self.pipeline.strip_captured_reference_prefix(
                ".18–19 Another note", {"240.18-19"}
            ),
            "Another note",
        )

    def test_stretched_margin_number_run_is_restored_from_source_geometry(self):
        source = self.root / "line-numbers.pdf"
        with fitz.open() as document:
            page = document.new_page(width=300, height=400)
            page.insert_text((35, 105), "10", fontsize=10)
            page.insert_text((35, 155), "15", fontsize=10)
            page.insert_text((35, 205), "20", fontsize=10)
            document.save(source)
        blocks = [
            {
                "left": 35.0,
                "top": 100.0,
                "width": 12.0,
                "height": 230.0,
                "category": "Text",
                "text": "10\n15\n20",
                "order": 1,
            },
            {
                "left": 100.0,
                "top": 100.0,
                "width": 180.0,
                "height": 80.0,
                "category": "Text",
                "text": "Body text that must remain.",
                "order": 2,
            },
        ]
        with fitz.open(source) as document:
            repaired = self.pipeline.repair_stretched_margin_numeric_ocr_blocks(
                blocks, 300.0, 400.0, source_page=document[0]
            )
        self.assertEqual(
            sorted(block["text"] for block in repaired),
            ["10", "15", "20", "Body text that must remain."],
        )
        restored = [
            block for block in repaired if block.get("stretched_margin_numeric_block_repaired")
        ]
        self.assertEqual(len(restored), 3)
        self.assertTrue(all(block["height"] < 15.0 for block in restored))

    def test_space_flattened_stretched_margin_numbers_are_restored(self):
        source = self.root / "space-flattened-line-numbers.pdf"
        with fitz.open() as document:
            page = document.new_page(width=300, height=400)
            page.insert_text((35, 155), "15", fontsize=10)
            page.insert_text((35, 205), "20", fontsize=10)
            document.save(source)
        blocks = [
            {
                "left": 35.0,
                "top": 100.0,
                "width": 12.0,
                "height": 230.0,
                "category": "Text",
                "text": "15 20",
                "order": 1,
            }
        ]
        with fitz.open(source) as document:
            repaired = self.pipeline.repair_stretched_margin_numeric_ocr_blocks(
                blocks, 300.0, 400.0, source_page=document[0]
            )
        self.assertEqual([block["text"] for block in repaired], ["15", "20"])
        self.assertTrue(
            all(block.get("stretched_margin_numeric_block_repaired") for block in repaired)
        )

    def test_duplicated_stretched_margin_numbers_are_restored_once(self):
        source = self.root / "duplicated-line-numbers.pdf"
        with fitz.open() as document:
            page = document.new_page(width=300, height=400)
            page.insert_text((35, 155), "20", fontsize=10)
            page.insert_text((35, 205), "25", fontsize=10)
            page.insert_text((35, 255), "30", fontsize=10)
            document.save(source)
        blocks = [
            {
                "left": 35.0,
                "top": 100.0,
                "width": 12.0,
                "height": 230.0,
                "category": "Text",
                "text": "20 20\n25 25\n30 30",
                "order": 1,
            }
        ]
        with fitz.open(source) as document:
            repaired = self.pipeline.repair_stretched_margin_numeric_ocr_blocks(
                blocks, 300.0, 400.0, source_page=document[0]
            )
        self.assertEqual([block["text"] for block in repaired], ["20", "25", "30"])
        self.assertTrue(
            all(block.get("stretched_margin_numeric_block_repaired") for block in repaired)
        )

    def test_overlapping_margin_line_number_gets_separate_column_and_prefix_cleanup(self):
        blocks = [
            {
                "left": 35.0,
                "top": 330.0,
                "width": 10.0,
                "height": 10.0,
                "category": "Text",
                "text": "40",
                "order": 1,
            },
            {
                "left": 40.0,
                "top": 328.0,
                "width": 220.0,
                "height": 30.0,
                "category": "Text",
                "text": "0 LUCINDO. A distinct dramatic line follows.",
                "order": 2,
            },
        ]
        repaired = self.pipeline.repair_overlapping_margin_line_number_blocks(
            blocks, 300.0, 400.0
        )
        body = repaired[1]
        self.assertGreater(body["left"], 45.0)
        self.assertEqual(body["text"], "LUCINDO. A distinct dramatic line follows.")
        self.assertTrue(body["margin_line_number_block_repaired"])

    def test_right_margin_line_number_is_removed_from_captured_body_suffix(self):
        blocks = [
            {
                "left": 405.0,
                "top": 160.0,
                "width": 8.0,
                "height": 10.0,
                "category": "Text",
                "text": "15",
                "order": 1,
            },
            {
                "left": 333.0,
                "top": 150.0,
                "width": 81.0,
                "height": 30.0,
                "category": "Text",
                "text": "Editorial sentence. 15",
                "order": 2,
            },
        ]
        repaired = self.pipeline.repair_overlapping_margin_line_number_blocks(
            blocks, 450.0, 400.0
        )
        body = repaired[1]
        self.assertEqual(body["text"], "Editorial sentence.")
        self.assertLess(body["left"] + body["width"], blocks[0]["left"])
        self.assertTrue(body["margin_line_number_block_repaired"])

    def test_short_right_edge_text_without_captured_marker_is_untouched(self):
        blocks = [
            {
                "left": 405.0,
                "top": 160.0,
                "width": 8.0,
                "height": 10.0,
                "category": "Text",
                "text": "15",
                "order": 1,
            },
            {
                "left": 333.0,
                "top": 150.0,
                "width": 81.0,
                "height": 30.0,
                "category": "Text",
                "text": "Editorial sentence.",
                "order": 2,
            },
        ]
        original = dict(blocks[1])
        repaired = self.pipeline.repair_overlapping_margin_line_number_blocks(
            blocks, 450.0, 400.0
        )
        self.assertEqual(repaired[1], original)

    def test_overlapping_margin_line_number_is_separated_from_footnote(self):
        blocks = [
            {
                "left": 35.0,
                "top": 330.0,
                "width": 10.0,
                "height": 11.0,
                "category": "Text",
                "text": "25",
                "order": 1,
            },
            {
                "left": 41.0,
                "top": 329.0,
                "width": 220.0,
                "height": 25.0,
                "category": "Footnote",
                "text": "*) A source footnote that must not collide with the line number.",
                "order": 2,
            },
        ]
        repaired = self.pipeline.repair_overlapping_margin_line_number_blocks(
            blocks, 300.0, 400.0
        )
        footnote = repaired[1]
        self.assertGreater(footnote["left"], 45.0)
        self.assertEqual(
            footnote["text"],
            "*) A source footnote that must not collide with the line number.",
        )
        self.assertTrue(footnote["margin_line_number_block_repaired"])

    def test_inverted_source_text_direction_rotates_ocr_boxes(self):
        with fitz.open() as document:
            page = document.new_page(width=300, height=400)
            for y, text in ((80, "First inverted line"), (110, "Second inverted line"), (140, "Third inverted line")):
                page.insert_text((260, y), text, fontsize=10, rotate=180)
            self.assertEqual(
                self.pipeline.source_page_text_rotation_degrees(page),
                180,
            )

        blocks = [
            {
                "left": 25.0,
                "top": 30.0,
                "width": 100.0,
                "height": 40.0,
                "category": "Text",
                "text": "Recognized upright text",
                "line_info": [{"text": "stale absolute geometry"}],
                "order": 1,
            }
        ]
        corrected = self.pipeline.rotate_blocks_for_source_orientation(
            blocks, 300.0, 400.0, 180
        )
        self.assertEqual(corrected[0]["left"], 175.0)
        self.assertEqual(corrected[0]["top"], 330.0)
        self.assertEqual(corrected[0]["line_info"], [])
        self.assertEqual(corrected[0]["source_orientation_correction_degrees"], 180)

    def test_anchored_transliteration_duplicate_block_is_removed(self):
        blocks = [
            {
                "left": 40.0,
                "top": 100.0,
                "width": 230.0,
                "height": 80.0,
                "category": "Text",
                "text": (
                    "1D. 113. 10 σε μιαν αιτιαν τουτων αποδιδονται πλεοναχως "
                    "των φαινομένων έκκαλουμένων μανικόν και ού καθηκόντως."
                ),
                "source_text": "full reference paragraph",
                "order": 1,
            },
            {
                "left": 42.0,
                "top": 145.0,
                "width": 210.0,
                "height": 28.0,
                "category": "Text",
                "text": "Ib. 113. Tò đè miạn ai tỉan tủu tịn đào dưới dưới π.",
                "source_text": "alternate transliteration",
                "order": 2,
            },
        ]
        repaired = self.pipeline.repair_overlapping_fuzzy_duplicate_ocr_lines(blocks)
        self.assertEqual(len(repaired), 1)
        self.assertIn("σε μιαν αιτιαν", repaired[0]["text"])
        self.assertEqual(repaired[0]["anchored_duplicate_ocr_blocks_removed"], 1)

    def test_margin_number_sequence_repairs_inverted_digit_errors(self):
        values = ["9", "10", "15", "20", "25", "30", "35", "04"]
        blocks = [
            {
                "left": 20.0,
                "top": 45.0 + index * 35.0,
                "width": 12.0,
                "height": 10.0,
                "category": "Text",
                "text": value,
                "source_text": value,
                "order": index,
            }
            for index, value in enumerate(values)
        ]
        repaired = self.pipeline.repair_margin_number_sequence_values(
            blocks, 300.0, 400.0
        )
        self.assertEqual(
            [block["text"] for block in repaired],
            ["5", "10", "15", "20", "25", "30", "35", "40"],
        )
        self.assertEqual(
            sum(bool(block.get("margin_number_sequence_value_repaired")) for block in repaired),
            2,
        )

    def test_margin_number_sequence_does_not_shift_across_missing_values(self):
        values = ["5", "10", "20", "25", "30", "35"]
        blocks = [
            {
                "left": 20.0,
                "top": 45.0 + index * 35.0,
                "width": 12.0,
                "height": 10.0,
                "category": "Text",
                "text": value,
                "source_text": value,
                "order": index,
            }
            for index, value in enumerate(values)
        ]
        repaired = self.pipeline.repair_margin_number_sequence_values(
            blocks, 300.0, 400.0
        )
        self.assertEqual([block["text"] for block in repaired], values)
        self.assertFalse(
            any(block.get("margin_number_sequence_value_repaired") for block in repaired)
        )

    def test_source_margin_sequence_restores_missing_and_bad_values(self):
        source = self.root / "complete-line-number-sequence.pdf"
        with fitz.open() as document:
            page = document.new_page(width=300, height=400)
            for index, value in enumerate(range(5, 40, 5)):
                page.insert_text((35, 75 + index * 40), str(value), fontsize=10)
            document.save(source)
        values = ["5", "10", "75", "25", "30", "35"]
        tops = [67.0, 107.0, 187.0, 227.0, 267.0, 307.0]
        blocks = [
            {
                "left": 34.0,
                "top": top,
                "width": 12.0,
                "height": 10.0,
                "category": "Text",
                "text": value,
                "source_text": value,
                "order": index,
            }
            for index, (value, top) in enumerate(zip(values, tops))
        ]
        with fitz.open(source) as document:
            repaired = self.pipeline.repair_margin_number_sequence_values(
                blocks, 300.0, 400.0, source_page=document[0]
            )
        self.assertEqual(
            [
                block["text"]
                for block in sorted(repaired, key=lambda item: item["top"])
            ],
            ["5", "10", "15", "20", "25", "30", "35"],
        )
        self.assertEqual(
            sum(bool(block.get("margin_number_sequence_value_repaired")) for block in repaired),
            2,
        )

    def test_damaged_inverted_page_uses_rotated_source_fallback(self):
        source = self.root / "inverted.pdf"
        with fitz.open() as document:
            page = document.new_page(width=300, height=400)
            page.insert_text((260, 120), "Inverted source paragraph", fontsize=12, rotate=180)
            document.save(source)
        result = {"anchored_duplicate_ocr_blocks_removed": 1, "cells": [{"text": "bad"}]}
        blocks, used = self.pipeline.fallback_damaged_source_orientation_page(
            source,
            0,
            result,
            [{"anchored_duplicate_ocr_blocks_removed": 1}],
            300.0,
            400.0,
            self.root / "work",
            180,
        )
        self.assertTrue(used)
        self.assertEqual(blocks[0]["category"], "ImageFallback")
        self.assertEqual(result["image_fallback_kind"], "source_orientation_quality")
        self.assertEqual(result["cells"], [])
        self.assertTrue(Path(result["image_fallback_path"]).is_file())

    def test_indented_consecutive_text_blocks_are_separated(self):
        blocks = [
            {
                "left": 112.0,
                "top": 250.0,
                "width": 278.0,
                "height": 12.0,
                "category": "Text",
                "text": "Ib. 82. A distinct short reference line.",
                "order": 1,
            },
            {
                "left": 54.0,
                "top": 256.0,
                "width": 339.0,
                "height": 82.0,
                "category": "Text",
                "text": (
                    "A full-width following paragraph that is distinct from the "
                    "indented reference and must begin below it without overprinting."
                ),
                "order": 2,
            },
        ]
        repaired = self.pipeline.separate_overlapping_consecutive_text_blocks(
            blocks, 453.0, 680.0
        )
        self.assertGreater(repaired[1]["top"], 262.0)
        self.assertTrue(repaired[1]["consecutive_text_overlap_separated"])

    def test_distinct_consecutive_text_blocks_are_separated(self):
        blocks = [
            {
                "left": 60.0,
                "top": 100.0,
                "width": 180.0,
                "height": 12.0,
                "category": "Text",
                "text": "A distinct final line of the preceding paragraph.",
                "order": 1,
            },
            {
                "left": 60.0,
                "top": 106.0,
                "width": 220.0,
                "height": 55.0,
                "category": "Text",
                "text": "A different and much longer following paragraph that must not overprint the preceding line.",
                "order": 2,
            },
        ]
        repaired = self.pipeline.separate_overlapping_consecutive_text_blocks(
            blocks, 300.0, 400.0
        )
        self.assertGreater(repaired[1]["top"], 112.0)
        self.assertAlmostEqual(repaired[1]["top"] + repaired[1]["height"], 161.0)
        self.assertTrue(repaired[1]["consecutive_text_overlap_separated"])


if __name__ == "__main__":
    unittest.main()
