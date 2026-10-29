"""Unit and regression tests for Candidate Semantic Verification Hardening.

Covers:
1. Unreachable code detection after unconditional returns, raises, and branches.
2. Zero false positives on legitimate early returns and multi-guard functions.
3. Docstring displacement detection (dead code / detached string expressions).
4. Validation guard bypass and guard removal detection.
5. Integration with apply_unified_diff_to_file, validate_patch_applicability, and MultiCheckVerifier.
6. GEN06 regression fixture validation: Pilot 03 faulty patch rejected, clean fix accepted.
"""

import ast
import json
from pathlib import Path
import subprocess
import pytest

from forge.repair.contracts import (
    validate_patch_applicability,
)
from forge.repair.semantic_verifier import (
    BypassedGuardInfo,
    SemanticAnalysisResult,
    UnreachableStatementInfo,
    analyze_python_ast_semantics,
    collect_unreachable_statements,
)
from forge.repair.verifier import (
    PatchApplicationError,
    apply_unified_diff_to_file,
    prepare_disposable_workspace,
)
from forge.workflow.models import (
    CheckStatus,
    MultiCheckVerificationPlan,
    VerificationAggregateOutcome,
    VerificationCheckSpec,
    VerificationCheckType,
    WorkflowIntakeSpec,
    IntakeSourceType,
)


# =============================================================================
# 1. Unreachable Statement Detection Tests
# =============================================================================

def test_unreachable_after_unconditional_return():
    """Detect statements immediately following an unconditional return in the same block."""
    orig_code = """
def process(x):
    return x * 2
"""
    patched_code = """
def process(x):
    return x * 2
    print("This is dead code")
    x = x + 1
"""
    res = analyze_python_ast_semantics(orig_code, patched_code, "process.py")
    assert not res.is_valid
    assert len(res.unreachable_statements) == 2
    assert any("unreachable" in v.lower() and "print" in v for v in res.violations)
    assert any("unreachable" in v.lower() and "x = x + 1" in v for v in res.violations)


def test_unreachable_after_unconditional_raise():
    """Detect statements following an unconditional raise in a block."""
    orig_code = """
def validate(x):
    if x < 0:
        raise ValueError("negative")
    return x
"""
    patched_code = """
def validate(x):
    if x < 0:
        raise ValueError("negative")
        print("unreachable after raise")
    return x
"""
    res = analyze_python_ast_semantics(orig_code, patched_code, "validate.py")
    assert not res.is_valid
    assert len(res.unreachable_statements) == 1
    assert res.unreachable_statements[0].terminator_type == "Raise"


def test_unreachable_after_both_branches_return():
    """Detect statements following an if/else where both branches unconditionally terminate."""
    orig_code = """
def branch(cond, a, b):
    if cond:
        return a
    else:
        return b
"""
    patched_code = """
def branch(cond, a, b):
    if cond:
        return a
    else:
        return b
    return "unreachable"
"""
    res = analyze_python_ast_semantics(orig_code, patched_code, "branch.py")
    assert not res.is_valid
    assert len(res.unreachable_statements) >= 1
    assert any("unreachable" in v.lower() for v in res.violations)


# =============================================================================
# 2. Positive Tests: Legitimate Early Returns (Zero False Positives)
# =============================================================================

def test_legitimate_conditional_early_return_not_flagged():
    """Conditional early returns (e.g. guard return None) must NOT be flagged as unreachable."""
    orig_code = """
def find_item(items, target):
    if not items:
        return None
    for item in items:
        if item == target:
            return item
    return None
"""
    patched_code = """
def find_item(items, target):
    # Added safe fast-path
    if not items:
        return None
    for item in items:
        if item == target:
            return item
    return None
"""
    res = analyze_python_ast_semantics(orig_code, patched_code, "find.py")
    assert res.is_valid
    assert len(res.violations) == 0
    assert len(res.unreachable_statements) == 0


def test_multiple_guard_statements_not_flagged():
    """Sequential validation guards that raise on invalid input must NOT be flagged."""
    orig_code = """
def calculate(a, b):
    if a is None:
        raise ValueError("a required")
    if b is None:
        raise ValueError("b required")
    if b == 0:
        raise ZeroDivisionError("division by zero")
    return a / b
"""
    patched_code = """
def calculate(a, b):
    if a is None:
        raise ValueError("a required")
    if b is None:
        raise ValueError("b required")
    if b == 0:
        raise ZeroDivisionError("division by zero")
    # Fixed formula
    return float(a) / float(b)
"""
    res = analyze_python_ast_semantics(orig_code, patched_code, "calc.py")
    assert res.is_valid
    assert len(res.violations) == 0


def test_try_except_with_normal_return():
    """Functions with try/except/finally blocks must be analyzed cleanly without false positives."""
    orig_code = """
def safe_parse(val):
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0
"""
    patched_code = """
def safe_parse(val):
    try:
        return int(val)
    except (ValueError, TypeError):
        # Fallback to zero
        return 0
"""
    res = analyze_python_ast_semantics(orig_code, patched_code, "parse.py")
    assert res.is_valid
    assert len(res.violations) == 0


