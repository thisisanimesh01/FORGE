"""Task intake normalization and target repository revision pinning for FORGE Phase 5.

Phase 5, Milestone 1: Unified Task Intake & Repository Context Pipeline.

Invariants:
- Deterministic intake IDs based on SHA256 of source type, target, and base SHA.
- Revision pinning: inspects git HEAD without modification; rejects dirty working tree unless explicitly allowed.
- Path sanitization: rejects path traversal ('..') and absolute paths for affected files.
- Command validation: rejects shell command injection operators (';', '&&', '|', '`', '$') in test specifications.
- Secret scrubbing: redacts credentials from diffs, issues, and descriptions.
"""

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from forge.config import redact_secrets
from forge.reproduction.models import FieldProvenance
from forge.repository.acquisition import run_git_command
from forge.repository.validation import validate_target
from forge.review.parser import parse_unified_diff
from forge.storage.store import ForgeStore
from forge.workflow.models import (
    DiffIntakePayload,
    DirtyRepositoryError,
    FindingIntakePayload,
    IntakeSourceType,
    IntakeValidationError,
    IssueIntakePayload,
    RepositoryRevisionInfo,
    RevisionDriftError,
    TestSpecIntakePayload,
    WorkflowIntakeSpec,
)


FORBIDDEN_COMMAND_OPERATORS = [";", "&&", "||", "|", "`", "$(", "${", "\n", "\r", ">", "<"]


def sanitize_affected_paths(paths: Optional[List[str]]) -> List[str]:
    """Validate, normalize, and deduplicate relative file paths.

    Rejects:
    - Absolute paths
    - Path traversal ('..' components)
    - Empty or whitespace strings
    """
    if not paths:
        return []

    cleaned: List[str] = []
    for raw in paths:
        if not raw or not raw.strip():
            continue
        norm = Path(raw.strip()).as_posix()
        if norm.startswith("/") or norm.startswith("\\") or re.match(r"^[a-zA-Z]:", norm):
            raise IntakeValidationError(f"Absolute path forbidden in affected_files: '{raw}'")
        parts = norm.split("/")
        if ".." in parts:
            raise IntakeValidationError(f"Path traversal ('..') forbidden in affected_files: '{raw}'")
        if norm and norm != ".":
            cleaned.append(norm)

    return sorted(list(set(cleaned)))


def resolve_and_pin_revision(
    repo_path: Path,
    expected_sha: Optional[str] = None,
    allow_dirty: bool = False,
) -> RepositoryRevisionInfo:
    """Resolve and pin the exact git revision of a target repository.

    Validates:
    - Path exists and contains a git repository.
    - Resolves 40-character commit SHA.
    - Verifies whether working tree is dirty (modified, staged, or untracked files).
    - Detects revision drift if an expected SHA was supplied.
    """
    resolved_path = repo_path.resolve()
    if not resolved_path.exists() or not resolved_path.is_dir():
        raise IntakeValidationError(f"Target repository path does not exist: {resolved_path}")

    # Check git directory presence
    git_dir = resolved_path / ".git"
    if not git_dir.exists():
        raise IntakeValidationError(f"Target path is not a git repository: {resolved_path}")

    # 1. Resolve HEAD commit SHA
    res_head = run_git_command(["rev-parse", "HEAD"], cwd=resolved_path)
    if res_head.returncode != 0:
        err = redact_secrets(res_head.stderr or res_head.stdout)
        raise IntakeValidationError(f"Failed to resolve git HEAD commit for {resolved_path}: {err}")

    commit_sha = res_head.stdout.strip().lower()
    if not re.match(r"^[0-9a-f]{7,40}$", commit_sha):
        raise IntakeValidationError(f"Resolved invalid git commit SHA: '{commit_sha}'")

    # 2. Check for working tree dirtiness
    res_status = run_git_command(["status", "--porcelain"], cwd=resolved_path)
    dirty_files: List[str] = []
    if res_status.returncode == 0 and res_status.stdout.strip():
        for line in res_status.stdout.splitlines():
            line_str = line.strip()
            if line_str:
                # Format: XY PATH
                parts = line_str.split(maxsplit=1)
                dirty_files.append(parts[1] if len(parts) > 1 else parts[0])

    is_dirty = len(dirty_files) > 0
    if is_dirty and not allow_dirty:
        raise DirtyRepositoryError(
            f"Target repository '{resolved_path}' has uncommitted changes in {len(dirty_files)} files: "
            f"{dirty_files[:3]}. Clean working tree required or set allow_dirty=True."
        )

    # 3. Verify against expected commit SHA if provided
    if expected_sha:
        exp_clean = expected_sha.strip().lower()
        if not (commit_sha.startswith(exp_clean) or exp_clean.startswith(commit_sha)):
            raise RevisionDriftError(
                f"Target repository revision drift detected: current HEAD '{commit_sha}' "
                f"does not match expected pinned commit '{expected_sha}'."
            )

    # 4. Resolve branch name
    res_branch = run_git_command(["rev-parse", "--abbrev-ref", "HEAD"], cwd=resolved_path)
    branch = res_branch.stdout.strip() if res_branch.returncode == 0 else None
    if branch == "HEAD":
        branch = None

    # 5. Extract commit metadata
    res_msg = run_git_command(["log", "-1", "--format=%s"], cwd=resolved_path)
    head_msg = redact_secrets(res_msg.stdout.strip()) if res_msg.returncode == 0 else None

    res_time = run_git_command(["log", "-1", "--format=%cI"], cwd=resolved_path)
    head_time = res_time.stdout.strip() if res_time.returncode == 0 else None

    return RepositoryRevisionInfo(
        commit_sha=commit_sha,
        short_sha=commit_sha[:8],
        branch=branch,
        is_dirty=is_dirty,
        dirty_files=dirty_files,
        head_commit_message=head_msg,
        head_commit_timestamp=head_time,
    )


