#!/usr/bin/env python3
"""Generate the daily "Moment in history" image and publish its metadata.

The app uses the public, machine-readable English Wikipedia On This Day REST
feed for event discovery. Event selection and the image prompt are kept
separate so the generated image is always the moment immediately before the
selected event, rather than the event itself.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import html
import json
import os
import random
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
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

import config_ui


TIME_ZONE = ZoneInfo("Europe/Amsterdam")
APP_VERSION = "1.9.40"
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
            "User-Agent": "MomentInHistory/1.9.31",
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
ARCHIVE_DIR = ROOT / "archive"
ARCHIVE_INDEX_PATH = ARCHIVE_DIR / "index.json"
ARCHIVE_HTML_PATH = ROOT / "archive.html"
LOCK_PATH = ROOT / ".generation.lock"
LOG_PATH = ROOT / "generation.log"
SELECTION_HISTORY_PATH = Path(
    os.environ.get("MOMENT_SELECTION_HISTORY_FILE", "/data/moment-in-history-selection-history.json")
)
ROTATION_TOP_CANDIDATES = 5
ROTATION_LOOKBACK_YEARS = 5

WIKIPEDIA_ON_THIS_DAY_URL = "https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/{month:02d}/{day:02d}"

OLLAMA_BASE_URL = _setting("OLLAMA_BASE_URL", "primary_ollama_url", "http://ollama-primary.example.local:11434")
OLLAMA_FALLBACK_URL = _setting("OLLAMA_FALLBACK_URL", "fallback_ollama_url", "http://ollama-secondary.example.local:11434")
TEXT_MODEL = _setting("MOMENT_TEXT_MODEL", "text_model", "qwen3.8:27b-mlx")
FALLBACK_TEXT_MODEL = _setting("MOMENT_FALLBACK_TEXT_MODEL", "fallback_text_model", "gemma4:e4b")
IMAGE_MODEL = _setting("MOMENT_IMAGE_MODEL", "image_model", "Tongyi-MAI/Z-Image-Turbo")
COMFYUI_MODEL = _setting(
    "MOMENT_COMFYUI_MODEL", "comfyui_model", "sd_xl_turbo_1.0.safetensors"
)
IMAGE_BACKEND_URL = _setting("MOMENT_IMAGE_BACKEND_URL", "image_backend_url", "http://image-backend.example.local:8188")
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
_LEGACY_GENERATE_ON_RESTART = _setting("MOMENT_GENERATE_ON_RESTART", "generate_on_restart", "false")
REGENERATE_ON_STARTUP = _setting(
    "MOMENT_REGENERATE_ON_STARTUP",
    "regenerate_on_startup",
    _LEGACY_GENERATE_ON_RESTART,
).lower() in {"1", "true", "yes", "on"}
REGENERATE_TODAY = _setting("MOMENT_REGENERATE_TODAY", "regenerate_today", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
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


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def fetch_bytes(url: str, *, timeout: int = 30) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "MomentInHistory/1.9.40 (+https://github.com/borgcube/HA-Moment-in-History-app)",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_json(url: str) -> dict[str, Any]:
    return json.loads(fetch_bytes(url).decode("utf-8"))


def format_event_year(year: int | None) -> str:
    """Return the historical year used as the image's only text label."""
    if year is None:
        return "Unknown year"
    if year < 0:
        return f"{abs(year)} BCE"
    return str(year)


def fetch_wikipedia_events(day: date) -> list[Event]:
    """Fetch significant events from English Wikipedia's On This Day feed."""
    feed_url = WIKIPEDIA_ON_THIS_DAY_URL.format(month=day.month, day=day.day)
    payload = fetch_json(feed_url)
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
                source="English Wikipedia On This Day",
                source_url=desktop.get("page") or feed_url,
            )
        )
    if not events:
        raise RuntimeError("English Wikipedia On This Day returned no events")
    return events


def event_key(event: Event) -> str:
    """Return a stable identity for the same historical event across feed updates."""
    return "\x1f".join(
        (
            str(event.year) if event.year is not None else "",
            event.source_url.strip().rstrip("/").casefold(),
            event.title.strip().casefold(),
            clean_text(event.text).casefold(),
        )
    )


def _event_from_metadata(metadata: dict[str, Any]) -> Event | None:
    """Build an Event from current or archived metadata when available."""
    text = clean_text(str(metadata.get("event") or metadata.get("description") or ""))
    if not text:
        return None
    raw_year = metadata.get("year")
    try:
        year = int(raw_year) if raw_year is not None else None
    except (TypeError, ValueError):
        year = None
    return Event(
        year=year,
        title=clean_text(str(metadata.get("title") or text)),
        text=text,
        source=clean_text(str(metadata.get("source") or "English Wikipedia On This Day")),
        source_url=clean_text(str(metadata.get("source_url") or "")),
    )


