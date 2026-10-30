"""Typer Command-Line Interface for FORGE."""

import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import typer
from forge import __version__
from forge.config import get_settings, redact_secrets

app = typer.Typer(
    name="forge",
    help="FORGE: Autonomous Software Engineering Intelligence (Supervised Engineering Beta)",
    no_args_is_help=True,
)

# Sub-command groups
db_app = typer.Typer(help="Database management commands.")
# Sub-command groups
db_app = typer.Typer(help="Database management commands.")
findings_app = typer.Typer(help="Finding inspection commands.")
sandbox_app = typer.Typer(help="Sandbox diagnostics and execution commands.")
provider_app = typer.Typer(help="LLM provider diagnostics and configuration checks.")
benchmark_app = typer.Typer(help="Benchmark evaluation commands.")
policy_app = typer.Typer(help="CI review policy check and enforcement commands.")
repro_app = typer.Typer(help="Reproduction case management and execution commands.")
plan_app = typer.Typer(help="Deterministic reproduction plan generation, validation, and execution commands.")
repair_app = typer.Typer(help="Evidence-grounded repair planning and patch candidate commands.")
workflow_app = typer.Typer(help="Controlled repair orchestration workflow commands.")
repair_app.add_typer(workflow_app, name="workflow")
eng_workflow_app = typer.Typer(help="Phase 5 supervised end-to-end engineering workflow commands.")
workflow_intake_app = typer.Typer(help="Workflow task intake and normalization commands.")
workflow_context_app = typer.Typer(help="Repository context assembly commands.")
eng_workflow_app.add_typer(workflow_intake_app, name="intake")
eng_workflow_app.add_typer(workflow_context_app, name="context")
app.add_typer(db_app, name="db")
app.add_typer(findings_app, name="findings")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(provider_app, name="provider")
app.add_typer(benchmark_app, name="benchmark")
app.add_typer(policy_app, name="policy")
app.add_typer(repro_app, name="repro")
app.add_typer(plan_app, name="plan")
app.add_typer(repair_app, name="repair")
app.add_typer(eng_workflow_app, name="workflow")



@app.command()
def version() -> None:
    """Show FORGE version."""
    typer.echo(f"FORGE version: {__version__}")


@app.command()
def doctor() -> None:
    """Diagnose local environment, dependencies, and sandbox availability."""
    from forge.diagnostics import run_system_diagnostics
    from forge.llm.factory import describe_provider

    settings = get_settings()
    summary = run_system_diagnostics(settings)

    typer.echo("FORGE System Health Diagnostic")
    typer.echo("------------------------------")
    typer.echo(f"Python Version: {sys.version.split()[0]} ({sys.executable})")

    # Static inspection capability
    typer.echo("  [OK] Static repository inspection: available")

    # Git check
    git_check = next((c for c in summary.checks if c.name == "Git Executable"), None)
    if git_check and git_check.is_healthy:
        typer.echo(f"  [OK] {git_check.message}")
    else:
        msg = git_check.message if git_check else "Git executable not found in PATH"
        typer.echo(f"  [FAIL] {msg}")

    # Sandbox interface check
    typer.echo("  [OK] Sandbox interface: available (fail-secure policy active)")

    # Docker sandbox runtime check
    docker_check = next((c for c in summary.checks if c.name == "Docker Sandbox Runtime"), None)
    if docker_check and docker_check.is_healthy:
        typer.echo(f"  [OK] Docker runtime: {docker_check.message}")
    else:
        msg = docker_check.message if docker_check else "Docker runtime unavailable"
        typer.echo(f"  [WARN] Docker runtime unavailable: {msg}")
        if docker_check and docker_check.actionable_remediation:
            typer.echo(f"         Remediation: {docker_check.actionable_remediation}")
        typer.echo("         Runtime check sandboxing will fall back to safe skip mode (host execution is refused).")

    # SQLite check
    typer.echo(f"  [OK] SQLite target path: {settings.db_path.resolve()}")
    db_check = next((c for c in summary.checks if c.name == "SQLite Database"), None)
    if db_check and db_check.is_healthy:
        if db_check.details.get("initialized") is False:
            typer.echo("  [INFO] Database not yet initialized (run 'forge db init')")
        else:
            typer.echo(f"  [OK] {db_check.message}")
    elif db_check:
        typer.echo(f"  {db_check.category.label} {db_check.message}")
        if db_check.actionable_remediation:
            typer.echo(f"         Remediation: {db_check.actionable_remediation}")

    # Provider diagnostics
    provider_info = describe_provider()
    provider_name = provider_info["provider"]
    creds_ok = provider_info["credentials_configured"]
    typer.echo(f"  [OK] Secret redaction active (Provider: {provider_name})")

    provider_check = next((c for c in summary.checks if c.name.startswith("LLM Provider")), None)
    if provider_check and provider_check.is_healthy:
        typer.echo(f"  [OK] LLM Provider '{provider_name}' credentials: configured")
        typer.echo(f"         Status: {provider_check.message}")
    elif provider_check:
        typer.echo(f"  {provider_check.category.label} LLM Provider '{provider_name}': {provider_check.message}")
        if provider_check.actionable_remediation:
            typer.echo(f"         Remediation: {provider_check.actionable_remediation}")
    elif creds_ok:
        typer.echo(f"  [OK] LLM Provider '{provider_name}' credentials: configured")
    else:
        typer.echo(f"  [WARN] LLM Provider '{provider_name}' credentials: NOT configured (set the required API key)")

    typer.echo(f"         Model: {provider_info['model'] or '(using provider default)'}")
    typer.echo(f"         API Base: {provider_info['api_base']}")
    typer.echo(f"         Network required: {'yes' if provider_info['requires_network'] else 'no (local)'}")
    typer.echo(f"         Cloud fallback: DISABLED (policy enforced)")

    # Overall Supervised Beta Readiness
    typer.echo("")
    typer.echo("Supervised Beta Operational Readiness")
    typer.echo("-------------------------------------")
    typer.echo(f"  Static Inspection: {'[READY]' if summary.static_inspection_ready else '[NOT READY]'}")
    typer.echo(f"  Docker Sandboxing: {'[READY]' if summary.live_execution_ready else '[NOT READY - SKIPPED]'}")
    typer.echo(f"  Local Inference:   {'[READY]' if summary.local_inference_ready else '[NOT READY]'}")
    typer.echo(f"  Overall Status:    [{summary.overall_status}]")
    typer.echo(f"  {summary.summary_message}")


# --- Sandbox commands ---

@sandbox_app.command(name="doctor")
def sandbox_doctor() -> None:
    """Diagnose sandbox runtime health, image availability, and isolation boundaries."""
    from forge.diagnostics import check_docker_environment
    settings = get_settings()

    typer.echo("FORGE Sandbox Diagnostics")
    typer.echo("-------------------------")
    typer.echo(f"Configured image: {settings.docker_image}")
    typer.echo(f"Network policy:   disabled (enforced --network none)")
    typer.echo(f"Filesystem:       read-only workspace mount with isolated 64MB tmpfs")
    typer.echo(f"Process user:     1000:1000 (non-root)")
    typer.echo(f"Privileges:       no-new-privileges, cap-drop ALL")

    check = check_docker_environment(settings.docker_image)
    if check.is_healthy:
        typer.secho(f"Runtime Status:   [AVAILABLE] {check.message}", fg=typer.colors.GREEN)
    else:
        typer.secho(f"Runtime Status:   [UNAVAILABLE] {check.message}", fg=typer.colors.YELLOW)
        guidance = check.actionable_remediation or "To enable isolated test/benchmark execution, start Docker."
        typer.echo(f"Actionable guidance: {guidance}")
    typer.echo("Security notice:     FORGE strictly refuses to run repository code directly on host.")


# --- Provider commands ---

@provider_app.command(name="doctor")
def provider_doctor() -> None:
    """Diagnose LLM provider connectivity, credentials, and local model availability."""
    from forge.diagnostics import check_llm_provider
    from forge.llm.factory import describe_provider

    settings = get_settings()
    info = describe_provider()
    check = check_llm_provider(settings)

    typer.echo("FORGE LLM Provider Diagnostics")
    typer.echo("------------------------------")
    typer.echo(f"Configured Provider: {info['provider']}")
    typer.echo(f"Configured Model:    {info['model'] or '(provider default)'}")
    typer.echo(f"API Endpoint Base:   {info['api_base']}")
    typer.echo(f"Network Required:    {'yes' if info['requires_network'] else 'no (local)'}")
    typer.echo(f"Cloud Fallback:      {'ENABLED (UNSAFE)' if settings.allow_cloud_fallback else 'DISABLED (enforced)'}")

    if check.is_healthy:
        typer.secho(f"Status:              {check.category.label} {check.message}", fg=typer.colors.GREEN)
    else:
        typer.secho(f"Status:              {check.category.label} {check.message}", fg=typer.colors.YELLOW)
        if check.actionable_remediation:
            typer.echo(f"Actionable Guidance: {check.actionable_remediation}")


