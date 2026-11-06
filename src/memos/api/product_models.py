import os
import uuid

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

# Import message types from core types module
from memos.api.contract import TOP_K_MAX
from memos.log import get_logger
from memos.types import MessageList, MessagesType, PermissionDict, SearchMode


def default_search_mode() -> SearchMode:
    """检索请求的默认 mode：``fine``，可用 ``MEMOS_SEARCH_MODE`` 覆盖。"""
    raw = (os.getenv("MEMOS_SEARCH_MODE") or "").strip().lower()
    if not raw:
        return SearchMode.FINE
    try:
        return SearchMode(raw)
    except ValueError:
        logger.warning("MEMOS_SEARCH_MODE=%s 不是合法取值，回退 fine", raw)
        return SearchMode.FINE


logger = get_logger(__name__)
T = TypeVar("T")


class BaseRequest(BaseModel):
    """Base model for all requests."""


class BaseResponse(BaseModel, Generic[T]):
    """Base model for all responses."""

    code: int = Field(200, description="Response status code")
    message: str = Field(..., description="Response message")
    data: T | None = Field(None, description="Response data")


# Product API Models
class UserRegisterRequest(BaseRequest):
    """Request model for user registration."""

    user_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()), description="User ID for registration"
    )
    mem_cube_id: str | None = Field(None, description="Cube ID for registration")
    user_name: str | None = Field(None, description="User name for registration")
    interests: str | None = Field(None, description="User interests")


class GetMemoryPlaygroundRequest(BaseRequest):
    """Request model for getting memories."""

    user_id: str = Field(..., description="User ID")
    memory_type: Literal["text_mem", "act_mem", "param_mem", "para_mem"] = Field(
        ..., description="Memory type"
    )
    mem_cube_ids: list[str] | None = Field(None, description="Cube IDs")
    search_query: str | None = Field(None, description="Search query")
    search_type: Literal["embedding", "fulltext"] = Field("fulltext", description="Search type")


# Start API Models
class Message(BaseModel):
    role: str = Field(..., description="Role of the message (user or assistant).")
    content: str = Field(..., description="Message content.")


class MemoryCreate(BaseRequest):
    user_id: str = Field(..., description="User ID")
    messages: MessageList | None = Field(None, description="List of messages to store.")
    memory_content: str | None = Field(None, description="Content to store as memory")
    doc_path: str | None = Field(None, description="Path to document to store")
    mem_cube_id: str | None = Field(None, description="ID of the memory cube")


class MemCubeRegister(BaseRequest):
    mem_cube_name_or_path: str = Field(..., description="Name or path of the MemCube to register.")
    mem_cube_id: str | None = Field(None, description="ID for the MemCube")


class CreateCubeRequest(BaseRequest):
    """Request model for creating a new memory cube."""

    cube_name: str = Field(..., description="Human-readable name for the memory cube")
    owner_id: str = Field(..., description="User ID of the cube owner")
    cube_path: str | None = Field(
        None, description="File system path where cube data will be stored"
    )
    cube_id: str | None = Field(
        None,
        description=(
            "Custom unique identifier for the cube. If not provided, one will be generated. "
            "Note: cube_id is also referred to as mem_cube_id throughout the API."
        ),
    )


class CreateCubeResponseData(BaseModel):
    """Data model for cube creation response."""

    cube_id: str = Field(..., description="The created cube ID (also called mem_cube_id)")
    cube_name: str = Field(..., description="Name of the created cube")
    owner_id: str = Field(..., description="Owner user ID")


class CreateCubeResponse(BaseResponse[CreateCubeResponseData]):
    """Response model for cube creation."""

    message: str = "Cube created successfully"


class RegisterCubeRequest(BaseRequest):
    """Request model for registering an existing memory cube.

    This allows loading a cube from disk or creating a new one if the path doesn't exist.
    """

    mem_cube_name_or_path: str = Field(
        ..., description="File path to the memory cube or name for a new cube"
    )
    mem_cube_id: str | None = Field(
        None,
        description=(
            "Custom identifier for the cube (also called cube_id). "
            "If not provided, one will be generated."
        ),
    )
    user_id: str | None = Field(
        None, description="User ID to associate with the cube. If not provided, uses default user"
    )


class RegisterCubeResponseData(BaseModel):
    """Data model for cube registration response."""

    cube_id: str = Field(..., description="The registered cube ID (also called mem_cube_id)")
    cube_name_or_path: str = Field(..., description="Name or path of the registered cube")


class RegisterCubeResponse(BaseResponse[RegisterCubeResponseData]):
    """Response model for cube registration."""

    message: str = "Cube registered successfully"


class ExtractPromptRequest(BaseRequest):
    """Request for one project's extract prompt."""

    mem_cube_id: str = Field(..., min_length=1, description="Cube / project id")


class SaveExtractPromptRequest(ExtractPromptRequest):
    """Save a complete extraction prompt for a project."""

    prompt: str = Field(
        ..., description="Complete prompt; must keep the ${conversation} placeholder"
    )


class GenerateExtractPromptRequest(ExtractPromptRequest):
    """Rewrite the current prompt from a one-line instruction (not saved)."""

    description: str = Field(..., min_length=1, description="What to change")


class ExtractDebugMessage(BaseModel):
    role: Literal["system", "user", "assistant"] = Field("user")
    content: str = Field("")


class DebugExtractRequest(ExtractPromptRequest):
    """Run the current prompt once without writing anything."""

    messages: list[ExtractDebugMessage] = Field(default_factory=list)
    extraction_types: list[str] | None = None


class ExtractPromptData(BaseModel):
    mem_cube_id: str
    prompt: str
    extraction_types: list[str]
    updated_at: str | None = None
    source: str
    is_default: bool


class ExtractPromptResponse(BaseResponse[ExtractPromptData]):
    pass


class PromptData(BaseModel):
    prompt: str


class PromptResponse(BaseResponse[PromptData]):
    pass


class PromptListRequest(BaseRequest):
    """Every ``/product/prompts/*`` request is scoped to one project."""

    mem_cube_id: str = Field(..., min_length=1, description="Cube / project id")


class PromptSlotState(BaseModel):
    """Where one language slot of a prompt currently comes from."""

    source: Literal["default", "project"]
    version_id: int = Field(0, description="0 when the slot is the built-in default")
    version_no: int = Field(0, description="Slot-local sequence, 1-based; 0 means default")
    updated_at: str | None = None
    author: str | None = None


class PromptListItem(BaseModel):
    key: str
    group: str
    title: dict[str, str]
    summary: dict[str, str]
    languages: list[str]
    editable: bool
    state: dict[str, PromptSlotState]


class PromptListData(BaseModel):
    prompts: list[PromptListItem]


class PromptListResponse(BaseResponse[PromptListData]):
    pass


class PromptPlaceholderModel(BaseModel):
    name: str
    token: str
    required: bool
    description: dict[str, str]


class PromptOutputContractModel(BaseModel):
    format: str
    required_keys: list[str]
    item_keys: list[str]
    description: dict[str, str]


class PromptRegistration(BaseModel):
    """The registration fields of one prompt."""

    key: str
    group: str
    title: dict[str, str]
    summary: dict[str, str]
    languages: list[str]
    syntax: str
    placeholders: list[PromptPlaceholderModel]
    locked_suffix: str | None = None
    input_kind: str
    input_schema: dict[str, Any]
    output_contract: PromptOutputContractModel
    sample_inputs: list[dict[str, Any]]
    editable: bool


class PromptEffective(BaseModel):
    body: str
    source: Literal["default", "project"]
    version_id: int
    version_no: int = 0
    updated_at: str | None = None
    author: str | None = None


class PromptDetailRequest(PromptListRequest):
    key: str = Field(..., min_length=1)


class PromptDetailData(PromptRegistration):
    defaults: dict[str, str]
    effective: dict[str, PromptEffective]


class PromptDetailResponse(BaseResponse[PromptDetailData]):
    pass


class PromptVersionsRequest(PromptListRequest):
    key: str = Field(..., min_length=1)
    lang: str = Field(..., min_length=1)
    limit: int = Field(20, description="Newest N versions, at most 100")
    before_version_id: int | None = Field(None, description="Pagination cursor")


class PromptVersionItem(BaseModel):
    version_id: int
    version_no: int = 0
    lang: str
    kind: str
    body: str
    note: str = ""
    author: str = ""
    created_at: str
    parent_version_id: int | None = None


class PromptVersionsData(BaseModel):
    versions: list[PromptVersionItem]
    has_more: bool


class PromptVersionsResponse(BaseResponse[PromptVersionsData]):
    pass


class PromptValidateRequest(BaseRequest):
    key: str = Field(..., min_length=1)
    lang: str = Field(..., min_length=1)
    body: str = ""


class PromptIssue(BaseModel):
    code: str
    message: str


class PromptValidateData(BaseModel):
    ok: bool
    errors: list[PromptIssue]
    warnings: list[PromptIssue]


class PromptValidateResponse(BaseResponse[PromptValidateData]):
    pass


class PromptSaveRequest(PromptListRequest):
    key: str = Field(..., min_length=1)
    lang: str = Field(..., min_length=1)
    body: str | None = Field(None, description="New body; mutually exclusive with restore")
    restore_from_version_id: int | None = Field(
        None, description="Roll the slot back to this version"
    )
    note: str = ""
    author: str = ""
    base_version_id: int = Field(0, description="Version the client last saw, 0 for default")

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "PromptSaveRequest":
        if (self.body is None) == (self.restore_from_version_id is None):
            raise ValueError("provide exactly one of body or restore_from_version_id")
        return self


class PromptSaveData(BaseModel):
    version_id: int
    version_no: int = 0
    created_at: str | None = None
    effective: PromptEffective


class PromptSaveResponse(BaseResponse[PromptSaveData]):
    pass


class PromptResetRequest(PromptListRequest):
    key: str | None = Field(None, description="Omit to reset every prompt of the project")
    lang: str | None = Field(None, description="Omit to reset every language of the key")
    author: str = ""


class PromptResetItem(BaseModel):
    key: str
    lang: str
    version_id: int
    version_no: int = 0


class PromptResetData(BaseModel):
    reset: list[PromptResetItem]


class PromptResetResponse(BaseResponse[PromptResetData]):
    pass


class PromptTestRequest(PromptListRequest):
    key: str = Field(..., min_length=1)
    lang: str = Field(..., min_length=1)
    body: str | None = Field(None, description="Draft to try; defaults to the effective body")
    input: dict[str, Any] = Field(default_factory=dict, description="Shaped by input_schema")
    compare_default: bool = False


class PromptRunError(BaseModel):
    code: str
    message: str


class PromptSlotUse(BaseModel):
    """One template slot a run actually read."""

    key: str
    lang: str
    source: Literal["draft", "project", "default"]
    version_id: int = 0