def _history_record(target_day: date, event: Event) -> dict[str, Any]:
    """Return the persisted form of one selected event."""
    return {
        "date": target_day.isoformat(),
        "event_key": event_key(event),
        "year": event.year,
        "title": event.title,
        "text": event.text,
        "source": event.source,
        "source_url": event.source_url,
    }


def load_selection_history() -> list[dict[str, Any]]:
    """Load annual event selections, tolerating a missing or older history file."""
    try:
        payload = json.loads(SELECTION_HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        payload = {}
    raw_records = payload.get("selections", []) if isinstance(payload, dict) else payload
    if not isinstance(raw_records, list):
        return []

    records: list[dict[str, Any]] = []
    for raw_record in raw_records:
        if not isinstance(raw_record, dict):
            continue
        date_text = str(raw_record.get("date", ""))
        try:
            date.fromisoformat(date_text)
        except ValueError:
            continue
        record = dict(raw_record)
        record["date"] = date_text
        if not clean_text(str(record.get("event_key", ""))):
            try:
                record["event_key"] = event_key(
                    Event(
                        year=int(record["year"]) if record.get("year") is not None else None,
                        title=clean_text(str(record.get("title", ""))),
                        text=clean_text(str(record.get("text", ""))),
                        source=clean_text(str(record.get("source", ""))),
                        source_url=clean_text(str(record.get("source_url", ""))),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        if clean_text(str(record.get("event_key", ""))):
            records.append(record)

    # Seed new installations from the current metadata generated before the
    # rotation history file existed. This makes the first annual rotation
    # after an upgrade aware of the previous year's selected event.
    try:
        current_metadata = json.loads(EVENT_JSON_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        current_metadata = None
    if isinstance(current_metadata, dict):
        current_date_text = str(current_metadata.get("date", ""))
        try:
            current_day = date.fromisoformat(current_date_text)
        except ValueError:
            current_day = None
        current_event = _event_from_metadata(current_metadata)
        if current_day and current_event and not any(
            record.get("date") == current_date_text for record in records
        ):
            records.append(_history_record(current_day, current_event))
    return records


def record_selection_history(target_day: date, event: Event) -> None:
    """Persist one selection for a generation date so future years can rotate."""
    target_date = target_day.isoformat()
    records = [record for record in load_selection_history() if record.get("date") != target_date]
    records.append(_history_record(target_day, event))
    records.sort(key=lambda record: str(record.get("date", "")))
    SELECTION_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = SELECTION_HISTORY_PATH.with_suffix(".tmp.json")
    temporary_path.write_text(
        json.dumps({"version": 1, "selections": records}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, SELECTION_HISTORY_PATH)


def load_existing_selection(target_day: date) -> tuple[Event, dict[str, Any]] | None:
    """Return the current event when a regeneration must preserve today's subject."""
    try:
        metadata = json.loads(EVENT_JSON_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(metadata, dict) or metadata.get("date") != target_day.isoformat():
        return None
    selected = _event_from_metadata(metadata)
    if not selected:
        return None
    return selected, metadata


def _rank_events(events: list[Event]) -> tuple[list[Event], str]:
    """Rank all candidates with local Ollama, with a deterministic fallback."""
    numbered = "\n".join(f"{i}: {event.year or '?'} — {event.text}" for i, event in enumerate(events))
    prompt = f"""You are ranking events for a daily historical illustration.
Rank the complete list from most impactful to least impactful. Prefer events with
broad, lasting consequences or unusually vivid historical stakes. Do not prioritize
a birth, death, holiday, or trivial detail merely because it is famous. Return JSON
only with this exact shape, where ranking contains valid zero-based indexes:
{{"ranking": [0, 1, 2], "why": "one concise sentence explaining the top choice"}}

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
        raw_ranking = decision.get("ranking")
        if not isinstance(raw_ranking, list):
            raw_ranking = [decision.get("index")] if decision.get("index") is not None else []
        ranking_indexes: list[int] = []
        for raw_index in raw_ranking:
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(events) and index not in ranking_indexes:
                ranking_indexes.append(index)
        if ranking_indexes:
            ranking_indexes.extend(index for index in range(len(events)) if index not in ranking_indexes)
            reason = clean_text(str(decision.get("why", "Ranked by local Ollama.")))
            return [events[index] for index in ranking_indexes], reason or "Ranked by local Ollama."
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
    scored = [
        (
            sum(weight for word, weight in weights.items() if word in event.text.lower()),
            len(event.text),
            index,
            event,
        )
        for index, event in enumerate(events)
    ]
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [item[3] for item in scored], "Ranked by the local deterministic fallback."


def _recent_penalty(event: Event, target_day: date, history: list[dict[str, Any]]) -> int:
    """Return a larger penalty for selections made in more recent years."""
    key = event_key(event)
    penalty = 0
    for record in history:
        if record.get("event_key") != key:
            continue
        try:
            selected_day = date.fromisoformat(str(record.get("date", "")))
        except ValueError:
            continue
        if selected_day.month != target_day.month or selected_day.day != target_day.day:
            continue
        age = target_day.year - selected_day.year
        if 1 <= age <= ROTATION_LOOKBACK_YEARS:
            penalty += ROTATION_LOOKBACK_YEARS - age + 1
    return penalty


def choose_event(events: list[Event], target_day: date | None = None) -> tuple[Event, str]:
    """Rank all candidates, then rotate deterministically through the top five."""
    if not events:
        raise ValueError("Cannot choose an event from an empty list")

    history = load_selection_history() if target_day is not None else []
    if target_day is not None:
        current_date = target_day.isoformat()
        for record in history:
            if record.get("date") != current_date:
                continue
            selected_key = record.get("event_key")
            for event in events:
                if event_key(event) == selected_key:
                    return event, f"Preserved the annual selection already recorded for {target_day.year}."

    ranked, ranking_reason = _rank_events(events)
    if target_day is None:
        return ranked[0], ranking_reason

    candidates = ranked[: min(ROTATION_TOP_CANDIDATES, len(ranked))]
    if len(candidates) == 1:
        return candidates[0], ranking_reason

    weights: list[float] = []
    for position, event in enumerate(candidates):
        impact_weight = len(candidates) - position
        recency_penalty = _recent_penalty(event, target_day, history)
        weights.append(max(0.25, float(impact_weight - recency_penalty)))

    seed_bytes = hashlib.sha256(
        f"moment-in-history:{target_day.isoformat()}".encode("utf-8")
    ).digest()
    selector = random.Random(int.from_bytes(seed_bytes[:8], "big")).random() * sum(weights)
    selected_position = 0
    for position, weight in enumerate(weights):
        selector -= weight
        if selector < 0:
            selected_position = position
            break
    selected = candidates[selected_position]
    return (
        selected,
        f"{ranking_reason} Annual rotation selected impact rank {selected_position + 1} "
        f"of {len(candidates)} for {target_day.year}; recent selections are penalized.",
    )


def post_json(url: str, payload: dict[str, Any], *, timeout: int = 900) -> str:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "MomentInHistory/1.0"},
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


def build_image_prompt(pre_moment: str, day: date, event_text: str = "") -> str:
    event_context = event_text.strip() or "the specific historical event described by the scene direction"
    return f"""Create one single full-bleed, highly detailed cinematic 2D illustration of a specific historical scene. Show the final ten seconds before the historical event below; the event is context only and must not be shown happening:
{event_context}

Scene direction (authoritative visual brief): {pre_moment}
Make the countdown unmistakable rather than generic: show one clear imminent physical action or trigger that is about to happen, such as a hand reaching the decisive object, a door or mechanism just beginning to move, a person taking the final step, or a crowd reacting to an approaching figure. Every visible person must have a purposeful posture, gesture, or gaze connected to that focal action. Do not depict a posed group, a passive gathering, or people merely standing symmetrically around an empty space.
Build a layered foreground, middle ground, and background with a strong focal point, varied scale, expressive faces and hands, authentic period clothing, historically plausible materials, specific architecture or landscape, tactile surfaces, atmospheric depth, and small environmental details that make the place and instant feel real. Use cinematic camera framing and directional light to guide the eye toward what is about to happen. Show anticipation, interruption, restraint, and cause-and-effect; do not show the completed event or its aftermath.
Use clean ink outlines, cel shading, simplified stylized forms, dramatic high-contrast modern graphic-novel finish, strong silhouette design, dynamic perspective, and polished concept-art quality. Avoid photorealism and photographic textures. Compose one coherent scene with no collage, poster layout, title card, decorative border, or empty text area.
The image must contain absolutely no visible writing or typography: no words, letters, numbers, numerals, captions, labels, logos, watermarks, signs, documents, pages, books, newspapers, maps, screens, speech bubbles, or blank writing surfaces. Do not copy the event description or add any title. If the scene direction mentions a script or paper, show only the people and their body language with the writing surface out of frame. Avoid modern objects in historical scenes, anachronistic clothing, graphic gore, blurry faces, extra limbs, illegible pseudo-text, and duplicate subjects."""


def post_image(url: str, payload: dict[str, Any], *, timeout: int = 1800) -> bytes:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "MomentInHistory/1.0"},
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
            "User-Agent": "MomentInHistory/1.1",
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
        "any text or typography, words, letters, numbers, numerals, writing, captions, "
        "labels, logos, watermarks, signs, speech bubbles, documents, pages, books, "
        "newspapers, maps, screens, posters, menus, banners, readable symbols, "
        "pseudo-text, title card, advertisement, user interface, decorative border, "
        "generic crowd, posed group portrait, passive waiting, symmetrical tableau, "
        "people merely standing, empty center, blurry, low quality, distorted anatomy"
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
            "User-Agent": "MomentInHistory/1.1",
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
    """Guarantee that the only intentional text is the supplied event-year label."""
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
    persisted_metadata = dict(metadata)
    # ``description`` was the old name for the event summary.  Persisting it
    # alongside ``event`` lets older consumers render the same sentence twice,
    # so drop it when rewriting current metadata, including legacy data during
    # startup refresh.
    persisted_metadata.pop("description", None)
    EVENT_JSON_PATH.write_text(
        json.dumps(persisted_metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    source_link = html.escape(metadata["source_url"], quote=True)
    title = html.escape(metadata["event"])
    pre_moment = html.escape(metadata["pre_moment"])
    date_label = html.escape(metadata["date_label"])
    source = html.escape(metadata["source"])
    EVENT_HTML_PATH.write_text(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Cache-Control" content="no-store"><title>Moment in history</title>
<style>html,body{{margin:0;background:#111;color:#f6f1e8;font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{max-width:760px;margin:auto;padding:24px}}.date{{color:#d9a441;text-transform:uppercase;letter-spacing:.12em;font-size:.8rem}}h1{{font:700 clamp(1.4rem,4vw,2.4rem)/1.1 Georgia,serif;margin:.35rem 0 1rem}}p{{margin:0 0 1rem}}a{{color:#f0c46a}}</style></head>
<body><main><div class="date" id="date">{date_label}</div><h1 id="event">{title}</h1><p><strong>Ten seconds before:</strong> <span id="pre-moment">{pre_moment}</span></p><p><a id="source" href="{source_link}" target="_blank" rel="noreferrer">Source: {source}</a></p></main>
<script>
(async function () {{
  try {{
    const response = await fetch('./event.json?refresh=' + Date.now(), {{cache: 'no-store'}});
    if (!response.ok) throw new Error('event metadata request failed');
    const data = await response.json();
    document.getElementById('date').textContent = data.date_label || '';
    document.getElementById('event').textContent = data.event || '';
    document.getElementById('pre-moment').textContent = data.pre_moment || '';
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


def refresh_event_page() -> None:
    """Rewrite a legacy current event page without generating a new image."""
    try:
        metadata = json.loads(EVENT_JSON_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return
    if not isinstance(metadata, dict):
        return
    required = ("date_label", "event", "pre_moment", "source", "source_url")
    if not all(field in metadata for field in required):
        return
    try:
        write_event_pages(metadata)
    except OSError as exc:
        log(f"Current event page refresh failed: {exc}")


def _archive_entries() -> list[dict[str, str]]:
    """Return valid archived image paths in newest-first order."""
    entries: list[dict[str, str]] = []
    if not ARCHIVE_DIR.exists():
        return entries
    for path in ARCHIVE_DIR.glob("*/*/*.png"):
        relative = path.relative_to(ARCHIVE_DIR).as_posix()
        match = re.fullmatch(r"(\d{4})/(\d{2})/(\d{4}-\d{2}-\d{2})\.png", relative)
        if not match:
            continue
        try:
            archive_day = date.fromisoformat(match.group(3))
        except ValueError:
            continue
        if archive_day.year != int(match.group(1)) or archive_day.month != int(match.group(2)):
            continue
        entries.append(
            {
                "date": archive_day.isoformat(),
                "date_label": f"{archive_day.day} {archive_day.strftime('%B %Y')}",
                "path": f"archive/{relative}",
            }
        )
        metadata_path = path.with_suffix(".json")
        if metadata_path.is_file():
            entries[-1]["metadata_path"] = f"archive/{metadata_path.relative_to(ARCHIVE_DIR).as_posix()}"
    return sorted(entries, key=lambda item: item["date"], reverse=True)


def archive_event_metadata(target_day: date) -> None:
    """Keep the educational event text beside an archived image."""
    try:
        metadata = json.loads(EVENT_JSON_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return
    if not isinstance(metadata, dict):
        return
    fields = (
        "date",
        "date_label",
        "event",
        "title",
        "year",
        "pre_moment",
        "selection_reason",
        "source",
        "source_url",
        "event_key",
        "generated_at",
    )
    archived = {field: metadata[field] for field in fields if field in metadata}
    archived["date"] = target_day.isoformat()
    metadata_path = ARCHIVE_DIR / f"{target_day.year:04d}" / f"{target_day.month:02d}" / f"{target_day.isoformat()}.json"
    temporary_path = metadata_path.with_suffix(".tmp.json")
    temporary_path.write_text(
        json.dumps(archived, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, metadata_path)


def write_archive_viewer() -> None:
    """Publish a static gallery page and index for the optional image archive."""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    entries = _archive_entries()
    ARCHIVE_INDEX_PATH.write_text(
        json.dumps({"archive_enabled": ARCHIVE_IMAGES, "entries": entries}, indent=2) + "\n",
        encoding="utf-8",
    )
    ARCHIVE_HTML_PATH.write_text(
        """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Cache-Control" content="no-store">
  <title>Moment in history — archive</title>
  <style>
    :root { color-scheme: dark; font-family: system-ui, sans-serif; }
    body { margin: 0; background: #111827; color: #f3f4f6; }
    main { max-width: 1200px; margin: auto; padding: 24px; }
    header { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
    h1 { margin: 0; font-size: 1.55rem; }
    a { color: #f0c46a; }
    .hint { color: #9ca3af; }
    .gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; margin-top: 20px; }
    .card { display: block; padding: 0 0 10px; overflow: hidden; border: 1px solid #374151; border-radius: 10px; background: #1f2937; color: #f9fafb; text-decoration: none; }
    .card:hover { border-color: #d9a441; }
    .card img { display: block; width: 100%; aspect-ratio: 1 / 1; object-fit: cover; background: #030712; }
    .card time { display: block; padding: 10px 12px 0; font-weight: 700; }
    .empty { padding: 24px; border: 1px dashed #4b5563; border-radius: 10px; color: #d1d5db; }
  </style>
</head>
<body>
<main>
  <header>
    <h1>Moment in history — archive</h1>
    <a href="./moment-in-history.png">View current image</a>
  </header>
  <p class="hint" id="status">Loading archived images…</p>
  <section class="gallery" id="gallery" aria-live="polite"></section>
</main>
<script>
(async function () {
  const status = document.getElementById('status');
  const gallery = document.getElementById('gallery');
  try {
    const response = await fetch('./archive/index.json?refresh=' + Date.now(), { cache: 'no-store' });
    if (!response.ok) throw new Error('archive index request failed');
    const data = await response.json();
    const entries = Array.isArray(data.entries) ? data.entries : [];
    status.textContent = data.archive_enabled
      ? (entries.length ? entries.length + ' archived image' + (entries.length === 1 ? '' : 's') : 'No archived images yet.')
      : 'Image archiving is disabled. Enable Archive images in App Configuration to keep dated copies.';
    if (!entries.length) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = data.archive_enabled ? 'The first archived image will appear after the next successful generation.' : status.textContent;
      gallery.appendChild(empty);
      return;
    }
    entries.forEach(function (entry) {
      const card = document.createElement('article');
      card.className = 'card';
      const imageLink = document.createElement('a');
      imageLink.href = entry.path;
      imageLink.target = '_blank';
      imageLink.rel = 'noreferrer';
      const image = document.createElement('img');
      image.loading = 'lazy';
      image.src = entry.path;
      image.alt = 'Moment in history — ' + entry.date_label;
      imageLink.appendChild(image);
      const label = document.createElement('time');
      label.dateTime = entry.date;
      label.textContent = entry.date_label;
      card.append(imageLink, label);
      if (entry.metadata_path) {
        const details = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = 'Show event details';
        details.appendChild(summary);
        details.addEventListener('toggle', async function () {
          if (!details.open || details.dataset.loaded) return;
          details.dataset.loaded = 'true';
          try {
            const metadataResponse = await fetch(entry.metadata_path + '?refresh=' + Date.now(), { cache: 'no-store' });
            if (!metadataResponse.ok) throw new Error('event details request failed');
            const metadata = await metadataResponse.json();
            const event = document.createElement('p');
            event.innerHTML = '<strong>Event:</strong> ';
            event.append(document.createTextNode(metadata.event || metadata.title || ''));
            const preMoment = document.createElement('p');
            preMoment.innerHTML = '<strong>Ten seconds before:</strong> ';
            preMoment.append(document.createTextNode(metadata.pre_moment || ''));
            details.append(event, preMoment);
            if (metadata.source_url) {
              const source = document.createElement('a');
              source.href = metadata.source_url;
              source.target = '_blank';
              source.rel = 'noreferrer';
              source.textContent = 'Source: ' + (metadata.source || metadata.source_url);
              details.appendChild(source);
            }
          } catch (error) {
            const failure = document.createElement('p');
            failure.textContent = 'Event details are unavailable for this archived image.';
            details.appendChild(failure);
          }
        });
        card.appendChild(details);
      }
      gallery.appendChild(card);
    });
  } catch (error) {
    status.textContent = 'Unable to load the image archive.';
  }
}());
</script>
</body>
</html>
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
        f"schedule_time={SCHEDULE_TIME}, regenerate_today={REGENERATE_TODAY}, "
        f"regenerate_on_startup={REGENERATE_ON_STARTUP}"
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
        f"disclosure={EVENT_HTML_PATH.name}, archive_viewer={ARCHIVE_HTML_PATH.name}, log={LOG_PATH.name}"
    )
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", SCHEDULE_TIME):
        log(f"Startup warning: schedule_time={SCHEDULE_TIME!r} is not valid HH:MM")
    log("Startup complete: daemon is ready for its daily schedule")


def _send_smtp_payload(payload: dict[str, Any]) -> None:
    """Send one notification through Home Assistant's SMTP service."""
    if not SUPERVISOR_TOKEN:
        raise RuntimeError("email_notify_entity is set but SUPERVISOR_TOKEN is unavailable")
    request = Request(
        f"{HOME_ASSISTANT_API_URL}/services/smtp/send_message",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "MomentInHistory/1.9.31",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"Home Assistant SMTP action failed ({exc.code}): {detail}") from exc


def event_email_html(metadata: dict[str, Any]) -> str:
    """Build a broadly compatible HTML email with a disclosure-style details block."""
    date_label = html.escape(str(metadata.get("date_label", "")), quote=True)
    event = html.escape(str(metadata.get("event", "")), quote=True)
    pre_moment = html.escape(str(metadata.get("pre_moment", "")), quote=True)
    source = html.escape(str(metadata.get("source", "")), quote=True)
    source_url = html.escape(str(metadata.get("source_url", "")), quote=True)
    return f"""<!doctype html>
<html lang="en">
  <body style="margin:0;background:#f4f1eb;color:#1c1c1c;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;line-height:1.5">
    <div style="max-width:760px;margin:0 auto;padding:24px 16px">
      <div style="color:#72551d;font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase">{date_label}</div>
      <h1 style="font-size:24px;line-height:1.2;margin:6px 0 18px">Moment in history</h1>
      <img src="cid:moment_in_history_image" alt="Moment in history illustration" style="display:block;width:100%;height:auto;border:0;margin:0 0 20px">
      <details style="background:#fff;border:1px solid #d8d1c5;border-radius:8px;padding:12px 16px">
        <summary style="cursor:pointer;font-weight:700">Show event details</summary>
        <div style="padding-top:14px">
          <p style="margin:0"><strong>Event</strong><br>{event}</p>
          <p style="margin:16px 0 0"><strong>Ten seconds before</strong><br>{pre_moment}</p>
          <p style="margin:16px 0 0"><a href="{source_url}" style="color:#315f9c">Source: {source}</a></p>
        </div>
      </details>
      <p style="color:#666;font-size:12px;margin:14px 0 0">If your email app does not support expandable sections, the complete details are included in the plain-text version of this message.</p>
    </div>
  </body>
</html>
"""


def generic_test_email_html() -> str:
    """Build the HTML fallback used before the first daily generation."""
    return """<!doctype html>
<html lang="en">
  <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;line-height:1.5;color:#1c1c1c">
    <h1>Moment in history — SMTP test</h1>
    <p>This is a test email from Moment in history.</p>
    <p>The app does not currently have a complete generated image and event available, so this generic message was sent instead.</p>
  </body>
</html>
"""


def send_event_email(
    metadata: dict[str, Any], image_path: Path, notify_entities: list[str] | None = None
) -> None:
    """Send the current image and readable event text through Home Assistant SMTP."""
    notify_entities = notify_entities if notify_entities is not None else configured_notify_entities()
    if not notify_entities:
        return
    if not EMAIL_CAMERA_ENTITY:
        raise RuntimeError("email_camera_entity must be set to attach the generated image")
    if not image_path.is_file():
        raise RuntimeError(f"generated image is missing: {image_path}")

    _send_smtp_payload(
        {
            "entity_id": notify_entities[0] if len(notify_entities) == 1 else notify_entities,
            "title": f"Moment in history — {metadata['date_label']}",
            "message": (
                f"Moment in history — {metadata['date_label']}\n\n"
                f"Event:\n{metadata['event']}\n\n"
                f"Ten seconds before:\n{metadata['pre_moment']}\n\n"
                f"Source: {metadata['source']}\n{metadata['source_url']}\n"
            ),
            "html": event_email_html(metadata),
            "attachments": [
                {
                    "media_source": {
                        "media_content_id": f"media-source://camera/{EMAIL_CAMERA_ENTITY}",
                        "media_content_type": "image/png",
                    },
                    "filename": "moment-in-history.png",
                    "content_id": "moment_in_history_image",
                }
            ],
        }
    )
    log(f"SMTP server accepted email via Home Assistant ({', '.join(notify_entities)})")


def send_test_email(notify_entities: list[str] | None = None) -> dict[str, str]:
    """Send a current-content or generic SMTP test without generating an image."""
    if notify_entities is None:
        notify_entities = configured_notify_entities()
    else:
        notify_entities = [str(value).strip() for value in notify_entities if str(value).strip()]
        if any(not value.startswith("notify.") for value in notify_entities):
            raise ValueError("Test email targets must be Home Assistant notify.* entities")
    if not notify_entities:
        raise RuntimeError("Choose at least one notify.* test recipient")
    if not SUPERVISOR_TOKEN:
        raise RuntimeError("Home Assistant API token is unavailable")

    metadata: dict[str, Any] = {}
    if EVENT_JSON_PATH.is_file() and IMAGE_PATH.is_file():
        try:
            loaded = json.loads(EVENT_JSON_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and all(
                str(loaded.get(field, "")).strip()
                for field in ("date_label", "event", "pre_moment", "source", "source_url")
            ) and EMAIL_CAMERA_ENTITY:
                send_event_email(loaded, IMAGE_PATH, notify_entities)
                return {"mode": "current"}
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    _send_smtp_payload(
        {
            "entity_id": notify_entities[0] if len(notify_entities) == 1 else notify_entities,
            "title": "Moment in history — SMTP test",
            "message": (
                "This is a test email from Moment in history.\n\n"
                "The app does not currently have a complete generated image and event available, "
                "so this generic message was sent instead."
            ),
            "html": generic_test_email_html(),
        }
    )
    log(f"SMTP server accepted generic test email via Home Assistant ({', '.join(notify_entities)})")
    return {"mode": "generic"}


def save_model_options(changes: dict[str, str]) -> None:
    """Persist Web UI selections before asking Supervisor to restart the app."""
    if not SUPERVISOR_TOKEN:
        raise RuntimeError("SUPERVISOR_TOKEN is unavailable")
    options = dict(config_ui.DEFAULT_WEB_OPTIONS)
    options.update(_load_addon_options())
    options.update(SUPERVISOR_OPTIONS)
    options.update(PERSISTED_WEB_OPTIONS)
    for field in config_ui.SELECTABLE_FIELDS:
        if field in changes:
            options[field] = changes[field]
    persisted_options = {
        field: options[field]
        for field in config_ui.WEB_SETTINGS_FIELDS
        if field in options
    }
    try:
        WEB_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        WEB_SETTINGS_PATH.write_text(
            json.dumps(persisted_options, indent=2) + "\n",
            encoding="utf-8",
        )
        PERSISTED_WEB_OPTIONS.update(persisted_options)
    except OSError as exc:
        raise RuntimeError(f"Web UI settings backup unavailable: {exc}") from exc
    request = Request(
        "http://supervisor/addons/self/options",
        data=json.dumps({"options": options}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "MomentInHistory/1.9.31",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            response.read()
    except HTTPError as exc:
        raise RuntimeError(f"Supervisor options update failed ({exc.code})") from exc


def clear_regenerate_today_request() -> None:
    """Consume the one-shot regenerate-today option after a successful generation."""
    global REGENERATE_TODAY
    if not REGENERATE_TODAY:
        return
    REGENERATE_TODAY = False
    if not SUPERVISOR_TOKEN:
        log("Regenerate today's event completed, but the one-shot option could not be reset: Supervisor API unavailable")
        return

    options = dict(config_ui.DEFAULT_WEB_OPTIONS)
    options.update(_load_addon_options())
    options.update(SUPERVISOR_OPTIONS)
    options.update(PERSISTED_WEB_OPTIONS)
    options["regenerate_today"] = False
    request = Request(
        "http://supervisor/addons/self/options",
        data=json.dumps({"options": options}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "MomentInHistory/1.9.31",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            response.read()
        log("Regenerate today's event request reset after successful generation")
    except Exception as exc:  # noqa: BLE001 - generation already succeeded
        log(f"Regenerate today's event was generated, but its one-shot option could not be reset: {type(exc).__name__}")


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


def get_smtp_details() -> dict[str, Any]:
    """Return safe editable SMTP fields for the Web UI."""
    return config_ui.smtp_config_details(HOME_ASSISTANT_API_URL, SUPERVISOR_TOKEN)


def update_smtp_integration(settings: dict[str, Any]) -> dict[str, Any]:
    """Update SMTP connection settings without changing recipients."""
    result = config_ui.update_smtp(HOME_ASSISTANT_API_URL, SUPERVISOR_TOKEN, settings)
    log("Managed SMTP integration settings updated")
    return result


def test_smtp_connection(settings: dict[str, Any]) -> dict[str, Any]:
    """Test SMTP settings entered in the Web UI without saving them."""
    return config_ui.test_smtp_connection(settings)


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
            smtp_details=get_smtp_details,
            update_smtp=update_smtp_integration,
            test_email=send_test_email,
            test_smtp_connection=test_smtp_connection,
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
    archive_path = ARCHIVE_DIR / f"{target_day.year:04d}" / f"{target_day.month:02d}" / f"{target_day.isoformat()}.png"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = archive_path.with_suffix(".tmp.png")
    shutil.copy2(IMAGE_PATH, temporary_path)
    os.replace(temporary_path, archive_path)
    size_mib = archive_path.stat().st_size / (1024 * 1024)
    log(f"Archived {target_day.isoformat()} image to {archive_path} ({size_mib:.2f} MiB)")
    try:
        archive_event_metadata(target_day)
    except OSError as exc:
        log(f"Archive event details update failed: {exc}")
    try:
        write_archive_viewer()
    except OSError as exc:
        log(f"Archive viewer update failed: {exc}")


def generate(target_day: date, *, reuse_existing_event: bool = False) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    existing_selection = load_existing_selection(target_day) if reuse_existing_event else None
    if existing_selection:
        selected, existing_metadata = existing_selection
        events = [selected]
        source_status = selected.source
        selection_reason = "Preserved the existing event for regeneration."
        previous_reason = clean_text(str(existing_metadata.get("selection_reason", "")))
        if previous_reason:
            selection_reason = f"{selection_reason} {previous_reason}"
        try:
            candidate_count = max(1, int(existing_metadata.get("candidate_count", 1)))
        except (TypeError, ValueError):
            candidate_count = 1
    else:
        try:
            events = fetch_wikipedia_events(target_day)
            source_status = "English Wikipedia On This Day"
        except (HTTPError, URLError, RuntimeError, TimeoutError) as exc:
            log(f"English Wikipedia On This Day unavailable ({exc})")
            raise

        selected, selection_reason = choose_event(events, target_day)
        candidate_count = len(events)
    pre_moment_prompt = f"Write a vivid, filmable visual brief of 60 to 90 words in 2 or 3 sentences for the exact ten seconds before this event. Treat the supplied event as authoritative context, but do not describe the event happening or its aftermath. Specify the historically appropriate location, period, lighting or weather, focal subject, up to five people's distinct posture/gaze/gesture, meaningful physical objects, and one immediate physical cue that proves the action is about to begin. Make the scene event-specific and causal, never a generic crowd or posed gathering. Mention only visible people, setting, light, atmosphere, and non-text-bearing physical objects. Do not mention paper, pages, scripts, screens, signs, labels, logos, or writing. Event: {selected.text}"
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
    event_year_label = format_event_year(selected.year)
    prompt = build_image_prompt(pre_moment, target_day, selected.text)
    temp_image = ROOT / f"moment-in-history-{int(time.time())}.png"
    image_backend_used = run_image_model(prompt, temp_image)
    overlay_date(temp_image, event_year_label)
    os.replace(temp_image, IMAGE_PATH)

    metadata = {
        "date": target_day.isoformat(),
        "date_label": date_label,
        "event": selected.text,
        "title": selected.title,
        "year": selected.year,
        "event_year_label": event_year_label,
        "pre_moment": pre_moment,
        "selection_reason": selection_reason,
        "source": source_status,
        "source_url": selected.source_url,
        "event_key": event_key(selected),
        "image_url": IMAGE_URL,
        "generated_at": datetime.now(TIME_ZONE).isoformat(),
        "image_model": IMAGE_MODEL,
        "image_provider": IMAGE_PROVIDER,
        "image_backend_used": image_backend_used,
        "openai_image_model": OPENAI_IMAGE_MODEL,
        "text_model": TEXT_MODEL,
        "candidate_count": candidate_count,
        "prompt": prompt,
    }
    write_event_pages(metadata)
    try:
        record_selection_history(target_day, selected)
    except OSError as exc:
        log(f"Event rotation history update failed: {exc}")
    archive_image(target_day)
    try:
        send_event_email(metadata, IMAGE_PATH)
    except Exception as exc:  # noqa: BLE001 - email must not stop daily generation
        log(f"Email delivery failed: {exc}")
    log(f"Generated {target_day.isoformat()} — {selected.text} ({candidate_count} candidates; {source_status})")


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
    if startup_pending and (REGENERATE_TODAY or REGENERATE_ON_STARTUP):
        return True
    if generated_date == today:
        return False
    # Startup always performs the normal schedule catch-up check. There is no
    # separate opt-in: a restart after today's schedule must not leave the day
    # without an image.
    return schedule_reached


def request_shutdown(_signum: int, _frame: Any) -> None:
    """Request a clean daemon exit without doing blocking work in a signal handler."""
    SHUTDOWN_EVENT.set()


def daemon() -> int:
    """Run inside the HA add-on and keep the daily asset current."""
    global REGENERATE_TODAY
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
        try:
            refresh_event_page()
            write_archive_viewer()
            if ARCHIVE_IMAGES and IMAGE_PATH.is_file():
                try:
                    existing_date = date.fromisoformat(
                        marker_path.read_text(encoding="utf-8").strip()
                    )
                except (OSError, ValueError):
                    existing_date = None
                if existing_date:
                    existing_archive = ARCHIVE_DIR / f"{existing_date.year:04d}" / f"{existing_date.month:02d}" / f"{existing_date.isoformat()}.png"
                    existing_metadata = existing_archive.with_suffix(".json")
                    if not existing_archive.is_file() or not existing_metadata.is_file():
                        archive_image(existing_date)
        except OSError as exc:
            log(f"Archive viewer unavailable: {exc}")
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
                if startup_pending and (REGENERATE_TODAY or REGENERATE_ON_STARTUP):
                    reason = "today's event was requested" if REGENERATE_TODAY else "regenerate_on_startup is enabled"
                    log(f"Startup state: creating a fresh image because {reason}")
                elif generated_date == today:
                    log(f"Startup state: today's image is already present ({today.isoformat()}); no generation needed")
                elif schedule_reached:
                    log(
                        f"Startup state: today's image is not present and schedule_time={SCHEDULE_TIME} "
                        "has passed; generating the missed daily event"
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
                        generate(
                            today,
                            reuse_existing_event=REGENERATE_TODAY or REGENERATE_ON_STARTUP,
                        )
                        marker_path.write_text(today.isoformat() + "\n", encoding="utf-8")
                        clear_regenerate_today_request()
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
