# Ground External API Code in Real Responses

When writing code that parses, extracts from, or depends on the shape of an external API response — including test
fixtures for such code — you MUST sample real responses before implementation.

## What counts as an external API

Any HTTP endpoint not owned by this project: PyPI, npm registry, GitHub API, OSV.dev, cloud provider APIs, third-party
SaaS APIs, etc.

## Required steps

1. **Fetch 3–5 real responses** with diverse inputs (different packages, orgs, edge cases — not just the happy-path
   example from the issue or docs).
2. **Map the full response structure across all sampled responses.** For each response, dump all top-level keys and
   explore nested objects — do not limit inspection to the fields mentioned in the issue or docs. Compare the structures
   across responses to identify which fields are always present, which are optional, and which contain the same data in
   different locations. The data you need may live in a section you did not expect (e.g. `ownership.roles` vs.
   `info.author`).
3. **Note structural variance**: unexpected keys, inconsistent ordering, optional fields, URL formats that differ across
   entries (e.g. sponsor links vs. repo links in the same dict).
4. **Use e2e tests for integration testing, not fabricated fixtures.** Scripts that parse external API responses must
   have e2e tests that hit real APIs (marked with `@pytest.mark.e2e`, skipped by default, run with `--run-e2e`). Do not
   mock full API responses for integration tests — even fixtures based on real data drift from reality and give false
   confidence. Reserve mocks for error/edge-case paths that cannot be triggered against real services (e.g., 404 errors,
   all-yanked packages). Unit tests for pure extraction logic (regex parsing, fallback chains) may use hand-crafted
   inputs since they test code branching, not API shape.

## Why

Fabricated fixtures encode assumptions about data shape. When those assumptions are wrong, tests pass against imagined
data while the code fails on real data. In `vet_pypi.py`, the code only checked `info.maintainer` and `info.author`
(both `None` for pydantic) because those were the fields the issue spec listed. The real maintainer list was under
`ownership.roles` — a top-level key the implementation never examined.
