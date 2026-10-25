# Milestone 5 Specification: LLM Provider Abstraction & Typed Investigation Tools

## 1. Goal
Connect FORGE to real LLM providers through a provider-independent interface, and build a bounded, typed investigation loop that uses repository intelligence, evidence models, and the sandbox interface to systematically investigate software repositories without human intervention, while preserving strict security boundaries and audit-only integrity.

## 2. Architectural Boundaries & Security Guarantees
1. **Audit-Only in Phase 1**:
   - The LLM and investigation tools MUST NEVER modify repository files, create git commits, switch branches, push code, or install untrusted dependencies.
   - All tool interactions with the target repository are strictly read-only (`read_file`, `list_dir`, `search`, `get_symbol_index`, `get_repository_map`, `read_source_excerpt`).
   - Sandbox execution (`run_sandbox_check`) only accepts strictly validated, predefined test/inspection commands through Milestone 4's execution policy. Arbitrary shell commands from the model are strictly rejected.
2. **Provider Isolation**:
   - The agent orchestration and tool execution logic is completely decoupled from the underlying LLM provider.
   - Providers implement a unified `LLMProvider` protocol returning typed `LLMResponse` objects with structured `ToolCallRequest` invocations.
   - Providers include:
     - `FakeLLMProvider`: Deterministic, reproducible, offline execution for unit testing, CI, and local verification without API keys or network.
     - `OpenAICompatibleProvider`: HTTP-based provider implementing OpenAI-compatible Chat Completions API (`/v1/chat/completions`) using standard `httpx`. Supports standard OpenAI, Azure OpenAI, Ollama, vLLM, and local OpenAI-compatible proxies.
3. **Fail-Secure Execution**:
   - If no LLM provider is configured or `--no-llm` is passed, the CLI runs in offline static inspection mode.
   - When an LLM API error, authentication error, or network failure occurs, the loop terminates cleanly with a logged error event without crashing or compromising data integrity.
   - If sandbox execution is requested but Docker is unavailable, the tool safely returns `UNAVAILABLE` without executing repository code on the host.
4. **Context Budgeting & Anti-Loop Safeguards**:
   - Explicit step limits (`max_steps`, default 5, hard maximum 10).
   - Per-tool execution timeouts and character/byte output limits.
   - Prompt context budgeting with truncation indicators (`[TRUNCATED: ...]`).
   - Loop/cycle detection: Rejects identical, repetitive tool invocations in successive iterations to prevent infinite reasoning or scraping loops.
5. **Finding & Evidence Integrity**:
   - Model claims and observations do NOT automatically mark findings as `verified`.
   - Tool outputs and model observations are recorded as evidence (`SOURCE_EXCERPT`, `STATIC_ANALYSIS`, `COMMAND_RESULT`) or finding notes.
   - All state transitions adhere to Milestone 3 finding lifecycle constraints (`suspected` -> `reproduced`).
6. **Secret Sanitization**:
   - All prompts sent to the LLM, responses received from the LLM, and tool arguments/results persisted to SQLite or displayed in CLI output are sanitized using `redact_secrets()`.

---

## 3. Detailed Component Design

### A. LLM Models & Abstractions (`app/forge/llm/models.py`)
- `MessageRole` (Enum): `SYSTEM = "system"`, `USER = "user"`, `ASSISTANT = "assistant"`, `TOOL = "tool"`.
- `ToolCallRequest` (BaseModel):
  - `call_id: str`
  - `name: str`
  - `arguments: Dict[str, Any]`
- `ChatMessage` (BaseModel):
  - `role: MessageRole`
  - `content: Optional[str]`
  - `name: Optional[str]` (for tool results)
  - `tool_call_id: Optional[str]` (for tool results)
  - `tool_calls: Optional[List[ToolCallRequest]]` (for assistant calls)
- `ToolParameterProperty` & `ToolParameterSchema` (BaseModel):
  - Standard JSON Schema definition for tool arguments.
- `ToolDefinition` (BaseModel):
  - `name: str`
  - `description: str`
  - `parameters: Dict[str, Any]` (JSON Schema)
- `LLMRequest` (BaseModel):
  - `messages: List[ChatMessage]`
  - `tools: List[ToolDefinition] = []`
  - `temperature: float = 0.0`
  - `max_tokens: Optional[int] = None`
  - `model: Optional[str] = None`
