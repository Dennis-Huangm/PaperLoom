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

- [ ] Run `scripts\build_release.ps1`; do not hand-copy the source tree.
- [ ] Build wheel without network-dependent build isolation when necessary.
- [ ] Install the wheel into a clean or disposable environment.
- [ ] Run `arxiv-ra --version` and `arxiv-ra doctor`.
- [ ] Record SHA-256 hashes in `release/<version>/SHA256SUMS.txt`.
- [ ] Copy release notes and changelog into the release directory.
