"""Exercise publication boundaries against an isolated server without touching real accounts or running conversions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid


class PaperPrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="pdf2podcast-privacy-")
        os.environ.update({
            "PDF2PODCAST_DATA_DIR": cls.temporary.name,
            "PDF2PODCAST_MOBILE_TOKEN": "isolated-test-mobile-token",
            "PDF2PODCAST_PRO_EMAILS": "capcutprojects@gmail.com",
            "PDF2PODCAST_ADMIN_EMAILS": "operator@example.test",
            "PDF2PODCAST_PUBLIC_WEB_ORIGIN": "https://pdf2podcast.example.test",
            "PDF2PODCAST_WEB_UPLOADS_PER_MINUTE": "1000",
            "PDF2PODCAST_WEB_QUEUE_PER_MINUTE": "1000",
        })
        import pdf2podcast
        staged = os.environ.get("PDF2PODCAST_TEST_MODULE_DIR")
        if staged:
            pdf2podcast.__path__.insert(0, staged)
        from pdf2podcast import experience_server
        cls.app = experience_server
        cls.app.init_web_state()
        cls.cookies = {}
        for sub, email in (("pro", "capcutprojects@gmail.com"), ("free", "free@example.test"),
                           ("other", "other@example.test"), ("operator", "operator@example.test")):
            token = cls.app.create_web_session({"sub": sub, "email": email, "name": sub, "picture": ""})
            cls.app.accept_public_terms(sub, cls.app.PUBLIC_TERMS_VERSION)
            cls.cookies[sub] = cls.app.WEB_SESSION_COOKIE + "=" + token
        cls.app.read_along_map = lambda path: {"paragraphs": [{"page": 1}], "version": 1}

        class QuietHandler(cls.app.Handler):
            def log_message(self, *args):
                pass

        cls.http = cls.app.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        cls.base = "http://127.0.0.1:" + str(cls.http.server_address[1])
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join(timeout=3)
        cls.temporary.cleanup()

    def request(self, path, user=None, *, body=None, headers=None, native=False):
        request_headers = dict(headers or {})
        if user:
            request_headers["Cookie"] = self.cookies[user]
        if native:
            request_headers["Authorization"] = "Bearer isolated-test-mobile-token"
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            request_headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(self.base + path, data=body, headers=request_headers)
        try:
            response = urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            data = response.read()
            if "application/json" in response.headers.get("Content-Type", ""):
                data = json.loads(data)
            return response.status, data, response.headers

    def upload(self, user="pro", visibility=None, data=None, native=False):
        data = data or (b"%PDF-1.4\nprivacy-fixture-" + uuid.uuid4().hex.encode())
        boundary = "privacy-test-boundary"
        body = (b"--" + boundary.encode() + b'\r\nContent-Disposition: form-data; name="pdf"; filename="fixture.pdf"'
                b"\r\nContent-Type: application/pdf\r\n\r\n" + data + b"\r\n--" + boundary.encode() + b"--\r\n")
        headers = {"Content-Type": "multipart/form-data; boundary=" + boundary}
        if visibility is not None:
            headers["X-Pdf2Podcast-Visibility"] = visibility
        status, job, _ = self.request("/api/audio/upload", None if native else user, body=body, headers=headers, native=native)
        return status, job, data

    def completed(self, job):
        root = self.app.safe_job(job["job_id"])
        self.app.save_job(job["job_id"], status="complete", title="Privacy fixture", progress_percent=100)
        (root / "audio.mp3").write_bytes(b"ID3" + b"fixture-audio" * 30)
        (root / "manifest.json").write_text('{"chapters": []}')
        (root / "transcript.json").write_text('{"lines": []}')
        (root / "transcript.txt").write_text("Fixture narration")

    def toggle(self, job_id, value, user="pro", headers=None):
        return self.request("/api/web/jobs/" + job_id + "/visibility", user,
                            body={"visibility": value}, headers=headers)

    def test_only_the_designated_account_gets_pro_and_private_is_its_upload_default(self):
        for user in ("pro", "free", "other", "operator"):
            status, payload, _ = self.request("/api/web/me", user)
            self.assertEqual(status, 200)
            self.assertEqual(payload["user"]["plan"], "pro" if user == "pro" else "free")
            self.assertEqual(payload["user"]["can_upload_private"], user == "pro")
        status, job, _ = self.upload()
        self.assertEqual(status, 202)
        self.assertEqual(job["visibility"], "private")
        self.assertTrue(job["is_owner"])
        self.assertTrue(job["can_change_visibility"])
        status, public, _ = self.upload("free")
        self.assertEqual(status, 202)
        self.assertEqual(public["visibility"], "public")
        self.assertTrue(public["is_owner"])
        self.assertFalse(public["can_change_visibility"])
        self.assertEqual(self.upload("free", "private")[0], 403)
        self.assertEqual(self.toggle(public["job_id"], "private", "free")[0], 403)
        self.assertEqual(self.toggle(public["job_id"], "private", "pro")[0], 404)

    def test_private_artifacts_and_metadata_are_owner_only_not_shared_mobile_or_other_accounts(self):
        _, job, _ = self.upload()
        self.completed(job)
        base = "/api/audio/jobs/" + job["job_id"]
        for suffix in ("", "/pdf", "/audio", "/manifest", "/sync", "/transcript", "/read-along"):
            self.assertEqual(self.request(base + suffix, "pro")[0], 200, suffix)
            self.assertEqual(self.request(base + suffix)[0], 401, suffix)
            self.assertEqual(self.request(base + suffix, "other")[0], 404, suffix)
            self.assertEqual(self.request(base + suffix, native=True)[0], 404, suffix)
        status, _, headers = self.request(base + "/audio", "pro", headers={"Range": "bytes=0-7"})
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Range"].split("/")[0], "bytes 0-7")
        for route, user, native in (("/api/public/jobs", None, False), ("/api/audio/jobs", "other", False),
                                     ("/api/audio/jobs", None, True)):
            _, payload, _ = self.request(route, user, native=native)
            self.assertNotIn(job["job_id"], {entry["job_id"] for entry in payload["jobs"]})
        self.assertEqual(self.request("/api/public/jobs/" + job["job_id"] + "/share")[0], 404)
        _, detail, headers = self.request(base, "pro")
        self.assertNotIn("signature=", detail["playback_url"])
        self.assertNotIn("source_job_id", detail)
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertIn("no-store", self.request(base + "/pdf", "pro")[2]["Cache-Control"])

    def test_old_public_audio_links_stop_authorizing_playback_after_unpublishing(self):
        _, job, _ = self.upload(visibility="public")
        self.completed(job)
        _, detail, _ = self.request("/api/audio/jobs/" + job["job_id"])
        old_link = detail["playback_url"]
        self.assertIn("signature=", old_link)
        self.assertEqual(self.request(old_link)[0], 200)
        self.assertEqual(self.toggle(job["job_id"], "private")[0], 200)
        self.assertEqual(self.request(old_link)[0], 401)
        self.assertEqual(self.request(old_link, "other")[0], 404)
        self.assertEqual(self.request(old_link, native=True)[0], 404)
        self.assertEqual(self.request(old_link, "pro")[0], 200)
        self.assertEqual(self.toggle(job["job_id"], "public")[0], 200)
        self.assertEqual(self.request("/api/audio/jobs/" + job["job_id"] + "/pdf")[0], 200)

    def test_identical_uploads_share_work_but_not_entry_ids_or_visibility_control(self):
        _, private, data = self.upload()
        self.completed(private)
        queued = self.app.UPLOADS.qsize()
        status, public, _ = self.upload("free", data=data)
        self.assertEqual(status, 200)
        self.assertNotEqual(private["job_id"], public["job_id"])
        self.assertTrue(public["deduplicated"])
        self.assertEqual(public["status"], "complete")
        self.assertNotIn("source_job_id", public)
        self.assertEqual(self.app.UPLOADS.qsize(), queued)
        self.assertEqual(self.request("/api/audio/jobs/" + public["job_id"] + "/pdf")[0], 200)
        self.assertEqual(self.request("/api/audio/jobs/" + private["job_id"] + "/pdf")[0], 401)
        self.assertEqual(self.toggle(private["job_id"], "public")[0], 200)
        self.assertEqual(self.toggle(private["job_id"], "private")[0], 200)
        self.assertEqual(self.request("/api/audio/jobs/" + public["job_id"] + "/pdf")[0], 200)
        status, own, _ = self.upload("pro", data=data)
        self.assertEqual(status, 200)
        self.assertEqual(own["job_id"], private["job_id"])
        self.assertEqual(own["visibility"], "private")

    def test_native_deduplication_cannot_reveal_the_original_private_entry(self):
        _, private, data = self.upload()
        self.completed(private)
        queued = self.app.UPLOADS.qsize()
        status, native, _ = self.upload(data=data, native=True)
        self.assertEqual(status, 200)
        self.assertNotEqual(native["job_id"], private["job_id"])
        self.assertEqual(self.app.UPLOADS.qsize(), queued)
        self.assertIn("signature=", native["playback_url"])
        self.assertEqual(self.request(native["playback_url"])[0], 200)
        self.assertEqual(self.request("/api/audio/jobs/" + private["job_id"], native=True)[0], 404)

    def test_visibility_mutations_reject_cross_origin_and_invalid_values(self):
        _, job, _ = self.upload()
        self.assertEqual(self.toggle(job["job_id"], "public", headers={"Origin": "https://untrusted.example"})[0], 403)
        self.assertEqual(self.toggle(job["job_id"], "public", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.toggle(job["job_id"], "unknown")[0], 400)
        self.assertEqual(self.toggle(job["job_id"], ["public"])[0], 400)
        _, detail, _ = self.request("/api/audio/jobs/" + job["job_id"], "pro")
        self.assertEqual(detail["visibility"], "private")

    def test_free_url_imports_cannot_request_private_visibility(self):
        status, _, _ = self.request("/api/audio/import", "free",
                                    body={"url": "https://example.test/paper.pdf", "visibility": "private"})
        self.assertEqual(status, 403)


    def test_deleted_accounts_never_make_private_papers_native_even_after_restart(self):
        import importlib
        from urllib.parse import urlsplit

        app = importlib.import_module("pdf2podcast.experience_server")
        status, job, _ = self.upload()
        self.assertEqual(status, 202)
        self.completed(job)
        job_id = job["job_id"]
        audio_path = f"/api/audio/jobs/{job_id}/audio"
        self.assertEqual(self.request(audio_path, user="pro")[0], 200)
        status, published, _ = self.toggle(job_id, "public")
        self.assertEqual(status, 200)
        link = urlsplit(published["job"]["playback_url"])
        old_audio_path = link.path + ("?" + link.query if link.query else "")
        self.assertEqual(self.request(old_audio_path)[0], 200)
        self.assertEqual(self.toggle(job_id, "private")[0], 200)

        # Clone only a disposable fixture owner; never delete shared test users.
        with app.web_db() as database:
            owner = dict(database.execute(
                "SELECT * FROM users WHERE email=?", ("capcutprojects@gmail.com",)
            ).fetchone())
            owner["sub"] = "deleted-private-fixture"
            owner["email"] = "deleted-private-fixture@example.test"
            columns = ", ".join('"' + column + '"' for column in owner)
            placeholders = ", ".join("?" for _ in owner)
            database.execute(f"INSERT INTO users ({columns}) VALUES ({placeholders})", tuple(owner.values()))
            database.execute("UPDATE user_jobs SET user_sub=? WHERE job_id=?", (owner["sub"], job_id))
            database.execute("DELETE FROM users WHERE sub=?", (owner["sub"],))

        for restarted in (False, True):
            with self.subTest(restarted=restarted):
                if restarted:
                    app.init_web_state()
                self.assertFalse(app.is_legacy_job(job_id))
                self.assertIn(self.request(audio_path, native=True)[0], (401, 403, 404))
                self.assertIn(self.request(audio_path, user="other")[0], (401, 403, 404))
                self.assertIn(self.request(old_audio_path)[0], (401, 403, 404))
                self.assertIn(self.request(old_audio_path, native=True)[0], (401, 403, 404))


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)
