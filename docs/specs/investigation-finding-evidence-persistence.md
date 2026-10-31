# Investigation Finding & Evidence Persistence Specification

## 1. Background & Problem Statement

In real-world repository audits (e.g. against `https://github.com/apache/hamilton.git`, `run_1ada6eb20587`), the investigation engine successfully parsed and dispatched read-only tools across 5 turns, but terminated with `MAX_STEPS_EXCEEDED` and zero recorded findings or evidence.

A deep architectural audit identified four core deficiencies:
1. **Absence of Finding-Creation Tools**: The tool registry provided exploration tools (`read_source_excerpt`, `search_symbols`, `list_directory`), but no tool existed for the model to convert observations into structured findings.
2. **Missing Static Evidence Persistence**: Static tools returned text to the model but did not persist verified `EvidenceRecord` entries in `ForgeStore`.
3. **Turn-Budget Blindness**: The agent had no visibility into its remaining turns, reading consecutive slices of the same file until the step budget was abruptly exhausted without a synthesis turn.
4. **Coarse Loop Detection**: Repetition tracking only checked identical arguments, failing to detect sequential pagination through the same file.

---

## 2. Architecture & Design Principles

### A. Phase 1 Audit-Only Invariant
- FORGE strictly investigates repositories. Repository source files are **never** modified.
- Finding creation enters the **`suspected`** lifecycle state (`FindingLifecycleState.SUSPECTED`). Transition to `verified` or repair states during autonomous investigation is strictly forbidden.

### B. Independent Evidence Verification (Zero Model Trust)
Model assertions and model-supplied source excerpts are **never** trusted as proof:
1. The target file is validated for directory containment within the acquired repository workspace using `normalize_relative_path` and `assert_contained_path`.
2. Absolute paths, directory traversals (`../`), and escaping symlinks are rejected with `SecurityError` or `ToolExecutionError`.
3. The server independently reads the real file from disk, verifies that the requested line range exists, and extracts the actual line-numbered source.
4. An SHA-256 integrity digest of the raw source excerpt is computed and persisted in `evidence.details["content_digest"]`.
5. The `EvidenceRecord` (type `SOURCE_EXCERPT`, provenance `forge:investigator:source_verification`) is persisted in SQLite and linked to the finding's `finding_id`.

---

## 3. Tool Specification: `record_finding`

### Parameter Schema
```json
{
  "type": "object",
  "properties": {
    "title": {
      "type": "string",
      "description": "Concise summary title of the suspected defect"
    },
    "description": {
      "type": "string",
      "description": "Detailed explanation of why this code is defective or vulnerable"
    },
    "affected_file": {
      "type": "string",
      "description": "Repository-relative file path containing the defective code"
    },
    "start_line": {
      "type": "integer",
      "minimum": 1,
      "description": "Starting line number of the relevant code excerpt (1-indexed)"
    },
    "end_line": {
      "type": "integer",
      "minimum": 1,
      "description": "Ending line number of the relevant code excerpt (inclusive)"
    },
    "severity": {
      "type": "string",
      "enum": ["critical", "high", "medium", "low", "info"],
      "default": "medium"
    },
    "confidence": {
      "type": "string",
      "enum": ["confirmed", "high", "medium", "low"],
      "default": "medium"
    },
    "defect_category": {
      "type": "string",
      "description": "Optional category (e.g. 'syntax_error', 'logic_bug', 'security', 'resource_leak')"
    },
    "suggested_step": {
      "type": "string",
      "description": "Optional suggested next verification step"
    },
    "limitations": {
      "type": "string",
      "description": "Optional known uncertainties or limitations regarding this finding"
    }
  },
  "required": ["title", "description", "affected_file", "start_line", "end_line"]
}
```

### Handler Lifecycle
1. Validates `title` and `description` are non-empty.
2. Validates `severity` and `confidence` against canonical enums.
3. Normalizes path and asserts workspace containment.
4. Reads source from disk and verifies `1 <= start_line <= end_line <= total_lines`.
5. Computes SHA-256 digest of verified lines.
6. Calls `store.create_finding()` with `lifecycle_state=SUSPECTED`.
7. Calls `store.attach_evidence()` with verified source excerpt and digest.
8. Returns structured JSON confirmation including `finding_id`, `evidence_id`, and `content_digest`.

---

## 4. Turn-Budget Awareness & Synthesis Controls