def compute_intake_id(
    source_type: IntakeSourceType,
    target_repo: str,
    base_commit_sha: str,
    payload_key: str,
) -> str:
    """Generate a deterministic intake identifier."""
    raw = f"{source_type.value}:{target_repo}:{base_commit_sha}:{payload_key}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:16]
    return f"intake_{source_type.value[:4]}_{digest}"


def create_intake_from_finding(
    repo_path: Path,
    finding_id: str,
    store: Optional[ForgeStore] = None,
    expected_sha: Optional[str] = None,
    allow_dirty: bool = False,
) -> WorkflowIntakeSpec:
    """Construct a validated WorkflowIntakeSpec from an existing finding in storage."""
    if not finding_id or not finding_id.strip():
        raise IntakeValidationError("finding_id cannot be empty")

    active_store = store or ForgeStore()
    finding = active_store.get_finding(finding_id.strip())
    if not finding:
        raise IntakeValidationError(f"Finding '{finding_id}' not found in storage")

    rev_info = resolve_and_pin_revision(
        repo_path=repo_path,
        expected_sha=expected_sha,
        allow_dirty=allow_dirty,
    )

    clean_files = sanitize_affected_paths(finding.affected_files)
    intake_id = compute_intake_id(
        source_type=IntakeSourceType.REVIEW_FINDING,
        target_repo=str(repo_path.resolve()),
        base_commit_sha=rev_info.commit_sha,
        payload_key=finding.finding_id,
    )

    finding_payload = FindingIntakePayload(
        finding_id=finding.finding_id,
        run_id=finding.run_id,
        severity=finding.severity,
        confidence=finding.confidence,
        evidence_ids=finding.supporting_evidence_ids,
        category="defect",
    )

    provenance = {
        "finding_id": FieldProvenance.STATIC_ANALYSIS,
        "title": FieldProvenance.STATIC_ANALYSIS,
        "description": FieldProvenance.STATIC_ANALYSIS,
        "affected_files": FieldProvenance.STATIC_ANALYSIS,
        "base_commit_sha": FieldProvenance.SYSTEM_POLICY,
    }

    return WorkflowIntakeSpec(
        intake_id=intake_id,
        source_type=IntakeSourceType.REVIEW_FINDING,
        target_repo=str(repo_path.resolve()),
        base_commit_sha=rev_info.commit_sha,
        title=finding.title,
        description=finding.description,
        affected_files=clean_files,
        finding_payload=finding_payload,
        metadata={"run_id": finding.run_id, "branch": rev_info.branch},
        provenance=provenance,
    )


