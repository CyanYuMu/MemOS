from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from memos.api.exceptions import APIExceptionHandler
from memos.api.product_models import APIADDRequest


def test_a_validator_value_error_is_answered_with_422_not_500():
    app = FastAPI()
    app.exception_handler(RequestValidationError)(APIExceptionHandler.validation_error_handler)

    @app.post("/add")
    def add(req: APIADDRequest):
        return {"ok": True}

    body = {"user_id": [f"u{i}" for i in range(21)], "messages": "hi"}
    response = TestClient(app).post("/add", json=body)

    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == 422 and payload["data"] is None
    assert "20" in str(payload["detail"])