# =============================================================================
# 3. Docstring Displacement Tests
# =============================================================================

def test_docstring_displacement_detected():
    """Detect when code is inserted between def header and docstring."""
    orig_code = """
def calculate_discount(price: float, discount_percent: float) -> float:
    \"\"\"Calculate discounted price after applying percentage discount.
    \"\"\"
    if price < 0:
        raise ValueError("Price cannot be negative")
    return price + (price * discount_percent / 100.0)
"""
    # Faulty candidate: return inserted before docstring
    patched_code = """
def calculate_discount(price: float, discount_percent: float) -> float:
    return price - (price * discount_percent / 100.0)
    \"\"\"Calculate discounted price after applying percentage discount.
    \"\"\"
    if price < 0:
        raise ValueError("Price cannot be negative")
    return price + (price * discount_percent / 100.0)
"""
    res = analyze_python_ast_semantics(orig_code, patched_code, "pricing.py")
    assert not res.is_valid
    assert res.docstring_displaced
    assert any("docstring displacement" in v.lower() for v in res.violations)
    assert any("unreachable" in v.lower() for v in res.violations)


def test_docstring_preserved_valid():
    """Functions with preserved docstrings must pass validation."""
    orig_code = """
def greet(name: str) -> str:
    \"\"\"Return greeting message.\"\"\"
    return f"Hello, {name}"
"""
    patched_code = """
def greet(name: str) -> str:
    \"\"\"Return greeting message.\"\"\"
    clean_name = name.strip()
    return f"Hello, {clean_name}"
"""
    res = analyze_python_ast_semantics(orig_code, patched_code, "greet.py")
    assert res.is_valid
    assert not res.docstring_displaced
    assert len(res.violations) == 0


# =============================================================================
# 4. Validation Guard Bypass and Removal Tests
# =============================================================================

def test_bypassed_validation_guard_detected():
    """Detect when an unconditional return is inserted before existing validation guards."""
    orig_code = """
def withdraw(balance, amount):
    if amount <= 0:
        raise ValueError("Amount must be positive")
    if amount > balance:
        raise ValueError("Insufficient funds")
    return balance - amount
"""
    patched_code = """
def withdraw(balance, amount):
    return balance - amount
    if amount <= 0:
        raise ValueError("Amount must be positive")
    if amount > balance:
        raise ValueError("Insufficient funds")
    return balance - amount
"""
    res = analyze_python_ast_semantics(orig_code, patched_code, "bank.py")
    assert not res.is_valid
    assert len(res.bypassed_guards) >= 1
    assert any("validation guard bypassed" in v.lower() or "guard stripped" in v.lower() for v in res.violations)


def test_removed_validation_guard_detected():
    """Detect when an existing input validation guard is completely stripped out."""
    orig_code = """
def transfer(account, amount):
    if amount < 0:
        raise ValueError("Negative transfer not permitted")
    return account.send(amount)
"""
    patched_code = """
def transfer(account, amount):
    # Guard removed by patch!
    return account.send(amount)
"""
    res = analyze_python_ast_semantics(orig_code, patched_code, "transfer.py")
    assert not res.is_valid
    assert len(res.bypassed_guards) == 1
    assert "removed" in res.bypassed_guards[0].violation_reason.lower() or "bypassed" in res.bypassed_guards[0].violation_reason.lower()


def test_guard_refinement_preserving_exception_accepted():
    """Refining a guard while preserving exception raising must remain valid."""
    orig_code = """
def process_data(data):
    if not data:
        raise ValueError("Data cannot be empty")
    return data[0]
"""
    patched_code = """
def process_data(data):
    if data is None or len(data) == 0:
        raise ValueError("Data cannot be empty")
    return data[0]
"""
    res = analyze_python_ast_semantics(orig_code, patched_code, "data.py")
    # Does not introduce unreachable code, does not displace docstring
    assert not res.docstring_displaced
    assert len(res.unreachable_statements) == 0


# =============================================================================
# 5. Integration with Patch Applicability and verifier.py
# =============================================================================

def test_apply_unified_diff_rejects_pilot03_faulty_patch(tmp_path: Path):
    """apply_unified_diff_to_file rejects the Pilot 03 faulty early-return patch."""
    src_file = tmp_path / "pricing.py"
    src_file.write_text("""def calculate_discount(price: float, discount_percent: float) -> float:
    \"\"\"Calculate discounted price after applying percentage discount.
    \"\"\"
    if price < 0:
        raise ValueError("Price cannot be negative")
    # Defect: adds discount to price instead of subtracting it
    return price + (price * discount_percent / 100.0)
""", encoding="utf-8")

    faulty_diff = """--- a/pricing.py
+++ b/pricing.py
@@ -1,4 +1,5 @@
 def calculate_discount(price: float, discount_percent: float) -> float:
+    return price - (price * discount_percent / 100.0)
     \"\"\"Calculate discounted price after applying percentage discount.
"""

    with pytest.raises(PatchApplicationError, match="semantic safety violations"):
        apply_unified_diff_to_file(src_file, faulty_diff, check_semantics=True)