def create_intake_from_diff(
    repo_path: Path,
    diff_text: str,
    title: Optional[str] = None,
    expected_sha: Optional[str] = None,
    allow_dirty: bool = False,
) -> WorkflowIntakeSpec:
    """Construct a validated WorkflowIntakeSpec from raw unified diff text."""
    if not diff_text or not diff_text.strip():
        raise IntakeValidationError("diff_text cannot be empty")

    sanitized_diff = redact_secrets(diff_text)
    bundle = parse_unified_diff(sanitized_diff)

    if not bundle.files:
        # Check if diff has standard unified diff headers (--- / +++) without diff --git
        file_pattern = re.compile(r"^\+\+\+\s+[b/]*(.*)$", re.MULTILINE)
        matches = list(file_pattern.finditer(sanitized_diff))
        if matches:
            rebuilt_lines: List[str] = []
            for line in sanitized_diff.splitlines():
                if line.startswith("--- ") and not any(l.startswith("diff --git") for l in rebuilt_lines):
                    raw = line[4:].strip().split("\t")[0]
                    if raw.startswith("a/"):
                        raw = raw[2:]
                    rebuilt_lines.append(f"diff --git a/{raw} b/{raw}")
                rebuilt_lines.append(line)
            bundle = parse_unified_diff("\n".join(rebuilt_lines))

    touched_files = [f.path for f in bundle.files] if bundle.files else []
    if not touched_files:
        file_pattern = re.compile(r"^\+\+\+\s+[b/]*(.*)$", re.MULTILINE)
        for m in file_pattern.finditer(sanitized_diff):
            raw = m.group(1).strip().split("\t")[0]
            if raw.startswith("b/"):
                raw = raw[2:]
            if raw and raw != "/dev/null":
                touched_files.append(raw)

    if not touched_files:
        raise IntakeValidationError("Unified diff contains no parseable file changes or hunks")

    additions = bundle.total_additions
    deletions = bundle.total_deletions
    if additions == 0 and deletions == 0:
        for line in sanitized_diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1

    rev_info = resolve_and_pin_revision(
        repo_path=repo_path,
        expected_sha=expected_sha,
        allow_dirty=allow_dirty,
    )

    clean_files = sanitize_affected_paths(touched_files)

    diff_digest = hashlib.sha256(sanitized_diff.encode("utf-8")).hexdigest()[:16]
    intake_id = compute_intake_id(
        source_type=IntakeSourceType.UNIFIED_DIFF,
        target_repo=str(repo_path.resolve()),
        base_commit_sha=rev_info.commit_sha,
        payload_key=diff_digest,
    )

    diff_payload = DiffIntakePayload(
        raw_diff=sanitized_diff,
        base_revision=bundle.base_revision,
        head_revision=bundle.head_revision,
        total_additions=additions,
        total_deletions=deletions,
        diff_bytes=len(sanitized_diff.encode("utf-8")),
    )

    resolved_title = title.strip() if title and title.strip() else f"Diff review on {len(clean_files)} files"
    desc = f"Unified diff intake affecting {len(clean_files)} files (+{bundle.total_additions}/-{bundle.total_deletions})."

    provenance = {
        "raw_diff": FieldProvenance.USER_INPUT,
        "affected_files": FieldProvenance.STATIC_ANALYSIS,
        "base_commit_sha": FieldProvenance.SYSTEM_POLICY,
    }

    return WorkflowIntakeSpec(
        intake_id=intake_id,
        source_type=IntakeSourceType.UNIFIED_DIFF,
        target_repo=str(repo_path.resolve()),
        base_commit_sha=rev_info.commit_sha,
        title=resolved_title,
        description=desc,
        affected_files=clean_files,
        diff_payload=diff_payload,
        metadata={"total_files": len(clean_files), "branch": rev_info.branch},
        provenance=provenance,
    )


def create_intake_from_issue(
    repo_path: Path,
    title: str,
    issue_text: str,
    affected_files: Optional[List[str]] = None,
    expected_sha: Optional[str] = None,
    allow_dirty: bool = False,
    reproduction_hints: Optional[List[str]] = None,
    issue_id: Optional[str] = None,
) -> WorkflowIntakeSpec:
    """Construct a validated WorkflowIntakeSpec from an issue description."""
    if not title or not title.strip():
        raise IntakeValidationError("Issue title cannot be empty")
    if not issue_text or not issue_text.strip():
        raise IntakeValidationError("Issue description cannot be empty")

    sanitized_title = redact_secrets(title.strip())
    sanitized_desc = redact_secrets(issue_text.strip())
    clean_files = sanitize_affected_paths(affected_files)

    rev_info = resolve_and_pin_revision(
        repo_path=repo_path,
        expected_sha=expected_sha,
        allow_dirty=allow_dirty,
    )

    issue_key = issue_id.strip() if issue_id and issue_id.strip() else sanitized_title
    intake_id = compute_intake_id(
        source_type=IntakeSourceType.ISSUE_DESCRIPTION,
        target_repo=str(repo_path.resolve()),
        base_commit_sha=rev_info.commit_sha,
        payload_key=issue_key,
    )

    issue_payload = IssueIntakePayload(
        issue_id=issue_id,
        issue_text=sanitized_desc,
        reproduction_hints=reproduction_hints or [],
    )

    provenance = {
        "title": FieldProvenance.USER_INPUT,
        "issue_text": FieldProvenance.USER_INPUT,
        "affected_files": FieldProvenance.USER_INPUT if clean_files else FieldProvenance.SYSTEM_POLICY,
        "base_commit_sha": FieldProvenance.SYSTEM_POLICY,
    }

    return WorkflowIntakeSpec(
        intake_id=intake_id,
        source_type=IntakeSourceType.ISSUE_DESCRIPTION,
        target_repo=str(repo_path.resolve()),
        base_commit_sha=rev_info.commit_sha,
        title=sanitized_title,
        description=sanitized_desc,
        affected_files=clean_files,
        issue_payload=issue_payload,
        metadata={"branch": rev_info.branch},
        provenance=provenance,
    )


