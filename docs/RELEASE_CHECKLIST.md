# Release checklist

## Code and behavior

- [ ] Full offline test suite passes.
- [ ] `compileall` and `pip check` pass.
- [ ] Dashboard, library, reports, weekly, profiles, settings, and status pages
      return successfully with an existing user configuration.
- [ ] A background job returns an HTML result URL when HTML exists.
- [ ] Same-day results remain independently readable for two profiles.
- [ ] Failed requested email delivery does not commit deduplication state.
- [ ] Obsidian full sync reads the active profile's daily files.

## Data safety

- [ ] Back up the destination application's source before updating it.
- [ ] Preserve `.env`, `config.yaml`, `profiles/`, and `run/`.
- [ ] Confirm source archives contain none of those paths.
- [ ] Confirm no credential values appear in package files or logs.

## Packaging

- [ ] Prepare `.venv` with `scripts\setup_environment.ps1 -Dev`, then run
      `scripts\build_release.ps1`; do not hand-copy the source tree.
- [ ] Match `pyproject.toml`, `arxiv_ra.__version__`, build default and release notes.
- [ ] Build with the project `.venv`; verify runtime source and packaged assets.
- [ ] Keep tests and evaluation tools in Git, excluding them from the user ZIP.
- [ ] Use the explicit documentation/script allowlist; exclude local review logs.
- [ ] Check README screenshot assets and all relative documentation links.
- [ ] For a local packaging check use `-OutputDirectory work/package-preview`;
      do not replace an already published release or move its tag for doc edits.
- [ ] Build wheel without network-dependent build isolation when necessary.
- [ ] Install the wheel into a clean or disposable environment.
- [ ] Run `paperloom --version` and `paperloom doctor`; verify the legacy
      `arxiv-ra --version` entry point still works.
- [ ] Record SHA-256 hashes in `release/<version>/SHA256SUMS.txt`.
- [ ] Copy release notes and changelog into the release directory.
- [ ] Compare staged files and package contents; scan both for local credentials.
- [ ] Push the reviewed commit and matching tag, then verify published asset hashes.
