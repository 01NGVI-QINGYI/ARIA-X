"""Exception hierarchy for ARIA-X.

Every failure that is expected during an engagement is a subclass of
:class:`AriaXError`. The agent runtimes convert tool failures into observations
instead of crashing, so most of these exceptions are caught at a boundary and
turned into structured results.
"""

from __future__ import annotations


class AriaXError(Exception):
    """Base class for all ARIA-X errors."""

    code = "aria-x.error"

    def __init__(self, message: str = "", **details: object) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"code": self.code, "message": self.message}
        if self.details:
            out["details"] = self.details
        return out


class ConfigError(AriaXError):
    code = "aria-x.config"


class StoreError(AriaXError):
    code = "aria-x.store"


class NotFound(AriaXError):
    code = "aria-x.not_found"


class ScopeViolation(AriaXError):
    """Raised when an action targets something outside the engagement scope."""

    code = "aria-x.scope.violation"


class ApprovalDenied(AriaXError):
    """Raised when a human (or policy) refuses a risky action."""

    code = "aria-x.approval.denied"


class BudgetExceeded(AriaXError):
    """The engagement exhausted a configured budget dimension."""

    code = "aria-x.budget.exceeded"


class SandboxError(AriaXError):
    code = "aria-x.sandbox"


class ToolError(AriaXError):
    code = "aria-x.tool"

    def __init__(self, message: str = "", tool: str = "", **details: object) -> None:
        super().__init__(message, tool=tool, **details)
        self.tool = tool


class LLMError(AriaXError):
    code = "aria-x.llm"

    def __init__(self, message: str = "", provider: str = "", status: int | None = None, **details: object) -> None:
        super().__init__(message, provider=provider, status=status, **details)
        self.provider = provider
        self.status = status


class AgentError(AriaXError):
    code = "aria-x.agent"


class ProtocolError(AriaXError):
    """The model returned output that does not match the task contract."""

    code = "aria-x.protocol"


class Cancelled(AriaXError):
    """The run/task was cancelled by a human or the kill switch."""

    code = "aria-x.cancelled"
