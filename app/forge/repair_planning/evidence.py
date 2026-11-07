"""Phase 12 M3: Evidence Collection and Freshness Validation for Repair Planning.

Collects, normalizes, and audits evidence from:
- Phase 12 M2 RootCauseReport (nodes, edges, chains, evidence references).
- Phase 12 M1 DiagnosisResult (root cause locations, hypotheses, evidence references).
- ForgeStore persistent storage.
- Real on-disk source files within repository bounds.

Enforces evidence freshness, line range integrity, path containment, and
distinguishes directly verified code facts from inferred causal relations.
"""

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from forge.analysis.evidence import normalize_relative_path
from forge.causality.schemas import CausalNode, CausalStatus, RootCauseReport
from forge.diagnosis.schemas import CandidateLocation, DiagnosisResult
from forge.repository.protection import is_protected_path
from forge.repository.validation import assert_contained_path, SecurityError
from forge.repair_planning.schemas import RepairEvidence
from forge.storage.store import ForgeStore


class RepairEvidenceCollector:
    """Collects and audits supporting evidence for repair planning."""

    def __init__(
        self,
        workspace_path: Path,
        store: Optional[ForgeStore] = None,
    ) -> None:
        self.workspace_path = workspace_path.resolve()
        self.store = store

    def collect_from_root_cause(
        self,
        report: RootCauseReport,
    ) -> Tuple[List[RepairEvidence], List[str]]:
        """Extract and audit evidence from a RootCauseReport.

        Returns:
            Tuple of (valid_evidence_list, stale_or_invalid_reasons)
        """
        evidence_list: List[RepairEvidence] = []
        issues: List[str] = []
        seen_keys: Set[str] = set()

        # 1. Audit Primary Root Cause Node
        if report.primary_root_cause:
            ev, issue = self._audit_node_evidence(
                node=report.primary_root_cause,
                source_label="root_cause_node",
                rel_desc="Direct location of primary defect root cause",
            )
            if ev:
                key = f"{ev.file_path}:{ev.start_line}:{ev.end_line}"
                if key not in seen_keys:
                    evidence_list.append(ev)
                    seen_keys.add(key)
            if issue:
                issues.append(issue)

        # 2. Audit Causal Chain Nodes
        for chain in report.causal_chains:
            for node_id in chain.node_ids:
                matching_nodes = [n for n in report.nodes if n.node_id == node_id]
                for node in matching_nodes:
                    ev, issue = self._audit_node_evidence(
                        node=node,
                        source_label=f"chain_node:{chain.chain_id}",
                        rel_desc=f"Causal chain progression step: {node.role.value}",
                    )
                    if ev:
                        key = f"{ev.file_path}:{ev.start_line}:{ev.end_line}"
                        if key not in seen_keys:
                            evidence_list.append(ev)
                            seen_keys.add(key)
                    if issue:
                        issues.append(issue)

        # 3. Audit Symptom Locations
        for symptom in report.symptom_locations:
            ev, issue = self._audit_node_evidence(
                node=symptom,
                source_label="symptom_node",
                rel_desc="Location of observed failure symptom or crash",
            )
            if ev:
                key = f"{ev.file_path}:{ev.start_line}:{ev.end_line}"
                if key not in seen_keys:
                    evidence_list.append(ev)
                    seen_keys.add(key)
            if issue:
                issues.append(issue)

        # 4. Resolve referenced Store Evidence records if store available
        if self.store is not None:
            for eid in report.evidence_references:
                try:
                    record = self.store.get_evidence(eid)
                    if record and record.file_path:
                        ev, issue = self._audit_raw_file_evidence(
                            evidence_id=record.evidence_id,
                            file_path=record.file_path,
                            start_line=record.start_line,
                            end_line=record.end_line,
                            evidence_type=record.evidence_type.value if hasattr(record.evidence_type, "value") else str(record.evidence_type),
                            evidence_source="forge_store_record",
                            rel_desc=f"Stored evidence record: {record.description}",
                            saved_hash=(record.details or {}).get("source_sha256") or (record.details or {}).get("content_hash"),
                        )
                        if ev:
                            key = f"{ev.file_path}:{ev.start_line}:{ev.end_line}"
                            if key not in seen_keys:
                                evidence_list.append(ev)
                                seen_keys.add(key)
                        if issue:
                            issues.append(issue)
                except Exception as exc:
                    issues.append(f"Could not load evidence '{eid}' from store: {exc}")

        return evidence_list, issues

    def collect_from_diagnosis(
        self,
        diagnosis: DiagnosisResult,
    ) -> Tuple[List[RepairEvidence], List[str]]:
        """Extract and audit evidence from a DiagnosisResult."""
        evidence_list: List[RepairEvidence] = []
        issues: List[str] = []
        seen_keys: Set[str] = set()

        for loc in diagnosis.root_cause_locations:
            ev, issue = self._audit_location_evidence(
                loc=loc,
                source_label="diagnosis_candidate_location",
                rel_desc=f"Candidate root cause location from diagnosis (confidence: {loc.confidence_weight})",
            )
            if ev:
                key = f"{ev.file_path}:{ev.start_line}:{ev.end_line}"
                if key not in seen_keys:
                    evidence_list.append(ev)
                    seen_keys.add(key)
            if issue:
                issues.append(issue)

        for hyp in diagnosis.hypotheses:
            ev, issue = self._audit_location_evidence(
                loc=hyp.suspected_location,
                source_label=f"hypothesis:{hyp.hypothesis_id}",
                rel_desc=f"Suspected location for hypothesis: {hyp.description}",
            )
            if ev:
                key = f"{ev.file_path}:{ev.start_line}:{ev.end_line}"
                if key not in seen_keys:
                    evidence_list.append(ev)
                    seen_keys.add(key)
            if issue:
                issues.append(issue)

        return evidence_list, issues

    def verify_file_location(
        self,
        file_path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Verify repository path safety, existence on disk, and line range.

        Returns:
            Tuple of (is_valid, content_hash_if_read, error_reason)
        """
        try:
            norm_path = normalize_relative_path(file_path)
        except Exception as norm_err:
            return False, None, f"Security violation: path traversal outside repository: {norm_err}"

        if not norm_path:
            return False, None, f"Invalid or empty relative file path: '{file_path}'"

        if is_protected_path(norm_path, base_dir=self.workspace_path) or ".git" in Path(norm_path).parts:
            return False, None, f"File path is protected: '{norm_path}'"

        try:
            abs_path = assert_contained_path(self.workspace_path / norm_path, self.workspace_path)
        except SecurityError as sec_err:
            return False, None, f"Security violation: path traversal outside repository: {sec_err}"

        if not abs_path.exists():
            return False, None, f"File '{norm_path}' does not exist on disk"

        if not abs_path.is_file():
            return False, None, f"Path '{norm_path}' exists on disk but is not a regular file"

        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except Exception as read_err:
            return False, None, f"Failed to read file '{norm_path}': {read_err}"

        lines = content.splitlines()
        total_lines = len(lines)

        if start_line is not None:
            if start_line < 1:
                return False, None, f"start_line ({start_line}) must be >= 1"
            if total_lines > 0 and start_line > total_lines:
                return False, None, f"start_line ({start_line}) exceeds file line count ({total_lines})"

        if end_line is not None:
            if start_line is not None and end_line < start_line:
                return False, None, f"end_line ({end_line}) cannot be less than start_line ({start_line})"
            if total_lines > 0 and end_line > total_lines:
                return False, None, f"end_line ({end_line}) exceeds file line count ({total_lines})"

        if start_line is not None and end_line is not None and total_lines > 0:
            clamped_end = min(total_lines, end_line)
            slice_lines = lines[start_line - 1 : clamped_end]
            content_hash = hashlib.sha256("\n".join(slice_lines).encode("utf-8")).hexdigest()
        else:
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        return True, content_hash, None

    # -----------------------------------------------------------------------
    # Internal Audit Helpers
    # -----------------------------------------------------------------------

    def _audit_node_evidence(
        self,
        node: CausalNode,
        source_label: str,
        rel_desc: str,
    ) -> Tuple[Optional[RepairEvidence], Optional[str]]:
        return self._audit_raw_file_evidence(
            evidence_id=f"rev_{node.node_id}",
            file_path=node.file_path,
            start_line=node.start_line,
            end_line=node.end_line,
            evidence_type=node.node_type.value,
            evidence_source=source_label,
            rel_desc=f"{rel_desc} ({node.description or node.symbol_id or node.file_path})",
            symbol_reference=node.symbol_id,
        )

    def _audit_location_evidence(
        self,
        loc: CandidateLocation,
        source_label: str,
        rel_desc: str,
    ) -> Tuple[Optional[RepairEvidence], Optional[str]]:
        return self._audit_raw_file_evidence(
            evidence_id=f"rev_loc_{hashlib.sha256(f'{loc.file_path}:{loc.start_line}:{loc.end_line}'.encode()).hexdigest()[:12]}",
            file_path=loc.file_path,
            start_line=loc.start_line,
            end_line=loc.end_line,
            evidence_type="candidate_location",
            evidence_source=source_label,
            rel_desc=rel_desc,
            symbol_reference=loc.symbol_id,
        )

    def _audit_raw_file_evidence(
        self,
        evidence_id: str,
        file_path: str,
        start_line: Optional[int],
        end_line: Optional[int],
        evidence_type: str,
        evidence_source: str,
        rel_desc: str,
        symbol_reference: Optional[str] = None,
        saved_hash: Optional[str] = None,
    ) -> Tuple[Optional[RepairEvidence], Optional[str]]:
        norm_path = normalize_relative_path(file_path) or file_path
        is_valid, current_hash, err = self.verify_file_location(norm_path, start_line, end_line)

        is_fresh = True
        stale_reason = None

        if not is_valid:
            is_fresh = False
            stale_reason = f"Evidence '{evidence_id}' referencing '{norm_path}' is stale or invalid: {err}"
        elif saved_hash and current_hash and saved_hash != current_hash:
            is_fresh = False
            stale_reason = (
                f"Evidence '{evidence_id}' content hash mismatch on '{norm_path}' "
                f"(current: {current_hash[:8]}..., recorded: {saved_hash[:8]}...)"
            )

        evidence = RepairEvidence(
            evidence_id=evidence_id,
            evidence_source=evidence_source,
            file_path=norm_path,
            symbol_reference=symbol_reference,
            start_line=start_line if is_valid else None,
            end_line=end_line if is_valid else None,
            evidence_type=evidence_type,
            source_hash=current_hash if is_valid else None,
            is_fresh=is_fresh,
            relationship_to_defect=rel_desc if is_fresh else f"{rel_desc} [STALE: {stale_reason}]",
        )

        return evidence, stale_reason
