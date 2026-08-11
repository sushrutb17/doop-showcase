## Summary

Describe the problem and the smallest bounded change.

## Evidence and scope

- Claim, invariant, or evidence entry affected:
- Publicly reproducible evidence added or updated:
- Explicitly out of scope:

## Verification

List the commands run and their results. Explain any genuinely inapplicable
check.

- [ ] `python3 tools/privacy_check.py`
- [ ] `uv run ruff check src tests tools`
- [ ] `uv run ruff format --check src tests tools`
- [ ] `uv run mypy src/whoop_local`
- [ ] `uv run pytest --cov=whoop_local --cov-report=term-missing -q`
- [ ] `uv run pip-audit --local --progress-spinner=off`
- [ ] `node --test integrations/openclaw/plugins/doop-safety-gate/test/*.test.js`

## Privacy and publication boundary

- [ ] Every fixture, date, identifier, profile, packet, screenshot, and log in
      this pull request is synthetic or generated for this repository.
- [ ] No database, export, real capture, health/workout record, demographic
      field, credential, device/chat identifier, owner-linked date, local path,
      or private runtime output appears in any commit.
- [ ] I reviewed the complete branch history, not only the final diff.
- [ ] My commit and tag identity uses my public GitHub handle as `user.name`
      and a GitHub noreply address as `user.email`.
- [ ] Third-party material has compatible redistribution terms and is recorded
      in the notices/provenance material where required.
- [ ] Public issue and pull-request text and attachments meet the same privacy
      boundary as repository files.

## Claim and safety review

- [ ] The change does not imply WHOOP/Garmin parity, medical validity, ground
      truth, a trained model, or protocol discovery without prior art.
- [ ] Recovery, strain, sleep, and training outputs remain identified as
      independent engineering proxies where relevant.
- [ ] Safety, confidence, and explicit-confirmation boundaries are preserved or
      their deliberate change is called out for review.
- [ ] Limitations and negative results remain visible rather than being
      smoothed over.
