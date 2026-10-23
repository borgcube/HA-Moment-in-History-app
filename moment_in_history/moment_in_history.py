#!/usr/bin/env python3
"""Generate the daily "Moment in history" image and publish its metadata.

The script deliberately tries Britannica first.  Some network paths receive a
Cloudflare challenge instead of the page, so a public, machine-readable
Wikimedia On This Day feed is used as a documented fallback.  Event selection
and the image prompt are kept separate so the generated image is always the
moment immediately before the selected event, rather than the event itself.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import html
import json
import os
import re
import signal
import shutil
import secrets
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

import config_ui


TIME_ZONE = ZoneInfo("Europe/Amsterdam")
APP_VERSION = "1.9.6"
POLL_INTERVAL_SECONDS = 30
SHUTDOWN_EVENT = threading.Event()
HOME_ASSISTANT_API_URL = os.environ.get(
    "HOME_ASSISTANT_API_URL", "http://supervisor/core/api"
).rstrip("/")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "").strip()


def _load_addon_options() -> dict[str, Any]:
    """Load Home Assistant add-on options when running inside an add-on."""
    options_path = Path("/data/options.json")
    if not options_path.exists():
        return {}
    try:
        value = json.loads(options_path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


ADDON_OPTIONS = _load_addon_options()
WEB_SETTINGS_PATH = Path(
    os.environ.get("MOMENT_WEB_SETTINGS_FILE", "/data/moment-in-history-web-settings.json")
)


def _load_supervisor_options() -> dict[str, Any]:
    """Read the Supervisor option set when the local options file is filtered."""
    if not SUPERVISOR_TOKEN:
        return {}
    request = Request(
        "http://supervisor/addons/self/info",
        headers={
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
            "Accept": "application/json",
            "User-Agent": "BORGcube-MomentInHistory/1.9.6",
        },
    )
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - local options remain a safe fallback
        print(
            f"Supervisor option lookup unavailable: {type(exc).__name__}",
            file=sys.stderr,
            flush=True,
        )
        return {}
    candidates = [payload]
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        candidates.append(payload["data"])
    for candidate in candidates:
        if isinstance(candidate, dict) and isinstance(candidate.get("options"), dict):
            return dict(candidate["options"])
    return {}


def _load_persisted_web_options() -> dict[str, Any]:
    """Load Web UI settings kept in the App's persistent data directory."""
    try:
        payload = json.loads(WEB_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        field: payload[field]
        for field in config_ui.WEB_SETTINGS_FIELDS
        if field in payload
    }


SUPERVISOR_OPTIONS = _load_supervisor_options()
PERSISTED_WEB_OPTIONS = _load_persisted_web_options()


def _runtime_options() -> dict[str, Any]:
    """Return Web UI settings merged with defaults and Supervisor options."""
    options = dict(config_ui.DEFAULT_WEB_OPTIONS)
    options.update(ADDON_OPTIONS)
    options.update(SUPERVISOR_OPTIONS)
    options.update(PERSISTED_WEB_OPTIONS)
    return options


def migrate_web_options() -> None:
    """Preserve Web UI settings before Supervisor filters removed schema fields."""
    if PERSISTED_WEB_OPTIONS:
        return
    source = {
        field: source_options[field]
        for field in config_ui.WEB_SETTINGS_FIELDS
        for source_options in (SUPERVISOR_OPTIONS, ADDON_OPTIONS)
        if field in source_options
    }
    if not source:
        return
    try:
        WEB_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        WEB_SETTINGS_PATH.write_text(json.dumps(source, indent=2) + "\n", encoding="utf-8")
        PERSISTED_WEB_OPTIONS.update(source)
    except OSError as exc:
        print(f"Web UI settings migration deferred: {exc}", file=sys.stderr, flush=True)


def _setting(env_name: str, option_name: str, default: str) -> str:
    return os.environ.get(env_name) or str(_runtime_options().get(option_name, default))


ROOT = Path(_setting("MOMENT_ROOT", "root", "/config/www/moment-in-history"))
IMAGE_PATH = ROOT / "moment-in-history.png"
EVENT_JSON_PATH = ROOT / "event.json"
EVENT_HTML_PATH = ROOT / "event.html"
LOCK_PATH = ROOT / ".generation.lock"
LOG_PATH = ROOT / "generation.log"

BRITANNICA_URL = "https://www.britannica.com/on-this-day"
WIKIMEDIA_URL = "https://api.wikimedia.org/feed/v1/wikipedia/en/onthisday/events/{month:02d}/{day:02d}"

OLLAMA_BASE_URL = _setting("OLLAMA_BASE_URL", "primary_ollama_url", "http://ollama01.borgcube.com:11434")
OLLAMA_FALLBACK_URL = _setting("OLLAMA_FALLBACK_URL", "fallback_ollama_url", "http://ollama02.borgcube.com:11434")
TEXT_MODEL = _setting("MOMENT_TEXT_MODEL", "text_model", "qwen3.8:27b-mlx")
FALLBACK_TEXT_MODEL = _setting("MOMENT_FALLBACK_TEXT_MODEL", "fallback_text_model", "gemma4:e4b")
IMAGE_MODEL = _setting("MOMENT_IMAGE_MODEL", "image_model", "Tongyi-MAI/Z-Image-Turbo")
COMFYUI_MODEL = _setting(
    "MOMENT_COMFYUI_MODEL", "comfyui_model", "sd_xl_turbo_1.0.safetensors"
)
IMAGE_BACKEND_URL = _setting("MOMENT_IMAGE_BACKEND_URL", "image_backend_url", "http://ollama02.borgcube.com:8188")
IMAGE_BACKEND_FALLBACK_URL = _setting("MOMENT_IMAGE_BACKEND_FALLBACK_URL", "image_backend_fallback_url", "").strip()
IMAGE_PROVIDER = _setting("MOMENT_IMAGE_PROVIDER", "image_provider", "openai").strip().lower()
OPENAI_API_KEY = _setting("OPENAI_API_KEY", "openai_api_key", "").strip()
OPENAI_IMAGE_MODEL = _setting("OPENAI_IMAGE_MODEL", "openai_image_model", "gpt-image-2")
OPENAI_IMAGE_QUALITY = _setting("OPENAI_IMAGE_QUALITY", "openai_image_quality", "high")
_LEGACY_IMAGE_SIZE = str(ADDON_OPTIONS.get("openai_image_size", "1024x1024"))
OPENAI_IMAGE_SIZE = os.environ.get("OPENAI_IMAGE_SIZE") or str(
    ADDON_OPTIONS.get("image_size", _LEGACY_IMAGE_SIZE)
)
if OPENAI_IMAGE_SIZE not in {"1024x1024", "1536x1024", "1024x1536"}:
    OPENAI_IMAGE_SIZE = "1024x1024"
