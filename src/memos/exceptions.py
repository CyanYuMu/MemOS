"""Custom exceptions for the MemOS library.

This module defines all custom exceptions used throughout the MemOS project.
All exceptions inherit from a base MemOSError class to provide a consistent
error handling interface.
"""


class MemOSError(Exception): ...


class ConfigurationError(MemOSError): ...


class MemCubeError(MemOSError): ...


class LLMError(MemOSError): ...


class LLMRateLimitError(LLMError):
    """Local admission failed; do not bypass it through model retries or fallback."""


class LLMRateLimitTimeoutError(LLMRateLimitError): ...


class LLMRateLimitQueueFullError(LLMRateLimitError): ...


class LLMRateLimitUnavailableError(LLMRateLimitError): ...


class EmbedderError(MemOSError): ...


class MemoryDeleteError(MemOSError):
    """One or more memories could not be deleted; the remaining ids were attempted."""


class PromptNotFoundError(MemOSError):
    """The requested prompt key, language, or version does not exist."""


class PromptVersionNotFoundError(PromptNotFoundError):
    """No such version in this project's slot.

    A version that belongs to another project is reported the same way, so the
    answer never says whether it exists elsewhere.
    """


class PromptValidationError(MemOSError):
    """A prompt draft failed static validation; ``errors`` carries the findings."""

    def __init__(self, message: str, errors: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.errors: list[dict[str, str]] = list(errors or [])


class PromptInputError(MemOSError):
    """The trial-run input does not match the prompt's ``input_schema``."""


class PromptInputTooLargeError(PromptInputError):
    """The trial-run input exceeds the message or character limits."""


class PromptBusyError(MemOSError):
    """Too many trial runs at once; ``retry_after_sec`` says how long to wait."""

    def __init__(self, message: str, retry_after_sec: int = 2) -> None:
        super().__init__(message)
        self.retry_after_sec = max(1, int(retry_after_sec))


class PromptVersionConflictError(MemOSError):
    """The slot moved on since the client read it; ``current`` is the live version."""

    def __init__(self, message: str, current: dict | None = None) -> None:
        super().__init__(message)
        self.current: dict = dict(current or {})
