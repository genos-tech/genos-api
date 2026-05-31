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


def set_current_user(user):
    """Manually set the current user. Pair with `clear_current_user()`
    in a try/finally; useful for SocketIO handlers that touch the ORM
    outside of an HTTP request."""
    _state.user = user


def clear_current_user():
    """Drop both the explicit user and cached request from this thread."""
    if hasattr(_state, "user"):
        delattr(_state, "user")
    if hasattr(_state, "request"):
        delattr(_state, "request")


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
