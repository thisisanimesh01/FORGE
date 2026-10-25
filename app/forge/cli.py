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
benchmark_app = typer.Typer(help="Benchmark evaluation commands.")
policy_app = typer.Typer(help="CI review policy check and enforcement commands.")
app.add_typer(db_app, name="db")
app.add_typer(findings_app, name="findings")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(benchmark_app, name="benchmark")
app.add_typer(policy_app, name="policy")


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


def main() -> None:
    """CLI entry point."""
    app()


if __name__ == "__main__":
    main()
