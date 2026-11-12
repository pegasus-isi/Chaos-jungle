"""Exercise bounded in-process CJ agent faults against a local HTTP service."""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import requests

from chaos_jungle import PromptInjection, ToolMutate, inject
from chaos_jungle.db.session_db import SessionDB


MARKER = "CJ_CI_PROMPT_MARKER"


class AgentHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.received.append(body)
        messages = body["messages"]
        tool_messages = [message for message in messages if message["role"] == "tool"]
        if tool_messages:
            count = json.loads(tool_messages[-1]["content"])["count"]
            answer = "{} results".format(count)
        else:
            answer = "marker seen" if MARKER in messages[-1]["content"] else "clean"
        response = json.dumps(
            {
                "id": "ci-agent-response",
                "object": "chat.completion",
                "model": "ci-agent-model",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": answer},
                     "finish_reason": "stop"}
                ],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        pass


def run_case(output_dir: Path, case: str) -> None:
    if case in {"tool-result", "tool-result-requests"}:
        behavior = ToolMutate(tool_name="search", replacement={"count": 0, "results": []})
        messages = [
            {"role": "user", "content": "Summarize the search result."},
            {"role": "tool", "name": "search", "content": json.dumps({"count": 2,
                                                                      "results": ["a", "b"]})},
        ]
        expected = ["2 results", "0 results", "2 results"]
    elif case == "prompt-input":
        behavior = PromptInjection(MARKER, target="user")
        messages = [{"role": "user", "content": "Summarize status."}]
        expected = ["clean", "marker seen", "clean"]
    else:
        raise ValueError("Unsupported case: {}".format(case))

    output_dir.mkdir(parents=True, exist_ok=True)
    db_path = output_dir / "session.db"
    db = SessionDB(path=str(db_path))
    session_id = db.open_session("ci-agent-{}".format(case), target_type="local-http")
    fault_id = db.record_fault(session_id, type(behavior).__name__, {"case": case})
    server = ThreadingHTTPServer(("127.0.0.1", 0), AgentHandler)
    server.received = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    status = "aborted"
    try:
        url = "http://127.0.0.1:{}/v1/chat/completions".format(server.server_port)
        payload = {"model": "ci-agent-model", "messages": messages, "max_tokens": 16}

        def call(client) -> str:
            response = client.post(url, json=payload, timeout=5)
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]

        if case == "tool-result-requests":
            client = requests.Session()
            client.trust_env = False
        else:
            client = httpx.Client(trust_env=False)
        with client:
            baseline = call(client)
            with inject(
                behavior, urls=["127.0.0.1"], session_id=session_id,
                db_path=str(db_path), measure=True,
            ) as measurements:
                fault = call(client)
            recovery = call(client)

        received = server.received
        traced_calls = db.get_llm_calls(session_id, phase="fault")
        report = {
            "case": case,
            "interpretation": "Local integration smoke test; not a model quality estimate",
            "fault_default_metrics": behavior.default_metrics,
            "observed_metrics": {
                "http_calls": measurements.http_calls,
                "http_errors": measurements.http_errors,
            },
            "answers": {"baseline": baseline, "fault": fault, "recovery": recovery},
            "evidence": {
                "request_modified": len(received) == 3 and received[1] != received[0],
                "restored": len(received) == 3 and received[2] == received[0],
                "fault_trace_count": len(traced_calls),
                "trace_modified": len(traced_calls) == 1 and bool(traced_calls[0]["was_modified"]),
            },
        }
        assert [baseline, fault, recovery] == expected, report
        assert measurements.http_calls == 1 and measurements.http_errors == 0, report
        assert all(report["evidence"].values()), report
        assert len(traced_calls) == 1 and traced_calls[0]["http_status"] == 200, report
        if case in {"tool-result", "tool-result-requests"}:
            counts = [json.loads(item["messages"][-1]["content"])["count"] for item in received]
            assert counts == [2, 0, 2], report
        else:
            assert [MARKER in item["messages"][-1]["content"] for item in received] == [
                False, True, False,
            ], report

        db.update_fault_status(fault_id, "reverted")
        db.update_fault_verification(
            fault_id, verified_active=True, verified_recovered=True,
            note="Modified request observed at the local service; normal request restored afterward",
        )
        db.record_fault_observation(
            fault_id, event_reached=True, triggered=True, manifested=True,
            evidence={"call_id": traced_calls[0]["id"], "request_modified": True,
                      "changed_answer": fault != baseline},
        )
        db.close_fault(fault_id)
        exposure = db.classify_session_exposure(session_id, policy_compliant=True)
        assert exposure["verdict"] == "valid", exposure
        report["exposure_report"] = exposure
        (output_dir / "metrics.json").write_text(json.dumps(report, indent=2) + "\n")
        db.record_result(session_id, report)
        db.set_session_verdict(session_id, "VALID")
        status = "reverted"
        (output_dir / "summary.txt").write_text(
            "{}: baseline={!r}, fault={!r}, recovery={!r}; request changed and restored.\n".format(
                case, baseline, fault, recovery
            )
        )
        print((output_dir / "summary.txt").read_text().strip())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        db.close_session(session_id, status=status)
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case", choices=("tool-result", "tool-result-requests", "prompt-input"),
        required=True,
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    run_case(args.output_dir, args.case)
