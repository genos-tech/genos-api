"""Thread-local current-user middleware.

Django signals run inside `Model.save()` and `Model.delete()`, where the
HTTP `request` object isn't accessible. To record "who did this" against
each `TaskActivity` row we stash the active `request` in a thread-local
and lazily resolve `request.user` from it inside the signal — by that
point DRF's `dispatch()` has already populated `request.user` from the
Bearer token, so token-authenticated calls are attributed correctly.

Signal handlers in `origin/signals/task_signals.py` read the actor via
`get_current_user()`. Code that bypasses Django middleware entirely
(SocketIO handlers reaching ORM directly, management commands) can opt
in by calling `set_current_user(user)` and clearing it in `finally`.
"""

import threading

_state = threading.local()


def get_current_user():
    """Return the acting user for the active thread, or None.

    Resolution order:
      1. An explicitly-set user (`set_current_user`) — used by code paths
         that don't run through WSGI (SocketIO handlers reaching ORM).
      2. `request.user` from the active HTTP request, but only after
         DRF has authenticated it. AnonymousUser → None so we never
         attribute audit rows to "no one".
    """

    explicit = getattr(_state, "user", None)
    if explicit is not None and getattr(explicit, "is_authenticated", False):
        return explicit

    request = getattr(_state, "request", None)
    if request is None:
        return None
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return user


def get_current_source():
    """Return how the active thread's writes reached Genos, as a dict for
    `TaskActivity.metadata["via"]`, or None when unknown (a webhook, a
    management command, the derived-status sync outside a request).

      * `{"kind": "agent"}`   — the in-app Spotlight agent, acting for the
        user after they approved the write (`set_current_user(..., via=)`);
      * `{"kind": "api_key", "key_id", "key_name", "key_prefix"}` — a
        public-API or MCP call. DRF puts the `ApiKey` on `request.auth`
        (and mirrors it onto the Django request we hold), read lazily for
        the same reason `request.user` is;
      * `{"kind": "app"}`     — a signed-in session/JWT request.
    """
    explicit = getattr(_state, "via", None)
    if explicit is not None:
        return explicit
    if get_current_user() is None:
        return None
    from origin.models.common.api_key_models import ApiKey  # noqa: PLC0415

    key = getattr(getattr(_state, "request", None), "auth", None)
    if isinstance(key, ApiKey):
        return {
            "kind": "api_key",
            "key_id": str(key.id),
            "key_name": key.name,
            "key_prefix": key.prefix,
        }
    return {"kind": "app"}


def set_current_user(user, via=None):
    """Manually set the current user (and, optionally, the `via` source
    `get_current_source` reports). Pair with `clear_current_user()` in a
    try/finally; useful for SocketIO handlers that touch the ORM outside
    of an HTTP request, and for the agent's worker thread — a thread
    started from a request does NOT see that request's thread-local."""
    _state.user = user
    _state.via = via


def clear_current_user():
    """Drop the explicit user/source and cached request from this thread."""
    for attr in ("user", "via", "request"):
        if hasattr(_state, attr):
            delattr(_state, attr)


class CurrentUserMiddleware:
    """WSGI middleware that pins the active `HttpRequest` to the
    thread-local for the lifetime of the request, then clears it.

    We deliberately stash the request rather than `request.user` —
    DRF's authentication runs inside the view's `dispatch()` (after
    middleware), so reading `request.user` here would give us
    `AnonymousUser` for Bearer-token traffic. Reading it lazily inside
    the signal sees the authenticated user.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            _state.request = request
            return self.get_response(request)
        finally:
            clear_current_user()
