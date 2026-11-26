"""Private remote storage for student questions and complete chat transcripts.

Optional backend: configured only via Streamlit Secrets on the server.
The application must never expose the secret API key to the browser.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError


class StorageError(RuntimeError):
    pass


class StudentStore:
    """Server-only Supabase/PostgREST + private Storage client."""

    BUCKET = "student-questions"
    MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

    def __init__(self, url: str, secret_key: str, *, opener=None):
        cleaned = url.rstrip("/")
        parsed = urlsplit(cleaned)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/"):
            raise ValueError("SUPABASE_URL must be a project https URL")
        if not secret_key or secret_key.startswith("sb_publishable_"):
            raise ValueError("Supabase server secret key is missing or invalid")
        self.url = cleaned
        self.key = secret_key
        self.opener = opener or urlopen

    def _call(self, path: str, *, method="GET", data=None, mime="application/json",
              prefer=None, raw=False):
        # Modern sb_secret keys are not JWTs and must not be used as bearer tokens.
        headers = {"apikey": self.key, "Accept": "application/json"}
        if not self.key.startswith("sb_secret_"):
            headers["Authorization"] = "Bearer " + self.key
        if data is not None:
            headers["Content-Type"] = mime
        if prefer:
            headers["Prefer"] = prefer
        req = Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with self.opener(req, timeout=15) as response:
                payload = response.read()
                return payload if raw else (json.loads(payload) if payload else None)
        except HTTPError as error:
            # No secret or student-submitted content in error messages.
            raise StorageError(f"Remote storage returned HTTP {error.code}") from None
        except Exception as error:
            raise StorageError(f"Remote storage request failed ({type(error).__name__})") from None

    @staticmethod
    def _id(problem_id: str) -> str:
        if not re.fullmatch(r"Q-[a-f0-9]{12}", problem_id):
            raise ValueError("Invalid problem ID")
        return problem_id

    def save_problem(self, problem_id: str, initial_question: str, data=None,
                     suffix=None, mime=None):
        problem_id = self._id(problem_id)
        file_path = None
        file_digest = None
        if data is not None:
            if len(data) > self.MAX_ATTACHMENT_BYTES:
                raise StorageError("File exceeds 10 MB upload limit")
            if suffix not in ("png", "jpg", "jpeg", "pdf"):
                raise ValueError("Unsupported attachment extension")
            valid_mimes = {"png": "image/png", "jpg": "image/jpeg",
                           "jpeg": "image/jpeg", "pdf": "application/pdf"}
            if mime != valid_mimes[suffix]:
                raise ValueError("Attachment MIME type does not match extension")
            file_path = f"{problem_id}/problem.{suffix}"
            file_digest = hashlib.sha256(data).hexdigest()
        row = {
            "problem_id": problem_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "initial_question": initial_question,
            "file_path": file_path,
            "file_sha256": file_digest,
        }
        # Insert the parent record first, so saving a chat turn cannot point to a
        # non-existent problem. A retry after a dropped response is idempotent.
        self._call("/rest/v1/student_problems?on_conflict=problem_id", method="POST",
                   data=json.dumps(row, ensure_ascii=False).encode("utf-8"),
                   prefer="resolution=ignore-duplicates,return=minimal")
        if file_path is not None:
            object_url = f"/storage/v1/object/{self.BUCKET}/{file_path}"
            try:
                # Idempotent: the same problem ID can be retried, but never changes
                # its uploaded content or creates an additional object.
                self._call(object_url, method="POST", data=data, mime=mime,
                           prefer="return=minimal")
            except StorageError as error:
                if "HTTP 409" not in str(error):
                    raise StorageError("Attachment could not be stored privately") from error
        return file_path

    def download_attachment(self, file_path: str) -> bytes:
        if not re.fullmatch(r"Q-[a-f0-9]{12}/problem\.(?:png|jpe?g|pdf)", file_path):
            raise ValueError("Invalid attachment path")
        payload = self._call(
            f"/storage/v1/object/authenticated/{self.BUCKET}/{quote(file_path, safe='/')}",
            raw=True)
        return payload or b""

    def save_turn(self, turn_id: str, problem_id: str, role: str, content: str,
                  status="ok"):
        problem_id = self._id(problem_id)
        if not re.fullmatch(r"[a-f0-9]{32}", turn_id):
            raise ValueError("Invalid turn ID")
        if role not in ("학생", "AI", "시스템"):
            raise ValueError("Invalid role")
        body = {"turn_id": turn_id, "problem_id": problem_id, "role": role,
                "content": content, "status": status}
        self._call("/rest/v1/student_turns?on_conflict=turn_id", method="POST",
                   data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                   prefer="resolution=ignore-duplicates,return=minimal")

    def recent_problems(self, limit=100):
        params = urlencode({"select": "problem_id,created_at,initial_question,file_path",
                            "order": "created_at.desc", "limit": min(500, max(1, limit))})
        return self._call("/rest/v1/student_problems?" + params) or []

    def turns_for_problem(self, problem_id: str):
        params = urlencode({"select": "created_at,role,content,status",
                            "problem_id": "eq." + self._id(problem_id),
                            "order": "created_at.asc"})
        return self._call("/rest/v1/student_turns?" + params) or []
