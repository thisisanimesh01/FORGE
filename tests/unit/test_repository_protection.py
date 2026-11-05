"""Comprehensive security regression tests for centralized protected-file boundary enforcement."""

import json
import os
from pathlib import Path
import shutil
import pytest

from forge.analysis.validation import validate_repository_path, RepositoryPathValidationError
from forge.repository.acquisition import (
    AcquisitionError,
    acquire_repository,
    cleanup_repository,
    run_git_command,
)
from forge.repository.detector import detect_repository
from forge.repository.indexer import build_symbol_index, index_python_file
from forge.repository.instructions import load_instruction_files
from forge.repository.protection import (
    PROTECTED_FILE_PATTERNS,
    GitMetadataProtectionError,
    filter_unprotected_paths,
    is_protected_name,
    is_protected_path,
    purge_protected_git_metadata,
    purge_protected_paths_from_workspace,
)
from forge.repository.validation import TargetType, TargetValidationResult
from forge.schemas import EvidenceType
from forge.workflow.context import (
    assemble_repository_context,
    extract_source_excerpts,
    render_context_prompt,
)
from forge.workflow.dossier import validate_engineering_dossier
from forge.workflow.intake import (
    IntakeValidationError,
    create_intake_from_issue,
    sanitize_affected_paths,
)
from forge.workflow.models import (
    ContextBudget,
    IssueIntakePayload,
    MultiCheckVerificationPlan,
    VerificationCheckSpec,
    VerificationCheckType,
    WorkflowIntakeSpec,
)
from forge.workflow.verification import MultiCheckVerifier
from forge.repair.models import RepairPlan


# =============================================================================
# 1. Centralized Policy: Pattern Matching, Case Variations, and Path Containment
# =============================================================================

@pytest.mark.parametrize(
    "relative_path",
    [
        ".env",
        ".env.local",
        ".env.production",
        ".env.stage",
        "config/.ENV",
        "deep/nested/path/.env.secret",
        "data/memory.db",
        "data/memory.db-wal",
        "data/memory.db-shm",
        "nested/storage.sqlite.db",
        "contacts.json",
        "config/CONTACTS.JSON",
        "deep/backup/contacts.json",
        "logs/friday.log",
        "nested/debug.LOG",
        "audit/security.log",
    ],
)
def test_identifies_protected_paths(relative_path: str, tmp_path: Path):
    """Verify that all protected patterns, case variations, and nested paths are detected."""
    assert is_protected_path(tmp_path / relative_path)
    assert is_protected_path(relative_path)


@pytest.mark.parametrize(
    "relative_path",
    [
        "app.py",
        "README.md",
        "config/settings.yaml",
        "tests/test_app.py",
        "database.py",
        "catalog.json",
        "calculator.py",
        "docs/spec.txt",
    ],
)
def test_allows_ordinary_repository_paths(relative_path: str, tmp_path: Path):
    """Verify that ordinary non-sensitive repository files are permitted."""
    assert not is_protected_path(tmp_path / relative_path)
    assert not is_protected_path(relative_path)


def test_is_protected_name_helper():
    """Verify is_protected_name component checking."""
    assert is_protected_name(".env")
    assert is_protected_name(".ENV.local")
    assert is_protected_name("contacts.json")
    assert is_protected_name("CONTACTS.JSON")
    assert is_protected_name("memory.db")
    assert is_protected_name("friday.log")
    assert not is_protected_name("app.py")
    assert not is_protected_name("README.md")
    assert not is_protected_name("")


def test_filter_unprotected_paths(tmp_path: Path):
    """Verify filter_unprotected_paths cleanly separates permitted from protected files."""
    paths = [
        tmp_path / "app.py",
        tmp_path / ".env",
        tmp_path / "contacts.json",
        tmp_path / "calculator.py",
        tmp_path / "debug.log",
    ]
    filtered = filter_unprotected_paths(paths, base_dir=tmp_path)
    filtered_names = [p.name for p in filtered]
    assert filtered_names == ["app.py", "calculator.py"]


def test_protects_symlink_to_sensitive_file(tmp_path: Path):
    """Verify symlink pointing to a sensitive file is detected as protected."""
    sensitive_file = tmp_path / "outside" / ".env"
    sensitive_file.parent.mkdir(parents=True, exist_ok=True)
    sensitive_file.write_text("test-secret", encoding="utf-8")

    alias = tmp_path / "repository" / "config.txt"
    alias.parent.mkdir(parents=True, exist_ok=True)
    try:
        alias.symlink_to(sensitive_file)
    except OSError:
        pytest.skip("Symlinks are unavailable in this environment")

    assert is_protected_path(alias)


def test_protects_symlink_escaping_base_dir(tmp_path: Path):
    """Verify symlinks pointing outside the repository root are treated as protected."""
    outside_file = tmp_path / "outside" / "passwd"
    outside_file.parent.mkdir(parents=True, exist_ok=True)
    outside_file.write_text("root:x:0:0", encoding="utf-8")

    repo_dir = tmp_path / "repository"
    repo_dir.mkdir(parents=True, exist_ok=True)
    symlink_file = repo_dir / "external_link.py"

    try:
        symlink_file.symlink_to(outside_file)
    except OSError:
        pytest.skip("Symlinks are unavailable in this environment")

    assert is_protected_path(symlink_file, base_dir=repo_dir)


