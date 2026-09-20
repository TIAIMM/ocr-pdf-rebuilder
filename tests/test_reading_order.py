from __future__ import annotations

import unittest

from ocr_pdf_rebuilder.reading_order import (
    has_clear_column_layout,
    order_items_for_reading,
)


def text_block(label, left, top, width=210, height=34, category="Text", order=0):
    return {
        "left": float(left),
        "top": float(top),
        "width": float(width),
        "height": float(height),
        "category": category,
        "text": label,
        "order": order,
    }


class ReadingOrderTests(unittest.TestCase):
    def test_clear_two_columns_are_read_down_then_across(self):
        blocks = [
            text_block("R1", 340, 100, order=0),
            text_block("L1", 50, 100, order=1),
            text_block("R2", 340, 150, order=2),
            text_block("L2", 50, 150, order=3),
        ]

        ordered = order_items_for_reading(blocks, 600, 800)

        self.assertEqual([block["text"] for block in ordered], ["L1", "L2", "R1", "R2"])
        self.assertEqual([block["order"] for block in ordered], [1, 3, 0, 2])

    def test_column_detector_requires_repeated_separated_blocks(self):
        blocks = [
            text_block("R1", 340, 100, order=0),
            text_block("L1", 50, 100, order=1),
            text_block("R2", 340, 150, order=2),
            text_block("L2", 50, 150, order=3),
        ]

        self.assertTrue(has_clear_column_layout(blocks, 600))
        self.assertFalse(has_clear_column_layout(blocks[:3], 600))

    def test_header_title_and_footer_surround_column_body(self):
        blocks = [
            text_block("R2", 340, 150, order=0),
            text_block("Footer", 50, 760, 500, 18, "Page-footer", 1),
            text_block("Title", 50, 55, 500, 28, "Title", 2),
            text_block("L1", 50, 100, order=3),
            text_block("Header", 50, 20, 500, 18, "Page-header", 4),
            text_block("R1", 340, 100, order=5),
            text_block("L2", 50, 150, order=6),
        ]

        ordered = order_items_for_reading(blocks, 600, 800)

        self.assertEqual(
            [block["text"] for block in ordered],
            ["Header", "Title", "L1", "L2", "R1", "R2", "Footer"],
        )

    def test_single_column_indentation_does_not_trigger_column_mode(self):
        blocks = [
            text_block("second paragraph", 100, 180, 440, order=0),
            text_block("first paragraph", 50, 100, 500, order=1),
            text_block("third paragraph", 70, 260, 480, order=2),
        ]

        ordered = order_items_for_reading(blocks, 600, 800)

        self.assertEqual(
            [block["text"] for block in ordered],
            ["first paragraph", "second paragraph", "third paragraph"],
        )

    def test_original_block_order_is_preserved_as_tie_breaker(self):
        blocks = [
            text_block("same top, second", 50, 100, order=8),
            text_block("same top, first", 50, 100, order=2),
        ]

        ordered = order_items_for_reading(blocks, 600, 800)

        self.assertEqual(
            [block["text"] for block in ordered],
            ["same top, first", "same top, second"],
        )


if __name__ == "__main__":
    unittest.main()
