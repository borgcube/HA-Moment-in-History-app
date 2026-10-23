import contextlib
import io
import json
import signal
import sys
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from urllib.error import HTTPError
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
            self.assertIn("Moment in history App 1.9.18 starting", report)
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

    def test_web_settings_migrate_to_persistent_app_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "web-settings.json"
            with patch.object(moment_in_history, "WEB_SETTINGS_PATH", settings_path), \
                    patch.object(moment_in_history, "PERSISTED_WEB_OPTIONS", {}), \
                    patch.object(
                        moment_in_history,
                        "SUPERVISOR_OPTIONS",
                        {
                            "primary_ollama_url": "http://ollama01.example:11434",
                            "openai_api_key": "secret-key",
                            "openai_image_model": "gpt-image-2",
                        },
                    ), \
                    patch.object(moment_in_history, "ADDON_OPTIONS", {"image_size": "1024x1024"}):
                moment_in_history.migrate_web_options()
                saved = json.loads(settings_path.read_text(encoding="utf-8"))

            self.assertEqual(saved["primary_ollama_url"], "http://ollama01.example:11434")
            self.assertEqual(saved["openai_api_key"], "secret-key")
            self.assertEqual(saved["openai_image_model"], "gpt-image-2")

    def test_daemon_handles_stop_as_clean_shutdown(self):
        class FakeServer:
            def __init__(self):
                self.shutdown_called = False
                self.close_called = False

            def shutdown(self):
                self.shutdown_called = True

            def server_close(self):
                self.close_called = True

        server = FakeServer()
        shutdown_event = threading.Event()

        def start_and_request_shutdown():
            shutdown_event.set()
            return server

        with patch.object(moment_in_history, "SHUTDOWN_EVENT", shutdown_event), \
                patch.object(moment_in_history, "start_config_ui", side_effect=start_and_request_shutdown), \
                patch.object(moment_in_history.signal, "signal") as signal_mock, \
                patch.object(moment_in_history.signal, "getsignal", return_value=signal.SIG_DFL), \
                patch.object(moment_in_history, "log") as log_mock:
            self.assertEqual(moment_in_history.daemon(), 0)

        self.assertTrue(server.shutdown_called)
        self.assertTrue(server.close_called)
        self.assertTrue(any("Shutdown requested; stopping cleanly" in call.args[0] for call in log_mock.call_args_list))
        self.assertTrue(any("Shutdown complete" in call.args[0] for call in log_mock.call_args_list))
        self.assertEqual(signal_mock.call_count, 4)

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

    def test_test_email_uses_current_content_without_generating(self):
        metadata = {
            "date_label": "23 October 2026",
            "event": "Current event",
            "pre_moment": "The room holds its breath.",
            "source": "Test source",
            "source_url": "https://example.invalid/event",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event_path = root / "event.json"
            image_path = root / "moment-in-history.png"
            event_path.write_text(json.dumps(metadata), encoding="utf-8")
            image_path.write_bytes(b"fake png")
            with patch.object(moment_in_history, "EVENT_JSON_PATH", event_path), \
                    patch.object(moment_in_history, "IMAGE_PATH", image_path), \
                    patch.object(moment_in_history, "EMAIL_NOTIFY_ENTITY", "notify.test"), \
                    patch.object(moment_in_history, "EMAIL_CAMERA_ENTITY", "camera.moment_in_history"), \
                    patch.object(moment_in_history, "SUPERVISOR_TOKEN", "secret-token"), \
                    patch.object(moment_in_history, "send_event_email") as send_event_email:
                result = moment_in_history.send_test_email()

        self.assertEqual(result, {"mode": "current"})
        send_event_email.assert_called_once_with(metadata, image_path, ["notify.test"])

    def test_test_email_sends_generic_message_without_current_content(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"[]"
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(moment_in_history, "EVENT_JSON_PATH", Path(directory) / "missing.json"), \
                patch.object(moment_in_history, "IMAGE_PATH", Path(directory) / "missing.png"), \
                patch.object(moment_in_history, "ROOT", Path(directory)), \
                patch.object(moment_in_history, "LOG_PATH", Path(directory) / "generation.log"), \
                patch.object(moment_in_history, "EMAIL_NOTIFY_ENTITY", "notify.test"), \
                patch.object(moment_in_history, "SUPERVISOR_TOKEN", "secret-token"), \
                patch.object(moment_in_history, "urlopen", return_value=response) as urlopen:
            result = moment_in_history.send_test_email()

        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(result, {"mode": "generic"})
        self.assertEqual(payload["title"], "Moment in history — SMTP test")
        self.assertNotIn("attachments", payload)

    def test_test_email_can_use_unsaved_notify_targets(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"[]"
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(moment_in_history, "EVENT_JSON_PATH", Path(directory) / "missing.json"), \
                patch.object(moment_in_history, "IMAGE_PATH", Path(directory) / "missing.png"), \
                patch.object(moment_in_history, "ROOT", Path(directory)), \
                patch.object(moment_in_history, "LOG_PATH", Path(directory) / "generation.log"), \
                patch.object(moment_in_history, "SUPERVISOR_TOKEN", "secret-token"), \
                patch.object(moment_in_history, "urlopen", return_value=response) as urlopen:
            result = moment_in_history.send_test_email(["notify.unsaved_target"])

        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(result, {"mode": "generic"})
        self.assertEqual(payload["entity_id"], "notify.unsaved_target")

    def test_safe_error_preserves_actionable_local_errors(self):
        self.assertEqual(
            config_ui._safe_error(RuntimeError("Select at least one email recipient first")),
            "Select at least one email recipient first",
        )

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


class ComfyUIImageProviderTest(unittest.TestCase):
    @staticmethod
    def _response(body: bytes, content_type: str | None = None) -> MagicMock:
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = body
        if content_type:
            response.headers.get_content_type.return_value = content_type
        return response

    def test_comfyui_provider_queues_workflow_and_downloads_png(self):
        queue_response = self._response(b'{"prompt_id":"prompt-1"}')
        history_response = self._response(
            b'{"prompt-1":{"status":{"status_str":"success"},"outputs":'
            b'{"9":{"images":[{"filename":"moment.png","subfolder":"","type":"output"}]}}}}'
        )
        image_response = self._response(b"png-bytes", "image/png")
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(moment_in_history, "IMAGE_BACKEND_URL", "http://comfyui.example/comfyui"), \
                patch.object(moment_in_history, "COMFYUI_MODEL", "sd_xl_turbo_1.0.safetensors"), \
                patch.object(moment_in_history, "IMAGE_WIDTH", 512), \
                patch.object(moment_in_history, "IMAGE_HEIGHT", 512), \
                patch.object(moment_in_history, "IMAGE_STEPS", 1), \
                patch.object(moment_in_history, "urlopen", side_effect=[queue_response, history_response, image_response]) as urlopen, \
                patch.object(moment_in_history.time, "sleep"):
            image = moment_in_history.generate_comfyui_image("A small wordless scene")

        self.assertEqual(image, b"png-bytes")
        queue_request = urlopen.call_args_list[0].args[0]
        workflow_request = json.loads(queue_request.data.decode("utf-8"))
        self.assertEqual(workflow_request["prompt"]["3"]["inputs"]["ckpt_name"], "sd_xl_turbo_1.0.safetensors")
        self.assertEqual(workflow_request["prompt"]["4"]["inputs"]["width"], 512)
        self.assertEqual(workflow_request["prompt"]["7"]["inputs"]["steps"], 1)
        self.assertIn("/history/prompt-1", urlopen.call_args_list[1].args[0].full_url)
        self.assertIn("filename=moment.png", urlopen.call_args_list[2].args[0].full_url)

    def test_comfyui_provider_reports_execution_errors(self):
        queue_response = self._response(b'{"prompt_id":"prompt-1"}')
        history_response = self._response(
            b'{"prompt-1":{"status":{"status_str":"error","messages":'
            b'[["execution_error",{}]]},"outputs":{}}}'
        )
        with patch.object(moment_in_history, "IMAGE_BACKEND_URL", "http://comfyui.example"), \
                patch.object(moment_in_history, "urlopen", side_effect=[queue_response, history_response]), \
                patch.object(moment_in_history.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "execution_error"):
                moment_in_history.generate_comfyui_image("A small wordless scene")

    def test_image_provider_dispatches_to_comfyui(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "image.png"
            with patch.object(moment_in_history, "IMAGE_PROVIDER", "comfyui"), \
                    patch.object(moment_in_history, "generate_comfyui_image", return_value=b"png-bytes"):
                description = moment_in_history.run_image_model("prompt", destination)
            self.assertEqual(destination.read_bytes(), b"png-bytes")
        self.assertIn("ComfyUI", description)

    def test_comfyui_provider_uses_fallback_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "image.png"
            with patch.object(moment_in_history, "IMAGE_PROVIDER", "comfyui"), \
                    patch.object(moment_in_history, "IMAGE_BACKEND_URL", "https://primary.example/comfyui"), \
                    patch.object(moment_in_history, "IMAGE_BACKEND_FALLBACK_URL", "https://fallback.example/comfyui"), \
                    patch.object(moment_in_history, "log"), \
                    patch.object(
                        moment_in_history,
                        "generate_comfyui_image",
                        side_effect=[RuntimeError("primary unavailable"), b"fallback-png"],
                    ) as generate:
                description = moment_in_history.run_image_model("prompt", destination)

            self.assertEqual(destination.read_bytes(), b"fallback-png")
            self.assertEqual(
                [call.args for call in generate.call_args_list],
                [
                    ("prompt", "https://primary.example/comfyui"),
                    ("prompt", "https://fallback.example/comfyui"),
                ],
            )
        self.assertIn("fallback.example", description)

    def test_comfyui_provider_is_in_the_app_schema(self):
        schema = (Path(__file__).resolve().parents[1] / "moment_in_history" / "config.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("image_size: list(1024x1024|1536x1024|1024x1536)", schema)
        self.assertIn("image_steps: int(1,50)", schema)
        self.assertNotIn("image_provider:", schema)
        self.assertNotIn("openai_api_key:", schema)
        self.assertNotIn("primary_ollama_url:", schema)
        self.assertNotIn("schedule_time:", schema)
        self.assertNotIn("email_notify_entity:", schema)

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

    def test_managed_email_mapping_removes_only_the_selected_smtp_subentry(self):
        with tempfile.TemporaryDirectory() as directory:
            mapping_path = Path(directory) / "managed-recipients.json"
            recipient = {
                "email": "person@example.com",
                "entity_id": "notify.person",
                "entry_id": "smtp-entry",
                "subentry_id": "smtp-recipient",
            }
            with patch.object(moment_in_history, "MANAGED_RECIPIENTS_PATH", mapping_path), \
                    patch.object(moment_in_history.config_ui, "add_smtp_recipient", return_value=recipient), \
                    patch.object(moment_in_history.config_ui, "delete_smtp_recipient") as delete_recipient, \
                    patch.object(moment_in_history, "HOME_ASSISTANT_API_URL", "http://supervisor/core/api"), \
                    patch.object(moment_in_history, "SUPERVISOR_TOKEN", "supervisor-token"), \
                    patch.object(moment_in_history, "log"):
                self.assertEqual(
                    moment_in_history.add_managed_email_recipient("person@example.com"),
                    {"email": "person@example.com", "entity_id": "notify.person"},
                )
                moment_in_history.remove_managed_email_recipient("person@example.com")

            delete_recipient.assert_called_once_with(
                "http://supervisor/core/api", "supervisor-token", recipient
            )
            self.assertEqual(json.loads(mapping_path.read_text(encoding="utf-8")), [])


class ModelSelectionTest(unittest.TestCase):
    def test_smtp_http_errors_are_returned_as_safe_actionable_messages(self):
        def fail_configure(_configuration):
            raise HTTPError(
                "http://supervisor/core/api/config/config_entries/flow",
                400,
                "Bad Request",
                {},
                io.BytesIO(b'{"message":"invalid_auth","password":"do-not-show"}'),
            )

        server = config_ui.start_server(
            port=0,
            get_options=lambda: {"primary_ollama_url": "", "fallback_ollama_url": ""},
            save_options=lambda changes: None,
            log=lambda message: None,
            configure_smtp=fail_configure,
        )
        port = server.server_address[1]
        try:
            request = Request(
                f"http://127.0.0.1:{port}/api/smtp",
                data=json.dumps({"sender": "sender@example.com"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request)
            response = json.loads(raised.exception.read().decode("utf-8"))
            self.assertEqual(response["error"], "HTTP 400: invalid_auth")
            self.assertNotIn("password", response["error"])
        finally:
            server.shutdown()
            server.server_close()

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

    def test_ollama_models_handles_null_families(self):
        with patch.object(
            config_ui,
            "_request_json",
            return_value={"models": [{"name": "qwen3.8:27b-mlx", "details": {"families": None}}]},
        ):
            self.assertEqual(config_ui.ollama_models("http://ollama01.example:11434"), ["qwen3.8:27b-mlx"])

    def test_openai_image_models_are_filtered(self):
        with patch.object(
            config_ui,
            "_request_json",
            return_value={"data": [{"id": "gpt-image-2"}, {"id": "gpt-5"}, {"id": "dall-e-3"}]},
        ) as request_json:
            self.assertEqual(config_ui.openai_image_models("secret-key"), ["dall-e-3", "gpt-image-2"])
            self.assertEqual(request_json.call_args.kwargs["headers"]["Authorization"], "Bearer secret-key")

    def test_comfyui_checkpoints_are_discovered(self):
        with patch.object(
            config_ui,
            "_request_json",
            return_value=["sd_xl_turbo_1.0.safetensors", "custom.safetensors"],
        ):
            self.assertEqual(
                config_ui.image_backend_models("https://openclaw.example/comfyui"),
                ["custom.safetensors", "sd_xl_turbo_1.0.safetensors"],
            )

    def test_compatible_local_image_api_model_is_discovered_from_health(self):
        with patch.object(
            config_ui,
            "_request_json",
            return_value={"ok": True, "model": "Tongyi-MAI/Z-Image-Turbo"},
        ) as request_json:
            self.assertEqual(
                config_ui.image_backend_models("http://storage01.example:8188"),
                ["Tongyi-MAI/Z-Image-Turbo"],
            )
        request_json.assert_called_once_with("http://storage01.example:8188/health")

    def test_model_discovery_includes_detected_image_checkpoints(self):
        with patch.object(config_ui, "ollama_models", return_value=[]), \
                patch.object(config_ui, "image_backend_models", return_value=["detected.safetensors"]):
            result = config_ui.discover_models(
                {
                    "primary_ollama_url": "",
                    "fallback_ollama_url": "",
                    "image_backend_url": "https://openclaw.example/comfyui",
                }
            )
        self.assertEqual(result["models"]["image_backend"], ["detected.safetensors"])

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

    def test_ingress_model_discovery_uses_entered_ollama_urls(self):
        server = config_ui.start_server(
            port=0,
            get_options=lambda: {"primary_ollama_url": "", "fallback_ollama_url": ""},
            save_options=lambda changes: None,
            log=lambda message: None,
        )
        port = server.server_address[1]
        primary = "http://entered-primary.example:11434"
        fallback = "http://entered-fallback.example:11434"
        try:
            with patch.object(config_ui, "ollama_models", side_effect=[["primary-model"], ["fallback-model"]]) as discover:
                with urlopen(
                    f"http://127.0.0.1:{port}/api/models?primary_ollama_url={primary}&fallback_ollama_url={fallback}"
                ) as response:
                    result = json.loads(response.read().decode("utf-8"))
            self.assertEqual(discover.call_args_list[0].args, (primary,))
            self.assertEqual(discover.call_args_list[1].args, (fallback,))
            self.assertEqual(result["models"]["primary_ollama"], ["primary-model"])
            self.assertEqual(result["models"]["fallback_ollama"], ["fallback-model"])
            self.assertEqual(result["config"]["primary_ollama_url"], primary)
            self.assertEqual(result["config"]["fallback_ollama_url"], fallback)
        finally:
            server.shutdown()
            server.server_close()

    def test_ingress_model_discovery_uses_entered_image_backend_url(self):
        server = config_ui.start_server(
            port=0,
            get_options=lambda: {"image_backend_url": "", "image_backend_fallback_url": ""},
            save_options=lambda changes: None,
            log=lambda message: None,
        )
        port = server.server_address[1]
        image_backend = "https://openclaw.example/comfyui"
        try:
            with patch.object(config_ui, "image_backend_models", return_value=["detected.safetensors"]) as discover:
                with urlopen(
                    f"http://127.0.0.1:{port}/api/models?image_backend_url={image_backend}"
                ) as response:
                    result = json.loads(response.read().decode("utf-8"))
            discover.assert_called_once_with(image_backend)
            self.assertEqual(result["models"]["image_backend"], ["detected.safetensors"])
        finally:
            server.shutdown()
            server.server_close()

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

    def test_supported_smtp_flow_creates_and_maps_a_recipient(self):
        entry = {"domain": "smtp", "entry_id": "smtp-entry"}
        states = [{"entity_id": "notify.already_here", "attributes": {"friendly_name": "already@example.com"}}]
        created_states = [{"entity_id": "notify.new_recipient", "attributes": {"friendly_name": "new@example.com"}}]
        with patch.object(
            config_ui,
            "_request_json",
            side_effect=[
                [entry],
                [entry],
                states,
                {"flow_id": "flow-1"},
                {"type": "create_entry"},
                [entry],
                created_states,
            ],
        ) as request_json, patch.object(
            config_ui,
            "_websocket_command",
            side_effect=[
                [{"subentry_id": "subentry-1", "subentry_type": "recipient", "title": "already@example.com", "unique_id": "already@example.com"}],
                [{"subentry_id": "subentry-2", "subentry_type": "recipient", "title": "new@example.com", "unique_id": "new@example.com"}],
            ]
        ):
            recipient = config_ui.add_smtp_recipient(
                "http://supervisor/core/api", "secret-token", "new@example.com"
            )

        self.assertEqual(
            recipient,
            {
                "email": "new@example.com",
                "entity_id": "notify.new_recipient",
                "entry_id": "smtp-entry",
                "subentry_id": "subentry-2",
            },
        )
        self.assertEqual(
            request_json.call_args_list[4].kwargs["payload"],
            {"recipient": "new@example.com"},
        )

    def test_smtp_setup_flow_creates_integration_and_first_recipient_without_retaining_password(self):
        recipient = {
            "email": "new@example.com",
            "entity_id": "notify.new_recipient",
            "entry_id": "smtp-entry",
            "subentry_id": "subentry-2",
        }
        with patch.object(
            config_ui,
            "_request_json",
            side_effect=[
                [],
                {"flow_id": "flow-1"},
                {"type": "create_entry", "result": {"entry_id": "smtp-entry"}, "next_flow": ["config_subentries_flow", "subflow-1"]},
                {"type": "create_entry", "result": {"subentry_id": "subentry-2"}},
            ],
        ) as request_json, patch.object(
            config_ui, "smtp_recipient_details", return_value={"recipients": [recipient]}
        ):
            created = config_ui.configure_smtp(
                "http://supervisor/core/api",
                "secret-token",
                {
                    "sender": "sender@example.com",
                    "sender_name": "Moment in history",
                    "server": "smtp.example.com",
                    "port": "587",
                    "encryption": "starttls",
                    "username": "sender@example.com",
                    "password": "secret-password",
                    "verify_ssl": True,
                    "recipient": "new@example.com",
                },
            )

        self.assertEqual(created, recipient)
        setup_payload = request_json.call_args_list[2].kwargs["payload"]
        self.assertEqual(setup_payload["server"], "smtp.example.com")
        self.assertEqual(setup_payload["password"], "secret-password")
        self.assertNotIn("user_input", setup_payload)
        self.assertEqual(request_json.call_args_list[3].kwargs["payload"], {"recipient": "new@example.com"})

    def test_smtp_update_flow_reconfigures_existing_entry_without_options_section(self):
        entry = {
            "domain": "smtp",
            "entry_id": "smtp-entry",
            "data": {
                "sender": "sender@example.com",
                "server": "smtp.example.com",
                "port": 587,
                "encryption": "starttls",
                "verify_ssl": True,
            },
        }
        with patch.object(
            config_ui,
            "_request_json",
            side_effect=[
                [entry],
                {"flow_id": "flow-update"},
                {"type": "abort", "reason": "reconfigure_successful"},
                [entry],
            ],
        ) as request_json:
            updated = config_ui.update_smtp(
                "http://supervisor/core/api",
                "secret-token",
                {
                    "sender": "sender@example.com",
                    "sender_name": "Updated sender",
                    "server": "smtp.new.example.com",
                    "port": "465",
                    "encryption": "tls",
                    "username": "sender@example.com",
                    "password": "secret-password",
                    "verify_ssl": True,
                },
            )

        self.assertTrue(updated["configured"])
        self.assertEqual(
            request_json.call_args_list[1].kwargs["payload"],
            {"handler": "smtp", "entry_id": "smtp-entry"},
        )
        update_payload = request_json.call_args_list[2].kwargs["payload"]
        self.assertEqual(update_payload["server"], "smtp.new.example.com")
        self.assertEqual(update_payload["password"], "secret-password")
        self.assertNotIn("options", update_payload)

    def test_smtp_details_prefill_from_temporary_reconfigure_form(self):
        entry = {"domain": "smtp", "entry_id": "smtp-entry"}
        flow = {
            "type": "form",
            "flow_id": "flow-details",
            "data_schema": [
                {"name": "sender", "value": "sender@example.com"},
                {"name": "sender_name", "description": {"suggested_value": "Moment in history"}},
                {"name": "server", "value": "smtp.example.com"},
                {"name": "port", "value": 465},
                {"name": "encryption", "value": "tls"},
                {"name": "username", "value": "sender@example.com"},
                {"name": "verify_ssl", "value": False},
                {"name": "password", "value": "must-not-be-returned"},
            ],
        }
        with patch.object(
            config_ui,
            "_request_json",
            side_effect=[[entry], flow, {}],
        ) as request_json:
            details = config_ui.smtp_config_details(
                "http://supervisor/core/api",
                "secret-token",
            )

        self.assertEqual(details["sender"], "sender@example.com")
        self.assertEqual(details["sender_name"], "Moment in history")
        self.assertEqual(details["server"], "smtp.example.com")
        self.assertEqual(details["port"], 465)
        self.assertEqual(details["encryption"], "tls")
        self.assertEqual(details["username"], "sender@example.com")
        self.assertFalse(details["verify_ssl"])
        self.assertNotIn("password", details)
        self.assertEqual(request_json.call_args_list[1].kwargs["payload"], {
            "handler": "smtp",
            "entry_id": "smtp-entry",
        })
        self.assertEqual(request_json.call_args_list[2].kwargs["method"], "DELETE")
        self.assertTrue(request_json.call_args_list[2].args[0].endswith("/flow/flow-details"))

    def test_smtp_connection_checks_starttls_and_auth_without_sending(self):
        smtp = MagicMock()
        context = MagicMock()
        with patch.object(config_ui.smtplib, "SMTP", return_value=smtp) as smtp_factory, \
                patch.object(config_ui.ssl, "create_default_context", return_value=context):
            result = config_ui.test_smtp_connection(
                {
                    "sender": "sender@example.com",
                    "server": "smtp.example.com",
                    "port": "587",
                    "encryption": "starttls",
                    "username": "sender@example.com",
                    "password": "secret-password",
                    "verify_ssl": True,
                }
            )

        self.assertEqual(
            result,
            {"ok": True, "authenticated": True, "server": "smtp.example.com", "port": 587},
        )
        smtp_factory.assert_called_once_with("smtp.example.com", 587, timeout=15)
        self.assertEqual(smtp.ehlo.call_count, 2)
        smtp.starttls.assert_called_once_with(context=context)
        smtp.login.assert_called_once_with("sender@example.com", "secret-password")
        smtp.quit.assert_called_once_with()

    def test_smtp_connection_uses_saved_password_when_editing_existing_entry(self):
        smtp = MagicMock()
        context = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            storage_path = Path(directory) / "core.config_entries"
            storage_path.write_text(
                json.dumps(
                    {
                        "data": [
                            {
                                "domain": "smtp",
                                "data": {
                                    "username": "sender@example.com",
                                    "password": "saved-password",
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(config_ui, "SMTP_STORAGE_PATH", storage_path), \
                    patch.object(config_ui.smtplib, "SMTP", return_value=smtp), \
                    patch.object(config_ui.ssl, "create_default_context", return_value=context):
                result = config_ui.test_smtp_connection(
                    {
                        "sender": "sender@example.com",
                        "server": "smtp.example.com",
                        "port": "587",
                        "encryption": "starttls",
                        "username": "sender@example.com",
                        "password": "",
                        "use_saved_credentials": True,
                        "verify_ssl": True,
                    }
                )

        self.assertTrue(result["authenticated"])
        smtp.login.assert_called_once_with("sender@example.com", "saved-password")

    def test_ingress_ui_exposes_smtp_setup_endpoint(self):
        settings: list[dict[str, object]] = []
        server = config_ui.start_server(
            port=0,
            get_options=lambda: {"primary_ollama_url": "", "fallback_ollama_url": ""},
            save_options=lambda changes: None,
            log=lambda message: None,
            configure_smtp=lambda configuration: (settings.append(configuration) or {"email": "new@example.com", "entity_id": "notify.new"}),
        )
        port = server.server_address[1]
        try:
            request = Request(
                f"http://127.0.0.1:{port}/api/smtp",
                data=json.dumps(
                    {
                        "sender": "sender@example.com",
                        "server": "smtp.example.com",
                        "port": "587",
                        "encryption": "starttls",
                        "password": "secret-password",
                        "verify_ssl": True,
                        "recipient": "new@example.com",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["recipient"]["entity_id"], "notify.new")
            self.assertEqual(settings[0]["server"], "smtp.example.com")
            self.assertEqual(settings[0]["password"], "secret-password")
        finally:
            server.shutdown()
            server.server_close()

    def test_ingress_ui_exposes_smtp_test_and_update_controls(self):
        updated: list[dict[str, object]] = []
        test_email_targets: list[list[str] | None] = []
        smtp_tested: list[dict[str, object]] = []
        server = config_ui.start_server(
            port=0,
            get_options=lambda: {"primary_ollama_url": "", "fallback_ollama_url": ""},
            save_options=lambda changes: None,
            log=lambda message: None,
            smtp_details=lambda: {"configured": True, "server": "smtp.example.com"},
            update_smtp=lambda settings: (updated.append(settings) or {"configured": True}),
            test_email=lambda targets=None: (test_email_targets.append(targets) or {"mode": "current"}),
            test_smtp_connection=lambda settings: (
                smtp_tested.append(settings) or {"ok": True, "authenticated": True}
            ),
        )
        port = server.server_address[1]
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/smtp") as response:
                details = json.loads(response.read().decode("utf-8"))
            self.assertTrue(details["configured"])

            request = Request(
                f"http://127.0.0.1:{port}/api/test-email",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                self.assertEqual(json.loads(response.read().decode("utf-8")), {"mode": "current"})

            request = Request(
                f"http://127.0.0.1:{port}/api/test-email",
                data=json.dumps({"entity_ids": ["notify.one", "notify.two"]}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                self.assertEqual(json.loads(response.read().decode("utf-8")), {"mode": "current"})
            self.assertEqual(test_email_targets, [None, ["notify.one", "notify.two"]])

            request = Request(
                f"http://127.0.0.1:{port}/api/smtp-test",
                data=json.dumps(
                    {
                        "sender": "sender@example.com",
                        "server": "smtp.example.com",
                        "port": "587",
                        "encryption": "starttls",
                        "username": "sender@example.com",
                        "password": "secret-password",
                        "recipient": "must-not-be-forwarded@example.com",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                self.assertEqual(
                    json.loads(response.read().decode("utf-8")),
                    {"ok": True, "authenticated": True},
                )
            self.assertEqual(smtp_tested[0]["server"], "smtp.example.com")
            self.assertNotIn("recipient", smtp_tested[0])

            request = Request(
                f"http://127.0.0.1:{port}/api/smtp",
                data=json.dumps(
                    {
                        "action": "update",
                        "sender": "sender@example.com",
                        "server": "smtp.new.example.com",
                        "port": "465",
                        "encryption": "tls",
                        "password": "secret-password",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                self.assertTrue(json.loads(response.read().decode("utf-8"))["settings"]["configured"])
            self.assertEqual(updated[0]["server"], "smtp.new.example.com")
        finally:
            server.shutdown()
            server.server_close()

    def test_ingress_ui_manages_email_recipient_endpoint(self):
        added: list[str] = []
        removed: list[str] = []
        server = config_ui.start_server(
            port=0,
            get_options=lambda: {"primary_ollama_url": "", "fallback_ollama_url": ""},
            save_options=lambda changes: None,
            log=lambda message: None,
            get_notify_entities=lambda: ["notify.email_recipient"],
            get_managed_recipients=lambda: {
                "available": True,
                "message": "SMTP recipients are managed by Home Assistant.",
                "recipients": [{"email": "managed@example.com", "entity_id": "notify.managed"}],
            },
            add_email_recipient=lambda email: (added.append(email) or {"email": email, "entity_id": "notify.added"}),
            remove_email_recipient=lambda email: removed.append(email),
        )
        port = server.server_address[1]
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/models") as response:
                discovery = json.loads(response.read().decode("utf-8"))
            self.assertEqual(discovery["email"]["managed_recipients"][0]["email"], "managed@example.com")
            request = Request(
                f"http://127.0.0.1:{port}/api/email-recipient",
                data=json.dumps({"action": "add", "email": "new@example.com"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                self.assertEqual(json.loads(response.read().decode("utf-8"))["recipient"]["entity_id"], "notify.added")
            request = Request(
                f"http://127.0.0.1:{port}/api/email-recipient",
                data=json.dumps({"action": "remove", "email": "new@example.com"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request):
                pass
            self.assertEqual(added, ["new@example.com"])
            self.assertEqual(removed, ["new@example.com"])
        finally:
            server.shutdown()
            server.server_close()

    def test_model_save_preserves_existing_options_and_secret(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"{}"
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(
                moment_in_history,
                "_load_addon_options",
                return_value={"openai_api_key": "secret-key", "other_option": True, "text_model": "old-model"},
            ), patch.object(moment_in_history, "SUPERVISOR_TOKEN", "supervisor-token"), \
                    patch.object(moment_in_history, "WEB_SETTINGS_PATH", Path(directory) / "web-settings.json"), \
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
            self.assertIn('id="email-enabled"', page)
            self.assertIn('id="smtp-dialog"', page)
            self.assertIn('id="test-email"', page)
            self.assertIn('id="edit-smtp"', page)
            self.assertIn('id="smtp-test-connection"', page)
            self.assertIn("Test connection", page)
            self.assertIn('id="test-email-dialog"', page)
            self.assertIn('id="test-email-entities"', page)
            self.assertIn("Choose where this one test email should go", page)
            self.assertIn('Configure SMTP settings', page)
            self.assertIn("label[for=\"smtp-recipient\"]", page)
            self.assertNotIn(".closest('label')", page)
            self.assertIn("Refresh now", page)
            self.assertIn("Open WebUI is not required", page)
            self.assertIn('id="primary-url-input"', page)
            self.assertIn('id="fallback-url-input"', page)
            self.assertIn('id="image-provider"', page)
            self.assertIn('id="openai-key"', page)
            self.assertIn('id="schedule-time"', page)
            self.assertIn('id="email-camera-entity"', page)
            self.assertIn('id="save" type="button">Save configuration', page)
            self.assertIn("Fallback local image API / ComfyUI URL", page)
            self.assertIn('id="comfyui-model-options"', page)
            self.assertIn("dirtyFields", page)
            self.assertIn("/api/tags", page)
            self.assertIn('id="openai-model"', page)
            self.assertIn("document.addEventListener('visibilitychange'", page)
            self.assertIn("window.setInterval", page)

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
                        "primary_ollama_url": "http://ollama01.example:11434",
                        "fallback_ollama_url": "http://ollama02.example:11434",
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
                    "primary_ollama_url": "http://ollama01.example:11434",
                    "fallback_ollama_url": "http://ollama02.example:11434",
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