def test_fails_closed_when_path_resolution_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify fail-closed behavior when resolution raises OSError or RuntimeError."""
    def fail_resolution(self, strict=False):
        raise OSError("simulated resolution failure")

    monkeypatch.setattr(Path, "resolve", fail_resolution)
    assert is_protected_path(tmp_path / "ordinary.py")


def test_path_traversal_detection():
    """Verify path traversal strings are detected and fail closed."""
    assert is_protected_path("../.env")
    assert is_protected_path("sub/../../contacts.json")
    assert is_protected_path("")
    assert is_protected_path("   ")


# =============================================================================
# 2. Repository Acquisition: Local and Remote Exclusion
# =============================================================================

def test_local_repository_acquisition_excludes_protected_files(tmp_path: Path):
    """Acquisition of local repository must exclude sensitive files and leave source untouched."""
    source_dir = tmp_path / "source_repo"
    source_dir.mkdir()

    # Normal files
    (source_dir / "main.py").write_text("print('hello')", encoding="utf-8")
    (source_dir / "README.md").write_text("# Test Repo", encoding="utf-8")

    # Sensitive files
    (source_dir / ".env").write_text("SECRET_KEY=12345", encoding="utf-8")
    (source_dir / ".env.local").write_text("API_KEY=xyz", encoding="utf-8")
    (source_dir / "contacts.json").write_text('{"name": "Alice"}', encoding="utf-8")
    (source_dir / "memory.db").write_text("binary-db-content", encoding="utf-8")
    (source_dir / "friday.log").write_text("2026-11-05 log message", encoding="utf-8")

    sub_dir = source_dir / "config"
    sub_dir.mkdir()
    (sub_dir / ".env.prod").write_text("PROD_SECRET=999", encoding="utf-8")
    (sub_dir / "settings.yaml").write_text("debug: false", encoding="utf-8")

    workspace_base = tmp_path / "workspaces"

    acquired = acquire_repository(
        target=str(source_dir),
        custom_workspace=workspace_base,
        allow_local=True,
    )

    ws = acquired.workspace_path
    assert ws.exists()

    # Permitted files must exist in workspace
    assert (ws / "main.py").exists()
    assert (ws / "README.md").exists()
    assert (ws / "config" / "settings.yaml").exists()

    # Sensitive files must NOT exist in acquired workspace
    assert not (ws / ".env").exists()
    assert not (ws / ".env.local").exists()
    assert not (ws / "contacts.json").exists()
    assert not (ws / "memory.db").exists()
    assert not (ws / "friday.log").exists()
    assert not (ws / "config" / ".env.prod").exists()

    # Original target repository must remain completely untouched
    assert (source_dir / ".env").exists()
    assert (source_dir / "contacts.json").exists()
    assert (source_dir / "memory.db").exists()
    assert (source_dir / "friday.log").exists()


def test_purge_protected_paths_from_workspace(tmp_path: Path):
    """Purge function must remove all protected files and directories, preserving permitted files."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "app.py").write_text("code", encoding="utf-8")
    (ws / ".env").write_text("secret", encoding="utf-8")
    (ws / "contacts.json").write_text("contacts", encoding="utf-8")
    (ws / "memory.db").write_text("db", encoding="utf-8")
    (ws / "test.log").write_text("log", encoding="utf-8")

    nested = ws / "data"
    nested.mkdir()
    (nested / "valid.json").write_text("{}", encoding="utf-8")
    (nested / "store.db-wal").write_text("wal", encoding="utf-8")

    purged = purge_protected_paths_from_workspace(ws)
    assert purged >= 5

    assert (ws / "app.py").exists()
    assert (nested / "valid.json").exists()
    assert not (ws / ".env").exists()
    assert not (ws / "contacts.json").exists()
    assert not (ws / "memory.db").exists()
    assert not (ws / "test.log").exists()
    assert not (nested / "store.db-wal").exists()


# =============================================================================
# 3. Repository Detector: Metadata and File Discovery Exclusion
# =============================================================================

