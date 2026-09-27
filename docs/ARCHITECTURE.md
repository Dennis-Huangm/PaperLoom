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
        └── outputs: render, activity, weekly, comparison, version_tracker, citation_graph
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

Daily publication merges snapshots by `(arxiv_id, version)` under a profile
file lock. Each publication stores its input and the preceding daily snapshot
under `YYYY-MM-DD/batches/<profile>/<batch-id>/`. SMTP sends only that batch;
processed IDs are merged with current state only after delivery succeeds.
Weekly recommendation collection keeps the highest revision among the same day's
snapshots; research activity can select the revision actually read instead.

`report_store.py` selects evidence using metadata identity: profile, arXiv ID,
and revision must match. Unscoped legacy metadata remains compatible, but a
known revision never falls back to unknown or different revisions. Scoped
reports take precedence over legacy reports of the same revision.

Obsidian paper manifest keys are `<profile>::<arxiv-id>`; legacy keys are
migrated on writes for their original named profile. Attachment paths include
profile and revision. Managed updates retain text on both sides of the markers
and custom YAML fields, and reject damaged markers/YAML before writing.
Zotero report exports carry a concrete artifact ID; PDF caches pin the revision,
and imported attachments deduplicate by content digest rather than PDF presence.

All JSON state writes use atomic same-directory replacement. A result is only
published after localization is complete, and processed-paper state is committed
only after requested email delivery succeeds.
Windows replacement retries access/sharing violations for a bounded interval,
because a temporary read handle may deny deletion even while writers are serialized.
Persistent failures still raise and leave the previous destination intact.

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

`version_sync.py` coordinates a revision-pinned single-paper update. An
authoritative arXiv lookup selects the target; a failed lookup never substitutes
cached metadata as the latest revision. `papers/<profile>/<id>/sync.json` stores
per-revision options and stage outcomes; `vN/` contains validated PDFs, metadata,
and the pre-update library snapshot. A profile/paper lock serializes updates.
Library refresh preserves original save time and source date, uses compare-and-
update, and never downgrades an existing newer revision or restores removed entries.
PDF publication precedes optional report generation and independent integration
stages. Report metadata distinguishes full and abstract-level output. Retrying a
pinned target reuses successful stages and retries failed, degraded or interrupted
ones. Zotero adds revision attachments without rewriting user-edited parent fields.
Paper sync stages have their own durable recovery. The GUI additionally persists
generic task records and supports explicit request re-execution as described below.

`version_batch.py` adds durable manual batch plans. Preview validates selections
against the local update catalog, freezes revision targets, selected stages and
an `AppConfig` snapshot, and performs no downloads or model calls. Execution is
sequential under a batch file lock; each paper uses the existing revision-pinned
sync service. Successful papers are skipped on resume, and unfinished papers reuse
successful single-paper stages. Explicit batch options never inherit additional
model or integration work from earlier single-paper requests. Export reuse also
checks the destination settings. `task_subtask` maps single-paper progress into the
batch range while forwarding warnings and cancellation. Durable plans and item
outcomes survive process loss; the GUI queue now also retains task records. Credentials
remain environment-variable references, not copied secret values. GET catalogs
perform no network requests. Recent history retains all unfinished execution batches
in addition to the latest 20 plans.

Version tracking stores all candidates and rotates a bounded batch by persisted
attempt sequence. It excludes dismissed papers and includes saved papers by
default. Detection is persisted before PDF/model comparison, so comparison failure
does not erase the update. Page catalogs derive available local revisions from
current files instead of trusting stale tracking state.

`version_auto.py` applies the profile-local `VersionSyncConfig` after each check,
while the tracker file lock is still held. Disabled policies do no synchronization.
It re-reads local file/reading state, excludes untracked or failed-query entries,
caps the round, and persists an `origin=automatic` batch before doing work. All
automatic plans (including prepared plans after a process crash) form an attempt
ledger keyed by paper and revision. Repeated checks do not retry these revisions;
users explicitly resume the durable batch instead. New remote revisions remain
eligible. Limits defer entire papers to later checks; they never silently drop
requested report/export steps after a PDF succeeds. `automatic.json` holds the
latest round summary/errors; individual outcomes remain in batch history.

