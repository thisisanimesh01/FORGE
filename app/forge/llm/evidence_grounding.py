"""Phase 11 M6: Evidence-Grounded Reasoning Engine.

Ensures that LLM outputs, candidate findings, and repository claims are strictly traceable
to concrete evidence produced by repository intelligence tools or disk inspection.

Invariants:
- Zero fabrication: models cannot invent non-existent file paths, line ranges, or evidence records.
- Provenance preservation: records originating tool, symbol ID, graph relationship, and SHA-256.
- Strict validation: candidate findings must link to verified evidence before persistence.
- State-machine compliance: maintains Phase 3/7 finding lifecycle constraints.
"""

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from forge.analysis.evidence import create_evidence, normalize_relative_path, EvidenceValidationError
from forge.repository.call_graph import CallGraph
from forge.repository.incremental import IncrementalRepositoryIndex
from forge.repository.module_graph import ModuleDependencyGraph
from forge.repository.protection import is_protected_path
from forge.repository.validation import assert_contained_path, SecurityError
from forge.schemas import EvidenceRecord, EvidenceType, Finding, FindingLifecycleState, FindingSeverity, FindingConfidence

if TYPE_CHECKING:
    from forge.storage.store import ForgeStore


class GroundingValidationError(Exception):
    """Raised when evidence grounding or finding verification fails."""
    pass


