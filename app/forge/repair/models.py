"""Data models, validation schemas, and policies for Phase 4 Repair Planning."""

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forge.repair.contracts import PatchCandidate, PatchValidationResult, RepairBudget, RepairOutcome
from forge.sandbox.contracts import ReproductionOutcome


class RepairPlanStatus(str, Enum):
    """Status lifecycle for an evidence-grounded repair plan."""
    PROPOSED = "proposed"       # Initial draft plan generated
    VALIDATED = "validated"     # Plan and patch candidate passed all safety/budget checks
    REJECTED = "rejected"       # Plan or patch violated safety, scope, or budget policy
    BLOCKED = "blocked"         # Plan generation blocked (e.g. unsafe scratch workspace, missing evidence)
    APPROVED = "approved"       # Human operator approved the plan


class ProtectedPathPolicy:
    """Security policy defining repository files that may never be modified by a repair patch."""

    PROTECTED_FILENAMES: Set[str] = {
        "AGENTS.md",
        "README.md",
        "LICENSE",
        "LICENSE.txt",
        "NOTICE",
        ".gitignore",
        ".gitattributes",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "forge.sqlite3",
    }

    PROTECTED_PREFIXES: Tuple[str, ...] = (
        ".git/",
        ".github/",
        ".venv/",
        "venv/",
        ".env",
        ".forge/",
    )

    @classmethod
    def is_protected(cls, file_path: str) -> Tuple[bool, Optional[str]]:
        """Return (True, reason) if file_path is protected from mutation."""
        norm = Path(file_path).as_posix().strip()
        if not norm or norm == ".":
            return True, "Empty or root path cannot be modified"
        if norm.startswith("/") or norm.startswith("\\"):
            return True, f"Absolute path '{file_path}' is forbidden"
        if ".." in norm.split("/"):
            return True, f"Path traversal in '{file_path}' is forbidden"
        if norm.startswith(".") and not norm.startswith("./"):
            # Hidden file or dot-dir
            return True, f"Hidden file/directory '{file_path}' is protected by policy"

        # Check exact basename
        name = Path(norm).name
        if name in cls.PROTECTED_FILENAMES:
            return True, f"File '{name}' is explicitly protected by security policy"

        # Check prefix
        for prefix in cls.PROTECTED_PREFIXES:
            if norm.startswith(prefix) or f"/{prefix}" in f"/{norm}":
                return True, f"Path '{file_path}' matches protected prefix '{prefix}'"

        return False, None


class StructuredPatchProposalSchema(BaseModel):
    """Strict schema for bounded structured search-and-replace code modification proposals."""
    model_config = ConfigDict(extra="ignore")

    target_file: str = Field(description="Relative path of file to modify")
    search_block: str = Field(description="Exact verbatim lines from the original target file to be replaced")
    replace_block: str = Field(description="Replacement lines to substitute in place of the search block")
    change_rationale: str = Field(default="", description="Concise explanation of the code modification rationale")


def configure_repair_response_schema(schema: Dict[str, Any]) -> None:
    """Ensure patch representation fields are declared required and nullable in wire JSON schema.

    Local LLM grammar engines (such as Ollama / llama.cpp) strictly omit optional fields unless
    they are present in schema['required']. Declaring them required while typing them as nullable
    ensures the model generates explicit values (string or null) on the wire.
    """
    required = schema.setdefault("required", [])
    for field in ("search_block", "replace_block", "unified_diff"):
        if field not in required:
            required.append(field)


