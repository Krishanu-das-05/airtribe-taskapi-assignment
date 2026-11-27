"""In-memory Idempotency-Key support.

Store shape: {key: {"body": <fingerprint of path + request body>, "response": {"status": int, "data": ...}}}

* same key + same body      -> replay the stored response, run nothing
* same key + different body -> 422 IDEMPOTENCY_KEY_REUSED
* only 2xx responses are stored, so a client can fix bad input and retry with the same key

The store lives in process memory: it is cleared on restart and is not shared between
worker processes. A production deployment would use Redis or a database table instead.
"""
import copy
import json
import threading
from functools import wraps

from rest_framework import status
from rest_framework.response import Response

from .errors import error_response

IDEMPOTENCY_HEADER = "Idempotency-Key"
MAX_KEY_LENGTH = 255

_store = {}
_lock = threading.Lock()


def clear_store():
    with _lock:
        _store.clear()


def _fingerprint(request):
    return json.dumps({"path": request.path, "body": request.data}, sort_keys=True, default=str)


def idempotent(view_method):
    @wraps(view_method)
    def wrapper(self, request, *args, **kwargs):
        key = request.headers.get(IDEMPOTENCY_HEADER)
        if key is None:
            return view_method(self, request, *args, **kwargs)

        key = key.strip()
        if not key or len(key) > MAX_KEY_LENGTH:
            return error_response(
                "VALIDATION_ERROR",
                "Validation failed",
                [{"field": IDEMPOTENCY_HEADER, "issue": f"must be 1-{MAX_KEY_LENGTH} characters"}],
            )

        fingerprint = _fingerprint(request)
        with _lock:
            entry = _store.get(key)
            if entry is not None:
                if entry["body"] != fingerprint:
                    return error_response(
                        "IDEMPOTENCY_KEY_REUSED",
                        "Idempotency-Key was already used with a different request body",
                        [{"field": IDEMPOTENCY_HEADER, "issue": "reused with a different request"}],
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                    )
                stored = entry["response"]
                return Response(copy.deepcopy(stored["data"]), status=stored["status"],
                                headers={"Idempotent-Replayed": "true"})

            response = view_method(self, request, *args, **kwargs)
            if 200 <= response.status_code < 300:
                _store[key] = {
                    "body": fingerprint,
                    "response": {"status": response.status_code, "data": copy.deepcopy(response.data)},
                }
            return response

    return wrapper