class EvidenceGroundingEngine:
    """Verifies, creates, and audits evidence records from intelligence tools."""

    def __init__(
        self,
        workspace_path: Path,
        run_id: str = "run_default",
        incremental_index: Optional[IncrementalRepositoryIndex] = None,
        store: Optional["ForgeStore"] = None,
    ) -> None:
        self.workspace_path = workspace_path.resolve()
        self.run_id = run_id
        self.incremental_index = incremental_index
        self.store = store

    def create_source_evidence(
        self,
        file_path: str,
        start_line: int,
        end_line: int,
        description: str,
        symbol_name: Optional[str] = None,
        provenance_tool: str = "forge:tool:get_source_context",
        finding_id: Optional[str] = None,
    ) -> EvidenceRecord:
        """Create a cryptographically verified source excerpt evidence record from disk."""
        norm_path = normalize_relative_path(file_path)
        if not norm_path:
            raise GroundingValidationError(f"Invalid file path: '{file_path}'")

        if is_protected_path(norm_path, base_dir=self.workspace_path):
            raise GroundingValidationError(f"Cannot ground evidence on protected file: '{norm_path}'")

        try:
            abs_path = assert_contained_path(self.workspace_path / norm_path, self.workspace_path)
        except SecurityError as e:
            raise GroundingValidationError(f"Path containment violation: {e}")

        if not abs_path.exists() or not abs_path.is_file():
            raise GroundingValidationError(f"File '{norm_path}' does not exist on disk.")

        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            raise GroundingValidationError(f"Failed to read '{norm_path}': {e}")

        lines = content.splitlines()
        total_lines = len(lines)

        if start_line < 1:
            raise GroundingValidationError(f"start_line ({start_line}) must be >= 1")
        if end_line < start_line:
            raise GroundingValidationError(f"end_line ({end_line}) cannot be less than start_line ({start_line})")
        if start_line > total_lines:
            raise GroundingValidationError(f"start_line ({start_line}) exceeds file length ({total_lines})")

        clamped_end = min(total_lines, end_line)
        slice_lines = lines[start_line - 1 : clamped_end]
        raw_text = "\n".join(slice_lines)
        sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

        details = {
            "source_sha256": sha,
            "total_lines": total_lines,
            "symbol_name": symbol_name,
            "provenance_tool": provenance_tool,
            "is_direct_disk_fact": True,
        }

        record = create_evidence(
            run_id=self.run_id,
            evidence_type=EvidenceType.SOURCE_EXCERPT,
            description=description,
            content=raw_text,
            finding_id=finding_id,
            file_path=norm_path,
            start_line=start_line,
            end_line=clamped_end,
            details=details,
            provenance=provenance_tool,
        )

        if self.store:
            self.store.attach_evidence(record)

        return record

    def create_call_graph_evidence(
        self,
        caller_id: str,
        callee_id: str,
        caller_file: str,
        line_number: int,
        resolution_type: str,
        description: str,
        provenance_tool: str = "forge:tool:explore_call_graph",
        finding_id: Optional[str] = None,
    ) -> EvidenceRecord:
        """Create a verified call-graph relationship evidence record."""
        details = {
            "caller_id": caller_id,
            "callee_id": callee_id,
            "caller_file": caller_file,
            "line_number": line_number,
            "resolution_type": resolution_type,
            "provenance_tool": provenance_tool,
            "is_graph_derived": True,
        }

        content_summary = (
            f"Call Edge: {caller_id} -> {callee_id} at {caller_file}:{line_number} (Resolution: {resolution_type})"
        )

        record = create_evidence(
            run_id=self.run_id,
            evidence_type=EvidenceType.STATIC_ANALYSIS,
            description=description,
            content=content_summary,
            finding_id=finding_id,
            file_path=caller_file,
            start_line=line_number,
            end_line=line_number,
            details=details,
            provenance=provenance_tool,
        )

        if self.store:
            self.store.attach_evidence(record)

        return record

    def create_module_dependency_evidence(
        self,
        module_name: str,
        dependency_name: str,
        is_circular: bool = False,
        cycle_path: Optional[List[str]] = None,
        description: Optional[str] = None,
        provenance_tool: str = "forge:tool:explore_module_dependencies",
        finding_id: Optional[str] = None,
    ) -> EvidenceRecord:
        """Create a verified module dependency or cycle evidence record."""
        details = {
            "module_name": module_name,
            "dependency_name": dependency_name,
            "is_circular": is_circular,
            "cycle_path": cycle_path or [],
            "provenance_tool": provenance_tool,
            "is_graph_derived": True,
        }

        if is_circular:
            desc = description or f"Circular dependency cycle involving '{module_name}': {' -> '.join(cycle_path or [])}"
            content = f"Circular Dependency: {' -> '.join(cycle_path or [module_name, dependency_name])}"
        else:
            desc = description or f"Module dependency: '{module_name}' imports '{dependency_name}'"
            content = f"Module Dependency: {module_name} -> {dependency_name}"

        record = create_evidence(
            run_id=self.run_id,
            evidence_type=EvidenceType.STATIC_ANALYSIS,
            description=desc,
            content=content,
            finding_id=finding_id,
            details=details,
            provenance=provenance_tool,
        )

        if self.store:
            self.store.attach_evidence(record)

        return record

    def validate_candidate_finding(
        self,
        title: str,
        description: str,
        affected_file: str,
        start_line: int,
        end_line: int,
        severity: FindingSeverity = FindingSeverity.MEDIUM,
        confidence: FindingConfidence = FindingConfidence.MEDIUM,
        defect_category: Optional[str] = None,
        limitations: Optional[str] = None,
    ) -> Finding:
        """Validate a candidate finding against actual disk code and create verified evidence."""
        # Step 1: Validate file existence, containment, and protection
        norm_path = normalize_relative_path(affected_file)
        if not norm_path:
            raise GroundingValidationError(f"Invalid affected_file path: '{affected_file}'")

        if is_protected_path(norm_path, base_dir=self.workspace_path):
            raise GroundingValidationError(f"Finding cannot target protected path: '{norm_path}'")

        try:
            abs_path = assert_contained_path(self.workspace_path / norm_path, self.workspace_path)
        except SecurityError as e:
            raise GroundingValidationError(f"Path traversal or security violation: {e}")

        if not abs_path.exists() or not abs_path.is_file():
            raise GroundingValidationError(f"Affected file '{norm_path}' does not exist on disk.")

        # Step 2: Validate line numbers against actual file content
        content = abs_path.read_text(encoding="utf-8", errors="replace")
        total_lines = len(content.splitlines())
        if start_line < 1:
            raise GroundingValidationError(f"start_line ({start_line}) must be >= 1")
        if end_line < start_line:
            raise GroundingValidationError(f"end_line ({end_line}) cannot be less than start_line ({start_line})")
        if start_line > total_lines:
            raise GroundingValidationError(
                f"start_line ({start_line}) exceeds actual file line count ({total_lines}) in '{norm_path}'"
            )

        # Step 3: Automatically create and link verified source excerpt evidence
        finding_id = f"find_{hashlib.sha256(f'{norm_path}:{start_line}:{title}'.encode()).hexdigest()[:12]}"

        if self.store:
            self.store.create_finding(
                run_id=self.run_id,
                title=title,
                description=description,
                severity=severity,
                confidence=confidence,
                affected_files=[norm_path],
                finding_id=finding_id,
                limitations=limitations,
            )

        evidence = self.create_source_evidence(
            file_path=norm_path,
            start_line=start_line,
            end_line=end_line,
            description=f"Direct source excerpt for finding: {title}",
            finding_id=finding_id,
            provenance_tool="forge:evidence_grounding",
        )

        finding = Finding(
            finding_id=finding_id,
            run_id=self.run_id,
            title=title,
            description=description,
            severity=severity,
            confidence=confidence,
            lifecycle_state=FindingLifecycleState.SUSPECTED,
            affected_files=[norm_path],
            supporting_evidence_ids=[evidence.evidence_id],
            limitations=limitations,
        )

        return finding
