"""Verify wrap_bedrock logs boto3 Bedrock runtime calls with a fake client."""

import json

import clustertrace
from clustertrace import storage


class FakeBody:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


class FakeBedrockClient:
    custom = "yes"

    def invoke_model(self, **kwargs):
        return {
            "body": FakeBody(
                {
                    "content": [{"type": "text", "text": "hello"}],
                    "usage": {"input_tokens": 11, "output_tokens": 6},
                }
            )
        }

    def invoke_model_with_response_stream(self, **kwargs):
        return {"body": [{"chunk": {"bytes": b'{"delta":{"text":"hi"}}'}}]}


def test_wrap_bedrock_logs_invoke_model():
    wrapped = clustertrace.wrap_bedrock(FakeBedrockClient())

    resp = wrapped.invoke_model(
        modelId="anthropic.claude-3-haiku-20240307-v1:0",
        body=json.dumps({"messages": [{"role": "user", "content": "hi"}]}),
    )

    assert json.loads(resp["body"].read())["usage"]["input_tokens"] == 11
    with storage.connect() as conn:
        row = conn.execute("SELECT name, kind, status, attrs_json FROM spans").fetchone()

    assert row["kind"] == "llm_call"
    assert row["status"] == "ok"
    assert "bedrock.invoke_model:anthropic.claude-3-haiku" in row["name"]
    assert '"provider": "bedrock"' in row["attrs_json"]
    assert '"input_tokens": 11' in row["attrs_json"]
    assert '"output_tokens": 6' in row["attrs_json"]


def test_wrap_bedrock_logs_streaming_call():
    wrapped = clustertrace.wrap_bedrock(FakeBedrockClient())

    wrapped.invoke_model_with_response_stream(
        modelId="anthropic.claude-3-haiku-20240307-v1:0",
        body=json.dumps({"messages": [{"role": "user", "content": "hi"}]}),
    )

    with storage.connect() as conn:
        row = conn.execute("SELECT status, attrs_json FROM spans").fetchone()

    assert row["status"] == "ok"
    assert '"streaming": true' in row["attrs_json"]


def test_wrap_bedrock_exposes_unrelated_attrs():
    wrapped = clustertrace.wrap_bedrock(FakeBedrockClient())

    assert wrapped.custom == "yes"