- `LLMResponse` (BaseModel):
  - `content: Optional[str]`
  - `tool_calls: List[ToolCallRequest] = []`
  - `finish_reason: str = "stop"` # "stop", "tool_calls", "length"
  - `prompt_tokens: int = 0`
  - `completion_tokens: int = 0`
  - `total_tokens: int = 0`
  - `model: str = ""`

### B. LLM Provider Protocol & Errors (`app/forge/llm/provider.py`)
- `LLMProviderError(Exception)`
- `LLMAuthenticationError(LLMProviderError)`
- `LLMRateLimitError(LLMProviderError)`
- `LLMContextLengthError(LLMProviderError)`
- `LLMProvider(Protocol)`:
  - `generate(request: LLMRequest) -> LLMResponse`
  - `is_available() -> bool`

### C. Fake LLM Provider (`app/forge/llm/fake.py`)
- `FakeLLMProvider`:
  - Configurable sequence of canned responses or rule-based responses.
  - Can simulate multi-step tool calls followed by a synthesis response.
  - Useful for zero-dependency CI tests and offline mock audits.

### D. OpenAI-Compatible Provider (`app/forge/llm/openai.py`)
- `OpenAICompatibleProvider`:
  - Built with `httpx.Client` (sync).
  - Configurable `api_key`, `api_base` (defaults to `https://api.openai.com/v1`), and `model` (defaults to `gpt-4o`).
  - Converts `LLMRequest` to standard OpenAI chat completion JSON payload with `tools`.
  - Parses OpenAI tool call response format.
  - Maps HTTP 401/403 to `LLMAuthenticationError`, 429 to `LLMRateLimitError`, 400 with context errors to `LLMContextLengthError`.
  - Redacts sensitive credentials from all error messages and debug logs.

### E. Typed Read-Only Investigation Tools (`app/forge/investigation/tools.py`)
Tools available to the investigation orchestrator:
1. `get_repository_map`: Returns high-level repository architecture, detected frameworks, entrypoints, packages, and symbol counts.
2. `search_symbols(query: str, symbol_type: Optional[str] = None)`: Queries the AST symbol index for matching classes, functions, or methods.
3. `read_source_excerpt(file_path: str, start_line: int, end_line: int)`: Reads lines from a file with strict containment checks and maximum line length caps.
4. `list_directory(path: str = ".")`: Lists directory entries within the repository root.
5. `search_text(query: str, path_scope: Optional[str] = None)`: Searches text in repository files with file count and match caps.
6. `get_evidence(finding_id: Optional[str] = None)`: Retrieves evidence records stored for the run or finding.
7. `list_findings()`: Returns current findings discovered so far.
8. `run_sandbox_check(command: List[str], relative_cwd: str = ".")`: Safely invokes `DockerSandbox` with Milestone 4 policy checks. If Docker is unavailable, safely returns `UNAVAILABLE`.

### F. Bounded Investigation Orchestrator (`app/forge/investigation/loop.py`)
- `InvestigationLoop`:
  - Maintains message history and context window within token budget.
  - Manages step limits (`max_steps`).
  - Detects duplicate tool call loops (e.g. calling `read_source_excerpt` on same file/lines 3 times consecutively).
  - Emits events to `ForgeStore`:
    - `LLM_REQUEST_STARTED`
    - `TOOL_CALL_REQUESTED`
    - `TOOL_CALL_COMPLETED`
    - `EVIDENCE_RECORDED`
    - `FINDING_CREATED`
  - Sanitizes all content with `redact_secrets()`.
  - Produces structured `InvestigationResult`:
    - `status`: "completed", "max_steps_exceeded", "failed", "aborted"
    - `steps_executed`: int
    - `findings`: List[Finding]
    - `final_summary`: str

### G. CLI Integration (`app/forge/cli.py`)
- `forge investigate <target>`:
  - Options: `--branch`, `--max-steps`, `--no-llm`, `--provider`, `--model`.
  - Validates repository target and acquires workspace safely.
  - Performs static intelligence mapping.
  - Executes bounded investigation loop.
  - Persists findings, evidence, and events to SQLite.
  - Prints structured markdown summary of the investigation results.