Policy settings live in the direction YAML; a new direction never inherits a
globally enabled policy. The initial default-profile migration preserves an
existing no-profile policy. The web save endpoint rejects a stale direction ID.
Daily, CLI and GUI checks share this orchestration; no new scheduler is installed.

`model_budget.py` provides a ContextVar request counter for each automatic batch
execution, including explicit resumes. `LLMClient` reserves and persists each
outbound request before dispatch; budgeted requests disable SDK internal retries.
Both vision requests and compatibility retries consume quota. Scope restoration
prevents quota leaking into other concurrent jobs or the independent version-diff
analysis. Steps that swallow a quota exception through a content fallback are
still marked degraded by the sync service. A manual continuation receives a new
quota from the frozen policy; cumulative and per-execution counts are recorded.

- The GUI queue accepts 1–8 concurrent jobs and coalesces duplicate active jobs.
- Job configuration is copied at submission. Deduplication uses kind, operation
  parameters, project root and configuration, not display text. Historical report
  jobs capture their selected snapshot before queuing. Shutdown leaves recoverable
  undispatched jobs interrupted; active jobs finish and release task-owned clients.
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

### September 24 reliability and reading records

- Browser forms and asynchronous requests carry the rendered profile ID. The
  HTTP boundary rejects stale pages and captures one configuration per request,
  including thread-pool work. Legacy API clients without a profile parameter
  retain active-profile semantics. Collection actions resolve explicit source
  dates/revisions or report artifact IDs; repeated adds never downgrade a saved
  revision.
- The reading-state document has an optional `reading` map, separate from
  `library` and `feedback`. Personal notes survive preference changes. Updates
  use the existing file lock and atomic replacement plus an expected timestamp
  to reject stale editors. Reading revision/history do not advance when the
  saved-paper metadata advances. Old state documents remain readable.
- Report Markdown is sanitized with nh3; math elements and evidence tables
  remain supported. Report interaction code lives in `static/report.js`.
  `web_artifacts.py` presents legacy reports safely from local Markdown using
  StaticFiles containment checks. No user artifact is rewritten by a GET.
- Report generations use distinct directories, preserving preceding attempts.
  Exact-profile/revision evidence and library links prefer full reports over
  abstract fallbacks. Generation metadata includes model, time and parsed
  pages/characters when known; these are processing facts, not factuality scores.
- PDF synchronization verifies stored SHA-256 digests, with page validation for
  legacy files. The library stage reconciles current state on every execution;
  a prior success cannot certify a later user-modified collection.
- `ZoteroClient.list_trackable_papers` owns external field parsing. Fallback
  abstract cache entries may be retried by explicit generation, never by GET.

### Recommendation controls and report evidence

- `ranker.py` produces score components from the same calculation that determines
  lexical rank. `Paper.ranking_explanation` persists matched keywords, categories,
  concept groups and final score inputs. History is never re-explained using a
  newer configuration. A run captures library and feedback together under the
  reading-state lock, so scoring and exclusion use the same preference snapshot.
- Feedback adds an explicit `scope`: `paper` excludes only that arXiv ID; `topic`
  uses user-entered terms with phrase boundaries. Unscoped legacy rules retain
  their earlier similarity behavior and are labeled accordingly. A dismissal
  retains `previous_library`; undo checks its feedback timestamp under the same
  lock, restores the saved entry when appropriate, and leaves personal notes
  intact. A later save or feedback edit invalidates an older undo request.
- Each recommendation attempt writes an immutable
  `selection-<profile>-<uuid>.json` alongside daily outputs. It records all papers
  returned to the ranking pipeline, selected papers and filtering reasons. The
  read-only feedback page resolves run IDs from an exact profile filename match
  and verifies the payload profile. Discovery-level non-recall remains outside
  the scope of this audit. The UI lists the latest 100 runs; older files remain.
- `evidence.py` owns quote validation and PDF location links. Model output uses
  `[[证据:verbatim quote]]`; normalization permits whitespace/Unicode differences,
  and only a unique physical-page match of 20–600 non-whitespace characters gets
  a link. Both parsers provide physical PDF page text; Docling Markdown remains
  the reading input, while PyMuPDF extracts independent page text for matching.
  Model-generated page fragments are not accepted as verified locations.
