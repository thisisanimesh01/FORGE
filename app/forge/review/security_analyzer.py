"""AST-based security analysis and bounded dataflow tracking for code review.

Performs deterministic static analysis on Python ASTs to detect:
- Hardcoded credentials in assignments, dicts, configs, and HTTP Authorization headers.
- Formatted/concatenated token construction.
- Sensitive values reaching sinks (logging, printing, HTTP clients, shell commands).
- Unsafe command execution with shell=True or dynamic shell invocation.
- Uncontained path traversal operations.
"""

import ast
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from forge.config import redact_secrets
from forge.review.models import FindingCategory
from forge.review.security_models import (
    SecurityDataflowPath,
    SecurityHazardFinding,
    SecurityHazardType,
    SecuritySinkKind,
    SecuritySourceKind,
)
from forge.schemas import FindingConfidence, FindingSeverity

# Rule IDs
RULE_HARDCODED_SECRET = "REV_SEC_HARDCODED_SECRET"
RULE_SHELL_EXECUTION = "REV_SEC_SHELL_EXECUTION"
RULE_PATH_TRAVERSAL = "REV_SEC_PATH_TRAVERSAL"
RULE_SECRET_EXPOSURE_LOG = "REV_SEC_SECRET_EXPOSURE_LOG"
RULE_SECRET_EXPOSURE_HTTP = "REV_SEC_SECRET_EXPOSURE_HTTP"
RULE_FORMATTED_TOKEN = "REV_SEC_FORMATTED_TOKEN"

# Known benign placeholders that should NEVER be flagged as credentials
BENIGN_PLACEHOLDERS = {
    "placeholder",
    "dummy",
    "fake",
    "sample",
    "example",
    "change_me",
    "changeme",
    "your_key_here",
    "your_token_here",
    "test_token",
    "none",
    "null",
    "undefined",
    "true",
    "false",
    "default",
    "username",
    "password",
    "secret",
    "bearer",
    "token",
    "auth",
    "redacted",
    "xxx",
    "test",
}

# Substring matches for benign identifiers/docstrings
BENIGN_PATTERNS = re.compile(
    r"(?i)^(?:your[-_]?(?:api[-_]?key|token|secret|password)|fake[-_]?|sample[-_]?|placeholder|example|<.*>|\.\.\.)$"
)

# Known secret variable / key names
SECRET_KEY_NAMES = {
    "api_key",
    "apikey",
    "secret_key",
    "secret",
    "token",
    "auth_token",
    "access_token",
    "password",
    "passwd",
    "pwd",
    "authorization",
    "auth",
    "private_key",
    "client_secret",
}

# Known token prefixes
TOKEN_PREFIXES = ("sk-", "ghp_", "github_pat_", "xoxb-", "xoxp-", "AKIA")

# Logging sinks
LOGGING_CALL_NAMES = {
    "logging.info",
    "logging.debug",
    "logging.warning",
    "logging.error",
    "logging.critical",
    "logger.info",
    "logger.debug",
    "logger.warning",
    "logger.error",
    "logger.critical",
    "log.info",
    "log.debug",
    "log.warning",
    "log.error",
    "log.critical",
    "print",
}

# HTTP client call names
HTTP_CALL_NAMES = {
    "requests.get",
    "requests.post",
    "requests.put",
    "requests.delete",
    "requests.request",
    "httpx.get",
    "httpx.post",
    "httpx.put",
    "httpx.delete",
    "httpx.request",
    "http.client",
    "urllib.request.urlopen",
}


def is_benign_placeholder(val: str) -> bool:
    """Check if a string value is a known documentation example or placeholder."""
    cleaned = (val or "").strip().lower()
    if not cleaned or len(cleaned) < 8:
        return True
    if cleaned in BENIGN_PLACEHOLDERS:
        return True
    if BENIGN_PATTERNS.match(cleaned):
        return True
    # Repeating identical character like 'xxxxxxxxxx' or '0000000000'
    if len(set(cleaned)) <= 2:
        return True
    return False


def is_credential_like_value(val: str) -> bool:
    """Determine if string value exhibits high-confidence credential characteristics."""
    if is_benign_placeholder(val):
        return False
    val_strip = val.strip()
    # Check known token prefixes (e.g. sk-, ghp_, AKIA)
    for prefix in TOKEN_PREFIXES:
        if val_strip.startswith(prefix) and len(val_strip) >= 16:
            return True
    # Check Bearer format: "Bearer <token>"
    if val_strip.lower().startswith("bearer ") and len(val_strip) >= 15:
        subtoken = val_strip.split(None, 1)[1]
        if not is_benign_placeholder(subtoken) and len(subtoken) >= 8:
            return True
    # Generic high entropy / length check for alphanumeric token
    if len(val_strip) >= 16 and re.match(r"^[a-zA-Z0-9_\-\.+=/]+$", val_strip):
        # Must contain mixed alphanumeric/digits
        has_alpha = any(c.isalpha() for c in val_strip)
        has_digit = any(c.isdigit() for c in val_strip)
        if has_alpha and has_digit:
            return True
    return False