class RepairModelResponseSchema(BaseModel):
    """Strict schema for parsing structured LLM repair diagnosis and patch proposals."""
    model_config = ConfigDict(extra="ignore", json_schema_extra=configure_repair_response_schema)

    root_cause: str = Field(description="Explanation of why the defect occurs")
    proposed_change: str = Field(description="Summary of proposed code modification")
    target_file: str = Field(description="Relative path of file to modify")
    expected_behavior: str = Field(description="Expected behavior after fix")
    regression_risks: List[str] = Field(default_factory=list, description="Potential side effects or regression risks")
    verification_plan: str = Field(description="Test command or verification step to validate repair")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0, description="Confidence score")
    uncertainty_notes: Optional[str] = Field(default=None, description="Known uncertainties or assumptions")
    search_block: Optional[str] = Field(default=None, description="Exact verbatim lines from original target file to be replaced")
    replace_block: Optional[str] = Field(default=None, description="Replacement lines to substitute in place of search_block")
    change_rationale: Optional[str] = Field(default=None, description="Concise explanation of the code modification rationale")
    unified_diff: Optional[str] = Field(
        default=None,
        description=(
            "Standard unified diff candidate patch starting with '--- a/<file>\\n+++ b/<file>\\n@@ -start,count +start,count @@'. "
            "Lines to add MUST start with '+', lines to remove MUST start with '-', and unchanged context lines MUST start with ' ' (single space). "
            "Empty diffs or diffs without '+' or '-' markers are forbidden."
        ),
    )

    @model_validator(mode="after")
    def validate_patch_proposal_representation(self) -> "RepairModelResponseSchema":
        has_search = self.search_block is not None and bool(self.search_block.strip())
        has_replace = self.replace_block is not None and bool(self.replace_block.strip())
        has_diff = self.unified_diff is not None and bool(self.unified_diff.strip())

        if has_search and not has_replace:
            raise ValueError("Partial structured proposal: 'search_block' provided without corresponding 'replace_block'")
        if has_replace and not has_search:
            raise ValueError("Partial structured proposal: 'replace_block' provided without corresponding 'search_block'")

        has_structured = has_search and has_replace

        if has_structured and has_diff:
            raise ValueError(
                "Ambiguous patch representation: Both structured search/replace blocks and 'unified_diff' were provided. "
                "Provide either structured blocks ('search_block' and 'replace_block') with 'unified_diff': null, "
                "or a legacy 'unified_diff' with structured blocks set to null."
            )

        if not has_structured and not has_diff:
            raise ValueError(
                "Missing patch representation: Repair proposal must provide either structured search/replace blocks "
                "('search_block' and 'replace_block') or a valid 'unified_diff'"
            )

        return self


class PlanValidationResult(BaseModel):
    """Comprehensive validation result for a RepairPlan."""
    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(description="ID of validated plan")
    is_valid: bool = Field(description="Whether the entire plan passed validation")
    violations: List[str] = Field(default_factory=list, description="List of policy or contract violations")
    evidence_valid: bool = Field(description="Whether cited evidence is present and verified")
    patch_valid: bool = Field(description="Whether unified diff is well-formed and bounded")
    scope_valid: bool = Field(description="Whether affected file is within scope and not protected")
    budget_valid: bool = Field(description="Whether patch complies with RepairBudget")
    base_sha_valid: bool = Field(description="Whether base SHA is valid and matches repository")
    validated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Timestamp")


