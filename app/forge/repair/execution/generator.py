"""Phase 8 Repair Execution Architecture — Candidate Patch Generator.

Implements evidence-grounded candidate patch generation using local LLM inference
or deterministic synthesis, enforcing minimal edits, budget caps, and single-file scope.
"""

from datetime import datetime, timezone
import difflib
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

from forge.config import redact_secrets
from forge.llm.factory import get_provider
from forge.llm.models import ChatMessage, LLMRequest, MessageRole
from forge.llm.provider import LLMProvider, LLMUnavailableError
from forge.repair.execution.failures import (
    PatchGenerationError,
    RepairPolicyViolationError,
    ScopeContainmentViolationError,
)
from forge.repair.execution.interfaces import RepairExecutorProtocol, RepairWorkspaceProtocol
from forge.repair.execution.models import PatchArtifact, RepairExecutionPlan
from forge.repair.execution.policy import RepairExecutionPolicy
from forge.repair.planner import parse_json_payload
from forge.schemas import EvidenceRecord, Finding


class RepairPatchGenerator(RepairExecutorProtocol):
    """Generates candidate PatchArtifacts inside isolated workspace boundaries."""

    def __init__(self, provider: Optional[LLMProvider] = None, model: Optional[str] = None):
        self._provider = provider
        self._model = model or "qwen2.5-coder:1.5b"

    def execute_repair(
        self,
        plan: RepairExecutionPlan,
        workspace: RepairWorkspaceProtocol,
        policy: RepairExecutionPolicy,
        finding: Optional[Finding] = None,
        evidence_records: Optional[List[EvidenceRecord]] = None,
        replacement_content: Optional[str] = None,
    ) -> PatchArtifact:
        """Generate a candidate PatchArtifact for the plan within the isolated workspace.

        Args:
            plan: Validated RepairExecutionPlan.
            workspace: Isolated RepairWorkspace containing repo files at base revision.
            policy: Safety policy enforcing budgets and scope.
            finding: Optional associated Finding.
            evidence_records: Optional supporting evidence records.
            replacement_content: Optional explicit replacement text (bypasses LLM for deterministic tests).

        Raises:
            PatchGenerationError: If LLM fails, times out, or produces invalid diff.
            RepairPolicyViolationError: If patch violates budget or touches protected files.
        """
        # Validate target file exists in workspace
        target_path = workspace.validate_containment(plan.target_file)
        if not target_path.is_file():
            raise PatchGenerationError(
                f"Target file '{plan.target_file}' does not exist in workspace",
                details={"target_file": plan.target_file},
            )

        orig_content = target_path.read_text(encoding="utf-8", errors="replace")

        # 1. Deterministic Replacement Mode (if explicit replacement_content provided)
        if replacement_content is not None:
            unified_diff = self._construct_diff_from_replacement(
                plan.target_file,
                orig_content,
                replacement_content,
                evidence_records,
            )
        else:
            # 2. LLM Generation Mode (Local Ollama / Configured Provider)
            unified_diff = self._generate_diff_with_provider(
                plan,
                orig_content,
                finding,
                evidence_records,
            )

        if not unified_diff or not unified_diff.strip():
            raise PatchGenerationError("Generated patch content is empty or contains no modifications")

        # Normalize and sanitize unified diff
        clean_diff = unified_diff.strip() + "\n"

        # Verify touched files in diff
        file_pattern = re.compile(r"^\+\+\+\s+[b/]*(.*)$", re.MULTILINE)
        touched: List[str] = []
        for m in file_pattern.finditer(clean_diff):
            f_name = m.group(1).strip()
            if f_name.startswith("b/"):
                f_name = f_name[2:]
            if f_name and f_name != "/dev/null":
                touched.append(Path(f_name).as_posix())

        if not touched:
            raise PatchGenerationError("Malformed diff: no target file headers found")

        norm_target = Path(plan.target_file).as_posix()
        for tf in touched:
            if tf != norm_target:
                raise ScopeContainmentViolationError(
                    f"Generated patch touched unauthorized file '{tf}'; only '{norm_target}' is permitted",
                    details={"touched": tf, "allowed": norm_target},
                )

        # Enforce policy budget limits
        lines_added = 0
        lines_deleted = 0
        hunks_count = 0
        for line in clean_diff.splitlines():
            if line.startswith("@@"):
                hunks_count += 1
            elif line.startswith("+") and not line.startswith("+++"):
                lines_added += 1
            elif line.startswith("-") and not line.startswith("---"):
                lines_deleted += 1

        patch_bytes = len(clean_diff.encode("utf-8"))
        files_modified_count = len(touched)

        policy.check_patch_budget(
            lines_added=lines_added,
            lines_deleted=lines_deleted,
            hunks_count=hunks_count,
            files_modified_count=files_modified_count,
            patch_bytes=patch_bytes,
        )

        finding_id = finding.finding_id if finding else "find_unknown"
        base_commit = workspace.base_commit_sha

        return PatchArtifact.create(
            repair_id=plan.repair_id,
            finding_id=finding_id,
            base_commit_sha=base_commit,
            target_file=norm_target,
            diff_content=clean_diff,
            affected_paths=[norm_target],
        )

    def _construct_diff_from_replacement(
        self,
        target_file: str,
        orig_content: str,
        replacement_content: str,
        evidence_records: Optional[List[EvidenceRecord]] = None,
    ) -> str:
        """Construct a minimal unified diff by replacing statement or block around evidence lines."""
        orig_lines = orig_content.splitlines(keepends=True)
        norm_target = Path(target_file).as_posix()

        # Check evidence line numbers if available
        start_line = 1
        end_line = len(orig_lines)
        if evidence_records:
            for ev in evidence_records:
                if ev.file_path == target_file and ev.start_line is not None:
                    start_line = max(1, ev.start_line)
                    end_line = min(len(orig_lines), ev.end_line or ev.start_line)
                    break

        # If replacement is a full-file replacement
        if replacement_content.startswith("--- a/") or replacement_content.startswith("diff --git"):
            return replacement_content

        # Line-based splice
        repl_lines = [
            l if l.endswith("\n") else f"{l}\n"
            for l in replacement_content.splitlines()
        ]
        if not repl_lines:
            repl_lines = ["\n"]

        patched_lines = (
            orig_lines[: start_line - 1]
            + repl_lines
            + orig_lines[end_line :]
        )

        diff = difflib.unified_diff(
            orig_lines,
            patched_lines,
            fromfile=f"a/{norm_target}",
            tofile=f"b/{norm_target}",
            n=3,
        )
        return "".join(diff)

    def _generate_diff_with_provider(
        self,
        plan: RepairExecutionPlan,
        orig_content: str,
        finding: Optional[Finding] = None,
        evidence_records: Optional[List[EvidenceRecord]] = None,
    ) -> str:
        """Query LLM provider (Ollama / Fake) to generate a bounded unified diff."""
        provider = self._provider
        if provider is None:
            try:
                provider = get_provider()
            except Exception as e:
                raise PatchGenerationError(f"Failed to initialize LLM provider: {e}") from e

        finding_title = finding.title if finding else "Defect"
        evidence_snippets = []
        if evidence_records:
            for ev in evidence_records:
                if ev.content:
                    evidence_snippets.append(f"Lines {ev.start_line}-{ev.end_line}: {ev.content}")

        ev_context = "\n".join(evidence_snippets) if evidence_snippets else "No source excerpt provided"

        # Bounded context of target file
        file_preview = orig_content[:8000]

        system_prompt = (
            "You are FORGE Repair Generator. Your task is to produce a minimal, precise unified diff "
            "to fix the described defect in the target file. You must strictly adhere to these rules:\n"
            "1. Output ONLY a valid JSON object with keys: 'target_file', 'unified_diff', and 'explanation'.\n"
            "2. Make the smallest reasonable change. Do not refactor unrelated code.\n"
            "3. Target exclusively the authorized file.\n"
            "4. The 'unified_diff' MUST be a valid unified diff starting with '--- a/' and '+++ b/' and contain @@ hunk headers.\n"
        )

        user_prompt = (
            f"Target File: {plan.target_file}\n"
            f"Defect Title: {finding_title}\n"
            f"Root Cause: {plan.root_cause}\n"
            f"Proposed Change: {plan.proposed_change}\n"
            f"Expected Behavior: {plan.expected_behavior}\n\n"
            f"Relevant Evidence Excerpt:\n{ev_context}\n\n"
            f"Source Code of {plan.target_file}:\n```\n{file_preview}\n```\n\n"
            "Respond with JSON format:\n"
            '{\n  "target_file": "' + plan.target_file + '",\n  "unified_diff": "--- a/' + plan.target_file + '\\n+++ b/' + plan.target_file + '\\n@@ ... @@\\n...",\n  "explanation": "..."\n}'
        )

        req = LLMRequest(
            messages=[
                ChatMessage(role=MessageRole.SYSTEM, content=system_prompt),
                ChatMessage(role=MessageRole.USER, content=user_prompt),
            ],
            temperature=0.0,
            max_tokens=2048,
        )

        try:
            response = provider.chat(req)
        except Exception as e:
            raise PatchGenerationError(f"LLM provider generation failed: {e}") from e

        raw_content = response.content or ""
        parsed_json, parse_err = parse_json_payload(raw_content)

        if parsed_json and "unified_diff" in parsed_json:
            return parsed_json["unified_diff"]

        # Fallback: check if raw response is a unified diff inside markdown code fence
        diff_match = re.search(r"```(?:diff)?\s*(--- a/[\s\S]*?)\s*```", raw_content, re.MULTILINE)
        if diff_match:
            return diff_match.group(1)

        # Check if raw response starts directly with diff header
        if "--- a/" in raw_content and "+++ b/" in raw_content:
            idx = raw_content.find("--- a/")
            return raw_content[idx:]

        raise PatchGenerationError(
            f"Model response did not contain a valid unified diff: {parse_err or 'missing unified_diff key'}",
            details={"raw_response": raw_content[:500]},
        )