def test_detector_excludes_protected_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Repository detector must not discover, count, or read protected files."""
    repo = tmp_path / "detector_repo"
    repo.mkdir()

    (repo / "main.py").write_text("print('hello')", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname = 'test'", encoding="utf-8")
    (repo / "README.md").write_text("# Repo", encoding="utf-8")

    # Sensitive files
    (repo / ".env").write_text("AWS_SECRET_KEY=confidential", encoding="utf-8")
    (repo / "contacts.json").write_text('{"phone": "123456"}', encoding="utf-8")
    (repo / "memory.db").write_text("sqlite-data", encoding="utf-8")
    (repo / "app.log").write_text("error details", encoding="utf-8")
    (repo / ".env.py").write_text("SECRET = 1", encoding="utf-8")

    opened_files = []
    original_read_text = Path.read_text

    def tracking_read_text(self, *args, **kwargs):
        opened_files.append(str(self))
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracking_read_text)

    intel = detect_repository(repo)

    # Permitted files discovered
    assert intel.is_python
    assert "main.py" in intel.entrypoints
    assert "pyproject.toml" in [Path(f).name for f in intel.configuration_files]
    assert "README.md" in intel.instruction_files

    # Protected files must NOT be in metadata
    config_names = [Path(f).name for f in intel.configuration_files]
    assert ".env" not in config_names
    assert "contacts.json" not in config_names

    # None of the protected files should have been read
    for opened in opened_files:
        assert not is_protected_name(Path(opened).name), f"Protected file '{opened}' was read by detector!"

    # Total files must only count non-protected files (main.py, pyproject.toml, README.md = 3)
    assert intel.total_files == 3


# =============================================================================
# 4. Python Indexer: AST and Direct Indexing Exclusion
# =============================================================================

def test_indexer_excludes_protected_python_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Indexer must not parse or read Python files matching protected patterns."""
    repo = tmp_path / "indexer_repo"
    repo.mkdir()

    (repo / "calc.py").write_text("def add(a, b): return a + b", encoding="utf-8")
    protected_py = repo / ".env.py"
    protected_py.write_text("def secret_key(): return 'topsecret'", encoding="utf-8")

    opened_files = []
    original_read_text = Path.read_text

    def tracking_read_text(self, *args, **kwargs):
        opened_files.append(str(self))
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracking_read_text)

    # Direct API invocation
    direct_symbols = index_python_file(protected_py, repo)
    assert direct_symbols == [], "Direct indexing of protected file must return empty list"

    # Recursive build_symbol_index
    index = build_symbol_index(repo)
    indexed_files = list(index.file_symbols.keys())

    assert "calc.py" in indexed_files
    assert not any(is_protected_name(Path(f).name) for f in indexed_files)

    # Verify protected file was never read
    assert str(protected_py) not in opened_files, ".env.py was read during indexing!"


# =============================================================================
# 5. Instruction Loader: Exclusion of Protected Patterns
# =============================================================================

