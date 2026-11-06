"""Repository inspection and intelligence package."""

from forge.repository.validation import (
    TargetType,
    TargetValidationResult,
    SecurityError,
    validate_target,
    assert_contained_path,
)
from forge.repository.acquisition import (
    AcquiredRepository,
    AcquisitionError,
    acquire_repository,
    cleanup_repository,
)
from forge.repository.detector import (
    ProjectIntelligence,
    detect_repository,
)
from forge.repository.indexer import (
    SymbolType,
    SymbolRecord,
    SymbolIndex,
    build_symbol_index,
    index_python_file,
    parse_python_file_details,
    rel_path_to_module_name,
)
from forge.repository.instructions import (
    InstructionFile,
    load_instruction_files,
    format_instructions_for_context,
)
from forge.repository.architecture import (
    RepositoryMap,
    build_context_prompt,
)
from forge.repository.protection import (
    PROTECTED_FILE_PATTERNS,
    is_protected_path,
    is_protected_name,
    filter_unprotected_paths,
    create_copytree_ignore,
    purge_protected_paths_from_workspace,
    verify_workspace_sanitized,
    GitMetadataProtectionError,
)
from forge.repository.git_mediator import (
    GitMediator,
    GitMediatorError,
    GitMediatorSecurityError,
)
from forge.repository.graph_schemas import (
    NodeKind,
    EdgeKind,
    CallResolutionStatus,
    GraphNode,
    CallEdge,
    CallGraphSummary,
    ImportRecord,
    CallSiteRecord,
    ImportEvidence,
    ModuleNode,
    ModuleEdge,
    ExternalImportRecord,
    UnresolvedImportRecord,
    DependencyCycle,
    StronglyConnectedComponent,
    ModuleDependencyGraphSummary,
)
from forge.repository.resolver import (
    WorkspaceImportResolver,
    ResolvedImport,
)
from forge.repository.call_graph import (
    CallGraph,
    build_call_graph,
)
from forge.repository.module_graph import (
    ModuleDependencyGraph,
    build_module_dependency_graph,
)

__all__ = [
    "TargetType",
    "TargetValidationResult",
    "SecurityError",
    "validate_target",
    "assert_contained_path",
    "AcquiredRepository",
    "AcquisitionError",
    "acquire_repository",
    "cleanup_repository",
    "ProjectIntelligence",
    "detect_repository",
    "SymbolType",
    "SymbolRecord",
    "SymbolIndex",
    "build_symbol_index",
    "index_python_file",
    "parse_python_file_details",
    "rel_path_to_module_name",
    "InstructionFile",
    "load_instruction_files",
    "format_instructions_for_context",
    "RepositoryMap",
    "build_context_prompt",
    "PROTECTED_FILE_PATTERNS",
    "is_protected_path",
    "is_protected_name",
    "filter_unprotected_paths",
    "create_copytree_ignore",
    "purge_protected_paths_from_workspace",
    "verify_workspace_sanitized",
    "GitMetadataProtectionError",
    "GitMediator",
    "GitMediatorError",
    "GitMediatorSecurityError",
    "NodeKind",
    "EdgeKind",
    "CallResolutionStatus",
    "GraphNode",
    "CallEdge",
    "CallGraphSummary",
    "ImportRecord",
    "CallSiteRecord",
    "ImportEvidence",
    "ModuleNode",
    "ModuleEdge",
    "ExternalImportRecord",
    "UnresolvedImportRecord",
    "DependencyCycle",
    "StronglyConnectedComponent",
    "ModuleDependencyGraphSummary",
    "WorkspaceImportResolver",
    "ResolvedImport",
    "CallGraph",
    "build_call_graph",
    "ModuleDependencyGraph",
    "build_module_dependency_graph",
]
