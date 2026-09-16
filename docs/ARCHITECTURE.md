# Architecture

PaperLoom (知织) is the product name. The distribution `arxiv-research-assistant`,
module `arxiv_ra`, legacy `arxiv-ra` command and persistent integration identifiers
remain stable for compatibility. New installations expose both CLI names.

This project is a local-first application. The web UI and CLI are entry points;
research data remains in the configured output directory and integrations only
write to destinations explicitly enabled by the user.

## Module boundaries

```text
CLI / local Web UI
        │
        ▼
DailyPipeline and feature orchestrators
        │
        ├── discovery: arxiv_client, ranker, feedback, profiles
        ├── enrichment: metadata, abstracts, pdf_pipeline, report
        ├── integrations: emailer, zotero, obsidian
        └── outputs: render, weekly, version_tracker, citation_graph
```

- `cli.py` parses commands and starts the local server. It contains no research
  or storage logic.
- `web.py` owns HTTP boundaries and route composition. Reusable web concerns are
  separated into `web_catalog.py` (read models), `web_settings.py` (validated
  configuration writes), and `web_jobs.py` (bounded background queue).
- `pipeline.py` coordinates one daily run. Its stages are deliberately separate:
  ranking, selection, metadata verification, publication, and optional exports.
- `storage.py` defines profile-aware artifact paths and backward-compatible
  recommendation reads. Core orchestration must not depend on a web module.
- `utils.py` contains dependency-free primitives, including atomic UTF-8 and
  JSON replacement.
- Integration modules (`zotero.py`, `obsidian.py`, `emailer.py`) are adapters.
  They do not decide which papers are recommended.
- `discovery.py` coordinates arXiv and alphaXiv. `hybrid` actively queries both;
  `auto` preserves legacy fallback semantics and `arxiv` uses only arXiv.
  Candidates merge by arXiv ID, retaining per-paper provenance. arXiv hydrates
  partial semantic results; failures remain explicit in a profile/run manifest.
- `alphaxiv.py` speaks MCP and returns partial papers, with unknown dates and
  versions preserved. `paper_data.py` owns `PaperResolver`, local snapshot lookup,
  revision matching, complete metadata caches, and best-available recovery.
  Historical reads preserve their selected revision; unversioned direct requests
  refresh from arXiv. Exact revision requests never substitute another revision.
  Publication venue verification remains in `metadata.py`.
- `reading_state.py` owns saved/dismissed transitions and migration. The existing
  library and feedback interfaces retain collection and scoring behavior. One
  locked atomic write updates both states; callers never coordinate two writes.
- `research_clients.py` owns lazy construction and lifetime of task dependencies.
  Pipeline, profile generation, weekly synthesis, version tracking, and Obsidian
  accept this module explicitly. Injected adapters remain caller-owned. Web/CLI
  execution scopes close created clients on both success and failure.
- History/feedback filtering precedes prefilter truncation. Semantic candidates
  have an explicit concept-group threshold; shared files never control ranking.

## Persistent data contract

The following are user data and must never be overwritten by an application
upgrade:

```text
.env
config.yaml
profiles/
run/ (or the configured output_dir)
```

Daily recommendation data is stored per research direction as
`run/YYYY-MM-DD/recommendations-<profile-id>.json`. The legacy
`recommendations.json` remains a compatibility alias. Deduplication, feedback,
and document-library state are also profile scoped.

All JSON state writes use atomic same-directory replacement. A result is only
published after localization is complete, and processed-paper state is committed
only after requested email delivery succeeds.

Reading state is authoritative in `reading-state-<profile-id>.json`. On first
mutation the two legacy library/feedback files are imported under a profile lock
and retained unchanged. After migration, writing those legacy files from an old
application or external script does not update the new state. Do not run old and
new writers against the same output directory. Conflicting legacy entries use the
later update timestamp; ties or missing timestamps favor the negative feedback.
Malformed authoritative state raises an error rather than silently resetting it.

New reports include profile and revision in their folder names. Weekly outputs,
citation maps and version comparisons also use profile-specific names. Existing
artifact catalogs continue to discover legacy layouts.

## Concurrency model

- The GUI queue accepts 1–8 concurrent jobs and coalesces duplicate active jobs.
- Job configuration is copied at submission. Deduplication uses kind, operation
  parameters, project root and configuration, not display text. Historical report
  jobs capture their selected snapshot before queuing. Shutdown cancels undispatched
  jobs; active jobs finish and release their task-owned clients.
- Reading-state operations coordinate independent store instances and processes
  through a profile file lock. Atomic replacement alone is not a transaction.
  Metadata repair uses compare-and-update so a page read cannot resurrect a removed
  or dismissed paper. Toggle decisions also run entirely under the same lock.
- Semantic Scholar requests share a process-wide rate limiter.
- Obsidian exports share a process-wide lock because manifests and indexes are
  read-modify-write documents.
- Page-rendering routes use cached or local summaries and do not wait for LLM
  generation.

## Adding a feature

1. Put external API protocol details in a dedicated adapter module.
2. Keep profile-aware paths and durable formats in `storage.py` or a focused
   store class.
3. Add orchestration to the relevant pipeline or synthesizer, not to a template.
4. Expose the operation through a thin CLI command or web route.
5. Add an offline test for the success path and at least one failure/retry case.
6. Never add credentials, live user data, generated reports, or caches to a
   release archive.

## Test commands

```powershell
$env:PYTHONPATH = (Resolve-Path "src").Path
python -m pytest -q -p no:cacheprovider --basetemp work\pytest-release
python -m compileall -q src tests
python -m pip check
```

UI QA captures and notes live under `docs/qa/ui/`; build products live under
`release/<version>/`. Temporary build and test output belongs in ignored `work/`,
`build/`, or `dist/` directories.