1. **Step Budget Injection**: Initial user message informs the model of the total step budget (`[Turn Budget: Step 1 of N]`).
2. **Penultimate Turn Warning**: When `steps_executed == max_steps - 1`, a high-priority warning informs the model that only 1 turn remains and prompts it to record findings.
3. **Final Turn Tool Scoping**: On the final step (`steps_executed == max_steps`), exploration tools (`read_source_excerpt`, `search_symbols`, `list_directory`, etc.) are omitted from the request tools. Only `record_finding` is permitted, preventing the agent from exhausting its budget on reading more files.
4. **Truthful Outcome Accounting**:
   - Investigations with recorded findings conclude with `status="completed"` and `findings_count > 0`.
   - Investigations with full tool exploration and a clean assessment conclude with `status="completed"` and `findings_count = 0`.
   - Investigations that executed zero tools and found zero defects are marked `status="failed"`.
   - Investigations where the model continuously requests tools without producing synthesis conclude with `status="max_steps_exceeded"`.

---

## 5. Same-File Pagination Protection

To prevent compact models from burning turn budgets reading successive chunks of a single file:
- Sequential reads of the same file path are tracked via `self.file_read_counts`.
- If `read_count >= 3`, a guidance warning is appended to the excerpt output:
  `"[Warning: Sequential source reading detected across {read_count} turns for '{file}'. If defects were identified, use 'record_finding' to document them with verified evidence. Otherwise, proceed to inspect other modules or synthesize your findings.]"`
- If `read_count > 4`, further reading of that file is halted to preserve the remaining turns for synthesis.

---

## 6. Context Truncation Hygiene

When conversation history exceeds `max_context_chars`:
- Message 0 (System prompt) and Message 1 (User prompt with Repository Architecture map) are unconditionally preserved.
- Slicing proceeds backwards from the most recent message, grouping each `MessageRole.TOOL` response with its preceding `MessageRole.ASSISTANT` containing `tool_calls`.
- Mid-conversation truncation notices use `MessageRole.USER` rather than `MessageRole.SYSTEM` to preserve chat template compatibility with compact local models.

---

## 7. Deterministic Evidence-to-Claim Validation

To eliminate false-positive findings (such as those observed in the Apache Hamilton audit where valid dictionary entries were claimed to have syntax errors or classified as loops):

### A. Python Static AST Syntax Analysis
- If a finding alleges a syntax defect (`syntax_error`, or title/description alleging "missing colon", "syntax error", "unexpected token"), the target file is parsed using in-memory Python `ast.parse`.
- If `ast.parse` succeeds without errors:
  - The source file is provably, deterministically valid Python syntax.
  - The claim is rejected with `SyntaxClaimValidationError`, preventing false positive findings from being recorded.
- If `ast.parse` fails with `SyntaxError`:
  - The syntax error line number (`err_line`) is compared against the cited line range `[start_line, end_line]`.
  - If the cited range does not cover or localize the actual syntax error, the finding is rejected with `InsufficientEvidenceError`.
- For specific "missing colon" claims, cited lines are inspected: if the line already contains a colon (e.g. `"key": value,`), the allegation is rejected as unsupported.

### B. Defect Category & Context Consistency
- Category and description consistency: If `defect_category` is "loop" or the title claims a loop defect, but the description describes a syntax error ("missing colon"), the finding is rejected with `ConsistencyValidationError`.
- Evidence context relevance: If a loop defect is claimed, the cited excerpt (or its enclosing block in the AST) must contain an actual loop construct (`for`, `while`, or recursion). Ordinary dictionary mappings or assignments without loop constructs are rejected with `InsufficientEvidenceError`.
- Non-empty content: Source excerpts must contain non-whitespace executable statements.

### C. Uncertainty Preservation for Semantic Claims
- Static analysis cannot prove or disprove semantic logic bugs without execution.
- FORGE preserves explicit uncertainty: findings remain in `suspected` state, unearned `confirmed` confidence is downgraded to `high`, and `limitations` disclosures are automatically attached.
- Target repository code is never executed on the host.

---

## 8. Deterministic Duplicate Detection & Linking Policy

To prevent repetitive finding records across turns:
- **Duplicate Matching Criteria**:
  1. Identical normalized target file path.
  2. Overlapping or identical line coordinates (`max(s1, s2) <= min(e1, e2)`).
  3. Matching canonical defect category or description token Jaccard similarity $\ge 0.5$.
  4. Identical or near-identical descriptions ($\ge 0.65$ similarity) even if the model altered the category string.
- **Handling Policy**:
  - *Identical Evidence*: If the candidate finding's excerpt has the exact same content digest and line range as an existing finding, creation of a redundant finding is prevented (`DuplicateFindingError`).
  - *Additional Evidence*: If the candidate finding provides a new or expanded line slice for the same defect, the new evidence is linked to the existing finding (`status="duplicate_linked"`), without creating a duplicate finding row.
- **Auditable Ledger**: Rejections and duplicate events are recorded in SQLite via `EventType.FINDING_VALIDATION_FAILED` and `EventType.FINDING_DUPLICATE_DETECTED`.
