"""Provider-reported model identity must not be inferred from the request."""

from types import SimpleNamespace

import pytest

from agent.codex_runtime import _consume_codex_event_stream


@pytest.mark.parametrize("served_model", ["requested-model", "served-model", None])
@pytest.mark.parametrize("terminal_type", ["completed", "incomplete", "failed"])
@pytest.mark.parametrize("as_namespace", [False, True])
def test_terminal_model_identity_is_separate_from_requested_model(
    served_model, terminal_type, as_namespace
):
    requested_model = "requested-model"
    terminal_response = {"id": "response-id", "status": terminal_type}
    if served_model is not None:
        terminal_response["model"] = served_model
    if as_namespace:
        terminal_response = SimpleNamespace(**terminal_response)
    response = _consume_codex_event_stream(
        [{"type": f"response.{terminal_type}", "response": terminal_response}],
        model=requested_model,
    )

    assert response.model == requested_model
    assert response.observed_model == served_model


def test_text_only_stream_has_no_observed_model():
    response = _consume_codex_event_stream(
        [{"type": "response.output_text.delta", "delta": "answer"}],
        model="requested-model",
    )

    assert response.output_text == "answer"
    assert response.model == "requested-model"
    assert response.observed_model is None
