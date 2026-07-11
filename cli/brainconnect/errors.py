"""The refusal taxonomy, and the transport envelope a future `brainconnect serve`
will use.

BrainConnect's in-process API refuses by raising. A transport cannot: it must answer
with a code, and a consumer must be able to tell *why* it was refused without parsing
English. These five codes are that vocabulary, and they are not interchangeable:

    safety_refused    the content is unsafe. A human may override, at the CLI.
    not_found         no such candidate. Nothing to argue with.
    forbidden         the actor may never do this. Retrying changes nothing.
    invalid_request   the request was malformed. Fix it and retry.
    backend_error     BrainConnect is degraded. Not the caller's fault.

The distinction that costs the most when it is lost is `forbidden` versus
`invalid_request`. An agent told "invalid" will fix its payload and try again; an
agent told "forbidden" learns that promotion is not available to it, which is the
whole point of the human gate. Collapsing them teaches a fleet to keep knocking.

Second most costly: `safety_refused` versus `invalid_request`. A safety refusal is
not a bug in the caller's request — the request was well-formed and the content was
dangerous. A consumer that retries it with a cleaner payload has misunderstood, and a
consumer that *escalates to a human* has understood exactly right.

**Nothing in the runtime calls this module.** It is imported by the contract tests and
by whatever serves HTTP later. Adding it changed no behaviour.

See docs/CONTRACT.md.
"""
from __future__ import annotations

SAFETY_REFUSED = "safety_refused"
NOT_FOUND = "not_found"
FORBIDDEN = "forbidden"
INVALID_REQUEST = "invalid_request"
BACKEND_ERROR = "backend_error"

#: code -> the HTTP status `brainconnect serve` should answer with.
HTTP_STATUS: dict[str, int] = {
    SAFETY_REFUSED: 409,     # Conflict: the request is fine; the content is not.
    NOT_FOUND: 404,
    FORBIDDEN: 403,
    INVALID_REQUEST: 400,
    BACKEND_ERROR: 503,      # Retry later, with backoff. Not 500: it is often transient.
}

#: Whether a consumer may usefully retry the identical request.
RETRYABLE: dict[str, bool] = {
    SAFETY_REFUSED: False,   # needs a human override, not a retry
    NOT_FOUND: False,
    FORBIDDEN: False,        # needs a different actor, not a retry
    INVALID_REQUEST: False,  # needs a different request
    BACKEND_ERROR: True,
}

CODES = tuple(HTTP_STATUS)


def _table():
    """Exception class -> code. Most specific first; `classify` walks in order.

    Imported lazily so that `import brainconnect.errors` stays cheap and free of cycles.
    """
    from . import api, candidates, feedback, ingest, profiles, refs
    from . import confidence, scopes
    from .backends import base as backends_base
    from .safety import configuration as safety_config, policies as safety_policies

    return (
        # Safety refusal is a subclass of CandidateError, so it must precede it.
        (candidates.SafetyRefused, SAFETY_REFUSED),
        (candidates.CandidateNotFound, NOT_FOUND),
        (candidates.ReviewerNotPermitted, FORBIDDEN),
        # A ledger the operator misconfigured, or a backend that cannot serve. The
        # caller cannot fix either by changing its request.
        (backends_base.BackendError, BACKEND_ERROR),
        (safety_config.SafetyConfigError, BACKEND_ERROR),
        (safety_policies.PolicyError, BACKEND_ERROR),
        # Everything else the caller got wrong.
        (candidates.CandidateError, INVALID_REQUEST),
        (api.ApiError, INVALID_REQUEST),
        (scopes.ScopeError, INVALID_REQUEST),
        (confidence.ConfidenceError, INVALID_REQUEST),
        (refs.RefError, INVALID_REQUEST),
        (profiles.ProfileError, INVALID_REQUEST),
        (feedback.FeedbackError, INVALID_REQUEST),
        (ingest.IngestError, INVALID_REQUEST),
    )


def classify(exc: BaseException) -> str:
    """The code for `exc`.

    An exception this module has never heard of is `backend_error`, never
    `invalid_request`: an unrecognised failure is BrainConnect's problem to explain,
    and telling the caller its request was malformed would be a guess dressed as an
    answer.
    """
    for cls, code in _table():
        if isinstance(exc, cls):
            return code
    return BACKEND_ERROR


def envelope(exc: BaseException) -> dict:
    """The body a transport should return for `exc`.

    `safety` is present only on a safety refusal, and is the audit-safe summary: rule
    names, severities, spans, engine attribution. **Never the matched text** — a
    refusal that quotes the credential it refused has published it.
    """
    code = classify(exc)
    body = {
        "error": {
            "code": code,
            "message": str(exc),
            "retryable": RETRYABLE[code],
        }
    }
    result = getattr(exc, "result", None)
    if result is not None and hasattr(result, "summary"):
        body["error"]["safety"] = result.summary()
    return body


def http_status(exc: BaseException) -> int:
    return HTTP_STATUS[classify(exc)]
