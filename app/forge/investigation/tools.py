"""Typed, bounded, read-only investigation tools for FORGE agents."""

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from forge.config import redact_secrets
from forge.llm.models import ToolDefinition
from forge.repository.architecture import RepositoryMap
from forge.repository.indexer import SymbolIndex
from forge.repository.validation import assert_contained_path, SecurityError
from forge.sandbox.adapter import Sandbox
from forge.sandbox.docker import DockerSandbox
from forge.sandbox.evidence import record_sandbox_result_in_store
from forge.sandbox.models import SandboxRequest, ExecutionLimits
from forge.storage.store import ForgeStore


class ToolExecutionError(Exception):
    """Raised when a tool execution fails or violates constraints."""
    pass


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
    ):
        self.workspace_path = workspace_path.resolve()
        self.repo_map = repo_map
        self.symbol_index = symbol_index or (repo_map.symbol_index if repo_map else None)
        self.store = store
        self.run_id = run_id or "run_default"
        self.sandbox = sandbox or DockerSandbox()
        self.max_output_chars = max_output_chars
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
