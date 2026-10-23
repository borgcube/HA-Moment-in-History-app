import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "moment_in_history"))
import moment_in_history  # noqa: E402


class EventPageRefreshTest(unittest.TestCase):
    def test_event_page_refreshes_metadata_without_replacing_server_fallback(self):
        metadata = {
            "date": "2026-10-23",
            "date_label": "23 October 2026",
            "event": "Current event",
            "title": "Current event",
            "year": 1868,
            "description": "Current event description",
            "pre_moment": "The room holds its breath.",
            "source": "Test source",
            "source_url": "https://example.invalid/event",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(moment_in_history, "ROOT", root), \
                    patch.object(moment_in_history, "EVENT_JSON_PATH", root / "event.json"), \
                    patch.object(moment_in_history, "EVENT_HTML_PATH", root / "event.html"):
                moment_in_history.write_event_pages(metadata)

            page = (root / "event.html").read_text(encoding="utf-8")
            self.assertIn("event.json?refresh=' + Date.now()", page)
            self.assertIn("cache: 'no-store'", page)
            self.assertIn("Current event", page)
            self.assertIn("The room holds its breath.", page)
            self.assertEqual(
                json.loads((root / "event.json").read_text(encoding="utf-8"))["event"],
                "Current event",
            )

    def test_event_email_sends_plain_text_and_image_to_comma_separated_recipients(self):
        metadata = {
            "date_label": "23 October 2026",
            "event": "Current event",
            "pre_moment": "The room holds its breath.",
            "source": "Test source",
            "source_url": "https://example.invalid/event",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_path = root / "moment-in-history.png"
            image_path.write_bytes(b"fake png")
            smtp = MagicMock()
            smtp.__enter__.return_value = smtp
            with patch.object(moment_in_history, "EMAIL_RECIPIENTS", "one@example.com, two@example.com"), \
                    patch.object(moment_in_history, "EMAIL_SMTP_HOST", "smtp.example.com"), \
                    patch.object(moment_in_history, "EMAIL_SMTP_PORT", 587), \
                    patch.object(moment_in_history, "EMAIL_SMTP_USERNAME", "sender@example.com"), \
                    patch.object(moment_in_history, "EMAIL_SMTP_PASSWORD", "secret"), \
                    patch.object(moment_in_history, "EMAIL_FROM", "sender@example.com"), \
                    patch.object(moment_in_history, "EMAIL_SMTP_USE_TLS", False), \
                    patch.object(moment_in_history, "EMAIL_SMTP_USE_SSL", False), \
                    patch.object(moment_in_history, "ROOT", root), \
                    patch.object(moment_in_history, "LOG_PATH", root / "generation.log"), \
                    patch.object(moment_in_history.smtplib, "SMTP", return_value=smtp):
                moment_in_history.send_event_email(metadata, image_path)

            smtp.login.assert_called_once_with("sender@example.com", "secret")
            message = smtp.send_message.call_args.args[0]
            self.assertEqual(message["To"], "one@example.com, two@example.com")
            self.assertIn("The room holds its breath.", message.get_body().get_content())
            self.assertEqual(next(message.iter_attachments()).get_filename(), "moment-in-history.png")


if __name__ == "__main__":
    unittest.main()
