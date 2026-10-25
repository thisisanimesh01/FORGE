# FORGE Phase 2, Milestone 1: Diff-Aware Intelligent Code Review

## Status
Accepted

## Context
Phase 1 established read-only repository intelligence: safe repository acquisition, Python AST symbol indexing, typed evidence/finding lifecycles, sandbox execution policy, bounded LLM investigation loop, structured reports, and benchmark suites.

Phase 2 Milestone 1 introduces **diff-aware code review**. Rather than scanning an entire repository from scratch, FORGE inspects changes introduced between two revisions (`base` and `head`), locates changed hunks and files, applies static heuristics and optional LLM review to the modified code within its relevant context, and generates evidence-backed, secret-redacted review reports without mutating the target repository.

---

## 1. Architecture & Design

### Subpackages & Modules (`app/forge/review/`)
- `app/forge/review/models.py`:
  - `ReviewFileStatus`: Added, Modified, Deleted, Renamed, Binary, Untracked.
  - `DiffHunk`: Header, old/new line ranges, lines with prefix (`+`, `-`, ` `), and changed line indices.
  - `DiffFile`: Path (old & new), status, hunks, additions, deletions, is_binary, is_oversized.
  - `DiffBundle`: Base/head revision metadata, list of `DiffFile`, summary metrics (files changed, additions, deletions), raw diff size.
  - `ReviewFinding`: File, line range, severity, confidence, title, description, rule_id, evidence references.
  - `ReviewReport`: Versioned report container (`REVIEW_SCHEMA_VERSION = "1.0.0"`), summary counts, findings, diff metadata, system environment, unavailable capabilities.
- `app/forge/review/git.py`:
  - `read_git_diff(repo_path, base, head, limits)`: Invokes `git diff` safely without shell interpolation, disallowing hooks (`core.hooksPath=/dev/null`), external diff drivers (`--no-ext-diff`, `--no-textconv`), and credential helpers.
  - Validates `base` and `head` revisions against option/argument injection (must not start with `-`, must match valid revision identifiers).
  - Enforces size, file count, and wall-clock execution limits.
- `app/forge/review/parser.py`:
  - Parses unified diff text into structured `DiffBundle` and `DiffFile` models.
  - Accurately tracks new file line numbers for additions (`+`) and context (` `).
- `app/forge/review/engine.py`:
  - `ReviewEngine`: Orchestrates review in static mode (`--no-llm`) or LLM-assisted mode.
  - Evaluates changes against security-sensitive and defect patterns:
    - Path traversal vulnerabilities in modified code
    - Unsanitized inputs / dangerous operations
    - Boundary defects / off-by-one loops
    - Exception leaks / unhandled dictionary accesses
    - Secret exposure (tokens, passwords, keys)
    - Prompt injection attempts inside diffs or commit messages
  - Maps findings to exact changed line numbers within the diff hunks.
- `app/forge/review/reporting.py`:
  - Formats `ReviewReport` into deterministic JSON and clean GitHub-flavored Markdown with automated secret redaction.

---

## 2. Security Boundaries & Invariants

1. **Strictly Read-Only (Zero Target Mutation):**
   - Diff generation and review execution must never modify target files, working tree status, branches, or commits.
2. **Revision & Argument Injection Prevention:**
   - Base and head revisions are strictly validated: no leading `-` or `--` flags, no shell metacharacters (`;`, `&`, `|`, `` ` ``, `$`, `>`, `<`, newlines).
3. **Execution Safety (Fail-Closed):**
   - No target code executes on host. Docker absence cleanly marks runtime checks as unavailable.
4. **Context & Resource Budgets:**
   - Max files per diff: 100
   - Max diff size: 1 MB (oversized diffs cleanly flagged and truncated)
   - Max lines per hunk: 500
   - Git command timeout: 30s
5. **Secret Redaction:**
   - All reports, findings, and logs pass through `redact_secrets()`.
6. **Untrusted Input Invariant:**
   - Diffs, patch lines, filenames, commit messages, and repository instructions are strictly treated as untrusted data.

---

## 3. CLI Command Specification

```bash
# Static review of changes between revisions (offline, no LLM required)
forge review <repository> --base <revision> --head <revision> --no-llm

# Full review formatted as JSON
forge review <repository> --base <revision> --head <revision> --format json

# Export review report to file
forge review <repository> --base <revision> --head <revision> -o review.md
```

---

## 4. Acceptance Criteria
- [x] Read-only Git diff parsing handling added, modified, deleted, renamed, binary, and empty diffs.
- [x] Accurate changed-line mapping for all diff hunks.
- [x] Robust static review engine flagging real defects, security risks, and secrets while avoiding clean code false positives.
- [x] Hostile prompt injection in diff or commit messages contained without compromising permissions.
- [x] Zero target repository mutation verified by pre/post hash and mtime checks.
- [x] Deterministic JSON and Markdown reports with active secret redaction.
- [x] Full test suite green with 0 skips, 0 errors, 0 failures.