class PromptRun(BaseModel):
    label: Literal["draft", "default"]
    ok: bool
    parse_ok: bool = False
    fallback_triggered: bool = False
    contract: dict[str, Any] = Field(default_factory=dict)
    output: Any = None
    raw_output: str = ""
    truncated: bool = False
    latency_ms: int = 0
    model: str = ""
    usage: dict[str, int] = Field(default_factory=dict)
    used_slots: list[PromptSlotUse] = Field(default_factory=list)
    warnings: list[PromptIssue] = Field(default_factory=list)
    error: PromptRunError | None = None


class PromptTestData(BaseModel):
    runs: list[PromptRun]


class PromptTestResponse(BaseResponse[PromptTestData]):
    pass


class PromptGenerateRequest(PromptListRequest):
    key: str = Field(..., min_length=1)
    lang: str = Field(..., min_length=1)
    instruction: str = Field(..., min_length=1, description="What to change")
    base_body: str | None = Field(None, description="Start from this text; defaults to effective")


class PromptGenerateData(BaseModel):
    body: str
    validation: PromptValidateData


class PromptGenerateResponse(BaseResponse[PromptGenerateData]):
    pass


class ExtractDebugResult(BaseModel):
    type: str
    content: str


class ExtractDebugData(BaseModel):
    extraction_types: list[str]
    results: list[ExtractDebugResult]


class ExtractDebugResponse(BaseResponse[ExtractDebugData]):
    pass


class ModelRecord(BaseModel):
    role: str
    model: str
    spec: str = ""
    source: str
    group: Literal["memory", "chat"]


class ModelListData(BaseModel):
    records: list[ModelRecord]
    total: int


class ModelListResponse(BaseResponse[ModelListData]):
    pass


class ChatRequest(BaseRequest):
    """Request model for chat operations.

    This model is used as the algorithm-facing chat interface, while also
    remaining backward compatible with older developer-facing APIs.
    """

    # ==== Basic identifiers ====
    user_id: str = Field(..., description="User ID")
    query: str = Field(..., description="Chat query message")
    readable_cube_ids: list[str] | None = Field(
        None, description="List of cube IDs user can read for multi-cube chat"
    )
    writable_cube_ids: list[str] | None = Field(
        None, description="List of cube IDs user can write for multi-cube chat"
    )
    history: MessageList | None = Field(None, description="Chat history")
    mode: SearchMode = Field(SearchMode.FAST, description="search mode: fast, fine, or mixture")
    system_prompt: str | None = Field(None, description="Base system prompt to use for chat")
    top_k: int = Field(10, ge=1, le=TOP_K_MAX, description="Number of results to return (max 25)")
    session_id: str | None = Field(None, description="Session ID for soft-filtering memories")
    include_preference: bool = Field(True, description="Whether to handle preference memory")
    pref_top_k: int = Field(6, description="Number of preference results to return")
    model_name_or_path: str | None = Field(None, description="Model name to use for chat")
    max_tokens: int | None = Field(None, description="Max tokens to generate")
    temperature: float | None = Field(None, description="Temperature for sampling")
    top_p: float | None = Field(None, description="Top-p (nucleus) sampling parameter")
    add_message_on_answer: bool = Field(True, description="Add dialogs to memory after chat")
    manager_user_id: str | None = Field(None, description="Manager User ID")
    project_id: str | None = Field(None, description="Project ID")
    memory_limit_number: int | None = Field(
        None,
        ge=1,
        le=TOP_K_MAX,
        description=(
            "Cloud-compatible alias for the recall size. When set it overrides "
            "`top_k`. Range 1-25, aligned with the cloud recall cap."
        ),
    )
    tags: list[str] | None = Field(
        None, description="Custom tags written back with the chat turn (write-back only)."
    )
    info: dict[str, Any] | None = Field(
        None, description="Business metadata written back with the chat turn (write-back only)."
    )
    agent_id: str | None = Field(
        None, description="Agent ID written back with the chat turn (merged into `info`)."
    )
    app_id: str | None = Field(
        None, description="App ID written back with the chat turn (merged into `info`)."
    )
    allow_public: bool = Field(
        False,
        description="Also write the chat turn into tenant-wide public memory (write-back only).",
    )
    relativity: float = Field(
        0.45,
        ge=0,
        description=(
            "Relevance threshold for recalled memories. "
            "Only memories with metadata.relativity >= relativity will be returned. "
            "Use 0 to disable threshold filtering. Default: 0.45."
        ),
    )

    # ==== Filter conditions ====
    filter: dict[str, Any] | None = Field(
        None,
        description=(
            "Optional metadata filter applied to recalled memories. Supports nested "
            '"and" / "or" conditions and comparison operators such as "gt" for fields '
            'like "id", "created_at", or other indexed metadata. Example: '
            '{"and": [{"id": "uuid-xxx"}, {"created_at": {"gt": "2024-01-01"}}]}'
        ),
    )

    # ==== Extended capabilities ====
    internet_search: bool = Field(False, description="Whether to use internet search")
    threshold: float = Field(0.5, description="Threshold for filtering references")

    # ==== Backward compatibility ====
    moscube: bool = Field(
        False,
        description="(Deprecated) Whether to use legacy MemOSCube pipeline.",
    )

    mem_cube_id: str | None = Field(
        None,
        description=(
            "(Deprecated) Single cube ID to use for chat. "
            "Prefer `readable_cube_ids` / `writable_cube_ids` for multi-cube chat."
        ),
    )

    @model_validator(mode="after")
    def _convert_deprecated_fields(self):
        """
        Normalize fields for algorithm interface while preserving backward compatibility.

        Rules:
        - mem_cube_id → readable_cube_ids / writable_cube_ids if they are missing
        - moscube: log warning when True (deprecated)
        """

        # ---- mem_cube_id backward compatibility ----
        if self.mem_cube_id is not None:
            logger.warning(
                "ChatRequest.mem_cube_id is deprecated and will be removed in a future version. "
                "Please migrate to `readable_cube_ids` / `writable_cube_ids`."
            )
            if not self.readable_cube_ids:
                self.readable_cube_ids = [self.mem_cube_id]
            if not self.writable_cube_ids:
                self.writable_cube_ids = [self.mem_cube_id]

        # ---- Deprecated moscube flag ----
        if self.moscube:
            logger.warning(
                "ChatRequest.moscube is deprecated. Legacy MemOSCube pipeline "
                "will be removed in a future version."
            )

        return self

    @model_validator(mode="after")
    def _apply_memory_limit_alias(self):
        """`memory_limit_number` is the cloud name for the recall cap."""
        if self.memory_limit_number is not None:
            self.top_k = self.memory_limit_number
        return self

    @field_validator("filter")
    @classmethod
    def _normalize_filter_fields(cls, value):
        from memos.search.filter_mapping import normalize_filter

        return normalize_filter(value)


class ChatPlaygroundRequest(ChatRequest):
    """Request model for chat operations in playground."""

    beginner_guide_step: str | None = Field(
        None, description="Whether to use beginner guide, option: [first, second]"
    )


class ChatBusinessRequest(ChatRequest):
    """Request model for chat operations for business user."""

    business_key: str = Field(..., description="Business User Key")
    need_search: bool = Field(False, description="Whether to need search before chat")


class ChatCompleteRequest(BaseRequest):
    """Request model for chat operations. will (Deprecated), instead use APIChatCompleteRequest."""

    user_id: str = Field(..., description="User ID")
    query: str = Field(..., description="Chat query message")
    mem_cube_id: str | None = Field(None, description="Cube ID to use for chat")
    history: MessageList | None = Field(None, description="Chat history")
    internet_search: bool = Field(False, description="Whether to use internet search")
    system_prompt: str | None = Field(None, description="Base prompt to use for chat")
    top_k: int = Field(10, ge=1, le=TOP_K_MAX, description="Number of results to return (max 25)")
    threshold: float = Field(0.5, description="Threshold for filtering references")
    session_id: str | None = Field(None, description="Session ID for soft-filtering memories")
    include_preference: bool = Field(True, description="Whether to handle preference memory")
    pref_top_k: int = Field(6, description="Number of preference results to return")
    filter: dict[str, Any] | None = Field(
        None,
        description="Optional metadata filter applied to memories used for chat recall.",
    )
    model_name_or_path: str | None = Field(None, description="Model name to use for chat")
    max_tokens: int | None = Field(None, description="Max tokens to generate")
    temperature: float | None = Field(None, description="Temperature for sampling")
    top_p: float | None = Field(None, description="Top-p (nucleus) sampling parameter")
    add_message_on_answer: bool = Field(True, description="Add dialogs to memory after chat")

    base_prompt: str | None = Field(None, description="(Deprecated) Base prompt alias")
    moscube: bool = Field(
        False, description="(Deprecated) Whether to use legacy MemOSCube pipeline"
    )


class UserCreate(BaseRequest):
    user_name: str | None = Field(None, description="Name of the user")
    role: str = Field("USER", description="Role of the user")
    user_id: str = Field(..., description="User ID")


class CubeShare(BaseRequest):
    target_user_id: str = Field(..., description="Target user ID to share with")


# Response Models
class SimpleResponse(BaseResponse[None]):
    """Simple response model for operations without data return."""


class UserRegisterResponse(BaseResponse[dict]):
    """Response model for user registration."""


class MemoryResponse(BaseResponse[list]):
    """Response model for memory operations."""

    usage: dict[str, int] | None = Field(
        None,
        description=(
            "Serving this request accumulated LLM token usage: "
            "prompt_tokens / completion_tokens / total_tokens / calls. "
            "Absent when the request made no LLM call."
        ),
    )


class SuggestionResponse(BaseResponse[list]):
    """Response model for suggestion operations."""

    data: dict[str, list[str]] | None = Field(None, description="Response data")


class AddStatusResponse(BaseResponse[dict]):
    """Response model for add status operations."""


class ConfigResponse(BaseResponse[None]):
    """Response model for configuration endpoint."""


class SearchResponse(BaseResponse[dict]):
    """Response model for search operations."""


class ChatResponse(BaseResponse[str]):
    """Response model for chat operations."""


class GetMemoryResponse(BaseResponse[dict]):
    """Response model for getting memories."""


class GetMemoryGraphResponse(BaseResponse[dict]):
    """Response model for the dashboard graph view."""


class GetEventMemoriesResponse(BaseResponse[dict]):
    """Response model for event and Dream memories."""


class DeleteMemoryResponse(BaseResponse[dict]):
    """Response model for deleting memories."""


class UserResponse(BaseResponse[dict]):
    """Response model for user operations."""


class UserListResponse(BaseResponse[list]):
    """Response model for user list operations."""


class MemoryCreateRequest(BaseRequest):
    """Request model for creating memories."""

    user_id: str = Field(..., description="User ID")
    messages: str | MessagesType | None = Field(None, description="List of messages to store.")
    memory_content: str | None = Field(None, description="Memory content to store")
    doc_path: str | None = Field(None, description="Path to document to store")
    mem_cube_id: str | None = Field(None, description="Cube ID")
    source: str | None = Field(None, description="Source of the memory")
    user_profile: bool = Field(False, description="User profile memory")
    session_id: str | None = Field(None, description="Session id")
    task_id: str | None = Field(None, description="Task ID for monitoring async tasks")


