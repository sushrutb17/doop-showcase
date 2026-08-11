# v1.0 release candidate

Status: source and evidence candidate accepted; the privacy-reviewed
demonstration video and annotated `v1.0` tag remain pending.

The `v1.0` tag will identify the completed portfolio artifact. The installable
Python package remains version `0.1.0` because its runtime API and device
compatibility are still experimental.

## Release-candidate acceptance

The rebuilt candidate passed its clean-clone release gate with no database, wearable,
credentials, private runtime, or owner profile:

- locked all-extra installation from `uv.lock` (109 packages resolved);
- privacy gate: 150 clean-clone files scanned;
- Ruff lint passed and formatting was clean across 96 Python files in the CI scope;
- mypy passed across 58 source files;
- Python: 305 tests passed;
- branch coverage: 65.61%, above the 65% release floor;
- OpenClaw safety plugin: 12 tests passed;
- dependency audit: no known vulnerabilities; the unpublished local package
  was the only expected unauditable entry;
- wheel and source distribution contents were checked for the packaged safety
  resource, Apache license, and third-party notices;
- the wheel installed into an isolated Python 3.11 environment and all five
  console commands passed their `--help` smoke tests.

A pre-release privacy review found that the earlier showcase history did not
meet the repository's stated boundary. That repository was made private. This
candidate is rebuilt from an allowlisted tree with fresh Git history, a GitHub
noreply identity, synthetic fixtures, and worktree/history/tag privacy checks.
No private value is repeated in this record.

## Published artifacts

- Repository: <https://github.com/sushrutb17/doop-showcase>
- Project overview: <https://doop-showcase.vercel.app>
- Reviewer path: `REVIEWER_CHECKLIST.md`
- Reverse-engineering narrative: `CASE_STUDY.md`
- Raw-to-coaching trace: `evidence/STATIC_TRACE.md`
- Agent evidence and revocations: `integrations/openclaw/ACCEPTANCE.md`
- Demonstration contract: `VIDEO_STORYBOARD.md`

## Before tagging

- [ ] Capture the four synthetic BROC beats.
- [ ] Review every video frame against `VIDEO_STORYBOARD.md`.
- [ ] Attach or link the accepted 3–5 minute video.
- [ ] Repeat the final clean-clone gate if the published tree changes.
- [ ] Create and push the annotated `v1.0` tag and GitHub release.
