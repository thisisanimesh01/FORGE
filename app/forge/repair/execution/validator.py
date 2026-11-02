"""Phase 8 Repair Execution Architecture — Request and Patch Validators.

Implements strict validation for RepairRequests and candidate PatchArtifacts:
- RepairRequestValidator: validates finding existence, lifecycle state, provenance,
  evidence integrity, authorized file scope, and workspace boundary.
- PatchValidator: validates unified diff syntax, hunk headers, context matching,
  scope containment, protected file rules, AST syntax, and semantic invariants.
"""

import ast
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from forge.repair.execution.failures import (
    CommitMismatchError,
    InsufficientEvidenceError,
    PatchValidationError,
    RepairRequestValidationError,
    ScopeContainmentViolationError,
    StaleBaseRevisionError,
    WorkspaceIntegrityViolationError,
)
from forge.repair.execution.interfaces import PatchValidatorProtocol, RepairWorkspaceProtocol
from forge.repair.execution.models import (
    PatchArtifact,
    RepairRequest,
    ValidationResult,
)
from forge.repair.execution.policy import RepairExecutionPolicy
from forge.repair.semantic_verifier import analyze_python_ast_semantics
from forge.schemas import EvidenceRecord, Finding, FindingLifecycleState
from forge.storage.database import get_db_connection
from forge.storage.store import ForgeStore


