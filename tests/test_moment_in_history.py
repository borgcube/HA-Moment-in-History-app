import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "moment_in_history"))
import moment_in_history  # noqa: E402
import config_ui  # noqa: E402


class EventPageRefreshTest(unittest.TestCase):
    def test_log_writes_file_and_supervisor_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stdout = io.StringIO()
            with patch.object(moment_in_history, "ROOT", root), \
                    patch.object(moment_in_history, "LOG_PATH", root / "generation.log"), \
                    contextlib.redirect_stdout(stdout):
                moment_in_history.log("startup test message")

            self.assertIn("startup test message", stdout.getvalue())
            self.assertIn("startup test message", (root / "generation.log").read_text(encoding="utf-8"))

    def test_startup_report_is_actionable_without_logging_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stdout = io.StringIO()
            with patch.object(moment_in_history, "ROOT", root), \
                    patch.object(moment_in_history, "LOG_PATH", root / "generation.log"), \
                    patch.object(moment_in_history, "OPENAI_API_KEY", "secret-api-key"), \
                    patch.object(moment_in_history, "EMAIL_NOTIFY_ENTITY", "notify.moment_in_history"), \
                    patch.object(moment_in_history, "EMAIL_CAMERA_ENTITY", "camera.moment_in_history"), \
                    patch.object(moment_in_history, "SUPERVISOR_TOKEN", "secret-supervisor-token"), \
                    patch.object(moment_in_history, "ARCHIVE_IMAGES", True), \
                    contextlib.redirect_stdout(stdout):
                moment_in_history.startup_report("test")

            report = stdout.getvalue()
            self.assertIn("Moment in history App 1.8.0 starting", report)
            self.assertIn("Runtime ready:", report)
            self.assertIn("Text service configured:", report)
            self.assertIn("Image service configured:", report)
            self.assertIn("Home Assistant API: available", report)
            self.assertIn("Email delivery: enabled via Home Assistant SMTP", report)
            self.assertIn("openai_key=present", report)
            self.assertNotIn("secret-api-key", report)
            self.assertNotIn("secret-supervisor-token", report)
            self.assertIn("Image archive: enabled", report)
            self.assertIn("Startup complete: daemon is ready", report)

    def test_restart_generation_is_disabled_by_default_but_schedule_remains_active(self):
        with patch.object(moment_in_history, "GENERATE_ON_RESTART", False):
            self.assertFalse(
                moment_in_history.should_generate_today(
                    None, date(2026, 10, 23), True, startup_pending=True
                )
            )
            self.assertTrue(
                moment_in_history.should_generate_today(
                    None, date(2026, 10, 23), True, startup_pending=False
                )
            )

    def test_restart_generation_can_be_enabled_explicitly(self):
        with patch.object(moment_in_history, "GENERATE_ON_RESTART", True):
            self.assertTrue(
                moment_in_history.should_generate_today(
                    None, date(2026, 10, 23), True, startup_pending=True
                )
            )

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

    def test_event_email_calls_home_assistant_smtp_with_current_camera(self):
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
            response = MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = b"[]"
            with patch.object(moment_in_history, "EMAIL_NOTIFY_ENTITY", "notify.moment_in_history"), \
                    patch.object(moment_in_history, "EMAIL_CAMERA_ENTITY", "camera.moment_in_history"), \
                    patch.object(moment_in_history, "SUPERVISOR_TOKEN", "secret-supervisor-token"), \
                    patch.object(moment_in_history, "HOME_ASSISTANT_API_URL", "http://supervisor/core/api"), \
                    patch.object(moment_in_history, "ROOT", root), \
                    patch.object(moment_in_history, "LOG_PATH", root / "generation.log"), \
                    patch.object(moment_in_history, "urlopen", return_value=response) as urlopen:
                moment_in_history.send_event_email(metadata, image_path)

            request = urlopen.call_args.args[0]
            payload = json.loads(request.data.decode("utf-8"))
            self.assertEqual(payload["entity_id"], "notify.moment_in_history")
            self.assertIn("The room holds its breath.", payload["message"])
            self.assertEqual(
                payload["attachments"][0]["media_source"]["media_content_id"],
                "media-source://camera/camera.moment_in_history",
            )
            self.assertEqual(request.get_header("Authorization"), "Bearer secret-supervisor-token")

    def test_event_email_supports_multiple_smtp_notify_entities(self):
        metadata = {
            "date_label": "23 October 2026",
            "event": "Current event",
            "pre_moment": "The room holds its breath.",
            "source": "Test source",
            "source_url": "https://example.invalid/event",
        }
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "moment-in-history.png"
            image_path.write_bytes(b"fake png")
            response = MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = b"[]"
            with patch.object(moment_in_history, "EMAIL_NOTIFY_ENTITY", "notify.first, notify.second"), \
                    patch.object(moment_in_history, "EMAIL_CAMERA_ENTITY", "camera.moment_in_history"), \
                    patch.object(moment_in_history, "SUPERVISOR_TOKEN", "secret-supervisor-token"), \
                    patch.object(moment_in_history, "ROOT", Path(directory)), \
                    patch.object(moment_in_history, "LOG_PATH", Path(directory) / "generation.log"), \
                    patch.object(moment_in_history, "urlopen", return_value=response) as urlopen:
                moment_in_history.send_event_email(metadata, image_path)

            payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
            self.assertEqual(payload["entity_id"], ["notify.first", "notify.second"])

    def test_generation_failure_does_not_send_email(self):
        event = moment_in_history.Event(
            year=1868,
            title="Current event",
            text="Current event description",
            source="Test source",
            source_url="https://example.invalid/event",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(moment_in_history, "ROOT", root), \
                    patch.object(moment_in_history, "IMAGE_PATH", root / "moment-in-history.png"), \
                    patch.object(moment_in_history, "EVENT_JSON_PATH", root / "event.json"), \
                    patch.object(moment_in_history, "EVENT_HTML_PATH", root / "event.html"), \
                    patch.object(moment_in_history, "LOG_PATH", root / "generation.log"), \
                    patch.object(moment_in_history, "fetch_britannica_events", return_value=[event]), \
                    patch.object(moment_in_history, "choose_event", return_value=(event, "test")), \
                    patch.object(moment_in_history, "post_json_with_fallback", return_value=json.dumps({"message": {"content": "The room holds its breath."}})), \
                    patch.object(moment_in_history, "run_image_model", side_effect=RuntimeError("image backend failed")), \
                    patch.object(moment_in_history, "send_event_email") as send_email:
                with self.assertRaisesRegex(RuntimeError, "image backend failed"):
                    moment_in_history.generate(date(2026, 10, 23))

            send_email.assert_not_called()

    def test_archive_image_writes_a_dated_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_path = root / "moment-in-history.png"
            image_path.write_bytes(b"fake png")
            with patch.object(moment_in_history, "ROOT", root), \
                    patch.object(moment_in_history, "IMAGE_PATH", image_path), \
                    patch.object(moment_in_history, "ARCHIVE_IMAGES", True), \
                    patch.object(moment_in_history, "LOG_PATH", root / "generation.log"):
                moment_in_history.archive_image(date(2026, 10, 23))

            archived = root / "archive" / "2026" / "10" / "2026-10-23.png"
            self.assertEqual(archived.read_bytes(), b"fake png")


class ModelSelectionTest(unittest.TestCase):
    def test_ollama_models_are_sorted_and_deduplicated(self):
        with patch.object(
            config_ui,
            "_request_json",
            return_value={
                "models": [
                    {"name": "gemma4:e4b"},
                    {"name": "qwen3.8:27b-mlx"},
                    {"name": "gemma4:e4b"},
                    {"name": "nomic-embed-text:latest", "details": {"family": "nomic-bert"}},
                    {"name": "x/flux2-klein:4b-fp8"},
                ]
            },
        ):
            self.assertEqual(
                config_ui.ollama_models("http://ollama01.example:11434"),
                ["gemma4:e4b", "qwen3.8:27b-mlx"],
            )

    def test_openai_image_models_are_filtered(self):
        with patch.object(
            config_ui,
            "_request_json",
            return_value={"data": [{"id": "gpt-image-2"}, {"id": "gpt-5"}, {"id": "dall-e-3"}]},
        ) as request_json:
            self.assertEqual(config_ui.openai_image_models("secret-key"), ["dall-e-3", "gpt-image-2"])
            self.assertEqual(request_json.call_args.kwargs["headers"]["Authorization"], "Bearer secret-key")

    def test_model_discovery_does_not_return_openai_key(self):
        with patch.object(config_ui, "ollama_models", side_effect=[["primary-model"], ["fallback-model"]]), \
                patch.object(config_ui, "openai_image_models", return_value=["gpt-image-2"]):
            result = config_ui.discover_models(
                {
                    "primary_ollama_url": "http://ollama01.example:11434",
                    "fallback_ollama_url": "http://ollama02.example:11434",
                    "text_model": "primary-model",
                    "fallback_text_model": "fallback-model",
                    "openai_image_model": "gpt-image-2",
                    "openai_api_key": "secret-key",
                }
            )

        serialized = json.dumps(result)
        self.assertNotIn("secret-key", serialized)
        self.assertTrue(result["config"]["openai_key_present"])
        self.assertEqual(result["models"]["openai_image"], ["gpt-image-2"])

    def test_home_assistant_notify_entities_are_discovered(self):
        with patch.object(
            config_ui,
            "_request_json",
            return_value=[
                {"entity_id": "notify.email_recipient"},
                {"entity_id": "sensor.not_an_email"},
                {"entity_id": "notify.telegram"},
            ],
        ):
            self.assertEqual(
                config_ui.home_assistant_notify_entities("http://supervisor/core/api", "secret-token"),
                ["notify.email_recipient", "notify.telegram"],
            )

    def test_model_save_preserves_existing_options_and_secret(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"{}"
        with patch.object(
            moment_in_history,
            "_load_addon_options",
            return_value={"openai_api_key": "secret-key", "other_option": True, "text_model": "old-model"},
        ), patch.object(moment_in_history, "SUPERVISOR_TOKEN", "supervisor-token"), \
                patch.object(moment_in_history, "urlopen", return_value=response) as urlopen_mock:
            moment_in_history.save_model_options({"text_model": "new-model"})

        request = urlopen_mock.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["options"]["text_model"], "new-model")
        self.assertEqual(payload["options"]["openai_api_key"], "secret-key")
        self.assertTrue(payload["options"]["other_option"])
        self.assertEqual(request.get_header("Authorization"), "Bearer supervisor-token")

    def test_ingress_ui_renders_and_saves_model_selection(self):
        saved: list[dict[str, str]] = []
        server = config_ui.start_server(
            port=0,
            get_options=lambda: {
                "primary_ollama_url": "",
                "fallback_ollama_url": "",
                "text_model": "old-primary",
                "fallback_text_model": "old-fallback",
                "openai_image_model": "gpt-image-2",
                "email_notify_entity": "notify.email_recipient",
            },
            save_options=saved.append,
            log=lambda message: None,
            get_notify_entities=lambda: ["notify.email_recipient"],
        )
        port = server.server_address[1]
        try:
            with urlopen(f"http://127.0.0.1:{port}/") as response:
                page = response.read().decode("utf-8")
            self.assertIn('<select id="primary-model">', page)
            self.assertIn('<select id="email-entity" multiple', page)
            self.assertIn("Refresh available models", page)

            with urlopen(f"http://127.0.0.1:{port}/api/models") as response:
                discovery = json.loads(response.read().decode("utf-8"))
            self.assertEqual(discovery["email"]["notify_entities"], ["notify.email_recipient"])

            request = Request(
                f"http://127.0.0.1:{port}/api/config",
                data=json.dumps(
                    {
                        "text_model": "new-primary",
                        "fallback_text_model": "new-fallback",
                        "openai_image_model": "gpt-image-2",
                        "email_notify_entity": "notify.email_recipient",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertTrue(payload["restart_required"])
            self.assertEqual(
                saved,
                [{
                    "text_model": "new-primary",
                    "fallback_text_model": "new-fallback",
                    "openai_image_model": "gpt-image-2",
                    "email_notify_entity": "notify.email_recipient",
                }],
            )
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