class RepairPlan(BaseModel):
    """An evidence-grounded, bounded plan and candidate patch for a verified defect."""
    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(description="Unique deterministic identifier for this repair plan")
    finding_id: str = Field(description="Finding identifier this plan addresses")
    run_id: Optional[str] = Field(default=None, description="Associated run ID if tracked in SQLite")
    target_repo: str = Field(description="Target repository URL or absolute path")
    base_commit_sha: str = Field(description="Immutable base commit SHA pinned to the finding")
    cited_evidence_ids: List[str] = Field(description="Evidence record IDs proving defect presence")
    root_cause: str = Field(description="Identified root cause of the defect")
    proposed_change: str = Field(description="High-level description of proposed change")
    target_file: str = Field(description="Declared relative path of file targeted for repair")
    affected_paths: List[str] = Field(default_factory=list, description="All file paths affected by patch candidate")
    expected_behavior: str = Field(description="Expected behavior after repair")
    regression_risks: List[str] = Field(default_factory=list, description="Identified regression risks")
    verification_plan: str = Field(description="Command or check to verify repair in sandbox")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0, description="Confidence score")
    uncertainty_notes: Optional[str] = Field(default=None, description="Known uncertainties or limitations")
    require_human_approval: bool = Field(default=True, description="Enforced human approval gate")
    status: RepairPlanStatus = Field(default=RepairPlanStatus.PROPOSED, description="Current status of the repair plan")
    provider: str = Field(default="fake", description="LLM provider used for diagnosis and patch candidate")
    model: str = Field(default="forge-fake-model", description="LLM model used")
    patch_candidate: Optional[PatchCandidate] = Field(default=None, description="Validated unified diff patch candidate")
    validation: Optional[PlanValidationResult] = Field(default=None, description="Plan and patch validation result")
    scratch_workspace_path: Optional[str] = Field(default=None, description="Isolated scratch workspace path")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Creation timestamp")

    @field_validator("require_human_approval")
    @classmethod
    def validate_human_approval(cls, v: bool) -> bool:
        if not v:
            raise ValueError("require_human_approval cannot be disabled; human approval gate is mandatory")
        return v


class RepairAttemptRecord(BaseModel):
    """Immutable audit record for a single patch application and verification attempt."""
    model_config = ConfigDict(frozen=True)

    attempt_id: str = Field(description="Unique identifier for this attempt")
    plan_id: str = Field(description="Associated repair plan ID")
    attempt_number: int = Field(ge=1, description="Sequential attempt number for this plan")
    finding_id: str = Field(description="Associated finding ID")
    run_id: Optional[str] = Field(default=None, description="Associated run ID")
    base_commit_sha: str = Field(description="Pinned base commit revision under test")
    patch_digest: str = Field(description="SHA256 digest of candidate patch")
    outcome: RepairOutcome = Field(description="Classified repair outcome")
    pre_repro_outcome: ReproductionOutcome = Field(description="Pre-patch defect reproduction outcome")
    post_repro_outcome: ReproductionOutcome = Field(description="Post-patch defect reproduction outcome")
    exit_code: Optional[int] = Field(default=None, description="Observed process exit code")
    duration_ms: float = Field(default=0.0, ge=0.0, description="Execution duration in milliseconds")
    sandbox_available: bool = Field(default=True, description="Whether isolated container sandbox was available")
    oracle_verified: bool = Field(default=True, description="Whether verification was backed by verified oracle")
    diagnostic_message: Optional[str] = Field(default=None, description="Sanitized diagnostic message")
    isolation_details: Dict[str, Any] = Field(default_factory=dict, description="Metadata describing sandbox boundary")
    stdout_excerpt: str = Field(default="", description="Sanitized and bounded standard output excerpt")
    stderr_excerpt: str = Field(default="", description="Sanitized and bounded standard error excerpt")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Timestamp")


class RepairVerificationResult(BaseModel):
    """High-level outcome of an isolated patch verification execution."""
    model_config = ConfigDict(frozen=True)

    plan_id: str = Field(description="Associated repair plan ID")
    attempt_id: str = Field(description="Associated repair attempt ID")
    outcome: RepairOutcome = Field(description="Deterministic classified repair outcome")
    diagnostic: str = Field(description="Explanatory diagnostic of verification outcome")
    pre_repro_outcome: ReproductionOutcome = Field(description="Observed pre-patch reproduction outcome")
    post_repro_outcome: ReproductionOutcome = Field(description="Observed post-patch reproduction outcome")
    patch_applied: bool = Field(description="Whether patch candidate was applied cleanly in workspace")
    syntax_valid: bool = Field(description="Whether post-patch syntax was valid (e.g. AST parsed)")
    sandbox_available: bool = Field(description="Whether isolated container execution was available")
    oracle_verified: bool = Field(description="Whether verification oracle was present and verified")
    attempt_record: Optional[RepairAttemptRecord] = Field(default=None, description="Detailed attempt record")
    completed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Completion timestamp")