- `source_spans.py` additionally builds page-local, overlapping source windows
  (500 raw characters, 100 overlap, at least 20 non-whitespace characters).
  `[[证据ID:ID]]` identifies the span format version, a 24-hex prefix of the
  whole page-text SHA-256, page and character offsets. IDs resolve only against
  the current page-text snapshot; they anchor repeated text to a known page.
  Legacy free-text quotes still require a unique page. Accepted ID citations
  store offsets and the full text digest; new comparison snapshots carry these
  fields forward. Neither mechanism validates semantic entailment.
- Analysis retains parser text and distributes a separate page-text span bank
  across bounded calls. Notes may promote only IDs supplied to that call.
  Synthesis restores cited source text with a 32k raw-character budget; unknown
  or over-budget IDs become explicit unavailable-evidence notes. This adds
  input material and can add chunk calls. `ReportGenerator.analysis_inputs()`
  is the shared call plan used by generation and real-evaluation request bounds.
  Prompts and therefore checkpoint signatures include the source-ID material.
- `report_metadata.py` replaces model front-matter tables with structured title,
  author, revision, category and publication fields. Paper-snapshot author names
  take precedence over metadata-provider spelling. Separate model institution
  text remains labeled unverified, with no fuzzy author/affiliation join. This
  does not independently verify upstream metadata or rewrite body claims.
- A report attempt stores `evidence.json` (quotes, physical pages, report sections)
  and summary evidence metadata. The appendix distinguishes processed pages,
  pages with extractable text, referenced pages and sections containing at least
  one validated quote. These counts do not measure entailment or content coverage.
  No extra LLM calls are needed for validation. OCR-only and cross-page quotes may
  be unlocatable; the report explicitly shows missing evidence rather than guessing.

### Research activity and cross-paper comparison

- Comparison quote selection uses section round-robin with a per-paper cap of
  60 excerpts and 12,000 quote characters, then restores source order for IDs.
  This replaces the first-20 rule that excluded later numeric rows and limits.
  Each quote remains capped at 1,000 characters; location validation is unchanged.
  The model must supply exactly the same evidence-ID set in each cell's evidence
  list and support list. This format requirement is not entailment validation.

- `reading_state.py` adds optional `activity_started_at` and `activity` fields to
  schema v1. Collection, removal/restoration, revision, reading and note changes
  append events in the same locked atomic write as their state mutation. Repeated
  identical updates do not generate new events. Historical note snapshots remain
  local and survive collection removal.
- `activity.py` combines this journal with profile-matching report metadata and
  version discoveries within timezone-aware calendar boundaries through the report
  cutoff. Legacy state contributes only timestamp-confirmable events before the
  journal began. Current note text is never assigned to an earlier historical date.
- `weekly.py` captures reading state once, prioritizes active papers within the
  corpus limit and appends deterministic event counts/history after synthesis.
  Reading an older revision cannot borrow the saved newer revision's abstract;
  report selection honors exact identity and the report cutoff. Notes are excluded
  from model input and appended locally only with explicit `include_notes`. When
  enabled, configured Obsidian weekly synchronization includes this appendix.
- Weekly attempts use `weekly/<week>-<profile>-<uuid>/` and preserve prior output.
  Alongside Markdown/HTML and metadata, `activity.json` stores the event snapshot
  (without private notes/tags by default), and `sources.json` stores the selected
  corpus and report excerpts. Catalogs and bulk Obsidian export order attempts by
  generation time, so the newest weekly attempt becomes the current managed note.
- `comparison.py` validates 2–5 distinct papers with known revisions from local
  recommendations, saved papers or scoped reports. Preparation freezes public
  material before enqueueing: exact revision/profile, preferred full report,
  bounded section excerpts, located quotes, report artifact ID and SHA-256.
  Unscoped legacy reports are excluded. Notes/tags never enter the snapshot.
- One structured model response populates seven fixed comparison dimensions.
  Each cell distinguishes source summary, inference and unknown; evidence IDs must
  belong to that paper and have exact support excerpts. Invalid/missing references
  or excerpts become unknown. This validates literal support, not semantic
  entailment. Quote page links reuse the original
  report's verified locations; experimental comparability remains a manual check.
  Model failures preserve an unknown matrix and the available evidence list.

### Quality guards and offline evaluation

