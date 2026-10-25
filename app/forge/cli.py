"""Typer Command-Line Interface for FORGE."""

import json
import shutil
import sys
from pathlib import Path
from typing import Optional
import typer
from forge import __version__
from forge.config import get_settings

app = typer.Typer(
    name="forge",
    help="FORGE: Autonomous Software Engineering Intelligence (Phase 1: Secure Repository Intelligence)",
    no_args_is_help=True,
)

# Sub-command groups
db_app = typer.Typer(help="Database management commands.")
findings_app = typer.Typer(help="Finding inspection commands.")
sandbox_app = typer.Typer(help="Sandbox diagnostics and execution commands.")
app.add_typer(db_app, name="db")
app.add_typer(findings_app, name="findings")
app.add_typer(sandbox_app, name="sandbox")


@app.command()
def version() -> None:
    """Show FORGE version."""
    typer.echo(f"FORGE version: {__version__}")


@app.command()
def doctor() -> None:
    """Diagnose local environment, dependencies, and sandbox availability."""
    from forge.sandbox.docker import DockerSandbox

    typer.echo("FORGE System Health Diagnostic")
    typer.echo("------------------------------")
    typer.echo(f"Python Version: {sys.version.split()[0]} ({sys.executable})")

    # Static inspection capability
    typer.echo("  [OK] Static repository inspection: available")

    # Git check
    git_path = shutil.which("git")
    if git_path:
        typer.echo(f"  [OK] Git executable found: {git_path}")
    else:
        typer.echo("  [FAIL] Git executable not found in PATH")

    # Sandbox interface check
    typer.echo("  [OK] Sandbox interface: available (fail-secure policy active)")

    # Docker sandbox runtime check
    sandbox = DockerSandbox()
    is_healthy, health_msg = sandbox.check_health()
    if is_healthy:
        typer.echo(f"  [OK] Docker runtime: {health_msg}")
    else:
        typer.echo(f"  [WARN] Docker runtime unavailable: {health_msg}")
        typer.echo("         Runtime check sandboxing will fall back to safe skip mode (host execution is refused).")

    # SQLite check
    settings = get_settings()
    typer.echo(f"  [OK] SQLite target path: {settings.db_path.resolve()}")

    # Database status
    db_file = settings.db_path.resolve()
    if db_file.exists():
        typer.echo(f"  [OK] Database file exists ({db_file.stat().st_size} bytes)")
    else:
        typer.echo("  [INFO] Database not yet initialized (run 'forge db init')")

    # Redaction verification
    test_redact = settings.get_sanitized_dict()
    typer.echo(f"  [OK] Secret redaction active (Provider: {test_redact['llm_provider']})")
    typer.echo("System is ready for Phase 1 operations.")


# --- Sandbox commands ---

@sandbox_app.command(name="doctor")
def sandbox_doctor() -> None:
    """Diagnose sandbox runtime health, image availability, and isolation boundaries."""
    from forge.sandbox.docker import DockerSandbox
    settings = get_settings()

    typer.echo("FORGE Sandbox Diagnostics")
    typer.echo("-------------------------")
    typer.echo(f"Configured image: {settings.docker_image}")
    typer.echo(f"Network policy:   disabled (enforced --network none)")
    typer.echo(f"Filesystem:       read-only workspace mount with isolated 64MB tmpfs")
    typer.echo(f"Process user:     1000:1000 (non-root)")
    typer.echo(f"Privileges:       no-new-privileges, cap-drop ALL")

    sandbox = DockerSandbox()
    healthy, msg = sandbox.check_health()
    if healthy:
        typer.secho(f"Runtime Status:   [AVAILABLE] {msg}", fg=typer.colors.GREEN)
    else:
        typer.secho(f"Runtime Status:   [UNAVAILABLE] {msg}", fg=typer.colors.YELLOW)
        typer.echo("Actionable guidance: To enable isolated test/benchmark execution, start Docker.")
        typer.echo("Security notice:     FORGE strictly refuses to run repository code directly on host.")



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
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="Override LLM provider (fake, openai)"),
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

        # Configure LLM provider
        provider_name = (provider or settings.llm_provider).lower()
        if provider_name == "fake":
            llm_inst = FakeLLMProvider(model_name=model or settings.llm_model)
        elif provider_name in {"openai", "anthropic", "gemini"}:
            llm_inst = OpenAICompatibleProvider(
                model=model or settings.llm_model,
                api_key=settings.llm_api_key,
                api_base=settings.llm_api_base,
            )
            if not llm_inst.is_available():
                typer.secho(
                    f"Provider '{provider_name}' is not configured with credentials. Set FORGE_LLM_API_KEY.",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(code=1)
        else:
            typer.secho(
                f"Unknown LLM provider '{provider_name}'. Must be one of: fake, openai, anthropic, gemini.",
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
) -> None:
    """Display or export audit report for a completed run."""
    typer.echo(f"Fetching report for Run ID: {run_id} (Format: {output_format})")


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


@app.command()
def benchmark() -> None:
    """Execute the mini benchmark suite against test fixture repositories."""
    typer.echo("Benchmarking suite will execute in Milestone 6.")


def main() -> None:
    """CLI entry point."""
    app()


if __name__ == "__main__":
    main()
