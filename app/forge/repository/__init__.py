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
    "InstructionFile",
    "load_instruction_files",
    "format_instructions_for_context",
    "RepositoryMap",
    "build_context_prompt",
]