class SecurityASTAnalyzer:
    """Performs bounded AST analysis and local intra-procedural dataflow tracking."""

    def __init__(
        self,
        file_path: str,
        source_code: str,
        changed_lines: Optional[Set[int]] = None,
        max_file_chars: int = 500_000,
    ):
        self.file_path = file_path
        self.source_code = source_code
        self.changed_lines = changed_lines or set()
        self.max_file_chars = max_file_chars
        self.findings: List[SecurityHazardFinding] = []

        # Local dataflow tracking state
        # Map of variable_name -> (source_kind, source_name, line_no, is_secret)
        self.symbol_sources: Dict[str, Tuple[SecuritySourceKind, str, int, bool]] = {}

    def analyze(self) -> List[SecurityHazardFinding]:
        """Parse AST and run security hazard detection."""
        if len(self.source_code) > self.max_file_chars:
            # File too large; fail secure by returning empty findings rather than hanging
            return []

        try:
            tree = ast.parse(self.source_code, filename=self.file_path)
        except SyntaxError:
            # Syntax errors in untrusted repository files must not crash analysis
            return []

        self._walk_and_track(tree)
        return self.findings

    def _is_relevant_line(self, lineno: Optional[int]) -> bool:
        """Check if line is within changed hunk lines or if all lines are analyzed."""
        if lineno is None:
            return False
        if not self.changed_lines:
            return True
        return lineno in self.changed_lines

    def _walk_and_track(self, root: ast.AST) -> None:
        """Walk AST nodes and track definitions, transformations, and sinks."""
        for node in ast.walk(root):
            # 1. Track assignments: secret variables, dictionary assignments, token formatting
            if isinstance(node, ast.Assign):
                self._check_assignment(node)

            # 2. Track dictionary literals (e.g. headers = {"Authorization": "Bearer ..."})
            elif isinstance(node, ast.Dict):
                self._check_dict_literal(node)

            # 3. Track calls (shell commands, path joins, logging sinks, HTTP sinks)
            elif isinstance(node, ast.Call):
                self._check_call(node)

    def _check_assignment(self, node: ast.Assign) -> None:
        lineno = getattr(node, "lineno", None)

        # Extract target variable names
        targets: List[Tuple[str, Optional[str]]] = []  # (var_name, key_name if subscript)
        for target in node.targets:
            if isinstance(target, ast.Name):
                targets.append((target.id, None))
            elif isinstance(target, ast.Subscript):
                # e.g. base_header["Authorization"] = "..."
                if isinstance(target.value, ast.Name):
                    key_val = None
                    if isinstance(target.slice, ast.Constant) and isinstance(target.slice.value, str):
                        key_val = target.slice.value
                    targets.append((target.value.id, key_val))

        # Check for literal string secrets in value
        literal_str = self._extract_literal_str(node.value)
        if literal_str is not None:
            for var_name, key_name in targets:
                # Check if target name or key name indicates secret
                target_label = key_name if key_name else var_name
                is_secret_name = target_label.lower() in SECRET_KEY_NAMES or any(
                    k in target_label.lower() for k in ("secret", "token", "password", "apikey", "api_key", "auth")
                )
                is_cred_val = is_credential_like_value(literal_str)

                # Confirmed secret: either name matches AND credential-like, or value has high-confidence token prefix
                if is_cred_val and (is_secret_name or any(literal_str.startswith(p) for p in TOKEN_PREFIXES)):
                    self.symbol_sources[var_name] = (SecuritySourceKind.LITERAL_SECRET, var_name, lineno, True)
                    if self._is_relevant_line(lineno):
                        self._add_hardcoded_secret_finding(
                            target_name=target_label,
                            literal_val=literal_str,
                            lineno=lineno,
                            is_header=(key_name is not None and key_name.lower() in ("authorization", "auth")),
                        )
                elif is_secret_name:
                    # Variable name indicates sensitive token even if value is an identifier or dynamic
                    self.symbol_sources[var_name] = (SecuritySourceKind.CONFIG_OBJECT, var_name, lineno, True)

        # Check for formatted token assembly: e.g. auth_val = f"Bearer {token}"
        elif isinstance(node.value, ast.JoinedStr):
            formatted_text, referenced_vars = self._extract_joined_str_info(node.value)
            for var_name, key_name in targets:
                target_label = key_name if key_name else var_name
                is_token_name = any(k in target_label.lower() for k in ("auth", "token", "secret", "bearer", "header"))
                contains_bearer = "bearer " in formatted_text.lower()
                has_secret_var = any(self.symbol_sources.get(rv, (None, None, None, False))[3] for rv in referenced_vars)

                if is_token_name or contains_bearer or has_secret_var:
                    self.symbol_sources[var_name] = (SecuritySourceKind.TOKEN_FORMAT, var_name, lineno, True)
                    if self._is_relevant_line(lineno):
                        self._add_formatted_token_finding(
                            target_name=target_label,
                            referenced_vars=referenced_vars,
                            lineno=lineno,
                        )

    def _check_dict_literal(self, node: ast.Dict) -> None:
        """Inspect dictionary key/value pairs for hardcoded credentials (e.g. headers)."""
        lineno = getattr(node, "lineno", None)
        for key_node, val_node in zip(node.keys, node.values):
            if key_node is None:
                continue
            key_name = None
            if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                key_name = key_node.value

            if not key_name:
                continue

            is_secret_key = key_name.lower() in SECRET_KEY_NAMES or any(
                k in key_name.lower() for k in ("authorization", "auth", "api_key", "secret", "token")
            )

            # Check literal value
            literal_val = self._extract_literal_str(val_node)
            if literal_val and is_credential_like_value(literal_val) and is_secret_key:
                val_line = getattr(val_node, "lineno", lineno)
                if self._is_relevant_line(val_line):
                    self._add_hardcoded_secret_finding(
                        target_name=key_name,
                        literal_val=literal_val,
                        lineno=val_line,
                        is_header=key_name.lower() in ("authorization", "auth"),
                    )

    def _check_call(self, node: ast.Call) -> None:
        """Inspect calls for shell execution, path traversal, logging leaks, and HTTP leaks."""
        lineno = getattr(node, "lineno", None)
        func_name = self._resolve_func_name(node.func)
        if not func_name:
            return

        # 1. Shell execution: subprocess.run / Popen / os.system with shell=True
        if func_name in ("subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call", "subprocess.check_output", "os.system"):
            shell_true = False
            if func_name == "os.system":
                shell_true = True
            else:
                for kw in node.keywords:
                    if kw.arg == "shell":
                        if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                            shell_true = True

            if shell_true and self._is_relevant_line(lineno):
                self._add_shell_execution_finding(func_name, lineno)

        # 2. Path traversal: os.path.join without containment check
        elif func_name in ("os.path.join", "pathlib.Path.joinpath"):
            if self._is_relevant_line(lineno):
                self._add_path_traversal_finding(func_name, lineno)

        # 3. Secret exposure in logging or print
        elif func_name in LOGGING_CALL_NAMES:
            self._check_sink_leak(
                node=node,
                func_name=func_name,
                sink_kind=SecuritySinkKind.LOGGING if func_name != "print" else SecuritySinkKind.PRINT_STATEMENT,
                rule_id=RULE_SECRET_EXPOSURE_LOG,
                hazard_type=SecurityHazardType.SECRET_EXPOSURE_LOG,
                lineno=lineno,
            )

        # 4. Secret exposure in HTTP client calls
        elif func_name in HTTP_CALL_NAMES:
            self._check_sink_leak(
                node=node,
                func_name=func_name,
                sink_kind=SecuritySinkKind.HTTP_CLIENT,
                rule_id=RULE_SECRET_EXPOSURE_HTTP,
                hazard_type=SecurityHazardType.SECRET_EXPOSURE_HTTP,
                lineno=lineno,
            )

    def _check_sink_leak(
        self,
        node: ast.Call,
        func_name: str,
        sink_kind: SecuritySinkKind,
        rule_id: str,
        hazard_type: SecurityHazardType,
        lineno: int,
    ) -> None:
        """Check if any argument passed into the sink references a tracked secret."""
        if not self._is_relevant_line(lineno):
            return

        for arg in node.args:
            # Case A: Passing a variable directly: logger.info("token: %s", token)
            if isinstance(arg, ast.Name):
                var_name = arg.id
                if var_name in self.symbol_sources:
                    source_kind, source_name, src_line, is_secret = self.symbol_sources[var_name]
                    if is_secret:
                        dataflow = SecurityDataflowPath(
                            source_kind=source_kind,
                            source_name=source_name,
                            source_line=src_line or lineno,
                            sink_kind=sink_kind,
                            sink_name=func_name,
                            sink_line=lineno,
                            transformation_steps=[f"pass_argument({var_name})"],
                            description=f"Sensitive variable '{var_name}' from line {src_line} passed directly into {sink_kind.value} sink '{func_name}'",
                        )
                        self._add_sink_leak_finding(rule_id, hazard_type, func_name, var_name, dataflow, lineno)
                        return

            # Case B: Formatted string containing sensitive variable: f"Token is: {token}"
            elif isinstance(arg, ast.JoinedStr):
                _, ref_vars = self._extract_joined_str_info(arg)
                for var_name in ref_vars:
                    if var_name in self.symbol_sources:
                        source_kind, source_name, src_line, is_secret = self.symbol_sources[var_name]
                        if is_secret:
                            dataflow = SecurityDataflowPath(
                                source_kind=source_kind,
                                source_name=source_name,
                                source_line=src_line or lineno,
                                sink_kind=sink_kind,
                                sink_name=func_name,
                                sink_line=lineno,
                                transformation_steps=[f"formatted_string_interpolation({var_name})"],
                                description=f"Sensitive variable '{var_name}' from line {src_line} interpolated into string passed to {sink_kind.value} sink '{func_name}'",
                            )
                            self._add_sink_leak_finding(rule_id, hazard_type, func_name, var_name, dataflow, lineno)
                            return

            # Case C: Passing raw credential literal directly into sink
            literal_str = self._extract_literal_str(arg)
            if literal_str and is_credential_like_value(literal_str):
                dataflow = SecurityDataflowPath(
                    source_kind=SecuritySourceKind.LITERAL_SECRET,
                    source_name="<literal>",
                    source_line=lineno,
                    sink_kind=sink_kind,
                    sink_name=func_name,
                    sink_line=lineno,
                    transformation_steps=["literal_argument"],
                    description=f"Raw credential literal passed directly into {sink_kind.value} sink '{func_name}'",
                )
                self._add_sink_leak_finding(rule_id, hazard_type, func_name, "<literal>", dataflow, lineno)
                return

    def _add_hardcoded_secret_finding(
        self,
        target_name: str,
        literal_val: str,
        lineno: int,
        is_header: bool = False,
    ) -> None:
        obs = f"Hardcoded credential assigned to '{target_name}' at line {lineno}."
        if is_header:
            obs = f"Hardcoded HTTP authorization credential assigned to header '{target_name}' at line {lineno}."
        inf = "Hardcoded secrets in repository revisions risk unauthorized exfiltration and credential compromise."
        rec = "Move sensitive credentials to external environment variables and load securely at runtime."

        snippet = self._get_line_snippet(lineno)
        finding = SecurityHazardFinding(
            rule_id=RULE_HARDCODED_SECRET,
            hazard_type=SecurityHazardType.HARDCODED_SECRET,
            category=FindingCategory.SECRET_EXPOSURE,
            severity=FindingSeverity.HIGH,
            confidence=FindingConfidence.HIGH,
            title="Hardcoded credential or token detected in diff",
            observed_evidence=obs,
            inference=inf,
            recommendation=rec,
            file_path=self.file_path,
            line_number=lineno,
            code_snippet=snippet,
            details={"target_name": target_name, "is_header": is_header},
        )
        self.findings.append(finding)

    def _add_formatted_token_finding(
        self,
        target_name: str,
        referenced_vars: List[str],
        lineno: int,
    ) -> None:
        vars_str = ", ".join(referenced_vars) if referenced_vars else "components"
        obs = f"Security credential dynamically assembled into '{target_name}' using string formatting referencing {vars_str} at line {lineno}."
        inf = "Dynamic token construction frequently precedes exposure in HTTP requests or logging sinks."
        rec = "Ensure assembled tokens are passed exclusively to trusted clients and never leaked into diagnostic output."

        snippet = self._get_line_snippet(lineno)
        finding = SecurityHazardFinding(
            rule_id=RULE_FORMATTED_TOKEN,
            hazard_type=SecurityHazardType.TOKEN_FORMATTING,
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.HIGH,
            confidence=FindingConfidence.HIGH,
            title="Security credential assembled via string formatting",
            observed_evidence=obs,
            inference=inf,
            recommendation=rec,
            file_path=self.file_path,
            line_number=lineno,
            code_snippet=snippet,
            details={"target_name": target_name, "referenced_vars": referenced_vars},
        )
        self.findings.append(finding)

    def _add_shell_execution_finding(self, func_name: str, lineno: int) -> None:
        obs = f"Invoking subprocess or system command '{func_name}' with shell=True on unvalidated arguments."
        inf = "Allows arbitrary shell command injection if command components contain user-controllable input."
        rec = "Pass arguments as a typed argument list with shell=False and avoid shell wrappers."

        snippet = self._get_line_snippet(lineno)
        finding = SecurityHazardFinding(
            rule_id=RULE_SHELL_EXECUTION,
            hazard_type=SecurityHazardType.UNSAFE_COMMAND_EXECUTION,
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.CRITICAL,
            confidence=FindingConfidence.HIGH,
            title="Insecure shell execution with shell=True",
            observed_evidence=obs,
            inference=inf,
            recommendation=rec,
            file_path=self.file_path,
            line_number=lineno,
            code_snippet=snippet,
            details={"func_name": func_name},
        )
        self.findings.append(finding)

    def _add_path_traversal_finding(self, func_name: str, lineno: int) -> None:
        obs = f"Path assembled using {func_name} with external input without visible containment check."
        inf = "An adversary could supply relative path components ('../') to escape the designated base directory."
        rec = "Enforce strict path containment using forge.repository.validation.assert_contained_path before filesystem access."

        snippet = self._get_line_snippet(lineno)
        finding = SecurityHazardFinding(
            rule_id=RULE_PATH_TRAVERSAL,
            hazard_type=SecurityHazardType.PATH_TRAVERSAL,
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.HIGH,
            confidence=FindingConfidence.HIGH,
            title="Potential uncontained path traversal in os.path.join",
            observed_evidence=obs,
            inference=inf,
            recommendation=rec,
            file_path=self.file_path,
            line_number=lineno,
            code_snippet=snippet,
            details={"func_name": func_name},
        )
        self.findings.append(finding)

    def _add_sink_leak_finding(
        self,
        rule_id: str,
        hazard_type: SecurityHazardType,
        sink_name: str,
        var_name: str,
        dataflow: SecurityDataflowPath,
        lineno: int,
    ) -> None:
        obs = f"Sensitive value '{var_name}' flows directly into output/sink '{sink_name}' at line {lineno}."
        inf = "Exposing sensitive authentication material or credentials in diagnostics or logs allows unauthorized capture."
        rec = f"Redact or remove '{var_name}' before logging or transmitting outside the secure boundary."

        snippet = self._get_line_snippet(lineno)
        title = "Sensitive credential exposure in logging or output sink"
        if hazard_type == SecurityHazardType.SECRET_EXPOSURE_HTTP:
            title = "Sensitive credential transmitted via HTTP client sink"

        finding = SecurityHazardFinding(
            rule_id=rule_id,
            hazard_type=hazard_type,
            category=FindingCategory.SECRET_EXPOSURE,
            severity=FindingSeverity.HIGH,
            confidence=FindingConfidence.HIGH,
            title=title,
            observed_evidence=obs,
            inference=inf,
            recommendation=rec,
            file_path=self.file_path,
            line_number=lineno,
            code_snippet=snippet,
            dataflow_path=dataflow,
            details={"sink_name": sink_name, "variable_name": var_name},
        )
        self.findings.append(finding)

    def _resolve_func_name(self, func_node: ast.AST) -> Optional[str]:
        """Resolve dotted function or call name (e.g. os.path.join, subprocess.run)."""
        if isinstance(func_node, ast.Name):
            return func_node.id
        elif isinstance(func_node, ast.Attribute):
            base = self._resolve_func_name(func_node.value)
            return f"{base}.{func_node.attr}" if base else func_node.attr
        return None

    def _extract_literal_str(self, node: ast.AST) -> Optional[str]:
        """Extract literal string if node is a Constant string."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    def _extract_joined_str_info(self, node: ast.JoinedStr) -> Tuple[str, List[str]]:
        """Extract approximate text and variable names from f-string / JoinedStr."""
        parts = []
        vars_found = []
        for val in node.values:
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                parts.append(val.value)
            elif isinstance(val, ast.FormattedValue) and isinstance(val.value, ast.Name):
                vars_found.append(val.value.id)
                parts.append(f"{{{val.value.id}}}")
        return "".join(parts), vars_found

    def _get_line_snippet(self, lineno: int) -> str:
        """Extract source line snippet."""
        lines = self.source_code.splitlines()
        if 1 <= lineno <= len(lines):
            return lines[lineno - 1].strip()
        return ""
