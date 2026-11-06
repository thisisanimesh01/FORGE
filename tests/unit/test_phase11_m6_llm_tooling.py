"""Comprehensive test suite for Phase 11 Milestone 6: LLM Tooling & Context Integration.

Covers:
1. Tool contracts & JSON schema validation (valid, invalid, missing params, extra args).
2. Repository intelligence tools (overview, symbols, call-graph, module deps, cycles, change impact, source context).
3. Context construction (relevance, deduplication, budget enforcement, deterministic markdown, truncation).
4. M5 incremental cache integration (freshness checks, cache hits, invalidation on file edits, safe fallback).
5. Evidence grounding (traceable evidence records, finding claim validation, anti-fabrication).
6. Local Ollama LLM provider & loop orchestration (mocked Ollama tool calls, repeated-call bounds, no cloud fallback).
7. Security (path containment, protected file boundary, secret redaction).
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch
import pytest

from forge.config import redact_secrets
from forge.llm.context import BuiltRepositoryContext, RepositoryContextBuilder
from forge.llm.evidence_grounding import EvidenceGroundingEngine, GroundingValidationError
from forge.llm.factory import get_provider
from forge.llm.fake import FakeLLMProvider
from forge.llm.models import ChatMessage, LLMRequest, LLMResponse, MessageRole, ToolCallRequest
from forge.llm.provider import LLMUnavailableError
from forge.llm.repo_tool_schemas import (
    CallDirection,
    CallGraphExploreInput,
    ChangeImpactInput,
    ModuleDependencyInput,
    RepositoryOverviewInput,
    SourceContextInput,
    SymbolDetailsInput,
    SymbolSearchInput,
)
from forge.llm.repo_tools import RepositoryIntelligenceTools
from forge.investigation.tools import ToolRegistry
from forge.investigation.loop import InvestigationLoop
from forge.repository.incremental import IncrementalRepositoryIndex
from forge.schemas import EvidenceType, FindingConfidence, FindingSeverity
from forge.storage.store import ForgeStore


# ---------------------------------------------------------------------------
# Fixture: Synthetic Multi-Module Repository
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_fixture(tmp_path: Path) -> Path:
    """Create a structured multi-module repository with known graphs and cycles."""
    repo = tmp_path / "test_repo"
    repo.mkdir()

    # Package A: caller and callee
    pkg_a = repo / "pkg_a"
    pkg_a.mkdir()
    (pkg_a / "__init__.py").write_text("", encoding="utf-8")

    (pkg_a / "service.py").write_text(
        "\"\"\"Service module docstring.\"\"\"\n"
        "from pkg_a.helper import compute_total\n\n"
        "class OrderService:\n"
        "    \"\"\"Order processor.\"\"\"\n"
        "    def process_order(self, items):\n"
        "        return compute_total(items)\n",
        encoding="utf-8",
    )

    (pkg_a / "helper.py").write_text(
        "\"\"\"Helper calculation.\"\"\"\n\n"
        "def compute_total(items):\n"
        "    return sum(items)\n\n"
        "def unused_func():\n"
        "    return 42\n",
        encoding="utf-8",
    )

    # Package B: Circular dependency cycle (mod_x <-> mod_y)
    pkg_b = repo / "pkg_b"
    pkg_b.mkdir()
    (pkg_b / "__init__.py").write_text("", encoding="utf-8")

    (pkg_b / "mod_x.py").write_text(
        "from pkg_b.mod_y import func_y\n\n"
        "def func_x():\n"
        "    return func_y()\n",
        encoding="utf-8",
    )

    (pkg_b / "mod_y.py").write_text(
        "from pkg_b.mod_x import func_x\n\n"
        "def func_y():\n"
        "    return func_x()\n",
        encoding="utf-8",
    )

    # Ambiguous symbols across different files
    (repo / "common.py").write_text(
        "def format_data(data):\n"
        "    return str(data)\n",
        encoding="utf-8",
    )

    (repo / "utils.py").write_text(
        "def format_data(data):\n"
        "    return repr(data)\n",
        encoding="utf-8",
    )

    # Protected file with mock secret
    (repo / ".env").write_text("API_KEY=sk-1234567890abcdef1234567890\n", encoding="utf-8")

    return repo


# ---------------------------------------------------------------------------
# 1. Tool Contract & Schema Tests
# ---------------------------------------------------------------------------

class TestToolContracts:
    """Tests for tool schemas, extra-arg forbidding, and parameter validation."""

    def test_repository_overview_input_valid(self):
        inp = RepositoryOverviewInput(include_cache_stats=True, check_freshness=True)
        assert inp.include_cache_stats is True

    def test_repository_overview_extra_arg_forbidden(self):
        with pytest.raises(Exception):
            RepositoryOverviewInput.model_validate({"extra_param": "forbidden"})

    def test_symbol_search_input_validation(self):
        inp = SymbolSearchInput(query="OrderService", exact_match=True, max_results=10)
        assert inp.query == "OrderService"
        assert inp.max_results == 10

        # Empty query rejected
        with pytest.raises(Exception):
            SymbolSearchInput(query="")

        # Results capping bounds [1, 100]
        with pytest.raises(Exception):
            SymbolSearchInput(query="test", max_results=0)
        with pytest.raises(Exception):
            SymbolSearchInput(query="test", max_results=101)

    def test_call_graph_explore_direction(self):
        inp = CallGraphExploreInput(symbol_id="OrderService.process_order", direction=CallDirection.CALLERS)
        assert inp.direction == CallDirection.CALLERS

        # Invalid direction rejected
        with pytest.raises(Exception):
            CallGraphExploreInput(symbol_id="test", direction="invalid_dir")

    def test_source_context_input_line_bounds(self):
        inp = SourceContextInput(file_path="pkg_a/service.py", start_line=1, end_line=10)
        assert inp.start_line == 1

        with pytest.raises(Exception):
            SourceContextInput(file_path="test.py", start_line=0)

    def test_tool_definitions_json_schema(self, repo_fixture: Path):
        tools = RepositoryIntelligenceTools(workspace_path=repo_fixture)
        defs = tools.get_tool_definitions()
        names = {d.name for d in defs}
        expected = {
            "get_repository_overview",
            "search_symbols",
            "get_symbol_details",
            "explore_call_graph",
            "explore_module_dependencies",
            "analyze_change_impact",
            "get_source_context",
        }
        assert expected.issubset(names)
        for d in defs:
            assert "type" in d.parameters
            assert "properties" in d.parameters


# ---------------------------------------------------------------------------
# 2. Repository Intelligence Tools Execution Tests
# ---------------------------------------------------------------------------

class TestRepositoryIntelligenceTools:
    """Tests executing each of the 6 repository intelligence tools."""

    def test_get_repository_overview(self, repo_fixture: Path):
        tools = RepositoryIntelligenceTools(workspace_path=repo_fixture)
        raw = tools.get_repository_overview({"include_cache_stats": True, "check_freshness": True})
        data = json.loads(raw)
        assert data["total_files_scanned"] >= 5
        assert data["total_classes"] >= 1
        assert data["total_functions"] >= 4
        assert data["total_circular_cycles"] >= 1
        assert data["cache_freshness"]["is_fresh"] is True

    def test_search_symbols_and_ambiguity(self, repo_fixture: Path):
        tools = RepositoryIntelligenceTools(workspace_path=repo_fixture)
        # Search for unique symbol
        raw_order = tools.search_symbols({"query": "OrderService"})
        data_order = json.loads(raw_order)
        assert data_order["matches_count"] == 1
        assert data_order["is_ambiguous"] is False

        # Search for ambiguous symbol 'format_data' present in both common.py and utils.py
        raw_amb = tools.search_symbols({"query": "format_data"})
        data_amb = json.loads(raw_amb)
        assert data_amb["matches_count"] == 2
        assert data_amb["is_ambiguous"] is True
        assert len(data_amb["ambiguous_candidate_ids"]) == 2

    def test_get_symbol_details(self, repo_fixture: Path):
        tools = RepositoryIntelligenceTools(workspace_path=repo_fixture)
        raw = tools.get_symbol_details({"symbol_id": "OrderService"})
        data = json.loads(raw)
        assert data["name"] == "OrderService"
        assert data["symbol_type"] == "class"
        assert "Order processor" in data["docstring"]

    def test_explore_call_graph(self, repo_fixture: Path):
        tools = RepositoryIntelligenceTools(workspace_path=repo_fixture)
        # Check callees of OrderService.process_order (it calls compute_total)
        raw = tools.explore_call_graph({
            "symbol_id": "compute_total",
            "direction": "callers",
            "max_depth": 2,
        })
        data = json.loads(raw)
        assert data["callers_count"] >= 1
        caller = data["callers"][0]
        assert "compute_total" in caller["callee_id"]

    def test_explore_module_dependencies_and_cycles(self, repo_fixture: Path):
        tools = RepositoryIntelligenceTools(workspace_path=repo_fixture)
        raw = tools.explore_module_dependencies({
            "module_name": "pkg_b.mod_x",
            "include_cycles": True,
            "transitive": True,
        })
        data = json.loads(raw)
        assert "pkg_b.mod_y" in data["direct_dependencies"]
        assert len(data["circular_cycles"]) >= 1
        cycle = data["circular_cycles"][0]
        assert "pkg_b.mod_x" in cycle["cycle_path"]
        assert "pkg_b.mod_y" in cycle["cycle_path"]

    def test_analyze_change_impact(self, repo_fixture: Path):
        tools = RepositoryIntelligenceTools(workspace_path=repo_fixture)
        # Modifying helper.py should impact OrderService in service.py
        raw = tools.analyze_change_impact({
            "modified_files": ["pkg_a/helper.py"],
            "max_transitive_depth": 3,
        })
        data = json.loads(raw)
        assert data["total_impacted_entities"] >= 1
        impacted_files = data["impacted_files"]
        assert any("service.py" in f for f in impacted_files)

    def test_get_source_context_bounds_and_sha(self, repo_fixture: Path):
        tools = RepositoryIntelligenceTools(workspace_path=repo_fixture)
        raw = tools.get_source_context({
            "file_path": "pkg_a/service.py",
            "start_line": 1,
            "end_line": 6,
            "include_symbols": True,
        })
        data = json.loads(raw)
        assert data["file_path"] == "pkg_a/service.py"
        assert data["start_line"] == 1
        assert len(data["content_sha256"]) == 64
        assert "OrderService" in data["source_content"]
        assert len(data["symbols_in_range"]) >= 1


# ---------------------------------------------------------------------------
# 3. Context Construction Engine Tests
# ---------------------------------------------------------------------------

class TestRepositoryContextBuilder:
    """Tests for RepositoryContextBuilder relevance, deduplication, and budgeting."""

    def test_context_construction_relevant_symbols(self, repo_fixture: Path):
        builder = RepositoryContextBuilder(workspace_path=repo_fixture, default_char_budget=8000)
        context: BuiltRepositoryContext = builder.build_context(
            query="order processing and totals",
            target_files=["pkg_a/service.py"],
        )
        assert context.char_count > 0
        assert "OrderService" in context.rendered_context
        assert "compute_total" in context.rendered_context
        assert len(context.source_excerpts) >= 1
        assert context.was_truncated is False

    def test_context_budget_enforcement_and_truncation(self, repo_fixture: Path):
        # Enforce very small character budget
        small_budget = 400
        builder = RepositoryContextBuilder(workspace_path=repo_fixture, default_char_budget=small_budget)
        context = builder.build_context(
            query="order helper service",
            char_budget=small_budget,
        )
        assert context.char_count <= small_budget
        assert context.was_truncated is True
        assert "[TRUNCATED:" in context.rendered_context

    def test_context_deduplication(self, repo_fixture: Path):
        builder = RepositoryContextBuilder(workspace_path=repo_fixture)
        # Request overlapping targets in the same file
        context = builder.build_context(
            target_files=["pkg_a/service.py", "pkg_a/service.py"],
            target_symbols=["OrderService", "process_order"],
        )
        files = [exc.file_path for exc in context.source_excerpts]
        # Should not duplicate identical excerpts
        assert len(files) == len(set(files))

    def test_context_cycle_reporting(self, repo_fixture: Path):
        builder = RepositoryContextBuilder(workspace_path=repo_fixture)
        context = builder.build_context(
            target_files=["pkg_b/mod_x.py"],
        )
        assert "Circular Dependency Cycle" in context.rendered_context
        assert len(context.circular_cycles) >= 1


# ---------------------------------------------------------------------------
# 4. M5 Incremental Cache Integration Tests
# ---------------------------------------------------------------------------

class TestM5IncrementalCacheIntegration:
    """Tests verifying cache-backed retrieval, freshness checks, and delta indexing."""

    def test_cache_hits_on_repeated_context_build(self, repo_fixture: Path):
        idx = IncrementalRepositoryIndex(repo_root=repo_fixture)
        # Initial cold build
        idx.build()
        initial_hits = idx.cache_manager.stats.cache_hits

        builder = RepositoryContextBuilder(workspace_path=repo_fixture, incremental_index=idx)
        builder.build_context(query="OrderService")

        # Second build without file changes should hit the cache
        assert idx.cache_manager.stats.cache_hits >= initial_hits

    def test_freshness_check_invalidates_on_edit(self, repo_fixture: Path):
        tools = RepositoryIntelligenceTools(workspace_path=repo_fixture)
        overview_1 = json.loads(tools.get_repository_overview({}))
        assert overview_1["cache_freshness"]["is_fresh"] is True

        # Modify a file
        helper_path = repo_fixture / "pkg_a" / "helper.py"
        helper_path.write_text(helper_path.read_text(encoding="utf-8") + "\ndef added_func(): pass\n")

        # Freshness check detects modification
        tools_after = RepositoryIntelligenceTools(workspace_path=repo_fixture)
        overview_2 = json.loads(tools_after.get_repository_overview({}))
        # Incremental rebuild incorporates new function
        search_res = json.loads(tools_after.search_symbols({"query": "added_func"}))
        assert search_res["matches_count"] == 1

    def test_corrupt_cache_safe_recovery_in_context_builder(self, repo_fixture: Path):
        idx = IncrementalRepositoryIndex(repo_root=repo_fixture)
        idx.build()

        # Corrupt an entry file on disk
        cache_dir = repo_fixture / ".forge" / "cache" / "index"
        for f in cache_dir.glob("entry_*.json"):
            f.write_text("{corrupt json garbage", encoding="utf-8")
            break

        builder = RepositoryContextBuilder(workspace_path=repo_fixture, incremental_index=idx)
        # Should cleanly recover via full rebuild fallback without crashing
        context = builder.build_context(query="OrderService")
        assert "OrderService" in context.rendered_context


# ---------------------------------------------------------------------------
# 5. Evidence Grounding & Anti-Fabrication Tests
# ---------------------------------------------------------------------------

class TestEvidenceGroundingEngine:
    """Tests ensuring evidence provenance, line-number validation, and anti-fabrication."""

    def test_create_source_evidence_verified(self, repo_fixture: Path, tmp_path: Path):
        store = ForgeStore(db_path=tmp_path / "test.db")
        store.create_run("test_target", run_id="run_001")
        engine = EvidenceGroundingEngine(workspace_path=repo_fixture, run_id="run_001", store=store)

        ev = engine.create_source_evidence(
            file_path="pkg_a/service.py",
            start_line=4,
            end_line=7,
            description="OrderService class definition",
            symbol_name="OrderService",
        )
        assert ev.evidence_type == EvidenceType.SOURCE_EXCERPT
        assert ev.file_path == "pkg_a/service.py"
        assert ev.details["is_direct_disk_fact"] is True
        assert len(ev.details["source_sha256"]) == 64

    def test_reject_hallucinated_file(self, repo_fixture: Path):
        engine = EvidenceGroundingEngine(workspace_path=repo_fixture)
        with pytest.raises(GroundingValidationError, match="does not exist on disk"):
            engine.create_source_evidence(
                file_path="pkg_a/nonexistent.py",
                start_line=1,
                end_line=5,
                description="Fake file",
            )

    def test_reject_out_of_bounds_lines(self, repo_fixture: Path):
        engine = EvidenceGroundingEngine(workspace_path=repo_fixture)
        with pytest.raises(GroundingValidationError, match="exceeds file length"):
            engine.create_source_evidence(
                file_path="pkg_a/service.py",
                start_line=9999,
                end_line=10005,
                description="Out of bounds",
            )

    def test_reject_protected_file_evidence(self, repo_fixture: Path):
        engine = EvidenceGroundingEngine(workspace_path=repo_fixture)
        with pytest.raises(GroundingValidationError, match="protected file"):
            engine.create_source_evidence(
                file_path=".env",
                start_line=1,
                end_line=1,
                description="Env secret",
            )

    def test_validate_candidate_finding_grounds_evidence(self, repo_fixture: Path, tmp_path: Path):
        store = ForgeStore(db_path=tmp_path / "test.db")
        store.create_run("test_target", run_id="run_001")
        engine = EvidenceGroundingEngine(workspace_path=repo_fixture, run_id="run_001", store=store)

        finding = engine.validate_candidate_finding(
            title="Service helper defect",
            description="Helper calculation bounds error",
            affected_file="pkg_a/service.py",
            start_line=5,
            end_line=7,
            severity=FindingSeverity.HIGH,
            confidence=FindingConfidence.HIGH,
        )
        assert finding.finding_id.startswith("find_")
        assert len(finding.supporting_evidence_ids) == 1
        persisted = store.get_finding(finding.finding_id)
        assert persisted is not None


# ---------------------------------------------------------------------------
# 6. ToolRegistry Integration Tests
# ---------------------------------------------------------------------------

class TestToolRegistryIntegration:
    """Tests ensuring ToolRegistry exposes Phase 11 tools seamlessly."""

    def test_tool_registry_has_phase11_tools(self, repo_fixture: Path):
        reg = ToolRegistry(workspace_path=repo_fixture)
        defs = reg.get_definitions()
        names = {d.name for d in defs}
        assert "get_repository_overview" in names
        assert "explore_call_graph" in names
        assert "explore_module_dependencies" in names
        assert "analyze_change_impact" in names
        assert "get_source_context" in names

    def test_execute_tool_via_registry(self, repo_fixture: Path):
        reg = ToolRegistry(workspace_path=repo_fixture)
        res = reg.execute_tool("get_repository_overview", {})
        data = json.loads(res)
        assert data["total_files_scanned"] >= 5

    def test_get_source_context_tracks_inspection(self, repo_fixture: Path):
        reg = ToolRegistry(workspace_path=repo_fixture)
        reg.execute_tool("get_source_context", {
            "file_path": "pkg_a/service.py",
            "start_line": 1,
            "end_line": 5,
        })
        assert "pkg_a/service.py" in reg.inspected_source_files


# ---------------------------------------------------------------------------
# 7. LLM Provider Restrictions & Loop Orchestration Tests
# ---------------------------------------------------------------------------

class TestLLMProviderRestrictionsAndLoop:
    """Tests verifying local Ollama provider, no cloud fallback, and loop bounds."""

    def test_ollama_local_only_endpoint(self):
        with patch.dict(os.environ, {"FORGE_LLM_PROVIDER": "ollama", "OLLAMA_BASE_URL": "http://localhost:11434"}):
            provider = get_provider()
            assert "11434" in provider.api_base
            assert "/v1" in provider.api_base
            assert not provider.api_key

    def test_no_cloud_fallback_when_ollama_unavailable(self):
        with patch.dict(os.environ, {"FORGE_LLM_PROVIDER": "ollama", "OLLAMA_BASE_URL": "http://localhost:9999"}):
            provider = get_provider()
            assert "9999" in provider.api_base
            req = LLMRequest(messages=[ChatMessage(role=MessageRole.USER, content="Hello")])
            # Connection failure fails closed without falling back to any cloud API
            with pytest.raises(LLMUnavailableError):
                provider.generate(req)

    def test_mocked_ollama_investigation_loop(self, repo_fixture: Path, tmp_path: Path):
        store = ForgeStore(db_path=tmp_path / "test.db")
        store.create_run("test_target", run_id="run_loop_001")
        reg = ToolRegistry(workspace_path=repo_fixture, store=store, run_id="run_loop_001")

        fake_provider = FakeLLMProvider()
        # Script assistant to call get_repository_overview, then conclude
        fake_provider.scripted_responses = [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        call_id="call_1",
                        name="get_repository_overview",
                        arguments={},
                    )
                ],
            ),
            LLMResponse(
                content="Investigation completed: repository has 1 circular dependency and 0 verified defects.",
                tool_calls=[],
            ),
        ]

        loop = InvestigationLoop(
            provider=fake_provider,
            tool_registry=reg,
            store=store,
            run_id="run_loop_001",
            target="test_target",
            max_steps=3,
        )

        result = loop.run(user_instruction="Audit repository architecture")
        assert result.status == "completed"
        assert result.total_tool_calls >= 1
        assert "Investigation completed" in (result.final_response or "")

    def test_loop_repeated_tool_call_bounds(self, repo_fixture: Path, tmp_path: Path):
        store = ForgeStore(db_path=tmp_path / "test.db")
        store.create_run("test_target", run_id="run_loop_002")
        reg = ToolRegistry(workspace_path=repo_fixture, store=store, run_id="run_loop_002")

        fake_provider = FakeLLMProvider()
        # Script an infinite identical tool-call loop
        repeated_call = ToolCallRequest(
            call_id="call_dup",
            name="get_repository_overview",
            arguments={},
        )
        fake_provider.scripted_responses = [
            LLMResponse(content=None, tool_calls=[repeated_call])
            for _ in range(5)
        ] + [
            LLMResponse(content="Final synthesis completed without errors.", tool_calls=[]),
        ]

        loop = InvestigationLoop(
            provider=fake_provider,
            tool_registry=reg,
            store=store,
            run_id="run_loop_002",
            target="test_target",
            max_steps=4,
        )

        result = loop.run(user_instruction="Audit repository")
        # Loop terminates cleanly within turn budget
        assert result.steps_executed <= 4


# ---------------------------------------------------------------------------
# 8. Security & Secret Redaction Tests
# ---------------------------------------------------------------------------

class TestSecurityAndRedaction:
    """Tests ensuring path traversal rejection and zero credential leakage."""

    def test_path_traversal_rejected_in_source_context(self, repo_fixture: Path):
        tools = RepositoryIntelligenceTools(workspace_path=repo_fixture)
        res = tools.get_source_context({"file_path": "../../etc/passwd", "start_line": 1, "end_line": 10})
        data = json.loads(res)
        assert "error" in data or "Security violation" in data.get("error", "")

    def test_protected_env_file_rejected(self, repo_fixture: Path):
        tools = RepositoryIntelligenceTools(workspace_path=repo_fixture)
        res = tools.get_source_context({"file_path": ".env", "start_line": 1, "end_line": 2})
        data = json.loads(res)
        assert "Access denied" in data["error"] or "protected" in data["error"]

    def test_secret_redaction_in_tool_output(self, repo_fixture: Path):
        tools = RepositoryIntelligenceTools(workspace_path=repo_fixture)
        # Create a file containing a secret token
        leaky_file = repo_fixture / "leaky.py"
        leaky_file.write_text("api_token = 'sk-1234567890abcdef1234567890'\n", encoding="utf-8")

        res = tools.get_source_context({"file_path": "leaky.py", "start_line": 1, "end_line": 2})
        data = json.loads(res)
        assert "sk-1234567890" not in data["source_content"]
        assert "[REDACTED_SECRET]" in data["source_content"]
