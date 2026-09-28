# Golden Set: Sources and Findings

Documents collected and verified on 2026-09-12. All are published by the issuer for public
access on its investor relations site. `manifest.yaml` records the source URL, retrieval date,
measured traits and checksum for each file.

## How each document was vetted

1. `pdftotext` over the first pages, counting characters, Arabic base characters
   (U+0600-U+06FF), Arabic presentation forms (U+FB50-U+FDFF, U+FE70-U+FEFF), Arabic-Indic
   digits (U+0660-U+0669) and ASCII digits. Roughly one character per page means no text layer.
2. `pdffonts` and `pdfimages -list` sampled on pages 1, 3, 5, 7 and 9, to separate a text PDF
   from page-sized scanned images, and to catch documents that mix both.
3. For scanned pages, a rendered PNG (`pdftoppm -r 150`) read by the macOS Vision OCR engine,
   to confirm the page is readable and to measure what that costs.

## The three tiers

| Tier | Documents | Why it is in the set |
|------|-----------|----------------------|
| A. Digital, bilingual | Almarai FY2025 annual report, English and Arabic | Main development and demo target. Both editions carry real text layers, and the statements sit on the same pages in both, so the two editions can be compared against each other |
| B. Scanned filings | Juhayna FY2025 and FY2024, Edita FY2025 and FY2024, both languages | What Egyptian exchange filings actually look like. These exercise the OCR path and the review flow |
| C. Mixed mode | Juhayna FY2025 English standalone | Short, fast to iterate against, and it proves that text-layer detection has to be per page |

## Findings that change the build

1. **OCR is mandatory, not optional.** Egyptian exchange filings are scans of signed
   statements, in English as well as Arabic. Of the ten EGX files collected, eight have no text
   layer at all and the other two are mixed. A plan that assumed digital PDFs only would fail on
   most of this population.
2. **Text-layer detection must be per page, not per document.** In the Juhayna English
   standalone, pages 1-2 and 6-61 carry text while pages 3-5 are images, and the statement of
   financial position is one of those images. Deciding OCR per document would either miss the
   balance sheet or waste OCR on 55 text pages.
3. **Arabic editions use Arabic-Indic digits,** and the thousands separator varies by source:
   Almarai's digital Arabic uses commas (`٢٢,٧٥٠,٣٤٢`), while scanned Egyptian filings use
   spaces (`٨ ٨٦٤ ٣٨٣ ٢٤٤`). The number parser needs both from the start.
4. **Digital Arabic is proper Unicode.** The Almarai Arabic edition has 21,239 Arabic
   characters and zero presentation forms in its first 15 pages, so no glyph-form normalization
   is needed there. It does carry bidi controls (U+202A, U+202B, U+202C), which must be stripped.
   Correction, 2026-09-27: its text layer stores Arabic in visual order, so every word comes out
   with its letters reversed (`ةمئاق` for `قائمة`). Of 82 Arabic corpus PDFs with a text layer,
   Almarai's is the only one that does this, but 77 return each line's words in reverse order,
   and lam-alef ligatures often come out swapped (`اآلخر` for `الآخر`). See
   `docs/blueprint/09-ingest-locate.md`, R24.
5. **Accounting vocabulary differs by country.** Saudi filings write assets as `الموجودات` and
   liabilities as `المطلوبات`; Egyptian filings write `الأصول` and `الالتزامات`. The Arabic
   lexicon carries both regional variants per canonical item.
6. **Column order flips between language editions.** In the Almarai Arabic edition the value
   columns extract as prior year, then current year, then note reference, the reverse of the
   English edition. Columns must be bound to their header text, never to position.
7. **A currency symbol may not be text at all.** Almarai's statement header extracts as
   `X '000`, because the riyal glyph is a font symbol. Currency detection has to read the
   surrounding words, not the symbol.
8. **Bilingual editions are page-aligned.** In the Almarai report the statements fall on the
   same pages in both languages (financial position 156-158, profit or loss 159, changes in
   equity 162-163, cash flows 164-166). Extracting both and requiring the figures to match is a
   verification method that costs no hand-annotation.
9. **Vision OCR reads statement pages, in accurate mode only.** Warm, at 150 dpi, an English
   statement page took 24.7 seconds and returned clean labels. The same page in fast mode
   returned `FlxdAyel` for Fixed Assets and corrupted digits, so fast mode cannot be used for
   statements. Budget roughly 3 to 4 minutes of OCR per scanned filing, against seconds for a
   digital one. Correction, 2026-09-27: most of the 24.7 seconds was the model loading, once per
   language per process. Warm accurate mode reads a whole scanned page at 100 dpi in about
   0.25 s; English pages take about 0.45 s because Vision reads only in the first language it
   is given, so each page is read in Arabic first and again in English when that finds no
   Arabic (R21). A 60-page scanned filing takes 11 to 15 s in Arabic and about 30 s in English.

## Rules for adding documents

- Public investor relations or exchange sources only. Record the exact URL and retrieval date.
- Prefer issuers that publish both language editions of the same statements.
- Record traits honestly in `manifest.yaml`. A scanned document labelled digital will quietly
  corrupt every accuracy measurement taken against it.
- Keep non-financial corporates in the main set. Banks and insurers belong in a separate group,
  since their statements use a different structure.
- Store files under `documents/` as `<issuer>-<fiscal year>-<lang>-<scope>.pdf`.
