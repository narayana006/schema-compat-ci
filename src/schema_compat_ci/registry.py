"""Minimal read-only client for the Schema Registry REST API.

Only three calls are made, none of which registers or changes anything:

  POST /compatibility/subjects/{subject}/versions?verbose=true
  GET  /config/{subject}?defaultToGlobal=true
  GET  /subjects/{subject}/versions/latest
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import quote

CONTENT_TYPE = "application/vnd.schemaregistry.v1+json"
SUBJECT_NOT_FOUND = 40401


class RegistryError(RuntimeError):
    """The registry could not be reached or answered with an unexpected error."""


@dataclass
class CompatibilityResult:
    subject_exists: bool
    is_compatible: bool
    messages: list[str] = field(default_factory=list)


class RegistryClient:
    def __init__(self, base_url: str, basic_auth: str | None = None, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {"Accept": CONTENT_TYPE + ", application/json"}
        if basic_auth:
            token = base64.b64encode(basic_auth.encode()).decode()
            self.headers["Authorization"] = f"Basic {token}"

    def _call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = None
        headers = dict(self.headers)
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = CONTENT_TYPE
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            raise RegistryError(f"cannot reach {self.base_url}: {exc}") from exc
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"message": raw.decode(errors="replace")[:200]}
        return status, payload if isinstance(payload, dict) else {"value": payload}

    @staticmethod
    def _fail(action: str, status: int, payload: dict) -> RegistryError:
        detail = payload.get("message", "")
        code = payload.get("error_code")
        return RegistryError(f"{action} failed: HTTP {status}" + (f" (error_code {code})" if code else "")
                             + (f": {detail}" if detail else ""))

    def test_compatibility(self, subject: str, schema_text: str, schema_type: str = "AVRO") -> CompatibilityResult:
        """Ask the registry whether the schema could be registered under the subject.

        The registry applies the subject's own compatibility level: against the
        latest version, or against all versions for the transitive levels.
        """
        path = f"/compatibility/subjects/{quote(subject, safe='')}/versions?verbose=true"
        status, payload = self._call("POST", path, {"schema": schema_text, "schemaType": schema_type})
        if status == 404 and payload.get("error_code") == SUBJECT_NOT_FOUND:
            return CompatibilityResult(subject_exists=False, is_compatible=True)
        if status != 200 or "is_compatible" not in payload:
            raise self._fail(f"compatibility check for subject {subject!r}", status, payload)
        return CompatibilityResult(True, bool(payload["is_compatible"]), list(payload.get("messages") or []))

    def compatibility_level(self, subject: str) -> str:
        """The level the registry uses for the subject (its own, or the global default)."""
        status, payload = self._call("GET", f"/config/{quote(subject, safe='')}?defaultToGlobal=true")
        if status != 200 or "compatibilityLevel" not in payload:
            raise self._fail(f"reading the compatibility level of {subject!r}", status, payload)
        return payload["compatibilityLevel"]

    def latest_schema(self, subject: str) -> tuple[str, str]:
        """(schema text, schema type) of the latest registered version."""
        status, payload = self._call("GET", f"/subjects/{quote(subject, safe='')}/versions/latest")
        if status != 200 or "schema" not in payload:
            raise self._fail(f"reading the latest version of {subject!r}", status, payload)
        return payload["schema"], payload.get("schemaType", "AVRO")
