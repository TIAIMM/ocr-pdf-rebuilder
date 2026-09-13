# Output contract

Both engines always produce a searchable PDF. Paddle uses the corresponding
`book_paddle_searchable.pdf` name alongside `book_paddle.pdf` and
`book_paddle.md`. Its completion record and QC report require this artifact too.
The image variant remains conditional on pages that contain a full-page fallback
or a source-page Picture crop.

For an input named `book.pdf`, successful processing creates:

- `book_mineru.pdf`: text-only reconstructed PDF;
- `book_mineru.md`: page-delimited Markdown representation;
- `book_mineru.version`: integrity-protected completed-state record;
- `book_mineru_with_images.pdf`: created when one or more pages require an image
  fallback or contain a source-page Picture crop;
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
5. Full-page fallback pages contain rasterized source pages in the image variant;
   OCR Picture cells contain rasterized crops at their source bboxes. The
   text-only PDF remains raster-free.
6. Markdown preserves recognized LaTeX math spans and block formulas. PDFs
   render formulas through the vector path when available, with the defined
   Unicode/text/crop fallbacks when needed; no LaTeX residue is inserted into
   ordinary PDF text.
7. Nested table HTML and footnotes remain available to reconstruction.
8. A completion state is reusable only when source, outputs, runtime and page
   counts still match.
9. The searchable variant renders pixel-identically to the source and carries
   extractable text wherever OCR produced overlayable text. Table and Picture
   cells contribute no raw HTML; Formula cells contribute a linearized search
   representation without raw LaTeX commands.

The `.version` suffix is historical; its content is checksummed JSON state, not
a simple version string.
