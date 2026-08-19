# ============================================================
# Muhafiz Data API — typed errors
#
# One exception type per failure mode the API_CONSUMER_GUIDE.md documents
# ("Missing or invalid keys return 401; an unknown resource ID returns 404;
# and invalid or omitted required query parameters return 422"), plus one
# for everything else (network failure, 5xx after retries are exhausted,
# an unexpected response shape). Callers that only care "did this work"
# can catch MuhafizApiError; callers that need to react differently to
# "record doesn't exist" vs. "the whole source is down" can catch the
# specific subclass.
# ============================================================


class MuhafizApiError(Exception):
    """Base class for every error this client raises."""


class MuhafizApiAuthError(MuhafizApiError):
    """401 — missing or invalid X-API-Key."""


class MuhafizApiNotFoundError(MuhafizApiError):
    """404 — the requested resource id does not exist."""

    def __init__(self, message: str, resource_id: str | None = None):
        super().__init__(message)
        self.resource_id = resource_id


class MuhafizApiValidationError(MuhafizApiError):
    """422 — invalid or missing required query parameters (page/page_size)."""


class MuhafizApiUnavailableError(MuhafizApiError):
    """
    5xx after retries are exhausted, a connection/timeout failure, or a
    response that doesn't match the documented {data, meta} envelope at
    all. Distinct from the three above because it means "try again later
    / the source itself is unreachable," not "this specific call is wrong."
    """