- `quality.py` normalizes literal numeric values with exact Decimal equality,
  Unicode/spacing normalization and a bounded set of unit aliases. It preserves
  signs and distinguishes percentages, percentage points and time units; it does
  not calculate relative gains or infer conversions. Compound formulas, known
  figure/table references and alphanumeric model/metric IDs are excluded.
- `evidence.py` audits numeric prose in experimental setup, key results and
  reproducibility sections against uniquely located quotes in the same paragraph
  or table row. Version 3 withholds unsupported prose and individual suspect
  table cells before Markdown/HTML publication and optional export. Full original
  blocks, character ranges, replacements, reasons and retention counts persist
  in `numeric_audit`, in evidence and metadata; report prose stays compact. The task and
  catalog expose pending checks. Blank physical-page text cannot yield `checked`
  quote status. `full` still denotes execution mode, not semantic correctness.
- Comparison output now requires `support: [{evidence, quote}]` per cell, with a
  continuous 8–600 non-whitespace character span for every cited source. Quotes
  must occur in the cited frozen evidence; numbers must occur in primary support
  (abstract or located quote), not merely elsewhere in the paper or in a model
  report. Report-only nonnumeric cells become inference. Mixed primary/report
  support also becomes inference with `basis: mixed_sources`; adding an abstract
  must not relabel the report's claims as entirely primary evidence. Obvious explicit
  cross-paper rankings, malformed cells and duplicate paper rows become unknown.
  Rejection reasons and accepted support spans persist in `matrix.json`; metadata
  records accepted/rejected/unknown counts with `semantic_support: not_assessed`.
- Invalid structured model output is discarded before generating the fallback
  matrix. Publication remains immutable and does not rewrite old comparisons.
  The guards add no model requests. Prompt changes flow through existing exact
  prompt signatures, invalidating incompatible report checkpoints on retry.
- `table_quality.py` uses exact source row names on cited physical pages. Only
  rows intersecting the cited range (or fully contained in a legacy quote) are
  eligible; it never searches other pages to rescue a claim. Plain PDF rows can
  be checked for ordered projection, retaining omitted metrics. Exact, unique,
  flat headers additionally bind metric columns and explicit qualifiers/units.
  Pipe source tables can bind explicitly present dataset/split/condition columns;
  plain PDF difficulty checks require standalone labels within the same table.
  Multi-level headers, damaged digits, aliases and ambiguous rows abstain.
  `headers_checked`, `partial_headers_checked`, `row_order_checked`, `unassessed`
  and `mismatch` distinguish the check actually performed. Abstention alone does
  not erase a table. A row-order check is not proof of its metric labels.
  Source row locations in diagnostics are `[physical_page, start, end]` offsets
  into the unchanged `ParsedPaper.page_texts` snapshot. Local expansion to the
  complete row/flat header does not change the original citation text or ID.
- Publication edits use exact character ranges and verify the input has not
  changed; stale audits fail rather than applying to another report. Source
  tokens are masked without changing offsets. Fenced examples, structural
  headings and compound formulas are excluded. Table cell boundaries respect
  escaped pipes, code and inline math, including optional outer pipes. Adjacent
  unsupported prose siblings may share a short placeholder; their individual
  original statements remain recorded. No model call or guessed value is used.
- These rules are deliberately not entailment verification: prose metric labels,
  unsupported dataset/baseline attribution, negation, causality, equivalent formulas and subtle
  rankings require human review. The offline corpus includes an explicit metric
  swap counterexample outside the automated pass denominator.
- `scripts/evaluate_quality.py` runs fixed synthetic text/responses from
  `tests/fixtures/quality/cases.json`, emitting a corpus hash and case outcomes.
  Pytest additionally exercises real local PDF parsing, long reports, truncation,
  precision, malformed output and artifact persistence. No credentials, personal
  material or live model endpoint is required; results are guard regression data,
  not a model quality benchmark.
- Report synthesis additionally receives bounded, uncompressed first-page text
  (5,000 characters) and up to 12 URL contexts (500 characters each), drawn from
  physical page text and parser text. These remain untrusted paper material;
  links are not fetched, resolved or automatically classified as project resources.
  This bypasses lossy chunk notes without introducing a new model request.
