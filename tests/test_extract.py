"""Stage 2 - the API call is faked; what is tested is the contract around it."""

import pytest

from radar.extract import ClaudeExtractor, ExtractionError, Usage
from radar.models import ExtractedChanges, ReleaseNotes, RemovedApi


class FakeUsage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeResponse:
    def __init__(self, parsed_output, usage=None, stop_reason="end_turn"):
        self.parsed_output = parsed_output
        self.usage = usage
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, response=None, error=None):
        self.messages = FakeMessages(response, error)


def notes(body="* `BaseSettings` has been removed."):
    return ReleaseNotes(
        package="pydantic", version="2.0", source="github", body=body
    )


def test_extraction_returns_the_parsed_schema():
    expected = ExtractedChanges(removed=[RemovedApi(name="BaseSettings")])
    client = FakeClient(FakeResponse(expected, FakeUsage(1200, 80)))
    extractor = ClaudeExtractor(client=client)

    changes = extractor.extract(notes())

    assert changes is expected
    assert extractor.last_usage.input_tokens == 1200


def test_request_carries_the_schema_and_the_notes():
    client = FakeClient(FakeResponse(ExtractedChanges()))
    ClaudeExtractor(client=client).extract(notes("body text here"))

    call = client.messages.calls[0]
    assert call["output_format"] is ExtractedChanges
    assert call["model"] == "claude-opus-5"
    assert "body text here" in call["messages"][0]["content"]
    assert "pydantic" in call["messages"][0]["content"]


def test_system_prompt_keeps_the_verdict_out_of_the_model():
    client = FakeClient(FakeResponse(ExtractedChanges()))
    ClaudeExtractor(client=client).extract(notes())

    system = client.messages.calls[0]["system"].lower()
    assert "do not assess severity" in system
    assert "breaking" in system  # ...as an instruction NOT to judge it


def test_empty_notes_do_not_reach_the_api():
    client = FakeClient(FakeResponse(ExtractedChanges()))
    extractor = ClaudeExtractor(client=client)

    changes = extractor.extract(notes(body="   \n  "))

    assert client.messages.calls == []
    assert changes.is_empty()
    assert extractor.last_usage == Usage()


def test_missing_structured_output_is_an_error():
    client = FakeClient(FakeResponse(None, FakeUsage(10, 0), stop_reason="max_tokens"))
    with pytest.raises(ExtractionError, match="max_tokens"):
        ClaudeExtractor(client=client).extract(notes())


def test_api_failure_is_wrapped_not_swallowed():
    client = FakeClient(error=RuntimeError("503 overloaded"))
    with pytest.raises(ExtractionError, match="503 overloaded"):
        ClaudeExtractor(client=client).extract(notes())


def test_cost_is_reported_per_run():
    usage = Usage(input_tokens=200_000, output_tokens=10_000)
    # 0.2 MTok in at $5 + 0.01 MTok out at $25
    assert usage.cost_usd == pytest.approx(1.0 + 0.25)
    assert "$1.2500" in usage.summary()