def test_instruction_loader_rejects_protected_instructions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Instruction loader must reject instruction files that are protected or symlinked to protected files."""
    repo = tmp_path / "inst_repo"
    repo.mkdir()

    # Normal instruction file
    (repo / "README.md").write_text("# Valid Guide", encoding="utf-8")

    # Symlink AGENTS.md -> contacts.json
    sensitive = repo / "contacts.json"
    sensitive.write_text('{"contacts": ["Alice"]}', encoding="utf-8")
    symlink_inst = repo / "AGENTS.md"
    try:
        symlink_inst.symlink_to(sensitive)
    except OSError:
        pytest.skip("Symlinks unavailable")

    opened_files = []
    original_read_text = Path.read_text

    def tracking_read_text(self, *args, **kwargs):
        opened_files.append(str(self))
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracking_read_text)

    instructions = load_instruction_files(repo)
    inst_paths = [i.file_path for i in instructions]

    assert "README.md" in inst_paths
    assert "AGENTS.md" not in inst_paths, "AGENTS.md symlinked to contacts.json must be excluded"
    assert str(sensitive) not in opened_files, "Sensitive target contacts.json was read by instruction loader!"


# =============================================================================
# 6. Workflow Context & Intake: Excerpts, Prompts, Hashing, and No-Read Proof
# =============================================================================

def test_sanitize_affected_paths_rejects_protected_files():
    """sanitize_affected_paths must raise IntakeValidationError when protected files are provided."""
    with pytest.raises(IntakeValidationError, match="Protected sensitive file forbidden"):
        sanitize_affected_paths([".env"])

    with pytest.raises(IntakeValidationError, match="Protected sensitive file forbidden"):
        sanitize_affected_paths(["config/contacts.json"])

    with pytest.raises(IntakeValidationError, match="Protected sensitive file forbidden"):
        sanitize_affected_paths(["data/memory.db"])

    with pytest.raises(IntakeValidationError, match="Protected sensitive file forbidden"):
        sanitize_affected_paths(["logs/run.log"])

    # Permitted paths must succeed
    cleaned = sanitize_affected_paths(["src/app.py", "pkg/calc.py"])
    assert cleaned == ["pkg/calc.py", "src/app.py"]


def test_extract_source_excerpts_no_read_and_no_hash_guarantee(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """extract_source_excerpts must NEVER read or hash protected files."""
    repo = tmp_path / "context_repo"
    repo.mkdir()

    (repo / "calculator.py").write_text("def add(x, y): return x + y\n", encoding="utf-8")
    (repo / ".env").write_text("SUPER_SECRET_TOKEN=xyz123\n", encoding="utf-8")
    (repo / "contacts.json").write_text('{"name": "Bob"}\n', encoding="utf-8")
    (repo / "memory.db").write_text("raw-bytes-database\n", encoding="utf-8")

    opened_files = []
    original_read_text = Path.read_text

    def tracking_read_text(self, *args, **kwargs):
        opened_files.append(str(self))
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", tracking_read_text)

    affected = [".env", "contacts.json", "memory.db", "calculator.py"]
    excerpts = extract_source_excerpts(repo, affected_files=affected)

    assert len(excerpts) == 1
    assert excerpts[0].file_path == "calculator.py"
    assert "add" in excerpts[0].content

    # Strict no-read guarantee: verify sensitive paths were NEVER opened
    for opened in opened_files:
        assert not is_protected_name(Path(opened).name), f"Protected file '{opened}' was read during excerpting!"


def test_rendered_prompt_never_exposes_protected_filenames(tmp_path: Path):
    """render_context_prompt must not expose protected file names in prompt header."""
    spec = WorkflowIntakeSpec(
        intake_id="intake_test_12345",
        source_type="issue_description",
        target_repo=str(tmp_path),
        base_commit_sha="a" * 40,
        title="Test task",
        description="Fix calculation bug",
        affected_files=[".env", "contacts.json", "calc.py"],
        issue_payload=IssueIntakePayload(
            title="Test task",
            issue_text="Fix calculation bug",
            affected_files=[".env", "contacts.json", "calc.py"],
        ),
    )

    from forge.repository.detector import ProjectIntelligence
    prompt_text, summary = render_context_prompt(
        intake=spec,
        intelligence=ProjectIntelligence(is_python=True),
        symbols=[],
        instructions=[],
        excerpts=[],
        budget=ContextBudget(),
    )

    assert ".env" not in prompt_text
    assert "contacts.json" not in prompt_text
    assert "calc.py" in prompt_text


# =============================================================================
# 7. Verification: SEMANTIC and Step 1 Protection
# =============================================================================

def test_verification_rejects_protected_target_file(tmp_path: Path):
    """MultiCheckVerifier must reject plans targeting protected files without reading."""
    from forge.repair.contracts import PatchCandidate, compute_patch_sha256

    diff = "--- a/.env\n+++ b/.env\n@@ -1 +1 @@\n-A=1\n+A=2\n"
    digest = compute_patch_sha256(diff)
    candidate = PatchCandidate(
        patch_id="patch_test_01",
        contract_id="contract_test_01",
        finding_id="find_test_01",
        base_commit_sha="b" * 40,
        target_files=[".env"],
        unified_diff=diff,
        added_lines=1,
        removed_lines=1,
        hunk_count=1,
        sha256_digest=digest,
    )

    plan = RepairPlan(
        plan_id="plan_test_01",
        finding_id="find_test_01",
        base_commit_sha="b" * 40,
        cited_evidence_ids=["evi_001"],
        target_repo=str(tmp_path),
        target_file=".env",
        root_cause="Sensitive file defect",
        proposed_change="Modify sensitive file",
        expected_behavior="Do not modify sensitive file",
        verification_plan="pytest",
        patch_candidate=candidate,
    )

    verifier = MultiCheckVerifier(sandbox=object())
    check = VerificationCheckSpec(
        check_type=VerificationCheckType.SEMANTIC,
        name="semantic:check",
        command=["python3", "-m", "py_compile", ".env"],
    )

    verif_plan = MultiCheckVerificationPlan(
        plan_id="vplan_test_01",
        workflow_id="wf_test_01",
        repair_plan_id="plan_test_01",
        patch_digest=digest,
        base_commit_sha="b" * 40,
        target_file=".env",
        checks=[check],
    )

    result = verifier._run_single_check(
        check=check,
        workspace_path=tmp_path,
        is_baseline=True,
        plan=verif_plan,
        attempt_id="att_test_01",
        target_repo=tmp_path,
    )

    assert result.status.value == "failed"
    assert "protected" in result.diagnostic.lower()


# =============================================================================
# 8. Dossier Validation: Protected File Artifact Rejection
# =============================================================================

def test_dossier_validation_rejects_protected_files(tmp_path: Path):
    """validate_exported_dossier must flag any protected files as security violations."""
    dossier_dir = tmp_path / "dossier"
    dossier_dir.mkdir()

    # Create dummy manifest containing contacts.json
    manifest_data = {
        "manifest_version": "1.0",
        "dossier_id": "dos_test",
        "workflow_id": "wf_test",
        "intake_id": "intake_test",
        "patch_digest": "d" * 64,
        "base_commit_sha": "e" * 40,
        "created_at": "2026-11-05T00:00:00Z",
        "files": {
            "contacts.json": {"byte_size": 10, "sha256": "f" * 64},
        },
    }
    (dossier_dir / "manifest.json").write_text(json.dumps(manifest_data), encoding="utf-8")
    (dossier_dir / "contacts.json").write_text("confidential", encoding="utf-8")

    res = validate_engineering_dossier(dossier_dir)
    assert not res.is_valid
    assert any("contacts.json" in err or "protected" in err for err in res.errors)


# =============================================================================
# 9. Analysis Validation & Evidence: validate_repository_path and create_evidence
# =============================================================================

def test_validate_repository_path_rejects_protected_paths(tmp_path: Path):
    """validate_repository_path must reject protected file paths with RepositoryPathValidationError."""
    (tmp_path / "contacts.json").write_text("{}", encoding="utf-8")
    (tmp_path / "safe.py").write_text("pass", encoding="utf-8")

    with pytest.raises(RepositoryPathValidationError, match="Security violation: Access to protected sensitive file forbidden"):
        validate_repository_path(tmp_path, "contacts.json")

    with pytest.raises(RepositoryPathValidationError, match="Security violation: Access to protected sensitive file forbidden"):
        validate_repository_path(tmp_path, ".env")

    # Permitted path works
    cand, norm = validate_repository_path(tmp_path, "safe.py")
    assert norm == "safe.py"


# =============================================================================
# 10. Git Metadata Privacy Boundary Remediation Tests
# =============================================================================

def test_git_metadata_purge_local_acquisition_current_commit(tmp_path: Path):
    """Acquisition of repository with protected file in HEAD must purge it from working tree and Git objects."""
    src = tmp_path / "src_repo"
    src.mkdir()
    run_git_command(["init"], cwd=src)
    run_git_command(["config", "user.name", "Audit Test"], cwd=src)
    run_git_command(["config", "user.email", "audit@test.local"], cwd=src)

    (src / "app.py").write_text("def run(): return 42\n", encoding="utf-8")
    (src / ".env").write_text("SYNTHETIC_HEAD_SECRET_KEY=head_secret_111\n", encoding="utf-8")
    run_git_command(["add", "."], cwd=src)
    run_git_command(["commit", "-m", "Commit with head .env"], cwd=src)

    # Repack to ensure content lives inside Git packfile
    run_git_command(["repack", "-a", "-d"], cwd=src)

    expected_sha = run_git_command(["rev-parse", "HEAD"], cwd=src).stdout.strip()
    expected_branch = run_git_command(["rev-parse", "--abbrev-ref", "HEAD"], cwd=src).stdout.strip()

    ws = tmp_path / "workspaces"
    acquired = acquire_repository(str(src), custom_workspace=ws, allow_local=True)

    try:
        # 1. Normal files available
        assert (acquired.workspace_path / "app.py").exists()
        assert (acquired.workspace_path / "app.py").read_text(encoding="utf-8") == "def run(): return 42\n"

        # 2. Protected file removed from working tree
        assert not (acquired.workspace_path / ".env").exists()

        # 3. Revision identity matches
        assert acquired.resolved_commit_sha == expected_sha
        assert acquired.target_branch == expected_branch
        assert run_git_command(["rev-parse", "HEAD"], cwd=acquired.trusted_git_path).stdout.strip() == expected_sha
        assert acquired.analysis_commit_sha == run_git_command(["rev-parse", "HEAD"], cwd=acquired.workspace_path).stdout.strip()

        # 4. Status works and reports clean/deleted
        st_res = run_git_command(["status", "--porcelain"], cwd=acquired.workspace_path)
        assert st_res.returncode == 0

        # 5. Git show / cat-file on protected file fails closed
        show_res = run_git_command(["show", "HEAD:.env"], cwd=acquired.workspace_path)
        assert show_res.returncode != 0

        # 6. Physical absence of secret bytes in all files including .git
        secret_bytes = b"head_secret_111"
        for root, dirs, files in os.walk(acquired.workspace_path):
            for f in files:
                f_path = Path(root) / f
                assert secret_bytes not in f_path.read_bytes(), f"Secret leaked in {f_path}"

        # 7. Original source repository remains untouched
        assert (src / ".env").exists()
        assert secret_bytes in (src / ".env").read_bytes()
    finally:
        cleanup_repository(acquired, allowed_base=ws)


def test_git_metadata_purge_historical_commit(tmp_path: Path):
    """Acquisition of repository with protected file in older history must purge its blobs from Git objects."""
    src = tmp_path / "history_repo"
    src.mkdir()
    run_git_command(["init"], cwd=src)
    run_git_command(["config", "user.name", "Audit Test"], cwd=src)
    run_git_command(["config", "user.email", "audit@test.local"], cwd=src)

    # Commit 1: Add .env.backup with secret
    (src / "base.py").write_text("x = 100\n", encoding="utf-8")
    (src / ".env.backup").write_text("SYNTHETIC_HISTORICAL_TOKEN=old_secret_222\n", encoding="utf-8")
    run_git_command(["add", "."], cwd=src)
    run_git_command(["commit", "-m", "Commit 1 with historical secret"], cwd=src)

    # Commit 2: Delete .env.backup, add calculator.py
    (src / ".env.backup").unlink()
    (src / "calculator.py").write_text("def calc(): return True\n", encoding="utf-8")
    run_git_command(["add", "-A"], cwd=src)
    run_git_command(["commit", "-m", "Commit 2: remove backup and add calc"], cwd=src)

    # Repack to put historical objects in packfile
    run_git_command(["repack", "-a", "-d"], cwd=src)

    expected_sha = run_git_command(["rev-parse", "HEAD"], cwd=src).stdout.strip()

    ws = tmp_path / "workspaces"
    acquired = acquire_repository(str(src), custom_workspace=ws, allow_local=True)

    try:
        # Permitted files present
        assert (acquired.workspace_path / "base.py").exists()
        assert (acquired.workspace_path / "calculator.py").exists()
        assert not (acquired.workspace_path / ".env.backup").exists()

        # Revision matches Commit 2
        assert acquired.resolved_commit_sha == expected_sha
        assert run_git_command(["rev-parse", "HEAD"], cwd=acquired.trusted_git_path).stdout.strip() == expected_sha
        assert acquired.analysis_commit_sha == run_git_command(["rev-parse", "HEAD"], cwd=acquired.workspace_path).stdout.strip()

        # Historical git show fails
        show_res = run_git_command(["show", "HEAD~1:.env.backup"], cwd=acquired.workspace_path)
        assert show_res.returncode != 0

        # Zero secret bytes in entire acquired workspace including .git
        secret_bytes = b"old_secret_222"
        for root, dirs, files in os.walk(acquired.workspace_path):
            for f in files:
                f_path = Path(root) / f
                assert secret_bytes not in f_path.read_bytes(), f"Historical secret leaked in {f_path}"

        # Original source repository untouched
        orig_show = run_git_command(["show", "HEAD~1:.env.backup"], cwd=src)
        assert orig_show.returncode == 0
        assert "old_secret_222" in orig_show.stdout
    finally:
        cleanup_repository(acquired, allowed_base=ws)


def test_git_metadata_purge_nested_directory_and_packs(tmp_path: Path):
    """Acquisition of repository with nested contacts.json and logs in packs must purge all blobs."""
    src = tmp_path / "nested_repo"
    src.mkdir()
    run_git_command(["init"], cwd=src)
    run_git_command(["config", "user.name", "Audit Test"], cwd=src)
    run_git_command(["config", "user.email", "audit@test.local"], cwd=src)

    nested_dir = src / "subsystem" / "data"
    nested_dir.mkdir(parents=True)
    (nested_dir / "contacts.json").write_text('{"contacts": ["secret_contact_333"]}\n', encoding="utf-8")

    logs_dir = src / "logs"
    logs_dir.mkdir()
    (logs_dir / "audit.log").write_text("DEBUG: secret_log_444\n", encoding="utf-8")

    (src / "service.py").write_text("def start(): pass\n", encoding="utf-8")
    run_git_command(["add", "."], cwd=src)
    run_git_command(["commit", "-m", "Commit with nested secrets and logs"], cwd=src)
    run_git_command(["repack", "-a", "-d"], cwd=src)

    expected_sha = run_git_command(["rev-parse", "HEAD"], cwd=src).stdout.strip()

    ws = tmp_path / "workspaces"
    acquired = acquire_repository(str(src), custom_workspace=ws, allow_local=True)

    try:
        assert (acquired.workspace_path / "service.py").exists()
        assert not (acquired.workspace_path / "subsystem" / "data" / "contacts.json").exists()
        assert not (acquired.workspace_path / "logs" / "audit.log").exists()

        assert acquired.resolved_commit_sha == expected_sha

        # Git show on nested files fails closed
        assert run_git_command(["show", "HEAD:subsystem/data/contacts.json"], cwd=acquired.workspace_path).returncode != 0
        assert run_git_command(["show", "HEAD:logs/audit.log"], cwd=acquired.workspace_path).returncode != 0

        # Scan for secrets
        for s in [b"secret_contact_333", b"secret_log_444"]:
            for root, dirs, files in os.walk(acquired.workspace_path):
                for f in files:
                    f_path = Path(root) / f
                    assert s not in f_path.read_bytes(), f"Secret {s} leaked in {f_path}"
    finally:
        cleanup_repository(acquired, allowed_base=ws)


def test_git_metadata_purge_remote_acquisition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Remote Git acquisition clones must unpack packs and purge protected blobs."""
    remote_src = tmp_path / "remote_origin"
    remote_src.mkdir()
    run_git_command(["init"], cwd=remote_src)
    run_git_command(["config", "user.name", "Remote Test"], cwd=remote_src)
    run_git_command(["config", "user.email", "remote@test.local"], cwd=remote_src)

    (remote_src / "module.py").write_text("class Core: pass\n", encoding="utf-8")
    (remote_src / ".env.production").write_text("SYNTHETIC_PROD_KEY=remote_secret_555\n", encoding="utf-8")
    run_git_command(["add", "."], cwd=remote_src)
    run_git_command(["commit", "-m", "Remote initial commit"], cwd=remote_src)
    run_git_command(["repack", "-a", "-d"], cwd=remote_src)

    expected_sha = run_git_command(["rev-parse", "HEAD"], cwd=remote_src).stdout.strip()

    # Route REMOTE_GIT target validation to local directory clone simulating remote HTTPS clone
    def fake_validate_target(target: str, allow_local: bool = True):
        return TargetValidationResult(
            is_valid=True,
            raw_target=target,
            target_type=TargetType.REMOTE_GIT,
            sanitized_target=str(remote_src),
        )

    monkeypatch.setattr("forge.repository.acquisition.validate_target", fake_validate_target)

    ws = tmp_path / "workspaces"
    acquired = acquire_repository("https://github.com/example/repo.git", custom_workspace=ws)

    try:
        assert acquired.is_local_copy is False
        assert acquired.resolved_commit_sha == expected_sha
        assert (acquired.workspace_path / "module.py").exists()
        assert not (acquired.workspace_path / ".env.production").exists()

        assert run_git_command(["show", "HEAD:.env.production"], cwd=acquired.workspace_path).returncode != 0

        secret_bytes = b"remote_secret_555"
        for root, dirs, files in os.walk(acquired.workspace_path):
            for f in files:
                f_path = Path(root) / f
                assert secret_bytes not in f_path.read_bytes(), f"Remote secret leaked in {f_path}"
    finally:
        cleanup_repository(acquired, allowed_base=ws)


