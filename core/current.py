"""Thread-local handle on the active request, used by the audit trail."""
import threading

_state = threading.local()


def set_current_request(request):
    _state.request = request


def get_current_request():
    return getattr(_state, 'request', None)


def get_current_user():
    request = get_current_request()
    user = getattr(request, 'user', None)
    if user is not None and getattr(user, 'is_authenticated', False):
        return user
    return None
