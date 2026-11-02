"""Exercise durable analytics and HTTP authorization without touching production data."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
from pdf2podcast import engagement
from pdf2podcast import experience_server as server


class QuietHandler(server.Handler):
    def log_message(self, *_):
        pass


class StopAfterTitle(BaseException):
    """Stop pipeline tests before external generation or audio work."""


class EngagementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        settings = patch.multiple(server, ROOT=self.root, JOBS=self.root / "jobs",
                                  WEB_DB=self.root / "web.sqlite3", WEB_ROOT=self.root,
                                  ADMIN_EMAILS={"admin@example.test"})
        settings.start()
        self.addCleanup(settings.stop)
        environment = patch.dict("os.environ", {"PDF2PODCAST_MOBILE_TOKEN": "test-mobile"})
        environment.start()
        self.addCleanup(environment.stop)
        server.init_web_state()
        self.cookies = {name: self.sign_in(name) for name in ("alice", "bob", "admin")}
        self.job = self.make_job(1)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.addCleanup(self.httpd.server_close)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)

    def sign_in(self, name):
        token = server.create_web_session({"sub": name, "email": f"{name}@example.test",
                                          "name": name.title(), "picture": f"https://example.test/{name}.png"})
        return f"{server.WEB_SESSION_COOKIE}={token}"

    def make_job(self, number, status="complete", public=True, linked_at=100):
        job_id = f"pdf-{number:016x}"
        root = server.safe_job(job_id)
        root.mkdir(parents=True)
        server.atomic_json(root / "job.json", {"job_id": job_id, "title": f"Paper {number}",
                           "status": status, "filename": "2410.12345v2.pdf"})
        self.link_job(job_id, "alice", public, linked_at)
        return job_id

    def link_job(self, job_id, owner, public=True, linked_at=100):
        with closing(server.web_db()) as database, database:
            database.execute(
                """INSERT INTO user_jobs(user_sub, job_id, filename, source_bytes, linked_at, public_release)
                   VALUES (?, ?, 'source.pdf', 10, ?, ?)""", (owner, job_id, linked_at, int(public)),
            )

    def request(self, method, path, body=None, cookie=None, raw=None, headers=None):
        headers = dict(headers or {})
        if cookie:
            headers["Cookie"] = cookie
        if body is not None:
            raw = json.dumps(body).encode()
        if raw is not None:
            headers.setdefault("Content-Type", "application/json")
        with closing(HTTPConnection(*self.httpd.server_address, timeout=5)) as connection:
            connection.request(method, path, body=raw, headers=headers)
            response = connection.getresponse()
            data = response.read()
            payload = json.loads(data) if "application/json" in response.getheader("Content-Type", "") else data
            return response.status, payload, dict(response.getheaders())

    def prepare(self, cookie=None, job=None):
        status, data, headers = self.request("GET", f"/api/public/jobs/{job or self.job}/share", cookie=cookie)
        self.assertEqual(status, 200, data)
        return data, headers

    def event(self, link, action, cookie=None, body=None):
        return self.request("POST", f"/api/public/shares/{link['code']}/{action}",
                            cookie=cookie, body={} if body is None else body)

    def visitor_cookie(self, headers):
        cookies = SimpleCookie()
        cookies.load(headers["Set-Cookie"])
        value = cookies[server.VISITOR_COOKIE]
        self.assertTrue(value["httponly"])
        self.assertTrue(value["secure"])
        self.assertEqual(value["samesite"], "Lax")
        self.assertEqual(int(value["max-age"]), 365 * 86400)
        return f"{server.VISITOR_COOKIE}={value.value}"

    def test_concurrent_counters_mapping_and_unique_visitors_survive_reopening(self):
        def record(index):
            with closing(server.web_db()) as database:
                link = engagement.prepare_share(database, self.job, "owner")
                engagement.add_listening(database, self.job, 0.25)
                engagement.record_share(database, link["code"], "owner")
                engagement.record_click(database, link["code"], f"visitor-{index % 5}")
                return link["code"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(record, range(80)))
        self.assertEqual(len(set(codes)), 1)
        with closing(server.web_db()) as database:
            engagement.init_schema(database)
            self.assertEqual(engagement.add_listening(database, self.job, 1), 21)
            stats = engagement.get_share(database, codes[0])
            self.assertEqual((stats["share_count"], stats["click_count"], stats["unique_clicks"]), (80, 80, 5))
            self.assertEqual(engagement.prepare_share(database, self.job, "owner"), stats)

    def test_click_counter_rolls_back_when_visitor_storage_fails(self):
        with closing(server.web_db()) as database:
            link = engagement.prepare_share(database, self.job, "owner")
            database.execute("""CREATE TRIGGER fail_visitor BEFORE INSERT ON engagement_visitors
                                BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
            with self.assertRaises(sqlite3.IntegrityError):
                engagement.record_click(database, link["code"], "visitor")
            self.assertEqual(engagement.get_share(database, link["code"]), link)

    def test_bounded_fractional_listening_rejects_invalid_numbers_without_changing_totals(self):
        path = f"/api/public/jobs/{self.job}/listen"
        self.assertEqual(self.request("POST", path, body={"seconds": 0.25})[:2], (200, {"listening_seconds": 0.25}))
        for seconds in (None, True, False, "1", [], {}, 0, -1, 300.1, float("nan"), float("inf"), 10 ** 1000):
            with self.subTest(seconds=str(seconds)[:20]):
                status, error, _ = self.request("POST", path, body={"seconds": seconds})
                self.assertEqual(status, 400)
                self.assertEqual(set(error), {"detail", "code"})
        self.assertEqual(self.request("POST", path, body={})[0], 400)
        self.assertEqual(self.request("POST", path, body={"seconds": 300})[1]["listening_seconds"], 300.25)
        with closing(server.web_db()) as database:
            for invalid in (True, float("nan"), float("inf"), 10 ** 1000):
                with self.assertRaises(ValueError):
                    engagement.add_listening(database, self.job, invalid)

    def test_malformed_and_oversized_engagement_requests_use_json_errors(self):
        link, _ = self.prepare(self.cookies["alice"])
        paths = [f"/api/public/jobs/{self.job}/listen", f"/api/public/shares/{link['code']}/share",
                 f"/api/public/shares/{link['code']}/click"]
        for path in paths:
            for raw in (b"{", b"[]", b"null", b'""', b"\xff", b"", b'{"x":NaN}', b"[" * 1200):
                status, error, _ = self.request("POST", path, raw=raw)
                self.assertEqual(status, 400, (path, raw[:20], error))
                self.assertEqual(set(error), {"detail", "code"})
            self.assertEqual(self.request("POST", path, raw=b" " * 4097)[0], 413)
            for length in ("invalid", "-1"):
                self.assertEqual(self.request("POST", path, raw=b"{}", headers={"Content-Length": length})[0], 400)

    def test_public_status_checks_reject_private_missing_and_incomplete_listening(self):
        queued = self.make_job(2, status="queued")
        private = self.make_job(3, public=False)
        completed = self.make_job(4, status="completed")
        for job in (private, "pdf-ffffffffffffffff", "invalid"):
            self.assertEqual(self.request("GET", f"/api/public/jobs/{job}/share")[0], 404)
            self.assertEqual(self.request("POST", f"/api/public/jobs/{job}/listen", body={"seconds": 1})[0], 404)
        self.assertEqual(self.request("POST", f"/api/public/jobs/{queued}/listen", body={"seconds": 1})[0], 409)
        self.assertEqual(self.request("POST", f"/api/public/jobs/{completed}/listen", body={"seconds": 1})[0], 200)
        self.prepare(job=queued)

    def test_html_initializes_the_guest_cookie_before_parallel_landing_requests(self):
        (self.root / "index.html").write_text("<html>Test</html>")
        status, _, headers = self.request("GET", "/?share=existing")
        self.assertEqual(status, 200)
        cookie = self.visitor_cookie(headers)
        self.assertEqual(headers["Cache-Control"], "no-store")
        link, headers = self.prepare(cookie)
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(self.event(link, "click", cookie)[1], link)
        self.assertNotIn("Set-Cookie", self.request("GET", "/", cookie=cookie)[2])

    def test_preparation_reuses_guest_links_without_counting_and_isolates_people_and_papers(self):
        link, headers = self.prepare()
        guest = self.visitor_cookie(headers)
        self.assertEqual(self.prepare(guest)[0], link)
        self.assertEqual(link["share_count"], 0)
        self.assertEqual(link["url"], f"/?share={link['code']}#read/{self.job}")
        self.assertEqual(set(link), {"code", "job_id", "url", "share_count", "click_count", "unique_clicks"})
        self.assertRegex(link["code"], r"^[A-Za-z0-9_-]{32}$")
        self.assertNotEqual(self.prepare()[0]["code"], link["code"])
        self.assertNotEqual(self.prepare(guest, self.make_job(2))[0]["code"], link["code"])
        self.assertEqual(self.event(link, "share", guest)[1]["share_count"], 1)
        self.assertEqual(self.prepare(guest)[0]["share_count"], 1)

    def test_authenticated_identity_wins_and_no_other_user_or_admin_can_claim_a_share(self):
        link, headers = self.prepare(self.cookies["alice"])
        self.assertNotIn("Set-Cookie", headers)
        second_session = self.sign_in("alice")
        self.assertEqual(self.prepare(second_session)[0], link)
        guest_link, headers = self.prepare()
        guest = self.visitor_cookie(headers)
        combined = f"{second_session}; {guest}"
        self.assertEqual(self.prepare(combined)[0], link)
        self.assertEqual(self.event(guest_link, "share", combined)[0], 403)
        for cookie in (self.cookies["bob"], self.cookies["admin"], guest, None):
            self.assertEqual(self.event(link, "share", cookie)[0], 403)
        self.assertEqual(self.event(link, "share", second_session, {"sub": "alice"})[0], 400)
        self.assertEqual(self.prepare(second_session)[0]["share_count"], 0)

    def test_clicks_ignore_self_and_deduplicate_browser_and_account_visitors(self):
        link, headers = self.prepare()
        owner = self.visitor_cookie(headers)
        self.assertEqual(self.event(link, "click", owner)[1], link)
        _, first, headers = self.event(link, "click")
        visitor = self.visitor_cookie(headers)
        self.assertEqual((first["click_count"], first["unique_clicks"]), (1, 1))
        repeated = self.event(link, "click", visitor)[1]
        self.assertEqual((repeated["click_count"], repeated["unique_clicks"]), (2, 1))
        different = self.event(link, "click")[1]
        self.assertEqual((different["click_count"], different["unique_clicks"]), (3, 2))
        account_link, _ = self.prepare(self.cookies["alice"])
        self.assertEqual(self.event(account_link, "click", self.cookies["alice"])[1], account_link)
        self.event(account_link, "click", self.cookies["bob"])
        repeated = self.event(account_link, "click", self.sign_in("bob"))[1]
        self.assertEqual((repeated["click_count"], repeated["unique_clicks"]), (2, 1))

    def test_withdrawn_or_deleted_papers_disable_existing_links(self):
        link, _ = self.prepare(self.cookies["alice"])
        with closing(server.web_db()) as database, database:
            database.execute("UPDATE user_jobs SET public_release = 0 WHERE job_id = ?", (self.job,))
        for action in ("share", "click"):
            self.assertEqual(self.event(link, action, self.cookies["alice"])[0], 404)
            self.assertEqual(self.event({"code": "missing"}, action)[0], 404)
        with closing(server.web_db()) as database, database:
            database.execute("UPDATE user_jobs SET public_release = 1 WHERE job_id = ?", (self.job,))
        (server.safe_job(self.job) / "job.json").unlink()
        self.assertEqual(self.event(link, "click")[0], 404)

    def test_catalog_ranks_before_limiting_and_breaks_ties_by_publication_time_and_id(self):
        newer = self.make_job(2, linked_at=200)
        tied = self.make_job(3, linked_at=200)
        private = self.make_job(4, public=False, linked_at=900)
        with closing(server.web_db()) as database:
            engagement.add_listening(database, self.job, 9)
            engagement.add_listening(database, private, 300)
        self.assertEqual(self.request("GET", "/api/public/jobs?limit=1")[1]["jobs"][0]["job_id"], self.job)
        catalog = self.request("GET", "/api/public/jobs")[1]["jobs"]
        self.assertEqual([item["job_id"] for item in catalog], [self.job, tied, newer])
        self.assertEqual([catalog[1][key] for key in ("listening_seconds", "share_count", "share_clicks")], [0, 0, 0])
        recent = self.request("GET", "/api/public/jobs?limit=2&sort=recent")[1]["jobs"]
        self.assertEqual([(item["job_id"], item["published_at"]) for item in recent], [(tied, 200), (newer, 200)])
        (server.safe_job(self.job) / "job.json").unlink()
        self.assertEqual(self.request("GET", "/api/public/jobs?limit=1")[1]["jobs"][0]["job_id"], tied)

    def test_catalog_aggregation_never_multiplies_shares_by_uploaders_or_exposes_identity(self):
        self.link_job(self.job, "bob")
        alice, _ = self.prepare(self.cookies["alice"])
        bob, _ = self.prepare(self.cookies["bob"])
        for link, owner in ((alice, "alice"), (alice, "alice"), (bob, "bob")):
            self.event(link, "share", self.cookies[owner])
        self.event(alice, "click", self.cookies["bob"])
        self.event(bob, "click", self.cookies["alice"])
        catalog = self.request("GET", "/api/public/jobs")[1]["jobs"]
        self.assertEqual(len(catalog), 1)
        self.assertEqual([catalog[0][key] for key in ("share_count", "share_clicks", "uploader_count")], [3, 2, 2])
        self.assertNotIn("uploader", catalog[0])
        for private_value in ("alice", "bob", "example.test", alice["code"], bob["code"]):
            self.assertNotIn(private_value, json.dumps(catalog))

    def test_dashboard_scopes_attribution_and_preserves_existing_auth_and_terms(self):
        alice, _ = self.prepare(self.cookies["alice"])
        bob, _ = self.prepare(self.cookies["bob"])
        guest, _ = self.prepare()
        status, own, headers = self.request("GET", "/api/web/me", cookie=self.cookies["alice"])
        self.assertEqual(status, 200)
        self.assertTrue(own["terms_required"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual([link["code"] for link in own["sharing"]["links"]], [alice["code"]])
        self.assertEqual(own["sharing"]["links"][0]["title"], "Paper 1")
        for field in ("user_sub", "sharer_key", "sharer_name", "sharer_email"):
            self.assertNotIn(field, own["sharing"]["links"][0])
        self.assertEqual(self.request("GET", "/api/web/sharing", cookie=self.cookies["alice"])[1], own["sharing"])
        admin = self.request("GET", "/api/web/me", cookie=self.cookies["admin"])[1]
        self.assertIn("admin_usage", admin)
        links = {link["code"]: link for link in admin["sharing"]["links"]}
        self.assertEqual(set(links), {alice["code"], bob["code"], guest["code"]})
        self.assertEqual(links[alice["code"]]["sharer_email"], "alice@example.test")
        self.assertEqual(links[bob["code"]]["sharer_name"], "Bob")
        self.assertEqual(links[guest["code"]]["sharer_name"], "Anonymous")
        self.assertNotIn("sharer_email", links[guest["code"]])
        for path in ("/api/web/me", "/api/web/sharing"):
            self.assertEqual(self.request("GET", path)[0], 401)
            self.assertEqual(self.request("GET", path, headers={"Authorization": "Bearer test-mobile"})[0], 400)
        self.assertEqual(self.request("POST", "/api/audio/upload", raw=b"pdf")[0], 401)
        self.assertEqual(self.request("POST", "/api/audio/upload", raw=b"pdf", cookie=self.cookies["alice"])[1]["code"], "public_terms_required")
        self.assertEqual(server.PUBLIC_TERMS_VERSION, "2026-10-26")

    def test_cached_titles_are_corrected_and_saved_before_resuming_audio(self):
        server.save_job(self.job, title="2410.12345v2", resume_prepared_content=True)
        root = server.safe_job(self.job)
        server.atomic_json(root / "content.json", {"version": 4, "title": "2410.12345v2", "chapters": [{"sections": []}]})
        server.atomic_json(root / "extracted.json", {"title": "Attention Is All You Need", "lines": []})
        with patch.object(server, "remove_unspeakable_sections", side_effect=StopAfterTitle):
            with self.assertRaises(StopAfterTitle):
                server.process_job(self.job)
        self.assertEqual(server.load_job(self.job)["title"], "Attention Is All You Need")
        self.assertEqual(server.read_json(root / "content.json")["title"], "Attention Is All You Need")

    def test_legacy_titles_use_the_shared_selector_before_annotation(self):
        server.save_job(self.job, title="Attention Is All You Need")
        document = {"title": "2410.12345v2", "lines": [], "page_count": 1}
        with patch.object(server, "extract_document", return_value=document), \
                patch.object(server, "annotation_batches", return_value=[]), \
                patch.object(server, "annotate_document_parallel", side_effect=StopAfterTitle):
            with self.assertRaises(StopAfterTitle):
                server.process_job(self.job)
        self.assertEqual(server.load_job(self.job)["title"], "Attention Is All You Need")
        self.assertEqual(server.read_json(server.safe_job(self.job) / "extracted.json")["title"], "Attention Is All You Need")

    def test_new_uploads_use_neutral_titles_instead_of_file_identifiers(self):
        server.accept_public_terms("alice", server.PUBLIC_TERMS_VERSION)
        with patch.object(server, "parse_pdf_upload", return_value=("2410.12345v2.pdf", b"%PDF-test")), \
                patch.object(server, "UPLOADS"):
            status, job, _ = self.request("POST", "/api/audio/upload", cookie=self.cookies["alice"], raw=b"%PDF-test")
        self.assertEqual(status, 202, job)
        self.assertEqual(job["title"], "Untitled document")


if __name__ == "__main__":
    unittest.main()
