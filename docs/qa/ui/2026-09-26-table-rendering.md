# Report table rendering — 2026-09-26

## Root cause and scope

SVGenius (`2506.03139`) included pipe tables directly after explanatory prose,
headings, and list items. Python-Markdown interpreted missing block boundaries
as paragraphs; list continuation also requires four spaces. Earlier numeric
audit validation did not verify the final rendered table structure.

The shared renderer now normalizes recognized table boundaries and immediate
list continuation, without changing saved Markdown. Code fences, indented code,
escaped pipes, inline code, formulas and citation links are covered by regression
tests. Alignment attributes survive sanitization. Wide tables scroll locally;
cell styles no longer split numbers or English words at arbitrary characters.

## Verification

- Full regression after the parser fix: **452 passed, 1 existing Starlette/httpx
  deprecation warning**, 64.02s (`work/table-rendering-tests.log`).
- Nine new rendering regression cases; initial failing cases reproduced the
  defect before implementation.
- Production `ReportStaticFiles` served the isolated fixture at localhost:8772.
  Browser DOM confirmed six tables, with body-row counts **16, 3, 9, 14, 6, 7**;
  the six-row win-rate table remains inside its list item.
- No paragraph retained the unrendered separator syntax. No KaTeX error or
  browser console error was observed. Final layout had no page overflow;
  complexity values such as `1148.67` stayed on one line.
- Screenshot: `work/table-rendering-fixed.png`.
- After the final cell-style adjustment, all 15 renderer tests passed again;
  compilation and `git diff --check` passed. Temporary QA tab and server closed.
- This was focused defect verification, not the deferred general desktop and
  narrow-screen acceptance pass.

## Existing artifacts

Re-rendered ten known local test HTML reports, including
`work/long-synthesis-balanced-20260926/library/2026-09-26/reports/2506.03139/report.html`.
Markdown, evidence and numerical claims were not rewritten; no model calls.
Original HTML and before/after hashes are retained under
`work/table-render-backups-20260926/manifest.json`. Source Markdown for SVGenius
retains SHA-256 `bfb6199f4c9e9d3caabcc0ff51b9e08def99b93de35d06e450ba8311d66df58c`.
The HTML hash in the earlier quality evaluation describes the pre-refresh
presentation; the backup manifest records this subsequent rendering-only update.
