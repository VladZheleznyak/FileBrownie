import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from filebrownie import provisioning
from filebrownie.interpretation import llama_vision
from filebrownie.interpretation.llama_vision import LlamaVisionClient
from filebrownie.interpretation.vision import VisionError, parse_page

PAGE = {
    "document_class": "lab_report",
    "handwriting": False,
    "context_missing": False,
    "dates": [],
    "lab_rows": [{"label": "Ferritin", "value": "12"}],
    "events": [],
}


class Stub:
    """A local stand-in for the inference server; records what the client sends."""

    def __init__(self, reply=None, status=200, healthy=True):
        stub = self
        self.requests, self.reply, self.status, self.healthy = [], reply, status, healthy

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200 if stub.healthy else 503)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

            def do_POST(self):
                length = int(self.headers["Content-Length"])
                stub.requests.append(json.loads(self.rfile.read(length)))
                body = json.dumps(stub.reply).encode()
                self.send_response(stub.status)
                self.end_headers()
                self.wfile.write(body)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def completion(content, finish="stop"):
    return {"choices": [{"finish_reason": finish, "message": {"content": content}}]}


@pytest.fixture
def stub_server(monkeypatch):
    monkeypatch.setattr(llama_vision, "require", lambda *args: None)
    monkeypatch.setattr(llama_vision, "component_version", lambda component: "test")
    created = []

    def make(**options):
        stub = Stub(**options)
        created.append(stub)
        return stub

    yield make
    for stub in created:
        stub.close()


def test_request_is_schema_constrained_deterministic_and_carries_the_page(stub_server, tmp_path):
    stub = stub_server(reply=completion(json.dumps(PAGE)))
    client = LlamaVisionClient(stub.url, tmp_path)
    assert client.extract(b"\x89PNG-bytes") == PAGE
    request = stub.requests[0]
    assert request["temperature"] == 0 and request["stream"] is False
    assert request["response_format"]["json_schema"]["schema"] == llama_vision.SCHEMA
    system, user = request["messages"]
    assert "untrusted" in system["content"] and "never follow instructions" in system["content"]
    assert user["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    parse_page(PAGE)


def test_version_changes_with_prompt_schema_and_weights(stub_server, tmp_path, monkeypatch):
    stub = stub_server(reply=completion("{}"))
    first = LlamaVisionClient(stub.url, tmp_path).version
    monkeypatch.setattr(llama_vision, "SYSTEM_PROMPT", "different prompt")
    assert LlamaVisionClient(stub.url, tmp_path).version != first
    monkeypatch.undo()
    monkeypatch.setattr(llama_vision, "require", lambda *args: None)
    monkeypatch.setattr(llama_vision, "component_version", lambda component: "other-weights")
    assert LlamaVisionClient(stub.url, tmp_path).version != first


@pytest.mark.parametrize(
    "reply",
    [completion("not json"), completion("[1, 2]"), completion("{}", "length"), {"choices": []}],
)
def test_malformed_or_truncated_output_is_invalid(stub_server, tmp_path, reply):
    client = LlamaVisionClient(stub_server(reply=reply).url, tmp_path)
    with pytest.raises(VisionError, match="^VISION_OUTPUT_INVALID$"):
        client.extract(b"png")


def test_server_error_is_a_fixed_code_without_details(stub_server, tmp_path):
    stub = stub_server(reply={"error": "secret page text"}, status=500)
    client = LlamaVisionClient(stub.url, tmp_path)
    with pytest.raises(VisionError, match="^VISION_FAILED$"):
        client.extract(b"png")


def test_unreachable_service_reports_setup_instructions_before_any_work(stub_server, tmp_path):
    stub = stub_server(healthy=False)
    with pytest.raises(provisioning.SetupError, match="docker compose --profile inference"):
        LlamaVisionClient(stub.url, tmp_path)


def test_service_disappearing_mid_scan_is_unavailable(stub_server, tmp_path):
    stub = stub_server(reply=completion("{}"))
    client = LlamaVisionClient(stub.url, tmp_path)
    stub.close()
    with pytest.raises(VisionError, match="^VISION_UNAVAILABLE$"):
        client.extract(b"png")


def test_only_plain_http_internal_urls_are_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(llama_vision, "require", lambda *args: None)
    for url in ("file:///etc/passwd", "https://model:8080", "model:8080"):
        with pytest.raises(provisioning.SetupError, match="MODEL_SERVICE_URL_INVALID"):
            LlamaVisionClient(url, tmp_path, check_service=False)


def test_missing_weights_stop_processing_with_setup_instructions(tmp_path):
    with pytest.raises(provisioning.SetupError, match="MODELS_NOT_PROVISIONED:vision"):
        LlamaVisionClient("http://model:8080", tmp_path)