class RepairRequestValidator:
    """Validates repair requests against stored findings, evidence, policy, and workspace state."""

    def __init__(self, store: Optional[ForgeStore] = None, db_path: Optional[Path] = None):
        self.store = store or ForgeStore(db_path=db_path)

    def validate_request(
        self,
        request: RepairRequest,
        workspace: RepairWorkspaceProtocol,
        policy: Optional[RepairExecutionPolicy] = None,
    ) -> Tuple[Finding, List[EvidenceRecord]]:
        """Validate all preconditions for a repair request before planning or patch generation.

        Raises:
            RepairRequestValidationError: If finding missing, invalid state, or mismatched target.
            InsufficientEvidenceError: If evidence records are missing or empty.
            StaleBaseRevisionError: If repository or evidence commit does not match request.
            ScopeContainmentViolationError: If authorized files violate containment or protected rules.
            WorkspaceIntegrityViolationError: If workspace integrity checks fail.

        Returns:
            Tuple of (Finding, List[EvidenceRecord])
        """
        active_policy = policy or RepairExecutionPolicy()

        # 1. Finding existence
        finding = self.store.get_finding(request.finding_id)
        if not finding:
            raise RepairRequestValidationError(
                f"Finding '{request.finding_id}' does not exist in store",
                details={"finding_id": request.finding_id},
            )

        # 2. Finding eligible validated state
        # In Phase 8, finding must be an active problem (suspected, reproduced, or root_caused)
        eligible_states = {
            FindingLifecycleState.SUSPECTED,
            FindingLifecycleState.REPRODUCED,
            FindingLifecycleState.ROOT_CAUSED,
        }
        if finding.lifecycle_state not in eligible_states:
            raise RepairRequestValidationError(
                f"Finding '{request.finding_id}' is in lifecycle state '{finding.lifecycle_state.value}', "
                f"which is not eligible for candidate repair generation (expected one of: {[s.value for s in eligible_states]})",
                details={"finding_id": request.finding_id, "state": finding.lifecycle_state.value},
            )

        # 3. Target repository & commit SHA matching against run if available
        if finding.run_id:
            run = self.store.get_run(finding.run_id)
            if run:
                # Compare repository target
                run_target = run.target.strip()
                req_target = str(request.target_repo).strip()
                if run_target and req_target:
                    # Normalize comparison (handling trailing slashes and .git)
                    norm_run = run_target.rstrip("/").removesuffix(".git")
                    norm_req = req_target.rstrip("/").removesuffix(".git")
                    if norm_run != norm_req and not norm_req.endswith(norm_run) and not norm_run.endswith(norm_req):
                        raise RepairRequestValidationError(
                            f"Finding repository target '{run.target}' does not match repair target '{request.target_repo}'",
                            details={"run_target": run.target, "request_target": request.target_repo},
                        )

        # 4. Evidence records validation
        if not request.evidence_ids:
            raise InsufficientEvidenceError(
                f"Repair request '{request.repair_id}' has no associated evidence record IDs",
                details={"repair_id": request.repair_id},
            )

        evidence_records: List[EvidenceRecord] = []
        for evi_id in request.evidence_ids:
            ev = self.store.get_evidence(evi_id)
            if not ev:
                raise InsufficientEvidenceError(
                    f"Referenced evidence record '{evi_id}' does not exist in store",
                    details={"evidence_id": evi_id},
                )

            # Check evidence provenance if details contain commit or repository target
            ev_commit = ev.details.get("commit_sha")
            if ev_commit:
                clean_ev_commit = str(ev_commit).strip().lower()
                clean_base_commit = request.base_commit_sha.strip().lower()
                if not (clean_ev_commit.startswith(clean_base_commit) or clean_base_commit.startswith(clean_ev_commit)):
                    raise StaleBaseRevisionError(
                        f"Evidence record '{evi_id}' commit '{clean_ev_commit}' does not match "
                        f"repair base commit SHA '{clean_base_commit}'",
                        details={"evidence_commit": clean_ev_commit, "base_commit_sha": clean_base_commit},
                    )

            # Check evidence file path existence in workspace
            if ev.file_path:
                try:
                    contained_path = workspace.validate_containment(ev.file_path)
                except Exception as exc:
                    raise ScopeContainmentViolationError(
                        f"Evidence file path '{ev.file_path}' escapes workspace boundary: {exc}",
                        details={"evidence_id": evi_id, "file_path": ev.file_path},
                    ) from exc

                if not contained_path.exists():
                    raise RepairRequestValidationError(
                        f"Evidence file path '{ev.file_path}' does not exist in workspace at base revision",
                        details={"evidence_id": evi_id, "file_path": ev.file_path},
                    )

            evidence_records.append(ev)

        # 5. Authorized files compliance with policy
        if not request.allowed_files:
            raise RepairRequestValidationError(
                f"Repair request '{request.repair_id}' has no allowed_files specified",
                details={"repair_id": request.repair_id},
            )

        for f in request.allowed_files:
            active_policy.validate_target_file(f, request.allowed_files)
            target_path = workspace.validate_containment(f)
            if not target_path.exists():
                raise RepairRequestValidationError(
                    f"Authorized file '{f}' does not exist in workspace",
                    details={"file": f},
                )

        # 6. Workspace identity check
        if request.workspace_id and request.workspace_id != workspace.workspace_id:
            raise RepairRequestValidationError(
                f"Workspace ID mismatch: request specifies '{request.workspace_id}', "
                f"but provided workspace is '{workspace.workspace_id}'",
                details={"request_workspace_id": request.workspace_id, "workspace_id": workspace.workspace_id},
            )

        # 7. Workspace integrity verification
        workspace.check_integrity()

        # 8. Target repository immutability verification
        if not workspace.check_target_immutability():
            raise StaleBaseRevisionError(
                "Target repository was modified; repair request cannot proceed against a dirty target",
            )

        return finding, evidence_records