- `scripts/evaluate_real_quality.py` snapshots five existing report/PDF pairs into
  a separate evaluation profile, with source hashes and no original writes.
  Default operation is offline. Explicit `--live` uses a bounded request budget
  for one text-only report and two comparisons (or comparisons only), records
  prompts/responses and declared response model IDs, and never invokes delivery
  or integrations. Credentials are referenced by environment variable names,
  not saved values. This is a targeted audit harness, not an accuracy benchmark.
- `comparisons/<date>-<profile>-<uuid>/` holds independent report, matrix, source
  snapshot and metadata files. Jobs deduplicate on the frozen snapshot and question;
  stale-profile submissions fail at the HTTP boundary. The GUI and CLI share the
  service and perform no automatic download or single-paper generation. Both new
  operations use the durable GUI task records and explicit recovery entry point.
- Literal note and evidence text is escaped before Markdown rendering; math
  delimiters use entities so copied links or LaTeX are not mistaken for formulas.

### Durable GUI tasks and explicit recovery

- `job_store.py` atomically publishes versioned JSON records under the output
  directory's private `.jobs/`. A nonblocking OS file lock allows one live GUI
  queue per output root. The lock remains held during graceful shutdown until
  active workers finish; the OS releases it if the process dies. `web_artifacts.py`
  denies access to the ledger, including case variants on Windows.
- `web_jobs.py` writes each request before dispatch and persists state transitions,
  progress, warnings, result URLs, original submission details and retry links.
  Startup restores history and changes queued/running/cancelling records to
  interrupted. It performs no automatic replay. Malformed/unsupported records
  remain untouched and produce visible load warnings rather than an empty reset.
- `job_requests.py` captures dataclass configuration and operation parameters,
  not closures or serialized executable objects. Credentials remain environment
  references; current runtime values are resolved by the existing clients.
  Recovery reuses the saved profile/configuration and current queue's output root.
  A stale or different-profile retry is rejected before execution.
- Supported recovery requests are report, digest, weekly, comparison and version
  batch. Report hooks persist the resolved public paper before PDF/model work;
  replay pins that revision or the original selected snapshot. Comparisons keep
  their complete prepared source snapshot. Weekly requests keep their cutoff and
  note opt-in; activity/materials are read again at execution. Weekly `generated_at`
  records actual publication time separately from `period_end` for correct catalog
  and Obsidian ordering after a delayed recovery.
- Digest recovery preserves retrieval options but forces `deliver=False`, even
  when the initial request selected email. This avoids repeating a possibly sent
  email after an ambiguous interruption. It re-queries sources and applies current
  reading preferences. Other configured optional integrations can run again.
- Retrying creates a new attempt without replacing the original. A stored child
  `retry_of` repairs a missing parent link after a crash between the two writes.
  Duplicate retry submissions return the existing child, including after success.
  Recovery identities are separate from fresh submissions: a pinned v2 recovery
  cannot coalesce with an active unversioned request that may be fetching v3.
  To retry another failure, operate on that newer failed attempt. Version batches
  reuse their own step ledger; single-report tasks additionally reuse the
  checkpoints described below. Other operations rerun their requests.
  External effects do not have an exactly-once guarantee.
