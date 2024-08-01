"""Registry mode against a fake Schema Registry speaking the documented REST API."""

import base64
import io
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from schema_compat_ci.avro import parse
from schema_compat_ci.cli import run
from schema_compat_ci.compat import check
from schema_compat_ci.registry import RegistryClient, RegistryError

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
V1_TEXT = (EXAMPLES / "order-v1.avsc").read_text()


class FakeRegistry(HTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.subjects = {}  # subject -> {"schema": text, "level": str, "type": str}
        self.requests = []  # (method, path, headers, body)
        self.required_auth = None
        self.url = f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/vnd.schemaregistry.v1+json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        self.server.requests.append((method, self.path, dict(self.headers), body))
        if self.server.required_auth and self.headers.get("Authorization") != self.server.required_auth:
            return self._send(401, {"error_code": 401, "message": "Unauthorized"})
        url = urlsplit(self.path)
        parts = [unquote(p) for p in url.path.strip("/").split("/")]
        subjects = self.server.subjects

        if method == "POST" and parts[0] == "compatibility" and parts[1] == "subjects" and parts[3:] == ["versions"]:
            entry = subjects.get(parts[2])
            if entry is None:
                return self._send(404, {"error_code": 40401, "message": f"Subject '{parts[2]}' not found."})
            if entry["type"] != "AVRO":
                return self._send(200, {"is_compatible": body["schemaType"] == entry["type"]})
            try:
                new = parse(body["schema"])
            except ValueError:
                return self._send(422, {"error_code": 42201, "message": "Invalid schema"})
            findings = check(parse(entry["schema"]), new, entry["level"])
            payload = {"is_compatible": not findings}
            if findings and "verbose=true" in url.query:
                payload["messages"] = [f"{{errorType:'{f.kind.upper()}', path:'{f.path}'}}" for f in findings]
            return self._send(200, payload)
        if method == "GET" and parts[0] == "config" and len(parts) == 2:
            if parts[1] not in subjects:
                return self._send(404, {"error_code": 40401, "message": "Subject not found."})
            return self._send(200, {"compatibilityLevel": subjects[parts[1]]["level"]})
        if method == "GET" and parts[0] == "subjects" and parts[2:] == ["versions", "latest"]:
            entry = subjects.get(parts[1])
            if entry is None:
                return self._send(404, {"error_code": 40401, "message": "Subject not found."})
            payload = {"subject": parts[1], "version": 3, "id": 17, "schema": entry["schema"]}
            if entry["type"] != "AVRO":
                payload["schemaType"] = entry["type"]
            return self._send(200, payload)
        return self._send(500, {"error_code": 50001, "message": "Error in the backend data store"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


@pytest.fixture
def registry():
    server = FakeRegistry()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def schemas(tmp_path):
    shutil.copy(EXAMPLES / "order-v2-compatible.avsc", tmp_path / "orders.avsc")
    shutil.copy(EXAMPLES / "order-v2-breaking.avsc", tmp_path / "returns.avsc")
    shutil.copy(EXAMPLES / "order-v1.avsc", tmp_path / "unregistered.avsc")
    return tmp_path


def cli(*argv):
    out = io.StringIO()
    return run([str(a) for a in argv], out=out), out.getvalue()


def test_client_sends_documented_request(registry):
    registry.subjects["orders-value"] = {"schema": V1_TEXT, "level": "BACKWARD", "type": "AVRO"}
    client = RegistryClient(registry.url + "/")
    result = client.test_compatibility("orders-value", V1_TEXT)
    assert result.subject_exists and result.is_compatible and result.messages == []
    method, path, headers, body = registry.requests[-1]
    assert (method, path) == ("POST", "/compatibility/subjects/orders-value/versions?verbose=true")
    assert headers["Content-Type"] == "application/vnd.schemaregistry.v1+json"
    assert body == {"schema": V1_TEXT, "schemaType": "AVRO"}
    assert client.compatibility_level("orders-value") == "BACKWARD"
    assert client.latest_schema("orders-value") == (V1_TEXT, "AVRO")


def test_subject_names_are_url_encoded(registry):
    registry.subjects["team/orders value"] = {"schema": V1_TEXT, "level": "FULL", "type": "AVRO"}
    client = RegistryClient(registry.url)
    assert client.test_compatibility("team/orders value", V1_TEXT).is_compatible
    assert registry.requests[-1][1] == "/compatibility/subjects/team%2Forders%20value/versions?verbose=true"


def test_unknown_subject_and_errors(registry):
    client = RegistryClient(registry.url)
    result = client.test_compatibility("nope", V1_TEXT)
    assert not result.subject_exists
    registry.subjects["s"] = {"schema": V1_TEXT, "level": "FULL", "type": "AVRO"}
    with pytest.raises(RegistryError, match="HTTP 422.*42201"):
        client.test_compatibility("s", "not a schema")
    with pytest.raises(RegistryError, match="cannot reach"):
        RegistryClient("http://127.0.0.1:1", timeout=2).test_compatibility("s", V1_TEXT)


def test_cli_registry_mode_uses_each_subjects_level(registry, schemas):
    registry.subjects["orders-value"] = {"schema": V1_TEXT, "level": "FULL", "type": "AVRO"}
    registry.subjects["returns-value"] = {"schema": V1_TEXT, "level": "FORWARD", "type": "AVRO"}
    code, text = cli("check", "--registry", registry.url, "--subject-template", "{name}-value", schemas)
    assert code == 1
    lines = text.splitlines()
    assert f"PASS  {schemas / 'orders.avsc'} (subject orders-value)" in lines
    assert f"FAIL  {schemas / 'returns.avsc'} (subject returns-value, level FORWARD)" in lines
    assert f"NEW   {schemas / 'unregistered.avsc'} (subject unregistered-value is not registered yet)" in lines
    assert any(line.strip().startswith("registry: {errorType:'MISSING_SYMBOLS'") for line in lines)
    # the local explanation follows the subject's FORWARD level: no BACKWARD findings
    assert any("[FORWARD] Order.legacy_ref:" in line for line in lines)
    assert not any("[BACKWARD]" in line for line in lines)
    # nothing but compatibility tests and reads was sent
    assert {(m, p.split("/")[1]) for m, p, _, _ in registry.requests} <= {
        ("POST", "compatibility"), ("GET", "config"), ("GET", "subjects")}


def test_cli_registry_default_subject_is_the_file_name(registry, schemas):
    registry.subjects["orders"] = {"schema": V1_TEXT, "level": "BACKWARD", "type": "AVRO"}
    code, text = cli("check", "--registry", registry.url, schemas / "orders.avsc")
    assert code == 0 and "(subject orders)" in text


def test_cli_registry_basic_auth_from_environment(registry, schemas, monkeypatch):
    registry.subjects["orders"] = {"schema": V1_TEXT, "level": "BACKWARD", "type": "AVRO"}
    registry.required_auth = "Basic " + base64.b64encode(b"key:secret").decode()
    code, text = cli("check", "--registry", registry.url, schemas / "orders.avsc")
    assert code == 2 and "ERROR" in text and "HTTP 401" in text
    monkeypatch.setenv("SCHEMA_REGISTRY_BASIC_AUTH_USER_INFO", "key:secret")
    assert cli("check", "--registry", registry.url, schemas / "orders.avsc")[0] == 0


def test_cli_registry_passes_protobuf_through_without_local_analysis(registry, tmp_path):
    proto = tmp_path / "events.proto"
    proto.write_text('syntax = "proto3";\nmessage Event { string id = 1; }\n')
    registry.subjects["events"] = {"schema": "", "level": "BACKWARD", "type": "PROTOBUF"}
    code, text = cli("check", "--registry", registry.url, tmp_path)
    assert code == 0 and "PASS" in text
    assert registry.requests[-1][3] == {"schema": proto.read_text(), "schemaType": "PROTOBUF"}
    assert len(registry.requests) == 1


def test_cli_registry_invalid_avro_is_caught_before_any_request(registry, tmp_path):
    (tmp_path / "bad.avsc").write_text("{")
    code, text = cli("check", "--registry", registry.url, tmp_path)
    assert code == 2 and "schema is invalid" in text and registry.requests == []
