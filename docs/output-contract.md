# Output contract

For an input named `book.pdf`, successful processing creates:

- `book_mineru.pdf`: text-only reconstructed PDF;
- `book_mineru.md`: page-delimited Markdown representation;
- `book_mineru.version`: integrity-protected completed-state record;
- `book_mineru_with_images.pdf`: created only when one or more pages require an
  image fallback;
- `book_mineru_searchable.pdf`: always created; the original source pages plus
  an invisible OCR text layer (PDF render mode 3) positioned from MinerU
  span/line boxes, so the scan stays visually unchanged while remaining
  searchable and selectable.

Required properties:

1. Every produced PDF has exactly the same page count as the source.
2. Trailing source blank pages are retained as blank pages.
3. The text-only PDF contains no raster images.
4. Handwriting, unusable scan and full-page-image fallback pages are blank in
   the text-only PDF.
5. The corresponding pages contain rasterized source pages in the image variant.
6. Formulas are rendered when safe; a failed formula renderer uses the defined
   text/crop fallback without inserting LaTeX residue into normal text.
7. Nested table HTML and footnotes remain available to reconstruction.
8. A completion state is reusable only when source, outputs, runtime and page
   counts still match.
9. The searchable variant renders pixel-identically to the source and carries
   extractable invisible text wherever OCR produced overlayable text; blank
   pages carry none and Table, Formula and Picture cells contribute no raw
   HTML or LaTeX to the layer.

The `.version` suffix is historical; its content is checksummed JSON state, not
a simple version string.
