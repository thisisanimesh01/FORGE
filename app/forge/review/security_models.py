"""Data models for security analysis and dataflow tracking."""

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

from forge.review.models import FindingCategory
from forge.schemas import FindingSeverity, FindingConfidence


class SecurityHazardType(str, Enum):
    """Categorization of detected security hazards."""
    HARDCODED_SECRET = "hardcoded_secret"
    UNSAFE_COMMAND_EXECUTION = "unsafe_command_execution"
    PATH_TRAVERSAL = "path_traversal"
    SECRET_EXPOSURE_LOG = "secret_exposure_log"
    SECRET_EXPOSURE_HTTP = "secret_exposure_http"
    TOKEN_FORMATTING = "token_formatting"


class SecuritySourceKind(str, Enum):
    """Origin kind for a sensitive value or external input."""
    LITERAL_SECRET = "literal_secret"
    TOKEN_FORMAT = "token_format"
    USER_INPUT = "user_input"
    HEADER_VALUE = "header_value"
    CONFIG_OBJECT = "config_object"


class SecuritySinkKind(str, Enum):
    """Target sink where a hazardous operation or leak occurs."""
    SUBPROCESS_SHELL = "subprocess_shell"
    OS_SYSTEM = "os_system"
    PATH_JOIN = "path_join"
    FILE_OPEN = "file_open"
    LOGGING = "logging"
    HTTP_CLIENT = "http_client"
    PRINT_STATEMENT = "print_statement"


class SecurityDataflowPath(BaseModel):
    """Tracks source -> transformation -> sink dataflow where statically evident."""
    model_config = ConfigDict(frozen=True)

    source_kind: SecuritySourceKind
    source_name: str
    source_line: int
    sink_kind: SecuritySinkKind
    sink_name: str
    sink_line: int
    transformation_steps: List[str] = Field(default_factory=list)
    description: str


class SecurityHazardFinding(BaseModel):
    """Normalized security hazard finding discovered by AST/Dataflow analysis."""
    model_config = ConfigDict(frozen=True)

    rule_id: str
    hazard_type: SecurityHazardType
    category: FindingCategory = FindingCategory.SECURITY
    severity: FindingSeverity = FindingSeverity.HIGH
    confidence: FindingConfidence = FindingConfidence.HIGH
    title: str
    observed_evidence: str
    inference: str
    recommendation: str
    file_path: str
    line_number: int
    end_line_number: Optional[int] = None
    code_snippet: str
    dataflow_path: Optional[SecurityDataflowPath] = None
    details: Dict[str, Any] = Field(default_factory=dict)
