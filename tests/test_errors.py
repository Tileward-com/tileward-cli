from __future__ import annotations

import pytest

from tileward import errors


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, errors.AuthenticationError),
        (402, errors.InsufficientBalanceError),
        (403, errors.PermissionError_),
        (404, errors.NotFoundError),
        (429, errors.RateLimitError),
        (500, errors.ServerError),
        (502, errors.ServerError),
        (400, errors.APIError),
    ],
)
def test_status_maps_to_class(status, expected):
    exc = errors.from_response(status, {"error": {"message": "nope"}})
    assert isinstance(exc, expected)
    assert exc.status == status


def test_openai_envelope_is_unpacked():
    exc = errors.from_response(
        404,
        {
            "error": {
                "message": "no such model",
                "type": "invalid_request",
                "code": "model_not_found",
            }
        },
    )
    assert exc.message == "no such model"
    assert exc.code == "model_not_found"
    assert exc.type == "invalid_request"


def test_plain_string_error_body():
    """The console endpoints answer `{"error": "sentence"}`, not the OpenAI object."""
    exc = errors.from_response(401, {"error": "Sign in required."})
    assert exc.message == "Sign in required."
    assert isinstance(exc, errors.AuthenticationError)


def test_html_body_does_not_raise_while_reporting():
    """A proxy 502 is often HTML. Reporting it must not itself explode."""
    exc = errors.from_response(502, "<html>Bad Gateway</html>")
    assert isinstance(exc, errors.ServerError)
    assert "Bad Gateway" in exc.message


def test_empty_body_still_says_something():
    exc = errors.from_response(500, {})
    assert "500" in str(exc)


def test_str_includes_request_id():
    exc = errors.from_response(500, {"error": {"message": "boom"}}, "req-123")
    assert "req-123" in str(exc)
