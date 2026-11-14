"""HTTP-level regression for output-token limits used by LangGraph clients."""

import json
import threading
import urllib.request

import pytest

from chaos_jungle.scripts.llm_proxy import llm_proxy as proxy


@pytest.mark.parametrize("completion_tokens,expected", [(10, True), (11, False)])
def test_langgraph_token_cap_http_round_trip(monkeypatch, completion_tokens, expected):
    forwarded = []
    recorded = []

    def fake_forward(method, url, headers, body):
        forwarded.append(json.loads(body))
        response = {
            "choices": [{"message": {"role": "assistant", "content": "x"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": completion_tokens},
        }
        return 200, json.dumps(response).encode(), "application/json", {}

    monkeypatch.setattr(proxy, "FAULT_CHAIN", [{"fault": "token_starve", "max_tokens": 10}])
    monkeypatch.setattr(proxy, "_DB_PATH", "test-db")
    monkeypatch.setattr(proxy, "_SESSION_ID", 1)
    monkeypatch.setattr(proxy, "_call_index", 0)
    monkeypatch.setattr(proxy, "_forward", fake_forward)
    monkeypatch.setattr(proxy, "_record_llm_call", lambda **fields: recorded.append(fields) or 1)
    monkeypatch.setattr(proxy, "_record_tool_calls", lambda *args, **kwargs: None)

    server = proxy._ReusableHTTPServer(("127.0.0.1", 0), proxy._ProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            data=json.dumps({"model": "test", "max_completion_tokens": 2048}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200

        assert forwarded == [{"model": "test", "max_completion_tokens": 10}]
        assert len(recorded) == 1
        lifecycle = json.loads(recorded[0]["fault_evidence_json"])[0]
        assert lifecycle["applied"] is True
        assert lifecycle["manifested"] is expected
        assert lifecycle["evidence"]["observed_completion_tokens"] == completion_tokens
        assert lifecycle["evidence"]["cap_respected"] is expected
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
