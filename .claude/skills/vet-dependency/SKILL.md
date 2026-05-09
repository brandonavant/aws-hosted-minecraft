---
name: vet-dependency
description: Vet a third-party dependency before recommending it. Use when selecting, evaluating, or comparing any library, framework, service, GitHub Actions version, or pricing model. Runs registry scripts that return raw signals — no confabulation possible.
allowed-tools: Bash(python3 *), Read, Grep, Glob, WebSearch, WebFetch
argument-hint: <package-name> [--registry pypi|npm|gh-action]
---

# Vet Dependency

Run deterministic checks against package registries and security APIs before recommending any third-party dependency.
Scripts return structured JSON — interpret the signals in context rather than relying on training knowledge.

## When this skill applies

Any response that includes or relies on:

- A technology, library, or framework recommendation
- A version number for a dependency
- Pricing for a service or API
- A claim that something is "current," "recommended," or "best practice"
- A claim that something is deprecated, unmaintained, or end-of-life
- A comparison of competing tools or services
- A GitHub Actions `uses:` directive with a version tag

## Workflow

### Step 1 — Determine the registry

Identify the package ecosystem from context or the `--registry` argument:

| Registry       | Script                                     | Trigger examples                     |
|----------------|--------------------------------------------|--------------------------------------|
| PyPI           | `scripts/vet_pypi.py`                      | `pip install`, `pyproject.toml` dep  |
| npm            | `scripts/vet_npm.py`                       | `npm install`, `package.json` dep    |
| GitHub Actions | `scripts/vet_gh_action.py`                 | `uses:` directive in a workflow file |

### Step 2 — Run the vetting script

```bash
python3 ${CLAUDE_SKILL_DIR}/scripts/vet_<registry>.py <package-name>
```

The script outputs a JSON report to stdout. If the script does not yet exist, fall back to Step 2b.

#### Step 2b — Manual fallback (until scripts are implemented)

If the registry script is not yet available, perform live web research instead:

1. **Current version and maintenance status.** Search for the package by name + current year.
2. **Deprecation and end-of-life status.** Check official docs, changelogs, and community signals.
3. **Security posture.** Search for "[package] vulnerability" and "[package] supply chain attack".
4. **Maintainer health.** Check maintainer count, release recency, and bus factor.
5. **Alternatives.** Search for "[category] alternatives" + current year.
6. **GitHub Actions only:** Verify the major-version tag exists with `gh api repos/{owner}/{repo}/git/refs/tags --jq
   '.[].ref'` and check Node.js runtime compatibility.

Structure your findings as if they came from a script — present raw signals, not conclusions.

### Step 3 — Interpret the report

Read the JSON output and assess risk. The scripts surface raw signals; your job is to interpret them in the context of
the user's project.

**Red flags** (recommend against or flag prominently):

- Last release > 12 months ago with no maintenance announcement
- Single maintainer with no organizational backing
- Known unpatched CVEs
- Missing SECURITY.md or disclosure process
- Recent supply chain incident involving the package or its transitive dependencies

**Yellow flags** (note but don't necessarily block):

- Maintainer count <= 2
- No releases in 6-12 months
- High transitive dependency count
- Recent ownership transfer

**Green signals** (increase confidence):

- Active releases within last 3 months
- Multiple maintainers or organizational backing
- Security policy in place
- Low dependency count

### Step 4 — Present findings

Include in your response:

1. The key signals (version, last release, maintainer count, known issues)
2. Any red or yellow flags with context
3. Your recommendation with explicit reasoning
4. If web research returned conflicting or unclear information, say so — do not fill gaps silently

## Output format reference

Vetting scripts emit JSON with this structure:

```json
{
  "package": "<name>",
  "registry": "pypi | npm | gh-action",
  "signals": {
    "latest_version": "...",
    "last_release_date": "...",
    "maintainer_count": 0,
    "has_security_policy": false,
    "known_vulnerabilities": [],
    "dependency_count": 0
  },
  "errors": [],
  "timestamp": "..."
}
```

Fields vary by registry. See each script's `--help` for the full schema.
