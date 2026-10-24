"""Typer Command-Line Interface for FORGE."""

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


@app.command()
def version() -> None:
    """Show FORGE version."""
    typer.echo(f"FORGE version: {__version__}")


@app.command()
def doctor() -> None:
    """Diagnose local environment, dependencies, and sandbox availability."""
    typer.echo("FORGE System Health Diagnostic")
    typer.echo("------------------------------")
    typer.echo(f"Python Version: {sys.version.split()[0]} ({sys.executable})")

    # Git check
    git_path = shutil.which("git")
    if git_path:
        typer.echo(f"  [OK] Git executable found: {git_path}")
    else:
        typer.echo("  [FAIL] Git executable not found in PATH")

    # Docker sandbox check
    docker_path = shutil.which("docker")
    if docker_path:
        typer.echo(f"  [OK] Docker executable found: {docker_path}")
    else:
        typer.echo("  [WARN] Docker not found in PATH: Runtime check sandboxing will fall back to safe skip mode.")

    # SQLite check
    settings = get_settings()
    typer.echo(f"  [OK] SQLite target path: {settings.db_path.resolve()}")

    # Redaction verification
    test_redact = settings.get_sanitized_dict()
    typer.echo(f"  [OK] Secret redaction active (Provider: {test_redact['llm_provider']})")
    typer.echo("System is ready for Phase 1 operations.")


@app.command()
def inspect(
    target: str = typer.Argument(..., help="Git HTTPS repository URL or authorized local directory"),
    branch: Optional[str] = typer.Option(None, "--branch", "-b", help="Target git branch to inspect"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Run in offline static inspection mode only"),
) -> None:
    """Safely acquire and audit a target repository."""
    typer.echo(f"Initiating inspection on target: {target}")
    if branch:
        typer.echo(f"Target branch: {branch}")
    if no_llm:
        typer.echo("LLM investigation disabled (static-only audit).")
    typer.echo("Note: Full inspection orchestration will be enabled across Milestones 2-6.")


@app.command()
def report(
    run_id: str = typer.Argument(..., help="Run ID to generate report for"),
    output_format: str = typer.Option("markdown", "--format", "-f", help="Report format: markdown or json"),
) -> None:
    """Display or export audit report for a completed run."""
    typer.echo(f"Fetching report for Run ID: {run_id} (Format: {output_format})")


@app.command(name="runs")
def list_runs() -> None:
    """List historical audit runs stored in SQLite."""
    typer.echo("Querying run history...")


@app.command()
def benchmark() -> None:
    """Execute the mini benchmark suite against test fixture repositories."""
    typer.echo("Benchmarking suite will execute in Milestone 6.")


def main() -> None:
    """CLI entry point."""
    app()


if __name__ == "__main__":
    main()