OPENAI_IMAGES_URL = _setting("OPENAI_IMAGES_URL", "openai_images_url", "https://api.openai.com/v1/images/generations")
IMAGE_SIZE_DIMENSIONS = {
    "1024x1024": (1024, 1024),
    "1536x1024": (1536, 1024),
    "1024x1536": (1024, 1536),
}


def _image_dimensions(size: str) -> tuple[int, int]:
    """Return dimensions for a supported size, with a safe legacy fallback."""
    if size in IMAGE_SIZE_DIMENSIONS:
        return IMAGE_SIZE_DIMENSIONS[size]
    try:
        legacy_width = int(ADDON_OPTIONS.get("image_width", 1024))
        legacy_height = int(ADDON_OPTIONS.get("image_height", 1024))
        if legacy_width > 0 and legacy_height > 0:
            return legacy_width, legacy_height
    except (TypeError, ValueError):
        pass
    return IMAGE_SIZE_DIMENSIONS["1024x1024"]


IMAGE_WIDTH, IMAGE_HEIGHT = _image_dimensions(OPENAI_IMAGE_SIZE)
IMAGE_STEPS = int(_setting("MOMENT_IMAGE_STEPS", "image_steps", "9"))
IMAGE_URL = _setting("MOMENT_IMAGE_URL", "image_url", "/local/moment-in-history/moment-in-history.png")
SCHEDULE_TIME = _setting("MOMENT_SCHEDULE_TIME", "schedule_time", "00:05")
GENERATE_ON_RESTART = _setting("MOMENT_GENERATE_ON_RESTART", "generate_on_restart", "false").lower() in {"1", "true", "yes", "on"}
RUN_ON_STARTUP = _setting("MOMENT_RUN_ON_STARTUP", "run_on_startup", "false").lower() in {"1", "true", "yes", "on"}
EMAIL_NOTIFY_ENTITY = _setting("EMAIL_NOTIFY_ENTITY", "email_notify_entity", "").strip()
EMAIL_CAMERA_ENTITY = _setting(
    "EMAIL_CAMERA_ENTITY", "email_camera_entity", "camera.moment_in_history"
).strip()
ARCHIVE_IMAGES = _setting("MOMENT_ARCHIVE_IMAGES", "archive_images", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
CONFIG_UI_PORT = int(_setting("MOMENT_CONFIG_UI_PORT", "config_ui_port", "8099"))
MANAGED_RECIPIENTS_PATH = Path(
    os.environ.get("MOMENT_MANAGED_RECIPIENTS_FILE", "/data/moment-in-history-managed-recipients.json")
)


def configured_notify_entities() -> list[str]:
    """Parse one or more comma-separated Home Assistant notify entities."""
    return [value.strip() for value in EMAIL_NOTIFY_ENTITY.split(",") if value.strip()]


@dataclass(frozen=True)
class Event:
    year: int | None
    title: str
    text: str
    source: str
    source_url: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "year": self.year,
            "title": self.title,
            "text": self.text,
            "source": self.source,
            "source_url": self.source_url,
        }


class VisibleTextParser(HTMLParser):
    """Collect readable blocks without depending on third-party packages."""

    BLOCK_TAGS = {"p", "li", "h1", "h2", "h3", "h4", "article"}
    SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "header", "footer"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._block_depth = 0
        self._buffer: list[str] = []
        self.blocks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        if tag in self.BLOCK_TAGS and not self._skip_depth:
            self._block_depth += 1
            if self._block_depth == 1:
                self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.BLOCK_TAGS and self._block_depth and not self._skip_depth:
            self._block_depth -= 1
            if self._block_depth == 0:
                value = clean_text(" ".join(self._buffer))
                if value:
                    self.blocks.append(value)
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and self._block_depth:
            self._buffer.append(data)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def fetch_bytes(url: str, *, timeout: int = 30) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "BORGcube-MomentInHistory/1.0 (+https://github.com/borgcube/HA-Moment-in-History-app)",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_json(url: str) -> dict[str, Any]:
    return json.loads(fetch_bytes(url).decode("utf-8"))


def parse_year(text: str) -> int | None:
    match = re.search(r"(?<!\d)(\d{1,4})(?:\s*(?:BCE|BC|CE|AD))?(?!\d)", text, re.I)
    return int(match.group(1)) if match else None


