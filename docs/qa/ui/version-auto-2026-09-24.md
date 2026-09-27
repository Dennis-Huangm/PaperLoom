# Automatic version sync acceptance

- Isolated fixture: `work/preview_version_auto.py`, localhost port 8771. Fake arXiv PDFs/metadata only; Zotero disabled, no model calls or real vault writes.
- Policy initially disabled; enabling with max 2 papers and saving starts no work.
- GUI version check discovers three v3 targets from saved v1 records, creates an automatic batch of two, and defers one.
- Injected second-paper download failure is visible in task warnings, batch details and paper status; first paper succeeds.
- Explicit batch continuation retries only the remaining paper; batch becomes 2/2 successful.
- Next check syncs the previously deferred third paper; update list becomes empty and automatic history shows 2/2 and 1/1 completed batches.
- Restarted a read-only server with `work/preview_auto_history.py`; policy and both batch records remain present.
- Visual checks at desktop 1280×900 and mobile 390×844; mobile DOM content width equals client width (375 px after scrollbar), no horizontal overflow. Labels, controls and save action readable.
- Temporary browser tab closed, viewport override reset, both temporary server processes stopped after command-line identity checks.

Validation: 231 tests passed (one existing Starlette/httpx deprecation warning), Python compilation passed. Test processes omit inherited SOCKS proxy environment variables; no system proxy settings changed. Offline tests include limits, actual model request counting including vision/compatibility retries, durable recovery, process loss, concurrent checks, profile isolation, invalid configurations and stale-profile saves.
