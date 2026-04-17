from contextvars import ContextVar

# Set by get_current_user() for every authenticated request.
# The logging filter reads this so user_id appears in every log line
# without passing it explicitly to each logger.info() call.
user_id_ctx: ContextVar[str] = ContextVar("user_id", default="")
