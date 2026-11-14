"""API-level checks: the resume endpoint's 409 mapping and the download
payload's "complete" marker (downloads must keep reporting the round as
incomplete while a resume runs or pages are still missing).

Run with the project venv:
  python -I tests/test_download_api.py
"""

import sys
import unittest

sys.path.insert(0, "/workspace/collector")

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402


class FakeCrawler:
    """Stands in for app.main.crawler; endpoints read it as a module global."""

    def __init__(self, status: str, round_id: str | None = "abc123def456"):
        self._status = status
        self._round_id = round_id
        self.resume_error: str | None = None

    def snapshot(self) -> dict:
        return {
            "round_id": self._round_id,
            "status": self._status,
            "base_url": "http://procurement-site:8080",
            "started_at": "2026-11-14T08:00:00+00:00",
            "finished_at": None,
            "resume_count": 2,
            "unfinished": [],
        }

    def records(self) -> list:
        return []

    async def start_resume(self) -> str:
        if self.resume_error:
            raise RuntimeError(self.resume_error)
        assert self._round_id is not None
        return self._round_id


class DownloadApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self._original = main.crawler

    def tearDown(self):
        main.crawler = self._original

    def _download(self, status: str) -> dict:
        main.crawler = FakeCrawler(status)
        resp = self.client.get("/api/records/download")
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def test_download_marks_incomplete_until_completed(self):
        for status in ("running", "resuming", "partial", "stopped", "stopping"):
            payload = self._download(status)
            self.assertEqual(payload["status"], status)
            self.assertIs(payload["complete"], False, f"{status} must download as incomplete")
        payload = self._download("completed")
        self.assertIs(payload["complete"], True)

    def test_download_carries_resume_count(self):
        payload = self._download("partial")
        self.assertEqual(payload["resume_count"], 2)

    def test_download_without_round_is_404(self):
        main.crawler = FakeCrawler("idle", round_id=None)
        resp = self.client.get("/api/records/download")
        self.assertEqual(resp.status_code, 404)

    def test_resume_endpoint_conflict_maps_to_409(self):
        fake = FakeCrawler("running")
        fake.resume_error = "a collection round is already running"
        main.crawler = fake
        resp = self.client.post("/api/rounds/resume")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("already running", resp.json()["detail"])

    def test_resume_endpoint_success(self):
        main.crawler = FakeCrawler("partial")
        resp = self.client.post("/api/rounds/resume")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["round_id"], "abc123def456")


if __name__ == "__main__":
    unittest.main(verbosity=2)