def fetch_britannica_events(day: date) -> list[Event]:
    """Best-effort extraction from Britannica's current On This Day page."""
    raw = fetch_bytes(BRITANNICA_URL).decode("utf-8", "replace")
    lowered = raw.lower()
    if "just a moment" in lowered or "cf_chl_opt" in lowered or len(raw) < 20_000:
        raise RuntimeError("Britannica returned a bot-challenge page")

    parser = VisibleTextParser()
    parser.feed(raw)
    blocks = parser.blocks
    events: list[Event] = []
    seen: set[str] = set()
    event_section = False
    for block in blocks:
        marker = block.lower()
        if marker in {"events", "historical events", "on this day"}:
            event_section = True
            continue
        if marker in {"births", "deaths", "holidays", "related"}:
            event_section = False
        if not event_section or len(block) < 35:
            continue
        if not re.search(r"\b\d{2,4}\b", block):
            continue
        normalized = block[:700]
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        events.append(Event(parse_year(normalized), normalized, normalized, "Britannica", BRITANNICA_URL))
    if not events:
        raise RuntimeError("Britannica page contained no parseable event blocks")
    return events


def fetch_wikimedia_events(day: date) -> list[Event]:
    payload = fetch_json(WIKIMEDIA_URL.format(month=day.month, day=day.day))
    events: list[Event] = []
    for item in payload.get("events", []):
        text = clean_text(str(item.get("text", "")))
        if not text:
            continue
        pages = item.get("pages") or []
        first_page = pages[0] if pages else {}
        titles = first_page.get("titles") or {}
        urls = first_page.get("content_urls") or {}
        desktop = urls.get("desktop") or {}
        title = clean_text(str(titles.get("normalized") or text))
        events.append(
            Event(
                year=item.get("year"),
                title=title,
                text=text,
                source="Wikimedia On This Day (fallback)",
                source_url=desktop.get("page") or WIKIMEDIA_URL.format(month=day.month, day=day.day),
            )
        )
    if not events:
        raise RuntimeError("Wikimedia returned no events")
    return events


def choose_event(events: list[Event]) -> tuple[Event, str]:
    """Use local Ollama to rank all candidates, with a deterministic fallback."""
    numbered = "\n".join(f"{i}: {event.year or '?'} — {event.text}" for i, event in enumerate(events))
    prompt = f"""You are selecting one event for a daily historical illustration.
Choose the single most impactful event from the complete list below. Prefer events
with broad, lasting consequences or unusually vivid historical stakes. Do not choose
a birth, death, holiday, or trivial detail. Return JSON only with this exact shape:
{{"index": 0, "why": "one concise sentence"}}

Complete candidate list:
{numbered}
"""
    try:
        payload = {
            "model": TEXT_MODEL,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1},
            "messages": [{"role": "user", "content": prompt}],
        }
        response = json.loads(post_json_with_fallback("/api/chat", payload))
        content = response.get("message", {}).get("content", "")
        decision = json.loads(content)
        index = int(decision.get("index", 0))
        if 0 <= index < len(events):
            return events[index], clean_text(str(decision.get("why", "Selected by local Ollama.")))
    except Exception as exc:  # noqa: BLE001 - deterministic fallback is intentional
        log(f"Ollama event ranking failed: {exc}")

    weights = {
        "war": 8,
        "revolution": 8,
        "crisis": 8,
        "president": 6,
        "invasion": 8,
        "disaster": 7,
        "first": 5,
        "invention": 5,
        "discovery": 5,
        "independence": 7,
        "treaty": 6,
        "launch": 4,
        "speech": 4,
    }
    scored = [(sum(weight for word, weight in weights.items() if word in event.text.lower()), i, event) for i, event in enumerate(events)]
    _, _, winner = max(scored, key=lambda item: (item[0], len(item[2].text)))
    return winner, "Selected by the local deterministic fallback ranking."


def post_json(url: str, payload: dict[str, Any], *, timeout: int = 900) -> str:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "BORGcube-MomentInHistory/1.0"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def post_json_with_fallback(path: str, payload: dict[str, Any], *, timeout: int = 900) -> str:
    last_error: Exception | None = None
    endpoints = [OLLAMA_BASE_URL]
    if OLLAMA_FALLBACK_URL not in endpoints:
        endpoints.append(OLLAMA_FALLBACK_URL)
    for endpoint in endpoints:
        try:
            endpoint_payload = dict(payload)
            if endpoint == OLLAMA_FALLBACK_URL and endpoint_payload.get("model") == TEXT_MODEL:
                endpoint_payload["model"] = FALLBACK_TEXT_MODEL
            return post_json(f"{endpoint.rstrip('/')}{path}", endpoint_payload, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - try the configured fallback
            last_error = exc
            log(f"Ollama endpoint unavailable ({endpoint}): {exc}")
    raise RuntimeError(f"All Ollama endpoints failed for {path}: {last_error}")


def build_image_prompt(pre_moment: str, day: date) -> str:
    return f"""Use case: historical-scene.
Asset type: daily Home Assistant dashboard artwork.
Primary request: a wordless cinematic cartoon illustration of the exact moment ten seconds before a major historical event. Show only the tense anticipation and immediate setup, never the event itself or its aftermath.
Scene direction (visual meaning only; never render these words): {pre_moment}
Style/medium: clearly 2D cartoon illustration, clean ink outlines, cel shading, simplified stylized forms, dramatic high-contrast modern graphic-novel finish, epic composition, strong silhouette design, dynamic perspective, polished concept art. Avoid photorealism and photographic textures.
Lighting/mood: bold directional lighting, deep shadows, luminous highlights, suspenseful anticipation.
Composition/framing: square 1024x1024 composition, visually legible at dashboard-card size, one coherent scene, no collage.
On-image text: none during generation. The date is composited separately after generation, so do not render any words, letters, numerals, signs, labels, captions, speech bubbles, logos, watermarks, documents, pages, books, newspapers, maps, screens, teleprompters, or any written surface. If the scene direction mentions a script or paper, show only the people and their body language with the writing surface out of frame or blank. Do not depict the event itself; depict only the instant immediately before it.
Avoid: modern objects in historical scenes, anachronistic clothing, graphic gore, blurry faces, extra limbs, illegible pseudo-text, and duplicate subjects."""


def post_image(url: str, payload: dict[str, Any], *, timeout: int = 1800) -> bytes:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "BORGcube-MomentInHistory/1.0"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get_content_type()
        body = response.read()
    if content_type != "image/png" or not body:
        raise RuntimeError(f"Image backend returned {content_type} with {len(body)} bytes")
    return body


def get_image(url: str, *, timeout: int = 1800) -> bytes:
    """Fetch a generated PNG from an image service without exposing its URL."""
    request = Request(
        url,
        headers={
            "Accept": "image/png",
            "User-Agent": "BORGcube-MomentInHistory/1.1",
        },
        method="GET",
    )
    with urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get_content_type()
        body = response.read()
    if content_type != "image/png" or not body:
        raise RuntimeError(f"Image backend returned {content_type} with {len(body)} bytes")
    return body


def comfyui_workflow(prompt: str, *, seed: int) -> dict[str, Any]:
    """Build the small API-format workflow used by the ComfyUI provider."""
    negative_prompt = (
        "text, letters, words, numbers, captions, labels, logos, watermarks, "
        "speech bubbles, documents, signs, blurry, low quality, distorted anatomy"
    )
    return {
        "3": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": COMFYUI_MODEL},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": IMAGE_WIDTH, "height": IMAGE_HEIGHT, "batch_size": 1},
        },
        "5": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["3", 1]},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative_prompt, "clip": ["3", 1]},
        },
        "7": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": max(1, IMAGE_STEPS),
                "cfg": 1.0,
                "sampler_name": "euler_ancestral",
                "scheduler": "sgm_uniform",
                "denoise": 1.0,
                "model": ["3", 0],
                "positive": ["5", 0],
                "negative": ["6", 0],
                "latent_image": ["4", 0],
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["7", 0], "vae": ["3", 2]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "moment-in-history", "images": ["8", 0]},
        },
    }


