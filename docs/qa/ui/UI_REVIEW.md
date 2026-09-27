# Design QA — Product Design Option 2

## Comparison target

- Source visual truth: `C:\Users\HUANG\Documents\Codex\2026-08-16\y-m-y\work\product-design-audit-2026-08-17\selected-option-2.png`
- Final implementation: `http://127.0.0.1:8000/`
- Final implementation screenshot: `implementation-home-final2.png`
- Full-view comparison: `design-qa-comparison-final2.png`
- Focused inspector comparison: `design-qa-focused-right-final2.png`
- State: desktop, 今日文献, first paper selected, all filters cleared

## Viewport and normalization

- Requested browser viewport: 1440 × 1024 CSS px
- Browser inner viewport: 1440 × 1024 CSS px
- Browser client area: 1425 × 1024 CSS px (15 px reserved for the vertical scrollbar)
- Device pixel ratio: 1.00000003, effectively 1×
- Source pixels: 1487 × 1058
- Implementation capture pixels: 1425 × 993
- Normalization: source was center-fitted with Lanczos resampling to 1425 × 993 before the side-by-side comparison. No density-only differences were filed as findings.

## Required fidelity surfaces

### Fonts and typography

- The implementation uses the local `Segoe UI`, `Noto Sans SC`, `Microsoft YaHei` stack, closely matching the source's neutral research-tool sans treatment.
- Paper titles retain stronger optical weight than authors and metadata. Long titles wrap without clipping.
- The final inspector body was increased to 15 px with a 1.65 line height after focused comparison showed the first pass was too small.

### Spacing and layout rhythm

- The horizontal header, search row, filter row, 46/54 split workspace, selected paper row, evidence inspector, and persistent bottom actions match the source hierarchy.
- Unsupported mock-only filters were intentionally omitted instead of being presented as non-functional chrome.
- Dividers, 4–5 px radii, restrained surfaces, and near-zero elevation match the selected direction.

### Colors and visual tokens

- Warm copper `#c7650e` is used for primary actions, active navigation, score outlines, and selected controls.
- Neutral white/gray surfaces and graphite text replace the previous blue-purple gradients and dark SaaS sidebar.
- Verified, declared, pending, and error states retain distinct semantic colors with readable contrast.

### Image quality and assets

- The selected visual contains no content imagery or decorative raster assets.
- All UI icons come from the vendored Font Awesome Free icon library; no emoji, text-glyph stand-ins, handcrafted SVG, CSS illustration, or unreliable paper thumbnail is used.
- Font Awesome loaded successfully in the browser.

### Copy and content

- Real recommendation titles, authors, arXiv IDs, scores, abstracts, and venue states are used instead of the mock's invented data.
- Internal states such as `verified_metadata` and `declared_in_arxiv` are translated into human-readable Chinese.

## Responsive and accessibility evidence

- Desktop screenshot: `implementation-home-final2.png`
- Report library: `implementation-reports.png`
- Task page: `implementation-tasks.png`
- Settings desktop: `implementation-settings.png`
- Settings 820 px: `implementation-settings-820.png`
- Settings 390 px: `implementation-settings-390.png`
- At 820 px and 390 px, document `scrollWidth` equaled `clientWidth`; no horizontal overflow was present.
- Configuration grid children use `minmax(0, 1fr)`, and all inputs/textareas use `max-width: 100%`.
- Visible focus states, semantic buttons/links/labels, reduced-motion support, and practical mobile targets are present.

## Primary interactions tested

- Selecting the second paper updates the inspector to RefineSVG.
- Searching `RefineSVG` reduces the visible list to one row.
- Clearing filters restores all 10 rows.
- Mobile navigation opens and reports `aria-expanded=true`.
- Report, task, settings, and status routes render through the shared navigation.
- Browser console errors checked: none.

## Comparison history

### Pass 1 — blocked

- [P1] The empty-results panel was visible with real results and consumed the lower half of the list.
  - Fix: added a global `[hidden]{display:none!important}` rule.
- [P2] Inspector actions were below the visible viewport.
  - Fix: split the inspector into fixed header, scrollable evidence body, and fixed action footer.
- [P2] Inspector text was visibly smaller and sparser than the source.
  - Fix: increased section body, heading, metadata, and tag sizes; expanded the abstract excerpt.

### Pass 2 — passed

- Post-fix evidence: `implementation-home-final2.png`, `design-qa-comparison-final2.png`, and `design-qa-focused-right-final2.png`.
- No actionable P0, P1, or P2 differences remain.
- Intentional deviations: unsupported mock-only filters and batch-selection controls were not implemented; real project data replaces fictional source data.

## Follow-up polish

- [P3] A future iteration could add user-configurable compact/comfortable row density.
- [P3] A future iteration could add a resizable divider between the list and inspector.

final result: passed