def create_intake_from_test_spec(
    repo_path: Path,
    test_file: str,
    test_command: str,
    test_name: Optional[str] = None,
    expected_failure_pattern: Optional[str] = None,
    expected_sha: Optional[str] = None,
    allow_dirty: bool = False,
    framework: str = "pytest",
) -> WorkflowIntakeSpec:
    """Construct a validated WorkflowIntakeSpec from a test specification."""
    if not test_file or not test_file.strip():
        raise IntakeValidationError("test_file cannot be empty")
    if not test_command or not test_command.strip():
        raise IntakeValidationError("test_command cannot be empty")

    clean_test_files = sanitize_affected_paths([test_file.strip()])
    if not clean_test_files:
        raise IntakeValidationError(f"Invalid test file path: '{test_file}'")
    clean_test_file = clean_test_files[0]

    # Validate test_command against shell injection
    cmd_strip = test_command.strip()
    for op in FORBIDDEN_COMMAND_OPERATORS:
        if op in cmd_strip:
            raise IntakeValidationError(
                f"Forbidden shell operator '{op}' detected in test_command: '{cmd_strip}'"
            )

    rev_info = resolve_and_pin_revision(
        repo_path=repo_path,
        expected_sha=expected_sha,
        allow_dirty=allow_dirty,
    )

    intake_id = compute_intake_id(
        source_type=IntakeSourceType.TEST_SPECIFICATION,
        target_repo=str(repo_path.resolve()),
        base_commit_sha=rev_info.commit_sha,
        payload_key=f"{clean_test_file}:{cmd_strip}",
    )

    test_payload = TestSpecIntakePayload(
        test_file=clean_test_file,
        test_command=cmd_strip,
        test_name=test_name.strip() if test_name else None,
        expected_failure_pattern=expected_failure_pattern,
        framework=framework,
    )

    title = f"Failing test: {test_name or clean_test_file}"
    desc = f"Defect reported via failing test command: `{cmd_strip}`"

    provenance = {
        "test_file": FieldProvenance.USER_INPUT,
        "test_command": FieldProvenance.USER_INPUT,
        "base_commit_sha": FieldProvenance.SYSTEM_POLICY,
    }

    return WorkflowIntakeSpec(
        intake_id=intake_id,
        source_type=IntakeSourceType.TEST_SPECIFICATION,
        target_repo=str(repo_path.resolve()),
        base_commit_sha=rev_info.commit_sha,
        title=title,
        description=desc,
        affected_files=[clean_test_file],
        test_payload=test_payload,
        metadata={"framework": framework, "branch": rev_info.branch},
        provenance=provenance,
    )


def validate_intake_spec(
    spec: WorkflowIntakeSpec,
    repo_path: Optional[Path] = None,
) -> Tuple[bool, List[str]]:
    """Validate an intake specification offline and against an optional target repository.

    Checks:
    - Base commit SHA format
    - Affected file path containment
    - Target repository revision alignment if repo_path provided
    - Presence of appropriate payload for source type
    """
    reasons: List[str] = []

    # 1. Base commit format
    if not re.match(r"^[0-9a-f]{7,40}$", spec.base_commit_sha):
        reasons.append(f"Invalid base commit SHA: '{spec.base_commit_sha}'")

    # 2. Check affected files
    for f in spec.affected_files:
        if f.startswith("/") or ".." in f.split("/"):
            reasons.append(f"Path traversal or absolute path in affected_files: '{f}'")

    # 3. Payload consistency
    if spec.source_type == IntakeSourceType.REVIEW_FINDING and not spec.finding_payload:
        reasons.append("Missing finding_payload for REVIEW_FINDING intake")
    elif spec.source_type == IntakeSourceType.UNIFIED_DIFF and not spec.diff_payload:
        reasons.append("Missing diff_payload for UNIFIED_DIFF intake")
    elif spec.source_type == IntakeSourceType.ISSUE_DESCRIPTION and not spec.issue_payload:
        reasons.append("Missing issue_payload for ISSUE_DESCRIPTION intake")
    elif spec.source_type == IntakeSourceType.TEST_SPECIFICATION and not spec.test_payload:
        reasons.append("Missing test_payload for TEST_SPECIFICATION intake")

    # 4. Target repository checks if provided
    if repo_path:
        try:
            rev_info = resolve_and_pin_revision(
                repo_path=repo_path,
                expected_sha=spec.base_commit_sha,
                allow_dirty=True,  # Validation check only
            )
        except Exception as e:
            reasons.append(f"Repository revision check failed: {e}")

    return len(reasons) == 0, reasons