def generate_comfyui_image(prompt: str, endpoint: str | None = None) -> bytes:
    """Queue a native ComfyUI workflow and return its saved PNG."""
    endpoint = (endpoint or IMAGE_BACKEND_URL).rstrip("/")
    queue_payload = {
        "prompt": comfyui_workflow(prompt, seed=secrets.randbelow(2**63)),
        "client_id": uuid.uuid4().hex,
    }
    response = json.loads(post_json(f"{endpoint}/prompt", queue_payload, timeout=60))
    prompt_id = str(response.get("prompt_id", "")).strip()
    if not prompt_id:
        error = response.get("error") or response.get("node_errors")
        raise RuntimeError(f"ComfyUI rejected the workflow: {error or 'no prompt id'}")

    deadline = time.monotonic() + 1800
    history_url = f"{endpoint}/history/{quote(prompt_id, safe='')}"
    while time.monotonic() < deadline:
        history = fetch_json(history_url)
        result = history.get(prompt_id)
        if isinstance(result, dict):
            status = result.get("status") or {}
            messages = status.get("messages") if isinstance(status, dict) else []
            for message in messages or []:
                if isinstance(message, list) and message and message[0] in {
                    "execution_error",
                    "execution_interrupted",
                }:
                    raise RuntimeError(f"ComfyUI execution failed: {message[0]}")

            outputs = result.get("outputs") or {}
            for node_output in outputs.values():
                if not isinstance(node_output, dict):
                    continue
                for image in node_output.get("images") or []:
                    if not isinstance(image, dict) or not image.get("filename"):
                        continue
                    query = urlencode(
                        {
                            "filename": str(image["filename"]),
                            "subfolder": str(image.get("subfolder", "")),
                            "type": str(image.get("type", "output")),
                        }
                    )
                    return get_image(f"{endpoint}/view?{query}", timeout=1800)

            if isinstance(status, dict) and status.get("status_str") == "error":
                raise RuntimeError("ComfyUI execution failed without an image")
        time.sleep(1)
    raise TimeoutError("ComfyUI image generation timed out")