def test_apply_unified_diff_accepts_clean_patch(tmp_path: Path):
    """apply_unified_diff_to_file cleanly accepts the correct minimal fix."""
    src_file = tmp_path / "pricing.py"
    src_file.write_text("""def calculate_discount(price: float, discount_percent: float) -> float:
    \"\"\"Calculate discounted price after applying percentage discount.
    \"\"\"
    if price < 0:
        raise ValueError("Price cannot be negative")
    # Defect: adds discount to price instead of subtracting it
    return price + (price * discount_percent / 100.0)
""", encoding="utf-8")

    clean_diff = """--- a/pricing.py
+++ b/pricing.py
@@ -5,3 +5,3 @@
     if price < 0:
         raise ValueError("Price cannot be negative")
-    # Defect: adds discount to price instead of subtracting it
-    return price + (price * discount_percent / 100.0)
+    # Fixed: subtract discount from price
+    return price - (price * discount_percent / 100.0)
"""

    ok, msg = apply_unified_diff_to_file(src_file, clean_diff)
    assert ok
    assert "syntax verified" in msg
    assert "return price - (" in src_file.read_text(encoding="utf-8")


def test_validate_patch_applicability_flags_semantic_violations(tmp_path: Path):
    """validate_patch_applicability in contracts.py flags semantic violations."""
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True)
    src_file = src_dir / "pricing.py"
    src_file.write_text("""def calculate_discount(price: float, discount_percent: float) -> float:
    \"\"\"Calculate discounted price after applying percentage discount.\"\"\"
    if price < 0:
        raise ValueError("Price cannot be negative")
    return price + (price * discount_percent / 100.0)
""", encoding="utf-8")

    faulty_diff = """--- a/src/pricing.py
+++ b/src/pricing.py
@@ -1,3 +1,4 @@
 def calculate_discount(price: float, discount_percent: float) -> float:
+    return price - (price * discount_percent / 100.0)
     \"\"\"Calculate discounted price after applying percentage discount.\"\"\"
"""

    is_appl, reasons = validate_patch_applicability(tmp_path, faulty_diff, check_semantics=True)
    assert not is_appl
    assert any("docstring displacement" in r.lower() or "unreachable" in r.lower() or "bypassed" in r.lower() for r in reasons)


# =============================================================================
# 6. GEN06 Regression Fixture Validation
# =============================================================================

def test_gen06_fixture_metadata_and_tests(tmp_path: Path):
    """Validate GEN06 fixture: baseline fails negative price or defect, clean fix passes all."""
    fixture_dir = Path(__file__).resolve().parent.parent / "fixtures" / "generalization_fixtures" / "GEN06_bypassed_validation_early_return"
    assert fixture_dir.is_dir()

    meta = json.loads((fixture_dir / "fixture.json").read_text(encoding="utf-8"))
    assert meta["fixture_id"] == "GEN06_bypassed_validation_early_return"

    # Setup isolated test repo
    repo = tmp_path / "gen06_repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "src" / "pricing.py").write_text((fixture_dir / "src" / "pricing.py").read_text(encoding="utf-8"), encoding="utf-8")
    (repo / "tests" / "test_pricing.py").write_text((fixture_dir / "tests" / "test_pricing.py").read_text(encoding="utf-8"), encoding="utf-8")

    # 1. Baseline test execution: normal discount test MUST fail with AssertionError (defect)
    res_base = subprocess.run(
        ["python3", "-m", "unittest", "-v", "tests/test_pricing.py"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert res_base.returncode != 0
    assert "AssertionError" in res_base.stderr or "AssertionError" in res_base.stdout

    # 2. Faulty Pilot 03 patch: apply to a test file
    faulty_diff = meta["faulty_pilot03_diff"]
    # Semantic analyzer must reject it
    with pytest.raises(PatchApplicationError):
        apply_unified_diff_to_file(repo / "src" / "pricing.py", faulty_diff, check_semantics=True)

    # 3. Correct minimal patch: apply cleanly and all 3 tests MUST pass
    clean_diff = meta["correct_minimal_diff"]
    ok, _ = apply_unified_diff_to_file(repo / "src" / "pricing.py", clean_diff)
    assert ok

    res_clean = subprocess.run(
        ["python3", "-m", "unittest", "-v", "tests/test_pricing.py"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert res_clean.returncode == 0
    assert "Ran 3 tests" in res_clean.stderr or "Ran 3 tests" in res_clean.stdout
    assert "OK" in res_clean.stderr or "OK" in res_clean.stdout