class PatchValidator(PatchValidatorProtocol):
    """Validates candidate patch artifacts against boundaries, budgets, syntax, and semantic invariants."""

    def validate_patch(
        self,
        patch: PatchArtifact,
        request: RepairRequest,
        policy: RepairExecutionPolicy,
        workspace: RepairWorkspaceProtocol,
    ) -> ValidationResult:
        """Verify that candidate patch respects scope, budget, hunk consistency, and syntax rules."""
        violations: List[str] = []
        checks_passed: List[str] = []

        # 1. Content Hash Verification
        diff_bytes = patch.diff_content.encode("utf-8")
        computed_digest = hashlib.sha256(diff_bytes).hexdigest()
        if computed_digest != patch.content_hash:
            violations.append(
                f"Content hash mismatch: declared '{patch.content_hash}' does not match "
                f"actual diff SHA-256 digest '{computed_digest}'"
            )
        else:
            checks_passed.append("content_hash_verified")

        # 2. Diff Syntax & Headers
        raw_diff = patch.diff_content
        file_header_pattern = re.compile(r"^---\s+[a/]*(.*?)\n\+\+\+\s+[b/]*(.*?)$", re.MULTILINE)
        headers = list(file_header_pattern.finditer(raw_diff))
        if not headers:
            violations.append("Malformed diff: missing file headers ('--- a/...' and '+++ b/...')")
        else:
            checks_passed.append("diff_headers_present")

        hunk_header_pattern = re.compile(
            r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@",
            re.MULTILINE,
        )
        hunks = list(hunk_header_pattern.finditer(raw_diff))
        if not hunks:
            violations.append("Malformed diff: no @@ hunk headers detected")
        else:
            checks_passed.append("hunk_headers_present")

        # 3. Path Normalization, Scope Containment, and Binary Patches
        touched_files: Set[str] = set()
        for h in headers:
            old_f = h.group(1).strip()
            new_f = h.group(2).strip()

            for raw_f in (old_f, new_f):
                clean_f = raw_f
                if clean_f.startswith("a/") or clean_f.startswith("b/"):
                    clean_f = clean_f[2:]
                if clean_f and clean_f != "/dev/null":
                    touched_files.add(Path(clean_f).as_posix())

        if "GIT binary patch" in raw_diff or "Binary files" in raw_diff:
            violations.append("Binary patches are strictly forbidden")

        # Check for unexpected file creation or deletion
        for h in headers:
            old_f = h.group(1).strip()
            new_f = h.group(2).strip()
            if old_f in ("/dev/null", "a//dev/null"):
                violations.append(f"Unexpected file creation forbidden in '{new_f}'")
            if new_f in ("/dev/null", "b//dev/null"):
                violations.append(f"Unexpected file deletion forbidden in '{old_f}'")

        # Check single file scope
        if len(touched_files) > policy.max_files_modified:
            violations.append(
                f"Patch modifies {len(touched_files)} files, exceeding budget max of {policy.max_files_modified}"
            )

        # Check authorized files containment
        allowed_set = {Path(f).as_posix() for f in request.allowed_files}
        for tf in touched_files:
            if ".." in tf.split("/"):
                violations.append(f"Path traversal ('..') forbidden in patch target '{tf}'")
            if tf.startswith("/") or tf.startswith("\\"):
                violations.append(f"Absolute paths forbidden in patch target '{tf}'")
            if tf not in allowed_set:
                violations.append(
                    f"Target file '{tf}' is not within declared allowed scope: {sorted(allowed_set)}"
                )
            try:
                policy.validate_target_file(tf, request.allowed_files)
                workspace.validate_containment(tf)
                checks_passed.append(f"scope_contained:{tf}")
            except Exception as e:
                violations.append(f"Policy validation failed for '{tf}': {e}")

        # 4. Resource and Budget Caps
        added_lines = 0
        deleted_lines = 0
        for line in raw_diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added_lines += 1
            elif line.startswith("-") and not line.startswith("---"):
                deleted_lines += 1

        total_lines_changed = added_lines + deleted_lines
        if total_lines_changed > policy.max_lines_changed:
            violations.append(
                f"Total changed lines ({total_lines_changed}) exceeds policy max of {policy.max_lines_changed}"
            )
        else:
            checks_passed.append("line_budget_satisfied")

        if len(hunks) > policy.max_hunks:
            violations.append(
                f"Hunk count ({len(hunks)}) exceeds policy max of {policy.max_hunks}"
            )
        else:
            checks_passed.append("hunk_budget_satisfied")

        if len(diff_bytes) > policy.max_patch_bytes:
            violations.append(
                f"Patch size ({len(diff_bytes)} bytes) exceeds policy max of {policy.max_patch_bytes} bytes"
            )
        else:
            checks_passed.append("byte_budget_satisfied")

        if len(hunks) > 0 and total_lines_changed == 0:
            violations.append("Patch candidate contains no additions or deletions (no-op diff)")

        # 5. Hunk Line Markers and Context Matching against Workspace
        if not violations and touched_files:
            target_rel = list(touched_files)[0]
            target_disk = workspace.workspace_path / target_rel
            if not target_disk.is_file():
                violations.append(f"Target file '{target_rel}' does not exist in workspace")
            else:
                orig_text = target_disk.read_text(encoding="utf-8", errors="replace")
                orig_lines = orig_text.splitlines(keepends=True)

                # Parse and match hunks
                hunk_body_pattern = re.compile(
                    r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@[^\n]*\n([\s\S]*?)(?=^@@|\Z)",
                    re.MULTILINE,
                )
                hunk_blocks = []
                for m in hunk_body_pattern.finditer(raw_diff):
                    old_start = int(m.group(1))
                    old_len = int(m.group(2)) if m.group(2) is not None else 1
                    new_start = int(m.group(3))
                    new_len = int(m.group(4)) if m.group(4) is not None else 1
                    hunk_blocks.append((old_start, old_len, new_start, new_len, m.group(5)))

                # Sort descending for in-memory splicing check
                hunk_blocks.sort(key=lambda x: x[0], reverse=True)
                current_lines = list(orig_lines)
                context_matched = True

                for old_start, old_len, new_start, new_len, hunk_body in hunk_blocks:
                    body_lines = hunk_body.splitlines()
                    expected_old: List[str] = []
                    replacement_new: List[str] = []

                    for bline in body_lines:
                        if bline.startswith("-") and not bline.startswith("---"):
                            expected_old.append(bline[1:])
                        elif bline.startswith("+") and not bline.startswith("+++"):
                            replacement_new.append(bline[1:])
                        elif bline.startswith(" "):
                            expected_old.append(bline[1:])
                            replacement_new.append(bline[1:])
                        elif bline == "":
                            expected_old.append("")
                            replacement_new.append("")
                        elif not bline.startswith("\\"):
                            violations.append(
                                f"Malformed diff line in hunk at line {old_start}: {bline[:60]!r}"
                            )
                            context_matched = False
                            break

                    if not context_matched:
                        break

                    target_idx = old_start - 1
                    match_idx = None
                    search_window = [target_idx] + [
                        target_idx + d for d in [1, -1, 2, -2, 3, -3, 4, -4, 5, -5]
                    ]

                    for cand_idx in search_window:
                        if cand_idx < 0 or cand_idx + len(expected_old) > len(current_lines):
                            continue
                        matches_cand = True
                        for k, exp in enumerate(expected_old):
                            actual = current_lines[cand_idx + k].rstrip("\r\n")
                            if actual != exp.rstrip("\r\n"):
                                matches_cand = False
                                break
                        if matches_cand:
                            match_idx = cand_idx
                            break

                    if match_idx is None:
                        violations.append(
                            f"Patch hunk context mismatch around line {old_start} in '{target_rel}'"
                        )
                        context_matched = False
                        break

                    formatted_new = [
                        l if l.endswith("\n") else f"{l}\n" for l in replacement_new
                    ]
                    current_lines[match_idx : match_idx + len(expected_old)] = formatted_new

                if context_matched:
                    checks_passed.append("context_matched")
                    patched_text = "".join(current_lines)

                    # 6. Python AST Syntax Check
                    if target_rel.endswith(".py"):
                        try:
                            ast.parse(patched_text)
                            checks_passed.append("python_ast_valid")
                        except SyntaxError as syn_err:
                            violations.append(f"Patched Python code contains syntax error: {syn_err}")

                        # 7. Semantic Invariant Analysis
                        sem_res = analyze_python_ast_semantics(
                            original_source=orig_text,
                            patched_source=patched_text,
                            file_path=target_rel,
                        )
                        if not sem_res.is_valid:
                            for v in sem_res.violations:
                                violations.append(f"Semantic invariant violation: {v}")
                        else:
                            checks_passed.append("semantic_invariants_preserved")

        is_valid = len(violations) == 0
        return ValidationResult(
            is_valid=is_valid,
            violations=violations,
            checks_passed=checks_passed,
            validated_at=datetime.now(timezone.utc),
        )