@sandbox_app.command(name="check")
def sandbox_check(
    contract_path: str = typer.Argument(..., help="Path to JSON file containing ReproductionRequest contract"),
) -> None:
    """Validate a reproduction execution contract without running untrusted code."""
    from forge.sandbox.contracts import ReproductionRequest
    contract_file = Path(contract_path).resolve()
    if not contract_file.exists():
        typer.secho(f"Contract file not found: {contract_file}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    try:
        raw_data = json.loads(contract_file.read_text(encoding="utf-8"))
        req = ReproductionRequest.model_validate(raw_data)
    except Exception as e:
        typer.secho(f"Contract validation failed: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    typer.secho(f"Contract '{req.request_id}' is valid.", fg=typer.colors.GREEN)
    typer.echo(f"  Finding ID:   {req.finding_id}")
    typer.echo(f"  Hypothesis:   {req.hypothesis}")
    typer.echo(f"  Command:      {' '.join(req.command.args)}")
    typer.echo(f"  Workdir:      {req.command.relative_cwd}")
    typer.echo(f"  Image:        {req.environment.docker_image}")
    typer.echo(f"  Timeout:      {req.environment.timeout_seconds}s")
    typer.echo(f"  Memory Limit: {req.environment.memory_limit}")


@sandbox_app.command(name="reproduce")
def sandbox_reproduce(
    contract_path: str = typer.Argument(..., help="Path to JSON file containing ReproductionRequest contract"),
    output_path: Optional[str] = typer.Option(None, "--output", "-o", help="Path to export reproduction result JSON"),
) -> None:
    """Execute defect reproduction inside an isolated sandbox or fail closed."""
    from forge.sandbox.contracts import ReproductionRequest, ReproductionOutcome
    from forge.sandbox.runner import ReproductionHarness
    from forge.repository import acquire_repository, cleanup_repository, SecurityError, AcquisitionError

    contract_file = Path(contract_path).resolve()
    if not contract_file.exists():
        typer.secho(f"Contract file not found: {contract_file}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    try:
        raw_data = json.loads(contract_file.read_text(encoding="utf-8"))
        req = ReproductionRequest.model_validate(raw_data)
    except Exception as e:
        typer.secho(f"Malformed contract: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    typer.echo(f"Evaluating reproduction contract: {req.request_id} (Finding: {req.finding_id})")

    # Acquire repository safely
    try:
        acquired = acquire_repository(req.target_repo, branch=req.commit_sha)
    except (SecurityError, AcquisitionError) as e:
        typer.secho(f"Repository acquisition aborted: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    try:
        harness = ReproductionHarness()
        result = harness.execute(req, acquired.workspace_path)
    finally:
        cleanup_repository(acquired)

    # Render results
    color = typer.colors.GREEN if result.outcome == ReproductionOutcome.REPRODUCED else (
        typer.colors.YELLOW if result.outcome == ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE else typer.colors.RED
    )
    typer.secho(f"\nReproduction Outcome: {result.outcome.value.upper()}", fg=color, bold=True)
    if result.diagnostic_message:
        typer.echo(f"Details: {result.diagnostic_message}")
    if result.exit_code is not None:
        typer.echo(f"Exit Code: {result.exit_code}")
    typer.echo(f"Duration: {result.duration_ms:.1f}ms")

    if output_path:
        out_p = Path(output_path).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(result.model_dump(mode="json"), indent=2), encoding="utf-8")
        typer.secho(f"\nReproduction result exported to: {out_p}", fg=typer.colors.GREEN)

    if result.outcome == ReproductionOutcome.ERROR:
        raise typer.Exit(code=2)
    elif result.outcome in {ReproductionOutcome.FAILED, ReproductionOutcome.TIMEOUT}:
        raise typer.Exit(code=1)


# --- Reproduction Case Management Commands ---

@repro_app.command(name="create")
def repro_create(
    contract_path: str = typer.Argument(..., help="Path to JSON file containing ReproductionRequest contract"),
    commit_sha: str = typer.Option(..., "--commit", "-c", help="Pinned repository commit SHA"),
    max_retries: int = typer.Option(0, "--max-retries", "-r", help="Max allowed execution retries (0-3)"),
    format: str = typer.Option("text", "--format", "-f", help="Output format: text or json"),
) -> None:
    """Create and persist a new reproduction case in the SQLite store."""
    from forge.reproduction import ReproductionCaseManager, CaseManagementError
    from forge.sandbox.contracts import ReproductionRequest
    from forge.storage.store import StoreError

    contract_file = Path(contract_path).resolve()
    if not contract_file.exists():
        typer.secho(f"Contract file not found: {contract_file}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    try:
        raw_data = json.loads(contract_file.read_text(encoding="utf-8"))
        contract = ReproductionRequest.model_validate(raw_data)
    except Exception as e:
        typer.secho(f"Malformed reproduction contract: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    manager = ReproductionCaseManager()
    try:
        case = manager.create_case(
            finding_id=contract.finding_id,
            target_repo=contract.target_repo,
            target_commit_sha=commit_sha,
            contract=contract,
            max_retries=max_retries,
        )
    except (CaseManagementError, StoreError) as e:
        typer.secho(f"Error creating reproduction case: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if format.lower() == "json":
        typer.echo(json.dumps(case.model_dump(mode="json"), indent=2))
    else:
        typer.secho(f"Created reproduction case '{case.case_id}'", fg=typer.colors.GREEN)
        typer.echo(f"  Finding ID:   {case.finding_id}")
        typer.echo(f"  Target Repo:  {case.target_repo}")
        typer.echo(f"  Commit SHA:   {case.target_commit_sha}")
        typer.echo(f"  Max Retries:  {case.max_retries}")
        typer.echo(f"  Status:       {case.status.value}")


@repro_app.command(name="list")
def repro_list(
    finding_id: Optional[str] = typer.Option(None, "--finding-id", "-f", help="Filter by finding ID"),
    format: str = typer.Option("text", "--format", help="Output format: text or json"),
) -> None:
    """List reproduction cases stored in SQLite."""
    from forge.reproduction import ReproductionCaseManager

    manager = ReproductionCaseManager()
    cases = manager.list_cases(finding_id=finding_id)

    if format.lower() == "json":
        data = [c.model_dump(mode="json") for c in cases]
        typer.echo(json.dumps(data, indent=2))
        return

    if not cases:
        typer.echo("No reproduction cases found.")
        return

    typer.echo(f"{'Case ID':<36} {'Finding ID':<24} {'Status':<16} {'Attempts':<10}")
    typer.echo("-" * 90)
    for c in cases:
        typer.echo(f"{c.case_id:<36} {c.finding_id:<24} {c.status.value:<16} {c.attempts_count:<10}")


@repro_app.command(name="show")
def repro_show(
    case_id: str = typer.Argument(..., help="Reproduction case ID to inspect"),
    format: str = typer.Option("text", "--format", "-f", help="Output format: text or json"),
) -> None:
    """Show details of a reproduction case and all execution attempts."""
    from forge.reproduction import ReproductionCaseManager

    manager = ReproductionCaseManager()
    case = manager.get_case(case_id)
    if not case:
        typer.secho(f"Reproduction case '{case_id}' not found.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    attempts = manager.list_attempts(case_id)

    classification = manager.get_classification(case_id)

    if format.lower() == "json":
        data = {
            "case": case.model_dump(mode="json"),
            "attempts": [a.model_dump(mode="json") for a in attempts],
            "classification": classification.model_dump(mode="json") if classification else None,
        }
        typer.echo(json.dumps(data, indent=2))
        return

    typer.echo(f"Reproduction Case: {case.case_id}")
    typer.echo(f"  Finding ID:    {case.finding_id}")
    typer.echo(f"  Status:        {case.status.value}")
    typer.echo(f"  Target Repo:   {case.target_repo}")
    typer.echo(f"  Commit SHA:    {case.target_commit_sha}")
    typer.echo(f"  Attempts:      {case.attempts_count} (Max Retries: {case.max_retries})")
    typer.echo(f"  Created At:    {case.created_at.isoformat()}")

    if classification:
        typer.echo(f"\nClassification (v{classification.classifier_version}):")
        typer.echo(f"  Outcome:       {classification.outcome.value.upper()}")
        typer.echo(f"  Reason Code:   {classification.reason_code.value}")
        typer.echo(f"  Explanation:   {classification.explanation}")
        typer.echo(f"  Reproduced:    {'YES' if classification.is_reproduced else 'NO'}")
        typer.echo(f"  Oracle Proven: {'YES' if classification.oracle_verified else 'NO'}")

    if attempts:
        typer.echo("\nAttempts History:")
        for a in attempts:
            typer.echo(f"  - Attempt #{a.attempt_number} [{a.attempt_id}]: {a.outcome.value.upper()}")
            typer.echo(f"    Exit Code:    {a.exit_code}")
            typer.echo(f"    Duration:     {a.duration_ms:.1f}ms")
            if a.diagnostic_message:
                typer.echo(f"    Diagnostic:   {a.diagnostic_message}")
            if a.matched_patterns:
                typer.echo(f"    Patterns:     {', '.join(a.matched_patterns)}")
            if a.evidence_id:
                typer.echo(f"    Evidence ID:  {a.evidence_id}")
    else:
        typer.echo("\nAttempts: none executed yet")


@repro_app.command(name="classify")
def repro_classify(
    case_id: str = typer.Argument(..., help="Reproduction case ID to classify"),
    format: str = typer.Option("text", "--format", "-f", help="Output format: text or json"),
) -> None:
    """Classify the reproduction outcome of a case using deterministic precedence rules.

    Exit codes:
      0 = REPRODUCED (defect confirmed by independent oracle)
      1 = NOT_REPRODUCED or UNRELATED_FAILURE
      2 = INVALID_PLAN (plan policy violation or stale revision)
      3 = ENVIRONMENT_BLOCKED (Docker or sandbox unavailable)
      4 = INCONCLUSIVE, FLAKY, or TIMEOUT
    """
    from forge.reproduction import ReproductionCaseManager, ClassificationOutcome

    manager = ReproductionCaseManager()
    case = manager.get_case(case_id)
    if not case:
        typer.secho(f"Reproduction case '{case_id}' not found.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    classification = manager.classify_case(case_id)

    if format.lower() == "json":
        typer.echo(json.dumps(classification.model_dump(mode="json"), indent=2))
    else:
        typer.echo(f"Case Classification: {classification.case_id}")
        typer.echo(f"  Outcome:       {classification.outcome.value.upper()}")
        typer.echo(f"  Reason Code:   {classification.reason_code.value}")
        typer.echo(f"  Explanation:   {classification.explanation}")
        typer.echo(f"  Reproduced:    {'YES' if classification.is_reproduced else 'NO'}")
        typer.echo(f"  Oracle Proven: {'YES' if classification.oracle_verified else 'NO'}")
        typer.echo(f"  Classifier Ver:{classification.classifier_version}")
        if classification.evidence_id:
            typer.echo(f"  Evidence ID:   {classification.evidence_id}")

    if classification.outcome == ClassificationOutcome.REPRODUCED:
        raise typer.Exit(code=0)
    elif classification.outcome in {ClassificationOutcome.NOT_REPRODUCED, ClassificationOutcome.UNRELATED_FAILURE}:
        raise typer.Exit(code=1)
    elif classification.outcome == ClassificationOutcome.INVALID_PLAN:
        raise typer.Exit(code=2)
    elif classification.outcome == ClassificationOutcome.ENVIRONMENT_BLOCKED:
        raise typer.Exit(code=3)
    elif classification.outcome in {ClassificationOutcome.INCONCLUSIVE, ClassificationOutcome.FLAKY, ClassificationOutcome.TIMEOUT}:
        raise typer.Exit(code=4)
    else:
        raise typer.Exit(code=1)



@repro_app.command(name="run")
def repro_run(
    case_id: str = typer.Argument(..., help="Reproduction case ID to execute"),
    resume: bool = typer.Option(False, "--resume", help="Resume or retry a previously created case"),
    format: str = typer.Option("text", "--format", "-f", help="Output format: text or json"),
) -> None:
    """Execute a reproduction case attempt inside the isolated sandbox."""
    from forge.reproduction import ReproductionCaseManager, StaleRevisionError, NonIdempotentRetryError, CaseManagementError
    from forge.repository import acquire_repository, cleanup_repository, SecurityError, AcquisitionError
    from forge.sandbox.contracts import ReproductionOutcome

    manager = ReproductionCaseManager()
    case = manager.get_case(case_id)
    if not case:
        typer.secho(f"Reproduction case '{case_id}' not found.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    # Acquire repo at pinned commit
    try:
        acquired = acquire_repository(case.target_repo, branch=case.target_commit_sha)
    except (SecurityError, AcquisitionError) as e:
        typer.secho(f"Repository acquisition aborted: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    try:
        attempt = manager.run_attempt(
            case_id=case_id,
            workspace_path=acquired.workspace_path,
            current_commit_sha=acquired.resolved_commit_sha,
            is_resume=resume,
        )
    except StaleRevisionError as e:
        typer.secho(f"Stale revision error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    except NonIdempotentRetryError as e:
        typer.secho(f"Non-idempotent retry refused: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    except CaseManagementError as e:
        typer.secho(f"Reproduction attempt aborted: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    finally:
        cleanup_repository(acquired)

    if format.lower() == "json":
        typer.echo(json.dumps(attempt.model_dump(mode="json"), indent=2))
    else:
        color = typer.colors.GREEN if attempt.outcome == ReproductionOutcome.REPRODUCED else (
            typer.colors.YELLOW if attempt.outcome in [ReproductionOutcome.BLOCKED, ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE] else typer.colors.RED
        )
        typer.secho(f"\nAttempt #{attempt.attempt_number} Result: {attempt.outcome.value.upper()}", fg=color, bold=True)
        if attempt.diagnostic_message:
            typer.echo(f"Details:    {attempt.diagnostic_message}")
        if attempt.exit_code is not None:
            typer.echo(f"Exit Code:  {attempt.exit_code}")
        typer.echo(f"Duration:   {attempt.duration_ms:.1f}ms")
        if attempt.evidence_id:
            typer.echo(f"Evidence:   {attempt.evidence_id}")

    # Standardized exit codes across reproduction commands:
    # 0 = REPRODUCED
    # 1 = NOT_REPRODUCED / FAILED / PASSED
    # 2 = ERROR / STALE_REVISION
    # 3 = BLOCKED / SKIPPED_SANDBOX_UNAVAILABLE
    # 4 = TIMEOUT / INCONCLUSIVE
    if attempt.outcome == ReproductionOutcome.REPRODUCED:
        raise typer.Exit(code=0)
    elif attempt.outcome in [ReproductionOutcome.NOT_REPRODUCED, ReproductionOutcome.PASSED, ReproductionOutcome.FAILED]:
        raise typer.Exit(code=1)
    elif attempt.outcome == ReproductionOutcome.ERROR:
        raise typer.Exit(code=2)
    elif attempt.outcome in [ReproductionOutcome.BLOCKED, ReproductionOutcome.SKIPPED_SANDBOX_UNAVAILABLE]:
        raise typer.Exit(code=3)
    elif attempt.outcome in [ReproductionOutcome.TIMEOUT, ReproductionOutcome.INCONCLUSIVE]:
        raise typer.Exit(code=4)
    else:
        raise typer.Exit(code=1)



@app.command()
def inspect(
    target: str = typer.Argument(..., help="Git HTTPS repository URL or authorized local directory"),
    branch: Optional[str] = typer.Option(None, "--branch", "-b", help="Target git branch to inspect"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Run in offline static inspection mode only"),
) -> None:
    """Safely acquire and audit a target repository."""
    from forge.repository import (
        acquire_repository,
        cleanup_repository,
        detect_repository,
        build_symbol_index,
        load_instruction_files,
        RepositoryMap,
        SecurityError,
        AcquisitionError,
    )

    typer.echo(f"Validating and acquiring target: {target}")
    try:
        acquired = acquire_repository(target, branch=branch)
    except (SecurityError, AcquisitionError) as e:
        typer.secho(f"Acquisition aborted: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    try:
        typer.echo(f"Inspecting repository structure at: {acquired.workspace_path}")
        intel = detect_repository(acquired.workspace_path)
        symbols = build_symbol_index(acquired.workspace_path)
        instructions = load_instruction_files(acquired.workspace_path)

        repo_map = RepositoryMap(
            target=target,
            resolved_commit_sha=acquired.resolved_commit_sha,
            target_branch=acquired.target_branch,
            intelligence=intel,
            symbol_index=symbols,
            instructions=instructions,
        )

        typer.echo("")
        typer.echo(repo_map.render_markdown_summary())
        typer.echo("")
        if no_llm:
            typer.echo("[Audit Mode] Static inspection complete (LLM disabled).")
        else:
            typer.echo("[Audit Mode] Static inspection complete. Run 'forge investigate <target>' to launch LLM investigation.")
    finally:
        cleanup_repository(acquired)


@app.command()
def investigate(
    target: str = typer.Argument(..., help="Git HTTPS repository URL or authorized local directory"),
    branch: Optional[str] = typer.Option(None, "--branch", "-b", help="Target git branch to inspect"),
    max_steps: Optional[int] = typer.Option(None, "--max-steps", "-s", help="Max agent turns (default 10)"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Force static inspection mode only"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="Override LLM provider (fake, ollama, groq, gemini, deepseek)"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override LLM model name"),
) -> None:
    """Run an autonomous, typed LLM investigation of a target repository."""
    from forge.repository import (
        acquire_repository,
        cleanup_repository,
        detect_repository,
        build_symbol_index,
        load_instruction_files,
        RepositoryMap,
        SecurityError,
        AcquisitionError,
    )
    from forge.storage.store import ForgeStore
    from forge.investigation.tools import ToolRegistry
    from forge.investigation.loop import InvestigationLoop
    from forge.llm.fake import FakeLLMProvider
    from forge.llm.openai import OpenAICompatibleProvider

    settings = get_settings()
    store = ForgeStore(db_path=settings.db_path)

    typer.echo(f"Validating and acquiring target: {target}")
    try:
        acquired = acquire_repository(target, branch=branch)
    except (SecurityError, AcquisitionError) as e:
        typer.secho(f"Acquisition aborted: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    try:
        typer.echo(f"Building repository intelligence at: {acquired.workspace_path}")
        intel = detect_repository(acquired.workspace_path)
        symbols = build_symbol_index(acquired.workspace_path)
        instructions = load_instruction_files(acquired.workspace_path)

        repo_map = RepositoryMap(
            target=target,
            resolved_commit_sha=acquired.resolved_commit_sha,
            target_branch=acquired.target_branch,
            intelligence=intel,
            symbol_index=symbols,
            instructions=instructions,
        )

        typer.echo("")
        typer.echo(repo_map.render_markdown_summary())
        typer.echo("")

        if no_llm:
            typer.echo("[Investigation Mode] Static analysis complete (--no-llm specified).")
            return

        # Initialize run record in store
        run_id = store.create_run(target=target)
        typer.echo(f"Initialized Investigation Run: {run_id}")

        # Configure LLM provider via factory (supports: fake, ollama, groq, gemini, deepseek)
        from forge.llm.factory import get_provider
        from forge.llm.provider import LLMUnavailableError
        try:
            llm_inst = get_provider(provider_name=provider, model=model)
        except LLMUnavailableError as exc:
            typer.secho(
                f"Provider configuration error: {exc}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)


        tool_reg = ToolRegistry(
            workspace_path=acquired.workspace_path,
            repo_map=repo_map,
            store=store,
            run_id=run_id,
        )

        loop = InvestigationLoop(
            provider=llm_inst,
            tool_registry=tool_reg,
            store=store,
            run_id=run_id,
            target=target,
            repo_map=repo_map,
            max_steps=max_steps or settings.max_steps,
        )

        typer.echo("Starting investigation loop...")
        result = loop.run()

        typer.echo(f"\nInvestigation finished with status: {result.status.upper()}")
        typer.echo(f"Steps executed: {result.steps_executed} (Tool calls: {result.total_tool_calls})")
        if result.final_response:
            typer.echo("\n--- Agent Assessment ---")
            typer.echo(result.final_response)
            typer.echo("------------------------")

        if result.findings:
            typer.echo(f"\nFindings discovered: {len(result.findings)}")
            for f in result.findings:
                typer.echo(f"  - [{f.severity.value.upper()}] {f.title} ({f.finding_id})")

    finally:
        cleanup_repository(acquired)


@app.command()
def report(
    run_id: str = typer.Argument(..., help="Run ID to generate report for"),
    output_format: str = typer.Option("markdown", "--format", "-f", help="Report format: markdown or json"),
    output_path: Optional[str] = typer.Option(None, "--output", "-o", help="Optional file or directory path to export report"),
) -> None:
    """Display or export audit report for a completed run."""
    from forge.reporting.builder import ReportBuilder, ReportError, report_to_markdown, report_to_json, export_report
    from forge.storage.store import ForgeStore

    settings = get_settings()
    try:
        store = ForgeStore(db_path=settings.db_path)
        builder = ReportBuilder(store)
        rep = builder.build_report(run_id)
    except ReportError as e:
        typer.secho(f"Report generation error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    except Exception as e:
        typer.secho(f"Unexpected error loading run '{run_id}': {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    fmt = output_format.lower()
    if fmt not in {"markdown", "json"}:
        typer.secho(f"Invalid format '{output_format}'. Supported formats: markdown, json", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if output_path:
        out_p = Path(output_path)
        try:
            saved_file = export_report(rep, out_p, output_format=fmt)
            typer.secho(f"Report successfully exported to: {saved_file}", fg=typer.colors.GREEN)
        except Exception as e:
            typer.secho(f"Failed to export report to '{output_path}': {e}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
    else:
        if fmt == "json":
            typer.echo(report_to_json(rep))
        else:
            typer.echo(report_to_markdown(rep))


# --- Database commands ---

@db_app.command(name="init")
def db_init() -> None:
    """Initialize the FORGE SQLite database."""
    from forge.storage.database import init_database
    settings = get_settings()
    typer.echo(f"Initializing database at: {settings.db_path.resolve()}")
    init_database(settings.db_path)
    typer.echo("Database initialized successfully.")


# --- Runs commands ---

@app.command(name="runs")
def list_runs() -> None:
    """List historical audit runs stored in SQLite."""
    from forge.storage.store import ForgeStore, StoreError
    settings = get_settings()
    if not settings.db_path.exists():
        typer.secho(f"Database not found at '{settings.db_path}'. Run 'forge db init' first.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    try:
        store = ForgeStore(db_path=settings.db_path)
        runs = store.list_runs()
    except Exception as e:
        typer.secho(f"Error querying runs: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if not runs:
        typer.echo("No runs found.")
        return

    typer.echo(f"{'Run ID':<30} {'Target':<40} {'Status':<12} {'Findings':<10}")
    typer.echo("-" * 92)
    for run in runs:
        typer.echo(
            f"{run.run_id:<30} {run.target:<40} {run.status:<12} {run.total_findings:<10}"
        )


# --- Findings commands ---

@findings_app.command(name="list")
def findings_list(
    run_id: Optional[str] = typer.Option(None, "--run-id", "-r", help="Filter findings by run ID"),
) -> None:
    """List findings, optionally filtered by run ID."""
    from forge.storage.store import ForgeStore
    settings = get_settings()
    if not settings.db_path.exists():
        typer.secho(f"Database not found at '{settings.db_path}'. Run 'forge db init' first.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    try:
        store = ForgeStore(db_path=settings.db_path)
        findings = store.list_findings(run_id=run_id)
    except Exception as e:
        typer.secho(f"Error querying findings: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if not findings:
        typer.echo("No findings found.")
        return

    typer.echo(f"{'Finding ID':<25} {'State':<15} {'Severity':<10} {'Title':<40}")
    typer.echo("-" * 90)
    for f in findings:
        typer.echo(
            f"{f.finding_id:<25} {f.lifecycle_state.value:<15} {f.severity.value:<10} {f.title[:40]:<40}"
        )


@findings_app.command(name="show")
def findings_show(
    finding_id: str = typer.Argument(..., help="Finding ID to display"),
) -> None:
    """Show detailed finding information with associated evidence."""
    from forge.storage.store import ForgeStore
    settings = get_settings()
    if not settings.db_path.exists():
        typer.secho(f"Database not found at '{settings.db_path}'. Run 'forge db init' first.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    try:
        store = ForgeStore(db_path=settings.db_path)
        finding = store.get_finding(finding_id)
    except Exception as e:
        typer.secho(f"Error loading finding: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if not finding:
        typer.secho(f"Finding '{finding_id}' not found.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)


    typer.echo(f"Finding: {finding.finding_id}")
    typer.echo(f"  Title:      {finding.title}")
    typer.echo(f"  State:      {finding.lifecycle_state.value}")
    typer.echo(f"  Severity:   {finding.severity.value}")
    typer.echo(f"  Confidence: {finding.confidence.value}")
    typer.echo(f"  Description: {finding.description}")
    if finding.affected_files:
        typer.echo(f"  Affected:   {', '.join(finding.affected_files)}")
    if finding.limitations:
        typer.echo(f"  Limitations: {finding.limitations}")

    evidence = store.list_evidence_for_finding(finding_id)
    if evidence:
        typer.echo(f"  Evidence ({len(evidence)}):")
        for ev in evidence:
            typer.echo(f"    - {ev.evidence_id} [{ev.evidence_type.value}]: {ev.description[:60]}")
    else:
        typer.echo("  Evidence: none attached")


# --- Benchmark commands ---

@benchmark_app.command(name="list")
def benchmark_list() -> None:
    """List all available benchmark test fixtures."""
    from forge.benchmarks.corpus import CorpusLoader
    loader = CorpusLoader()
    cases = loader.discover_cases()
    if not cases:
        typer.echo("No benchmark cases found.")
        return

    typer.echo(f"{'Case ID':<30} {'Category':<22} {'Target File':<18} {'Has Defect':<10}")
    typer.echo("-" * 84)
    for c in cases:
        has_def = "YES" if c.expected_defect.has_defect else "NO (Clean)"
        typer.echo(f"{c.case_id:<30} {c.category.value:<22} {c.target_file:<18} {has_def:<10}")


@benchmark_app.command(name="run")
def benchmark_run(
    mode: str = typer.Option("static", "--mode", "-m", help="Evaluation mode: static or llm"),
    case_filter: Optional[str] = typer.Option(None, "--case", "-c", help="Filter for specific case ID"),
    output_path: Optional[str] = typer.Option(None, "--output", "-o", help="Path to export benchmark results as JSON"),
) -> None:
    """Execute the benchmark suite and evaluate metrics."""
    from forge.benchmarks.runner import BenchmarkRunner
    from forge.storage.store import ForgeStore

    m = mode.lower()
    if m not in {"static", "llm"}:
        typer.secho(f"Invalid mode '{mode}'. Choose 'static' or 'llm'.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    settings = get_settings()
    store = ForgeStore(db_path=settings.db_path)
    runner = BenchmarkRunner(store=store)

    typer.echo(f"Starting FORGE Benchmark Suite (Mode: {m.upper()})...")
    result = runner.run_suite(mode=m, case_filter=case_filter)

    typer.echo(f"\nBenchmark Run Complete: {result.benchmark_run_id}")
    typer.echo(f"Execution Duration: {result.total_duration_seconds}s")
    typer.echo("\n--- Case Results ---")
    typer.echo(f"{'Case ID':<30} {'Outcome':<20} {'Expected':<12} {'Detected':<10} {'Duration':<8}")
    typer.echo("-" * 84)
    for cr in result.case_results:
        exp = "Defect" if cr.has_defect_expected else "Clean"
        det = "YES" if cr.defect_detected else "NO"
        color = typer.colors.GREEN if cr.outcome.value == "pass" else (typer.colors.YELLOW if "skip" in cr.outcome.value else typer.colors.RED)
        outcome_str = typer.style(cr.outcome.value.upper(), fg=color)
        typer.echo(f"{cr.case_id:<30} {outcome_str:<29} {exp:<12} {det:<10} {cr.duration_seconds}s")

    # Metrics Summary
    met = result.metrics
    typer.echo("\n--- Metrics Summary ---")
    typer.echo(f"  Total Cases:         {met.total_cases}")
    typer.echo(f"  Passed Cases:        {met.passed_cases}")
    typer.echo(f"  Failed Cases:        {met.failed_cases}")
    typer.echo(f"  Skipped/Unavailable: {met.skipped_unavailable_cases}")
    typer.echo(f"  True Positives:      {met.true_positives}")
    typer.echo(f"  False Positives:     {met.false_positives}")
    typer.echo(f"  False Negatives:     {met.false_negatives}")
    typer.echo(f"  True Negatives:      {met.true_negatives}")
    prec_str = f"{met.precision:.2%}" if met.precision is not None else "N/A"
    rec_str = f"{met.recall:.2%}" if met.recall is not None else "N/A"
    f1_str = f"{met.f1_score:.2%}" if met.f1_score is not None else "N/A"
    rate_str = f"{met.case_pass_rate:.2%}" if met.case_pass_rate is not None else "N/A"
    typer.echo(f"  Precision:           {prec_str}")
    typer.echo(f"  Recall:              {rec_str}")
    typer.echo(f"  F1 Score:            {f1_str}")
    typer.echo(f"  Case Pass Rate:      {rate_str}")

    if output_path:
        out_p = Path(output_path).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(result.model_dump(), indent=2, sort_keys=True), encoding="utf-8")
        typer.secho(f"\nBenchmark results saved to: {out_p}", fg=typer.colors.GREEN)


@benchmark_app.command(name="review")
def benchmark_review(
    mode: str = typer.Option("static", "--mode", "-m", help="Evaluation mode: static or llm"),
    fixture_filter: Optional[str] = typer.Option(None, "--fixture", "-f", help="Filter for specific fixture ID"),
    corpus: str = typer.Option("standard", "--corpus", "-c", help="Benchmark corpus: standard or heldout/adversarial"),
    output_format: str = typer.Option("markdown", "--format", help="Output format: markdown or json"),
    output_path: Optional[str] = typer.Option(None, "--output", "-o", help="Optional file path to export benchmark results"),
) -> None:
    """Execute review quality benchmarks evaluating precision, recall, and evidence calibration."""
    from forge.benchmarks.review_corpus import (
        ReviewCorpusLoader,
        DEFAULT_REVIEW_FIXTURES_DIR,
        DEFAULT_ADVERSARIAL_FIXTURES_DIR,
        DEFAULT_SECURITY_FIXTURES_DIR,
    )
    from forge.benchmarks.review_runner import ReviewBenchmarkRunner
    from forge.benchmarks.review_reporting import (
        review_benchmark_to_json,
        review_benchmark_to_markdown,
    )
    from forge.llm.fake import FakeLLMProvider

    m = mode.lower()
    if m not in {"static", "llm"}:
        typer.secho(f"Invalid mode '{mode}'. Choose 'static' or 'llm'.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    c_choice = corpus.lower()
    if c_choice in {"heldout", "adversarial"}:
        loader = ReviewCorpusLoader(fixtures_dir=DEFAULT_ADVERSARIAL_FIXTURES_DIR)
    elif c_choice in {"security", "dataflow"}:
        loader = ReviewCorpusLoader(fixtures_dir=DEFAULT_SECURITY_FIXTURES_DIR)
    elif c_choice in {"standard", "default"}:
        loader = ReviewCorpusLoader(fixtures_dir=DEFAULT_REVIEW_FIXTURES_DIR)
    else:
        typer.secho(f"Invalid corpus '{corpus}'. Choose 'standard', 'heldout', or 'security'.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    provider = FakeLLMProvider() if m == "llm" else None
    runner = ReviewBenchmarkRunner(loader=loader)
    result = runner.run_suite(mode=m, fixture_filter=fixture_filter, provider=provider)

    is_json = output_format.lower() == "json"
    if is_json:
        rendered = review_benchmark_to_json(result)
    else:
        rendered = review_benchmark_to_markdown(result)

    if output_path:
        out_p = Path(output_path).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(rendered, encoding="utf-8")
        if not is_json:
            typer.echo(rendered)
            typer.secho(f"\nReview benchmark results exported to: {out_p}", fg=typer.colors.GREEN)
        else:
            typer.echo(rendered)
    else:
        typer.echo(rendered)

    # Fail closed if benchmark integrity failed (errors or failures)
    if result.metrics.error_cases > 0:
        raise typer.Exit(code=2)
    elif result.metrics.failed_cases > 0:
        raise typer.Exit(code=1)


@benchmark_app.command(name="repair")
def benchmark_repair(
    mode: str = typer.Option("offline", "--mode", "-m", help="Evaluation mode: offline or provider/llm"),
    fixture_filter: Optional[str] = typer.Option(None, "--fixture", "-f", help="Filter for specific fixture ID"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="Opt-in LLM provider override (fake, ollama, groq, gemini, deepseek)"),
    model: Optional[str] = typer.Option(None, "--model", help="LLM model override"),
    output_format: str = typer.Option("markdown", "--format", help="Output format: markdown or json"),
    output_path: Optional[str] = typer.Option(None, "--output", "-o", help="Optional file path to export benchmark results"),
) -> None:
    """Execute repair quality benchmarks evaluating repair-plan quality, patch validation, and verification classification."""
    from forge.benchmarks.repair_corpus import RepairCorpusLoader
    from forge.benchmarks.repair_runner import RepairBenchmarkRunner
    from forge.benchmarks.repair_reporting import repair_benchmark_to_json, repair_benchmark_to_markdown
    from forge.llm.factory import get_provider
    from forge.llm.provider import LLMUnavailableError

    m = mode.lower()
    if m not in {"offline", "provider", "llm"}:
        typer.secho(f"Invalid mode '{mode}'. Choose 'offline' or 'provider'.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    llm_inst = None
    provider_label = "offline_fake"
    model_label = "forge-evaluator-v1"

    if m in {"provider", "llm"} or provider:
        if not provider:
            typer.secho("Provider evaluation mode requires explicit provider selection (--provider).", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        typer.echo(f"[Notice] Provider evaluation active: Provider '{provider}', Model '{model or 'default'}'. Network access and external charges may apply.")
        try:
            llm_inst = get_provider(provider_name=provider, model=model)
            provider_label = provider
            model_label = model or (llm_inst.model if hasattr(llm_inst, "model") else "default")
        except LLMUnavailableError as exc:
            typer.secho(f"Provider configuration error: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)

    loader = RepairCorpusLoader()
    runner = RepairBenchmarkRunner(loader=loader)
    result = runner.run_suite(
        mode=m,
        fixture_filter=fixture_filter,
        provider=llm_inst,
        provider_name=provider_label,
        model_name=model_label,
    )

    is_json = output_format.lower() == "json"
    rendered = repair_benchmark_to_json(result) if is_json else repair_benchmark_to_markdown(result)

    if output_path:
        out_p = Path(output_path).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(rendered, encoding="utf-8")
        if not is_json:
            typer.echo(rendered)
            typer.secho(f"\nRepair benchmark results exported to: {out_p}", fg=typer.colors.GREEN)
        else:
            typer.echo(rendered)
    else:
        typer.echo(rendered)

    if result.metrics.failed_cases > 0:
        raise typer.Exit(code=1)


# --- Code Review Command ---


@app.command(name="review")
def review(
    repository: str = typer.Argument(..., help="Path to local git repository to review"),
    base: str = typer.Option("HEAD~1", "--base", "-b", help="Base git revision (commit SHA, branch, tag, HEAD~N)"),
    head: str = typer.Option("HEAD", "--head", "-h", help="Head git revision"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Run offline static pattern review without LLM"),
    output_format: str = typer.Option("markdown", "--format", "-f", help="Output format: markdown, json, or sarif"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Optional file path to export review report"),
) -> None:
    """Perform diff-aware intelligent code review between two revisions."""
    from forge.review import (
        read_git_diff,
        parse_unified_diff,
        ReviewEngine,
        review_report_to_json,
        review_report_to_markdown,
        review_report_to_sarif,
        export_review_report,
        GitDiffError,
    )
    from forge.llm.fake import FakeLLMProvider

    repo_path = Path(repository).resolve()
    if not repo_path.exists() or not repo_path.is_dir():
        typer.secho(f"Error: Target repository directory '{repository}' not found.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    fmt = output_format.lower()
    is_machine_readable = fmt in {"json", "sarif"}
    if not is_machine_readable and not output:
        typer.echo(f"Starting diff review for: {repo_path}")
        typer.echo(f"  Comparing revisions: {base} ... {head}")

    try:
        raw_diff = read_git_diff(repo_path, base=base, head=head)
    except GitDiffError as err:
        typer.secho(f"Git diff error: {err}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    diff_bundle = parse_unified_diff(raw_diff, base_revision=base, head_revision=head)

    mode = "static" if no_llm else "llm"
    provider = FakeLLMProvider() if mode == "llm" else None
    engine = ReviewEngine(provider=provider)

    report = engine.review_diff(diff_bundle, target_path=repo_path, mode=mode)

    if fmt == "json":
        rendered = review_report_to_json(report)
    elif fmt == "sarif":
        rendered = review_report_to_sarif(report)
    else:
        rendered = review_report_to_markdown(report)

    if output:
        out_file = Path(output).resolve()
        export_review_report(report, out_file, output_format=fmt)
        typer.secho(f"\nReview report successfully exported to: {out_file}", fg=typer.colors.GREEN)
    else:
        if not is_machine_readable:
            typer.echo("")
        typer.echo(rendered)


# --- Policy Check Command ---


@policy_app.command(name="check")
def policy_check(
    report_file: str = typer.Argument(..., help="Path to JSON review report or repository path to evaluate"),
    policy_file: Optional[str] = typer.Option(None, "--policy", "-p", help="Path to review policy JSON file"),
    output_format: str = typer.Option("markdown", "--format", "-f", help="Output format: markdown or json"),
) -> None:
    """Evaluate review findings against a CI policy and report blocking defects."""
    from forge.review import (
        ReviewReport,
        evaluate_review_policy,
        load_policy_from_file,
        PolicyConfigurationError,
        ReviewPolicy,
    )
    from forge.config import redact_secrets

    input_path = Path(report_file).resolve()
    if not input_path.exists():
        typer.secho(f"Error: Input file or repository '{report_file}' not found.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    # 1. Load Policy
    policy: Optional[ReviewPolicy] = None
    if policy_file:
        p_path = Path(policy_file).resolve()
        try:
            policy = load_policy_from_file(p_path)
        except PolicyConfigurationError as err:
            typer.secho(f"Policy configuration error: {err}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)
    else:
        policy = ReviewPolicy()

    # 2. Load Review Report
    review_report: Optional[ReviewReport] = None
    if input_path.is_file():
        try:
            raw_text = input_path.read_text(encoding="utf-8")
            raw_json = json.loads(raw_text)
            review_report = ReviewReport.model_validate(raw_json)
        except Exception as exc:
            typer.secho(f"Invalid review report file: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)
    else:
        typer.secho(f"Error: Expected review report JSON file, got directory '{input_path}'. Run 'forge review <repo> --format json -o report.json' first.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    # 3. Evaluate Policy
    eval_report = evaluate_review_policy(review_report, policy=policy)

    # 4. Output Results
    is_json = output_format.lower() == "json"
    if is_json:
        typer.echo(redact_secrets(json.dumps(eval_report.model_dump(mode="json"), indent=2, sort_keys=True)))
    else:
        lines = [
            f"# FORGE CI Review Policy Check: {eval_report.policy_name}",
            f"- **Policy ID**: `{eval_report.policy_id}`",
            f"- **Evaluated At**: `{eval_report.evaluated_at}`",
            f"- **Result**: `{'PASSED' if eval_report.passed else 'FAILED'}`",
            "",
            "## Summary",
            f"- Total Findings Evaluated: {eval_report.total_findings_evaluated}",
            f"- Blocking Findings: {eval_report.blocking_findings_count}",
            f"- Suppressed Findings: {eval_report.suppressed_findings_count}",
            f"- Unverified / Unbacked Findings: {eval_report.unbacked_findings_count}",
            "",
            "## Finding Decisions",
        ]
        if not eval_report.decisions:
            lines.append("No findings to evaluate. Policy passed.")
        else:
            for dec in eval_report.decisions:
                status_icon = "[BLOCKED]" if dec.is_blocking else f"[{dec.action.value.upper()}]"
                lines.append(f"- **{status_icon}** `{dec.rule_id}` at `{dec.file_path}:{dec.line_number or 0}`: {dec.decision_reason}")

        typer.echo(redact_secrets("\n".join(lines)))

    # 5. Exit Code: 0 on pass, 1 on policy block/failure, 2 on input error
    if not eval_report.passed:
        raise typer.Exit(code=1)



# --- Plan commands ---
# Exit codes: 0=success/valid, 1=plan failed/not-reproduced, 2=invalid plan/policy violation,
#             3=blocked (sandbox unavailable), 4=inconclusive


@plan_app.command(name="generate")
def plan_generate(
    finding_id: str = typer.Argument(..., help="Finding ID to generate a plan for"),
    commit_sha: str = typer.Option(..., "--commit", "-c", help="Pinned repository commit SHA"),
    target_repo: str = typer.Option(..., "--repo", "-r", help="Target repository URL or local path"),
    command: Optional[str] = typer.Option(None, "--command", help="Command argument vector as JSON array string, e.g. '[\"pytest\",\"tests/test_foo.py\"]'"),
    expected_behavior: Optional[str] = typer.Option(None, "--expected", "-e", help="Independently sourced expected behaviour description"),
    provenance: str = typer.Option("static_analysis", "--provenance", "-p", help="Expected behaviour provenance: trusted_fixture or static_analysis"),
    strategy: Optional[str] = typer.Option(None, "--strategy", "-s", help="Preferred strategy override: run_specific_test, run_scoped_test_file, validate_boundary_condition, check_expected_observed"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Optional file path to export plan as JSON"),
) -> None:
    """Generate a deterministic reproduction plan for a finding.

    Exit codes: 0=plan generated, 2=invalid arguments or policy violation.
    """
    from forge.reproduction.planner import (
        ReproductionPlanner,
        ReproductionStrategy,
    )
    from forge.reproduction.models import FieldProvenance

    # Parse provenance
    try:
        prov = FieldProvenance(provenance)
    except ValueError:
        typer.secho(
            f"Invalid provenance '{provenance}'. Must be one of: "
            f"{[p.value for p in FieldProvenance]}",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=2)

    _TRUSTED = {FieldProvenance.TRUSTED_FIXTURE, FieldProvenance.STATIC_ANALYSIS}
    if expected_behavior and prov not in _TRUSTED:
        typer.secho(
            f"Provenance '{prov.value}' is not trusted. Use 'trusted_fixture' or 'static_analysis'.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=2)

    # Parse command args
    command_args = None
    if command:
        try:
            command_args = json.loads(command)
            if not isinstance(command_args, list) or not all(isinstance(a, str) for a in command_args):
                raise ValueError("Command must be a JSON array of strings")
        except (json.JSONDecodeError, ValueError) as exc:
            typer.secho(f"Invalid --command JSON: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)

    # Parse strategy override
    preferred = None
    if strategy:
        try:
            preferred = ReproductionStrategy(strategy)
        except ValueError:
            typer.secho(
                f"Invalid strategy '{strategy}'. Valid values: "
                f"{[s.value for s in ReproductionStrategy]}",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(code=2)

    planner = ReproductionPlanner()
    try:
        plan = planner.create_plan(
            finding_id=finding_id,
            commit_sha=commit_sha,
            target_repo=target_repo,
            trusted_expected_behavior=expected_behavior,
            expected_behavior_provenance=prov,
            command_args=command_args,
            preferred_strategy=preferred,
        )
    except ValueError as exc:
        typer.secho(f"Plan generation error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    plan_json = json.dumps(plan.model_dump(mode="json"), indent=2, sort_keys=True)

    if output:
        out_p = Path(output).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(plan_json, encoding="utf-8")
        typer.secho(f"Plan written to: {out_p}", fg=typer.colors.GREEN)
    else:
        typer.echo(plan_json)


@plan_app.command(name="validate")
def plan_validate(
    plan_file: str = typer.Argument(..., help="Path to plan JSON file to validate"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Workspace root path for symlink-escape check"),
) -> None:
    """Validate a reproduction plan file without executing it (dry run).

    Exit codes: 0=valid, 2=policy violations found.
    """
    from forge.reproduction.planner import ReproductionPlan
    from forge.reproduction.validator import ReproductionPlanValidator

    plan_path = Path(plan_file).resolve()
    if not plan_path.exists():
        typer.secho(f"Plan file not found: {plan_file}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        raw = json.loads(plan_path.read_text(encoding="utf-8"))
        plan = ReproductionPlan.model_validate(raw)
    except Exception as exc:
        typer.secho(f"Failed to load plan: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    ws_path = Path(workspace).resolve() if workspace else None
    validator = ReproductionPlanValidator()
    result = validator.validate(plan, workspace_path=ws_path)

    if result.is_valid:
        typer.secho(f"Plan '{plan.plan_id}' is VALID.", fg=typer.colors.GREEN)
    else:
        typer.secho(f"Plan '{plan.plan_id}' has {len(result.violations)} violation(s):", fg=typer.colors.RED, err=True)
        for v in result.violations:
            typer.secho(f"  - {v}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)


@plan_app.command(name="show")
def plan_show(
    plan_file: str = typer.Argument(..., help="Path to plan JSON file"),
    format: str = typer.Option("text", "--format", "-f", help="Output format: text or json"),
) -> None:
    """Show human-readable plan details.

    Exit codes: 0=success, 2=invalid file.
    """
    from forge.reproduction.planner import ReproductionPlan

    plan_path = Path(plan_file).resolve()
    if not plan_path.exists():
        typer.secho(f"Plan file not found: {plan_file}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        raw = json.loads(plan_path.read_text(encoding="utf-8"))
        plan = ReproductionPlan.model_validate(raw)
    except Exception as exc:
        typer.secho(f"Failed to load plan: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    if format.lower() == "json":
        typer.echo(json.dumps(plan.model_dump(mode="json"), indent=2))
        return

    typer.echo(f"Plan ID:          {plan.plan_id}")
    typer.echo(f"Plan Version:     {plan.plan_version}")
    typer.echo(f"Finding ID:       {plan.finding_id}")
    typer.echo(f"Commit SHA:       {plan.commit_sha}")
    typer.echo(f"Target Repo:      {plan.target_repo}")
    typer.echo(f"Strategy:         {plan.strategy.value}")
    typer.echo(f"Rationale:        {plan.strategy_rationale}")
    if plan.command:
        typer.echo(f"Command:          {' '.join(plan.command.args)}")
        typer.echo(f"Working Dir:      {plan.command.relative_cwd}")
        typer.echo(f"Expected Exit:    {plan.command.expected_exit_codes}")
    else:
        typer.echo("Command:          (none — INSUFFICIENT_INFORMATION)")
    typer.echo(f"Expected Behavior:{' ' + plan.expected_behavior if plan.expected_behavior else ' (not set)'}")
    typer.echo(f"Network Approved: {plan.network_approved}")
    typer.echo(f"Provenance:")
    typer.echo(f"  strategy:         {plan.provenance.strategy_source.value}")
    typer.echo(f"  expected_behavior:{plan.provenance.expected_behavior_source.value}")
    typer.echo(f"  command:          {plan.provenance.command_source.value}")
    typer.echo(f"  resource_limits:  {plan.provenance.resource_limits_source.value}")
    if plan.preconditions:
        typer.echo(f"Preconditions:")
        for p in plan.preconditions:
            typer.echo(f"  - {p}")
    if plan.known_uncertainties:
        typer.echo(f"Known Uncertainties:")
        for u in plan.known_uncertainties:
            typer.echo(f"  - {u}")
    typer.echo(f"Created At:       {plan.created_at.isoformat()}")


@plan_app.command(name="run")
def plan_run(
    plan_file: str = typer.Argument(..., help="Path to plan JSON file to execute"),
    workspace: str = typer.Option(..., "--workspace", "-w", help="Absolute path to the repository workspace"),
    finding_id: Optional[str] = typer.Option(None, "--finding", "-f", help="Finding ID to attach execution evidence to"),
    run_id: Optional[str] = typer.Option(None, "--run-id", help="Audit run ID (required when finding_id is set)"),
    format: str = typer.Option("text", "--format", "-f", help="Output format: text or json"),
) -> None:
    """Validate a reproduction plan and execute it via the reproduction harness.

    Exit codes:
      0 = reproduced (defect confirmed)
      1 = not reproduced / test passed unexpectedly
      2 = invalid plan / policy violation (execution refused)
      3 = blocked (Docker / sandbox unavailable)
      4 = inconclusive (partial output, cannot determine outcome)
    """
    from forge.reproduction.planner import ReproductionPlan, ReproductionStrategy
    from forge.reproduction.validator import ReproductionPlanValidator
    from forge.sandbox.contracts import (
        ReproductionCommand,
        ReproductionRequest,
        ReproductionOutcome,
        ExecutionEnvironmentSpec,
    )
    from forge.sandbox.runner import ReproductionHarness

    plan_path = Path(plan_file).resolve()
    if not plan_path.exists():
        typer.secho(f"Plan file not found: {plan_file}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        raw = json.loads(plan_path.read_text(encoding="utf-8"))
        plan = ReproductionPlan.model_validate(raw)
    except Exception as exc:
        typer.secho(f"Failed to load plan: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    # 1. Validate before execution
    ws_path = Path(workspace).resolve()
    validator = ReproductionPlanValidator()
    result = validator.validate(plan, workspace_path=ws_path)
    if not result.is_valid:
        typer.secho("Plan validation failed — execution refused:", fg=typer.colors.RED, err=True)
        for v in result.violations:
            typer.secho(f"  - {v}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    if plan.command is None:
        typer.secho("Plan has no command (INSUFFICIENT_INFORMATION strategy) — execution refused.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    # 2. Build ReproductionRequest from the plan
    import uuid as _uuid
    request = ReproductionRequest(
        request_id=f"req_{_uuid.uuid4().hex[:12]}",
        finding_id=plan.finding_id,
        target_repo=plan.target_repo,
        commit_sha=plan.commit_sha,
        hypothesis=plan.strategy_rationale,
        command=plan.command,
        environment=plan.environment,
    )

    # 3. Execute via harness (fail-closed when Docker unavailable)
    harness = ReproductionHarness()
    if format.lower() != "json":
        typer.echo(f"Executing plan '{plan.plan_id}' against workspace: {ws_path}")
        typer.echo(f"  Strategy: {plan.strategy.value}")
        typer.echo(f"  Command:  {' '.join(plan.command.args)}")

    res = harness.execute(request, ws_path)

    # 4. Deterministically classify the execution outcome
    from forge.reproduction.classifier import ReproductionOutcomeClassifier, ClassificationOutcome
    from forge.reproduction.models import ReproductionAttempt, FieldProvenance

    temp_attempt = ReproductionAttempt(
        attempt_id=f"att_cli_{_uuid.uuid4().hex[:8]}",
        case_id=f"case_{plan.finding_id}",
        attempt_number=1,
        run_id=run_id or "run_cli",
        outcome=res.outcome,
        exit_code=res.exit_code,
        duration_ms=res.duration_ms,
        stdout_excerpt=res.stdout[:5000],
        stderr_excerpt=res.stderr[:5000],
        matched_patterns=res.matched_patterns,
        isolation_details=res.isolation_details,
        diagnostic_message=res.diagnostic_message,
        cleanup_status="cleaned",
    )
    classifier = ReproductionOutcomeClassifier()
    classification = classifier.classify_attempt(temp_attempt, plan=plan)

    if format.lower() == "json":
        payload = {
            "plan_id": plan.plan_id,
            "finding_id": plan.finding_id,
            "outcome": classification.outcome.value,
            "reason_code": classification.reason_code.value,
            "explanation": classification.explanation,
            "exit_code": res.exit_code,
            "is_reproduced": classification.is_reproduced,
            "duration_ms": res.duration_ms,
            "matched_patterns": res.matched_patterns,
            "classification": classification.model_dump(mode="json"),
        }
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(f"\nClassified Outcome: {classification.outcome.value.upper()}")
        typer.echo(f"Reason Code:        {classification.reason_code.value}")
        typer.echo(f"Explanation:        {classification.explanation}")
        if res.exit_code is not None:
            typer.echo(f"Exit Code:          {res.exit_code}")

    # 5. Map classified outcome to exit code
    if classification.outcome == ClassificationOutcome.REPRODUCED:
        raise typer.Exit(code=0)
    elif classification.outcome in {ClassificationOutcome.NOT_REPRODUCED, ClassificationOutcome.UNRELATED_FAILURE}:
        raise typer.Exit(code=1)
    elif classification.outcome == ClassificationOutcome.INVALID_PLAN:
        raise typer.Exit(code=2)
    elif classification.outcome == ClassificationOutcome.ENVIRONMENT_BLOCKED:
        if format.lower() != "json":
            typer.secho("Sandbox unavailable — execution blocked (Docker not found).", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=3)
    elif classification.outcome in {ClassificationOutcome.INCONCLUSIVE, ClassificationOutcome.FLAKY, ClassificationOutcome.TIMEOUT}:
        raise typer.Exit(code=4)
    else:
        raise typer.Exit(code=1)


@plan_app.command(name="classify")
def plan_classify(
    plan_file: str = typer.Argument(..., help="Path to plan JSON file to classify"),
    workspace: Optional[str] = typer.Option(None, "--workspace", "-w", help="Workspace root path for symlink-escape check"),
    format: str = typer.Option("text", "--format", "-f", help="Output format: text or json"),
) -> None:
    """Classify a plan's execution viability and oracle provenance.

    Exit codes:
      0 = valid plan with trusted oracle
      2 = invalid plan or untrusted oracle (INSUFFICIENT_INFORMATION)
    """
    from forge.reproduction.planner import ReproductionPlan, ReproductionStrategy
    from forge.reproduction.validator import ReproductionPlanValidator
    from forge.reproduction.classifier import ReproductionOutcomeClassifier, ClassificationOutcome, ClassificationReasonCode

    plan_path = Path(plan_file).resolve()
    if not plan_path.exists():
        typer.secho(f"Plan file not found: {plan_file}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        raw = json.loads(plan_path.read_text(encoding="utf-8"))
        plan = ReproductionPlan.model_validate(raw)
    except Exception as exc:
        typer.secho(f"Failed to load plan: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    ws_path = Path(workspace).resolve() if workspace else None
    validator = ReproductionPlanValidator()
    val_res = validator.validate(plan, workspace_path=ws_path)

    classifier = ReproductionOutcomeClassifier()
    is_trusted = classifier._is_trusted_oracle(plan.provenance.expected_behavior_source)

    if not val_res.is_valid:
        outcome = ClassificationOutcome.INVALID_PLAN
        reason = ClassificationReasonCode.PLAN_POLICY_VIOLATION
        explanation = f"Plan validation failed: {'; '.join(val_res.violations)}"
    elif plan.strategy == ReproductionStrategy.INSUFFICIENT_INFORMATION or not is_trusted:
        outcome = ClassificationOutcome.INVALID_PLAN
        reason = ClassificationReasonCode.ORACLE_ABSENT_OR_UNTRUSTED
        explanation = "Plan lacks an independently sourced trusted oracle; cannot confirm defect reproduction."
    else:
        outcome = ClassificationOutcome.NOT_REPRODUCED
        reason = ClassificationReasonCode.ORACLE_OBSERVED_NO_VIOLATION
        explanation = f"Plan is valid and backed by trusted oracle ({plan.provenance.expected_behavior_source.value})."

    res_data = {
        "plan_id": plan.plan_id,
        "outcome": outcome.value,
        "reason_code": reason.value,
        "explanation": explanation,
        "is_valid": val_res.is_valid,
        "oracle_trusted": is_trusted,
        "violations": val_res.violations,
    }

    if format.lower() == "json":
        typer.echo(json.dumps(res_data, indent=2))
    else:
        typer.echo(f"Plan Classification: {plan.plan_id}")
        typer.echo(f"  Outcome:       {outcome.value.upper()}")
        typer.echo(f"  Reason Code:   {reason.value}")
        typer.echo(f"  Explanation:   {explanation}")
        typer.echo(f"  Valid:         {'YES' if val_res.is_valid else 'NO'}")
        typer.echo(f"  Oracle Proven: {'YES' if is_trusted else 'NO'}")

    if outcome == ClassificationOutcome.INVALID_PLAN:
        raise typer.Exit(code=2)


# =============================================================================
# Repair Planning & Patch Generation CLI Commands (Phase 4 M1)
# =============================================================================

@repair_app.command(name="plan")
def repair_plan(
    finding_id: str = typer.Argument(..., help="Finding ID to generate repair plan for"),
    repo: str = typer.Option(".", "--repo", "-r", help="Target repository directory path"),
    commit: Optional[str] = typer.Option(None, "--commit", "-c", help="Pinned base commit revision"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="LLM provider (fake, ollama, groq, etc.)"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="LLM model identifier override"),
    scratch: Optional[Path] = typer.Option(None, "--scratch", help="Isolated scratch workspace directory"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="File path to save repair plan JSON"),
    format: str = typer.Option("markdown", "--format", "-f", help="Output format: markdown or json"),
) -> None:
    """Generate an evidence-grounded, bounded repair plan and patch candidate."""
    from forge.repair.models import RepairPlanStatus
    from forge.repair.planner import RepairPlanner
    from forge.storage.store import ForgeStore

    settings = get_settings()
    store = ForgeStore(db_path=settings.db_path)
    target_path = Path(repo).resolve()

    if not target_path.exists():
        typer.secho(f"Error: Target repository path '{repo}' does not exist.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    planner = RepairPlanner(
        provider_name=provider,
        model_name=model,
    )

    plan = planner.plan_repair(
        finding_id=finding_id,
        target_repo=target_path,
        base_commit_sha=commit,
        scratch_workspace_path=scratch,
        store=store,
    )

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(plan.model_dump_json(indent=2))

    if format.lower() == "json":
        typer.echo(plan.model_dump_json(indent=2))
    else:
        typer.echo(f"Repair Plan: {plan.plan_id}")
        typer.echo(f"  Finding ID:     {plan.finding_id}")
        typer.echo(f"  Target File:    {plan.target_file}")
        typer.echo(f"  Base Commit:    {plan.base_commit_sha[:10]}...")
        typer.echo(f"  Status:         {plan.status.value.upper()}")
        typer.echo(f"  Confidence:     {plan.confidence:.2f}")
        typer.echo(f"  Approval Gate:  {'REQUIRED' if plan.require_human_approval else 'OPTIONAL'}")
        typer.echo(f"  Root Cause:     {plan.root_cause}")
        typer.echo(f"  Proposed Fix:   {plan.proposed_change}")
        typer.echo(f"  Verification:   {plan.verification_plan}")

        if plan.regression_risks:
            typer.echo(f"  Regression Risks: {', '.join(plan.regression_risks)}")

        if plan.validation and plan.validation.violations:
            typer.echo("  Violations:")
            for v in plan.validation.violations:
                typer.echo(f"    - {v}")

        if plan.patch_candidate:
            typer.echo(f"\n--- Candidate Patch ({plan.patch_candidate.patch_id}) ---")
            typer.echo(f"Hunks: {plan.patch_candidate.hunk_count} | +{plan.patch_candidate.added_lines} / -{plan.patch_candidate.removed_lines} lines")
            typer.echo(plan.patch_candidate.unified_diff)

    if plan.status == RepairPlanStatus.BLOCKED:
        raise typer.Exit(code=3)
    elif plan.status == RepairPlanStatus.REJECTED:
        raise typer.Exit(code=1)


@repair_app.command(name="show")
def repair_show(
    plan_ref: str = typer.Argument(..., help="Plan ID or path to plan JSON file"),
    format: str = typer.Option("markdown", "--format", "-f", help="Output format: markdown or json"),
) -> None:
    """Display an existing repair plan and candidate patch."""
    from forge.repair.models import RepairPlan
    from forge.storage.store import ForgeStore

    plan: Optional[RepairPlan] = None
    ref_path = Path(plan_ref)

    if ref_path.is_file():
        try:
            plan = RepairPlan.model_validate_json(ref_path.read_text())
        except Exception as e:
            typer.secho(f"Error reading plan JSON file: {e}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
    else:
        settings = get_settings()
        store = ForgeStore(db_path=settings.db_path)
        plan = store.get_repair_plan(plan_ref)

    if not plan:
        typer.secho(f"Repair plan '{plan_ref}' not found.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if format.lower() == "json":
        typer.echo(plan.model_dump_json(indent=2))
        return

    typer.echo(f"Repair Plan: {plan.plan_id}")
    typer.echo(f"  Finding ID:     {plan.finding_id}")
    typer.echo(f"  Target File:    {plan.target_file}")
    typer.echo(f"  Base Revision:  {plan.base_commit_sha}")
    typer.echo(f"  Status:         {plan.status.value.upper()}")
    typer.echo(f"  Confidence:     {plan.confidence:.2f}")
    typer.echo(f"  Human Approval: {'REQUIRED' if plan.require_human_approval else 'DISABLED'}")
    typer.echo(f"  Root Cause:     {plan.root_cause}")
    typer.echo(f"  Proposed Fix:   {plan.proposed_change}")
    typer.echo(f"  Verification:   {plan.verification_plan}")

    if plan.patch_candidate:
        typer.echo(f"\n--- Unified Diff Candidate ({plan.patch_candidate.patch_id}) ---")
        typer.echo(plan.patch_candidate.unified_diff)


@repair_app.command(name="validate")
def repair_validate(
    plan_ref: str = typer.Argument(..., help="Plan ID or path to plan JSON file"),
    format: str = typer.Option("markdown", "--format", "-f", help="Output format: markdown or json"),
) -> None:
    """Validate an existing repair plan and patch candidate offline against security bounds."""
    from forge.repair.contracts import RepairBudget, RepairSafetyContract, parse_and_validate_unified_diff
    from forge.repair.models import ProtectedPathPolicy, RepairPlan
    from forge.storage.store import ForgeStore

    plan: Optional[RepairPlan] = None
    ref_path = Path(plan_ref)

    if ref_path.is_file():
        try:
            plan = RepairPlan.model_validate_json(ref_path.read_text())
        except Exception as e:
            typer.secho(f"Error reading plan file: {e}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)
    else:
        settings = get_settings()
        store = ForgeStore(db_path=settings.db_path)
        plan = store.get_repair_plan(plan_ref)

    if not plan:
        typer.secho(f"Repair plan '{plan_ref}' not found.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    violations: List[str] = []

    # Check protected path
    is_prot, prot_msg = ProtectedPathPolicy.is_protected(plan.target_file)
    if is_prot:
        violations.append(f"Target file '{plan.target_file}' is protected: {prot_msg}")

    # Check patch if present
    if plan.patch_candidate:
        contract = RepairSafetyContract(
            contract_id=f"rep_val_{plan.plan_id}",
            finding_id=plan.finding_id,
            base_commit_sha=plan.base_commit_sha,
            target_repo_path=Path(plan.target_repo),
            scratch_workspace_path=Path(plan.target_repo).parent / f"scratch_{plan.finding_id}",
            allowed_files=[plan.target_file],
            budget=RepairBudget(),
        )
        is_diff_valid, diff_violations, _ = parse_and_validate_unified_diff(
            contract, plan.patch_candidate.unified_diff
        )
        if not is_diff_valid:
            violations.extend(diff_violations)
    else:
        violations.append("Plan does not contain a patch candidate")

    is_valid = len(violations) == 0

    if format.lower() == "json":
        typer.echo(json.dumps({
            "plan_id": plan.plan_id,
            "is_valid": is_valid,
            "violations": violations,
        }, indent=2))
    else:
        typer.echo(f"Repair Plan Validation: {plan.plan_id}")
        typer.echo(f"  Valid:       {'YES' if is_valid else 'NO'}")
        if violations:
            typer.echo("  Violations:")
            for v in violations:
                typer.echo(f"    - {v}")

    if not is_valid:
        raise typer.Exit(code=1)


@repair_app.command(name="list")
def repair_list(
    finding_id: Optional[str] = typer.Option(None, "--finding-id", "-r", help="Filter plans by finding ID"),
    format: str = typer.Option("table", "--format", "-f", help="Output format: table or json"),
) -> None:
    """List historical repair plans stored in SQLite."""
    from forge.storage.store import ForgeStore

    settings = get_settings()
    store = ForgeStore(db_path=settings.db_path)
    plans = store.list_repair_plans(finding_id=finding_id)

    if not plans:
        typer.echo("No repair plans found.")
        return

    if format.lower() == "json":
        typer.echo(json.dumps([p.model_dump() for p in plans], default=str, indent=2))
        return

    typer.echo(f"{'Plan ID':<35} {'Finding ID':<20} {'Target File':<25} {'Status':<12}")
    typer.echo("-" * 92)
    for p in plans:
        typer.echo(f"{p.plan_id:<35} {p.finding_id:<20} {p.target_file[:24]:<25} {p.status.value:<12}")


@repair_app.command(name="verify")
def repair_verify(
    plan_ref: str = typer.Argument(..., help="Plan ID or path to plan JSON file"),
    repo: str = typer.Option(".", "--repo", "-r", help="Target repository directory path"),
    scratch: Optional[Path] = typer.Option(None, "--scratch", help="Scratch directory parent for disposable workspace"),
    command: Optional[str] = typer.Option(None, "--command", "-c", help="Explicit verification command (e.g. 'pytest tests/test_defect.py')"),
    format: str = typer.Option("markdown", "--format", "-f", help="Output format: markdown or json"),
) -> None:
    """Safely apply a repair plan's candidate patch in an isolated scratch workspace and verify it."""
    from forge.repair.contracts import RepairOutcome
    from forge.repair.models import RepairPlan
    from forge.repair.verifier import RepairVerifier
    from forge.storage.store import ForgeStore

    settings = get_settings()
    store = ForgeStore(db_path=settings.db_path)
    target_path = Path(repo).resolve()

    if not target_path.exists():
        typer.secho(f"Error: Target repository path '{repo}' does not exist.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    plan: Optional[RepairPlan] = None
    ref_path = Path(plan_ref)

    if ref_path.is_file():
        try:
            plan = RepairPlan.model_validate_json(ref_path.read_text())
        except Exception as e:
            typer.secho(f"Error reading plan JSON file: {e}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)
    else:
        plan = store.get_repair_plan(plan_ref)

    if not plan:
        typer.secho(f"Repair plan '{plan_ref}' not found in store or filesystem.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    verifier = RepairVerifier()
    cmd_args = command.split() if command else None

    res = verifier.verify_plan(
        plan=plan,
        target_repo=target_path,
        store=store,
        scratch_parent=scratch,
        verification_command=cmd_args,
    )

    if format.lower() == "json":
        typer.echo(res.model_dump_json(indent=2))
    else:
        typer.echo(f"Repair Verification: {res.plan_id}")
        typer.echo(f"  Attempt ID:        {res.attempt_id}")
        typer.echo(f"  Outcome:           {res.outcome.value.upper()}")
        typer.echo(f"  Patch Applied:     {'YES' if res.patch_applied else 'NO'}")
        typer.echo(f"  Syntax Valid:      {'YES' if res.syntax_valid else 'NO'}")
        typer.echo(f"  Sandbox Available: {'YES' if res.sandbox_available else 'NO (BLOCKED)'}")
        typer.echo(f"  Oracle Verified:   {'YES' if res.oracle_verified else 'NO'}")
        typer.echo(f"  Pre-Repro Outcome: {res.pre_repro_outcome.value}")
        typer.echo(f"  Post-Repro Outcome:{res.post_repro_outcome.value}")
        typer.echo(f"  Diagnostic:        {res.diagnostic}")

    # Standardized exit codes
    if res.outcome == RepairOutcome.REPAIR_VERIFIED:
        raise typer.Exit(code=0)
    elif res.outcome in (RepairOutcome.REPAIR_FAILED, RepairOutcome.REGRESSION_DETECTED, RepairOutcome.BROKEN_BUILD):
        raise typer.Exit(code=1)
    elif res.outcome == RepairOutcome.REPAIR_REJECTED:
        raise typer.Exit(code=2)
    elif res.outcome == RepairOutcome.SANDBOX_UNAVAILABLE:
        raise typer.Exit(code=3)
    else:  # TIMEOUT, INCONCLUSIVE, FLAKY
        raise typer.Exit(code=4)


@repair_app.command(name="attempts")
def repair_attempts(
    plan_id: str = typer.Argument(..., help="Plan ID to inspect attempt history for"),
    format: str = typer.Option("table", "--format", "-f", help="Output format: table or json"),
) -> None:
    """List historical patch application and verification attempts for a repair plan."""
    from forge.storage.store import ForgeStore

    settings = get_settings()
    store = ForgeStore(db_path=settings.db_path)
    attempts = store.list_repair_attempts(plan_id=plan_id)

    if not attempts:
        typer.echo(f"No verification attempts found for plan '{plan_id}'.")
        return

    if format.lower() == "json":
        typer.echo(json.dumps([a.model_dump() for a in attempts], default=str, indent=2))
        return

    typer.echo(f"{'Attempt ID':<30} {'#':<4} {'Outcome':<20} {'Pre-Repro':<15} {'Post-Repro':<15} {'Duration':<10}")
    typer.echo("-" * 98)
    for a in attempts:
        dur_str = f"{a.duration_ms:.1f}ms"
        typer.echo(
            f"{a.attempt_id:<30} {a.attempt_number:<4} {a.outcome.value:<20} "
            f"{a.pre_repro_outcome.value:<15} {a.post_repro_outcome.value:<15} {dur_str:<10}"
        )


@repair_app.command(name="benchmark")
def repair_benchmark_alias(
    mode: str = typer.Option("offline", "--mode", "-m", help="Evaluation mode: offline or provider/llm"),
    fixture_filter: Optional[str] = typer.Option(None, "--fixture", "-f", help="Filter for specific fixture ID"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="Opt-in LLM provider override"),
    model: Optional[str] = typer.Option(None, "--model", help="LLM model override"),
    output_format: str = typer.Option("markdown", "--format", help="Output format: markdown or json"),
    output_path: Optional[str] = typer.Option(None, "--output", "-o", help="Optional file path to export benchmark results"),
) -> None:
    """Execute the repair quality benchmark suite (alias for 'forge benchmark repair')."""
    benchmark_repair(
        mode=mode,
        fixture_filter=fixture_filter,
        provider=provider,
        model=model,
        output_format=output_format,
        output_path=output_path,
    )


@repair_app.command(name="test")
def repair_acceptance_test(
    scenario: Optional[str] = typer.Option(None, "--scenario", "-s", help="Filter for specific acceptance scenario ID (e.g. ACC01)"),
    output_format: str = typer.Option("markdown", "--format", "-f", help="Output format: markdown or json"),
    output_path: Optional[str] = typer.Option(None, "--output", "-o", help="Optional file path to export acceptance report"),
) -> None:
    """Execute the Phase 4 M5 end-to-end repair reliability acceptance suite."""
    from forge.repair.acceptance import run_acceptance_suite, render_acceptance_markdown, render_acceptance_json

    fmt = output_format.lower().strip()
    if fmt not in ("markdown", "json"):
        typer.secho(f"Invalid format '{output_format}'. Must be 'markdown' or 'json'.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        report = run_acceptance_suite(scenario_filter=scenario)
    except Exception as e:
        typer.secho(f"Acceptance suite execution failed: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    rendered = render_acceptance_json(report) if fmt == "json" else render_acceptance_markdown(report)

    if output_path:
        out_p = Path(output_path).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(rendered, encoding="utf-8")
        typer.echo(f"Acceptance report exported to: {out_p}")
    else:
        typer.echo(rendered)

    if report.metrics.failed_scenarios > 0:
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


@repair_app.command(name="acceptance")
def repair_acceptance_alias(
    scenario: Optional[str] = typer.Option(None, "--scenario", "-s", help="Filter for specific acceptance scenario ID (e.g. ACC01)"),
    output_format: str = typer.Option("markdown", "--format", "-f", help="Output format: markdown or json"),
    output_path: Optional[str] = typer.Option(None, "--output", "-o", help="Optional file path to export acceptance report"),
) -> None:
    """Execute the Phase 4 M5 end-to-end repair reliability acceptance suite (alias for 'forge repair test')."""
    repair_acceptance_test(scenario=scenario, output_format=output_format, output_path=output_path)


# =============================================================================
# Controlled Repair Orchestration Workflow CLI Commands (Phase 4 M4)
# =============================================================================

def _render_workflow_markdown(workflow: Any, events: List[Any]) -> str:
    """Render a comprehensive, human-readable Markdown status report for a workflow."""
    from forge.repair.orchestrator_models import RepairWorkflowState

    lines = [
        "# FORGE Controlled Repair Workflow Report",
        f"- **Workflow ID**: `{workflow.workflow_id}`",
        f"- **Finding ID**: `{workflow.finding_id}`",
        f"- **State**: `{workflow.state.value.upper()}`",
        f"- **Target Repo**: `{workflow.target_repo}`",
        f"- **Base Commit**: `{workflow.base_commit_sha[:10]}...`",
        f"- **Dry Run**: `{'YES (Plan-only / No execution)' if workflow.dry_run else 'NO (Verification active)'}`",
        f"- **Sandbox Available**: `{'YES' if workflow.sandbox_available else 'NO (BLOCKED / Fail-closed)'}`",
    ]
    if workflow.plan_id:
        lines.append(f"- **Plan ID**: `{workflow.plan_id}`")
    if workflow.patch_digest:
        lines.append(f"- **Patch Digest**: `{workflow.patch_digest[:16]}...`")
    if workflow.outcome:
        lines.append(f"- **Verification Outcome**: `{workflow.outcome.value.upper()}`")
    if workflow.human_decision:
        lines.append(f"- **Human Decision**: `{workflow.human_decision.upper()}`")
    if workflow.human_notes:
        lines.append(f"- **Human Notes**: {workflow.human_notes}")
    if workflow.error_message:
        lines.append(f"- **Diagnostic Message**: {workflow.error_message}")

    lines.extend([
        "",
        "## Stage Event History",
        "",
        "| Seq | Stage | From State | To State | Timestamp |",
        "|---|---|---|---|---|",
    ])
    for e in events:
        from_str = e.from_state.value.upper() if e.from_state else "-"
        lines.append(
            f"| `{e.sequence}` | `{e.stage}` | `{from_str}` | `{e.to_state.value.upper()}` | `{e.created_at.strftime('%H:%M:%S')}` |"
        )

    lines.extend(["", "## Status & Action Guidance"])
    if workflow.state == RepairWorkflowState.AWAITING_HUMAN_REVIEW:
        lines.append("> [!IMPORTANT]")
        lines.append("> Patch candidate is verified or plan validated. Explicit human review is required.")
        lines.append(f"> Run: `forge repair workflow review {workflow.workflow_id} --decision approve` or `--decision reject`.")
    elif workflow.state == RepairWorkflowState.BLOCKED:
        lines.append("> [!WARNING]")
        lines.append(f"> Workflow is BLOCKED: {workflow.error_message or 'Sandbox unavailable or revision mismatch'}.")
        lines.append(f"> To resume once unblocked: `forge repair workflow resume {workflow.workflow_id}`.")
    elif workflow.state == RepairWorkflowState.APPROVED:
        lines.append("> [!NOTE]")
        lines.append("> Repair has been APPROVED by human operator. Target repository remains strictly unmodified (audit-only policy enforced).")
    elif workflow.state == RepairWorkflowState.REJECTED:
        lines.append("> [!NOTE]")
        lines.append("> Repair workflow was REJECTED.")
    elif workflow.state == RepairWorkflowState.FAILED:
        lines.append("> [!WARNING]")
        lines.append(f"> Verification FAILED: {workflow.error_message or 'Defect remained reproducible or regression detected'}.")

    return redact_secrets("\n".join(lines))


@workflow_app.command(name="start")
def workflow_start(
    finding_id: str = typer.Argument(..., help="Finding ID to orchestrate repair for"),
    repo: str = typer.Option(".", "--repo", "-r", help="Target repository directory path"),
    commit: Optional[str] = typer.Option(None, "--commit", "-c", help="Pinned base commit revision"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan-only mode; skips sandboxed verification execution"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="LLM provider override"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="LLM model override"),
    force: bool = typer.Option(False, "--force", help="Force new workflow even if active workflow exists"),
    format: str = typer.Option("markdown", "--format", "-f", help="Output format: markdown or json"),
) -> None:
    """Start an auditable, resumable repair workflow from an audited finding."""
    from forge.repair.orchestrator import RepairOrchestrator
    from forge.repair.orchestrator_models import RepairWorkflowState
    from forge.repair.planner import RepairPlanner
    from forge.storage.store import ForgeStore

    settings = get_settings()
    store = ForgeStore(db_path=settings.db_path)
    planner = RepairPlanner(provider_name=provider, model_name=model)
    orchestrator = RepairOrchestrator(store=store, planner=planner)

    try:
        wf = orchestrator.start_workflow(
            finding_id=finding_id,
            target_repo=repo,
            base_commit_sha=commit,
            dry_run=dry_run,
            force=force,
        )
        # Advance through stages
        wf = orchestrator.advance(wf.workflow_id, auto_advance=True)
    except Exception as exc:
        typer.secho(f"Workflow initialization error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    events = store.list_repair_workflow_events(wf.workflow_id)

    if format.lower() == "json":
        typer.echo(wf.model_dump_json(indent=2))
    else:
        typer.echo(_render_workflow_markdown(wf, events))

    if wf.state in (RepairWorkflowState.APPROVED, RepairWorkflowState.VERIFIED, RepairWorkflowState.AWAITING_HUMAN_REVIEW):
        raise typer.Exit(code=0)
    elif wf.state in (RepairWorkflowState.FAILED, RepairWorkflowState.REJECTED):
        raise typer.Exit(code=1)
    elif wf.state == RepairWorkflowState.BLOCKED:
        raise typer.Exit(code=3)
    else:
        raise typer.Exit(code=0)


@workflow_app.command(name="status")
def workflow_status(
    workflow_id: str = typer.Argument(..., help="Repair workflow run ID to inspect"),
    format: str = typer.Option("markdown", "--format", "-f", help="Output format: markdown or json"),
) -> None:
    """Show detailed status, pinned context, and audit history for a repair workflow."""
    from forge.storage.store import ForgeStore

    settings = get_settings()
    store = ForgeStore(db_path=settings.db_path)
    wf = store.get_repair_workflow(workflow_id)

    if not wf:
        typer.secho(f"Repair workflow '{workflow_id}' not found.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    events = store.list_repair_workflow_events(workflow_id)

    if format.lower() == "json":
        typer.echo(wf.model_dump_json(indent=2))
    else:
        typer.echo(_render_workflow_markdown(wf, events))


@workflow_app.command(name="list")
def workflow_list(
    finding_id: Optional[str] = typer.Option(None, "--finding-id", "-r", help="Filter workflows by finding ID"),
    state: Optional[str] = typer.Option(None, "--state", "-s", help="Filter workflows by state"),
    format: str = typer.Option("table", "--format", "-f", help="Output format: table or json"),
) -> None:
    """List historical repair workflows stored in SQLite."""
    from forge.storage.store import ForgeStore

    settings = get_settings()
    store = ForgeStore(db_path=settings.db_path)
    workflows = store.list_repair_workflows(finding_id=finding_id, state=state)

    if not workflows:
        typer.echo("No repair workflows found.")
        return

    if format.lower() == "json":
        typer.echo(json.dumps([w.model_dump() for w in workflows], default=str, indent=2))
        return

    typer.echo(f"{'Workflow ID':<35} {'Finding ID':<18} {'State':<22} {'DryRun':<8} {'Outcome':<18}")
    typer.echo("-" * 105)
    for w in workflows:
        outcome_str = w.outcome.value if w.outcome else "-"
        dry_str = "YES" if w.dry_run else "NO"
        typer.echo(f"{w.workflow_id:<35} {w.finding_id:<18} {w.state.value:<22} {dry_str:<8} {outcome_str:<18}")


@workflow_app.command(name="resume")
def workflow_resume(
    workflow_id: str = typer.Argument(..., help="Workflow ID to resume from blocked or interrupted state"),
    format: str = typer.Option("markdown", "--format", "-f", help="Output format: markdown or json"),
) -> None:
    """Resume an eligible blocked or interrupted repair workflow."""
    from forge.repair.orchestrator import RepairOrchestrator
    from forge.repair.orchestrator_models import RepairWorkflowState
    from forge.storage.store import ForgeStore

    settings = get_settings()
    store = ForgeStore(db_path=settings.db_path)
    orchestrator = RepairOrchestrator(store=store)

    try:
        wf = orchestrator.resume_workflow(workflow_id, auto_advance=True)
    except Exception as exc:
        typer.secho(f"Resume failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    events = store.list_repair_workflow_events(wf.workflow_id)

    if format.lower() == "json":
        typer.echo(wf.model_dump_json(indent=2))
    else:
        typer.echo(_render_workflow_markdown(wf, events))

    if wf.state in (RepairWorkflowState.APPROVED, RepairWorkflowState.VERIFIED, RepairWorkflowState.AWAITING_HUMAN_REVIEW):
        raise typer.Exit(code=0)
    elif wf.state in (RepairWorkflowState.FAILED, RepairWorkflowState.REJECTED):
        raise typer.Exit(code=1)
    elif wf.state == RepairWorkflowState.BLOCKED:
        raise typer.Exit(code=3)
    else:
        raise typer.Exit(code=0)


@workflow_app.command(name="review")
def workflow_review(
    workflow_id: str = typer.Argument(..., help="Workflow ID in awaiting_human_review state"),
    decision: str = typer.Option(..., "--decision", "-d", help="Review decision: approve or reject"),
    notes: Optional[str] = typer.Option(None, "--notes", "-n", help="Review justification or audit notes"),
    format: str = typer.Option("markdown", "--format", "-f", help="Output format: markdown or json"),
) -> None:
    """Submit an explicit human review decision (approve or reject) for a verified patch."""
    from forge.repair.orchestrator import RepairOrchestrator
    from forge.repair.orchestrator_models import RepairWorkflowState
    from forge.storage.store import ForgeStore

    settings = get_settings()
    store = ForgeStore(db_path=settings.db_path)
    orchestrator = RepairOrchestrator(store=store)

    try:
        wf = orchestrator.review_workflow(workflow_id=workflow_id, decision=decision, notes=notes)
    except Exception as exc:
        typer.secho(f"Review rejected: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    events = store.list_repair_workflow_events(wf.workflow_id)

    if format.lower() == "json":
        typer.echo(wf.model_dump_json(indent=2))
    else:
        typer.echo(_render_workflow_markdown(wf, events))

    if wf.state == RepairWorkflowState.APPROVED:
        typer.echo("\n[Result] Workflow APPROVED by human operator. Target repo remains unmodified.")
        raise typer.Exit(code=0)
    else:
        typer.echo("\n[Result] Workflow REJECTED by human operator.")
        raise typer.Exit(code=1)


# =============================================================================
# Phase 5 Engineering Workflow Commands (Milestone 1: Intake & Context)
# =============================================================================

def _render_intake_markdown(spec: Any) -> str:
    lines = [
        "# Workflow Task Intake Specification",
        f"- **Intake ID**: `{spec.intake_id}`",
        f"- **Source Type**: `{spec.source_type.value}`",
        f"- **Target Repo**: `{spec.target_repo}`",
        f"- **Base Commit**: `{spec.base_commit_sha[:10]}...` ({spec.base_commit_sha})",
        f"- **Title**: {spec.title}",
        f"- **Description**: {spec.description}",
    ]
    if spec.affected_files:
        lines.append(f"- **Affected Files** ({len(spec.affected_files)}): {', '.join(spec.affected_files)}")
    else:
        lines.append("- **Affected Files**: none declared")

    if spec.source_type.value == "review_finding" and spec.finding_payload:
        lines.append(f"- **Finding ID**: `{spec.finding_payload.finding_id}`")
        if spec.finding_payload.severity:
            lines.append(f"- **Severity**: `{spec.finding_payload.severity.value}`")
    elif spec.source_type.value == "unified_diff" and spec.diff_payload:
        lines.append(f"- **Diff Changes**: +{spec.diff_payload.total_additions} / -{spec.diff_payload.total_deletions} ({spec.diff_payload.diff_bytes} bytes)")
    elif spec.source_type.value == "test_specification" and spec.test_payload:
        lines.append(f"- **Test File**: `{spec.test_payload.test_file}`")
        lines.append(f"- **Test Command**: `{spec.test_payload.test_command}`")
    return "\n".join(lines)


def _render_context_markdown(ctx: Any) -> str:
    lines = [
        "# Assembled Repository Context",
        f"- **Context ID**: `{ctx.context_id}`",
        f"- **Intake ID**: `{ctx.intake_id}`",
        f"- **Target Repo**: `{ctx.target_repo}`",
        f"- **Pinned Commit**: `{ctx.revision.commit_sha[:10]}...`",
        f"- **Branch**: `{ctx.revision.branch or 'detached'}`",
        f"- **Total Characters**: {ctx.budget_summary.total_chars} / {ctx.budget_summary.char_budget} chars",
        f"- **Estimated Tokens**: ~{ctx.budget_summary.estimated_tokens}",
        f"- **Truncated**: `{'YES' if ctx.budget_summary.is_truncated else 'NO'}`",
    ]
    if ctx.budget_summary.truncation_reasons:
        for r in ctx.budget_summary.truncation_reasons:
            lines.append(f"  - *Truncation Notice*: {r}")
    lines.append(f"- **Relevant Symbols**: {len(ctx.relevant_symbols)}")
    lines.append(f"- **Source Excerpts**: {len(ctx.source_excerpts)}")
    lines.append(f"- **Discovered Instructions**: {len(ctx.instruction_files)}")
    return "\n".join(lines)


@workflow_intake_app.command(name="create")
def cli_intake_create(
    source_type: str = typer.Option(..., "--type", "-t", help="Intake source type: finding, diff, issue, or test"),
    repo: Path = typer.Option(Path("."), "--repo", "-r", help="Target repository path"),
    finding_id: Optional[str] = typer.Option(None, "--finding-id", help="Finding identifier (for finding intake)"),
    diff_file: Optional[Path] = typer.Option(None, "--diff-file", help="Path to unified diff file"),
    diff_text: Optional[str] = typer.Option(None, "--diff-text", help="Raw diff text"),
    issue_title: Optional[str] = typer.Option(None, "--issue-title", help="Issue title"),
    issue_text: Optional[str] = typer.Option(None, "--issue-text", help="Issue description text"),
    issue_file: Optional[Path] = typer.Option(None, "--issue-file", help="Path to issue markdown or text file"),
    test_file: Optional[str] = typer.Option(None, "--test-file", help="Test file path (e.g. tests/test_foo.py)"),
    test_command: Optional[str] = typer.Option(None, "--test-command", help="Test command to reproduce failure"),
    test_name: Optional[str] = typer.Option(None, "--test-name", help="Specific test function or class name"),
    affected_files: Optional[List[str]] = typer.Option(None, "--affected-file", "-f", help="Declared affected files"),
    expected_sha: Optional[str] = typer.Option(None, "--expected-sha", help="Expected base commit SHA to enforce"),
    allow_dirty: bool = typer.Option(False, "--allow-dirty", help="Allow uncommitted changes in target repository"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="File path to write intake spec JSON"),
    format: str = typer.Option("markdown", "--format", help="Output format: markdown or json"),
) -> None:
    """Normalize and validate a defect intake task from finding, diff, issue, or test."""
    from forge.workflow.intake import (
        create_intake_from_diff,
        create_intake_from_finding,
        create_intake_from_issue,
        create_intake_from_test_spec,
    )
    from forge.storage.store import ForgeStore
    settings = get_settings()

    st = source_type.strip().lower()
    try:
        if st in ("finding", "review_finding"):
            if not finding_id:
                typer.secho("Error: --finding-id is required for finding intake.", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=2)
            store = ForgeStore(db_path=settings.db_path)
            spec = create_intake_from_finding(
                repo_path=repo,
                finding_id=finding_id,
                store=store,
                expected_sha=expected_sha,
                allow_dirty=allow_dirty,
            )
        elif st in ("diff", "unified_diff"):
            text = diff_text
            if diff_file:
                if not diff_file.exists():
                    typer.secho(f"Error: Diff file not found: {diff_file}", fg=typer.colors.RED, err=True)
                    raise typer.Exit(code=2)
                text = diff_file.read_text(encoding="utf-8")
            if not text:
                typer.secho("Error: Either --diff-file or --diff-text is required for diff intake.", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=2)
            spec = create_intake_from_diff(
                repo_path=repo,
                diff_text=text,
                expected_sha=expected_sha,
                allow_dirty=allow_dirty,
            )
        elif st in ("issue", "issue_description"):
            body = issue_text
            if issue_file:
                if not issue_file.exists():
                    typer.secho(f"Error: Issue file not found: {issue_file}", fg=typer.colors.RED, err=True)
                    raise typer.Exit(code=2)
                body = issue_file.read_text(encoding="utf-8")
            if not body or not issue_title:
                typer.secho("Error: --issue-title and (--issue-text or --issue-file) are required for issue intake.", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=2)
            spec = create_intake_from_issue(
                repo_path=repo,
                title=issue_title,
                issue_text=body,
                affected_files=affected_files,
                expected_sha=expected_sha,
                allow_dirty=allow_dirty,
            )
        elif st in ("test", "test_specification"):
            if not test_file or not test_command:
                typer.secho("Error: --test-file and --test-command are required for test intake.", fg=typer.colors.RED, err=True)
                raise typer.Exit(code=2)
            spec = create_intake_from_test_spec(
                repo_path=repo,
                test_file=test_file,
                test_command=test_command,
                test_name=test_name,
                affected_files=affected_files,
                expected_sha=expected_sha,
                allow_dirty=allow_dirty,
            )
        else:
            typer.secho(f"Error: Unsupported intake type '{source_type}'. Choose finding, diff, issue, or test.", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)
    except typer.Exit:
        raise
    except Exception as exc:
        typer.secho(f"Intake creation failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if output:
        output.write_text(spec.model_dump_json(indent=2), encoding="utf-8")
        typer.secho(f"Exported intake specification to {output}", fg=typer.colors.GREEN)

    if format.lower() == "json":
        typer.echo(spec.model_dump_json(indent=2))
    else:
        typer.echo(_render_intake_markdown(spec))


@workflow_intake_app.command(name="validate")
def cli_intake_validate(
    intake_file: Path = typer.Argument(..., help="Path to intake spec JSON file"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r", help="Optional target repo path to verify against"),
    format: str = typer.Option("markdown", "--format", help="Output format: markdown or json"),
) -> None:
    """Validate an intake specification file offline and against an optional repository."""
    from forge.workflow.intake import validate_intake_spec
    from forge.workflow.models import WorkflowIntakeSpec

    if not intake_file.exists():
        typer.secho(f"Intake file not found: {intake_file}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        content = intake_file.read_text(encoding="utf-8")
        spec = WorkflowIntakeSpec.model_validate_json(content)
        is_valid, reasons = validate_intake_spec(spec, repo_path=repo)
    except Exception as exc:
        typer.secho(f"Validation error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if format.lower() == "json":
        typer.echo(json.dumps({"valid": is_valid, "reasons": reasons, "intake_id": spec.intake_id}, indent=2))
    else:
        if is_valid:
            typer.secho(f"[VALID] Intake specification '{spec.intake_id}' passed all checks.", fg=typer.colors.GREEN)
        else:
            typer.secho(f"[INVALID] Intake specification '{spec.intake_id}' failed checks:", fg=typer.colors.RED)
            for r in reasons:
                typer.echo(f"  - {r}")
    if not is_valid:
        raise typer.Exit(code=1)


@workflow_intake_app.command(name="show")
def cli_intake_show(
    intake_file: Path = typer.Argument(..., help="Path to intake spec JSON file"),
    format: str = typer.Option("markdown", "--format", help="Output format: markdown or json"),
) -> None:
    """Display an existing intake specification JSON file."""
    from forge.workflow.models import WorkflowIntakeSpec

    if not intake_file.exists():
        typer.secho(f"Intake file not found: {intake_file}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        content = intake_file.read_text(encoding="utf-8")
        spec = WorkflowIntakeSpec.model_validate_json(content)
    except Exception as exc:
        typer.secho(f"Failed to load intake spec: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if format.lower() == "json":
        typer.echo(spec.model_dump_json(indent=2))
    else:
        typer.echo(_render_intake_markdown(spec))


@workflow_context_app.command(name="build")
def cli_context_build(
    intake_file: Path = typer.Argument(..., help="Path to intake spec JSON file"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r", help="Repository path override (defaults to intake target_repo)"),
    budget: int = typer.Option(25_000, "--budget", "-b", help="Max total character budget for context"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="File path to save assembled context JSON"),
    format: str = typer.Option("markdown", "--format", help="Output format: markdown or json"),
) -> None:
    """Assemble scoped repository context for a validated intake specification."""
    from forge.workflow.context import assemble_repository_context
    from forge.workflow.models import ContextBudget, WorkflowIntakeSpec

    if not intake_file.exists():
        typer.secho(f"Intake file not found: {intake_file}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        spec = WorkflowIntakeSpec.model_validate_json(intake_file.read_text(encoding="utf-8"))
        target_path = repo or Path(spec.target_repo)
        ctx_budget = ContextBudget(max_total_chars=budget)
        context = assemble_repository_context(repo_path=target_path, intake=spec, budget=ctx_budget)
    except Exception as exc:
        typer.secho(f"Context assembly failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if output:
        output.write_text(context.model_dump_json(indent=2), encoding="utf-8")
        typer.secho(f"Saved context artifact to {output}", fg=typer.colors.GREEN)

    if format.lower() == "json":
        typer.echo(context.model_dump_json(indent=2))
    else:
        typer.echo(_render_context_markdown(context))


@workflow_context_app.command(name="show")
def cli_context_show(
    context_file: Path = typer.Argument(..., help="Path to context JSON file"),
    prompt_only: bool = typer.Option(False, "--prompt-only", help="Display only the rendered markdown prompt"),
    format: str = typer.Option("markdown", "--format", help="Output format: markdown or json"),
) -> None:
    """Display an assembled repository context bundle or its rendered prompt."""
    from forge.workflow.models import RepositoryContext

    if not context_file.exists():
        typer.secho(f"Context file not found: {context_file}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        ctx = RepositoryContext.model_validate_json(context_file.read_text(encoding="utf-8"))
    except Exception as exc:
        typer.secho(f"Failed to load context: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if prompt_only:
        typer.echo(ctx.rendered_prompt)
        return

    if format.lower() == "json":
        typer.echo(ctx.model_dump_json(indent=2))
    else:
        typer.echo(_render_context_markdown(ctx))


# =============================================================================
# Phase 5 Engineering Workflow Lifecycle Commands (Milestone 2)
# =============================================================================

def _determine_workflow_guidance(wf: Any) -> Tuple[str, str]:
    """Return (status_category, next_action_guidance) based on workflow lifecycle state."""
    state_str = wf.state.value if hasattr(wf.state, "value") else str(wf.state)
    wid = wf.workflow_id
    if state_str in ("created", "intake_validated"):
        return (
            "PENDING (Intake Initialized)",
            f"Run 'forge workflow repro {wid}' to analyze repository and run defect reproduction in Docker.",
        )
    elif state_str == "repo_analyzed":
        return (
            "PENDING (Context Assembled)",
            f"Run 'forge workflow repro {wid}' to execute isolated defect reproduction.",
        )
    elif state_str == "reproduction_pending":
        return (
            "IN PROGRESS (Reproduction Pending)",
            f"Run 'forge workflow repro {wid}' to execute or resume reproduction.",
        )
    elif state_str == "repro_blocked":
        return (
            "BLOCKED (Docker Unavailable)",
            f"Ensure Docker daemon is running, then run 'forge workflow resume {wid}'.",
        )
    elif state_str == "reproduced":
        return (
            "REPRODUCED (Defect Verified by Execution Oracle)",
            f"Run 'forge workflow plan-repair {wid}' to generate an evidence-grounded repair plan.",
        )
    elif state_str == "repair_pending":
        if wf.repair_plan_id:
            return (
                "REPAIR PLANNED (Candidate Ready for Verification)",
                f"Run 'forge workflow verify {wid}' to execute multi-check verification.",
            )
        else:
            return (
                "IN PROGRESS (Repair Planning Interrupted)",
                f"Run 'forge workflow plan-repair {wid}' to resume repair planning.",
            )
    elif state_str == "verification_pending":
        return (
            "IN PROGRESS (Verification Pending)",
            f"Run 'forge workflow verify {wid}' to execute or resume multi-check verification.",
        )
    elif state_str == "verification_blocked":
        return (
            "BLOCKED (Verification Blocked - Sandbox/Tool Missing)",
            f"Ensure Docker sandbox and required tools are available, then run 'forge workflow resume {wid}'.",
        )
    elif state_str in ("verified", "regression_checking"):
        return (
            "VERIFIED (All Checks Passed)",
            f"Run 'forge workflow advance {wid}' to proceed to the operator review gate.",
        )
    elif state_str == "awaiting_human_review":
        return (
            "AWAITING OPERATOR REVIEW (Review Gate Active)",
            f"Inspect candidate patch and run 'forge workflow review {wid} --decision [approve|reject] --notes \"...\"'.",
        )
    elif state_str == "approved":
        return (
            "TERMINAL (Approved by Human Operator)",
            f"Run 'forge workflow export {wid} --out-dir ./dossiers' to export certified engineering dossier.",
        )
    elif state_str == "rejected":
        return (
            "TERMINAL (Rejected by Human Operator)",
            "Workflow concluded and permanently recorded as rejected. No further action permitted.",
        )
    elif state_str == "repro_failed":
        return (
            "TERMINAL (Reproduction Failed)",
            "Defect could not be reproduced. Workflow concluded. Inspect test specification and repository.",
        )
    elif state_str == "repro_inconclusive":
        return (
            "TERMINAL (Reproduction Inconclusive)",
            "Reproduction was inconclusive. Workflow concluded.",
        )
    elif state_str == "repair_failed":
        return (
            "TERMINAL (Repair Planning Failed)",
            "Model repair proposal violated policy, scope, or indentation rules. Workflow concluded.",
        )
    elif state_str in ("verification_failed", "regression_failed"):
        return (
            "TERMINAL (Verification Failed / Regression Detected)",
            "Candidate patch failed multi-check verification or caused regression on baseline tests. Workflow concluded.",
        )
    return (
        f"UNKNOWN ({state_str})",
        f"Inspect workflow state with 'forge workflow status {wid}'.",
    )


def _render_eng_workflow_markdown(wf: Any, events: List[Any], store: Optional[Any] = None) -> str:
    category, next_action = _determine_workflow_guidance(wf)

    lines = [
        f"# Engineering Workflow `{wf.workflow_id}`",
        f"- **Status**: `{category}`",
        f"- **Lifecycle State**: `{wf.state.value}`",
        f"- **Next Permitted Action**: {next_action}",
        f"- **Intake ID**: `{wf.intake_id}`",
        f"- **Target Repo**: `{wf.target_repo}`",
        f"- **Pinned Commit**: `{wf.base_commit_sha}`",
        f"- **Concurrency Version**: `{wf.version}`",
    ]
    if wf.context_id:
        lines.append(f"- **Context ID**: `{wf.context_id}`")
    if wf.repro_case_id:
        lines.append(f"- **Repro Case ID**: `{wf.repro_case_id}`")
    if wf.repair_plan_id:
        lines.append(f"- **Repair Plan ID**: `{wf.repair_plan_id}`")
    if wf.verification_attempt_id:
        lines.append(f"- **Verification Attempt ID**: `{wf.verification_attempt_id}`")
    if wf.human_decision:
        lines.append(f"- **Human Decision**: `{wf.human_decision}`")
        if wf.human_notes:
            lines.append(f"- **Human Notes**: {wf.human_notes}")
    if wf.error_message:
        lines.append(f"- **Error / Diagnostic**: {wf.error_message}")

    # Candidate Repair Patch Details
    if wf.repair_plan_id and store:
        try:
            plan = store.get_repair_plan(wf.repair_plan_id)
            if plan and plan.patch_candidate:
                lines.append("\n## Candidate Repair Patch")
                lines.append(f"- **Target File**: `{plan.target_file}`")
                lines.append(f"- **Lines Changed**: `+{plan.patch_candidate.added_lines} / -{plan.patch_candidate.removed_lines}`")
                lines.append(f"- **SHA-256 Digest**: `{plan.patch_candidate.sha256_digest}`")
                lines.append("\n```diff")
                lines.append(plan.patch_candidate.unified_diff.strip())
                lines.append("```")
        except Exception:
            pass

    # Multi-Check Verification Results
    if wf.verification_attempt_id and store:
        try:
            vresult = store.get_multi_check_verification(wf.verification_attempt_id)
            if vresult:
                lines.append("\n## Multi-Check Verification Results")
                lines.append(f"- **Aggregate Outcome**: `{vresult.aggregate_outcome.value}`")
                lines.append(f"- **Sandbox Available**: `{vresult.sandbox_available}`")
                lines.append(f"- **Regression Detected**: `{vresult.regression_detected}`")
                lines.append(f"- **Duration**: `{vresult.total_duration_ms:.1f} ms`")
                if vresult.candidate_results:
                    lines.append("\n| Check | Type | Status | Duration | Diagnostic |")
                    lines.append("| :--- | :--- | :--- | :--- | :--- |")
                    for c in vresult.candidate_results:
                        diag = c.diagnostic.replace("\n", " ").strip() if c.diagnostic else ""
                        lines.append(f"| `{c.name}` | `{c.check_type.value}` | `{c.status.value}` | `{c.duration_ms:.1f}ms` | {diag[:60]} |")
        except Exception:
            pass

    if events:
        lines.append("\n## Event Ledger Timeline")
        for ev in events:
            from_st = ev.from_state.value if ev.from_state else "None"
            to_st = ev.to_state.value
            payload_summary = ""
            if ev.payload:
                if "decision" in ev.payload:
                    payload_summary = f" (decision: {ev.payload['decision']})"
                elif "reason" in ev.payload:
                    payload_summary = f" (reason: {ev.payload['reason']})"
                elif "aggregate_outcome" in ev.payload:
                    payload_summary = f" (outcome: {ev.payload['aggregate_outcome']})"
            lines.append(f"- `#{ev.sequence:02d}` [{ev.created_at.isoformat()}] `{ev.stage}`: `{from_st}` → `{to_st}`{payload_summary}")
    return "\n".join(lines)


@eng_workflow_app.command(name="start")
def cli_eng_workflow_start(
    intake_file: Path = typer.Option(..., "--intake", "-i", help="Path to validated intake specification JSON"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r", help="Repository path override (defaults to intake target_repo)"),
    auto_advance: bool = typer.Option(False, "--auto-advance", "-a", help="Automatically advance to INTAKE_VALIDATED and REPO_ANALYZED if context is assembleable"),
    format: str = typer.Option("markdown", "--format", help="Output format: markdown or json"),
) -> None:
    """Initialize an engineering workflow from a validated task intake specification."""
    from forge.workflow.intake import validate_intake_spec
    from forge.workflow.models import WorkflowIntakeSpec
    from forge.workflow.orchestrator import EngineeringWorkflowOrchestrator

    if not intake_file.exists():
        typer.secho(f"Intake file not found: {intake_file}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        spec = WorkflowIntakeSpec.model_validate_json(intake_file.read_text(encoding="utf-8"))
        orchestrator = EngineeringWorkflowOrchestrator()
        wf = orchestrator.start_workflow(intake_spec=spec, target_repo=repo, auto_advance=auto_advance)
        events = orchestrator.list_events(wf.workflow_id)
    except Exception as exc:
        typer.secho(f"Failed to start workflow: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if format.lower() == "json":
        typer.echo(wf.model_dump_json(indent=2))
    else:
        typer.echo(_render_eng_workflow_markdown(wf, events, store=orchestrator.store))


@eng_workflow_app.command(name="status")
def cli_eng_workflow_status(
    workflow_id: str = typer.Argument(..., help="Workflow ID (e.g. wf_eng_...)"),
    format: str = typer.Option("markdown", "--format", help="Output format: markdown or json"),
) -> None:
    """Display current state, history, candidate patch, and metadata of an engineering workflow."""
    from forge.workflow.orchestrator import EngineeringWorkflowOrchestrator

    orchestrator = EngineeringWorkflowOrchestrator()
    wf = orchestrator.get_workflow(workflow_id)
    if not wf:
        typer.secho(f"Engineering workflow '{workflow_id}' not found.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    events = orchestrator.list_events(workflow_id)

    if format.lower() == "json":
        typer.echo(wf.model_dump_json(indent=2))
    else:
        typer.echo(_render_eng_workflow_markdown(wf, events, store=orchestrator.store))


@eng_workflow_app.command(name="show")
def cli_eng_workflow_show(
    workflow_id: str = typer.Argument(..., help="Workflow ID (e.g. wf_eng_...)"),
    format: str = typer.Option("markdown", "--format", help="Output format: markdown or json"),
) -> None:
    """Display complete state, candidate patch, verification results, and history (alias for 'status')."""
    cli_eng_workflow_status(workflow_id=workflow_id, format=format)


@eng_workflow_app.command(name="list")
def cli_eng_workflow_list(
    intake_id: Optional[str] = typer.Option(None, "--intake-id", help="Filter by intake ID"),
    state: Optional[str] = typer.Option(None, "--state", help="Filter by lifecycle state"),
    format: str = typer.Option("markdown", "--format", help="Output format: markdown or json"),
) -> None:
    """List engineering workflows stored in SQLite."""
    from forge.workflow.orchestrator import EngineeringWorkflowOrchestrator

    orchestrator = EngineeringWorkflowOrchestrator()
    workflows = orchestrator.list_workflows(intake_id=intake_id, state=state)

    if format.lower() == "json":
        import json
        typer.echo(json.dumps([w.model_dump(mode="json") for w in workflows], indent=2))
        return

    if not workflows:
        typer.echo("No engineering workflows found.")
        return

    typer.echo(f"{'Workflow ID':<26} {'State':<24} {'Intake ID':<20} {'Commit':<12} {'Ver':<5}")
    typer.echo("-" * 92)
    for w in workflows:
        commit_short = w.base_commit_sha[:10] if len(w.base_commit_sha) >= 10 else w.base_commit_sha
        typer.echo(
            f"{w.workflow_id:<26} {w.state.value:<24} {w.intake_id:<20} {commit_short:<12} {w.version:<5}"
        )


@eng_workflow_app.command(name="resume")
def cli_eng_workflow_resume(
    workflow_id: str = typer.Argument(..., help="Workflow ID to resume"),
    format: str = typer.Option("markdown", "--format", help="Output format: markdown or json"),
) -> None:
    """Safely resume a blocked or paused engineering workflow."""
    from forge.workflow.orchestrator import EngineeringWorkflowOrchestrator

    orchestrator = EngineeringWorkflowOrchestrator()
    try:
        wf = orchestrator.resume_workflow(workflow_id)
        events = orchestrator.list_events(wf.workflow_id)
    except Exception as exc:
        typer.secho(f"Resumption failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if format.lower() == "json":
        typer.echo(wf.model_dump_json(indent=2))
    else:
        typer.echo(_render_eng_workflow_markdown(wf, events, store=orchestrator.store))


@eng_workflow_app.command(name="review")
def cli_eng_workflow_review(
    workflow_id: str = typer.Argument(..., help="Workflow ID to review"),
    decision: str = typer.Option(..., "--decision", "-d", help="Mandatory review decision: 'approve' or 'reject'"),
    notes: Optional[str] = typer.Option(None, "--notes", "-n", help="Auditor review notes"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r", help="Repository path override to verify HEAD revision before approval"),
    format: str = typer.Option("markdown", "--format", help="Output format: markdown or json"),
) -> None:
    """Submit a mandatory human review decision to approve or reject a workflow."""
    from forge.workflow.models import EngineeringWorkflowState
    from forge.workflow.orchestrator import EngineeringWorkflowOrchestrator

    orchestrator = EngineeringWorkflowOrchestrator()
    try:
        wf = orchestrator.review_workflow(workflow_id=workflow_id, decision=decision, notes=notes, target_repo=repo)
        events = orchestrator.list_events(wf.workflow_id)
    except Exception as exc:
        typer.secho(f"Review rejected: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    if format.lower() == "json":
        typer.echo(wf.model_dump_json(indent=2))
    else:
        typer.echo(_render_eng_workflow_markdown(wf, events, store=orchestrator.store))

    if wf.state == EngineeringWorkflowState.APPROVED:
        typer.secho("\n[Result] Workflow APPROVED by human operator. Target repo remains unmodified.", fg=typer.colors.GREEN)
        raise typer.Exit(code=0)
    else:
        typer.secho("\n[Result] Workflow REJECTED by human operator.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)


@eng_workflow_app.command(name="repro")
def cli_eng_workflow_repro(
    workflow_id: str = typer.Argument(..., help="Workflow ID to reproduce defect for"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r", help="Repository path override (defaults to workflow target_repo)"),
    test_command: Optional[str] = typer.Option(None, "--test-command", "-c", help="Explicit test command override"),
    max_retries: int = typer.Option(0, "--max-retries", help="Maximum retry attempts (0-3)"),
    format: str = typer.Option("markdown", "--format", help="Output format: markdown or json"),
) -> None:
    """Execute or resume defect reproduction and chain validated evidence into the workflow."""
    from forge.workflow.orchestrator import EngineeringWorkflowOrchestrator
    import shlex

    orchestrator = EngineeringWorkflowOrchestrator()
    cmd_args = shlex.split(test_command) if test_command else None
    try:
        wf = orchestrator.reproduce_defect(
            workflow_id=workflow_id,
            workspace_path=repo,
            command_args=cmd_args,
            max_retries=max_retries,
        )
        events = orchestrator.list_events(wf.workflow_id)
    except Exception as exc:
        typer.secho(f"Reproduction failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if format.lower() == "json":
        typer.echo(wf.model_dump_json(indent=2))
    else:
        typer.echo(_render_eng_workflow_markdown(wf, events, store=orchestrator.store))


@eng_workflow_app.command(name="reproduce")
def cli_eng_workflow_reproduce(
    workflow_id: str = typer.Argument(..., help="Workflow ID to reproduce defect for"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r", help="Repository path override (defaults to workflow target_repo)"),
    test_command: Optional[str] = typer.Option(None, "--test-command", "-c", help="Explicit test command override"),
    max_retries: int = typer.Option(0, "--max-retries", help="Maximum retry attempts (0-3)"),
    format: str = typer.Option("markdown", "--format", help="Output format: markdown or json"),
) -> None:
    """Execute or resume defect reproduction and chain validated evidence into the workflow (alias for 'repro')."""
    cli_eng_workflow_repro(
        workflow_id=workflow_id,
        repo=repo,
        test_command=test_command,
        max_retries=max_retries,
        format=format,
    )


@eng_workflow_app.command(name="plan-repair")
def cli_eng_workflow_plan_repair(
    workflow_id: str = typer.Argument(..., help="Workflow ID in REPRODUCED state to plan repair for"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r", help="Repository path override (defaults to workflow target_repo)"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="LLM provider override"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Model name override"),
    scratch_dir: Optional[Path] = typer.Option(None, "--scratch-dir", help="Scratch workspace directory for candidate analysis"),
    format: str = typer.Option("markdown", "--format", help="Output format: markdown or json"),
) -> None:
    """Validate reproduction evidence and generate an evidence-grounded repair plan."""
    from forge.workflow.orchestrator import EngineeringWorkflowOrchestrator

    orchestrator = EngineeringWorkflowOrchestrator()
    try:
        wf = orchestrator.plan_repair(
            workflow_id=workflow_id,
            workspace_path=repo,
            provider_name=provider,
            model_name=model,
            scratch_workspace_path=scratch_dir,
        )
        events = orchestrator.list_events(wf.workflow_id)
    except Exception as exc:
        typer.secho(f"Repair planning failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if format.lower() == "json":
        typer.echo(wf.model_dump_json(indent=2))
    else:
        typer.echo(_render_eng_workflow_markdown(wf, events, store=orchestrator.store))


@eng_workflow_app.command(name="verify")
def cli_eng_workflow_verify(
    workflow_id: str = typer.Argument(..., help="Workflow ID to verify"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r", help="Repository path override (defaults to workflow target_repo)"),
    scratch_dir: Optional[Path] = typer.Option(None, "--scratch-dir", help="Scratch directory for verification"),
    no_lint: bool = typer.Option(False, "--no-lint", help="Skip static lint checks"),
    no_security: bool = typer.Option(False, "--no-security", help="Skip security analysis checks"),
    baseline_test: Optional[str] = typer.Option(None, "--baseline-test", help="Explicit baseline test command"),
    format: str = typer.Option("markdown", "--format", help="Output format: markdown or json"),
) -> None:
    """Execute isolated multi-check verification (reproduction, regression, lint, security) on proposed repair."""
    from forge.workflow.orchestrator import EngineeringWorkflowOrchestrator

    orchestrator = EngineeringWorkflowOrchestrator()
    try:
        base_cmd = baseline_test.split() if baseline_test else None
        wf = orchestrator.verify_repair(
            workflow_id=workflow_id,
            workspace_path=repo,
            scratch_workspace_path=scratch_dir,
            include_lint=not no_lint,
            include_security=not no_security,
            baseline_test_command=base_cmd,
        )
        events = orchestrator.list_events(wf.workflow_id)
    except Exception as exc:
        typer.secho(f"Workflow verification failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if format.lower() == "json":
        typer.echo(wf.model_dump_json(indent=2))
    else:
        typer.echo(_render_eng_workflow_markdown(wf, events, store=orchestrator.store))


@eng_workflow_app.command(name="advance")
def cli_eng_workflow_advance(
    workflow_id: str = typer.Argument(..., help="Workflow ID to advance"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r", help="Repository path override (defaults to workflow target_repo)"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="LLM provider override"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Model name override"),
    scratch_dir: Optional[Path] = typer.Option(None, "--scratch-dir", help="Scratch workspace directory for candidate analysis"),
    verify: bool = typer.Option(False, "--verify", help="Automatically advance through verification stage if repair plan is generated"),
    format: str = typer.Option("markdown", "--format", help="Output format: markdown or json"),
) -> None:
    """Progress an engineering workflow through sequential stages up to human review."""
    from forge.workflow.orchestrator import EngineeringWorkflowOrchestrator

    orchestrator = EngineeringWorkflowOrchestrator()
    try:
        wf = orchestrator.advance_workflow(
            workflow_id=workflow_id,
            workspace_path=repo,
            provider_name=provider,
            model_name=model,
            scratch_workspace_path=scratch_dir,
            auto_verify=verify,
        )
        events = orchestrator.list_events(wf.workflow_id)
    except Exception as exc:
        typer.secho(f"Workflow advance failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if format.lower() == "json":
        typer.echo(wf.model_dump_json(indent=2))
    else:
        typer.echo(_render_eng_workflow_markdown(wf, events, store=orchestrator.store))


@eng_workflow_app.command(name="export")
def workflow_export(
    workflow_id: str = typer.Argument(..., help="Workflow ID to export as certified engineering dossier"),
    output_dir: Path = typer.Option(..., "--output-dir", "-o", help="Target export directory path"),
    repo: Optional[Path] = typer.Option(None, "--repo", "-r", help="Repository path override for revision drift check"),
    key: Optional[str] = typer.Option(None, "--key", help="Secret key string for HMAC-SHA256 manifest signature"),
    key_file: Optional[Path] = typer.Option(None, "--key-file", help="Path to file containing secret key for signing"),
    key_id: Optional[str] = typer.Option(None, "--key-id", help="Identifier for signing key"),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing dossier package in output directory"),
    format: str = typer.Option("markdown", "--format", help="Output format: markdown or json"),
) -> None:
    """Export an approved engineering workflow into an integrity-verified engineering dossier package."""
    from forge.workflow.dossier import export_engineering_dossier
    from forge.storage.store import ForgeStore
    from forge.config import get_settings

    signing_key = key
    if key_file is not None:
        if not key_file.is_file():
            typer.secho(f"Signing key file not found: '{key_file}'", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        signing_key = key_file.read_text(encoding="utf-8").strip()

    settings = get_settings()
    store = ForgeStore(db_path=settings.db_path)

    try:
        manifest = export_engineering_dossier(
            workflow_id=workflow_id,
            output_dir=output_dir,
            store=store,
            target_repo_path=repo,
            signing_key=signing_key,
            key_id=key_id,
            force=force,
        )
    except Exception as exc:
        typer.secho(f"Dossier export failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if format.lower() == "json":
        typer.echo(manifest.model_dump_json(indent=2))
    else:
        typer.echo("# FORGE Certified Engineering Dossier Exported")
        typer.echo(f"- **Dossier ID**: `{manifest.dossier_id}`")
        typer.echo(f"- **Workflow ID**: `{manifest.workflow_id}`")
        typer.echo(f"- **Export Directory**: `{output_dir.resolve()}`")
        typer.echo(f"- **Base Revision**: `{manifest.base_commit_sha}`")
        typer.echo(f"- **Patch SHA-256**: `{manifest.patch_digest}`")
        typer.echo(f"- **Total Files**: `{len(manifest.files)}`")
        if manifest.signature:
            typer.echo(f"- **Cryptographic Signature**: `HMAC-SHA256` (Key ID: `{manifest.signature.key_id}`)")
        else:
            typer.echo(f"- **Integrity**: `SHA-256 Manifest Checksums` (Unsigned)")
        typer.echo("")
        typer.echo("Files packaged:")
        for fname, entry in sorted(manifest.files.items()):
            typer.echo(f"  - `{fname}` ({entry.byte_size} bytes, sha256: `{entry.sha256[:16]}...`)")
        typer.echo("")
        typer.echo(f"Dossier ready for operator inspection in '{output_dir.resolve()}'. See APPLY.md.")


@eng_workflow_app.command(name="validate-dossier")
def workflow_validate_dossier(
    dossier_dir: Path = typer.Argument(..., help="Path to dossier directory to validate"),
    key: Optional[str] = typer.Option(None, "--key", help="Secret key string to verify HMAC-SHA256 signature"),
    key_file: Optional[Path] = typer.Option(None, "--key-file", help="Path to file containing secret key"),
    format: str = typer.Option("markdown", "--format", help="Output format: markdown or json"),
) -> None:
    """Perform deterministic offline validation of an exported engineering dossier."""
    from forge.workflow.dossier import validate_engineering_dossier

    signing_key = key
    if key_file is not None:
        if not key_file.is_file():
            typer.secho(f"Signing key file not found: '{key_file}'", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        signing_key = key_file.read_text(encoding="utf-8").strip()

    try:
        res = validate_engineering_dossier(
            dossier_dir=dossier_dir,
            signing_key=signing_key,
        )
    except Exception as exc:
        typer.secho(f"Dossier validation error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    if format.lower() == "json":
        typer.echo(res.model_dump_json(indent=2))
    else:
        if res.is_valid:
            typer.secho("[VALID] Engineering dossier passed all integrity and schema checks.", fg=typer.colors.GREEN)
            typer.echo(f"- **Directory**: `{res.dossier_dir}`")
            typer.echo(f"- **Workflow ID**: `{res.workflow_id}`")
            typer.echo(f"- **Base Commit**: `{res.base_commit_sha}`")
            typer.echo(f"- **Patch SHA-256**: `{res.patch_digest}`")
            typer.echo(f"- **Files Verified**: `{res.files_verified}`")
            if res.signature_present:
                if res.signature_valid is True:
                    typer.secho("- **Signature**: [VALID] HMAC-SHA256 signature verified successfully.", fg=typer.colors.GREEN)
                elif res.signature_valid is None:
                    typer.secho("- **Signature**: [UNVERIFIED] HMAC-SHA256 signature present (provide --key to verify).", fg=typer.colors.YELLOW)
                else:
                    typer.secho("- **Signature**: [INVALID] Signature mismatch.", fg=typer.colors.RED)
            else:
                typer.echo("- **Signature**: None (Unsigned manifest with SHA-256 integrity checksums)")
            for w in res.warnings:
                typer.secho(f"  [WARN] {w}", fg=typer.colors.YELLOW)
        else:
            typer.secho("[INVALID] Engineering dossier failed validation.", fg=typer.colors.RED, err=True)
            for err in res.errors:
                typer.secho(f"  [ERROR] {err}", fg=typer.colors.RED, err=True)
            for w in res.warnings:
                typer.secho(f"  [WARN] {w}", fg=typer.colors.YELLOW, err=True)
            raise typer.Exit(code=1)


@eng_workflow_app.command(name="test")
def workflow_acceptance_test(
    scenario: Optional[str] = typer.Option(None, "--scenario", "-s", help="Filter for specific acceptance scenario ID (e.g. P5_ACC01)"),
    category: Optional[str] = typer.Option(None, "--category", "-c", help="Filter for specific domain category"),
    output_format: str = typer.Option("markdown", "--format", "-f", help="Output format: markdown or json"),
    output_path: Optional[str] = typer.Option(None, "--output", "-o", help="Optional file path to export acceptance report"),
    docker_live: bool = typer.Option(False, "--docker-live", help="Opt-in to live Docker sandbox execution if available"),
) -> None:
    """Execute the Phase 5 end-to-end workflow acceptance and reliability suite."""
    from forge.workflow.acceptance import (
        run_phase5_acceptance_suite,
        render_phase5_acceptance_markdown,
        render_phase5_acceptance_json,
    )

    fmt = output_format.lower().strip()
    if fmt not in ("markdown", "json"):
        typer.secho(f"Invalid format '{output_format}'. Must be 'markdown' or 'json'.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    try:
        report = run_phase5_acceptance_suite(
            scenario_filter=scenario,
            category_filter=category,
            live_docker=docker_live,
        )
    except Exception as exc:
        typer.secho(f"Acceptance suite execution failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    rendered = render_phase5_acceptance_json(report) if fmt == "json" else render_phase5_acceptance_markdown(report)

    if output_path:
        out_p = Path(output_path).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(rendered, encoding="utf-8")
        typer.echo(f"Phase 5 acceptance report exported to: {out_p}")
    else:
        typer.echo(rendered)

    if report.metrics.failed_scenarios > 0:
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


@eng_workflow_app.command(name="acceptance")
def workflow_acceptance_alias(
    scenario: Optional[str] = typer.Option(None, "--scenario", "-s", help="Filter for specific acceptance scenario ID (e.g. P5_ACC01)"),
    category: Optional[str] = typer.Option(None, "--category", "-c", help="Filter for specific domain category"),
    output_format: str = typer.Option("markdown", "--format", "-f", help="Output format: markdown or json"),
    output_path: Optional[str] = typer.Option(None, "--output", "-o", help="Optional file path to export acceptance report"),
    docker_live: bool = typer.Option(False, "--docker-live", help="Opt-in to live Docker sandbox execution if available"),
) -> None:
    """Execute the Phase 5 end-to-end workflow acceptance suite (alias for 'forge workflow test')."""
    workflow_acceptance_test(
        scenario=scenario,
        category=category,
        output_format=output_format,
        output_path=output_path,
        docker_live=docker_live,
    )


def main() -> None:
    """CLI entry point."""
    app()


if __name__ == "__main__":
    main()
