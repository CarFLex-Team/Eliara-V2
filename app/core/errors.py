"""Domain exception hierarchy.

Every exception carries a `public_message` that is safe to show users and an
`internal_detail` that goes only to structured logs. The single FastAPI
exception handler in app.main guarantees that stack traces, SQL text, and
internal object names never leave the process.
"""


class EliaraError(Exception):
    status_code: int = 500
    public_message: str = "An internal error occurred. Please try again."

    def __init__(self, internal_detail: str = "", public_message: str | None = None):
        self.internal_detail = internal_detail
        if public_message is not None:
            self.public_message = public_message
        super().__init__(internal_detail or self.public_message)


class DiscoveryError(EliaraError):
    public_message = "The analytics catalog is temporarily unavailable."


class RoutingError(EliaraError):
    public_message = "I could not interpret this request. Please rephrase it."
    status_code = 422


class SQLValidationError(EliaraError):
    public_message = "This question could not be translated into a safe query."
    status_code = 422


class SQLExecutionError(EliaraError):
    public_message = "The query could not be completed. Try narrowing the question."


class LLMUnavailableError(EliaraError):
    public_message = "The AI service is temporarily unavailable. Please retry shortly."
    status_code = 503


class SessionError(EliaraError):
    public_message = "Invalid session."
    status_code = 400


class RateLimited(EliaraError):
    public_message = "Too many requests. Please wait a moment and try again."
    status_code = 429