def test_git_metadata_purge_fails_closed_on_symlink(tmp_path: Path):
    """If .git in workspace is a symlink, acquisition/purge must fail closed."""
    ws = tmp_path / "workspace_with_symlink_git"
    ws.mkdir()
    (ws / "app.py").write_text("code\n", encoding="utf-8")

    outside_git = tmp_path / "external_git_dir"
    outside_git.mkdir()

    symlink_git = ws / ".git"
    try:
        symlink_git.symlink_to(outside_git)
    except OSError:
        pytest.skip("Symlinks not supported in environment")

    with pytest.raises((AcquisitionError, GitMetadataProtectionError), match="cannot be a symlink"):
        purge_protected_git_metadata(ws)


def test_git_metadata_purge_fails_closed_on_internal_symlink_escape(tmp_path: Path):
    """If internal directory inside .git escapes workspace, purge must fail closed."""
    ws = tmp_path / "workspace_escaping_internals"
    ws.mkdir()
    git_dir = ws / ".git"
    git_dir.mkdir()

    outside_dir = tmp_path / "external_objects"
    outside_dir.mkdir()

    symlink_obj = git_dir / "objects"
    try:
        symlink_obj.symlink_to(outside_dir)
    except OSError:
        pytest.skip("Symlinks not supported in environment")

    with pytest.raises((AcquisitionError, GitMetadataProtectionError), match="escaping symlink inside .git"):
        purge_protected_git_metadata(ws)