class SearchRequest(BaseRequest):
    """Request model for searching memories."""

    user_id: str = Field(..., description="User ID")
    query: str = Field(..., description="Search query")
    mem_cube_id: str | None = Field(None, description="Cube ID to search in")
    top_k: int = Field(10, ge=1, le=TOP_K_MAX, description="Number of results to return (max 25)")
    session_id: str | None = Field(None, description="Session ID for soft-filtering memories")


class APISearchRequest(BaseRequest):
    """Request model for searching memories."""

    #: Internal switch used by the cloud facade: ``memory_limit_number`` is a budget
    #: across all views, not a per-view ``top_k``. Private so callers cannot set it.
    _budget_across_views: bool = PrivateAttr(default=False)

    # ==== Response shaping ====
    include_sources: bool = Field(
        False,
        description=(
            "Whether to include metadata.sources (conversation history) in the response. "
            "Off by default: a single preference can carry hundreds of MB of sources."
        ),
    )

    # ==== Basic inputs ====
    query: str = Field(
        ...,
        description="User search query",
    )
    user_id: str = Field(..., min_length=1, description="User ID")
    agent_id: str | list[str] | None = Field(
        None,
        description=(
            "Optional agent id (string or list). When set, memory in the agent's "
            "own scope is included in the results."
        ),
    )
    group_id: str | None = Field(
        None,
        description="Optional group id; includes group-shared memory in the results.",
    )
    app_id: str | None = Field(
        None,
        description=(
            "Calling application id (cloud `app_id`). Added to the filter as `app_id = ...` "
            "unless the filter already pins `app_id`."
        ),
    )
    include_memory_view: (
        list[Literal["detail_factual", "preference", "profile", "event", "tool_memory"]]
        | None
    ) = Field(
        None,
        description=(
            "Cloud-compatible recall whitelist. When provided it takes precedence "
            "over the legacy `include_preference` / "
            "`search_tool_memory` switches."
        ),
    )
    knowledgebase_ids: list[str] | None = Field(
        None,
        description=(
            "Knowledge bases to recall alongside user memory. Accepted for contract "
            "compatibility; KB recall is disabled while `KB_RECALL_ENABLED=false`."
        ),
    )
    time_aware: bool | None = Field(
        None,
        description=(
            "Read the time wording in `query` (e.g. 上周, 去年3月, 以前, last 7 days) and answer "
            "for that moment: a window selects which version of a changeable fact (or which "
            "event) was valid then, past wording attaches `history_versions`, and a query "
            "without time wording returns only what is currently valid. Falls back to the "
            "unrestricted result when nothing matches. Omit to follow the server default "
            "(`SEARCH_TIME_AWARE`, off)."
        ),
    )
    include_history: bool = Field(
        False,
        description=(
            "Attach `metadata.history_versions` (earlier versions of the memory, newest "
            "first) to each hit that has any. Does not change which memories are returned."
        ),
    )

    # ==== Cube scoping ====
    readable_cube_ids: list[str] | None = Field(
        None,
        description=(
            "List of cube IDs that are readable for this request. "
            "Required for algorithm-facing API; optional for developer-facing API."
        ),
    )

    # ==== Search mode ====
    mode: SearchMode = Field(
        default_factory=default_search_mode,
        description="Search mode: fast, fine, or mixture.",
    )

    session_id: str | None = Field(
        None,
        description=(
            "Session ID used as a soft signal to prioritize more relevant memories. "
            "Only used for weighting, not as a hard filter."
        ),
    )

    # ==== Result control ====
    top_k: int = Field(
        9,
        ge=1,
        le=TOP_K_MAX,
        description=(
            "Number of textual memories to retrieve (top-K). Default: 9, max: 25 "
            "(aligned with the cloud recall cap)."
        ),
    )

    relativity: float = Field(
        0.45,
        ge=0,
        description=(
            "Relevance threshold for recalled memories. "
            "Only memories with metadata.relativity >= relativity will be returned. "
            "Use 0 to disable threshold filtering. Default: 0.45."
        ),
    )

    dedup: Literal["no", "sim", "mmr"] | None = Field(
        "mmr",
        description=(
            "Optional dedup option for textual memories. "
            "Use 'no' for no dedup, 'sim' for similarity dedup, 'mmr' for MMR-based dedup. "
            "If None, default exact-text dedup is applied."
        ),
    )

    sort_by: Literal["relativity", "created_at", "updated_at", "chat_time"] | None = Field(
        None,
        description=(
            "Optional post-retrieval sort key. Leave unset to keep the retrieval "
            "(relativity) order. `chat_time` sorts by the event time carried over "
            "from the input messages."
        ),
    )
    sort_order: Literal["asc", "desc"] = Field(
        "desc",
        description="Sort direction used when `sort_by` is set. Default: 'desc'.",
    )

    rerank: bool = Field(
        True,
        description=(
            "Whether to apply the textual memory reranker during search. "
            "Set false to return retrieval-order candidates before post-search dedup/formatting."
        ),
    )

    pref_top_k: int = Field(
        6,
        ge=0,
        description="Number of preference memories to retrieve (top-K). Default: 6.",
    )

    include_preference: bool = Field(
        True,
        description=(
            "Whether to retrieve preference memories along with general memories. "
            "If enabled, the system will automatically recall user preferences "
            "relevant to the query. Default: True."
        ),
    )

    dream_mem_top_k: int = Field(
        1,
        ge=0,
        description=(
            "Number of cross-case synthesis memories (daily digest + derived insights) "
            "to append as extra slots. They are returned in a separate `dream_mem` bucket "
            "and never compete with `text_mem`. Default: 1."
        ),
    )

    include_dream_memory: bool = Field(
        False,
        description=(
            "Whether to append cross-case synthesis memories along with general memories. "
            "If enabled, the system will additionally recall daily digests and derived "
            "insights relevant to the query. Default: False (opt-in)."
        ),
    )

    search_tool_memory: bool = Field(
        True,
        description=(
            "Whether to retrieve tool memories along with general memories. "
            "If enabled, the system will automatically recall tool memories "
            "relevant to the query. Default: True."
        ),
    )

    tool_mem_top_k: int = Field(
        6,
        ge=0,
        description="Number of tool memories to retrieve (top-K). Default: 6.",
    )


    context_format: str = Field(
        "memory",
        description=(
            "How hits read in the search context: 'memory' (default) returns the extracted "
            "memory text; 'sources' returns the original messages each conversation hit was "
            "extracted from (the extracted text moves to metadata.extracted_memory); 'mixed' "
            "(alias 'heuristic_routing') picks one of the two per query. Any other value is "
            "passed through to an installed search.context.render plugin, and rejected with "
            "422 when none is installed."
        ),
    )

    # ==== Filter conditions ====
    filter: dict[str, Any] | None = Field(
        None,
        description=(
            "Optional metadata filter applied to memory search results. Supports nested "
            '"and" / "or" conditions and comparison operators such as "gt" for fields '
            'like "id", "created_at", or other indexed metadata. Example: '
            '{"and": [{"id": "uuid-xxx"}, {"created_at": {"gt": "2024-01-01"}}]}'
        ),
    )

    # ==== Extended capabilities ====
    internet_search: bool = Field(
        False,
        description=(
            "Whether to enable internet search in addition to memory search. "
            "Primarily used by internal algorithms. Default: False."
        ),
    )

    # Inner user, not supported in API yet
    threshold: float | None = Field(
        None,
        description=(
            "Internal similarity threshold for searching plaintext memories. "
            "If None, default thresholds will be applied."
        ),
    )
    # Internal field for search memory type
    search_memory_type: str = Field(
        "All",
        description=(
            "Memory category to search. Supported values include All, WorkingMemory, "
            "LongTermMemory, UserMemory, OuterMemory, ToolSchemaMemory, "
            "ToolTrajectoryMemory, RawFileMemory, AllSummaryMemory, "
            "PreferenceMemory, DreamDiary, and InsightMemory. Default: All."
        ),
    )

    # ==== Context ====
    reference_time: str | None = Field(
        None,
        description=(
            "Optional reference time for time-sensitive search parsing. "
            "If omitted, search uses the current server time."
        ),
    )

    chat_history: MessageList | None = Field(
        None,
        description=(
            "Historical chat messages used internally by algorithms. "
            "If None, internal stored history may be used; "
            "if provided (even an empty list), this value will be used as-is."
        ),
    )

    # ==== Backward compatibility ====
    mem_cube_id: str | None = Field(
        None,
        description=(
            "(Deprecated) Single cube ID to search in. "
            "Prefer `readable_cube_ids` for multi-cube search."
        ),
    )

    moscube: bool = Field(
        False,
        description="(Deprecated / internal) Whether to use legacy MemOSCube path.",
    )

    operation: list[PermissionDict] | None = Field(
        None,
        description="(Internal) Operation definitions for multi-cube read permissions.",
    )

    # ==== Source for  plugin ====
    source: str | None = Field(
        None,
        description=(
            "Origin of the search request. Plugin callers can use this value to route "
            "different search behavior without changing the query payload."
        ),
    )

    neighbor_discovery: bool = Field(
        False,
        description="Whether to enable neighbor discovery. "
        "If enabled, the system will automatically recall neighbor chunks "
        "relevant to the query. Default: False.",
    )

    @model_validator(mode="after")
    def _convert_deprecated_fields(self) -> "APISearchRequest":
        """
        Convert deprecated fields to new fields for backward compatibility.
        Ensures full backward compatibility:
            - mem_cube_id → readable_cube_ids
            - moscube is ignored with warning
            - operation ignored
        """
        # Convert mem_cube_id to readable_cube_ids (new field takes priority)
        if self.mem_cube_id is not None:
            if not self.readable_cube_ids:
                self.readable_cube_ids = [self.mem_cube_id]
            logger.warning(
                "Deprecated field `mem_cube_id` is used in APISearchRequest. "
                "It will be removed in a future version. "
                "Please migrate to `readable_cube_ids`."
            )

        # Reject moscube if set to True (no longer supported)
        if self.moscube:
            logger.warning(
                "Deprecated field `moscube` is used in APISearchRequest. "
                "Legacy MemOSCube pipeline will be removed soon."
            )

        # Warn about operation (internal)
        if self.operation:
            logger.warning(
                "Internal field `operation` is provided in APISearchRequest. "
                "This field is deprecated and ignored."
            )

        return self

    @model_validator(mode="after")
    def _apply_include_memory_view(self) -> "APISearchRequest":
        """`include_memory_view` overrides the legacy per-type switches."""
        from memos.memories.textual.owner import normalize_id_list

        normalize_id_list(self.agent_id)

        views = self.include_memory_view
        if views is None:
            return self
        self.include_preference = "preference" in views
        self.search_tool_memory = "tool_memory" in views
        return self

    @model_validator(mode="after")
    def _apply_app_id(self) -> "APISearchRequest":
        """Cloud `app_id` narrows the recall to memories written by that application."""
        if self.app_id:
            self.filter = _and_condition(self.filter, {"app_id": self.app_id})
        return self

    @field_validator("filter")
    @classmethod
    def _normalize_filter_fields(cls, value):
        from memos.search.filter_mapping import normalize_filter

        return normalize_filter(value)


