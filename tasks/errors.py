"""One consistent error shape for every API error response.

{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Validation failed",
    "details": [{"field": "title", "issue": "must not be empty"}]
  }
}
"""
import logging

from rest_framework import exceptions, status as http_status
from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)


def error_response(code, message, details=None, status=http_status.HTTP_400_BAD_REQUEST, headers=None):
    return Response(
        {"error": {"code": str(code), "message": str(message), "details": details or []}},
        status=status,
        headers=headers,
    )


def errors_to_details(errors, prefix=""):
    """Flatten DRF serializer.errors (dict / list / nested) into [{"field", "issue"}]."""
    details = []
    if isinstance(errors, dict):
        for field, value in errors.items():
            name = f"{prefix}.{field}" if prefix else str(field)
            details.extend(errors_to_details(value, name))
    elif isinstance(errors, (list, tuple)):
        for index, value in enumerate(errors):
            if isinstance(value, (dict, list, tuple)):
                details.extend(errors_to_details(value, f"{prefix}[{index}]"))
            else:
                details.append({"field": prefix or "non_field_errors", "issue": str(value)})
    else:
        details.append({"field": prefix or "non_field_errors", "issue": str(errors)})
    return details


def validation_error_response(errors, code="VALIDATION_ERROR", message="Validation failed",
                              status=http_status.HTTP_400_BAD_REQUEST):
    return error_response(code, message, errors_to_details(errors), status)


_EXCEPTION_CODES = {
    exceptions.ValidationError: ("VALIDATION_ERROR", "Validation failed"),
    exceptions.ParseError: ("MALFORMED_REQUEST", "Malformed request body"),
    exceptions.NotFound: ("NOT_FOUND", "Resource not found"),
    exceptions.MethodNotAllowed: ("METHOD_NOT_ALLOWED", "Method not allowed"),
    exceptions.UnsupportedMediaType: ("UNSUPPORTED_MEDIA_TYPE", "Unsupported media type"),
    exceptions.NotAcceptable: ("NOT_ACCEPTABLE", "Not acceptable"),
}


def api_exception_handler(exc, context):
    """Safety net so errors raised outside our views (bad JSON, bad filters, bad page) keep the shape."""
    response = exception_handler(exc, context)
    if response is None:
        logger.exception("Unhandled API error", exc_info=exc)
        return error_response("INTERNAL_ERROR", "An unexpected error occurred",
                              status=http_status.HTTP_500_INTERNAL_SERVER_ERROR)

    code, message = "ERROR", "Request failed"
    for exc_class, (exc_code, exc_message) in _EXCEPTION_CODES.items():
        if isinstance(exc, exc_class):
            code, message = exc_code, exc_message
            break

    detail = getattr(exc, "detail", None)
    if isinstance(exc, exceptions.ValidationError):
        details = errors_to_details(detail)
    elif detail:
        details = [{"field": "non_field_errors", "issue": str(detail)}]
    else:
        details = []
    return error_response(code, message, details, response.status_code, headers=_passthrough_headers(response))


def _passthrough_headers(response):
    return {name: response[name] for name in ("Allow", "Retry-After") if response.has_header(name)}