def test_git_metadata_purge_fails_closed_on_purge_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """If Git metadata purge encounters an operational failure, it must fail closed."""
    src = tmp_path / "fail_repo"
    src.mkdir()
    run_git_command(["init"], cwd=src)
    run_git_command(["config", "user.name", "Fail Test"], cwd=src)
    run_git_command(["config", "user.email", "fail@test.local"], cwd=src)
    (src / "app.py").write_text("code", encoding="utf-8")
    (src / ".env").write_text("secret", encoding="utf-8")
    run_git_command(["add", "."], cwd=src)
    commit_res = run_git_command(["commit", "-m", "init"], cwd=src)
    assert commit_res.returncode == 0, f"Git commit failed: {commit_res.stderr}"

    # Monkeypatch GitMediator.export_sanitized_workspace to raise an error
    def faulty_export(self, dest_dir, ref=None):
        raise GitMetadataProtectionError("Simulated filesystem I/O corruption during export")

    monkeypatch.setattr("forge.repository.acquisition.GitMediator.export_sanitized_workspace", faulty_export)

    ws = tmp_path / "workspaces"
    with pytest.raises(AcquisitionError, match="Failed to export sanitized repository workspace"):
        acquire_repository(str(src), custom_workspace=ws, allow_local=True)

    # Destination directory and trusted mirror must have been cleaned up
    assert not any(ws.glob("repo_*"))
    assert not any(ws.glob("trusted_*"))