def _and_condition(filter_: dict[str, Any] | None, condition: dict[str, Any]) -> dict[str, Any]:
    """Add one more condition to an already-normalized filter."""
    if not filter_:
        return condition
    if set(filter_) == {"and"}:
        return {"and": [*filter_["and"], condition]}
    if set(filter_) & {"and", "or"}:
        return {"and": [filter_, condition]}
    from memos.search.filter_mapping import is_per_cube_map

    if is_per_cube_map(filter_):
        per_cube: dict[str, Any] = {}
        for cube_id, node in filter_.items():
            children = node["and"] if set(node) == {"and"} else [node]
            per_cube[cube_id] = {"and": [*children, condition]}
        return per_cube
    return {"and": [filter_, condition]}


class APIADDRequest(BaseRequest):
    """Request model for creating memories."""

    # Model-level example so the interactive docs (/docs) show a copy-paste-ready
    # payload. Without it, Swagger UI renders the leading `str` branch of the
    # `messages` union as `"string"` (see issue #1505). This only affects the
    # generated OpenAPI schema, not validation or runtime behaviour.
    model_config = {
        "json_schema_extra": {
            "example": {
                "user_id": "8736b16e-1d20-4163-980b-a5063c3facdc",
                "writable_cube_ids": ["b32d0977-435d-4828-a86f-4f47f8b55bca"],
                "messages": [{"role": "user", "content": "I am learning ggplot2 in R."}],
                "async_mode": "async",
            }
        }
    }

    # ==== Basic identifiers ====
    user_id: str | list[str] = Field(
        None,
        description=(
            "User ID. A list is accepted for group chat: one memory view is "
            "extracted per participant (max 20 ids)."
        ),
    )
    agent_id: str | list[str] | None = Field(
        None,
        description=(
            "Agent ID (string or list, max 20). With "
            "`MEMOS_AGENT_MEMORY_ENABLED=true` each agent also gets an "
            "independent memory view; otherwise it is only a filterable tag."
        ),
    )
    group_id: str | None = Field(
        None,
        description=(
            "Group / conversation id. When set, a group-shared memory view is "
            "written in addition to the per-participant views."
        ),
    )
    app_id: str | None = Field(
        None,
        description=(
            "Calling application id (cloud `app_id`). Stored on every node as `app_id` "
            "and usable as a filter; an explicit `info.app_id` wins."
        ),
    )
    allow_public: bool = Field(
        False,
        description="Also write this conversation into tenant-wide public memory.",
    )
    allow_memory_view: (
        list[Literal["detail_factual", "preference", "profile", "event", "tool_memory"]]
        | None
    ) = Field(
        None,
        description=(
            "Write-side generation whitelist (cloud `allow_memory_view`). "
            "Omit to generate every type (historical behavior); an empty list "
            "generates nothing."
        ),
    )
    expires_at: str | None = Field(
        None,
        description="Optional ISO 8601 expiry time applied to the memories written by this request.",
    )
    session_id: str | None = Field(
        None,
        description="Session ID. If not provided, a default session will be used.",
    )
    task_id: str | None = Field(None, description="Task ID for monitoring async tasks")
    manager_user_id: str | None = Field(None, description="Manager User ID")
    project_id: str | None = Field(None, description="Project ID")

    # ==== Multi-cube writing ====
    writable_cube_ids: list[str] | None = Field(
        None, description="List of cube IDs user can write for multi-cube add"
    )

    # ==== Async control ====
    async_mode: Literal["async", "sync"] = Field(
        "async",
        description=(
            "Whether to add memory in async mode. "
            "Use 'async' to enqueue background add (non-blocking), "
            "or 'sync' to add memories in the current call. "
            "Default: 'async'."
        ),
    )

    mode: Literal["fast", "fine"] | None = Field(
        None,
        description=(
            "(Internal) Add mode used only when async_mode='sync'. "
            "If set to 'fast', the handler will use a fast add pipeline. "
            "Ignored when async_mode='async'."
        ),
    )

    # ==== Business tags & info ====
    custom_tags: list[str] | None = Field(
        None,
        description=(
            "Custom tags for this add request, e.g. ['Travel', 'family']. "
            "These tags can be used as filters in search."
        ),
    )
    custom_tags_match: bool | None = Field(
        None,
        description=(
            "Treat `custom_tags` as candidates and add the ones semantically close to each "
            "memory (embedding similarity, no extra LLM call). Omit to follow the server "
            "default (`CUSTOM_TAGS_SEMANTIC_MATCH`, off)."
        ),
    )

    custom_extract_prompt: dict[str, str] | None = Field(
        None,
        description=(
            "Per-request extraction guidance keyed by stage: detail_factual, preference, "
            "profile, event, tool_memory, image, document. The text only decides what "
            "to extract or ignore; the output structure stays the default. 'document' falls "
            "back to 'detail_factual'. Each value is at most 4000 characters."
        ),
    )

    info: dict[str, Any] | None = Field(
        None,
        description=(
            "Additional metadata for the add request. "
            "All keys can be used as filters in search. "
            "Example: "
            "{'agent_id': 'xxxxxx', "
            "'app_id': 'xxxx', "
            "'source_type': 'web', "
            "'source_url': 'https://example.com', "
            "'source_content': '西湖是杭州最著名的景点'}."
        ),
    )

    # ==== Input content ====
    messages: MessagesType | None = Field(
        None,
        description=(
            "List of messages to store. Supports: "
            "- system / user / assistant messages with 'content' and 'chat_time'; "
            "- tool messages including: "
            "  * tool_description (name, description, parameters), "
            "  * tool_input (call_id, name, argument), "
            "  * raw tool messages where content is str or list[str], "
            "  * tool_output with structured output items "
            "    (input_text / input_image / input_file, etc.). "
            "Also supports pure input items when there is no dialog."
        ),
    )
    # The public ``MessagesType`` is OpenAI's typed-dict union, which drops
    # cloud-only fields such as ``role_id`` / ``role_name`` / ``chat_time``.
    # Group chat needs them for per-participant attribution, so keep one raw
    # copy before validation (excluded from responses / OpenAPI).
    raw_messages: SkipJsonSchema[list[dict[str, Any]] | None] = Field(
        None, exclude=True, description="(Internal) raw message dicts before validation"
    )

    # ==== Chat history ====
    chat_history: MessageList | None = Field(
        None,
        description=(
            "Historical chat messages used internally by algorithms. "
            "If None, internal stored history will be used; "
            "if provided (even an empty list), this value will be used as-is."
        ),
    )

    # ==== Feedback flag ====
    is_feedback: bool = Field(
        False,
        description=("Whether this request represents user feedback. Default: False."),
    )


    # ==== Backward compatibility fields (will delete later) ====
    mem_cube_id: str | None = Field(
        None,
        description="(Deprecated) Target cube ID for this add request (optional for developer API).",
    )

    memory_content: str | None = Field(
        None,
        description="(Deprecated) Plain memory content to store. Prefer using `messages`.",
    )
    doc_path: str | None = Field(
        None,
        description="(Deprecated / internal) Path to document to store.",
    )
    source: str | None = Field(
        None,
        description=(
            "(Deprecated) Simple source tag of the memory. "
            "Prefer using `info.source_type` / `info.source_url`."
        ),
    )
    operation: list[PermissionDict] | None = Field(
        None,
        description="(Internal) Operation definitions for multi-cube write permissions.",
    )

    @field_validator("custom_extract_prompt", mode="before")
    @classmethod
    def _validate_custom_extract_prompt(cls, value: Any) -> dict[str, str] | None:
        from memos.mem_reader.custom_extract import normalize_custom_extract_prompt

        return normalize_custom_extract_prompt(value)

    @model_validator(mode="before")
    @classmethod
    def _preserve_raw_messages(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # Always derive from ``messages``: never accept a caller-supplied
        # sidecar that could bypass message validation / size limits.
        copied = dict(data)
        raw = copied.get("messages")
        copied["raw_messages"] = (
            [message for message in raw if isinstance(message, dict)]
            if isinstance(raw, list)
            else None
        )
        return copied

    @model_validator(mode="after")
    def _convert_deprecated_fields(self) -> "APIADDRequest":
        """
        Convert deprecated fields to new fields for backward compatibility.
        This keeps the API fully backward-compatible while allowing
        internal logic to use only the new fields.

        Rules:
            - mem_cube_id → writable_cube_ids
            - memory_content → messages
            - doc_path → messages (input_file)
            - source → info["source"]
            - operation → merged into writable_cube_ids (ignored otherwise)
        """
        # ---- async_mode / mode relationship ----
        if self.async_mode == "async" and self.mode is not None:
            logger.warning(
                "APIADDRequest.mode is ignored when async_mode='async'. "
                "Fast add pipeline is only available in sync mode."
            )
            self.mode = None

        # Convert mem_cube_id to writable_cube_ids (new field takes priority)
        if self.mem_cube_id:
            logger.warning(
                "APIADDRequest.mem_cube_id is deprecated and will be removed in a future version. "
                "Please use `writable_cube_ids` instead."
            )
            if not self.writable_cube_ids:
                self.writable_cube_ids = [self.mem_cube_id]

        # Handle deprecated operation field
        if self.operation:
            logger.warning(
                "APIADDRequest.operation is deprecated and will be removed. "
                "Use `writable_cube_ids` for multi-cube writes."
            )

        # Convert memory_content to messages (new field takes priority)
        if self.memory_content:
            logger.warning(
                "APIADDRequest.memory_content is deprecated. "
                "Use `messages` with a structured message instead."
            )
            if self.messages is None:
                self.messages = []
            self.messages.append(
                {
                    "type": "text",
                    "text": self.memory_content,
                }
            )

        # Handle deprecated doc_path
        if self.doc_path:
            logger.warning(
                "APIADDRequest.doc_path is deprecated. "
                "Use `messages` with an input_file item instead."
            )
            if self.messages is None:
                self.messages = []
            self.messages.append(
                {
                    "type": "file",
                    "file": {"path": self.doc_path},
                }
            )

        # Convert source to info.source_type (new field takes priority)
        if self.source:
            logger.warning(
                "APIADDRequest.source is deprecated. "
                "Use `info['source_type']` / `info['source_url']` instead."
            )
            if self.info is None:
                self.info = {}
            self.info.setdefault("source", self.source)

        return self

    @model_validator(mode="after")
    def _validate_owner_ids(self) -> "APIADDRequest":
        """Bound the group-chat id lists (cloud: max 20 per request)."""
        from memos.memories.textual.owner import normalize_id_list

        normalize_id_list(self.user_id)
        normalize_id_list(self.agent_id)
        return self

    @model_validator(mode="after")
    def _validate_multimodal_limits(self) -> "APIADDRequest":
        """Enforce the cloud file/token caps and force async for multimodal input."""
        from memos.api.request_validation import inspect_add_messages

        inspection = inspect_add_messages(self.messages)
        if inspection.has_multimodal:
            # Cloud contract: file memories take long to process, so a multimodal
            # request always runs asynchronously even if the caller asked for sync.
            self.async_mode = "async"
        return self

    @model_validator(mode="after")
    def _merge_app_id_into_info(self) -> "APIADDRequest":
        if self.app_id and "app_id" not in (self.info or {}):
            self.info = {**(self.info or {}), "app_id": self.app_id}
        return self


class APIExtractRequest(BaseRequest):
    """Request model for the dry-run extraction endpoint.

    Same input surface as ``APIADDRequest`` minus feedback/upload flags, but the
    reader output is returned directly and **nothing is persisted**. Mirrors the
    cloud ``extract/memory`` contract: extract facts/preferences only.
    """

    user_id: str = Field(..., description="User ID")
    session_id: str | None = Field(
        None,
        description="Session ID. If not provided, a default session will be used.",
    )
    task_id: str | None = Field(None, description="Task ID for call correlation")
    manager_user_id: str | None = Field(None, description="Manager User ID")
    project_id: str | None = Field(None, description="Project ID")

    writable_cube_ids: list[str] | None = Field(
        None, description="List of cube IDs to extract against for multi-cube reads"
    )
    mem_cube_id: str | None = Field(
        None,
        description="(Deprecated) Single cube ID. Prefer `writable_cube_ids`.",
    )

    mode: Literal["fast", "fine"] = Field(
        "fine",
        description=(
            "Extraction pipeline: 'fine' runs the full reader (facts + preferences), "
            "'fast' runs the lightweight path. Default: 'fine'."
        ),
    )
    extraction_types: list[Literal["memory", "preference"]] | None = Field(
        None,
        description=(
            "Cloud `extraction_types`: keep only these kinds of output. Unset returns "
            "everything the pipeline produced."
        ),
    )

    custom_tags: list[str] | None = Field(
        None, description="Custom tags to attach to extracted memories."
    )
    custom_extract_prompt: dict[str, str] | None = Field(
        None,
        description=(
            "Per-request extraction guidance keyed by stage: detail_factual, preference, "
            "profile, event, tool_memory, image, document. The text only decides what "
            "to extract or ignore; the output structure stays the default. 'document' falls "
            "back to 'detail_factual'. Each value is at most 4000 characters."
        ),
    )
    info: dict[str, Any] | None = Field(
        None, description="Additional metadata used by extraction and filters."
    )
    messages: MessagesType = Field(..., description="Messages to extract memories from.")
    raw_messages: SkipJsonSchema[list[dict[str, Any]] | None] = Field(
        None, exclude=True, description="(Internal) raw message dicts before validation"
    )
    chat_history: MessageList | None = Field(
        None, description="Historical chat messages used by extraction algorithms."
    )

    @field_validator("custom_extract_prompt", mode="before")
    @classmethod
    def _validate_custom_extract_prompt(cls, value: Any) -> dict[str, str] | None:
        from memos.mem_reader.custom_extract import normalize_custom_extract_prompt

        return normalize_custom_extract_prompt(value)

    @model_validator(mode="before")
    @classmethod
    def _preserve_raw_messages(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # Always derive from ``messages``: never accept a caller-supplied
        # sidecar that could bypass message validation / size limits.
        copied = dict(data)
        raw = copied.get("messages")
        copied["raw_messages"] = (
            [message for message in raw if isinstance(message, dict)]
            if isinstance(raw, list)
            else None
        )
        return copied

    @model_validator(mode="after")
    def _convert_deprecated_fields(self) -> "APIExtractRequest":
        if self.mem_cube_id and not self.writable_cube_ids:
            logger.warning(
                "APIExtractRequest.mem_cube_id is deprecated. Use `writable_cube_ids` instead."
            )
            self.writable_cube_ids = [self.mem_cube_id]
        return self

    @model_validator(mode="after")
    def _validate_limits(self) -> "APIExtractRequest":
        from memos.api.request_validation import inspect_add_messages

        inspect_add_messages(self.messages)
        return self


class APIFeedbackRequest(BaseRequest):
    """Request model for processing feedback info."""

    user_id: str = Field(..., description="User ID")
    agent_id: str | None = Field(None, description="Agent the feedback belongs to")
    app_id: str | None = Field(None, description="Calling application id")
    allow_public: bool = Field(False, description="Allow the correction to become public memory")
    session_id: str | None = Field(
        "default_session", description="Session ID for soft-filtering memories"
    )
    task_id: str | None = Field(None, description="Task ID for monitoring async tasks")
    history: MessageList | None = Field(..., description="Chat history")
    retrieved_memory_ids: list[str] | None = Field(
        None, description="Retrieved memory ids at last turn"
    )
    feedback_content: str | None = Field(..., description="Feedback content to process")
    feedback_time: str | None = Field(None, description="Feedback time")
    writable_cube_ids: list[str] | None = Field(
        None, description="List of cube IDs user can write for multi-cube add"
    )
    async_mode: Literal["sync", "async"] = Field(
        "async", description="feedback mode: sync or async"
    )
    corrected_answer: bool = Field(False, description="Whether need return corrected answer")
    info: dict[str, Any] | None = Field(
        None,
        description=(
            "Additional metadata for the add request. "
            "All keys can be used as filters in search. "
            "Example: "
            "{'agent_id': 'xxxxxx', "
            "'app_id': 'xxxx', "
            "'source_type': 'web', "
            "'source_url': 'https://example.com', "
            "'source_content': 'West Lake is the most famous scenic spot in Hangzhou'}."
        ),
    )
    # ==== mem_cube_id is NOT enabled====
    mem_cube_id: str | None = Field(
        None,
        description=(
            "(Deprecated) Single cube ID to search in. "
            "Prefer `readable_cube_ids` for multi-cube search."
        ),
    )


class APIChatCompleteRequest(BaseRequest):
    """Request model for chat operations."""

    user_id: str = Field(..., description="User ID")
    query: str = Field(..., description="Chat query message")
    readable_cube_ids: list[str] | None = Field(
        None, description="List of cube IDs user can read for multi-cube chat"
    )
    writable_cube_ids: list[str] | None = Field(
        None, description="List of cube IDs user can write for multi-cube chat"
    )
    history: MessageList | None = Field(None, description="Chat history")
    mode: SearchMode = Field(SearchMode.FAST, description="search mode: fast, fine, or mixture")
    system_prompt: str | None = Field(None, description="Base system prompt to use for chat")
    top_k: int = Field(10, ge=1, le=TOP_K_MAX, description="Number of results to return (max 25)")
    session_id: str | None = Field(None, description="Session ID for soft-filtering memories")
    include_preference: bool = Field(True, description="Whether to handle preference memory")
    pref_top_k: int = Field(6, description="Number of preference results to return")
    model_name_or_path: str | None = Field(None, description="Model name to use for chat")
    max_tokens: int | None = Field(None, description="Max tokens to generate")
    temperature: float | None = Field(None, description="Temperature for sampling")
    top_p: float | None = Field(None, description="Top-p (nucleus) sampling parameter")
    add_message_on_answer: bool = Field(True, description="Add dialogs to memory after chat")
    manager_user_id: str | None = Field(None, description="Manager User ID")
    project_id: str | None = Field(None, description="Project ID")
    memory_limit_number: int | None = Field(
        None,
        ge=1,
        le=TOP_K_MAX,
        description=(
            "Cloud-compatible alias for the recall size. When set it overrides "
            "`top_k`. Range 1-25, aligned with the cloud recall cap."
        ),
    )
    tags: list[str] | None = Field(
        None, description="Custom tags written back with the chat turn (write-back only)."
    )
    info: dict[str, Any] | None = Field(
        None, description="Business metadata written back with the chat turn (write-back only)."
    )
    agent_id: str | None = Field(
        None, description="Agent ID written back with the chat turn (merged into `info`)."
    )
    app_id: str | None = Field(
        None, description="App ID written back with the chat turn (merged into `info`)."
    )
    allow_public: bool = Field(
        False,
        description="Also write the chat turn into tenant-wide public memory (write-back only).",
    )
    relativity: float = Field(
        0.45,
        ge=0,
        description=(
            "Relevance threshold for recalled memories. "
            "Only memories with metadata.relativity >= relativity will be returned. "
            "Use 0 to disable threshold filtering. Default: 0.45."
        ),
    )

    # ==== Filter conditions ====
    filter: dict[str, Any] | None = Field(
        None,
        description=(
            "Optional metadata filter applied to recalled memories. Supports nested "
            '"and" / "or" conditions and comparison operators such as "gt" for fields '
            'like "id", "created_at", or other indexed metadata. Example: '
            '{"and": [{"id": "uuid-xxx"}, {"created_at": {"gt": "2024-01-01"}}]}'
        ),
    )

    # ==== Extended capabilities ====
    internet_search: bool = Field(False, description="Whether to use internet search")
    threshold: float = Field(0.5, description="Threshold for filtering references")

    # ==== Backward compatibility ====
    mem_cube_id: str | None = Field(None, description="Cube ID to use for chat")
    moscube: bool = Field(
        False, description="(Deprecated) Whether to use legacy MemOSCube pipeline"
    )

    @model_validator(mode="after")
    def _apply_memory_limit_alias(self):
        """`memory_limit_number` is the cloud name for the recall cap."""
        if self.memory_limit_number is not None:
            self.top_k = self.memory_limit_number
        return self

    @field_validator("filter")
    @classmethod
    def _normalize_filter_fields(cls, value):
        from memos.search.filter_mapping import normalize_filter

        return normalize_filter(value)


class AddStatusRequest(BaseRequest):
    """Request model for checking add status."""

    mem_cube_id: str = Field(..., description="Cube ID")
    user_id: str | None = Field(None, description="User ID")
    session_id: str | None = Field(None, description="Session ID")


class GetMemoryByIdsRequest(BaseModel):
    """Body for ``POST /product/get_memory_by_ids``.

    Contract v2 uses this object shape. The legacy bare-array shape
    (``["id-1", "id-2"]``) is still accepted during the transition, but it is
    not part of the contract.
    """

    memory_ids: list[str] = Field(
        default_factory=list,
        description="Memory IDs to fetch. The response contains the subset that exists.",
    )
    include_sources: bool = Field(
        False,
        description=(
            "Whether to include metadata.sources (conversation history) in the response. "
            "Off by default: a single preference can carry hundreds of MB of sources."
        ),
    )


class GetMemoryRequest(BaseRequest):
    """Request model for getting memories."""

    mem_cube_id: str | None = Field(None, description="Cube ID; defaults to `user_id`")
    user_id: str | None = Field(None, description="User ID")
    agent_id: str | list[str] | None = Field(
        None, description="Only memories related to these agents (cloud `agent_id`)"
    )
    include_preference: bool = Field(True, description="Whether to return preference memory")
    include_tool_memory: bool = Field(True, description="Whether to return tool memory")
    include_sources: bool = Field(
        False,
        description=(
            "Whether to include metadata.sources (conversation history) in the response. "
            "Off by default: a single preference can carry hundreds of MB of sources."
        ),
    )
    include_memory_view: (
        list[Literal["detail_factual", "preference", "profile", "event", "tool_memory"]]
        | None
    ) = Field(
        None,
        description=(
            "Cloud-compatible view whitelist. When provided it takes precedence over "
            "`include_preference` / `include_tool_memory`; "
            "`profile` adds a `profile_mem` bucket and `event` splits events out of `text_mem`."
        ),
    )
    filter: dict[str, Any] | None = Field(
        None,
        description="Optional metadata filter applied when retrieving memories.",
    )
    page: int | None = Field(1, ge=1, description="Page number, starting at 1 (cloud default 1)")
    page_size: int | None = Field(
        10, ge=1, description="Items per page (cloud default 10); None exports everything"
    )

    @model_validator(mode="after")
    def _resolve_tenant_and_views(self) -> "GetMemoryRequest":
        self.mem_cube_id = (self.mem_cube_id or "").strip() or self.user_id
        views = self.include_memory_view
        if views is not None:
            self.include_preference = "preference" in views
            self.include_tool_memory = "tool_memory" in views
            return self

    @field_validator("filter")
    @classmethod
    def _normalize_filter_fields(cls, value):
        from memos.search.filter_mapping import normalize_filter

        return normalize_filter(value)


class GetMemoryDashboardRequest(GetMemoryRequest):
    """Request model for getting memories for dashboard."""

    page: int | None = Field(
        None,
        description="Page number (starts from 1). If None, exports all data without pagination.",
    )
    page_size: int | None = Field(
        None, description="Number of items per page. If None, exports all data without pagination."
    )


class GetMemoryGraphRequest(BaseRequest):
    """Request model for the dashboard graph view (nodes plus PARENT/MERGED_TO edges)."""

    mem_cube_id: str | None = Field(None, description="Cube ID; defaults to `user_id`")
    user_id: str | None = Field(None, description="User ID; fallback for `mem_cube_id`")
    memory_types: list[str] | None = Field(
        None,
        description="Only return nodes of these memory types; omitted or empty means all types.",
    )
    status: list[str] = Field(
        default_factory=lambda: ["activated"],
        description="Only return nodes in these lifecycle statuses; defaults to activated.",
    )
    limit: int = Field(
        2000, ge=1, le=10_000, description="Maximum number of nodes and edges to return."
    )
    label_max: int = Field(
        200, ge=1, le=4000, description="Maximum label length stored on each node."
    )
    include_edges: bool = Field(True, description="Return PARENT/MERGED_TO edges as well.")

    @model_validator(mode="after")
    def _resolve_tenant_and_defaults(self) -> "GetMemoryGraphRequest":
        self.mem_cube_id = (self.mem_cube_id or "").strip() or (self.user_id or "").strip()
        if not self.mem_cube_id:
            raise ValueError("mem_cube_id or user_id is required")
        if not self.memory_types:
            self.memory_types = None
        if not self.status:
            self.status = ["activated"]
        return self


class GetEventMemoriesRequest(BaseRequest):
    """Request model for event and Dream memories, paginated by created_at."""

    mem_cube_id: str | None = Field(None, description="Cube ID; defaults to `user_id`")
    user_id: str | None = Field(None, description="User ID; fallback for `mem_cube_id`")
    memory_types: list[str] = Field(
        default_factory=lambda: ["EventMemory", "Context", "InsightMemory", "DreamDiary"],
        description="Memory types to include. Empty means all types.",
    )
    status: list[str] | None = Field(
        None,
        description=(
            "Only return nodes in these lifecycle statuses; omitted or empty means every "
            "status except deleted (Dream diaries use completed/context_only)."
        ),
    )
    search_query: str | None = Field(
        None, description="Case-sensitive substring matched against the memory text."
    )
    page: int = Field(1, ge=1, description="Page number, starting at 1.")
    page_size: int = Field(20, ge=1, le=200, description="Items per page.")

    @model_validator(mode="after")
    def _resolve_tenant_and_defaults(self) -> "GetEventMemoriesRequest":
        self.mem_cube_id = (self.mem_cube_id or "").strip() or (self.user_id or "").strip()
        if not self.mem_cube_id:
            raise ValueError("mem_cube_id or user_id is required")
        if not self.memory_types:
            self.memory_types = []
        if not self.status:
            self.status = None
        self.search_query = (self.search_query or "").strip() or None
        return self


class DeleteMemoryRequest(BaseRequest):
    """Request model for deleting memories."""

    writable_cube_ids: list[str] | None = Field(None, description="Writable cube IDs")
    memory_ids: list[str] | None = Field(None, description="Memory IDs")
    file_ids: list[str] | None = Field(None, description="File IDs")
    filter: dict[str, Any] | None = Field(
        None,
        description="Optional metadata filter applied when selecting memories for deletion.",
    )
    user_id: str | None = Field(
        None,
        description="Quick delete condition: remove memories for this user_id.",
    )
    agent_id: str | None = Field(
        None,
        description=(
            "Quick delete condition: remove the independent memories owned by this agent "
            "(owner_type=agent). The tenant comes from `writable_cube_ids`, or from "
            "`user_id` when no cube is named. Memories of users that merely carry this "
            "agent_id as a compatibility property are kept."
        ),
    )
    session_id: str | None = Field(
        None,
        description="Quick delete condition: remove memories for this session_id.",
    )
    conversation_id: str | None = Field(
        None,
        description="Alias of session_id for backward compatibility.",
    )
    auto_cleanup_working: bool | None = Field(
        False,
        description=(
            "(Internal) Whether to automatically delete related WorkingMemory nodes "
            "based on working_binding metadata when deleting by memory_ids."
        ),
    )

    @field_validator("agent_id")
    @classmethod
    def _strip_agent_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("agent_id must not be empty")
        return value

    @field_validator("filter")
    @classmethod
    def _normalize_filter_fields(cls, value):
        from memos.search.filter_mapping import normalize_filter

        # Keep an explicit empty object so the handler can report a precise error.
        if value == {}:
            return value
        return normalize_filter(value)

    @model_validator(mode="after")
    def normalize_session_alias(self) -> "DeleteMemoryRequest":
        """Normalize conversation_id to session_id."""
        if self.conversation_id and self.session_id and self.conversation_id != self.session_id:
            raise ValueError("conversation_id and session_id must be the same when both are set")
        if self.session_id is None and self.conversation_id is not None:
            self.session_id = self.conversation_id
        return self


class SuggestionRequest(BaseRequest):
    """Request model for getting suggestion queries."""

    user_id: str = Field(..., description="User ID")
    mem_cube_id: str = Field(..., description="Cube ID")
    language: Literal["zh", "en"] = Field("zh", description="Language for suggestions")
    message: MessagesType | None = Field(None, description="List of messages to store.")


# ─── MemOS Client Response Models ──────────────────────────────────────────────


class MessageDetail(BaseModel):
    """Individual message detail model based on actual API response."""

    model_config = {"extra": "allow"}


class MemoryDetail(BaseModel):
    """Individual memory detail model based on actual API response."""

    model_config = {"extra": "allow"}


class FileDetail(BaseModel):
    """Individual file detail model based on actual API response."""

    model_config = {"extra": "allow"}


class GetMessagesData(BaseModel):
    """Data model for get messages response based on actual API."""

    message_detail_list: list[MessageDetail] = Field(
        default_factory=list, alias="message_detail_list", description="List of message details"
    )


class GetCreateKnowledgebaseData(BaseModel):
    """Data model for create knowledgebase response based on actual API."""

    id: str = Field(..., description="Knowledgebase id")


class SearchMemoryData(BaseModel):
    """Data model for search memory response based on actual API."""

    memory_detail_list: list[MemoryDetail] = Field(
        default_factory=list, alias="memory_detail_list", description="List of memory details"
    )
    message_detail_list: list[MessageDetail] | None = Field(
        None, alias="message_detail_list", description="List of message details (usually None)"
    )
    preference_detail_list: list[MessageDetail] | None = Field(
        None,
        alias="preference_detail_list",
        description="List of preference details (usually None)",
    )
    tool_memory_detail_list: list[MessageDetail] | None = Field(
        None,
        alias="tool_memory_detail_list",
        description="List of tool_memor details (usually None)",
    )
    profile_detail_list: list[MemoryDetail] | None = Field(
        None, alias="profile_detail_list", description="List of profile memory details"
    )
    event_detail_list: list[MemoryDetail] | None = Field(
        None, alias="event_detail_list", description="List of event memory details"
    )
    preference_note: str = Field(
        None, alias="preference_note", description="String of preference_note"
    )


class GetKnowledgebaseFileData(BaseModel):
    """Data model for search memory response based on actual API."""

    file_detail_list: list[FileDetail] = Field(
        default_factory=list, alias="file_detail_list", description="List of files details"
    )
    total: int | None = Field(None, description="Total number of matching files")
    page: int | None = Field(None, description="Current page number")
    page_size: int | None = Field(None, alias="page_size", description="Page size")


class GetMemoryData(BaseModel):
    """Data model for search memory response based on actual API."""

    memory_detail_list: list[MemoryDetail] = Field(
        default_factory=list, alias="memory_detail_list", description="List of memory details"
    )
    preference_detail_list: list[MessageDetail] | None = Field(
        None, alias="preference_detail_list", description="List of preference detail"
    )
    tool_memory_detail_list: list[MemoryDetail] | None = Field(
        None, alias="tool_memory_detail_list", description="List of tool memory details"
    )
    profile_detail_list: list[MemoryDetail] | None = Field(
        None, alias="profile_detail_list", description="List of profile memory details"
    )
    event_detail_list: list[MemoryDetail] | None = Field(
        None, alias="event_detail_list", description="List of event memory details"
    )
    total: int | None = Field(None, description="Total number of memories")
    size: int | None = Field(None, description="Page size")
    current: int | None = Field(None, description="Current page number")
    pages: int | None = Field(None, description="Total number of pages")


class AddMessageData(BaseModel):
    """Data model for add message response based on actual API."""

    success: bool = Field(..., description="Operation success status")
    task_id: str = Field(..., description="Operation task_id")
    status: str = Field(..., description="Operation task status")


class DeleteMessageData(BaseModel):
    """Data model for delete  Message based on actual API."""

    success: bool = Field(..., description="Operation success status")


class ChatMessageData(BaseModel):
    """Data model for chat  Message based on actual API."""

    response: str = Field(..., description="Operation response")


class GetTaskStatusMessageData(BaseModel):
    """Data model for task status Message based on actual API."""

    status: str = Field(..., description="Operation task status")


class GetTaskStatusData(BaseModel):
    """Current OpenMem task status response data."""

    task_id: str = Field(..., description="Task identifier")
    status: str = Field(..., description="Operation task status")
    memory_views: dict[str, Any] | None = Field(
        None, alias="memory_views", description="Memory view changes produced by the task"
    )


# ─── MemOS Response Models (Similar to OpenAI ChatCompletion) ──────────────────


class MemOSGetMessagesResponse(BaseModel):
    """Response model for get messages operation based on actual API."""

    code: int = Field(..., description="Response status code")
    message: str = Field(..., description="Response message")
    data: GetMessagesData = Field(..., description="Messages data")

    @property
    def messages(self) -> list[MessageDetail]:
        """Convenient access to message list."""
        return self.data.message_detail_list


class MemOSSearchResponse(BaseModel):
    """Response model for search memory operation based on actual API."""

    code: int = Field(..., description="Response status code")
    message: str = Field(..., description="Response message")
    data: SearchMemoryData = Field(..., description="Search results data")

    @property
    def memories(self) -> list[MemoryDetail]:
        """Convenient access to memory list."""
        return self.data.memory_detail_list

    @property
    def preferences(self) -> list[MemoryDetail]:
        """Convenient access to preference list."""
        return self.data.preference_detail_list

    @property
    def tool_memories(self) -> list[MemoryDetail]:
        """Convenient access to tool_memory list."""
        return self.data.tool_memory_detail_list


class MemOSDeleteKnowledgebaseResponse(BaseModel):
    """Response model for delete knowledgebase operation based on actual API."""

    code: int = Field(..., description="Response status code")
    message: str = Field(..., description="Response message")
    data: DeleteMessageData = Field(..., description="delete results data")

    @property
    def success(self) -> bool:
        """Convenient access to success status."""
        return self.data.success


class MemOSDeleteMemoryResponse(BaseModel):
    """Response model for delete knowledgebase operation based on actual API."""

    code: int = Field(..., description="Response status code")
    message: str = Field(..., description="Response message")
    data: DeleteMessageData = Field(..., description="delete results data")

    @property
    def success(self) -> bool:
        """Convenient access to success status."""
        return self.data.success


class MemOSChatResponse(BaseModel):
    """Response model for chat operation based on actual API."""

    code: int = Field(..., description="Response status code")
    message: str = Field(..., description="Response message")
    data: ChatMessageData = Field(..., description="chat results data")

    @property
    def response(self) -> str:
        """Convenient access to success status."""
        return self.data.response


class MemOSGetTaskStatusResponse(BaseModel):
    """Response model for get task status operation based on actual API."""

    code: int = Field(..., description="Response status code")
    message: str = Field(..., description="Response message")
    data: GetTaskStatusData = Field(..., description="Task status data")

    @property
    def messages(self) -> list[GetTaskStatusData]:
        """Backward-compatible list access to task status data."""
        return [self.data]


class MemOSCreateKnowledgebaseResponse(BaseModel):
    """Response model for create knowledgebase operation based on actual API."""

    code: int = Field(..., description="Response status code")
    message: str = Field(..., description="Response message")
    data: GetCreateKnowledgebaseData = Field(..., description="Messages data")

    @property
    def knowledgebase_id(self) -> str:
        """Convenient access to knowledgebase id."""
        return self.data.id


class MemOSAddKnowledgebaseFileResponse(BaseModel):
    """Response model for add knowledgebase-file operation based on actual API."""

    code: int = Field(..., description="Response status code")
    message: str = Field(..., description="Response message")
    data: list[dict[str, Any]]

    @property
    def memories(self) -> list[dict[str, Any]]:
        """Convenient access to memory list."""
        return self.data


class MemOSGetMemoryResponse(BaseModel):
    """Response model for get memory operation based on actual API."""

    code: int = Field(..., description="Response status code")
    message: str = Field(..., description="Response message")
    data: GetMemoryData = Field(..., description="Get results data")

    @property
    def memories(self) -> list[MemoryDetail]:
        """Convenient access to memory list."""
        return self.data.memory_detail_list

    @property
    def preferences(self) -> list[MessageDetail] | None:
        """Convenient access to preference list."""
        return self.data.preference_detail_list


class MemOSGetKnowledgebaseFileResponse(BaseModel):
    """Response model for get KnowledgebaseFile operation based on actual API."""

    code: int = Field(..., description="Response status code")
    message: str = Field(..., description="Response message")
    data: GetKnowledgebaseFileData = Field(..., description="Get results data")

    @property
    def files(self) -> list[FileDetail]:
        """Convenient access to file list."""
        return self.data.file_detail_list


class MemOSAddResponse(BaseModel):
    """Response model for add message operation based on actual API."""

    code: int = Field(..., description="Response status code")
    message: str = Field(..., description="Response message")
    data: AddMessageData = Field(..., description="Add operation data")

    @property
    def success(self) -> bool:
        """Convenient access to success status."""
        return self.data.success

    @property
    def task_id(self) -> str:
        """Convenient access to task_id status."""
        return self.data.task_id

    @property
    def status(self) -> str:
        """Convenient access to status status."""
        return self.data.status


class MemOSAddFeedBackResponse(BaseModel):
    """Response model for add feedback operation based on actual API."""

    code: int = Field(..., description="Response status code")
    message: str = Field(..., description="Response message")
    data: AddMessageData = Field(..., description="Add operation data")

    @property
    def success(self) -> bool:
        """Convenient access to success status."""
        return self.data.success

    @property
    def task_id(self) -> str:
        """Convenient access to task_id status."""
        return self.data.task_id

    @property
    def status(self) -> str:
        """Convenient access to status status."""
        return self.data.status


# ─── Scheduler Status Models ───────────────────────────────────────────────────


class StatusRequest(BaseRequest):
    """Request model for querying scheduler task status."""

    user_id: str = Field(..., description="User ID")
    task_id: str | None = Field(None, description="Optional Task ID to query a specific task")


class StatusResponseItem(BaseModel):
    """Individual task status item."""

    task_id: str = Field(..., description="The ID of the task")
    status: Literal["in_progress", "completed", "waiting", "failed", "cancelled"] = Field(
        ..., description="The current status of the task"
    )


class StatusResponse(BaseResponse[list[StatusResponseItem]]):
    """Response model for scheduler status operations."""

    message: str = "Memory get status successfully"


class TaskQueueData(BaseModel):
    """Queue-level metrics for scheduler tasks."""

    user_id: str = Field(..., description="User ID the query is scoped to")
    user_name: str | None = Field(None, description="User name if available")
    mem_cube_id: str | None = Field(
        None, description="MemCube ID if a single cube is targeted; otherwise None"
    )
    stream_keys: list[str] = Field(..., description="Matched Redis stream keys for this user")
    users_count: int = Field(..., description="Distinct users currently present in queue streams")
    pending_tasks_count: int = Field(
        ..., description="Count of pending (delivered, not acked) tasks"
    )
    remaining_tasks_count: int = Field(..., description="Count of enqueued tasks (xlen)")
    pending_tasks_detail: list[str] = Field(
        ..., description="Per-stream pending counts, formatted as '{stream_key}:{count}'"
    )
    remaining_tasks_detail: list[str] = Field(
        ..., description="Per-stream remaining counts, formatted as '{stream_key}:{count}'"
    )


class TaskQueueResponse(BaseResponse[TaskQueueData]):
    """Response model for scheduler task queue status."""

    message: str = "Scheduler task queue status retrieved successfully"


class TaskSummary(BaseModel):
    """Aggregated counts of tasks by status."""

    waiting: int = Field(0, description="Number of tasks waiting to run")
    in_progress: int = Field(0, description="Number of tasks currently running")
    pending: int = Field(
        0, description="Number of tasks fetched by workers but not yet acknowledged"
    )
    completed: int = Field(0, description="Number of tasks completed")
    failed: int = Field(0, description="Number of tasks failed")
    cancelled: int = Field(0, description="Number of tasks cancelled")
    total: int = Field(0, description="Total number of tasks counted")


class AllStatusResponseData(BaseModel):
    """Aggregated scheduler status metrics."""

    scheduler_summary: TaskSummary = Field(
        ..., description="Aggregated status for scheduler-managed tasks"
    )
    all_tasks_summary: TaskSummary = Field(
        ..., description="Aggregated status for all tracked tasks"
    )


class AllStatusResponse(BaseResponse[AllStatusResponseData]):
    """Response model for full scheduler status operations."""

    message: str = "Scheduler status summary retrieved successfully"


# ─── Internal API Endpoints Models (for internal use) ───────────────────────────────────────────────────


class GetUserNamesByMemoryIdsRequest(BaseRequest):
    """Request model for getting user names by memory ids."""

    memory_ids: list[str] = Field(..., description="Memory IDs")


class GetUserNamesByMemoryIdsResponse(BaseResponse[dict[str, str | None]]):
    """Response model for getting user names by memory ids."""


class ExistMemCubeIdRequest(BaseRequest):
    """Request model for checking if mem cube id exists."""

    mem_cube_id: str = Field(..., description="Mem cube ID")


class ExistMemCubeIdResponse(BaseResponse[dict[str, bool]]):
    """Response model for checking if mem cube id exists."""


class DeleteMemoryByRecordIdRequest(BaseRequest):
    """Request model for deleting memory by record id."""

    mem_cube_id: str = Field(..., description="Mem cube ID")
    record_id: str = Field(..., description="Record ID")
    hard_delete: bool = Field(False, description="Hard delete")


class DeleteMemoryByRecordIdResponse(BaseResponse[dict]):
    """Response model for deleting memory by record id."""


class RecoverMemoryByRecordIdRequest(BaseRequest):
    """Request model for recovering memory by record id."""

    mem_cube_id: str = Field(..., description="Mem cube ID")
    delete_record_id: str = Field(..., description="Delete record ID")


class RecoverMemoryByRecordIdResponse(BaseResponse[dict]):
    """Response model for recovering memory by record id."""


# ─── Cloud parity: update / status / knowledge base / profile ──────────────────


MEMORY_VIEW_VALUES = ("detail_factual", "preference", "profile", "event", "tool_memory")


class UpdateMemoryRequest(BaseRequest):
    """Request model for manually updating one memory.

    Mirrors the cloud ``update/memory`` contract. ``content`` and ``title``
    rewrite the memory (the embedding is recomputed and both the graph and the
    vector payload are updated); ``status`` only transitions lifecycle state.
    """

    memory_id: str = Field(..., description="Memory id from search/get results")
    content: str | None = Field(None, description="New memory content")
    title: str | None = Field(None, description="New memory title (stored as `key`)")
    status: Literal["activated", "archived", "frozen", "expired"] | None = Field(
        None, description="New lifecycle status"
    )
    user_id: str | None = Field(
        None,
        description=(
            "Optional ownership check. When provided the memory must belong to "
            "this user (node `user_name` or `user_id`)."
        ),
    )
    mem_cube_id: str | None = Field(
        None, description="Optional tenant/cube id used to scope the lookup"
    )
    metadata: dict[str, Any] | None = Field(
        None, description="Optional extra metadata fields to merge (does not rewrite content)"
    )

    @model_validator(mode="after")
    def _require_change(self) -> "UpdateMemoryRequest":
        if not any(
            value is not None for value in (self.content, self.title, self.status, self.metadata)
        ):
            raise ValueError("content, title, status or metadata is required")
        if self.content is not None and not self.content.strip():
            raise ValueError("content must not be empty")
        return self


class UpdateMemoryResponse(BaseResponse[dict]):
    """Response model for update memory operations."""


class AddStatusRequestV2(BaseRequest):
    """Request model for ``/product/add_status`` (cloud ``get/status``)."""

    user_id: str | None = Field(None, description="User ID the task belongs to")
    task_id: str = Field(..., description="Business task id returned by add")
    mem_cube_id: str | None = Field(None, description="Optional cube id")


class AddStatusResponseV2(BaseResponse[dict]):
    """Response model for the add status endpoint."""


class GetMessageRequest(BaseRequest):
    """Request model for ``/product/get_message`` (cloud ``get/message``)."""

    user_id: str = Field(..., min_length=1, description="User the conversation belongs to")
    conversation_id: str | None = Field(
        None, description="Conversation to read; same as `session_id` on add"
    )
    session_id: str | None = Field(None, description="Alias of `conversation_id`")
    mem_cube_id: str | None = Field(
        None, description="Tenant / cube the messages were added to; defaults to `user_id`"
    )
    message_limit_number: int = Field(
        6, ge=1, le=50, description="How many of the latest messages to return, oldest first"
    )

    @model_validator(mode="after")
    def _resolve_conversation(self) -> "GetMessageRequest":
        self.user_id = self.user_id.strip()
        if not self.user_id:
            raise ValueError("user_id must not be blank")
        self.mem_cube_id = (self.mem_cube_id or "").strip() or None
        if self.conversation_id and self.session_id and self.conversation_id != self.session_id:
            raise ValueError("conversation_id and session_id must be the same when both are set")
        self.conversation_id = self.conversation_id or self.session_id
        if not self.conversation_id:
            raise ValueError("conversation_id is required")
        return self


class GetMessageResponse(BaseResponse[dict]):
    """Response model for the get message endpoint."""


class RerankRequest(BaseRequest):
    """Request model for ``/product/rerank`` (cloud ``rerank``)."""

    query: str = Field(..., min_length=1, description="Query the documents are scored against")
    documents: list[str] = Field(..., min_length=1, description="Candidate texts, each one memory")
    model: str | None = Field(
        None, description="Accepted for contract compatibility; the deployment's reranker is used"
    )
    top_n: int | None = Field(None, ge=1, description="Return only the top N; all by default")


class RerankResponse(BaseResponse[dict]):
    """Response model for the rerank endpoint."""


class KnowledgeBaseCreateRequest(BaseRequest):
    """Create a knowledge base (cloud ``create/knowledgebase``)."""

    user_id: str | None = Field(None, description="Owner user id")
    mem_cube_id: str | None = Field(None, description="Tenant / cube id")
    knowledgebase_name: str = Field(..., min_length=1, description="Knowledge base name")
    knowledgebase_description: str | None = Field(None, description="Description")


class KnowledgeBaseDeleteRequest(BaseRequest):
    """Request model for ``/product/delete_knowledgebase`` (cloud ``delete/knowledgebase``)."""

    user_id: str | None = Field(None, description="Owner user id")
    mem_cube_id: str | None = Field(None, description="Tenant / cube id")
    knowledgebase_id: str = Field(..., description="Knowledge base id")
    delete_all: bool = Field(
        False,
        description=(
            "Cloud `delete_all`. Accepted for contract compatibility: our bases are "
            "tenant-scoped and have no project link, so both values delete the base and files."
        ),
    )


class KnowledgeBaseUpdateRequest(BaseRequest):
    """Rename a knowledge base or replace its description."""

    user_id: str | None = Field(None, description="Owner user id")
    mem_cube_id: str | None = Field(None, description="Tenant / cube id")
    knowledgebase_id: str = Field(..., description="Knowledge base id")
    knowledgebase_name: str | None = Field(None, min_length=1, description="New name")
    knowledgebase_description: str | None = Field(None, description="New description")


class KnowledgeBaseListRequest(BaseRequest):
    """List / get knowledge bases."""

    user_id: str | None = Field(None, description="Owner user id")
    mem_cube_id: str | None = Field(None, description="Tenant / cube id")
    knowledgebase_id: str | None = Field(None, description="Get one knowledge base")
    page: int | None = Field(None, ge=1, description="Page number (1-based)")
    page_size: int | None = Field(None, ge=1, le=200, description="Page size")


class KnowledgeBaseFileAddRequest(BaseRequest):
    """Add files to a knowledge base.

    ``file`` accepts cloud's JSON form: a list of ``{"content": "<url>"}`` or
    ``{"name": ..., "content": "<base64, data URL or url>", "type": "document"}``.
    A file's own ``type`` wins over the request-level ``type``. Multipart upload
    uses the same route with ``upload`` files (handled by the router).
    """

    user_id: str | None = Field(None, description="Owner user id")
    mem_cube_id: str | None = Field(None, description="Tenant / cube id")
    knowledgebase_id: str = Field(..., description="Knowledge base id")
    file: list[dict[str, Any]] | None = Field(None, description="Cloud JSON file list")
    type: str | None = Field(None, description="Optional file category")


class KnowledgeBaseFileDeleteRequest(BaseRequest):
    """Delete knowledge base files."""

    user_id: str | None = Field(None, description="Owner user id")
    mem_cube_id: str | None = Field(None, description="Tenant / cube id")
    file_ids: list[str] = Field(..., min_length=1, description="File ids")


class KnowledgeBaseFileListRequest(BaseRequest):
    """List / get knowledge base files."""

    user_id: str | None = Field(None, description="Owner user id")
    mem_cube_id: str | None = Field(None, description="Tenant / cube id")
    knowledgebase_id: str | None = Field(None, description="List files of a KB")
    file_ids: list[str] | None = Field(None, description="Get specific files")
    type: str | None = Field(None, description="Optional file category")
    page: int | None = Field(None, ge=1, description="Page number (1-based)")
    page_size: int | None = Field(None, ge=1, le=200, description="Page size")


class KnowledgeBaseFileDownloadRequest(BaseRequest):
    """Download one knowledge base file."""

    user_id: str | None = Field(None, description="Owner user id")
    mem_cube_id: str | None = Field(None, description="Tenant / cube id")
    file_id: str = Field(..., description="File id")


class ProfileTemplateCreateRequest(BaseRequest):
    """Create a profile template."""

    user_id: str | None = Field(None, description="Owner user id")
    mem_cube_id: str | None = Field(None, description="Tenant / cube id")
    name: str = Field(..., min_length=1, description="Template name")
    metadata: dict[str, Any] = Field(..., description="Template tree (max 3 levels)")


class ProfileTemplateUpdateRequest(BaseRequest):
    """Update a profile template (cascades to bound instances)."""

    user_id: str | None = Field(None, description="Owner user id")
    mem_cube_id: str | None = Field(None, description="Tenant / cube id")
    profile_template_id: str = Field(..., description="Template id")
    name: str | None = Field(None, description="New template name")
    metadata: dict[str, Any] | None = Field(None, description="New template tree")


class ProfileTemplateQueryRequest(BaseRequest):
    """List / get profile templates."""

    user_id: str | None = Field(None, description="Owner user id")
    mem_cube_id: str | None = Field(None, description="Tenant / cube id")
    profile_template_id: str | None = Field(None, description="Get one template")
    page: int | None = Field(None, ge=1, description="Page number (1-based)")
    page_size: int | None = Field(None, ge=1, le=200, description="Page size")
    size: int | None = Field(None, ge=1, le=200, description="Page size (cloud name)")


class ProfileTemplateDeleteRequest(BaseRequest):
    """Delete a profile template; bound instances become `detached`."""

    user_id: str | None = Field(None, description="Owner user id")
    mem_cube_id: str | None = Field(None, description="Tenant / cube id")
    profile_template_id: str = Field(..., description="Template id")


class ProfileBindRequest(BaseRequest):
    """Bind subjects to profile templates."""

    user_id: str | None = Field(None, description="Owner user id")
    mem_cube_id: str | None = Field(None, description="Tenant / cube id")
    bind_list: list[dict[str, Any]] = Field(
        ...,
        min_length=1,
        max_length=20,
        description="Items like {user_id|agent_id, profile_template_id}, at most 20",
    )


class ProfileEditRequest(BaseRequest):
    """Edit one profile instance."""

    user_id: str | None = Field(None, description="Subject user id")
    agent_id: str | None = Field(None, description="Subject agent id")
    mem_cube_id: str | None = Field(None, description="Tenant / cube id")
    profile_template_id: str = Field(..., description="Template id")
    metadata: dict[str, Any] | None = Field(None, description="Field values to set")
    remove_fields: list[str] | None = Field(None, description="Field paths to remove")

    @model_validator(mode="after")
    def _require_subject_and_change(self) -> "ProfileEditRequest":
        if not self.user_id and not self.agent_id:
            raise ValueError("user_id or agent_id is required")
        if self.user_id and self.agent_id:
            raise ValueError("user_id and agent_id are mutually exclusive")
        if self.metadata is None and not self.remove_fields:
            raise ValueError("metadata or remove_fields is required")
        return self


class ProfileDeleteRequest(BaseRequest):
    """Delete one profile instance."""

    user_id: str | None = Field(None, description="Subject user id")
    agent_id: str | None = Field(None, description="Subject agent id")
    mem_cube_id: str | None = Field(None, description="Tenant / cube id")
    profile_template_id: str = Field(..., description="Template id")

    @model_validator(mode="after")
    def _require_subject(self) -> "ProfileDeleteRequest":
        if bool(self.user_id) == bool(self.agent_id):
            raise ValueError("exactly one of user_id or agent_id is required")
        return self


class ProfileQueryRequest(BaseRequest):
    """Get the profile instances of one subject."""

    user_id: str | None = Field(None, description="Subject user id")
    agent_id: str | None = Field(None, description="Subject agent id")
    mem_cube_id: str | None = Field(None, description="Tenant / cube id")
    profile_template_id: str | None = Field(None, description="Optional template filter")
