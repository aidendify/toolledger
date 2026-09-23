"""ToolLedger local verifier-style tests (PRD §10)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

# Isolate env before importing app
_TMP = tempfile.mkdtemp(prefix="toolledger-test-")
os.environ["DATABASE_PATH"] = str(Path(_TMP) / "test.db")
os.environ["SECRET_KEY"] = "test-secret"
os.environ["OWNER_PASSWORD"] = "testpass"
os.environ["BUSINESS_NAME"] = "Harbor HVAC"
os.environ["PUBLIC_BASE_URL"] = "http://127.0.0.1:8080"
os.environ["OVERDUE_HOURS"] = "24"
os.environ["MARKETING_URL"] = ""
for k in (
    "SMTP_HOST",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_FROM_NUMBER",
    "OWNER_EMAIL",
    "OWNER_PHONE",
):
    os.environ.pop(k, None)

import app as app_module  # noqa: E402
import helpers as H  # noqa: E402

SAMPLE = Path(__file__).resolve().parent / "sample-tools.csv"


class ToolLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        path = Path(os.environ["DATABASE_PATH"])
        if path.exists():
            path.unlink()
        H.init_schema(H.connect_db(str(path)))
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        self.app = app_module.app

    def _login(self):
        return self.client.post(
            "/login",
            data={"password": "testpass"},
            follow_redirects=False,
        )

    def test_health_public(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertFalse(data["smtp_configured"])
        self.assertFalse(data["twilio_configured"])

    def test_auth_gates_home(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers.get("Location", ""))
        self._login()
        r2 = self.client.get("/")
        self.assertEqual(r2.status_code, 200)

    def test_import_tokens_labels(self):
        self._login()
        with SAMPLE.open("rb") as fh:
            r = self.client.post(
                "/tools/import",
                data={"file": (fh, "sample-tools.csv")},
                content_type="multipart/form-data",
                follow_redirects=True,
            )
        self.assertEqual(r.status_code, 200)
        with self.app.app_context():
            db = H.get_db()
            tools = H.list_tools(db)
            self.assertGreaterEqual(len(tools), 6)
            for t in tools:
                self.assertTrue(t["token"])
        labels = self.client.get("/labels")
        self.assertEqual(labels.status_code, 200)
        body = labels.get_data(as_text=True)
        self.assertIn("http://127.0.0.1:8080/t/", body)

    def test_checkout_return_force_transfer(self):
        self._login()
        with self.app.app_context():
            db = H.get_db()
            tid = H.create_tool(db, name="Leak detector")
            a = H.create_person(db, name="Alex Tech", role="Tech")
            b = H.create_person(db, name="Blake Helper", role="Helper")
            tool = H.get_tool(db, tid)
            token = tool["token"]

        # Public page reachable without login (new client)
        pub = app_module.app.test_client()
        r = pub.get(f"/t/{token}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("available", r.get_data(as_text=True).lower())

        r = pub.post(
            f"/t/{token}/checkout",
            data={"person_id": str(a), "job_ref": "Job-42", "note": "AM run"},
            follow_redirects=True,
        )
        self.assertEqual(r.status_code, 200)
        with self.app.app_context():
            tool = H.get_tool_by_token(H.get_db(), token)
            self.assertEqual(tool["status"], "out")
            self.assertEqual(tool["holder_person_id"], a)
            self.assertEqual(tool["job_ref"], "Job-42")

        # Force transfer without confirm via checkout → need_confirm page
        r = pub.post(
            f"/t/{token}/checkout",
            data={"person_id": str(b), "job_ref": "Job-99"},
            follow_redirects=False,
        )
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("confirm", body.lower())
        self.assertIn("force", body.lower())

        # Explicit transfer with confirm
        r = pub.post(
            f"/t/{token}/transfer",
            data={
                "person_id": str(b),
                "job_ref": "Job-99",
                "confirm_transfer": "1",
            },
            follow_redirects=True,
        )
        self.assertEqual(r.status_code, 200)
        with self.app.app_context():
            db = H.get_db()
            tool = H.get_tool_by_token(db, token)
            self.assertEqual(tool["status"], "out")
            self.assertEqual(tool["holder_person_id"], b)
            kinds = [e["kind"] for e in H.tool_events(db, tid)]
            self.assertIn("force_transfer", kinds)
            self.assertIn("checked_out", kinds)

        r = pub.post(f"/t/{token}/return", data={}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        with self.app.app_context():
            tool = H.get_tool_by_token(H.get_db(), token)
            self.assertEqual(tool["status"], "available")
            self.assertIsNone(tool["holder_person_id"])

        # Idempotent return
        r = pub.post(f"/t/{token}/return", data={}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)

    def test_missing_overdue_custody_csv(self):
        self._login()
        with self.app.app_context():
            db = H.get_db()
            tid = H.create_tool(db, name="Recovery machine")
            pid = H.create_person(db, name="Casey")
            tool = H.get_tool(db, tid)
            H.checkout_tool(db, tool, person_id=pid, actor="owner")
            tool = H.get_tool(db, tid)
            H.mark_missing(db, tool, missing=True)

        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Recovery machine", r.get_data(as_text=True))

        csv_r = self.client.get("/export/custody.csv")
        self.assertEqual(csv_r.status_code, 200)
        text = csv_r.get_data(as_text=True)
        self.assertIn("Recovery machine", text)
        self.assertGreater(len(text.strip().splitlines()), 1)

        # Overdue path via short threshold
        with self.app.app_context():
            db = H.get_db()
            H.set_setting(db, "overdue_hours", "0")
            tid2 = H.create_tool(db, name="Torque wrench")
            p2 = H.create_person(db, name="Dana")
            t2 = H.get_tool(db, tid2)
            H.checkout_tool(db, t2, person_id=p2, actor="owner")
            items = H.missing_or_overdue(db)
            names = {i["name"] for i in items}
            self.assertIn("Torque wrench", names)
            self.assertIn("Recovery machine", names)

    def test_works_without_smtp_twilio(self):
        self._login()
        r = self.client.post("/alerts/missing", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True).lower()
        self.assertTrue("not configured" in body or "no missing" in body or "notification" in body)


if __name__ == "__main__":
    unittest.main()
