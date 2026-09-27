# Automatic report rendering — 2026-09-26

## Reproduction and root cause

The reported rMSE/PSS sections put bullet items directly after a PDF citation or
explanatory paragraph. Python-Markdown returned one paragraph containing the
literal hyphens. Removing math and sanitization reproduced the defect; adding
one blank line removed it. The previous table-only workaround did not address
the underlying Markdown dialect mismatch or nested list indentation.

Seven new regression cases initially failed (`work/render-workflow-red.log`).
The common renderer now uses markdown-it-py 4.x CommonMark with tables and
strikethrough, following the [official parser API](https://markdown-it-py.readthedocs.io/en/latest/using.html).
Math rules run within tokenization, preserving literal code. The previous table
preprocessor and global math regex replacement were removed. A small table
adapter retains existing code/math-pipe behavior and rejects excess cells.

## Automatic publication checks

- Recognizable bullet/table syntax left in a paragraph triggers an error.
- Parsed list/table/heading/math counts are checked after sanitization; directory
  targets must still exist.
- `render_report` validates before atomically writing HTML. A failed check cannot
  replace an existing good HTML artifact.
- The paper-pipeline regression uses a local PDF and fake model output. It
  verifies successful list/math rendering, then disables list parsing and
  proves generation stops with Markdown retained and no HTML/success metadata.
- A sanitizer fault injection also proves structure loss stops publication.
- Old report HTTP views return a readable 422 response and Markdown link rather
  than a traceback or stale malformed HTML.

Coverage includes five list markers, three nesting levels, quoted lists, table
boundaries and list-contained tables, escaped/code/math pipes, literal examples,
negative numbers, hyphenated model names, formulas, images, sanitization,
duplicate heading navigation, and ordered-list starting numbers. These checks
are structural, not semantic or universal malformed-Markdown repair.

## Results

- Full suite: **466 passed, 1 existing Starlette/httpx warning**, 62.73s;
  `work/commonmark-final.log`.
- `compileall`, dependency consistency (`pip check`) and `git diff --check` pass.
- Replayed ten existing test reports/comparisons through the production renderer.
  All pass; Markdown SHA-256 values remain unchanged. HTML backups and hashes:
  `work/commonmark-render-backups-20260926/manifest.json`.
- SVGenius contains 51 list items and six tables with body row counts
  `[16, 3, 9, 14, 6, 7]`. rMSE and PSS each have two separate list items.
- Browser at localhost:8772 confirmed six tables, 51 items, no missing directory
  targets, no KaTeX error and no console error. Screenshot:
  `work/commonmark-workflow-fixed.png`.
- No real model call or external integration. Only targeted defect verification;
  the broader desktop/narrow-screen acceptance remains deferred.

Existing directly opened HTML must be refreshed/re-rendered to pick up changes;
the ten identified test artifacts were refreshed. Running app processes must
load the updated package to use the new renderer. Unrelated user tabs and reports
were left alone. Math-bearing heading anchors may differ from old placeholder
IDs; generated table-of-contents links are checked against current headings.
