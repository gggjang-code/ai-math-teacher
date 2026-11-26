import json
import unittest
from urllib.error import HTTPError
from io import BytesIO
from student_store import StudentStore, StorageError


class FakeResponse:
    def __init__(self, data=b""):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._data


class FakeOpener:
    def __init__(self):
        self.calls = []

    def __call__(self, req, timeout=15):
        self.calls.append((req.full_url, req.get_method(), req.data, dict(req.header_items())))
        if req.get_method() == "GET":
            return FakeResponse(b"[]")
        return FakeResponse()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.opener = FakeOpener()
        self.store = StudentStore("https://example.supabase.co", "fake-test-key", opener=self.opener)
        self.pid = "Q-123456abcdef"

    def test_problem_and_single_image_upload(self):
        self.store.save_problem(self.pid, "어떻게 풀어요?", b"fake image", "jpg", "image/jpeg")
        self.assertEqual(len(self.opener.calls), 2)
        self.assertIn("/storage/v1/object/student-questions/" + self.pid, self.opener.calls[0][0])
        self.assertIn("/rest/v1/student_problems", self.opener.calls[1][0])
        row = json.loads(self.opener.calls[1][2])
        self.assertEqual(row["initial_question"], "어떻게 풀어요?")
        self.assertEqual(row["file_sha256"], __import__("hashlib").sha256(b"fake image").hexdigest())

    def test_every_turn_has_unique_id_and_problem_relation(self):
        self.store.save_turn("a"*32, self.pid, "학생", "다음 힌트")
        self.store.save_turn("b"*32, self.pid, "AI", "2단계입니다.")
        self.assertEqual(len(self.opener.calls), 2)
        rows = [json.loads(call[2]) for call in self.opener.calls]
        self.assertEqual([row["role"] for row in rows], ["학생", "AI"])
        self.assertTrue(all(row["problem_id"] == self.pid for row in rows))
        self.assertNotEqual(rows[0]["turn_id"], rows[1]["turn_id"])

    def test_eight_hint_turns_share_one_problem_photo(self):
        self.store.save_problem(self.pid, "새 문제", b"image-only-once", "png", "image/png")
        for i in range(1, 9):
            self.store.save_turn(f"{i:032x}", self.pid, "학생", f"{i}단계 힌트 주세요")
            self.store.save_turn(f"{i+100:032x}", self.pid, "AI", f"{i}단계 힌트입니다")
        photo_uploads = [call for call in self.opener.calls
                         if "/storage/v1/object/" in call[0] and call[1] == "POST"]
        turn_writes = [call for call in self.opener.calls
                       if "/rest/v1/student_turns" in call[0] and call[1] == "POST"]
        self.assertEqual(len(photo_uploads), 1)
        self.assertEqual(len(turn_writes), 16)
        self.assertEqual({json.loads(call[2])["problem_id"] for call in turn_writes},
                         {self.pid})

    def test_fails_closed_on_invalid_id_and_mime(self):
        with self.assertRaises(ValueError):
            self.store.save_problem("../etc", "q")
        with self.assertRaises(ValueError):
            self.store.save_problem(self.pid, "q", b"data", "exe", "application/octet-stream")
        self.assertFalse(self.opener.calls)

    def test_http_error_omits_credentials(self):
        def fail(req, timeout=15):
            raise HTTPError(req.full_url, 403, "nope", {}, BytesIO(b"PRIVATE KEY"))
        store = StudentStore("https://example.supabase.co", "a-private-key", opener=fail)
        with self.assertRaises(StorageError) as ctx:
            store.save_turn("a"*32, self.pid, "학생", "질문")
        self.assertNotIn("a-private-key", str(ctx.exception))
        self.assertNotIn("PRIVATE KEY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
