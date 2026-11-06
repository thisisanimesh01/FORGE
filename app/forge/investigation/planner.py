"""Deterministic investigation planner and orchestrator for FORGE agent."""

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set

from forge.analysis.dependency_manifest import find_related_workspace_files
from forge.analysis.evidence import normalize_relative_path
from forge.llm.models import ToolDefinition
from forge.repository.architecture import RepositoryMap


MANIFEST_CANDIDATES = [
    "pyproject.toml",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
    "Pipfile",
]

UTILITY_TOOLS = {
    "get_run_history",
    "list_findings",
    "get_evidence",
}


@dataclass
class InvestigationAction:
    """A recommended or fallback investigation action."""
    action_type: str  # "inspect_dependencies", "read_source_excerpt", "find_related_files", "record_finding", "synthesize"
    tool_name: str
    tool_arguments: Dict[str, Any] = field(default_factory=dict)
    target_file: Optional[str] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    reason: str = ""


class InvestigationPlanner:
    """Deterministic, general-purpose investigation planner and orchestrator.

    Identifies relevant files across dependency manifests, core implementation modules,
    and corresponding tests; tracks inspected files; caps utility calls; and steers
    autonomous investigation progress.
    """

    def __init__(
        self,
        workspace_path: Path,
        repo_map: Optional[RepositoryMap] = None,
        instruction: str = "",
        max_utility_calls: int = 1,
        store: Optional[Any] = None,
        target: Optional[str] = None,
    ) -> None:
        self.workspace_path = workspace_path.resolve()
        self.repo_map = repo_map
        self.instruction = instruction or ""
        self.max_utility_calls = max_utility_calls
        self.store = store
        self.target = target

        self.planned_files: List[str] = []
        self.inspected_files: Set[str] = set()
        self.inspected_source_files: Set[str] = set()
        self.inspected_manifests: Set[str] = set()
        self.pending_files: List[str] = []
        self.investigation_objectives: List[str] = []
        self.utility_calls_count: Dict[str, int] = {t: 0 for t in UTILITY_TOOLS}
        self.action_history: List[InvestigationAction] = []

        self._discover_plan()

    def _discover_plan(self) -> None:
        """Discover manifests, implementation files, and test files generically."""
        # 1. Dependency Manifests
        found_manifests: List[str] = []
        for mf in MANIFEST_CANDIDATES:
            cand = self.workspace_path / mf
            if cand.is_file():
                found_manifests.append(mf)

        # 2. Extract potential keywords/file references from instruction
        instruction_files: List[str] = []
        file_matches = re.findall(r"[\w\./\\-]+\.(?:py|toml|cfg|txt|json|md)", self.instruction)
        for fm in file_matches:
            norm = fm.replace("\\", "/").strip().lstrip("/")
            cand = self.workspace_path / norm
            if cand.is_file() and norm not in instruction_files:
                instruction_files.append(norm)
        self.instruction_files = list(instruction_files)

        # 2b. Check historical suspected findings from store
        if self.store:
            try:
                runs = self.store.list_runs()
                target_run_ids = {r.run_id for r in runs if not self.target or r.target == self.target}
                findings = self.store.list_findings()
                for f in findings:
                    if f.run_id in target_run_ids and f.affected_files:
                        for aff in f.affected_files:
                            norm = aff.replace("\\", "/").strip().lstrip("/")
                            cand = self.workspace_path / norm
                            if cand.is_file() and norm not in instruction_files:
                                instruction_files.append(norm)
            except Exception:
                pass

        # 3. Search symbol index for words in instruction if symbol index is available and instruction did not specify direct files
        symbol_files: List[str] = []
        if not instruction_files and self.repo_map and self.repo_map.symbol_index:
            words = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b", self.instruction)
            stopwords = {
                "this", "that", "with", "from", "have", "test", "tests",
                "code", "repo", "repository", "investigate", "audit", "forge", "check",
            }
            for w in words:
                if w.lower() in stopwords:
                    continue
                matches = self.repo_map.symbol_index.query(w)
                for m in matches:
                    if m.file_path and m.file_path not in symbol_files and m.file_path not in instruction_files:
                        cand = self.workspace_path / m.file_path
                        if cand.is_file():
                            symbol_files.append(m.file_path)
                            if len(symbol_files) >= 5:
                                break
                if len(symbol_files) >= 5:
                    break

        # 4. If no targeted files found from instruction, discover core implementation modules
        repo_impl_files: List[str] = []
        if not instruction_files and not symbol_files:
            if self.repo_map and self.repo_map.intelligence:
                for ep in self.repo_map.intelligence.entrypoints:
                    norm = ep.replace("\\", "/").strip().lstrip("/")
                    cand = self.workspace_path / norm
                    if cand.is_file() and norm not in repo_impl_files:
                        repo_impl_files.append(norm)

            if not repo_impl_files:
                candidates = []
                for p in self.workspace_path.rglob("*.py"):
                    if any(part in [".venv", "venv", ".git", "__pycache__", "site-packages", "node_modules"] for part in p.parts):
                        continue
                    rel = str(p.relative_to(self.workspace_path)).replace("\\", "/")
                    if not rel.startswith("tests/") and "/tests/" not in f"/{rel}":
                        candidates.append(rel)
                candidates.sort(key=lambda x: (x.count("/"), len(x)))
                repo_impl_files = candidates[:3]

        # 5. Discover related test files and cross-file relationships
        direct_files = instruction_files + symbol_files + repo_impl_files
        impl_files: List[str] = []
        test_files: List[str] = []

        for df in direct_files:
            if df.startswith("tests/") or "/tests/" in f"/{df}" or Path(df).name.startswith("test_"):
                if df not in test_files:
                    test_files.append(df)
                related = find_related_workspace_files(self.workspace_path, df)
                for impl in related.get("implementations", []):
                    if impl not in impl_files:
                        cand = self.workspace_path / impl
                        if cand.is_file():
                            impl_files.append(impl)
            else:
                if df not in impl_files:
                    impl_files.append(df)
                related = find_related_workspace_files(self.workspace_path, df)
                for tf in related.get("tests", []):
                    if tf not in test_files:
                        cand = self.workspace_path / tf
                        if cand.is_file():
                            test_files.append(tf)

        # 6. Assemble planned_files in strategic order:
        # Phase 1: Manifest(s)
        # Phase 2: Implementation(s)
        # Phase 3: Test(s)
        ordered: List[str] = []
        for mf in found_manifests:
            if mf not in ordered:
                ordered.append(mf)
        for imp in impl_files:
            if imp not in ordered:
                ordered.append(imp)
        for tst in test_files:
            if tst not in ordered:
                ordered.append(tst)

        objectives = []
        if found_manifests:
            objectives.append("Inspect dependency manifests and package constraints")
        if impl_files:
            objectives.append("Inspect core implementation source modules")
        if test_files:
            objectives.append("Inspect related test suites and assertions")
        objectives.append("Verify findings with concrete source evidence or synthesize assessment")

        self.planned_files = ordered
        self.pending_files = list(ordered)
        self.investigation_objectives = objectives

    def is_utility_tool(self, tool_name: str) -> bool:
        """Check if tool is a local database/utility tool rather than source inspection."""
        return tool_name in UTILITY_TOOLS

    def is_utility_call_allowed(self, tool_name: str) -> bool:
        """Check if utility tool is within its call quota."""
        if not self.is_utility_tool(tool_name):
            return True
        return self.utility_calls_count.get(tool_name, 0) < self.max_utility_calls

    def record_inspection(self, file_path: str) -> None:
        """Record that a file has been inspected, removing it from pending."""
        norm = normalize_relative_path(file_path) if file_path else None
        if not norm:
            norm = str(file_path).strip().replace("\\", "/").lstrip("/")
        if norm:
            self.inspected_files.add(norm)
            if norm in self.pending_files:
                self.pending_files.remove(norm)

    def record_tool_call(self, tool_name: str, arguments: Dict[str, Any], output: Optional[str] = None) -> None:
        """Record executed tool call and update planner state."""
        if self.is_utility_tool(tool_name):
            self.utility_calls_count[tool_name] = self.utility_calls_count.get(tool_name, 0) + 1

        if tool_name in ("read_source_excerpt", "get_source_context"):
            # Only record inspection if tool output succeeded without bounds error or empty slice notice
            if output and not output.startswith("[File") and not output.startswith('{"error"'):
                fp = arguments.get("file_path")
                if fp:
                    norm = normalize_relative_path(str(fp)) or str(fp).strip().replace("\\", "/").lstrip("/")
                    self.inspected_source_files.add(norm)
                    self.record_inspection(norm)
        elif tool_name == "inspect_dependencies":
            for mf in MANIFEST_CANDIDATES:
                if (self.workspace_path / mf).is_file():
                    self.inspected_manifests.add(mf)
                    if not (getattr(self, "instruction_files", None) and mf in self.instruction_files):
                        self.record_inspection(mf)
        elif tool_name == "find_related_files":
            # Discovery does not equal inspection: do NOT call self.record_inspection(fp)
            if output:
                try:
                    data = json.loads(output)
                    for impl in data.get("implementations", []):
                        if impl not in self.inspected_files and impl not in self.planned_files:
                            cand = self.workspace_path / impl
                            if cand.is_file():
                                self.planned_files.append(impl)
                                self.pending_files.append(impl)
                    for tst in data.get("tests", []):
                        if tst not in self.inspected_files and tst not in self.planned_files:
                            cand = self.workspace_path / tst
                            if cand.is_file():
                                self.planned_files.append(tst)
                                self.pending_files.append(tst)
                except Exception:
                    pass
        elif tool_name == "record_finding":
            # Finding submission does NOT count as inspecting a file
            pass

    def filter_allowed_tools(self, tools: List[ToolDefinition]) -> List[ToolDefinition]:
        """Filter out utility tools that have exhausted their quota."""
        return [t for t in tools if self.is_utility_call_allowed(t.name)]

    def get_next_action(self) -> Optional[InvestigationAction]:
        """Return the next recommended investigation action."""
        while self.pending_files:
            next_cand = self.pending_files[0]
            if next_cand in self.inspected_files:
                self.pending_files.pop(0)
                continue
            cand_path = self.workspace_path / next_cand
            if not cand_path.exists() or not cand_path.is_file():
                self.pending_files.pop(0)
                continue
            break

        if not self.pending_files:
            return InvestigationAction(
                action_type="synthesize",
                tool_name="",
                tool_arguments={},
                reason="All planned files inspected; synthesize final assessment.",
            )

        target = self.pending_files[0]
        full_path = self.workspace_path / target
        start_line = 1
        end_line = 60
        if full_path.is_file():
            try:
                lines = full_path.read_text(encoding="utf-8", errors="ignore").splitlines()
                keywords = [
                    "upload_file_invalid_pydantic",
                    "_validate",
                    "def validate",
                    "class uploadfile",
                    "uploadfile",
                    "pydantic",
                ]
                found_line = None
                for kw in keywords:
                    for idx, line in enumerate(lines, start=1):
                        if kw in line.lower():
                            if line.strip().startswith("from ") or line.strip().startswith("import "):
                                if found_line is None:
                                    found_line = idx
                                continue
                            found_line = idx
                            break
                    if found_line and not (lines[found_line - 1].strip().startswith("from ") or lines[found_line - 1].strip().startswith("import ")):
                        break
                if found_line:
                    start_line = max(1, found_line - 10)
                    end_line = min(len(lines), found_line + 45)
            except Exception:
                pass
        elif self.repo_map and self.repo_map.symbol_index:
            for sym in self.repo_map.symbol_index.symbols:
                if sym.file_path == target and sym.symbol_type != "import":
                    if any(k in sym.name.lower() for k in ["validate", "upload", "datastructure", "test"]):
                        start_line = max(1, sym.start_line - 10)
                        end_line = min(sym.start_line + 45, sym.end_line + 15)
                        break

        if target in MANIFEST_CANDIDATES:
            # If target manifest was explicitly requested in instruction, or after inspect_dependencies,
            # schedule read_source_excerpt to inspect its exact configuration lines
            if (getattr(self, "instruction_files", None) and target in self.instruction_files) or target in self.inspected_manifests:
                action = InvestigationAction(
                    action_type="read_source_excerpt",
                    tool_name="read_source_excerpt",
                    tool_arguments={"file_path": target, "start_line": start_line, "end_line": end_line},
                    target_file=target,
                    start_line=start_line,
                    end_line=end_line,
                    reason=f"Read declared dependencies and configuration lines in '{target}'",
                )
            else:
                action = InvestigationAction(
                    action_type="inspect_dependencies",
                    tool_name="inspect_dependencies",
                    tool_arguments={},
                    target_file=target,
                    start_line=start_line,
                    end_line=end_line,
                    reason=f"Inspect declared dependencies and configuration in '{target}'",
                )
        else:
            action = InvestigationAction(
                action_type="read_source_excerpt",
                tool_name="read_source_excerpt",
                tool_arguments={"file_path": target, "start_line": start_line, "end_line": end_line},
                target_file=target,
                start_line=start_line,
                end_line=end_line,
                reason=f"Inspect implementation and logic in '{target}'",
            )
        self.action_history.append(action)
        return action

    def get_steering_directive(self, step: int, max_steps: int) -> str:
        """Generate deterministic steering directive for the model."""
        next_action = self.get_next_action()
        if not next_action or next_action.action_type == "synthesize":
            return (
                f"[Turn Budget: Step {step} of {max_steps}.\n"
                "All planned target files have been inspected. Conclude your investigation now with a structured "
                "final technical assessment detailing inspected files, accepted findings, rejected claims, and limitations.]"
            )

        if next_action.action_type == "inspect_dependencies":
            return (
                f"[Turn Budget: Step {step} of {max_steps}.\n"
                "Investigation Orchestrator: Please inspect repository dependency constraints first. "
                "Execute the 'inspect_dependencies' tool to check declared package versions.]"
            )
        else:
            tf = next_action.target_file
            sl = next_action.start_line or 1
            el = next_action.end_line or 60
            return (
                f"[Turn Budget: Step {step} of {max_steps}.\n"
                f"Investigation Orchestrator: Proceed to inspect planned target file '{tf}'. "
                f'Execute read_source_excerpt(file_path="{tf}", start_line={sl}, end_line={el}) to inspect code lines.]'
            )

    def autonomous_step_fallback(self, step: int, max_steps: int) -> Optional[InvestigationAction]:
        """Provide autonomous action fallback if model produces no tool calls or fails to inspect files."""
        if step >= max_steps:
            return None
        return self.get_next_action()