def test_git_metadata_purge_fails_closed_on_remote_export_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """If GitMediator.export_sanitized_workspace encounters an operational failure during remote acquisition, fail closed."""
    remote_src = tmp_path / "remote_fail_repo"
    remote_src.mkdir()
    run_git_command(["init"], cwd=remote_src)
    run_git_command(["config", "user.name", "Remote Fail Test"], cwd=remote_src)
    run_git_command(["config", "user.email", "remotefail@test.local"], cwd=remote_src)
    (remote_src / "app.py").write_text("code", encoding="utf-8")
    (remote_src / ".env").write_text("secret", encoding="utf-8")
    run_git_command(["add", "."], cwd=remote_src)
    commit_res = run_git_command(["commit", "-m", "init"], cwd=remote_src)
    assert commit_res.returncode == 0, f"Git commit failed: {commit_res.stderr}"

    def fake_validate_target(target: str, allow_local: bool = True):
        return TargetValidationResult(
            is_valid=True,
            raw_target=target,
            target_type=TargetType.REMOTE_GIT,
            sanitized_target=str(remote_src),
        )

    monkeypatch.setattr("forge.repository.acquisition.validate_target", fake_validate_target)

    def faulty_export(self, dest_dir, ref=None):
        raise GitMetadataProtectionError("Simulated filesystem I/O corruption during remote export")

    monkeypatch.setattr("forge.repository.acquisition.GitMediator.export_sanitized_workspace", faulty_export)

    ws = tmp_path / "workspaces"
    with pytest.raises(AcquisitionError, match="Failed to export sanitized repository workspace"):
        acquire_repository("https://github.com/example/fail.git", custom_workspace=ws)

    # Destination directory and trusted mirror must have been cleaned up
    assert not any(ws.glob("repo_*"))
    assert not any(ws.glob("trusted_*"))


# =============================================================================
# 11. Git Integrity & Referential Completeness Regression Tests
# =============================================================================

