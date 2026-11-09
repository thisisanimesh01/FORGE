"""Phase 12 M6: Security & Risk Analyzer.

Performs static inspection of patch contents and affected files for
security-sensitive patterns, dangerous APIs, dependency additions,
and permission changes.
Uses hybrid AST inspection and stateful tokenizer scanning to distinguish
actual executable invocations from comments, documentation, and string literals.
Operates strictly read-only without executing any code.
"""

import ast
import re
import textwrap
from typing import Any, Dict, List, Optional, Set, Tuple

from forge.repair_quality.schemas import (
    QualityDimension,
    QualityDimensionScore,
    QualityStatus,
    RepairQualityPolicy,
    RiskSeverity,
)


DANGEROUS_CALL_PATTERNS = [
    (r"\b(eval|exec)\s*\(", RiskSeverity.CRITICAL, "Arbitrary code execution primitive (eval/exec)"),
    (r"\b(subprocess\.(call|check_call|check_output|run|Popen)|os\.system|os\.popen)\s*\(", RiskSeverity.HIGH, "Process spawning / shell invocation"),
    (r"\bpickle\.(loads?|Unpickler)\s*\(", RiskSeverity.HIGH, "Unsafe object deserialization (pickle)"),
    (r"\bverify\s*=\s*False\b", RiskSeverity.HIGH, "TLS/SSL verification explicitly disabled"),
    (r"\bshutil\.rmtree\s*\(", RiskSeverity.MEDIUM, "Recursive filesystem deletion"),
    (r"\btempfile\.mktemp\s*\(", RiskSeverity.MEDIUM, "Insecure temporary file creation (mktemp)"),
]

SENSITIVE_VAR_PATTERN = re.compile(
    r"\b(password|secret|api_key|token|auth_token|access_token|private_key)\b",
    re.IGNORECASE,
)