- The task page paginates historical records and retains unresolved recoverable
  attempts on its first page. Other GUI task kinds retain records but use their
  existing feature-specific entry points. CLI commands are outside the generic
  GUI ledger (except the scheduler's shared queue). No automatic recovery replay
  or record-pruning policy is added.

### Single-report step recovery

- `report_checkpoint.py` owns private, disposable `.jobs/report-work/<uuid>/`
  directories. Only explicit retries share the ID; fresh reports never coalesce
  with this cache. The ID is saved to the job before PDF/model processing. A
  task-local context scopes use to GUI single-report generation, preserving the
  reporter interface and avoiding global mutable state on shared clients.
- Parse snapshots store text, physical page text, parser metadata, PDF and image
  files. Portable relative paths are containment-checked, file hashes and parsed
  data hashes verified before copying into a new immutable report artifact.
  Paper/profile mismatches create a new cache; parser/source changes invalidate
  dependent model work. Missing caches after backup restore safely recompute.
- Each successful chunk, figure explanation and synthesis response is atomically
  saved before the next cancellation boundary. Text-call signatures include exact
  prompts, model configuration, a hash of the effective endpoint, and verified
  source identity. Figure signatures additionally cover image bytes and supplied
  paper/caption information; its prompt protocol is explicitly versioned.
  Runtime credentials are never persisted. Changing a served model behind an
  unchanged model name/endpoint cannot be detected; a fresh report bypasses reuse.
- Only nonempty, integrity-checked responses qualify as completed. Corrupt or
  incompatible entries warn and recompute the affected work. Checkpoint write
  failure aborts instead of disguising it as an abstract fallback. A response
  lost before persistence may require another request; no exactly-once model
  execution guarantee is made.
- Job records expose only saved-step counts/flags, not cached evidence. Abstract
  fallbacks after failure retain their result link and permit explicit retry.
  Retry linkage, profile guards and no automatic replay remain unchanged. The
  first history page retains these unresolved fallback tasks. Metadata and HTML
  figure discovery still run; configured Obsidian export can run again.
- The existing backup allowlist deliberately excludes these cache directories;
  task records and selected published artifacts remain backed up. CLI reports,
  daily/nested report workflows and version batches do not acquire this new
  generic checkpoint scope. Version batches retain their existing step ledger.

### Local full-text search

- `search.py` owns a disposable SQLite index under private `.search/`, with one
  database per hashed profile identity. It gathers saved/recommended metadata,
  retained notes, single-paper Markdown reports and already-local report/sync
  PDFs. Unscoped legacy artifacts keep the catalog's shared visibility rules.
  Original JSON, Markdown and PDFs remain authoritative; no model/network calls
  or third-party vector store are involved.
- GUI refresh runs as a normal queued task with captured profile identity. JSON
  records use content signatures; report/PDF signatures include metadata and
  body size/mtime. Unchanged sources reuse extracted chunks. PDF chunks retain
  physical page numbers; notes use `read_version`, including removed favorites.
  Invalid PDFs are omitted with warnings; empty pages are counted without OCR.
- A per-profile OS lock serializes updates. One SQLite transaction replaces
  changed/deleted sources, chunk rows and FTS entries together. Cancellation or
  failure rolls back; full rebuild creates a separate database before replacement,
  including when the previous database is corrupt. Background tasks can return
  no artifact; index tasks use the search page and do not publish database links.
- NFKC/case normalization and whitespace normalization are shared by indexing
  and query parsing. FTS5 trigram accelerates literal terms of at least three
  characters, with bound `instr` predicates for exact containment and shorter
  terms. User input never becomes raw FTS or SQL syntax. All terms must appear
  in one chunk/title, not anywhere in a whole paper. Results group by source and
  paginate at 20; ordering is deterministic by title/ID/revision/source.
- GET searches read the last explicitly refreshed snapshot. Source previews check
  current signatures before displaying cached text; changed/deleted sources demand
  refresh. Search pages are `no-store`, source text is escaped, and `.search`
  downloads are denied case-insensitively. Old-profile forms/source links return
  409. The cache is local plaintext, not encrypted storage.
- First release excludes OCR, weekly/comparison artifacts, semantic search and
  external libraries. Short terms scan indexed chunks; large-library benchmarks
  remain future work. SQLite must support FTS5's trigram tokenizer (3.34+).

### Portable backups and isolated restoration

- `backup.py` defines a version-1 ZIP manifest with explicit project/data namespaces,
  sizes and SHA-256 for every payload. The allowlist includes configuration,
  profiles, direction state, recommendation JSON, version state/batches and GUI
  job records. Reports/figures and PDFs are independent opt-ins; PDF-only backups
  retain adjacent metadata. `.env` is a separate explicit plaintext opt-in.
  Runtime environment values, code, external libraries, locks and search caches
  are excluded. Backups are private data, not sanitized exports.
- Creation streams files, compares selected source membership/size/mtime before
  and after, then verifies the ZIP before publishing it under project `backups/`.
  It requires quiescent writers rather than claiming a filesystem-wide snapshot.
  The GUI refuses creation while background jobs are pending/running. CLI users
  must also stop concurrent writes. Archive limits bound file count and declared
  uncompressed size; transformed configuration/job JSON is additionally bounded.
- Restore targets only managed `restored/<name>` folders. Preview validates all
  archive paths/hashes and reports file-level keep/replace decisions against the
  migrated payload. Its token binds archive contents, destination tree contents,
  path and mode. Execution revalidates it and stages a complete destination copy;
  extra destination files are retained. Before publication, the archive and target
  are hashed again. The old directory is renamed to a retained `before` sibling.
- `config.yaml` output becomes relative `run`; job and batch configuration paths
  are relocated and version-state report paths remapped. Personal note strings
  are not rewritten. External connection settings remain as configured and are
  not executed by restoration. Missing optional report/PDF files are not fetched.
- A durable prepared/complete journal surrounds the directory handoff. If the
  second rename fails, the old directory is put back. After process interruption,
  explicit reconcile restores a missing target from `before`, or keeps an already
  published target; unpublished staging directories are retained for inspection.
  Windows rename retries handle transient sharing failures without rerunning the
  entire operation. This does not promise durability against all disk failures.
- A per-target update lock serializes restores. Standard restored GUI queues also
  hold an external `restored/.owners/<name>/.jobs/owner.lock`, preventing startup
  during publication and replacement while the queue is alive. This guard shares
  the main JobStore lifetime, including workers finishing after GUI shutdown.
  Non-GUI writers remain outside this coordination and must be stopped.
- The settings page exposes create/download/preview/restore/reconcile, with profile
  checks on mutations and no-store responses. Archive download is an explicit
  route; generic artifacts deny `backups`/`restored` components as well as private
  job/search data. CLI exposes the same service through `backup` subcommands.
  Restore never targets the live source installation or arbitrary existing folders.

### Explicit multi-profile scheduling

- `load_config(path, profile_id=...)` loads a named profile without modifying
  `profiles/active.txt`; missing/invalid explicit IDs fail rather than falling back
  to the active profile. Normal callers retain the existing active-profile behavior.
- `scheduler.py` stores a versioned master switch and per-profile time, timezone,
  weekdays, email opt-in and effective timestamp in `schedule-config.json`.
  Defaults are disabled. GUI lifespan starts a 30-second polling thread; CLI
  `schedule` shares it and `--once` waits for that tick's jobs to finish. The
  existing JobStore owner lock prevents simultaneous GUI/CLI dispatchers.
- Daily identities use profile plus local calendar date. Windows of up to two
  hours permit delayed dispatch; older slots are marked missed, prior dates are
  not backfilled, and slots before a save/enable timestamp are not eligible.
  DST gaps are skipped and folds choose the first instant. Editing a plan does
  not clear existing date identities. Queue start time may be later than dispatch.
- A schedule lock serializes ticks/settings edits. Intent is written to
  `schedule-state.json` before queue submission. Jobs use immutable
  `schedule:<profile>:<date>` identities and saved digest requests. Reconciliation
  links a published child after an interrupted linkage write; a reservation with
  no known job becomes interrupted without replay. Completed identities can also
  repair a missing schedule entry. State writes are skipped when unchanged.
- Each submission captures that profile's config and uses its scheduled timezone
  for the pipeline. Existing parallelism applies. An active digest of that profile
  delays dispatch within the grace window. Pause stops future submissions, not
  already-queued work. Failure/cancellation/interruption is never automatically
  retried, and manual digest recovery retains its no-email contract. Invalid or
  missing profiles surface errors without blocking healthy profiles.
- Schedule files join default backups and are blocked from generic artifact
  downloads. A restored project's existing marker blocks dispatch unless an
  explicit enable action records `scheduling_confirmed`; copied enabled settings
  alone cannot activate automation. This does not change external configuration.
- The Windows installer optionally replaces the same legacy task using
  `-MultiProfile`, checking `schedule --once` every five minutes with IgnoreNew.
  Legacy `run` and manual CLI commands are outside schedule identity deduplication;
  they must not be used as a parallel automatic entry point. No machine task is
  installed by application startup or development tests.

### Feature checklist

1. Put external API protocol details in a dedicated adapter module.
2. Keep profile-aware paths and durable formats in `storage.py` or a focused
   store class.
3. Add orchestration to the relevant pipeline or synthesizer, not to a template.
4. Expose the operation through a thin CLI command or web route.
5. Add an offline test for the success path and at least one failure/retry case.
6. Never add credentials, live user data, generated reports, or caches to a
   release archive.

## Compact report presentation

### Grounding chunk quotations before synthesis

`source_spans.ground_note_quotes` resolves continuous legacy chunk quotations
against the current physical page snapshot before final synthesis. Exact unique
locations become Q1 range IDs with the existing whole-page-text digest, page,
and character offsets. Fixed S1 windows are unchanged. NFKC/whitespace changes
are allowed; fuzzy matching, punctuation repair and cross-page joining are not.
Ambiguous or unmatched notes keep an explicit unavailable-evidence marker.
Precise ranges and fixed windows share the same 32k synthesis budget and formula/
page allocation. The final prompt requests IDs instead of requiring retyped
quotations. Chunk prompts and cached raw responses are unchanged.

`evidence.py` reconstructs Q1 ranges only for available full-report PDF page text,
validating digest, bounds and length. Numeric checking uses exactly the range,
not neighboring cells. Evidence metadata and comparison snapshots retain the
same source ID/page/offset/hash fields. Audit version 2 separates both table rows
and adjacent/nested list items, preventing evidence from another item being
borrowed. Neither range identity nor literal numeric presence proves semantic
attribution. See `NOTE_GROUNDING_2026-09-26.md` for the bounded live comparison.

### Presentation

`evidence.py` retains citation/number audit details in `evidence.json`, while
generated reading Markdown has only inline PDF references, actionable warnings
and a compact coverage summary. `comparison.py` links matrix references directly
to versioned papers, local PDFs or reports; frozen material and per-cell checks
remain in `sources.json` and `matrix.json`, without duplicating them in the body.

`report_presentation.compact_report` adapts the known generated appendices in
historical Markdown at HTML rendering and Obsidian export boundaries. It preserves
core sections, remaps old comparison anchors before dropping the excerpt section,
and ignores apparent section headings in fenced code. Historical source files
are not rewritten. Obsidian copies existing evidence JSON into profile/revision
attachments and rewrites detail links; absent legacy detail files get a plain
availability note instead of a broken link.

Disk HTML resolves packaged assets relative to its location (file URI for a
different Windows drive); HTTP `ReportStaticFiles` renders with `/static` URLs.
The existing HTML sanitizer and HTTP CSP remain in force. Disk reports depend on
the installed package assets and are not self-contained portable exports.

`markdown_rendering.py` defines the shared HTML dialect: markdown-it-py
CommonMark with tables and strikethrough. Lists may interrupt prose and nest
according to their marker indentation. Math is parsed as block/inline tokens,
so code spans and fences remain literal. The earlier table-boundary preprocessor
and whole-document math placeholder substitution have been retired. A narrow
table adapter preserves pipes inside code/math, terminates tables before plain
prose, and rejects extra cells instead of silently truncating them. Alignment
uses sanitized `align` attributes. Wide tables scroll within the article.

Before any caller publishes HTML, the common renderer checks for recognizable
unparsed bullet/table syntax in paragraphs, then compares parsed structural
counts and heading targets with sanitized HTML. `ReportRenderError` stops
publication before atomic replacement. Single-paper, weekly, comparison and
version reports all use this entry point; GUI/CLI callers receive the error.
Generation saves Markdown first, retaining it for diagnosis. Legacy HTTP views
return a readable 422 error and source link on a rendering check failure.
The tests inject parser/sanitizer failures and exercise the actual paper
pipeline, so a passing syntax-only unit test cannot stand in for publication
behavior. These checks do not assess semantic truth, validate all TeX commands,
or guarantee detection of every malformed Markdown dialect.

Heading IDs still use Python-Markdown's slug/uniqueness helper and the directory
links are generated from the same parsed tokens. Math-bearing heading IDs can
change because literal TeX replaces the old transient placeholder IDs; ordinary
headings and source/PDF links keep their existing conventions.

## Test commands

```powershell
$env:PYTHONPATH = (Resolve-Path "src").Path
python -m pytest -q -p no:cacheprovider --basetemp work\pytest-release
python -m compileall -q src tests
python -m pip check
```

Keep tests and reproducible fixtures in the Git repository. User source packages
omit tests and evaluation scripts; contributors should clone the repository.
Only maintained user and architecture documentation belongs in `docs/`.
UI QA captures, dated reviews, local evaluation results and planning notes belong
in ignored `work/`. Selected public UI screenshots live in `assets/screenshots/`.
Build products live under `release/<version>/`; temporary output belongs in
ignored `work/`, `build/`, or `dist/` directories.
