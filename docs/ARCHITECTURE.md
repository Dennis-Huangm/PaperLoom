# Architecture

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
- `alphaxiv.py` is an optional discovery fallback only. It returns candidate arXiv
  IDs after an arXiv API failure; existing metadata verification remains the
  source of truth for publication fields.

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

## Concurrency model

- The GUI queue accepts 1–8 concurrent jobs and coalesces duplicate active jobs.
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