WELL_KNOWN_CREDENTIAL_PATTERNS = [
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS Access Key ID"),
    (re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,}\b"), "GitHub Personal Access Token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b"), "GitHub Fine-Grained Token"),
    (re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"), "Slack Token"),
    (re.compile(r"\b(sk_live|rk_live)_[0-9a-zA-Z]{24,}\b"), "Stripe Live Secret Key"),
    (re.compile(r"-----BEGIN (RSA|EC|OPENSSH|DSA|PGP) PRIVATE KEY-----"), "Private Cryptographic Key"),
]

SAFE_PLACEHOLDER_PATTERN = re.compile(
    r"^(test|dummy|fake|placeholder|mock|sample|example|default|changeme|none|null|undefined)[-_0-9a-z]*$",
    re.IGNORECASE,
)

DEPENDENCY_MANIFEST_PATTERNS = [
    r"pyproject\.toml$",
    r"requirements.*\.txt$",
    r"setup\.(py|cfg)$",
]


class _PatchRiskVisitor(ast.NodeVisitor):
    """AST visitor that checks for dangerous calls, aliased imports, and credentials."""

    def __init__(self) -> None:
        self.findings: List[Tuple[RiskSeverity, str]] = []
        self.aliases: Dict[str, str] = {}
        self.func_aliases: Dict[str, str] = {}

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            target = alias.asname or alias.name
            if alias.name in ("subprocess", "os", "pickle", "shutil", "tempfile"):
                self.aliases[target] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        for alias in node.names:
            target = alias.asname or alias.name
            if mod == "subprocess":
                self.func_aliases[target] = f"subprocess.{alias.name}"
            elif mod == "os":
                self.func_aliases[target] = f"os.{alias.name}"
            elif mod == "pickle":
                self.func_aliases[target] = f"pickle.{alias.name}"
            elif mod in ("builtins", "__builtin__"):
                if alias.name in ("eval", "exec"):
                    self.func_aliases[target] = alias.name
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # 1. Direct name call (e.g. eval(x), exec(x), or aliased run(x))
        if isinstance(node.func, ast.Name):
            name = node.func.id
            if name in ("eval", "exec"):
                self.findings.append(
                    (RiskSeverity.CRITICAL, f"Arbitrary code execution primitive ({name}) invocation")
                )
            elif name in self.func_aliases:
                resolved = self.func_aliases[name]
                if resolved in ("eval", "exec"):
                    self.findings.append(
                        (RiskSeverity.CRITICAL, f"Arbitrary code execution primitive ({resolved} via '{name}') invocation")
                    )
                elif resolved.startswith("subprocess."):
                    self.findings.append(
                        (RiskSeverity.HIGH, f"Process spawning / shell invocation ({resolved} via '{name}')")
                    )
                elif resolved in ("os.system", "os.popen"):
                    self.findings.append(
                        (RiskSeverity.HIGH, f"Process spawning / shell invocation ({resolved} via '{name}')")
                    )

        # 2. Attribute call (e.g. subprocess.run, os.system, pickle.loads)
        elif isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            val = node.func.value

            # Check module or aliased object
            mod_name = None
            if isinstance(val, ast.Name):
                mod_name = self.aliases.get(val.id, val.id)

            if mod_name == "subprocess" and attr in ("call", "check_call", "check_output", "run", "Popen"):
                self.findings.append(
                    (RiskSeverity.HIGH, f"Process spawning / shell invocation (subprocess.{attr})")
                )
            elif mod_name == "os" and attr in ("system", "popen"):
                self.findings.append(
                    (RiskSeverity.HIGH, f"Process spawning / shell invocation (os.{attr})")
                )
            elif mod_name == "pickle" and attr in ("loads", "load", "Unpickler"):
                self.findings.append(
                    (RiskSeverity.HIGH, f"Unsafe object deserialization (pickle.{attr})")
                )
            elif mod_name == "shutil" and attr == "rmtree":
                self.findings.append(
                    (RiskSeverity.MEDIUM, "Recursive filesystem deletion (shutil.rmtree)")
                )
            elif mod_name == "tempfile" and attr == "mktemp":
                self.findings.append(
                    (RiskSeverity.MEDIUM, "Insecure temporary file creation (tempfile.mktemp)")
                )

        # 3. Indirect call via getattr(os, "system")(cmd) or getattr(__builtins__, "eval")(x)
        elif isinstance(node.func, ast.Call) and isinstance(node.func.func, ast.Name) and node.func.func.id == "getattr":
            if len(node.func.args) >= 2 and isinstance(node.func.args[1], ast.Constant) and isinstance(node.func.args[1].value, str):
                target_attr = node.func.args[1].value
                if target_attr in ("eval", "exec"):
                    self.findings.append(
                        (RiskSeverity.CRITICAL, f"Indirect arbitrary code execution primitive (getattr -> {target_attr})")
                    )
                elif target_attr in ("system", "popen", "run", "Popen", "call", "check_call"):
                    self.findings.append(
                        (RiskSeverity.HIGH, f"Indirect process spawning invocation (getattr -> {target_attr})")
                    )

        # 4. Indirect call via __builtins__["eval"](x)
        elif isinstance(node.func, ast.Subscript) and isinstance(node.func.value, ast.Name) and node.func.value.id == "__builtins__":
            if isinstance(node.func.slice, ast.Constant) and node.func.slice.value in ("eval", "exec"):
                self.findings.append(
                    (RiskSeverity.CRITICAL, f"Arbitrary code execution primitive (__builtins__[{node.func.slice.value!r}]) invocation")
                )

        # 5. TLS verification explicitly disabled (e.g. requests.get(url, verify=False))
        for kw in node.keywords:
            if kw.arg == "verify" and isinstance(kw.value, ast.Constant) and kw.value.value is False:
                self.findings.append(
                    (RiskSeverity.HIGH, "TLS/SSL verification explicitly disabled (verify=False)")
                )

        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        # Check target variable names and values for credentials
        target_names: List[str] = []
        for target in node.targets:
            if isinstance(target, ast.Name):
                target_names.append(target.id)

        for name in target_names:
            if SENSITIVE_VAR_PATTERN.search(name):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    val = node.value.value
                    self._check_credential_value(name, val)

        self.generic_visit(node)

    def _check_credential_value(self, var_name: str, val: str) -> None:
        # Check well-known patterns (always critical, even with misleading safe substrings)
        for pattern, desc in WELL_KNOWN_CREDENTIAL_PATTERNS:
            if pattern.search(val):
                self.findings.append(
                    (RiskSeverity.CRITICAL, f"Potential hardcoded {desc} in variable '{var_name}'")
                )
                return

        # Generic credentials check
        if len(val) >= 8:
            # Check if this is a genuine harmless test placeholder
            is_placeholder = bool(SAFE_PLACEHOLDER_PATTERN.match(val.strip()))
            if not is_placeholder and len(val) < 200:
                self.findings.append(
                    (RiskSeverity.CRITICAL, f"Potential hardcoded credential/token in variable '{var_name}'")
                )


class RepairRiskAnalyzer:
    """Inspects code modifications for security risks and vulnerabilities."""

    def __init__(self, policy: Optional[RepairQualityPolicy] = None):
        self.policy = policy or RepairQualityPolicy()

    def analyze_risk(
        self,
        patch_content: str,
        modified_files: List[str],
        metrics: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, Any], QualityDimensionScore, RiskSeverity]:
        """Analyze security risk level of the proposed repair."""
        findings: List[str] = []
        highest_severity = RiskSeverity.LOW

        # 1. Parse added Python code with AST for high-fidelity detection
        python_snippets = self._extract_added_python_code(patch_content)
        for filename, code in python_snippets.items():
            ast_findings = self._inspect_python_code_ast(code)
            for severity, desc in ast_findings:
                findings.append(f"{severity.value.upper()}: {desc} in '{filename}'")
                highest_severity = self._escalate_severity(highest_severity, severity)

        # 2. Stateful line scanning fallback for non-Python or unparsed snippets
        line_findings, line_sev = self._scan_raw_added_lines(patch_content)
        for severity, desc in line_findings:
            if desc not in findings:
                findings.append(desc)
                highest_severity = self._escalate_severity(highest_severity, severity)

        # 3. Check for dependency additions
        dependency_changes: List[str] = []
        for f in modified_files:
            for d_pattern in DEPENDENCY_MANIFEST_PATTERNS:
                if re.search(d_pattern, f):
                    dependency_changes.append(f)
                    findings.append(f"Dependency manifest modified: '{f}'")
                    highest_severity = self._escalate_severity(highest_severity, RiskSeverity.MEDIUM)

        # 4. Consider maintainability/lint findings from metrics if passed
        if metrics:
            lint_findings = metrics.get("type_lint_findings") or []
            if lint_findings:
                findings.extend(lint_findings)
                highest_severity = self._escalate_severity(highest_severity, RiskSeverity.MEDIUM)

        # 5. Score Calculation based on highest severity
        severity_score_map = {
            RiskSeverity.NEGLIGIBLE: 1.0,
            RiskSeverity.LOW: 1.0,
            RiskSeverity.MEDIUM: 0.70,
            RiskSeverity.HIGH: 0.30,
            RiskSeverity.CRITICAL: 0.0,
        }
        score = severity_score_map[highest_severity]

        status = (
            QualityStatus.PASSED
            if highest_severity in (RiskSeverity.NEGLIGIBLE, RiskSeverity.LOW)
            else (QualityStatus.HIGH_RISK if highest_severity in (RiskSeverity.HIGH, RiskSeverity.CRITICAL) else QualityStatus.FAILED)
        )

        risk_metrics = {
            "risk_severity": highest_severity.value,
            "security_findings_count": len(findings),
            "dependency_manifests_modified": dependency_changes,
        }

        explanation = (
            f"Risk assessed as '{highest_severity.value}'. "
            f"Security findings: {len(findings)}. "
            f"Dependency changes: {len(dependency_changes)}."
        )

        dimension_score = QualityDimensionScore(
            dimension=QualityDimension.SECURITY_RISK,
            score=score,
            weight=self.policy.dimension_weights.get(QualityDimension.SECURITY_RISK, 0.10),
            status=status,
            explanation=explanation,
            findings=findings,
            is_measured=True,
        )

        return risk_metrics, dimension_score, highest_severity

    def _extract_added_python_code(self, patch_content: str) -> Dict[str, str]:
        """Extract added lines for each modified Python file."""
        files_code: Dict[str, List[str]] = {}
        current_file = "snippet.py"
        is_python = True

        for line in patch_content.splitlines():
            if line.startswith("+++ b/"):
                filepath = line[6:].strip()
                current_file = filepath
                is_python = filepath.endswith(".py")
                if is_python and current_file not in files_code:
                    files_code[current_file] = []
            elif line.startswith("+") and not line.startswith("+++"):
                if is_python:
                    if current_file not in files_code:
                        files_code[current_file] = []
                    files_code[current_file].append(line[1:])

        return {f: "\n".join(lines) for f, lines in files_code.items() if lines}

    def _inspect_python_code_ast(self, code: str) -> List[Tuple[RiskSeverity, str]]:
        """Parse added code with AST and run visitor."""
        findings: List[Tuple[RiskSeverity, str]] = []
        if not code.strip():
            return findings

        # Try parsing directly
        candidates = [
            code,
            # Wrap in function context if snippet is indented or incomplete statement
            "def __patch_scope():\n" + textwrap.indent(textwrap.dedent(code), "    "),
        ]

        parsed_tree = None
        for candidate in candidates:
            try:
                parsed_tree = ast.parse(candidate)
                break
            except SyntaxError:
                continue

        if parsed_tree:
            visitor = _PatchRiskVisitor()
            visitor.visit(parsed_tree)
            findings.extend(visitor.findings)

        return findings

    def _scan_raw_added_lines(self, patch_content: str) -> Tuple[List[Tuple[RiskSeverity, str]], RiskSeverity]:
        """Stateful multi-line regex fallback scanner."""
        findings: List[Tuple[RiskSeverity, str]] = []
        highest = RiskSeverity.LOW

        in_triple_quotes = False
        triple_quote_delim = None

        for line in patch_content.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                raw = line[1:]
                stripped = raw.strip()

                if not stripped:
                    continue

                # Handle triple-quote docstring boundaries
                if not in_triple_quotes:
                    if '"""' in stripped:
                        in_triple_quotes = (stripped.count('"""') % 2 == 1)
                        triple_quote_delim = '"""'
                    elif "'''" in stripped:
                        in_triple_quotes = (stripped.count("'''") % 2 == 1)
                        triple_quote_delim = "'''"
                    if in_triple_quotes:
                        continue
                else:
                    if triple_quote_delim and triple_quote_delim in stripped:
                        if stripped.count(triple_quote_delim) % 2 == 1:
                            in_triple_quotes = False
                            triple_quote_delim = None
                    continue

                # Skip comment-only lines
                if stripped.startswith("#"):
                    continue

                # Strip comments and strings for executable check
                executable = self._strip_comments_and_strings(raw)
                for pattern, severity, desc in DANGEROUS_CALL_PATTERNS:
                    if re.search(pattern, executable):
                        findings.append(
                            (severity, f"{severity.value.upper()}: {desc} in added code: '{stripped[:60]}'")
                        )
                        highest = self._escalate_severity(highest, severity)

                # Check well-known credentials on raw line
                for pattern, desc in WELL_KNOWN_CREDENTIAL_PATTERNS:
                    if pattern.search(raw):
                        findings.append(
                            (RiskSeverity.CRITICAL, f"CRITICAL: Potential hardcoded {desc}")
                        )
                        highest = self._escalate_severity(highest, RiskSeverity.CRITICAL)

        return findings, highest

    def _strip_comments_and_strings(self, line: str) -> str:
        """Strip comments and string literal contents from a Python code line."""
        no_strings = re.sub(r'("[^"\\]*(?:\\.[^"\\]*)*"|\'[^\'\\]*(?:\\.[^\'\\]*)*\')', '""', line)
        if "#" in no_strings:
            no_strings = no_strings.split("#", 1)[0]
        return no_strings

    def _escalate_severity(self, current: RiskSeverity, new_sev: RiskSeverity) -> RiskSeverity:
        """Escalate to the higher severity level."""
        severity_rank = {
            RiskSeverity.NEGLIGIBLE: 0,
            RiskSeverity.LOW: 1,
            RiskSeverity.MEDIUM: 2,
            RiskSeverity.HIGH: 3,
            RiskSeverity.CRITICAL: 4,
        }
        if severity_rank[new_sev] > severity_rank[current]:
            return new_sev
        return current
