"""Typed, bounded, read-only investigation tools for FORGE agents."""

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from forge.analysis.evidence import (
    create_evidence,
    normalize_relative_path,
    EvidenceValidationError,
)
from forge.config import redact_secrets
from forge.llm.models import ToolDefinition
from forge.repository.architecture import RepositoryMap
from forge.repository.indexer import SymbolIndex
from forge.repository.validation import assert_contained_path, SecurityError
from forge.sandbox.adapter import Sandbox
from forge.sandbox.docker import DockerSandbox
from forge.sandbox.evidence import record_sandbox_result_in_store
from forge.sandbox.models import SandboxRequest, ExecutionLimits
from forge.schemas import (
    EventType,
    EvidenceType,
    FindingConfidence,
    FindingLifecycleState,
    FindingSeverity,
)
from forge.storage.store import ForgeStore
from forge.analysis.validation import (
    ToolExecutionError,
    FindingValidationError,
    SyntaxClaimValidationError,
    ConsistencyValidationError,
    InsufficientEvidenceError,
    DuplicateFindingError,
    RepeatedRejectedClaimError,
    ValidationOutcome,
    RejectedClaimRecord,
    RejectedClaimTracker,
    build_rejection_directive,
    validate_finding_claim,
)


MAX_EXCERPT_LINES = 250
MAX_SEARCH_RESULTS = 50
MAX_DIR_ENTRIES = 100