def generate_openai_image(prompt: str) -> bytes:
    """Generate a PNG through the OpenAI Images API without logging the key."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OpenAI image API key is not configured")
    payload = {
        "model": OPENAI_IMAGE_MODEL,
        "prompt": prompt,
        "size": OPENAI_IMAGE_SIZE,
        "quality": OPENAI_IMAGE_QUALITY,
        "output_format": "png",
    }
    request = Request(
        OPENAI_IMAGES_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "BORGcube-MomentInHistory/1.1",
        },
        method="POST",
    )
    with urlopen(request, timeout=1800) as response:
        response_payload = json.loads(response.read().decode("utf-8"))
    data = response_payload.get("data") or []
    if not data or not isinstance(data[0], dict):
        raise RuntimeError("OpenAI image response contained no image data")
    first = data[0]
    encoded = first.get("b64_json")
    if encoded:
        try:
            image = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("OpenAI returned invalid base64 image data") from exc
        if image:
            return image
    image_url = first.get("url")
    if image_url:
        return fetch_bytes(str(image_url), timeout=1800)
    raise RuntimeError("OpenAI image response contained neither b64_json nor url")


def run_image_model(prompt: str, destination: Path) -> str:
    """Ask OpenAI for a PNG, with the configured LAN backend as fallback.

    This is intentionally separate from Ollama. Ollama remains the text/ranking
    service, while image generation can use OpenAI or the LAN backend.
    """
    if IMAGE_PROVIDER == "openai":
        try:
            destination.write_bytes(generate_openai_image(prompt))
            return f"OpenAI {OPENAI_IMAGE_MODEL} ({OPENAI_IMAGE_QUALITY}, {OPENAI_IMAGE_SIZE})"
        except Exception as exc:  # noqa: BLE001 - keep the LAN fallback available
            log(f"OpenAI image backend unavailable: {exc}; trying LAN fallback")

    if IMAGE_PROVIDER == "comfyui":
        endpoints = [IMAGE_BACKEND_URL]
        if IMAGE_BACKEND_FALLBACK_URL and IMAGE_BACKEND_FALLBACK_URL not in endpoints:
            endpoints.append(IMAGE_BACKEND_FALLBACK_URL)
        last_error: Exception | None = None
        for endpoint in endpoints:
            try:
                destination.write_bytes(generate_comfyui_image(prompt, endpoint))
                return f"ComfyUI {COMFYUI_MODEL} ({endpoint_host(endpoint)})"
            except Exception as exc:  # noqa: BLE001 - try the configured fallback
                last_error = exc
                log(f"ComfyUI backend unavailable ({endpoint}): {exc}")
        raise RuntimeError(f"All ComfyUI backends failed: {last_error}")

    payload = {
        "prompt": prompt,
        "width": IMAGE_WIDTH,
        "height": IMAGE_HEIGHT,
        "steps": IMAGE_STEPS,
    }
    endpoints = [IMAGE_BACKEND_URL]
    if IMAGE_BACKEND_FALLBACK_URL and IMAGE_BACKEND_FALLBACK_URL not in endpoints:
        endpoints.append(IMAGE_BACKEND_FALLBACK_URL)
    last_error: Exception | None = None
    for endpoint in endpoints:
        try:
            destination.write_bytes(post_image(f"{endpoint.rstrip('/')}/generate", payload))
            return f"LAN {IMAGE_MODEL} ({endpoint})"
        except Exception as exc:  # noqa: BLE001 - try the configured fallback
            last_error = exc
            log(f"Image backend unavailable ({endpoint}): {exc}")
    raise RuntimeError(f"All image backends failed: {last_error}")


def overlay_date(path: Path, label: str) -> None:
    """Guarantee that the only intentional text is the exact date."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        log("Pillow is not installed; relying on the image model's date rendering")
        return
    image = Image.open(path).convert("RGB")
    draw = ImageDraw.Draw(image, "RGBA")
    width, height = image.size
    font_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNS.ttf",
        "/Library/Fonts/Arial.ttf",
    ]
    font_path = next((candidate for candidate in font_candidates if Path(candidate).exists()), None)
    font = ImageFont.truetype(font_path, max(20, width // 28)) if font_path else ImageFont.load_default()
    band_height = max(54, height // 12)
    draw.rectangle((0, height - band_height, width, height), fill=(0, 0, 0, 178))
    bounds = draw.textbbox((0, 0), label, font=font)
    text_width = bounds[2] - bounds[0]
    text_height = bounds[3] - bounds[1]
    draw.text(((width - text_width) // 2, height - band_height + (band_height - text_height) // 2 - bounds[1]), label, fill=(255, 255, 255, 255), font=font)
    image.save(path, format="PNG", optimize=True)


def write_event_pages(metadata: dict[str, Any]) -> None:
    EVENT_JSON_PATH.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    source_link = html.escape(metadata["source_url"], quote=True)
    title = html.escape(metadata["event"])
    description = html.escape(metadata["description"])
    pre_moment = html.escape(metadata["pre_moment"])
    date_label = html.escape(metadata["date_label"])
    source = html.escape(metadata["source"])
    EVENT_HTML_PATH.write_text(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Cache-Control" content="no-store"><title>Moment in history</title>
<style>html,body{{margin:0;background:#111;color:#f6f1e8;font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{max-width:760px;margin:auto;padding:24px}}.date{{color:#d9a441;text-transform:uppercase;letter-spacing:.12em;font-size:.8rem}}h1{{font:700 clamp(1.4rem,4vw,2.4rem)/1.1 Georgia,serif;margin:.35rem 0 1rem}}p{{margin:0 0 1rem}}a{{color:#f0c46a}}</style></head>
<body><main><div class="date" id="date">{date_label}</div><h1 id="event">{title}</h1><p><strong>Ten seconds before:</strong> <span id="pre-moment">{pre_moment}</span></p><p id="description">{description}</p><p><a id="source" href="{source_link}" target="_blank" rel="noreferrer">Source: {source}</a></p></main>
<script>
(async function () {{
  try {{
    const response = await fetch('./event.json?refresh=' + Date.now(), {{cache: 'no-store'}});
    if (!response.ok) throw new Error('event metadata request failed');
    const data = await response.json();
    document.getElementById('date').textContent = data.date_label || '';
    document.getElementById('event').textContent = data.event || '';
    document.getElementById('pre-moment').textContent = data.pre_moment || '';
    document.getElementById('description').textContent = data.description || '';
    const sourceElement = document.getElementById('source');
    sourceElement.href = data.source_url || '#';
    sourceElement.textContent = data.source ? 'Source: ' + data.source : 'Source';
  }} catch (error) {{
    // The server-rendered values remain visible if the refresh is unavailable.
    console.warn('Moment in history refresh failed', error);
  }}
}})();
</script></body></html>
""",
        encoding="utf-8",
    )
def log(message: str) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now(TIME_ZONE).isoformat()} {message}"
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        print(f"{line} [file log unavailable: {exc}]", file=sys.stderr, flush=True)
    print(line, flush=True)


def endpoint_host(value: str) -> str:
    """Return only the host portion so startup logs never expose URL credentials."""
    parsed = urlsplit(value)
    return parsed.hostname or value.split("/", 1)[0]


def startup_report(mode: str) -> None:
    """Write an actionable, secret-free startup report for Supervisor and users."""
    ROOT.mkdir(parents=True, exist_ok=True)
    if EMAIL_NOTIFY_ENTITY:
        email_status = (
            f"enabled via Home Assistant SMTP ({EMAIL_NOTIFY_ENTITY}, "
            f"camera={EMAIL_CAMERA_ENTITY or 'not configured'}, "
            f"api_token={'present' if SUPERVISOR_TOKEN else 'missing'})"
        )
    else:
        email_status = "disabled (email_notify_entity is empty)"

    log(f"Moment in history App {APP_VERSION} starting (mode={mode})")
    log(
        f"Runtime ready: timezone={TIME_ZONE.key}, poll_interval={POLL_INTERVAL_SECONDS}s, "
        f"schedule_time={SCHEDULE_TIME}, run_on_startup={RUN_ON_STARTUP}, "
        f"generate_on_restart={GENERATE_ON_RESTART}"
    )
    log(
        f"Text service configured: primary={endpoint_host(OLLAMA_BASE_URL)} "
        f"model={TEXT_MODEL}; fallback={endpoint_host(OLLAMA_FALLBACK_URL)} "
        f"model={FALLBACK_TEXT_MODEL}"
    )
    log(
        f"Image service configured: provider={IMAGE_PROVIDER}, openai_model={OPENAI_IMAGE_MODEL}, "
        f"size={OPENAI_IMAGE_SIZE}, steps={IMAGE_STEPS}, "
        f"openai_key={'present' if OPENAI_API_KEY else 'missing'}, "
        f"lan_backend={endpoint_host(IMAGE_BACKEND_URL)}, comfyui_model={COMFYUI_MODEL}"
    )
    log(f"Home Assistant API: {'available' if SUPERVISOR_TOKEN else 'unavailable'}")
    log(f"Email delivery: {email_status}; only after successful image replacement")
    if ARCHIVE_IMAGES:
        log(f"Image archive: enabled at {ROOT / 'archive'}; budget approximately 2 MB per image")
    else:
        log("Image archive: disabled; enable archive_images to retain approximately 2 MB per image")
    log(
        f"Output ready: root={ROOT}, image={IMAGE_PATH.name}, metadata={EVENT_JSON_PATH.name}, "
        f"disclosure={EVENT_HTML_PATH.name}, log={LOG_PATH.name}"
    )
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", SCHEDULE_TIME):
        log(f"Startup warning: schedule_time={SCHEDULE_TIME!r} is not valid HH:MM")
    log("Startup complete: daemon is ready for its daily schedule")


def send_event_email(metadata: dict[str, Any], image_path: Path) -> None:
    """Send the current image and readable event text through Home Assistant SMTP."""
    notify_entities = configured_notify_entities()
    if not notify_entities:
        return
    if not SUPERVISOR_TOKEN:
        raise RuntimeError("email_notify_entity is set but SUPERVISOR_TOKEN is unavailable")
    if not EMAIL_CAMERA_ENTITY:
        raise RuntimeError("email_camera_entity must be set to attach the generated image")
    if not image_path.is_file():
        raise RuntimeError(f"generated image is missing: {image_path}")

    payload = {
        "entity_id": notify_entities[0] if len(notify_entities) == 1 else notify_entities,
        "title": f"Moment in history — {metadata['date_label']}",
        "message": (
            f"Moment in history — {metadata['date_label']}\n\n"
            f"Event:\n{metadata['event']}\n\n"
            f"Ten seconds before:\n{metadata['pre_moment']}\n\n"
            f"Source: {metadata['source']}\n{metadata['source_url']}\n"
        ),
        "attachments": [
            {
                "media_source": {
                    "media_content_id": f"media-source://camera/{EMAIL_CAMERA_ENTITY}",
                    "media_content_type": "image/png",
                },
                "filename": "moment-in-history.png",
            }
        ],
    }
    request = Request(
        f"{HOME_ASSISTANT_API_URL}/services/smtp/send_message",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "BORGcube-MomentInHistory/1.9.6",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"Home Assistant SMTP action failed ({exc.code}): {detail}") from exc
    log(f"Email sent via Home Assistant SMTP ({EMAIL_NOTIFY_ENTITY})")


def save_model_options(changes: dict[str, str]) -> None:
    """Persist Web UI selections through Supervisor and an App-owned backup."""
    if not SUPERVISOR_TOKEN:
        raise RuntimeError("SUPERVISOR_TOKEN is unavailable")
    options = dict(config_ui.DEFAULT_WEB_OPTIONS)
    options.update(_load_addon_options())
    options.update(SUPERVISOR_OPTIONS)
    options.update(PERSISTED_WEB_OPTIONS)
    for field in config_ui.SELECTABLE_FIELDS:
        if field in changes:
            options[field] = changes[field]
    request = Request(
        "http://supervisor/addons/self/options",
        data=json.dumps({"options": options}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "BORGcube-MomentInHistory/1.9.6",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            response.read()
    except HTTPError as exc:
        raise RuntimeError(f"Supervisor options update failed ({exc.code})") from exc
    try:
        WEB_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        WEB_SETTINGS_PATH.write_text(
            json.dumps(
                {
                    field: options[field]
                    for field in config_ui.WEB_SETTINGS_FIELDS
                    if field in options
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        PERSISTED_WEB_OPTIONS.update(
            {
                field: options[field]
                for field in config_ui.WEB_SETTINGS_FIELDS
                if field in options
            }
        )
    except OSError as exc:
        print(f"Web UI settings backup unavailable: {exc}", file=sys.stderr, flush=True)


def get_home_assistant_notify_entities() -> list[str]:
    """Return current Home Assistant notify entities for the Ingress UI."""
    return config_ui.home_assistant_notify_entities(HOME_ASSISTANT_API_URL, SUPERVISOR_TOKEN)


def _read_managed_recipients() -> list[dict[str, Any]]:
    """Read the app-owned email mapping without exposing addresses in logs."""
    try:
        payload = json.loads(MANAGED_RECIPIENTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [
        {
            "email": str(item.get("email", "")).strip(),
            "entity_id": str(item.get("entity_id", "")).strip() or None,
            "entry_id": str(item.get("entry_id", "")).strip(),
            "subentry_id": str(item.get("subentry_id", "")).strip(),
        }
        for item in payload
        if isinstance(item, dict) and str(item.get("email", "")).strip()
    ]


def _write_managed_recipients(recipients: list[dict[str, Any]]) -> None:
    MANAGED_RECIPIENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = MANAGED_RECIPIENTS_PATH.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(recipients, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary_path, MANAGED_RECIPIENTS_PATH)


def get_managed_email_recipients() -> dict[str, Any]:
    """Return only SMTP recipients that this App can safely manage."""
    details = config_ui.smtp_recipient_details(HOME_ASSISTANT_API_URL, SUPERVISOR_TOKEN)
    current = {
        str(item.get("email", "")).casefold(): item
        for item in details.get("recipients", [])
        if isinstance(item, dict)
    }
    recipients = []
    for item in _read_managed_recipients():
        live = current.get(item["email"].casefold())
        if live:
            item = {**item, "entity_id": live.get("entity_id") or item.get("entity_id")}
        recipients.append(
            {
                "email": item["email"],
                "entity_id": item.get("entity_id"),
            }
        )
    return {
        "available": bool(details.get("available")),
        "smtp_configured": bool(details.get("available")),
        "message": details.get("message", ""),
        "recipients": recipients,
    }


def add_managed_email_recipient(email: str) -> dict[str, Any]:
    """Create an SMTP recipient and remember the subentry needed to remove it later."""
    normalized = email.strip()
    existing = _read_managed_recipients()
    for item in existing:
        if item["email"].casefold() == normalized.casefold():
            return {"email": item["email"], "entity_id": item.get("entity_id")}
    recipient = config_ui.add_smtp_recipient(HOME_ASSISTANT_API_URL, SUPERVISOR_TOKEN, normalized)
    record = {
        "email": str(recipient["email"]),
        "entity_id": str(recipient.get("entity_id", "")) or None,
        "entry_id": str(recipient.get("entry_id", "")),
        "subentry_id": str(recipient.get("subentry_id", "")),
    }
    _write_managed_recipients(existing + [record])
    log(f"Managed SMTP recipient added ({record['entity_id'] or 'entity pending'})")
    return {"email": record["email"], "entity_id": record["entity_id"]}


def configure_smtp_integration(settings: dict[str, Any]) -> dict[str, Any]:
    """Create Home Assistant SMTP without retaining the submitted credentials."""
    recipient = config_ui.configure_smtp(HOME_ASSISTANT_API_URL, SUPERVISOR_TOKEN, settings)
    record = {
        "email": str(recipient["email"]),
        "entity_id": str(recipient.get("entity_id", "")) or None,
        "entry_id": str(recipient.get("entry_id", "")),
        "subentry_id": str(recipient.get("subentry_id", "")),
    }
    existing = _read_managed_recipients()
    _write_managed_recipients(existing + [record])
    log(f"Managed SMTP integration configured ({record['entity_id'] or 'entity pending'})")
    return {"email": record["email"], "entity_id": record["entity_id"]}


def remove_managed_email_recipient(email: str) -> None:
    """Remove an App-managed SMTP recipient through Home Assistant's API."""
    normalized = email.strip()
    existing = _read_managed_recipients()
    record = next((item for item in existing if item["email"].casefold() == normalized.casefold()), None)
    if record is None:
        raise ValueError("That email address is not managed by this App")
    config_ui.delete_smtp_recipient(HOME_ASSISTANT_API_URL, SUPERVISOR_TOKEN, record)
    _write_managed_recipients([item for item in existing if item is not record])
    log(f"Managed SMTP recipient removed ({record.get('entity_id') or 'entity pending'})")


def start_config_ui() -> Any | None:
    """Start the Ingress-only model selection page."""
    try:
        server = config_ui.start_server(
            port=CONFIG_UI_PORT,
            get_options=_runtime_options,
            save_options=save_model_options,
            log=log,
            get_notify_entities=get_home_assistant_notify_entities,
            get_managed_recipients=get_managed_email_recipients,
            add_email_recipient=add_managed_email_recipient,
            remove_email_recipient=remove_managed_email_recipient,
            configure_smtp=configure_smtp_integration,
        )
        log(f"Configuration UI: Home Assistant Ingress listening on port {CONFIG_UI_PORT}")
        return server
    except OSError as exc:
        log(f"Configuration UI unavailable: {exc}")
        return None


def archive_image(target_day: date) -> None:
    """Keep an optional dated copy without affecting the current dashboard image."""
    if not ARCHIVE_IMAGES:
        return
    archive_path = ROOT / "archive" / f"{target_day.year:04d}" / f"{target_day.month:02d}" / f"{target_day.isoformat()}.png"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = archive_path.with_suffix(".tmp.png")
    shutil.copy2(IMAGE_PATH, temporary_path)
    os.replace(temporary_path, archive_path)
    size_mib = archive_path.stat().st_size / (1024 * 1024)
    log(f"Archived {target_day.isoformat()} image to {archive_path} ({size_mib:.2f} MiB)")


def generate(target_day: date) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    try:
        events = fetch_britannica_events(target_day)
        source_status = "Britannica"
    except (HTTPError, URLError, RuntimeError, TimeoutError) as exc:
        log(f"Britannica unavailable ({exc}); using Wikimedia fallback")
        events = fetch_wikimedia_events(target_day)
        source_status = "Wikimedia fallback after Britannica attempt"

    selected, selection_reason = choose_event(events)
    pre_moment_prompt = f"Describe in exactly one vivid sentence of no more than 35 words the scene exactly ten seconds before this event, without depicting the event itself. Treat the supplied event as authoritative; do not add corrections, caveats, or commentary. Mention only people, setting, lighting, atmosphere, and non-text-bearing physical objects. Do not mention paper, pages, scripts, screens, signs, labels, logos, or writing: {selected.text}"
    try:
        pre_payload = {
            "model": TEXT_MODEL,
            "stream": False,
            "messages": [{"role": "user", "content": pre_moment_prompt}],
            "options": {"temperature": 0.35},
        }
        pre_response = json.loads(post_json_with_fallback("/api/chat", pre_payload))
        pre_moment = clean_text(pre_response.get("message", {}).get("content", ""))
    except Exception as exc:  # noqa: BLE001
        log(f"Ollama pre-moment description failed: {exc}")
        pre_moment = "The setting holds in tense anticipation, with every detail poised for what is about to happen."

    date_label = f"{target_day.day} {target_day.strftime('%B %Y')}"
    prompt = build_image_prompt(pre_moment, target_day)
    temp_image = ROOT / f"moment-in-history-{int(time.time())}.png"
    image_backend_used = run_image_model(prompt, temp_image)
    overlay_date(temp_image, date_label)
    os.replace(temp_image, IMAGE_PATH)

    metadata = {
        "date": target_day.isoformat(),
        "date_label": date_label,
        "event": selected.text,
        "title": selected.title,
        "year": selected.year,
        "description": selected.text,
        "pre_moment": pre_moment,
        "selection_reason": selection_reason,
        "source": source_status,
        "source_url": selected.source_url,
        "britannica_url": BRITANNICA_URL,
        "image_url": IMAGE_URL,
        "generated_at": datetime.now(TIME_ZONE).isoformat(),
        "image_model": IMAGE_MODEL,
        "image_provider": IMAGE_PROVIDER,
        "image_backend_used": image_backend_used,
        "openai_image_model": OPENAI_IMAGE_MODEL,
        "text_model": TEXT_MODEL,
        "candidate_count": len(events),
        "prompt": prompt,
    }
    write_event_pages(metadata)
    archive_image(target_day)
    try:
        send_event_email(metadata, IMAGE_PATH)
    except Exception as exc:  # noqa: BLE001 - email must not stop daily generation
        log(f"Email delivery failed: {exc}")
    log(f"Generated {target_day.isoformat()} — {selected.text} ({len(events)} candidates; {source_status})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Generate for YYYY-MM-DD instead of today")
    parser.add_argument("--daemon", action="store_true", help="Run continuously and generate once per day")
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    migrate_web_options()
    startup_report("daemon" if args.daemon else "one-shot")
    if args.daemon:
        return daemon()
    target_day = date.fromisoformat(args.date) if args.date else datetime.now(TIME_ZONE).date()
    with LOCK_PATH.open("w", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("Generation already running; exiting")
            return 0
        generate(target_day)
    return 0


def should_generate_today(
    generated_date: date | None,
    today: date,
    schedule_reached: bool,
    startup_pending: bool,
) -> bool:
    """Decide whether this loop may generate without changing stored config."""
    if generated_date == today:
        return False
    if startup_pending:
        if GENERATE_ON_RESTART:
            return True
        return RUN_ON_STARTUP and schedule_reached
    return schedule_reached


def request_shutdown(_signum: int, _frame: Any) -> None:
    """Request a clean daemon exit without doing blocking work in a signal handler."""
    SHUTDOWN_EVENT.set()


def daemon() -> int:
    """Run inside the HA add-on and keep the daily asset current."""
    SHUTDOWN_EVENT.clear()
    previous_handlers = {
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
        signal.SIGINT: signal.getsignal(signal.SIGINT),
    }
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    server = start_config_ui()
    try:
        marker_path = ROOT / ".last-generated-date"
        last_attempt_date: date | None = None
        last_attempt_at = 0.0
        startup_pending = True
        initial_status_logged = False
        while not SHUTDOWN_EVENT.is_set():
            now = datetime.now(TIME_ZONE)
            today = now.date()
            try:
                generated_date = date.fromisoformat(marker_path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                generated_date = None
            schedule_reached = now.strftime("%H:%M") >= SCHEDULE_TIME
            should_run = should_generate_today(generated_date, today, schedule_reached, startup_pending)
            if not initial_status_logged:
                if generated_date == today:
                    log(f"Startup state: today's image is already present ({today.isoformat()}); no generation needed")
                elif startup_pending and not GENERATE_ON_RESTART:
                    log(
                        f"Startup state: today's image is not present; restart generation is disabled; "
                        f"waiting for schedule_time={SCHEDULE_TIME}"
                    )
                elif not schedule_reached:
                    log(f"Startup state: waiting for schedule_time={SCHEDULE_TIME}; current_time={now.strftime('%H:%M')}")
                initial_status_logged = True
            retry_allowed = last_attempt_date != today or time.time() - last_attempt_at >= 900
            if should_run and retry_allowed:
                last_attempt_date = today
                last_attempt_at = time.time()
                try:
                    with LOCK_PATH.open("w", encoding="utf-8") as lock_handle:
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                        generate(today)
                        marker_path.write_text(today.isoformat() + "\n", encoding="utf-8")
                except Exception as exc:  # noqa: BLE001 - daemon logs and retries
                    log(f"Daily generation failed: {exc}")
            startup_pending = False
            if SHUTDOWN_EVENT.wait(POLL_INTERVAL_SECONDS):
                break
        log("Shutdown requested; stopping cleanly")
        return 0
    except KeyboardInterrupt:
        log("Shutdown requested; stopping cleanly")
        return 0
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        SHUTDOWN_EVENT.clear()
        log("Shutdown complete")


if __name__ == "__main__":
    raise SystemExit(main())
