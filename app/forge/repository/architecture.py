"""Architecture mapping and LLM context budget management."""

from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field

from forge.repository.detector import ProjectIntelligence
from forge.repository.indexer import SymbolIndex
from forge.repository.instructions import InstructionFile, format_instructions_for_context


class RepositoryMap(BaseModel):
    """Compact architecture and structure map of the repository."""
    target: str
    resolved_commit_sha: Optional[str] = None
    target_branch: Optional[str] = None
    intelligence: ProjectIntelligence
    symbol_index: SymbolIndex
    instructions: List[InstructionFile] = Field(default_factory=list)

    def render_markdown_summary(self) -> str:
        """Produce a clean human-readable Markdown architecture summary."""
        lines = [
            f"# Repository Architecture: {self.target}",
            f"- **Branch**: {self.target_branch or 'default'}",
            f"- **Commit SHA**: {self.resolved_commit_sha or 'unknown'}",
            f"- **Python Project**: {'Yes' if self.intelligence.is_python else 'No'}",
            f"- **Total Files**: {self.intelligence.total_files} ({self.intelligence.total_python_files} Python files)",
            f"- **Total Lines of Code**: {self.intelligence.total_lines}",
        ]

        if self.intelligence.detected_frameworks:
            lines.append(f"- **Detected Frameworks**: {', '.join(self.intelligence.detected_frameworks)}")

        if self.intelligence.entrypoints:
            lines.append(f"- **Entrypoints**: {', '.join(self.intelligence.entrypoints)}")

        if self.intelligence.configuration_files:
            lines.append(f"- **Config Files**: {', '.join(self.intelligence.configuration_files)}")

        if self.intelligence.package_directories:
            lines.append(f"- **Packages**: {', '.join(self.intelligence.package_directories)}")

        if self.intelligence.test_directories:
            lines.append(f"- **Test Dirs**: {', '.join(self.intelligence.test_directories)}")

        lines.append("")
        lines.append("## Symbol Index Overview")
        lines.append(f"- Classes: {self.symbol_index.total_classes}")
        lines.append(f"- Functions: {self.symbol_index.total_functions}")
        lines.append(f"- Methods: {self.symbol_index.total_methods}")
        lines.append(f"- Imports: {self.symbol_index.total_imports}")

        if self.instructions:
            lines.append("")
            lines.append("## Detected Instruction Files")
            for inst in self.instructions:
                trunc_note = " (truncated)" if inst.was_truncated else ""
                lines.append(f"- `{inst.file_path}` ({inst.char_count} chars){trunc_note}")

        return "\n".join(lines)


def build_context_prompt(
    repo_map: RepositoryMap,
    max_char_budget: int = 15_000,
) -> str:
    """Build a budgeted LLM investigation context with explicit truncation markers."""
    header = (
        "=== SYSTEM CONTEXT: REPOSITORY ARCHITECTURE MAP ===\n"
        f"Target: {repo_map.target}\n"
        f"Commit: {repo_map.resolved_commit_sha or 'unknown'}\n"
        f"Frameworks: {', '.join(repo_map.intelligence.detected_frameworks) or 'None'}\n"
        f"Entrypoints: {', '.join(repo_map.intelligence.entrypoints) or 'None'}\n"
        f"Packages: {', '.join(repo_map.intelligence.package_directories) or 'None'}\n"
        f"Test Directories: {', '.join(repo_map.intelligence.test_directories) or 'None'}\n"
        "===================================================\n\n"
    )

    # Key symbols excerpt
    symbol_lines = ["=== SYMBOL CATALOG (EXCERPT) ==="]
    for sym in repo_map.symbol_index.symbols[:50]:  # Top 50 symbols
        parent = f"{sym.parent_class}." if sym.parent_class else ""
        doc = f" - {sym.docstring[:60]}..." if sym.docstring else ""
        symbol_lines.append(f"[{sym.symbol_type.upper()}] {sym.file_path}:{sym.start_line} -> {parent}{sym.name}{doc}")
    symbol_lines.append("=================================\n\n")
    symbols_text = "\n".join(symbol_lines)

    # Untrusted instructions
    instructions_text = format_instructions_for_context(repo_map.instructions)

    full_context = header + symbols_text + instructions_text

    if len(full_context) > max_char_budget:
        truncated = full_context[:max_char_budget] + f"\n\n[TRUNCATED: Context exceeded character budget of {max_char_budget}]"
        return truncated

    return full_context