def test_git_integrity_missing_referenced_blobs_behavior(tmp_path: Path):
    """Verify exact supported vs failing Git operations when referenced blobs are purged.

    Documents the architectural reality:
    - Supported: rev-parse, status, ls-tree, show ordinary files, branch management.
    - Failing: show protected files (bad object), diff HEAD (unable to read blob),
      fsck --full (broken link from tree to blob).
    """
    src = tmp_path / "integrity_repo"
    src.mkdir()
    run_git_command(["init"], cwd=src)
    run_git_command(["config", "user.name", "Integrity Test"], cwd=src)
    run_git_command(["config", "user.email", "integrity@test.local"], cwd=src)

    (src / "README.md").write_text("ordinary content\n", encoding="utf-8")
    (src / ".env").write_text("SENSITIVE_KEY=12345\n", encoding="utf-8")
    run_git_command(["add", "."], cwd=src)
    run_git_command(["commit", "-m", "Commit 1"], cwd=src)

    src_dir = src / "src"
    src_dir.mkdir()
    (src_dir / "app.py").write_text("print('app')\n", encoding="utf-8")
    (src / "contacts.json").write_text('{"name": "Secret"}\n', encoding="utf-8")
    run_git_command(["add", "."], cwd=src)
    run_git_command(["commit", "-m", "Commit 2"], cwd=src)

    # Repack into packfile to test unpacked loose blob purge
    run_git_command(["repack", "-a", "-d"], cwd=src)
    expected_head = run_git_command(["rev-parse", "HEAD"], cwd=src).stdout.strip()

    # Copy to workspace and purge
    ws = tmp_path / "integrity_workspace"
    shutil.copytree(src, ws)
    purge_protected_paths_from_workspace(ws)
    purged_count = purge_protected_git_metadata(ws)
    assert purged_count >= 2

    # 1. Supported operations
    res_rev = run_git_command(["rev-parse", "HEAD"], cwd=ws)
    assert res_rev.returncode == 0
    assert res_rev.stdout.strip() == expected_head, "Commit identity must be preserved"

    res_status = run_git_command(["status", "--porcelain"], cwd=ws)
    assert res_status.returncode == 0

    res_lstree = run_git_command(["ls-tree", "HEAD"], cwd=ws)
    assert res_lstree.returncode == 0
    assert "README.md" in res_lstree.stdout
    assert "contacts.json" in res_lstree.stdout

    res_show_ord = run_git_command(["show", "HEAD:src/app.py"], cwd=ws)
    assert res_show_ord.returncode == 0
    assert "print('app')" in res_show_ord.stdout

    res_branch = run_git_command(["checkout", "-b", "feature_branch"], cwd=ws)
    assert res_branch.returncode == 0

    # 2. Failing operations (by design: protected blob is physically absent)
    res_show_prot = run_git_command(["show", "HEAD:contacts.json"], cwd=ws)
    assert res_show_prot.returncode != 0
    assert "bad object" in res_show_prot.stderr or "fatal" in res_show_prot.stderr

    res_show_env = run_git_command(["show", "HEAD:.env"], cwd=ws)
    assert res_show_env.returncode != 0
    assert "bad object" in res_show_env.stderr or "fatal" in res_show_env.stderr

    res_diff_head = run_git_command(["diff", "HEAD"], cwd=ws)
    assert res_diff_head.returncode != 0
    assert "unable to read" in res_diff_head.stderr

    res_fsck = run_git_command(["fsck", "--full"], cwd=ws)
    assert res_fsck.returncode != 0, "fsck must report broken links for purged blobs"
    assert "broken link from" in res_fsck.stdout or "broken link from" in res_fsck.stderr


def test_git_metadata_purge_rejects_alternates(tmp_path: Path):
    """Git alternates pointing to external object stores must be rejected immediately."""
    ws = tmp_path / "workspace_alternates"
    ws.mkdir()
    run_git_command(["init"], cwd=ws)
    (ws / "main.py").write_text("print('hello')", encoding="utf-8")

    info_dir = ws / ".git" / "objects" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    (info_dir / "alternates").write_text("/external/path/to/objects\n", encoding="utf-8")

    with pytest.raises(GitMetadataProtectionError, match="Git alternates file detected"):
        purge_protected_git_metadata(ws)


def test_git_metadata_purge_cleans_dangling_blobs(tmp_path: Path):
    """Dangling unreachable blobs in object store must be purged via git prune."""
    ws = tmp_path / "workspace_dangling"
    ws.mkdir()
    run_git_command(["init"], cwd=ws)
    run_git_command(["config", "user.name", "Dangling Test"], cwd=ws)
    run_git_command(["config", "user.email", "dangling@test.local"], cwd=ws)

    (ws / "app.py").write_text("print('app')", encoding="utf-8")
    run_git_command(["add", "."], cwd=ws)
    run_git_command(["commit", "-m", "init"], cwd=ws)

    # Inject an unreachable loose object directly via hash-object
    import subprocess
    p = subprocess.Popen(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=ws,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    dangling_sha, _ = p.communicate("unreachable_loose_secret_token\n")
    dangling_sha = dangling_sha.strip()
    dangling_path = ws / ".git" / "objects" / dangling_sha[:2] / dangling_sha[2:]
    assert dangling_path.exists(), "Dangling blob must exist before purge"

    # Purge Git metadata
    purge_protected_git_metadata(ws)

    # Dangling blob must be pruned
    assert not dangling_path.exists(), "Dangling unreachable blob should be pruned from .git/objects"
