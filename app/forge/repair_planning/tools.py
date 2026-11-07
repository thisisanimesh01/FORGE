"""Phase 12 M3: Repair Planning Tools for LLM & Agent Invocation.

Exposes structured, typed, read-only tools:
- create_repair_plan: Synthesizes an evidence-driven, risk-aware repair plan.
- compare_repair_strategies: Evaluates and compares candidate repair strategies.
- assess_repair_risk: Analyzes potential side effects, dependency reach, and regressions.
- explain_repair_plan: Summarizes plan objectives, selected strategy, and trade-offs.
- validate_repair_plan: Audits safety invariants (approval_required, execution_permitted).
- generate_verification_plan: Recommends on-disk and proposed tests for validation.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import ValidationError

from forge.config import redact_secrets
from forge.llm.models import ToolDefinition
from forge.repair_planning.planner import RepairPlanningEngine
from forge.repair_planning.risk import RepairRiskAnalyzer
from forge.repair_planning.schemas import (
    AssessRepairRiskInput,
    AssessRepairRiskOutput,
    CompareRepairStrategiesInput,
    CompareRepairStrategiesOutput,
    CreateRepairPlanInput,
    CreateRepairPlanOutput,
    ExplainRepairPlanInput,
    ExplainRepairPlanOutput,
    GenerateVerificationPlanInput,
    GenerateVerificationPlanOutput,
    RepairPlan,
    ValidateRepairPlanInput,
    ValidateRepairPlanOutput,
)
from forge.repair_planning.validation import RepairPlanValidator
from forge.repository.incremental import IncrementalRepositoryIndex
from forge.repository.validation import SecurityError


class RepairPlanningTools:
    """Provides validated tool execution handlers for autonomous repair planning."""

    def __init__(
        self,
        workspace_path: Path,
        incremental_index: Optional[IncrementalRepositoryIndex] = None,
        max_output_chars: int = 50_000,
    ) -> None:
        self.workspace_path = workspace_path.resolve()
        self.incremental_index = incremental_index
        self.max_output_chars = max_output_chars
        self._engine: Optional[RepairPlanningEngine] = None
        self._risk_analyzer: Optional[RepairRiskAnalyzer] = None
        self._validator: Optional[RepairPlanValidator] = None

    def get_engine(self) -> RepairPlanningEngine:
        if self._engine is None:
            self._engine = RepairPlanningEngine(
                workspace_path=self.workspace_path,
                incremental_index=self.incremental_index,
            )
        return self._engine

    def get_risk_analyzer(self) -> RepairRiskAnalyzer:
        if self._risk_analyzer is None:
            self._risk_analyzer = RepairRiskAnalyzer(
                workspace_path=self.workspace_path,
                incremental_index=self.incremental_index,
            )
        return self._risk_analyzer

    def get_validator(self) -> RepairPlanValidator:
        if self._validator is None:
            self._validator = RepairPlanValidator(workspace_path=self.workspace_path)
        return self._validator

    # -----------------------------------------------------------------------
    # Tool Handlers
    # -----------------------------------------------------------------------

    def create_repair_plan(self, args: Dict[str, Any]) -> str:
        """Synthesize an evidence-grounded repair plan."""
        try:
            params = CreateRepairPlanInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for create_repair_plan: {e}"})

        engine = self.get_engine()
        try:
            plan = engine.plan_repair(
                failing_symbol=params.failing_symbol,
                file_path=params.file_path,
                line_number=params.line_number,
                error_message=params.error_message,
            )

            selected_strat = next(
                (s for s in plan.candidate_strategies if s.strategy_id == plan.selected_strategy_id),
                None,
            )

            output = CreateRepairPlanOutput(
                plan_id=plan.plan_id,
                status=plan.status,
                selected_strategy_id=plan.selected_strategy_id,
                selected_strategy_title=selected_strat.title if selected_strat else None,
                affected_files=plan.affected_files,
                affected_symbols=plan.affected_symbols,
                risk_level=plan.risk_assessment.overall_risk,
                confidence=plan.confidence,
                execution_permitted=plan.execution_permitted,
                approval_required=plan.approval_required,
                summary=plan.summary,
                candidate_strategy_count=len(plan.candidate_strategies),
            )
            return output.model_dump_json(indent=2)
        except Exception as exc:
            return json.dumps({"error": f"Failed to create repair plan: {exc}"})

    def compare_repair_strategies(self, args: Dict[str, Any]) -> str:
        """Compare candidate repair strategies."""
        try:
            params = CompareRepairStrategiesInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for compare_repair_strategies: {e}"})

        engine = self.get_engine()
        plan: Optional[RepairPlan] = None

        if params.plan_id:
            plan = engine.get_cached_plan(params.plan_id)

        if plan is None:
            try:
                plan = engine.plan_repair(
                    file_path=params.file_path,
                    failing_symbol=params.failing_symbol,
                )
            except Exception as exc:
                return json.dumps({"error": f"Could not generate candidate strategies: {exc}"})

        strategies_data = []
        for s in plan.candidate_strategies:
            strategies_data.append({
                "strategy_id": s.strategy_id,
                "title": s.title,
                "approach": s.approach.value,
                "complexity": s.complexity.value,
                "invasiveness": s.invasiveness.value,
                "reversibility": s.reversibility.value,
                "score": s.score,
                "is_recommended": s.is_recommended,
                "rejection_reasons": s.rejection_reasons,
            })

        tradeoff = (
            f"Compared {len(strategies_data)} strategies for '{plan.affected_symbols or ['unknown']}'. "
            f"Recommended strategy is '{plan.selected_strategy_id or 'None'}' based on lowest invasiveness "
            "and minimal architectural impact."
        )

        output = CompareRepairStrategiesOutput(
            plan_id=plan.plan_id,
            strategies=strategies_data,
            recommended_strategy_id=plan.selected_strategy_id,
            tradeoff_analysis=tradeoff,
        )
        return output.model_dump_json(indent=2)

    def assess_repair_risk(self, args: Dict[str, Any]) -> str:
        """Assess the risk profile of a proposed modification."""
        try:
            params = AssessRepairRiskInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for assess_repair_risk: {e}"})

        analyzer = self.get_risk_analyzer()
        try:
            risk = analyzer.assess_risk(
                affected_files=params.affected_files,
                affected_symbols=params.affected_symbols,
                proposed_approach=params.proposed_approach,
            )

            output = AssessRepairRiskOutput(
                overall_risk=risk.overall_risk,
                likelihood=risk.likelihood,
                risk_score=risk.risk_score,
                affected_components=risk.affected_components,
                potential_regressions=risk.potential_regressions,
                security_implications=risk.security_implications,
                data_integrity_implications=risk.data_integrity_implications,
                dependency_implications=risk.dependency_implications,
                explanation=risk.explanation,
            )
            return output.model_dump_json(indent=2)
        except Exception as exc:
            return json.dumps({"error": f"Failed to assess repair risk: {exc}"})

    def explain_repair_plan(self, args: Dict[str, Any]) -> str:
        """Provide an explainable summary of a repair plan."""
        try:
            params = ExplainRepairPlanInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for explain_repair_plan: {e}"})

        engine = self.get_engine()
        plan = engine.get_cached_plan(params.plan_id)
        if plan is None:
            return json.dumps({"error": f"Plan '{params.plan_id}' not found in plan cache."})

        selected_strat = next(
            (s for s in plan.candidate_strategies if s.strategy_id == plan.selected_strategy_id),
            None,
        )

        output = ExplainRepairPlanOutput(
            plan_id=plan.plan_id,
            status=plan.status,
            summary=plan.summary,
            problem_statement=plan.problem_statement,
            repair_objective=plan.repair_objective,
            selected_strategy={
                "strategy_id": selected_strat.strategy_id,
                "title": selected_strat.title,
                "description": selected_strat.description,
                "approach": selected_strat.approach.value,
                "invasiveness": selected_strat.invasiveness.value,
            } if selected_strat else None,
            risk_explanation=plan.risk_assessment.explanation,
            verification_summary=(
                f"{len(plan.verification_plan.existing_targeted_tests)} existing test(s) on disk, "
                f"{len(plan.verification_plan.recommended_new_tests)} proposed test(s)."
            ),
            rollback_summary=(
                f"{len(plan.rollback_plan.changes_to_reverse)} reverse change step(s), "
                f"{len(plan.rollback_plan.rollback_triggers)} rollback trigger(s)."
            ),
            approval_required=plan.approval_required,
            execution_permitted=plan.execution_permitted,
        )
        return output.model_dump_json(indent=2)

    def validate_repair_plan(self, args: Dict[str, Any]) -> str:
        """Validate safety invariants for a repair plan."""
        try:
            params = ValidateRepairPlanInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for validate_repair_plan: {e}"})

        engine = self.get_engine()
        validator = self.get_validator()

        plan: Optional[RepairPlan] = None
        if params.plan_id:
            plan = engine.get_cached_plan(params.plan_id)
        elif params.plan_json:
            try:
                plan = RepairPlan.model_validate_json(params.plan_json)
            except Exception as e:
                return json.dumps({"error": f"Failed to parse plan_json: {e}"})

        if plan is None:
            return json.dumps({"error": "Either plan_id or valid plan_json must be provided."})

        is_valid, errors, checked = validator.validate_plan(plan)

        output = ValidateRepairPlanOutput(
            is_valid=is_valid,
            validation_errors=errors,
            checked_invariants=checked,
            execution_permitted_guarantee=(not plan.execution_permitted),
            approval_required_guarantee=plan.approval_required,
        )
        return output.model_dump_json(indent=2)

    def generate_verification_plan(self, args: Dict[str, Any]) -> str:
        """Generate targeted verification recommendations."""
        try:
            params = GenerateVerificationPlanInput.model_validate(args)
        except ValidationError as e:
            return json.dumps({"error": f"Invalid arguments for generate_verification_plan: {e}"})

        engine = self.get_engine()
        existing_tests, proposed_tests = engine._build_verification_tests(
            affected_files=params.affected_files,
            target_symbol=params.affected_symbols[0] if params.affected_symbols else "unknown",
        )

        output = GenerateVerificationPlanOutput(
            existing_targeted_tests=existing_tests,
            recommended_new_tests=proposed_tests,
            regression_test_scope=["Execute workspace test suite across affected packages."],
            security_checks=["Inspect AST for security guard integrity."],
            static_analysis_requirements=["Run flake8 and mypy."],
            expected_verification_criteria=["All targeted tests pass with zero regressions."],
        )
        return output.model_dump_json(indent=2)

    # -----------------------------------------------------------------------
    # Tool Definitions & Registry Interface
    # -----------------------------------------------------------------------

    def get_tool_definitions(self) -> List[ToolDefinition]:
        """Return ToolDefinitions for repair planning tools."""
        return [
            ToolDefinition(
                name="create_repair_plan",
                description="Construct an evidence-grounded, risk-aware repair plan for a diagnosed software defect.",
                parameters=CreateRepairPlanInput.model_json_schema(),
            ),
            ToolDefinition(
                name="compare_repair_strategies",
                description="Compare candidate repair strategies for a defect across complexity, invasiveness, and reversibility.",
                parameters=CompareRepairStrategiesInput.model_json_schema(),
            ),
            ToolDefinition(
                name="assess_repair_risk",
                description="Perform deterministic risk and dependency reach analysis for proposed code modifications.",
                parameters=AssessRepairRiskInput.model_json_schema(),
            ),
            ToolDefinition(
                name="explain_repair_plan",
                description="Summarize and explain the rationale, selected strategy, and trade-offs of a generated repair plan.",
                parameters=ExplainRepairPlanInput.model_json_schema(),
            ),
            ToolDefinition(
                name="validate_repair_plan",
                description="Audit a repair plan against mandatory safety invariants (approval_required, execution_permitted).",
                parameters=ValidateRepairPlanInput.model_json_schema(),
            ),
            ToolDefinition(
                name="generate_verification_plan",
                description="Identify verified on-disk test suites and formulate recommended new tests to validate a repair.",
                parameters=GenerateVerificationPlanInput.model_json_schema(),
            ),
        ]

    def execute_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """Execute a tool by name with arguments and return sanitized result."""
        handlers = {
            "create_repair_plan": self.create_repair_plan,
            "compare_repair_strategies": self.compare_repair_strategies,
            "assess_repair_risk": self.assess_repair_risk,
            "explain_repair_plan": self.explain_repair_plan,
            "validate_repair_plan": self.validate_repair_plan,
            "generate_verification_plan": self.generate_verification_plan,
        }

        if name not in handlers:
            return json.dumps({
                "error": f"Tool '{name}' is not recognized in RepairPlanningTools.",
                "available_tools": list(handlers.keys()),
            })

        try:
            raw_output = handlers[name](arguments)
            redacted = redact_secrets(raw_output)
            if len(redacted) > self.max_output_chars:
                return (
                    redacted[: self.max_output_chars]
                    + f"\n\n[TRUNCATED: Output exceeded maximum allowed size of {self.max_output_chars} characters]"
                )
            return redacted
        except SecurityError as sec_err:
            return json.dumps({"error": f"Security violation: {sec_err}"})
        except Exception as exc:
            return json.dumps({"error": f"Unexpected tool execution error: {exc}"})