class ToolRegistry:
    """Manages available tools, validation, execution, and JSON Schema definitions."""

    def __init__(
        self,
        workspace_path: Path,
        repo_map: Optional[RepositoryMap] = None,
        symbol_index: Optional[SymbolIndex] = None,
        store: Optional[ForgeStore] = None,
        run_id: Optional[str] = None,
        sandbox: Optional[Sandbox] = None,
        max_output_chars: int = 50_000,
        rejected_tracker: Optional[RejectedClaimTracker] = None,
    ):
        self.workspace_path = workspace_path.resolve()
        self.repo_map = repo_map
        self.symbol_index = symbol_index or (repo_map.symbol_index if repo_map else None)
        self.store = store
        self.run_id = run_id or "run_default"
        self.sandbox = sandbox or DockerSandbox()
        self.max_output_chars = max_output_chars
        self.rejected_tracker = rejected_tracker or RejectedClaimTracker()
        self._handlers: Dict[str, Callable[[Dict[str, Any]], str]] = {}
        self._definitions: List[ToolDefinition] = []

        self._register_tools()

    def get_definitions(self) -> List[ToolDefinition]:
        """Return JSON Schema tool definitions for LLM providers."""
        return list(self._definitions)

    def execute_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """Execute a tool by name with arguments and return sanitized result string."""
        if name not in self._handlers:
            return json.dumps({
                "error": f"Tool '{name}' is not recognized or available.",
                "available_tools": list(self._handlers.keys()),
            })

        handler = self._handlers[name]
        try:
            raw_output = handler(arguments)
            redacted = redact_secrets(raw_output)
            if len(redacted) > self.max_output_chars:
                return (
                    redacted[: self.max_output_chars]
                    + f"\n\n[TRUNCATED: Output exceeded maximum allowed size of {self.max_output_chars} characters]"
                )
            return redacted
        except FindingValidationError as fve:
            rejection = fve.to_rejection_dict()
            return json.dumps(rejection, indent=2)
        except SecurityError as sec_err:
            return json.dumps({"error": f"Security violation: {sec_err}"})
        except ToolExecutionError as tool_err:
            return json.dumps({"error": str(tool_err)})
        except Exception as exc:
            return json.dumps({"error": f"Unexpected tool execution error: {exc}"})


    def _register_tools(self) -> None:
        """Register all typed read-only investigation tools."""
        # 1. get_repository_map
        self._definitions.append(
            ToolDefinition(
                name="get_repository_map",
                description="Get high-level repository architecture, detected frameworks, entrypoints, packages, and symbol counts.",
                parameters={"type": "object", "properties": {}},
            )
        )
        self._handlers["get_repository_map"] = self._handle_get_repository_map

        # 2. search_symbols
        self._definitions.append(
            ToolDefinition(
                name="search_symbols",
                description="Search indexed Python symbols (classes, functions, methods, imports) by name and optional type.",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Symbol name or substring to search for",
                        },
                        "symbol_type": {
                            "type": "string",
                            "enum": ["class", "function", "method", "import"],
                            "description": "Optional symbol type filter",
                        },
                    },
                    "required": ["query"],
                },
            )
        )
        self._handlers["search_symbols"] = self._handle_search_symbols

        # 3. read_source_excerpt
        self._definitions.append(
            ToolDefinition(
                name="read_source_excerpt",
                description="Read lines from a specific source file in the repository within line limits.",
                parameters={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Relative path to the file from the repository root",
                        },
                        "start_line": {
                            "type": "integer",
                            "description": "Starting line number (1-indexed)",
                            "minimum": 1,
                        },
                        "end_line": {
                            "type": "integer",
                            "description": "Ending line number (inclusive)",
                            "minimum": 1,
                        },
                    },
                    "required": ["file_path", "start_line", "end_line"],
                },
            )
        )
        self._handlers["read_source_excerpt"] = self._handle_read_source_excerpt

        # 4. list_directory
        self._definitions.append(
            ToolDefinition(
                name="list_directory",
                description="List files and directories inside a relative path within the repository root.",
                parameters={
                    "type": "object",
                    "properties": {
                        "relative_path": {
                            "type": "string",
                            "description": "Relative directory path (defaults to root '.')",
                            "default": ".",
                        },
                    },
                },
            )
        )
        self._handlers["list_directory"] = self._handle_list_directory

        # 5. search_text
        self._definitions.append(
            ToolDefinition(
                name="search_text",
                description="Search for exact text or pattern across repository files (capped output).",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Text substring to search for",
                        },
                        "path_scope": {
                            "type": "string",
                            "description": "Optional subdirectory or file path to limit the search scope",
                        },
                    },
                    "required": ["query"],
                },
            )
        )
        self._handlers["search_text"] = self._handle_search_text

        # 6. get_evidence
        self._definitions.append(
            ToolDefinition(
                name="get_evidence",
                description="Retrieve recorded evidence for the current audit run or a specific finding.",
                parameters={
                    "type": "object",
                    "properties": {
                        "finding_id": {
                            "type": "string",
                            "description": "Optional finding ID to retrieve attached evidence for",
                        },
                    },
                },
            )
        )
        self._handlers["get_evidence"] = self._handle_get_evidence

        # 7. list_findings
        self._definitions.append(
            ToolDefinition(
                name="list_findings",
                description="List all findings recorded so far in the current audit run.",
                parameters={"type": "object", "properties": {}},
            )
        )
        self._handlers["list_findings"] = self._handle_list_findings

        # 8. get_run_history
        self._definitions.append(
            ToolDefinition(
                name="get_run_history",
                description="Retrieve a list of recent historical audit runs from the database, including status and summary.",
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of runs to return (default 10, max 50)",
                            "minimum": 1,
                            "maximum": 50,
                        },
                    },
                },
            )
        )
        self._handlers["get_run_history"] = self._handle_get_run_history

        # 9. run_sandbox_check
        self._definitions.append(
            ToolDefinition(
                name="run_sandbox_check",
                description="Execute an approved check or test command inside an isolated Docker sandbox container. Disallowed commands and host execution are strictly rejected.",
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Command vector to execute, e.g. ['pytest', '-k', 'test_auth'] or ['flake8']",
                        },
                        "relative_cwd": {
                            "type": "string",
                            "description": "Relative working directory inside the workspace",
                            "default": ".",
                        },
                    },
                    "required": ["command"],
                },
            )
        )
        self._handlers["run_sandbox_check"] = self._handle_run_sandbox_check

        # 10. record_finding
        self._definitions.append(
            ToolDefinition(
                name="record_finding",
                description=(
                    "Record a structured defect or security finding with server-side verified source evidence. "
                    "The affected_file and line range must exist in the repository workspace. FORGE independently "
                    "verifies and extracts the real source excerpt from disk, computes its SHA-256 digest, and persists "
                    "the finding in 'suspected' state."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Concise summary title of the suspected defect",
                        },
                        "description": {
                            "type": "string",
                            "description": "Detailed explanation of why this code is defective or vulnerable",
                        },
                        "affected_file": {
                            "type": "string",
                            "description": "Repository-relative file path containing the defective code",
                        },
                        "start_line": {
                            "type": "integer",
                            "description": "Starting line number of the relevant code excerpt (1-indexed)",
                            "minimum": 1,
                        },
                        "end_line": {
                            "type": "integer",
                            "description": "Ending line number of the relevant code excerpt (inclusive)",
                            "minimum": 1,
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["critical", "high", "medium", "low", "info"],
                            "description": "Severity level of the issue",
                            "default": "medium",
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["confirmed", "high", "medium", "low"],
                            "description": "Confidence level in the defect observation",
                            "default": "medium",
                        },
                        "defect_category": {
                            "type": "string",
                            "description": "Optional defect category (e.g. 'syntax_error', 'logic_bug', 'security', 'resource_leak')",
                        },
                        "suggested_step": {
                            "type": "string",
                            "description": "Optional suggested next verification step or reproduction check",
                        },
                        "limitations": {
                            "type": "string",
                            "description": "Optional known uncertainties or limitations regarding this finding",
                        },
                    },
                    "required": ["title", "description", "affected_file", "start_line", "end_line"],
                },
            )
        )
        self._handlers["record_finding"] = self._handle_record_finding

    # -------------------------------------------------------------
    # Tool Handlers
    # -------------------------------------------------------------

    def _handle_get_repository_map(self, args: Dict[str, Any]) -> str:
        if self.repo_map:
            return self.repo_map.render_markdown_summary()
        return "Repository map is not initialized."

    def _handle_search_symbols(self, args: Dict[str, Any]) -> str:
        query = args.get("query", "").strip()
        sym_type = args.get("symbol_type")
        if not query:
            raise ToolExecutionError("Query parameter is required")

        if not self.symbol_index:
            return json.dumps({"symbols": [], "message": "Symbol index unavailable"})

        matches = self.symbol_index.query(query, symbol_type=sym_type)
        output = []
        for s in matches[:MAX_SEARCH_RESULTS]:
            output.append({
                "name": s.name,
                "symbol_type": s.symbol_type,
                "file_path": s.file_path,
                "start_line": s.start_line,
                "end_line": s.end_line,
                "parent_class": s.parent_class,
                "docstring": s.docstring[:100] if s.docstring else None,
            })

        return json.dumps({
            "total_matches": len(matches),
            "returned_matches": len(output),
            "symbols": output,
        }, indent=2)

    def _handle_read_source_excerpt(self, args: Dict[str, Any]) -> str:
        file_path_raw = args.get("file_path", "")
        start_line = args.get("start_line", 1)
        end_line = args.get("end_line", 1)

        if not file_path_raw:
            raise ToolExecutionError("file_path parameter is required")
        if not isinstance(start_line, int) or start_line < 1:
            raise ToolExecutionError("start_line must be an integer >= 1")
        if not isinstance(end_line, int) or end_line < start_line:
            raise ToolExecutionError("end_line must be >= start_line")

        if (end_line - start_line + 1) > MAX_EXCERPT_LINES:
            end_line = start_line + MAX_EXCERPT_LINES - 1

        candidate = self.workspace_path / file_path_raw.strip().lstrip("/")
        resolved_file = assert_contained_path(candidate, self.workspace_path)

        if not resolved_file.exists() or not resolved_file.is_file():
            raise ToolExecutionError(f"File not found: '{file_path_raw}'")

        # Reject symlinks that point outside workspace
        assert_contained_path(resolved_file.resolve(), self.workspace_path)

        try:
            content = resolved_file.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            raise ToolExecutionError(f"Failed to read file '{file_path_raw}': {exc}")

        lines = content.splitlines()
        total_lines = len(lines)
        if start_line > total_lines:
            return f"[File '{file_path_raw}' has {total_lines} lines; requested start_line {start_line} is out of bounds]"

        slice_lines = lines[start_line - 1 : min(end_line, total_lines)]
        numbered_lines = [
            f"{start_line + i:4d} | {line}"
            for i, line in enumerate(slice_lines)
        ]

        header = f"=== File: {file_path_raw} (Lines {start_line}-{min(end_line, total_lines)} of {total_lines}) ==="
        return header + "\n" + "\n".join(numbered_lines)

    def _handle_list_directory(self, args: Dict[str, Any]) -> str:
        rel_path = args.get("relative_path", ".").strip().lstrip("/")
        target_dir = self.workspace_path / rel_path
        resolved_dir = assert_contained_path(target_dir, self.workspace_path)

        if not resolved_dir.exists() or not resolved_dir.is_dir():
            raise ToolExecutionError(f"Directory not found: '{rel_path}'")

        # Reject symlinks that point outside workspace
        assert_contained_path(resolved_dir.resolve(), self.workspace_path)

        entries = []
        try:
            for item in sorted(resolved_dir.iterdir()):
                # If symlink points outside workspace, skip it
                try:
                    assert_contained_path(item.resolve(), self.workspace_path)
                except SecurityError:
                    continue

                rel_item = str(item.relative_to(self.workspace_path))
                # Skip virtualenvs and git internals
                if any(p in [".git", ".venv", "__pycache__"] for p in item.parts):
                    continue
                entries.append({
                    "name": item.name,
                    "path": rel_item,
                    "is_directory": item.is_dir(),
                    "size_bytes": item.stat().st_size if item.is_file() else None,
                })
                if len(entries) >= MAX_DIR_ENTRIES:
                    break
        except Exception as exc:
            raise ToolExecutionError(f"Error listing directory '{rel_path}': {exc}")

        return json.dumps({
            "directory": rel_path or ".",
            "total_entries": len(entries),
            "entries": entries,
        }, indent=2)

    def _handle_search_text(self, args: Dict[str, Any]) -> str:
        query = args.get("query", "")
        path_scope = args.get("path_scope")

        if not query:
            raise ToolExecutionError("query parameter is required")

        search_root = self.workspace_path
        if path_scope:
            candidate = self.workspace_path / path_scope.strip().lstrip("/")
            search_root = assert_contained_path(candidate, self.workspace_path)

        assert_contained_path(search_root.resolve(), self.workspace_path)

        results = []
        files_to_check = [search_root] if search_root.is_file() else search_root.rglob("*")

        for f in files_to_check:
            if not f.is_file():
                continue
            # Ensure resolved symlinks do not escape workspace
            try:
                assert_contained_path(f.resolve(), self.workspace_path)
            except SecurityError:
                continue
            if any(p in [".git", ".venv", "__pycache__", ".pytest_cache"] for p in f.parts):
                continue
            if f.stat().st_size > 500_000:
                continue

            try:
                rel_file = str(f.relative_to(self.workspace_path))
                lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
                for line_idx, line in enumerate(lines, start=1):
                    if query.lower() in line.lower():
                        results.append({
                            "file": rel_file,
                            "line": line_idx,
                            "match": line.strip()[:150],
                        })
                        if len(results) >= MAX_SEARCH_RESULTS:
                            break
            except Exception:
                continue

            if len(results) >= MAX_SEARCH_RESULTS:
                break

        return json.dumps({
            "query": query,
            "total_results": len(results),
            "results": results,
        }, indent=2)

    def _handle_get_evidence(self, args: Dict[str, Any]) -> str:
        if not self.store:
            return json.dumps({"evidence": [], "message": "Store unavailable"})

        finding_id = args.get("finding_id")
        if finding_id:
            records = self.store.list_evidence_for_finding(finding_id)
        else:
            # Query all evidence for run
            conn = self.store._conn()
            try:
                cur = conn.execute("SELECT evidence_id FROM evidence WHERE run_id = ?", (self.run_id,))
                records = [self.store.get_evidence(r["evidence_id"]) for r in cur.fetchall()]
            finally:
                conn.close()

        out = []
        for ev in records:
            if ev:
                out.append({
                    "evidence_id": ev.evidence_id,
                    "evidence_type": ev.evidence_type.value,
                    "description": ev.description,
                    "file_path": ev.file_path,
                    "lines": f"{ev.start_line}-{ev.end_line}" if ev.start_line else None,
                    "provenance": ev.provenance,
                    "content_snippet": ev.content[:200] + ("..." if len(ev.content) > 200 else ""),
                })

        return json.dumps({"total": len(out), "evidence": out}, indent=2)

    def _handle_list_findings(self, args: Dict[str, Any]) -> str:
        if not self.store:
            return json.dumps({"findings": [], "message": "Store unavailable"})

        findings = self.store.list_findings(run_id=self.run_id)
        out = []
        for f in findings:
            out.append({
                "finding_id": f.finding_id,
                "title": f.title,
                "severity": f.severity.value,
                "confidence": f.confidence.value,
                "state": f.lifecycle_state.value,
                "affected_files": f.affected_files,
                "evidence_count": len(f.supporting_evidence_ids),
            })
        return json.dumps({"total": len(out), "findings": out}, indent=2)

    def _handle_get_run_history(self, args: Dict[str, Any]) -> str:
        if not self.store:
            return json.dumps({"runs": [], "message": "Store unavailable"})

        raw_limit = args.get("limit", 10)
        try:
            limit = max(1, min(50, int(raw_limit)))
        except (TypeError, ValueError):
            limit = 10

        runs = self.store.list_runs(limit=limit)
        out = []
        for r in runs:
            out.append({
                "run_id": r.run_id,
                "target": r.target,
                "status": r.status,
                "started_at": r.started_at.isoformat(),
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "total_findings": r.total_findings,
                "total_evidence": r.total_evidence,
            })
        return json.dumps({"total": len(out), "runs": out}, indent=2)

    def _handle_run_sandbox_check(self, args: Dict[str, Any]) -> str:
        import uuid
        from forge.sandbox.models import ExecutionStatus, FailureClassification
        from forge.sandbox.policy import PolicyViolationError

        command = args.get("command")
        if not command or not isinstance(command, list):
            raise ToolExecutionError("command parameter must be a non-empty list of strings")

        relative_cwd = args.get("relative_cwd", ".")
        request_id = f"req_{uuid.uuid4().hex[:12]}"

        request = SandboxRequest(
            request_id=request_id,
            run_id=self.run_id,
            workspace_path=self.workspace_path,
            command=[str(c) for c in command],
            relative_cwd=str(relative_cwd),
            limits=ExecutionLimits(timeout_seconds=30.0),
            provenance="forge:agent_investigation",
        )

        result = self.sandbox.run(request)
        if self.store:
            try:
                record_sandbox_result_in_store(result, self.store, attach_as_evidence=True)
            except Exception:
                pass

        return json.dumps({
            "status": result.status.value,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "isolation_established": result.isolation_established,
            "error_message": result.error_message,
        }, indent=2)

    def _handle_record_finding(self, args: Dict[str, Any]) -> str:
        """Handle record_finding by independently verifying source evidence from disk and persisting Finding and Evidence."""
        if not self.store:
            raise ToolExecutionError("Database store unavailable: cannot persist findings")

        title = str(args.get("title", "")).strip()
        if not title:
            raise ToolExecutionError("title is required")

        description = str(args.get("description", "")).strip()
        if not description:
            raise ToolExecutionError("description is required")

        sev_str = str(args.get("severity", "medium")).lower().strip()
        try:
            severity = FindingSeverity(sev_str)
        except ValueError:
            raise ToolExecutionError(
                f"Invalid severity '{sev_str}'. Allowed: {[s.value for s in FindingSeverity]}"
            )

        conf_str = str(args.get("confidence", "medium")).lower().strip()
        try:
            confidence = FindingConfidence(conf_str)
        except ValueError:
            raise ToolExecutionError(
                f"Invalid confidence '{conf_str}'. Allowed: {[c.value for c in FindingConfidence]}"
            )

        raw_file = args.get("affected_file") or args.get("file_path")
        if not raw_file or not str(raw_file).strip():
            raise ToolExecutionError("affected_file parameter is required")
        raw_file_str = str(raw_file).strip()

        # Step 1-3: Path normalization & containment check
        try:
            norm_path = normalize_relative_path(raw_file_str)
        except EvidenceValidationError as exc:
            raise ToolExecutionError(f"Invalid affected_file path: {exc}")

        if not norm_path:
            raise ToolExecutionError("affected_file cannot be empty")

        candidate = self.workspace_path / norm_path
        resolved_file = assert_contained_path(candidate, self.workspace_path)

        if not resolved_file.exists() or not resolved_file.is_file():
            raise ToolExecutionError(f"Affected file not found: '{norm_path}'")

        # Reject symlinks escaping workspace
        assert_contained_path(resolved_file.resolve(), self.workspace_path)

        # Step 4-5: Line range validation
        start_line_raw = args.get("start_line", 1)
        end_line_raw = args.get("end_line", 1)
        try:
            start_line = int(start_line_raw)
            end_line = int(end_line_raw)
        except (TypeError, ValueError):
            raise ToolExecutionError("start_line and end_line must be valid integers")

        if start_line < 1:
            raise ToolExecutionError(f"start_line must be >= 1, got {start_line}")
        if end_line < start_line:
            raise ToolExecutionError(
                f"end_line must be >= start_line, got start_line={start_line}, end_line={end_line}"
            )

        # Step 6: Read actual source directly from disk
        try:
            file_content = resolved_file.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            raise ToolExecutionError(f"Failed to read file '{norm_path}': {exc}")

        lines = file_content.splitlines()
        total_lines = len(lines)
        if start_line > total_lines:
            raise ToolExecutionError(
                f"start_line {start_line} exceeds total file lines ({total_lines}) in '{norm_path}'"
            )

        actual_end = min(end_line, total_lines)
        slice_lines = lines[start_line - 1 : actual_end]
        if not slice_lines:
            raise ToolExecutionError(f"No source lines extracted for range {start_line}-{end_line}")

        numbered_lines = [
            f"{start_line + i:4d} | {line}"
            for i, line in enumerate(slice_lines)
        ]
        real_excerpt = (
            f"=== File: {norm_path} (Lines {start_line}-{actual_end} of {total_lines}) ===\n"
            + "\n".join(numbered_lines)
        )

        # Step 7: Integrity digest calculation
        raw_slice_text = "\n".join(slice_lines)
        content_digest = hashlib.sha256(raw_slice_text.encode("utf-8")).hexdigest()

        # Step 8: Deterministic claim validation, category consistency, static syntax, and deduplication
        suggested_step = args.get("suggested_step")
        limitations = args.get("limitations")
        defect_category = args.get("defect_category")

        # Check per-run rejected claim tracker to prevent duplicate/spam retries
        if self.rejected_tracker:
            prior_rejection = self.rejected_tracker.check_repeated_rejection(
                norm_path=norm_path,
                start_line=start_line,
                end_line=actual_end,
                defect_category=defect_category,
                title=title,
                description=description,
            )
            if prior_rejection:
                err_msg = (
                    f"Repeated rejected finding submission: A claim on '{norm_path}:{start_line}-{actual_end}' "
                    f"was already rejected ({prior_rejection.rejection_reason})."
                )
                repeat_err = RepeatedRejectedClaimError(
                    err_msg,
                    affected_file=norm_path,
                    start_line=start_line,
                    end_line=actual_end,
                    defect_category=defect_category,
                    rejection_type="RepeatedRejectedClaimError",
                )
                if self.store and self.run_id:
                    self.store.append_event(
                        self.run_id,
                        EventType.FINDING_VALIDATION_FAILED,
                        {
                            "error_type": "RepeatedRejectedClaimError",
                            "message": err_msg,
                            "file": norm_path,
                            "lines": f"{start_line}-{actual_end}",
                            "title": title,
                            "category": defect_category,
                            "prior_rejection_type": prior_rejection.rejection_type,
                        },
                    )
                raise repeat_err

        try:
            val_outcome = validate_finding_claim(
                workspace_path=self.workspace_path,
                norm_path=norm_path,
                resolved_file=resolved_file,
                start_line=start_line,
                end_line=actual_end,
                total_lines=total_lines,
                slice_lines=slice_lines,
                content_digest=content_digest,
                defect_category=defect_category,
                title=title,
                description=description,
                confidence=confidence,
                finding_provider=self.store,
                run_id=self.run_id,
                rejected_tracker=self.rejected_tracker,
            )
        except FindingValidationError as fve:
            if fve.affected_file is None:
                fve.affected_file = norm_path
            if fve.start_line is None:
                fve.start_line = start_line
            if fve.end_line is None:
                fve.end_line = actual_end
            if fve.defect_category is None:
                fve.defect_category = defect_category
            if fve.directive is None:
                fve.directive = build_rejection_directive(
                    rejection_type=fve.rejection_type,
                    message=fve.message,
                    affected_file=norm_path,
                    start_line=start_line,
                    end_line=actual_end,
                    defect_category=defect_category,
                )

            # Record in rejected-claim tracker so retries are intercepted
            if self.rejected_tracker and not isinstance(fve, RepeatedRejectedClaimError):
                self.rejected_tracker.record_rejected_claim(
                    norm_path=norm_path,
                    start_line=start_line,
                    end_line=actual_end,
                    defect_category=defect_category,
                    title=title,
                    description=description,
                    rejection_type=fve.__class__.__name__,
                    rejection_reason=str(fve),
                )

            if self.store and self.run_id:
                event_type = (
                    EventType.FINDING_DUPLICATE_DETECTED
                    if isinstance(fve, DuplicateFindingError)
                    else EventType.FINDING_VALIDATION_FAILED
                )
                self.store.append_event(
                    self.run_id,
                    event_type,
                    {
                        "error_type": fve.__class__.__name__,
                        "message": str(fve),
                        "file": norm_path,
                        "lines": f"{start_line}-{actual_end}",
                        "title": title,
                        "category": defect_category,
                    },
                )
            raise

        # Check if candidate finding was linked to an existing finding as additional evidence
        if val_outcome.action == "duplicate_linked" and val_outcome.duplicate_of_id:
            details = {
                "content_digest": content_digest,
                "digest_algorithm": "sha256",
                "file_path": norm_path,
                "start_line": start_line,
                "end_line": actual_end,
                "total_file_lines": total_lines,
                "defect_category": defect_category,
                "verified_against_workspace": True,
                "linked_as_duplicate": True,
            }
            evidence = create_evidence(
                run_id=self.run_id,
                evidence_type=EvidenceType.SOURCE_EXCERPT,
                description=f"Verified source excerpt from '{norm_path}' lines {start_line}-{actual_end}",
                content=real_excerpt,
                finding_id=val_outcome.duplicate_of_id,
                file_path=norm_path,
                start_line=start_line,
                end_line=actual_end,
                details=details,
                provenance="forge:investigator:source_verification",
            )
            self.store.attach_evidence(evidence)
            if self.store and self.run_id:
                self.store.append_event(
                    self.run_id,
                    EventType.FINDING_DUPLICATE_DETECTED,
                    {
                        "action": "linked_evidence",
                        "existing_finding_id": val_outcome.duplicate_of_id,
                        "evidence_id": evidence.evidence_id,
                        "file": norm_path,
                        "lines": f"{start_line}-{actual_end}",
                    },
                )
            return json.dumps({
                "status": "duplicate_linked",
                "finding_id": val_outcome.duplicate_of_id,
                "evidence_id": evidence.evidence_id,
                "message": f"Duplicate finding linked. Additional verified source evidence attached to existing finding '{val_outcome.duplicate_of_id}'.",
            }, indent=2)

        # Step 9: Persist validated Finding and Evidence in store
        final_confidence = val_outcome.adjusted_confidence or confidence
        final_limitations = limitations or val_outcome.limitations

        finding = self.store.create_finding(
            run_id=self.run_id,
            title=title,
            description=description,
            severity=severity,
            confidence=final_confidence,
            affected_files=[norm_path],
            suggested_step=suggested_step,
            limitations=final_limitations,
        )

        details = {
            "content_digest": content_digest,
            "digest_algorithm": "sha256",
            "file_path": norm_path,
            "start_line": start_line,
            "end_line": actual_end,
            "total_file_lines": total_lines,
            "defect_category": defect_category,
            "verified_against_workspace": True,
        }

        evidence = create_evidence(
            run_id=self.run_id,
            evidence_type=EvidenceType.SOURCE_EXCERPT,
            description=f"Verified source excerpt from '{norm_path}' lines {start_line}-{actual_end}",
            content=real_excerpt,
            finding_id=finding.finding_id,
            file_path=norm_path,
            start_line=start_line,
            end_line=actual_end,
            details=details,
            provenance="forge:investigator:source_verification",
        )
        self.store.attach_evidence(evidence)

        return json.dumps({
            "status": "success",
            "finding_id": finding.finding_id,
            "title": finding.title,
            "severity": finding.severity.value,
            "confidence": finding.confidence.value,
            "lifecycle_state": finding.lifecycle_state.value,
            "affected_file": norm_path,
            "lines": f"{start_line}-{actual_end}",
            "evidence_id": evidence.evidence_id,
            "content_digest": content_digest,
            "message": f"Finding '{finding.finding_id}' successfully recorded in 'suspected' state with verified source evidence '{evidence.evidence_id}'.",
        }, indent=2)
