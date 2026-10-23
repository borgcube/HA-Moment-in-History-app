"""Home Assistant Ingress UI for configuring Moment in history."""

from __future__ import annotations

import json
import os
import re
import socket
import smtplib
import ssl
import struct
import time
from base64 import b64encode
from hashlib import sha1
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit, urlunsplit
from urllib.request import Request, urlopen


OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
MODEL_FIELDS = ("text_model", "fallback_text_model", "openai_image_model")
ENDPOINT_FIELDS = ("primary_ollama_url", "fallback_ollama_url")
IMAGE_ENDPOINT_FIELDS = ("image_backend_url", "image_backend_fallback_url")
WEB_SETTINGS_FIELDS = (
    *ENDPOINT_FIELDS,
    *MODEL_FIELDS,
    *IMAGE_ENDPOINT_FIELDS,
    "image_provider",
    "openai_api_key",
    "openai_image_quality",
    "image_model",
    "comfyui_model",
    "email_notify_entity",
    "email_camera_entity",
    "schedule_time",
)
SELECTABLE_FIELDS = WEB_SETTINGS_FIELDS
DEFAULT_WEB_OPTIONS = {
    "primary_ollama_url": "http://ollama01.borgcube.com:11434",
    "fallback_ollama_url": "http://ollama02.borgcube.com:11434",
    "text_model": "qwen3.8:27b-mlx",
    "fallback_text_model": "gemma4:e4b",
    "image_backend_url": "https://openclaw.borgcube.com/comfyui",
    "image_backend_fallback_url": "",
    "image_provider": "openai",
    "openai_api_key": "",
    "openai_image_model": "gpt-image-2",
    "openai_image_quality": "high",
    "image_model": "Tongyi-MAI/Z-Image-Turbo",
    "comfyui_model": "sd_xl_turbo_1.0.safetensors",
    "email_notify_entity": "",
    "email_camera_entity": "camera.moment_in_history",
    "schedule_time": "00:05",
}
URL_FIELDS = (*ENDPOINT_FIELDS, *IMAGE_ENDPOINT_FIELDS)
MAX_REQUEST_BYTES = 16 * 1024
SMTP_STORAGE_PATH = Path(
    os.environ.get("HOME_ASSISTANT_SMTP_STORAGE_PATH", "/config/.storage/core.config_entries")
)


def _request_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    payload: Any = None,
    timeout: int = 15,
) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={
            "Accept": "application/json",
            "User-Agent": "BORGcube-MomentInHistory/1.9.18",
            **({"Content-Type": "application/json"} if data is not None else {}),
            **(headers or {}),
        },
        method=method,
    )
    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


def _safe_error(exc: Exception) -> str:
    """Return a useful error without exposing credentials or response bodies."""
    if isinstance(exc, HTTPError):
        detail = ""
        try:
            body = exc.read(4096).decode("utf-8", errors="replace").strip()
            if body:
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    payload = body
                candidates: list[str] = []
                if isinstance(payload, dict):
                    for key in ("message", "error", "reason"):
                        value = payload.get(key)
                        if value:
                            candidates.append(str(value))
                    errors = payload.get("errors")
                    if isinstance(errors, dict):
                        candidates.extend(str(value) for value in errors.values() if value)
                elif isinstance(payload, str):
                    candidates.append(payload)
                for candidate in candidates:
                    cleaned = re.sub(r"\s+", " ", candidate).strip()
                    cleaned = re.sub(
                        r"(?i)(password|token|authorization|api[_ -]?key)\s*[:=]\s*\S+",
                        r"\1=[redacted]",
                        cleaned,
                    )
                    if cleaned and len(cleaned) <= 240:
                        detail = cleaned
                        break
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        return f"HTTP {exc.code}: {detail}" if detail else f"HTTP {exc.code}"
    if isinstance(exc, (URLError, TimeoutError)):
        return "network or timeout error"
    if isinstance(exc, (ValueError, RuntimeError)):
        message = str(exc).strip()
        if message:
            message = re.sub(
                r"(?i)(password|token|authorization|api[_ -]?key)\s*[:=]\s*\S+",
                r"\1=[redacted]",
                message,
            )
            return message[:240]
    return "invalid response"


def _display_url(value: str) -> str:
    """Return an endpoint label without embedded credentials or query data."""
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.hostname:
        return value
    host = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port:
        host = f"{host}:{port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def ollama_models(base_url: str) -> list[str]:
    """Return model names advertised by an Ollama endpoint."""
    if not base_url.strip():
        return []
    payload = _request_json(f"{base_url.rstrip('/')}/api/tags")
    if not isinstance(payload, dict):
        raise ValueError("Ollama response was not an object")
    models = payload.get("models") or []
    names = {
        str(item.get("name", "")).strip()
        for item in models
        if isinstance(item, dict)
        and str(item.get("name", "")).strip()
        and _is_text_model(item)
    }
    return sorted(names, key=str.casefold)


def _is_text_model(item: dict[str, Any]) -> bool:
    """Exclude embedding and diffusion-only models from chat model choices."""
    name = str(item.get("name", "")).casefold()
    details = item.get("details") or {}
    family = str(details.get("family", "")).casefold() if isinstance(details, dict) else ""
    families = details.get("families") if isinstance(details, dict) else []
    if not isinstance(families, (list, tuple)):
        families = []
    family_names = " ".join(str(value).casefold() for value in families if value)
    return not (
        "embed" in name
        or "bert" in family
        or "bert" in family_names
        or any(token in name for token in ("flux", "diffusion", "stable-diffusion", "sdxl"))
    )


def openai_image_models(api_key: str) -> list[str]:
    """Return image-capable model IDs visible to the configured OpenAI key."""
    if not api_key.strip():
        return []
    payload = _request_json(
        OPENAI_MODELS_URL,
        headers={"Authorization": f"Bearer {api_key.strip()}"},
    )
    if not isinstance(payload, dict):
        raise ValueError("OpenAI response was not an object")
    model_ids = {
        str(item.get("id", "")).strip()
        for item in payload.get("data", [])
        if isinstance(item, dict) and str(item.get("id", "")).strip()
    }
    return sorted(
        (model_id for model_id in model_ids if _is_image_model(model_id)),
        key=lambda value: value.casefold(),
    )


def image_backend_models(base_url: str) -> list[str]:
    """Discover a compatible API model or ComfyUI checkpoint."""
    if not base_url.strip():
        return []
    last_error: Exception | None = None

    # The compatible local image service exposes its loaded model through
    # /health and intentionally does not implement ComfyUI's checkpoint APIs.
    try:
        health = _request_json(f"{base_url.rstrip('/')}/health")
        if isinstance(health, dict):
            model = str(health.get("model", "")).strip()
            if model:
                return [model]
    except Exception as exc:  # noqa: BLE001 - try the ComfyUI API next
        last_error = exc

    for path in ("/models/checkpoints", "/object_info/CheckpointLoaderSimple"):
        try:
            payload = _request_json(f"{base_url.rstrip('/')}{path}")
            names: set[str] = set()
            if isinstance(payload, list):
                names.update(str(value).strip() for value in payload if str(value).strip())
            elif isinstance(payload, dict):
                values = payload.get("models") or payload.get("checkpoints") or payload.get("data")
                if isinstance(values, list):
                    for value in values:
                        if isinstance(value, str) and value.strip():
                            names.add(value.strip())
                        elif isinstance(value, dict):
                            for key in ("id", "name", "filename"):
                                candidate = str(value.get(key, "")).strip()
                                if candidate:
                                    names.add(candidate)
                nodes = payload.values()
                for node in nodes:
                    if not isinstance(node, dict):
                        continue
                    required = (node.get("input") or {}).get("required", {})
                    checkpoint = required.get("ckpt_name") if isinstance(required, dict) else None
                    choices = checkpoint[0] if isinstance(checkpoint, list) and checkpoint else checkpoint
                    if isinstance(choices, list):
                        names.update(str(value).strip() for value in choices if str(value).strip())
            if names:
                return sorted(names, key=str.casefold)
        except Exception as exc:  # noqa: BLE001 - discovery is best effort
            last_error = exc
    if last_error:
        raise last_error
    return []


def _is_image_model(model_id: str) -> bool:
    normalized = model_id.casefold()
    return (
        normalized.startswith("gpt-image")
        or normalized.startswith("dall-e")
        or normalized.startswith("chatgpt-image")
        or "image" in normalized
    )


def discover_models(options: dict[str, Any]) -> dict[str, Any]:
    """Discover models without returning any configured secrets."""
    primary_url = str(options.get("primary_ollama_url", "")).strip()
    fallback_url = str(options.get("fallback_ollama_url", "")).strip()
    api_key = str(options.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")).strip()

    result: dict[str, Any] = {
        "config": {
            "primary_ollama_url": _display_url(primary_url),
            "fallback_ollama_url": _display_url(fallback_url),
            "text_model": str(options.get("text_model", "")),
            "fallback_text_model": str(options.get("fallback_text_model", "")),
            "image_backend_url": _display_url(str(options.get("image_backend_url", "")).strip()),
            "image_backend_fallback_url": _display_url(str(options.get("image_backend_fallback_url", "")).strip()),
            "image_provider": str(options.get("image_provider", "openai")),
            "openai_image_model": str(options.get("openai_image_model", "")),
            "openai_image_quality": str(options.get("openai_image_quality", "high")),
            "image_model": str(options.get("image_model", "")),
            "comfyui_model": str(options.get("comfyui_model", "")),
            "email_notify_entity": str(options.get("email_notify_entity", "")),
            "email_camera_entity": str(options.get("email_camera_entity", "")),
            "schedule_time": str(options.get("schedule_time", "00:05")),
            "openai_key_present": bool(api_key),
        },
        "models": {
            "primary_ollama": [],
            "fallback_ollama": [],
            "openai_image": [],
            "image_backend": [],
        },
        "errors": {},
    }

    for key, url in (
        ("primary_ollama", primary_url),
        ("fallback_ollama", fallback_url),
    ):
        if not url:
            result["errors"][key] = "endpoint is not configured"
            continue
        try:
            result["models"][key] = ollama_models(url)
        except Exception as exc:  # noqa: BLE001 - show a safe UI diagnostic
            result["errors"][key] = _safe_error(exc)

    if api_key:
        try:
            result["models"]["openai_image"] = openai_image_models(api_key)
        except Exception as exc:  # noqa: BLE001 - show a safe UI diagnostic
            result["errors"]["openai_image"] = _safe_error(exc)
    else:
        result["errors"]["openai_image"] = "enter an OpenAI API key in App Configuration first"

    image_urls = (
        ("image_backend", str(options.get("image_backend_url", "")).strip()),
        ("image_backend_fallback", str(options.get("image_backend_fallback_url", "")).strip()),
    )
    for key, url in image_urls:
        if not url:
            continue
        try:
            discovered = image_backend_models(url)
            result["models"]["image_backend"] = sorted(
                set(result["models"]["image_backend"]) | set(discovered),
                key=str.casefold,
            )
        except Exception as exc:  # noqa: BLE001 - show a safe UI diagnostic
            result["errors"][key] = _safe_error(exc)

    return result


def home_assistant_notify_entities(api_url: str, supervisor_token: str) -> list[str]:
    """Return Home Assistant notify entities without exposing their states."""
    if not supervisor_token.strip():
        return []
    payload = _request_json(
        f"{api_url.rstrip('/')}/states",
        headers={"Authorization": f"Bearer {supervisor_token.strip()}"},
    )
    states = payload if isinstance(payload, list) else payload.get("states", []) if isinstance(payload, dict) else []
    return sorted(
        {
            str(item.get("entity_id", "")).strip()
            for item in states
            if isinstance(item, dict) and str(item.get("entity_id", "")).startswith("notify.")
        },
        key=str.casefold,
    )


def _home_assistant_headers(token: str) -> dict[str, str]:
    if not token.strip():
        raise RuntimeError("Home Assistant API token is unavailable")
    return {"Authorization": f"Bearer {token.strip()}"}


def _smtp_config_entries(api_url: str, token: str) -> list[dict[str, Any]]:
    payload = _request_json(
        f"{api_url.rstrip('/')}/config/config_entries/entry?domain=smtp",
        headers=_home_assistant_headers(token),
    )
    if not isinstance(payload, list):
        raise ValueError("Home Assistant returned an invalid SMTP entry list")
    return [
        entry
        for entry in payload
        if isinstance(entry, dict) and str(entry.get("domain", "")) == "smtp"
    ]


_SMTP_EDITABLE_FIELDS = {
    "sender",
    "sender_name",
    "server",
    "port",
    "encryption",
    "username",
    "verify_ssl",
}


def _smtp_flow_values(flow: dict[str, Any]) -> dict[str, Any]:
    """Extract non-secret current values from an SMTP reconfigure form."""
    schema = flow.get("data_schema")
    if not isinstance(schema, list):
        return {}
    values: dict[str, Any] = {}
    for field in schema:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name", "")).strip()
        if name not in _SMTP_EDITABLE_FIELDS:
            continue
        if "value" in field:
            values[name] = field["value"]
        elif "suggested_value" in field:
            values[name] = field["suggested_value"]
        elif isinstance(field.get("description"), dict) and "suggested_value" in field["description"]:
            # Home Assistant serializes values added by
            # ``add_suggested_values_to_schema`` inside the field description.
            values[name] = field["description"]["suggested_value"]
    return values


def _smtp_reconfigure_values(api_url: str, token: str, entry_id: str) -> dict[str, Any]:
    """Read current SMTP values through a temporary, cancelled reconfigure flow."""
    flow = _request_json(
        f"{api_url.rstrip('/')}/config/config_entries/flow",
        headers=_home_assistant_headers(token),
        method="POST",
        payload={"handler": "smtp", "entry_id": entry_id},
    )
    flow_id = str(flow.get("flow_id", "")) if isinstance(flow, dict) else ""
    if not flow_id:
        raise RuntimeError("Home Assistant did not start the SMTP details flow")
    try:
        if not isinstance(flow, dict) or flow.get("type") != "form":
            return {}
        return _smtp_flow_values(flow)
    finally:
        # Reading the form creates a real config flow, so always cancel it and
        # leave the saved SMTP integration untouched.
        try:
            _request_json(
                f"{api_url.rstrip('/')}/config/config_entries/flow/{flow_id}",
                headers=_home_assistant_headers(token),
                method="DELETE",
            )
        except Exception:
            pass


def smtp_config_details(api_url: str, token: str) -> dict[str, Any]:
    """Return editable SMTP fields without returning the stored password."""
    entries = _smtp_config_entries(api_url, token)
    if not entries:
        return {"configured": False}
    entry = entries[0]
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    if not data:
        # Home Assistant's REST list response deliberately omits config-entry
        # data.  Use the supported reconfigure flow to obtain the current form
        # values, then cancel it; never expose or retain the password.
        try:
            data = _smtp_reconfigure_values(api_url, token, str(entry.get("entry_id", "")))
        except Exception:
            data = {}
    return {
        "configured": True,
        "entry_id": str(entry.get("entry_id", "")),
        "sender": str(data.get("sender", "")),
        "sender_name": str(data.get("sender_name", "")),
        "server": str(data.get("server", "")),
        "port": data.get("port", 587),
        "encryption": str(data.get("encryption", "starttls")),
        "username": str(data.get("username", "")),
        "verify_ssl": bool(data.get("verify_ssl", True)),
    }


def _websocket_url(api_url: str) -> tuple[str, str, int | None, str]:
    parsed = urlsplit(api_url.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Home Assistant API URL must use http or https")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = parsed.path.removesuffix("/api").rstrip("/") + "/websocket"
    return scheme, parsed.hostname, parsed.port, path or "/websocket"


def _websocket_send(sock: socket.socket, payload: dict[str, Any]) -> None:
    _websocket_send_frame(sock, json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def _websocket_send_frame(sock: socket.socket, body: bytes, opcode: int = 1) -> None:
    mask = os.urandom(4)
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(body))
    length = len(masked)
    if length < 126:
        header = bytes((0x80 | opcode, 0x80 | length))
    elif length < 65536:
        header = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack("!H", length)
    else:
        header = bytes((0x80 | opcode, 0x80 | 127)) + struct.pack("!Q", length)
    sock.sendall(header + mask + masked)


def _websocket_receive(sock: socket.socket) -> tuple[int, bytes]:
    header = _read_exact(sock, 2)
    first, second = header
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _read_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exact(sock, 8))[0]
    mask = _read_exact(sock, 4) if second & 0x80 else None
    body = _read_exact(sock, length)
    if mask:
        body = bytes(value ^ mask[index % 4] for index, value in enumerate(body))
    return first & 0x0F, body


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise RuntimeError("Home Assistant WebSocket closed unexpectedly")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class _BufferedSocket:
    """Expose handshake bytes already read before WebSocket framing starts."""

    def __init__(self, sock: socket.socket, buffered: bytes) -> None:
        self._sock = sock
        self._buffered = bytearray(buffered)

    def recv(self, size: int) -> bytes:
        if self._buffered:
            chunk = bytes(self._buffered[:size])
            del self._buffered[:size]
            return chunk
        return self._sock.recv(size)

    def sendall(self, data: bytes) -> None:
        self._sock.sendall(data)

    def close(self) -> None:
        self._sock.close()


def _websocket_command(
    api_url: str,
    token: str,
    message: dict[str, Any],
    *,
    timeout: int = 15,
) -> Any:
    """Run one authenticated Home Assistant WebSocket command with stdlib only."""
    scheme, host, port, path = _websocket_url(api_url)
    connection = socket.create_connection((host, port or (443 if scheme == "wss" else 80)), timeout=timeout)
    connection.settimeout(timeout)
    if scheme == "wss":
        connection = ssl.create_default_context().wrap_socket(connection, server_hostname=host)
    key = b64encode(os.urandom(16)).decode("ascii")
    host_header = host if port is None else f"{host}:{port}"
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")
    try:
        connection.sendall(request)
        response = b""
        while b"\r\n\r\n" not in response:
            response += connection.recv(4096)
            if len(response) > 32 * 1024:
                raise RuntimeError("Home Assistant WebSocket handshake was too large")
        header, buffered = response.split(b"\r\n\r\n", 1)
        if not header.startswith(b"HTTP/1.1 101"):
            raise RuntimeError("Home Assistant WebSocket handshake was rejected")
        response_headers = {
            line.partition(b":")[0].strip().lower(): line.partition(b":")[2].strip()
            for line in header.split(b"\r\n")[1:]
            if b":" in line
        }
        expected_accept = b64encode(sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest())
        if response_headers.get(b"sec-websocket-accept") != expected_accept:
            raise RuntimeError("Home Assistant WebSocket handshake was invalid")
        connection = _BufferedSocket(connection, buffered)

        opcode, body = _websocket_receive(connection)
        if opcode != 1 or json.loads(body).get("type") != "auth_required":
            raise RuntimeError("Home Assistant WebSocket authentication was not requested")
        _websocket_send(connection, {"type": "auth", "access_token": token.strip()})
        opcode, body = _websocket_receive(connection)
        auth = json.loads(body) if opcode == 1 else {}
        if auth.get("type") != "auth_ok":
            raise RuntimeError("Home Assistant WebSocket authentication failed")

        command = {"id": 1, **message}
        _websocket_send(connection, command)
        while True:
            opcode, body = _websocket_receive(connection)
            if opcode == 9:
                _websocket_send_frame(connection, body, opcode=10)
                continue
            if opcode != 1:
                continue
            result = json.loads(body)
            if result.get("type") != "result" or result.get("id") != 1:
                continue
            if not result.get("success"):
                error = result.get("error") or {}
                raise RuntimeError(str(error.get("message", "Home Assistant command failed")))
            return result.get("result")
    finally:
        connection.close()


def _home_assistant_states(api_url: str, token: str) -> list[dict[str, Any]]:
    payload = _request_json(
        f"{api_url.rstrip('/')}/states",
        headers=_home_assistant_headers(token),
    )
    states = payload if isinstance(payload, list) else payload.get("states", []) if isinstance(payload, dict) else []
    return [state for state in states if isinstance(state, dict)]


def _find_notify_entity(states: list[dict[str, Any]], email: str) -> str | None:
    normalized = email.casefold()
    for state in states:
        entity_id = str(state.get("entity_id", ""))
        attributes = state.get("attributes") or {}
        friendly_name = str(attributes.get("friendly_name", ""))
        if entity_id.startswith("notify.") and friendly_name.casefold() == normalized:
            return entity_id
    return None


def _find_notify_entity_in_registry(
    registry: Any,
    *,
    entry_id: str,
    subentry_id: str,
    email: str,
) -> str | None:
    """Resolve an SMTP notify entity using Home Assistant's entity registry."""
    entities = registry.get("entities", []) if isinstance(registry, dict) else registry
    if not isinstance(entities, list):
        return None
    expected_unique_id = f"{entry_id}_{email}".casefold()
    for entity in entities:
        if not isinstance(entity, dict) or not str(entity.get("entity_id", "")).startswith("notify."):
            continue
        if str(entity.get("config_entry_id", "")) != entry_id:
            continue
        if subentry_id and str(entity.get("config_subentry_id", "")) == subentry_id:
            return str(entity["entity_id"])
        if str(entity.get("unique_id", "")).casefold() == expected_unique_id:
            return str(entity["entity_id"])
    return None


def smtp_recipient_details(api_url: str, token: str) -> dict[str, Any]:
    """Return SMTP recipient subentries and their current notify entity IDs."""
    entries = _smtp_config_entries(api_url, token)
    if not entries:
        return {"available": False, "message": "No Home Assistant SMTP integration is configured.", "recipients": []}
    states = _home_assistant_states(api_url, token)
    registry: Any = None
    recipients: list[dict[str, Any]] = []
    for entry in entries:
        entry_id = str(entry.get("entry_id", ""))
        subentries = _websocket_command(
            api_url,
            token,
            {"type": "config_entries/subentries/list", "entry_id": entry_id},
        )
        for subentry in subentries or []:
            if not isinstance(subentry, dict) or subentry.get("subentry_type") != "recipient":
                continue
            email = str(subentry.get("unique_id", "")).strip()
            subentry_id = str(subentry.get("subentry_id", ""))
            entity_id = _find_notify_entity(states, email)
            if not entity_id:
                if registry is None:
                    try:
                        registry = _websocket_command(
                            api_url,
                            token,
                            {"type": "config/entity_registry/list"},
                        )
                    except Exception:
                        registry = []
                entity_id = _find_notify_entity_in_registry(
                    registry,
                    entry_id=entry_id,
                    subentry_id=subentry_id,
                    email=email,
                )
            recipients.append(
                {
                    "email": email,
                    "entity_id": entity_id,
                    "entry_id": entry_id,
                    "subentry_id": subentry_id,
                }
            )
    return {"available": True, "message": "SMTP recipients are managed by Home Assistant.", "recipients": recipients}


def _config_flow_error(result: Any, fallback: str) -> str:
    """Extract a safe config-flow error code without returning submitted values."""
    if not isinstance(result, dict):
        return fallback
    errors = result.get("errors")
    if isinstance(errors, dict):
        for value in errors.values():
            if value:
                return str(value)
    reason = result.get("reason")
    return str(reason) if reason else fallback


def _smtp_reconfigure_succeeded(result: Any) -> bool:
    """Recognize Home Assistant's successful SMTP reconfigure responses."""
    if not isinstance(result, dict):
        return False
    if result.get("type") in {"create_entry", "reconfigure_successful"}:
        return True
    return result.get("type") == "abort" and result.get("reason") == "reconfigure_successful"


def _wait_for_smtp_recipient(api_url: str, token: str, email: str) -> dict[str, Any]:
    """Wait for Home Assistant to expose the notify entity after a flow creates it."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        details = smtp_recipient_details(api_url, token)
        for recipient in details["recipients"]:
            if recipient["email"].casefold() == email.casefold() and recipient.get("entity_id"):
                return recipient
        time.sleep(0.5)
    raise RuntimeError("Home Assistant created the recipient, but its notify entity is not ready yet")


def add_smtp_recipient(api_url: str, token: str, email: str) -> dict[str, Any]:
    """Create one recipient through Home Assistant's supported SMTP config flow."""
    email = email.strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("Enter a valid email address")
    entries = _smtp_config_entries(api_url, token)
    if not entries:
        raise RuntimeError("Configure the Home Assistant SMTP integration first")
    details = smtp_recipient_details(api_url, token)
    for recipient in details["recipients"]:
        if recipient["email"].casefold() == email.casefold():
            if not recipient.get("entity_id"):
                return _wait_for_smtp_recipient(api_url, token, email)
            return recipient
    entry_id = str(entries[0].get("entry_id", ""))
    flow = _request_json(
        f"{api_url.rstrip('/')}/config/config_entries/subentries/flow",
        headers=_home_assistant_headers(token),
        method="POST",
        payload={"handler": [entry_id, "recipient"]},
    )
    flow_id = str(flow.get("flow_id", "")) if isinstance(flow, dict) else ""
    if not flow_id:
        raise RuntimeError("Home Assistant did not start the SMTP recipient flow")
    # Home Assistant's config-flow REST endpoint accepts the submitted form
    # fields directly.  Wrapping them in ``user_input`` makes that wrapper
    # itself look like an unknown form field on current releases.
    result = _request_json(
        f"{api_url.rstrip('/')}/config/config_entries/subentries/flow/{flow_id}",
        headers=_home_assistant_headers(token),
        method="POST",
        payload={"recipient": email},
    )
    if not isinstance(result, dict) or result.get("type") != "create_entry":
        raise RuntimeError(_config_flow_error(result, "Home Assistant rejected the recipient"))
    return _wait_for_smtp_recipient(api_url, token, email)


def _smtp_connection_input(settings: dict[str, Any], *, include_options: bool = True) -> dict[str, Any]:
    """Validate SMTP connection fields and build Home Assistant flow input."""
    sender = str(settings.get("sender", "")).strip()
    sender_name = str(settings.get("sender_name", "")).strip()
    server = str(settings.get("server", "")).strip()
    username = str(settings.get("username", "")).strip()
    password = str(settings.get("password", ""))
    encryption = str(settings.get("encryption", "starttls")).strip().lower()
    try:
        port = int(settings.get("port", 587))
    except (TypeError, ValueError) as exc:
        raise ValueError("SMTP port must be a number") from exc
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", sender):
        raise ValueError("Enter a valid sender email address")
    if not server:
        raise ValueError("Enter the SMTP server host")
    if not 1 <= port <= 65535:
        raise ValueError("SMTP port must be between 1 and 65535")
    if encryption not in {"starttls", "tls", "none"}:
        raise ValueError("Choose STARTTLS, TLS, or no encryption")

    user_input: dict[str, Any] = {
        "sender": sender,
        "server": server,
        "port": port,
        "encryption": encryption,
        "verify_ssl": bool(settings.get("verify_ssl", True)),
    }
    if include_options:
        user_input["options"] = {}
    if sender_name:
        user_input["sender_name"] = sender_name
    if username:
        user_input["username"] = username
    if password:
        user_input["password"] = password
    return user_input


def _saved_smtp_data() -> dict[str, Any]:
    """Read saved SMTP data for an internal connection test only."""
    try:
        payload = json.loads(SMTP_STORAGE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    stored_data = payload.get("data", {}) if isinstance(payload, dict) else {}
    if isinstance(stored_data, dict):
        entries = stored_data.get("entries", [])
    else:
        entries = stored_data
    if not isinstance(entries, list):
        return {}
    for entry in entries:
        if not isinstance(entry, dict) or str(entry.get("domain", "")) != "smtp":
            continue
        data = entry.get("data")
        return data if isinstance(data, dict) else {}
    return {}


def _settings_for_existing_smtp_test(settings: dict[str, Any]) -> dict[str, Any]:
    """Fill missing auth fields from Home Assistant's saved SMTP entry."""
    if not settings.get("use_saved_credentials") or str(settings.get("password", "")):
        return settings
    saved = _saved_smtp_data()
    if not saved:
        return settings
    result = dict(settings)
    if not str(result.get("username", "")).strip() and saved.get("username"):
        result["username"] = saved["username"]
    if saved.get("password"):
        result["password"] = saved["password"]
    return result


def test_smtp_connection(settings: dict[str, Any]) -> dict[str, Any]:
    """Validate and test SMTP connectivity without saving settings or sending mail."""
    settings = _settings_for_existing_smtp_test(settings)
    user_input = _smtp_connection_input(settings, include_options=False)
    username = str(user_input.get("username", ""))
    password = str(user_input.get("password", ""))
    if bool(username) != bool(password):
        raise ValueError("Enter both the SMTP username and password, or leave both blank")

    verify_ssl = bool(user_input.get("verify_ssl", True))
    context = ssl.create_default_context()
    if not verify_ssl:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    server: smtplib.SMTP | smtplib.SMTP_SSL | None = None
    try:
        if user_input["encryption"] == "tls":
            server = smtplib.SMTP_SSL(
                str(user_input["server"]),
                int(user_input["port"]),
                timeout=15,
                context=context,
            )
        else:
            server = smtplib.SMTP(
                str(user_input["server"]),
                int(user_input["port"]),
                timeout=15,
            )
            server.ehlo()
            if user_input["encryption"] == "starttls":
                server.starttls(context=context)
                server.ehlo()
        authenticated = False
        if username and password:
            server.login(username, password)
            authenticated = True
        return {
            "ok": True,
            "authenticated": authenticated,
            "server": str(user_input["server"]),
            "port": int(user_input["port"]),
        }
    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError("SMTP authentication failed") from exc
    except (OSError, smtplib.SMTPException) as exc:
        detail = str(exc).strip()
        raise RuntimeError(f"SMTP connection failed: {detail[:200]}" if detail else "SMTP connection failed") from exc
    finally:
        if server is not None:
            try:
                server.quit()
            except (OSError, smtplib.SMTPException):
                server.close()


def configure_smtp(api_url: str, token: str, settings: dict[str, Any]) -> dict[str, Any]:
    """Create Home Assistant's SMTP integration and its first recipient through config flows."""
    recipient = str(settings.get("recipient", "")).strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", recipient):
        raise ValueError("Enter a valid recipient email address")
    if _smtp_config_entries(api_url, token):
        raise RuntimeError("Home Assistant SMTP is already configured; add a recipient instead")

    user_input = _smtp_connection_input(settings)

    flow = _request_json(
        f"{api_url.rstrip('/')}/config/config_entries/flow",
        headers=_home_assistant_headers(token),
        method="POST",
        payload={"handler": "smtp"},
    )
    flow_id = str(flow.get("flow_id", "")) if isinstance(flow, dict) else ""
    if not flow_id:
        raise RuntimeError("Home Assistant did not start the SMTP setup flow")
    result = _request_json(
        f"{api_url.rstrip('/')}/config/config_entries/flow/{flow_id}",
        headers=_home_assistant_headers(token),
        method="POST",
        payload=user_input,
    )
    if not isinstance(result, dict) or result.get("type") != "create_entry":
        raise RuntimeError(_config_flow_error(result, "Home Assistant rejected the SMTP settings"))

    next_flow = result.get("next_flow")
    if isinstance(next_flow, list) and len(next_flow) >= 2 and str(next_flow[1]).strip():
        recipient_result = _request_json(
            f"{api_url.rstrip('/')}/config/config_entries/subentries/flow/{str(next_flow[1])}",
            headers=_home_assistant_headers(token),
            method="POST",
            payload={"recipient": recipient},
        )
        if not isinstance(recipient_result, dict) or recipient_result.get("type") != "create_entry":
            raise RuntimeError(
                "SMTP was configured, but Home Assistant rejected the first recipient: "
                + _config_flow_error(recipient_result, "recipient setup failed")
            )
    else:
        # Keep compatibility with versions that create the parent entry without
        # returning the next recipient flow.
        add_smtp_recipient(api_url, token, recipient)
    return _wait_for_smtp_recipient(api_url, token, recipient)


def update_smtp(api_url: str, token: str, settings: dict[str, Any]) -> dict[str, Any]:
    """Update an existing Home Assistant SMTP config entry through reconfigure."""
    entries = _smtp_config_entries(api_url, token)
    if not entries:
        raise RuntimeError("Home Assistant SMTP is not configured yet")
    entry_id = str(entries[0].get("entry_id", "")).strip()
    if not entry_id:
        raise RuntimeError("Home Assistant returned an SMTP entry without an entry ID")
    flow = _request_json(
        f"{api_url.rstrip('/')}/config/config_entries/flow",
        headers=_home_assistant_headers(token),
        method="POST",
        payload={"handler": "smtp", "entry_id": entry_id},
    )
    flow_id = str(flow.get("flow_id", "")) if isinstance(flow, dict) else ""
    if not flow_id:
        raise RuntimeError("Home Assistant did not start the SMTP update flow")
    result = _request_json(
        f"{api_url.rstrip('/')}/config/config_entries/flow/{flow_id}",
        headers=_home_assistant_headers(token),
        method="POST",
        payload=_smtp_connection_input(settings, include_options=False),
    )
    if not _smtp_reconfigure_succeeded(result):
        raise RuntimeError(_config_flow_error(result, "Home Assistant rejected the SMTP update"))
    return smtp_config_details(api_url, token)


def delete_smtp_recipient(api_url: str, token: str, recipient: dict[str, Any]) -> None:
    """Delete only the SMTP subentry previously selected by the user."""
    entry_id = str(recipient.get("entry_id", ""))
    subentry_id = str(recipient.get("subentry_id", ""))
    if not entry_id or not subentry_id:
        raise RuntimeError("This recipient was not created through the supported SMTP flow")
    _websocket_command(
        api_url,
        token,
        {
            "type": "config_entries/subentries/delete",
            "entry_id": entry_id,
            "subentry_id": subentry_id,
        },
    )


def _page() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Moment in history — configuration</title>
  <style>
    :root { color-scheme: dark; font-family: system-ui, sans-serif; }
    body { margin: 0; background: #111827; color: #f3f4f6; }
    main { max-width: 860px; margin: auto; padding: 24px; }
    section { background: #1f2937; border-radius: 12px; padding: 18px; margin: 16px 0; }
    h1 { margin-top: 0; font-size: 1.55rem; }
    h2 { font-size: 1.1rem; margin: 0 0 8px; }
    p, label { color: #d1d5db; }
    label { display: block; margin: 14px 0 6px; }
    select, input, button { box-sizing: border-box; width: 100%; padding: 10px; border-radius: 8px; border: 1px solid #4b5563; font: inherit; }
    select, input { background: #111827; color: #f9fafb; }
    button { background: #d9a441; border-color: #d9a441; color: #111827; cursor: pointer; font-weight: 700; margin-top: 10px; }
    button.secondary { background: transparent; color: #f9fafb; }
    button:disabled, select:disabled, input:disabled { opacity: .55; cursor: not-allowed; }
    .recipient-row { display: flex; gap: 8px; align-items: center; }
    .recipient-row input { flex: 1; }
    .recipient-row button { width: auto; min-width: 92px; margin-top: 0; }
    .pills { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
    .pill { display: inline-flex; align-items: center; gap: 8px; padding: 7px 10px; border-radius: 999px; background: #374151; color: #f9fafb; font-size: .9rem; }
    .pill button { width: auto; min-width: 0; padding: 0 2px; margin: 0; border: 0; background: transparent; color: #fca5a5; font-size: 1.1rem; line-height: 1; }
    .url, .status, .error { overflow-wrap: anywhere; font-size: .9rem; }
    .url { color: #9ca3af; }
    .status { min-height: 1.4em; margin-top: 12px; }
    .error { color: #fca5a5; margin: 8px 0; }
    .hint { color: #9ca3af; font-size: .9rem; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 0 14px; }
    .grid > div { min-width: 0; }
    .full { grid-column: 1 / -1; }
    .toggle { display: flex; gap: 10px; align-items: center; margin: 8px 0 14px; }
    .toggle input { width: auto; }
    dialog { max-width: 680px; width: calc(100% - 32px); border: 1px solid #4b5563; border-radius: 12px; background: #1f2937; color: #f3f4f6; }
    dialog::backdrop { background: rgba(0, 0, 0, .72); }
    dialog form { display: grid; gap: 8px; }
    dialog menu { display: flex; gap: 8px; justify-content: flex-end; padding: 0; margin: 12px 0 0; }
    dialog menu button { width: auto; min-width: 110px; }
  </style>
</head>
<body>
<main>
  <h1>Moment in history — configuration</h1>
  <p>This is the app’s configuration page. Home Assistant App Configuration contains only the core image and restart controls. Configure providers, models, schedules, and email here; changes are saved through Home Assistant and restart the app without generating an image unless you explicitly enable that option.</p>

  <section>
    <h2>Text and event selection</h2>
    <p class="hint">Models are discovered directly from the Ollama APIs using <code>/api/tags</code>. Open WebUI is not required.</p>
    <div class="grid">
      <div>
        <label for="primary-url-input">Primary Ollama API URL</label>
        <input id="primary-url-input" type="url" inputmode="url" placeholder="http://ollama01.example.local:11434">
      </div>
      <div>
        <label for="fallback-url-input">Fallback Ollama API URL</label>
        <input id="fallback-url-input" type="url" inputmode="url" placeholder="http://ollama02.example.local:11434">
      </div>
      <div>
        <label for="primary-model">Primary text model</label>
        <select id="primary-model"></select>
      </div>
      <div>
        <label for="fallback-model">Fallback text model</label>
        <select id="fallback-model"></select>
      </div>
    </div>
  </section>

  <section>
    <h2>Image generation</h2>
    <div class="grid">
      <div>
        <label for="image-provider">Image provider</label>
        <select id="image-provider">
          <option value="openai">OpenAI Images</option>
          <option value="comfyui">ComfyUI</option>
          <option value="lan">Compatible local image API</option>
        </select>
      </div>
      <div>
        <label for="openai-quality">OpenAI image quality</label>
        <select id="openai-quality">
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
        </select>
      </div>
      <div class="full">
        <label for="openai-key">OpenAI API key</label>
        <input id="openai-key" type="password" autocomplete="new-password" placeholder="Leave blank to keep the saved key">
        <label class="toggle"><input id="clear-openai-key" type="checkbox"> Clear the saved OpenAI API key</label>
        <p class="hint" id="openai-status">The key is stored by Home Assistant and is never returned to this page.</p>
      </div>
      <div>
        <label for="openai-model">OpenAI image model</label>
        <select id="openai-model"></select>
      </div>
      <div>
        <label for="image-model">Local image model</label>
        <input id="image-model" type="text" placeholder="Tongyi-MAI/Z-Image-Turbo">
      </div>
      <div>
        <label for="image-backend-url">Local image API / ComfyUI URL</label>
        <input id="image-backend-url" type="url" inputmode="url" placeholder="https://openclaw.borgcube.com/comfyui">
      </div>
      <div>
        <label for="image-backend-fallback-url">Fallback local image API / ComfyUI URL</label>
        <input id="image-backend-fallback-url" type="url" inputmode="url" placeholder="Optional">
      </div>
      <div class="full">
        <label for="comfyui-model">Local image model / ComfyUI checkpoint</label>
        <input id="comfyui-model" list="comfyui-model-options" type="text" placeholder="sd_xl_turbo_1.0.safetensors">
        <datalist id="comfyui-model-options"></datalist>
        <p class="hint">The app detects a compatible local API model from <code>/health</code> or ComfyUI checkpoints from its API. Image size is selected in the App Configuration page. Image steps apply only to local diffusion backends; OpenAI ignores them.</p>
      </div>
    </div>
  </section>

  <section>
    <h2>Daily schedule</h2>
    <label for="schedule-time">Daily generation time</label>
    <input id="schedule-time" type="time" required>
    <p class="hint">The app checks every 30 seconds and generates once after this local time. Restarts preserve the current image.</p>
  </section>

  <section>
    <h2>Email delivery</h2>
    <label class="toggle"><input id="email-enabled" type="checkbox"> Send the daily image and event by email</label>
    <p class="hint" id="email-status">Enable email delivery to check Home Assistant SMTP and configure it if needed.</p>
    <div class="recipient-row">
      <button id="edit-smtp" class="secondary" type="button">Configure SMTP settings</button>
      <button id="test-email" class="secondary" type="button">Send test email</button>
    </div>
    <p class="hint">The test uses the current image and event when available; otherwise it sends a generic SMTP test message. It never generates an image.</p>
    <label for="email-entity">Existing Home Assistant notify entities</label>
    <select id="email-entity" multiple size="4"></select>
    <label for="email-camera-entity">Image attachment camera entity</label>
    <input id="email-camera-entity" type="text" placeholder="camera.moment_in_history">
    <label for="email-recipient">Add an SMTP email recipient</label>
    <div class="recipient-row">
      <input id="email-recipient" type="text" inputmode="email" autocomplete="email" placeholder="name@example.com — press Space or Enter">
      <button id="add-email" type="button">Add email</button>
    </div>
    <div class="pills" id="managed-recipients" aria-live="polite"></div>
    <p class="hint">Added addresses are created through Home Assistant’s SMTP integration. Removing a pill removes only the SMTP recipient created through this page. Select Save configuration to apply delivery targets.</p>
  </section>

  <button class="secondary" id="refresh" type="button">Refresh now</button>
  <button id="save" type="button">Save configuration</button>
  <div class="status" id="status" role="status"></div>
  <div id="errors"></div>
</main>
<dialog id="smtp-dialog" aria-labelledby="smtp-dialog-title">
  <form id="smtp-form">
    <h2 id="smtp-dialog-title">Configure Home Assistant SMTP</h2>
    <p class="hint">These credentials are sent to Home Assistant's SMTP config flow and are not saved by this app.</p>
    <label for="smtp-sender">Sender email</label>
    <input id="smtp-sender" type="email" autocomplete="email" required placeholder="sender@example.com">
    <label for="smtp-sender-name">Sender name</label>
    <input id="smtp-sender-name" type="text" autocomplete="name" placeholder="Moment in history">
    <label for="smtp-server">SMTP server host</label>
    <input id="smtp-server" type="text" required placeholder="smtp.example.com">
    <label for="smtp-port">SMTP port</label>
    <input id="smtp-port" type="number" min="1" max="65535" value="587" required>
    <label for="smtp-encryption">Connection security</label>
    <select id="smtp-encryption">
      <option value="starttls" selected>STARTTLS (recommended, usually port 587)</option>
      <option value="tls">TLS (usually port 465)</option>
      <option value="none">None (only when explicitly supported by the provider)</option>
    </select>
    <label for="smtp-username">Username</label>
    <input id="smtp-username" type="text" autocomplete="username" placeholder="Usually the sender email">
    <label for="smtp-password">Password or app-specific password</label>
    <input id="smtp-password" type="password" autocomplete="current-password">
    <label class="toggle"><input id="smtp-verify-ssl" type="checkbox" checked> Verify the SSL certificate</label>
    <label for="smtp-recipient">First recipient email</label>
    <input id="smtp-recipient" type="email" autocomplete="email" required placeholder="recipient@example.com">
    <div class="status" id="smtp-status" role="status"></div>
    <menu>
      <button id="smtp-test-connection" class="secondary" type="button">Test connection</button>
      <button id="smtp-cancel" class="secondary" type="button">Cancel</button>
      <button id="smtp-submit" type="submit">Configure SMTP and add recipient</button>
    </menu>
  </form>
</dialog>
<dialog id="test-email-dialog" aria-labelledby="test-email-dialog-title">
  <form id="test-email-form">
    <h2 id="test-email-dialog-title">Send test email</h2>
    <p class="hint">Choose where this one test email should go. This does not change the saved daily delivery recipients.</p>
    <label for="test-email-entities">Existing Home Assistant notify entities</label>
    <select id="test-email-entities" multiple size="5"></select>
    <label for="test-email-recipient">Or add an SMTP email address for this test</label>
    <input id="test-email-recipient" type="email" autocomplete="email" placeholder="name@example.com">
    <div class="status" id="test-email-status" role="status"></div>
    <menu>
      <button id="test-email-cancel" class="secondary" type="button">Cancel</button>
      <button id="test-email-send" type="submit">Send test email</button>
    </menu>
  </form>
</dialog>
<script>
(function () {
  const fields = [
    { id: 'primary-model', config: 'text_model', result: 'primary_ollama', label: 'Saved value' },
    { id: 'fallback-model', config: 'fallback_text_model', result: 'fallback_ollama', label: 'Saved value' },
    { id: 'openai-model', config: 'openai_image_model', result: 'openai_image', label: 'Saved value' }
  ];
  const status = document.getElementById('status');
  const errors = document.getElementById('errors');
  const emailRecipient = document.getElementById('email-recipient');
  const emailEnabled = document.getElementById('email-enabled');
  const emailEntity = document.getElementById('email-entity');
  const smtpDialog = document.getElementById('smtp-dialog');
  const smtpForm = document.getElementById('smtp-form');
  const smtpStatus = document.getElementById('smtp-status');
  const testEmailDialog = document.getElementById('test-email-dialog');
  const testEmailForm = document.getElementById('test-email-form');
  const testEmailEntities = document.getElementById('test-email-entities');
  const testEmailRecipient = document.getElementById('test-email-recipient');
  const testEmailStatus = document.getElementById('test-email-status');
  const dirtyFields = new Set();
  let emailEnableRequested = false;
  function apiPath(path) {
    let base = window.location.pathname;
    if (!base.endsWith('/')) base += '/';
    return base + path;
  }
  function fill(field, current, models, disabled) {
    const select = document.getElementById(field.id);
    const currentValues = dirtyFields.has(field.id)
      ? Array.from(select.selectedOptions).map(function (option) { return option.value; }).filter(Boolean)
      : (field.multiple ? (current || '').split(',').map(function (value) { return value.trim(); }).filter(Boolean) : [current]);
    select.replaceChildren();
    currentValues.forEach(function (value) {
      if (!models.includes(value)) select.add(new Option(value + ' (' + field.label + '; not currently advertised)', value));
    });
    models.forEach(function (model) { select.add(new Option(model, model)); });
    Array.from(select.options).forEach(function (option) { option.selected = currentValues.includes(option.value); });
    if (!select.options.length) {
      select.add(new Option(field.allowEmpty ? 'Email delivery disabled' : 'No models discovered', ''));
    }
    select.disabled = disabled;
  }
  function renderTestEmailEntities() {
    const selected = new Set(Array.from(emailEntity.selectedOptions).map(function (option) { return option.value; }).filter(Boolean));
    testEmailEntities.replaceChildren();
    Array.from(emailEntity.options).forEach(function (option) {
      if (!option.value) return;
      testEmailEntities.add(new Option(option.textContent, option.value, false, selected.has(option.value)));
    });
    if (!testEmailEntities.options.length) {
      const option = new Option('No notify entities discovered — add an SMTP address below', '');
      option.disabled = true;
      testEmailEntities.add(option);
    }
  }
  function setValue(id, value) {
    if (dirtyFields.has(id)) return;
    document.getElementById(id).value = value || '';
  }
  function setDatalist(id, values) {
    const datalist = document.getElementById(id);
    datalist.replaceChildren();
    values.forEach(function (value) { datalist.appendChild(new Option(value, value)); });
  }
  function render(data) {
    const config = data.config || {};
    const models = data.models || {};
    setValue('primary-url-input', config.primary_ollama_url);
    setValue('fallback-url-input', config.fallback_ollama_url);
    fill(fields[0], config.text_model, models.primary_ollama || [], false);
    fill(fields[1], config.fallback_text_model, models.fallback_ollama || [], false);
    fill(fields[2], config.openai_image_model, models.openai_image || [], !config.openai_key_present);
    setValue('image-provider', config.image_provider || 'openai');
    setValue('openai-quality', config.openai_image_quality || 'high');
    setValue('image-model', config.image_model);
    setValue('image-backend-url', config.image_backend_url);
    setValue('image-backend-fallback-url', config.image_backend_fallback_url);
    setValue('comfyui-model', config.comfyui_model);
    const imageBackendModels = models.image_backend || [];
    setDatalist('comfyui-model-options', imageBackendModels);
    if (!config.comfyui_model && imageBackendModels.length) setValue('comfyui-model', imageBackendModels[0]);
    setValue('schedule-time', config.schedule_time || '00:05');
    setValue('email-camera-entity', config.email_camera_entity || 'camera.moment_in_history');
    const emailSelect = document.getElementById('email-entity');
    const email = data.email || {};
    const smtpButton = document.getElementById('edit-smtp');
    smtpButton.hidden = false;
    smtpButton.textContent = email.smtp_configured === true ? 'Edit SMTP settings' : 'Configure SMTP settings';
    fill({ id: 'email-entity', multiple: true, allowEmpty: true, label: 'Saved value' }, config.email_notify_entity, email.notify_entities || [], false);
    emailSelect.setAttribute('aria-label', 'Existing Home Assistant notify entities');
    renderTestEmailEntities();
    if (!emailEnableRequested && !dirtyFields.has('email-enabled')) emailEnabled.checked = Boolean(config.email_notify_entity);
    const managed = email.managed_recipients || [];
    const pills = document.getElementById('managed-recipients');
    pills.replaceChildren();
    managed.forEach(function (recipient) {
      const pill = document.createElement('span');
      pill.className = 'pill';
      const label = document.createElement('span');
      label.textContent = recipient.email + (recipient.entity_id ? ' → ' + recipient.entity_id : ' (waiting for Home Assistant)');
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = '×';
      remove.title = 'Remove ' + recipient.email;
      remove.addEventListener('click', function () { removeEmail(recipient.email); });
      pill.append(label, remove);
      pills.appendChild(pill);
    });
    document.getElementById('openai-status').textContent = config.openai_key_present
      ? 'OpenAI image models visible to the saved API key are listed below. Enter a replacement key above only when needed.'
      : 'No OpenAI API key is saved. Add one above to discover OpenAI image models.';
    document.getElementById('email-status').textContent = email.smtp_configured === false
      ? 'Home Assistant SMTP is not configured. Enable email or choose Add email to configure it now.'
      : (email.management_available
        ? (email.notify_entities && email.notify_entities.length
          ? 'Choose one or more notify entities, or add an SMTP email address below.'
          : 'SMTP is configured, but no recipient is available yet. Add an SMTP email address below.')
        : (email.management_message || 'Home Assistant SMTP status is unavailable; choose an existing notify entity instead.'));
    errors.replaceChildren();
    Object.keys(data.errors || {}).forEach(function (key) {
      const item = document.createElement('div');
      item.className = 'error';
      item.textContent = key + ': ' + data.errors[key];
      errors.appendChild(item);
    });
  }
  let refreshInFlight = null;
  function discoveryUrl() {
    const query = new URLSearchParams();
    [
      ['primary_ollama_url', 'primary-url-input'],
      ['fallback_ollama_url', 'fallback-url-input'],
      ['image_backend_url', 'image-backend-url'],
      ['image_backend_fallback_url', 'image-backend-fallback-url']
    ].forEach(function (pair) {
      const value = document.getElementById(pair[1]).value.trim();
      if (value) query.set(pair[0], value);
    });
    const suffix = query.toString();
    return apiPath('api/models') + (suffix ? '?' + suffix : '');
  }
  async function refresh(options) {
    const silent = Boolean(options && options.silent);
    if (refreshInFlight) return refreshInFlight;
    refreshInFlight = (async function () {
      if (!silent) status.textContent = 'Querying configured endpoints…';
      try {
        const response = await fetch(discoveryUrl(), { cache: 'no-store' });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'model discovery failed');
        render(data);
        if (!silent) status.textContent = 'Model lists refreshed.';
        return data;
      } catch (error) {
        if (!silent) status.textContent = 'Unable to query models: ' + error.message;
      } finally {
        refreshInFlight = null;
      }
    }());
    return refreshInFlight;
  }
  async function manageEmail(action, email) {
    const response = await fetch(apiPath('api/email-recipient'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: action, email: email })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'email recipient update failed');
    return data;
  }
  async function configureSmtp(settings) {
    const response = await fetch(apiPath('api/smtp'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'SMTP setup failed');
    return data;
  }
  function smtpSettings(includeRecipient) {
    const settings = {
      sender: document.getElementById('smtp-sender').value.trim(),
      sender_name: document.getElementById('smtp-sender-name').value.trim(),
      server: document.getElementById('smtp-server').value.trim(),
      port: document.getElementById('smtp-port').value,
      encryption: document.getElementById('smtp-encryption').value,
      username: document.getElementById('smtp-username').value.trim(),
      password: document.getElementById('smtp-password').value,
      verify_ssl: document.getElementById('smtp-verify-ssl').checked
    };
    if (includeRecipient) settings.recipient = document.getElementById('smtp-recipient').value.trim();
    return settings;
  }
  function setSmtpDialogMode(mode) {
    const updating = mode === 'update';
    smtpForm.dataset.mode = mode;
    document.getElementById('smtp-dialog-title').textContent = updating
      ? 'Update Home Assistant SMTP'
      : 'Configure Home Assistant SMTP';
    document.getElementById('smtp-submit').textContent = updating
      ? 'Update SMTP settings'
      : 'Configure SMTP and add recipient';
    document.getElementById('smtp-recipient').required = !updating;
    document.querySelector('label[for="smtp-recipient"]').hidden = updating;
    document.getElementById('smtp-recipient').hidden = updating;
    document.getElementById('smtp-password').placeholder = updating
      ? 'Enter the password again if authentication is required'
      : '';
  }
  function openSmtpDialog(recipient) {
    setSmtpDialogMode('configure');
    document.getElementById('smtp-recipient').value = recipient || emailRecipient.value.trim();
    smtpStatus.textContent = '';
    showSmtpDialog();
  }
  function showSmtpDialog() {
    if (typeof smtpDialog.showModal === 'function') {
      smtpDialog.showModal();
    } else {
      smtpDialog.setAttribute('open', 'open');
    }
  }
  function resetSmtpForm() {
    smtpForm.reset();
    document.getElementById('smtp-port').value = '587';
    document.getElementById('smtp-verify-ssl').checked = true;
  }
  async function editSmtp() {
    resetSmtpForm();
    setSmtpDialogMode('configure');
    smtpStatus.textContent = 'Loading Home Assistant SMTP settings…';
    showSmtpDialog();
    try {
      const response = await fetch(apiPath('api/smtp'), { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'SMTP settings could not be loaded');
      if (data.configured !== true) {
        smtpStatus.textContent = 'Enter the SMTP details Home Assistant should use.';
        return;
      }
      setSmtpDialogMode('update');
      document.getElementById('smtp-sender').value = data.sender || '';
      document.getElementById('smtp-sender-name').value = data.sender_name || '';
      document.getElementById('smtp-server').value = data.server || '';
      document.getElementById('smtp-port').value = data.port || '587';
      document.getElementById('smtp-encryption').value = data.encryption || 'starttls';
      document.getElementById('smtp-username').value = data.username || '';
      document.getElementById('smtp-password').value = '';
      document.getElementById('smtp-verify-ssl').checked = data.verify_ssl !== false;
      document.getElementById('smtp-recipient').value = '';
      smtpStatus.textContent = 'Enter the SMTP password again if your server requires authentication.';
    } catch (error) {
      smtpStatus.textContent = 'Unable to load SMTP settings: ' + error.message;
    }
  }
  function showTestEmailDialog() {
    renderTestEmailEntities();
    testEmailRecipient.value = '';
    testEmailStatus.textContent = testEmailEntities.options.length && !testEmailEntities.options[0].disabled
      ? 'Select one or more recipients, or enter a new SMTP email address below.'
      : 'No existing notify entity is available. Enter an SMTP email address below.';
    if (typeof testEmailDialog.showModal === 'function') testEmailDialog.showModal();
    else testEmailDialog.setAttribute('open', 'open');
  }
  function closeTestEmailDialog() {
    if (typeof testEmailDialog.close === 'function') testEmailDialog.close();
    else testEmailDialog.removeAttribute('open');
  }
  async function testEmail() {
    const button = document.getElementById('test-email');
    button.disabled = true;
    status.textContent = 'Checking SMTP configuration…';
    try {
      const smtpResponse = await fetch(apiPath('api/smtp'), { cache: 'no-store' });
      const smtp = await smtpResponse.json();
      if (!smtpResponse.ok) throw new Error(smtp.error || 'SMTP settings could not be checked');
      if (smtp.configured !== true) {
        status.textContent = 'Configure SMTP settings first.';
        await editSmtp();
        return;
      }
      showTestEmailDialog();
    } catch (error) {
      status.textContent = 'Unable to prepare test email: ' + error.message;
    } finally {
      button.disabled = false;
    }
  }
  async function sendTestEmail(event) {
    event.preventDefault();
    const button = document.getElementById('test-email-send');
    button.disabled = true;
    testEmailStatus.textContent = 'Preparing test email…';
    try {
      const targets = Array.from(testEmailEntities.selectedOptions)
        .map(function (option) { return option.value; })
        .filter(Boolean);
      const recipient = testEmailRecipient.value.trim();
      if (recipient) {
        testEmailStatus.textContent = 'Adding the SMTP recipient in Home Assistant…';
        const result = await manageEmail('add', recipient);
        const entityId = result.recipient && result.recipient.entity_id;
        if (!entityId) throw new Error('Home Assistant did not make a notify entity available for that address');
        targets.push(entityId);
      }
      if (!targets.length) throw new Error('Choose at least one notify entity or enter an SMTP email address');
      testEmailStatus.textContent = 'Sending SMTP test email…';
      const response = await fetch(apiPath('api/test-email'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entity_ids: Array.from(new Set(targets)) })
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'test email failed');
      closeTestEmailDialog();
      await refresh({ silent: true });
      status.textContent = data.mode === 'current'
        ? 'Test email accepted by Home Assistant SMTP with the current image and event. Provider delivery is not confirmed. Daily delivery settings were not changed.'
        : 'Generic test email accepted by Home Assistant SMTP. Provider delivery is not confirmed. Daily delivery settings were not changed.';
    } catch (error) {
      testEmailStatus.textContent = 'Unable to send test email: ' + error.message;
    } finally {
      button.disabled = false;
    }
  }
  async function testSmtpConnection() {
    const button = document.getElementById('smtp-test-connection');
    button.disabled = true;
    smtpStatus.textContent = 'Testing SMTP connection and authentication…';
    try {
      const settings = smtpSettings(false);
      if (smtpForm.dataset.mode === 'update' && !settings.password) settings.use_saved_credentials = true;
      const response = await fetch(apiPath('api/smtp-test'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(settings)
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'SMTP connection test failed');
      smtpStatus.textContent = data.authenticated
        ? 'SMTP connection and authentication succeeded. Nothing was saved or sent.'
        : 'SMTP connection succeeded. No authentication was attempted; nothing was saved or sent.';
    } catch (error) {
      smtpStatus.textContent = 'SMTP connection test failed: ' + error.message;
    } finally {
      button.disabled = false;
    }
  }
  function closeSmtpDialog() {
    if (typeof smtpDialog.close === 'function') smtpDialog.close();
    else smtpDialog.removeAttribute('open');
  }
  async function addEmail() {
    const email = emailRecipient.value.trim();
    if (!email) return;
    try {
      const current = await refresh();
      if (current && current.email && current.email.smtp_configured === false) {
        openSmtpDialog(email);
        return;
      }
      status.textContent = 'Adding the SMTP recipient through Home Assistant…';
      const result = await manageEmail('add', email);
      emailRecipient.value = '';
      await refresh();
      emailEnabled.checked = true;
      const entityId = result.recipient && result.recipient.entity_id;
      if (entityId) {
        const select = document.getElementById('email-entity');
        Array.from(select.options).forEach(function (option) { if (option.value === entityId) option.selected = true; });
      }
      status.textContent = 'Recipient added. Select Save configuration to enable delivery to it.';
    } catch (error) {
      status.textContent = 'Unable to add recipient: ' + error.message;
    }
  }
  async function removeEmail(email) {
    if (!window.confirm('Remove the SMTP recipient ' + email + '?')) return;
    status.textContent = 'Removing the SMTP recipient from Home Assistant…';
    try {
      await manageEmail('remove', email);
      await refresh();
      status.textContent = 'Recipient removed. Select Save configuration to update delivery targets.';
    } catch (error) {
      status.textContent = 'Unable to remove recipient: ' + error.message;
    }
  }
  async function save() {
    status.textContent = 'Saving configuration…';
    const values = {};
    values.primary_ollama_url = document.getElementById('primary-url-input').value.trim();
    values.fallback_ollama_url = document.getElementById('fallback-url-input').value.trim();
    fields.forEach(function (field) {
      const select = document.getElementById(field.id);
      values[field.config] = field.multiple
        ? Array.from(select.selectedOptions).map(function (option) { return option.value; }).filter(Boolean).join(',')
        : select.value;
    });
    values.image_provider = document.getElementById('image-provider').value;
    values.openai_image_quality = document.getElementById('openai-quality').value;
    values.image_model = document.getElementById('image-model').value.trim();
    values.image_backend_url = document.getElementById('image-backend-url').value.trim();
    values.image_backend_fallback_url = document.getElementById('image-backend-fallback-url').value.trim();
    values.comfyui_model = document.getElementById('comfyui-model').value.trim();
    values.email_camera_entity = document.getElementById('email-camera-entity')
      ? document.getElementById('email-camera-entity').value.trim()
      : '';
    values.schedule_time = document.getElementById('schedule-time').value;
    const openaiKey = document.getElementById('openai-key').value;
    if (openaiKey) values.openai_api_key = openaiKey;
    values.clear_openai_key = document.getElementById('clear-openai-key').checked;
    if (!emailEnabled.checked) values.email_notify_entity = '';
    try {
      const response = await fetch(apiPath('api/config'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(values)
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'save failed');
      status.textContent = 'Saved. Home Assistant is restarting the App to apply the configuration.';
    } catch (error) {
      status.textContent = 'Unable to save: ' + error.message;
    }
  }
  document.getElementById('refresh').addEventListener('click', refresh);
  document.getElementById('save').addEventListener('click', save);
  document.getElementById('edit-smtp').addEventListener('click', editSmtp);
  document.getElementById('test-email').addEventListener('click', testEmail);
  testEmailForm.addEventListener('submit', sendTestEmail);
  document.getElementById('test-email-cancel').addEventListener('click', closeTestEmailDialog);
  document.getElementById('smtp-test-connection').addEventListener('click', testSmtpConnection);
  ['primary-url-input', 'fallback-url-input', 'image-provider', 'openai-quality', 'openai-key',
    'clear-openai-key', 'openai-model', 'image-model', 'image-backend-url',
    'image-backend-fallback-url', 'comfyui-model', 'schedule-time', 'email-enabled',
    'email-entity', 'email-camera-entity'].forEach(function (id) {
      const element = document.getElementById(id);
      if (!element) return;
      ['input', 'change'].forEach(function (eventName) {
        element.addEventListener(eventName, function () { dirtyFields.add(id); });
      });
    });
  ['primary-url-input', 'fallback-url-input', 'image-backend-url', 'image-backend-fallback-url'].forEach(function (id) {
    let timer = null;
    document.getElementById(id).addEventListener('input', function () {
      window.clearTimeout(timer);
      timer = window.setTimeout(function () { refresh({ silent: true }); }, 500);
    });
  });
  document.getElementById('add-email').addEventListener('click', addEmail);
  emailEnabled.addEventListener('change', async function () {
    if (!emailEnabled.checked) {
      emailEnableRequested = false;
      status.textContent = 'Email delivery will be disabled when you save.';
      return;
    }
    emailEnableRequested = true;
    const current = await refresh();
    if (!current) {
      emailEnableRequested = false;
      emailEnabled.checked = false;
    } else if (current.email && current.email.smtp_configured === false) {
      openSmtpDialog('');
    } else {
      emailEnableRequested = false;
      emailEnabled.checked = true;
    }
  });
  emailEntity.addEventListener('change', function () {
    if (emailEntity.selectedOptions.length) emailEnabled.checked = true;
  });
  document.getElementById('smtp-cancel').addEventListener('click', function () {
    emailEnableRequested = false;
    emailEnabled.checked = false;
    closeSmtpDialog();
  });
  smtpForm.addEventListener('submit', async function (event) {
    event.preventDefault();
    const updating = smtpForm.dataset.mode === 'update';
    smtpStatus.textContent = updating ? 'Updating Home Assistant SMTP…' : 'Configuring Home Assistant SMTP…';
    const settings = smtpSettings(!updating);
    if (updating) {
      settings.action = 'update';
      delete settings.recipient;
    }
    try {
      const result = await configureSmtp(settings);
      const entityId = result.recipient && result.recipient.entity_id;
      smtpForm.reset();
      document.getElementById('smtp-port').value = '587';
      document.getElementById('smtp-verify-ssl').checked = true;
      closeSmtpDialog();
      setSmtpDialogMode('configure');
      emailRecipient.value = '';
      const current = await refresh();
      emailEnableRequested = false;
      emailEnabled.checked = true;
      if (entityId) {
        if (!Array.from(emailEntity.options).some(function (option) { return option.value === entityId; })) {
          emailEntity.add(new Option(entityId, entityId));
        }
        Array.from(emailEntity.options).forEach(function (option) { if (option.value === entityId) option.selected = true; });
      }
      status.textContent = updating
        ? 'SMTP settings updated. Existing recipients were preserved.'
        : (current ? 'SMTP configured and recipient added. Select Save configuration to enable delivery.' : 'SMTP configured and recipient added.');
    } catch (error) {
      smtpStatus.textContent = 'Unable to configure SMTP: ' + error.message;
    }
  });
  emailRecipient.addEventListener('keydown', function (event) {
    if (event.key === ' ' || event.key === 'Enter' || event.key === ',') {
      event.preventDefault();
      addEmail();
    }
  });
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) refresh({ silent: true });
  });
  window.addEventListener('focus', function () { refresh({ silent: true }); });
  window.setInterval(function () {
    if (!document.hidden) refresh({ silent: true });
  }, 30000);
  refresh();
}());
</script>
</body>
</html>
"""


def start_server(
    *,
    port: int,
    get_options: Callable[[], dict[str, Any]],
    save_options: Callable[[dict[str, str]], None],
    log: Callable[[str], None],
    get_notify_entities: Callable[[], list[str]] | None = None,
    get_managed_recipients: Callable[[], dict[str, Any]] | None = None,
    add_email_recipient: Callable[[str], dict[str, Any]] | None = None,
    remove_email_recipient: Callable[[str], None] | None = None,
    configure_smtp: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    smtp_details: Callable[[], dict[str, Any]] | None = None,
    update_smtp: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    test_email: Callable[..., dict[str, Any]] | None = None,
    test_smtp_connection: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> ThreadingHTTPServer:
    """Start the authenticated-by-Ingress configuration server."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "MomentInHistoryConfig/1.8"

        def _allowed_client(self) -> bool:
            # Home Assistant Ingress uses 172.30.32.2. Localhost remains useful
            # for deterministic smoke tests and does not expose a host port.
            return self.client_address[0] in {"172.30.32.2", "127.0.0.1", "::1"}

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            self._send(status, "application/json; charset=utf-8", json.dumps(payload).encode("utf-8"))

        def _not_found(self) -> None:
            self._json(404, {"error": "not found"})

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if not self._allowed_client():
                self._json(403, {"error": "Ingress access required"})
                return
            path = urlsplit(self.path).path
            if path in {"/", "/index.html"}:
                self._send(200, "text/html; charset=utf-8", _page().encode("utf-8"))
                return
            if path == "/api/smtp":
                try:
                    if not smtp_details:
                        raise ValueError("SMTP settings are unavailable")
                    self._json(200, smtp_details())
                except Exception as exc:  # noqa: BLE001 - return a safe diagnostic
                    self._json(502, {"error": _safe_error(exc)})
                return
            if path == "/api/models":
                try:
                    options = dict(get_options())
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    for field in (*ENDPOINT_FIELDS, *IMAGE_ENDPOINT_FIELDS):
                        if field in query:
                            options[field] = query[field][0].strip()
                    result = discover_models(options)
                    result["email"] = {
                        "notify_entities": [],
                        "managed_recipients": [],
                        "management_available": bool(get_managed_recipients),
                        "smtp_configured": None,
                    }
                    if get_notify_entities:
                        try:
                            result["email"]["notify_entities"] = get_notify_entities()
                        except Exception as exc:  # noqa: BLE001 - show a safe UI diagnostic
                            result["errors"]["email"] = _safe_error(exc)
                    if get_managed_recipients:
                        try:
                            managed = get_managed_recipients()
                            result["email"].update(
                                {
                                    "managed_recipients": managed.get("recipients", []),
                                    "management_available": bool(managed.get("available")),
                                    "smtp_configured": managed.get("smtp_configured", managed.get("available")),
                                    "management_message": str(managed.get("message", "")),
                                }
                            )
                        except Exception as exc:  # noqa: BLE001 - show a safe UI diagnostic
                            result["email"]["management_available"] = False
                            result["email"]["smtp_configured"] = None
                            result["email"]["management_message"] = "Automatic SMTP recipient management is unavailable."
                            result["errors"]["email_management"] = _safe_error(exc)
                    self._json(200, result)
                except Exception as exc:  # noqa: BLE001 - return a safe diagnostic
                    log(f"Configuration UI model discovery failed: {_safe_error(exc)}")
                    self._json(500, {"error": "model discovery failed"})
                return
            self._not_found()

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if not self._allowed_client():
                self._json(403, {"error": "Ingress access required"})
                return
            path = urlsplit(self.path).path
            if path not in {"/api/config", "/api/email-recipient", "/api/smtp", "/api/smtp-test", "/api/test-email"}:
                self._not_found()
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_REQUEST_BYTES:
                    raise ValueError("invalid request size")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("request must be an object")
                if path == "/api/test-email":
                    if not test_email:
                        raise ValueError("SMTP test email is unavailable")
                    target_entities = payload.get("entity_ids")
                    if target_entities is None:
                        result = test_email()
                    else:
                        if not isinstance(target_entities, list) or not target_entities:
                            raise ValueError("Choose at least one notify.* test recipient")
                        entities = [str(value).strip() for value in target_entities]
                        if any(not value.startswith("notify.") for value in entities if value):
                            raise ValueError("Test email targets must be Home Assistant notify.* entities")
                        if any(not value for value in entities):
                            raise ValueError("Choose at least one notify.* test recipient")
                        result = test_email(entities)
                    self._json(200, result if isinstance(result, dict) else {"mode": "generic"})
                    return
                if path == "/api/smtp-test":
                    if not test_smtp_connection:
                        raise ValueError("SMTP connection test is unavailable")
                    configuration = {
                        key: payload.get(key)
                        for key in (
                            "sender",
                            "sender_name",
                            "server",
                            "port",
                            "encryption",
                            "username",
                            "password",
                            "verify_ssl",
                        )
                    }
                    if payload.get("use_saved_credentials") is True:
                        configuration["use_saved_credentials"] = True
                    self._json(200, test_smtp_connection(configuration))
                    return
                if path == "/api/smtp":
                    configuration = {
                        key: payload.get(key)
                        for key in (
                            "sender",
                            "sender_name",
                            "server",
                            "port",
                            "encryption",
                            "username",
                            "password",
                            "verify_ssl",
                            "recipient",
                        )
                    }
                    if str(payload.get("action", "")).strip().lower() == "update":
                        if not update_smtp:
                            raise ValueError("SMTP update is unavailable")
                        self._json(200, {"settings": update_smtp(configuration)})
                    else:
                        if not configure_smtp:
                            raise ValueError("SMTP setup is unavailable")
                        result = configure_smtp(configuration)
                        self._json(200, {"recipient": result})
                    return
                if path == "/api/email-recipient":
                    action = str(payload.get("action", "")).strip().lower()
                    email = str(payload.get("email", "")).strip()
                    if action == "add" and add_email_recipient:
                        recipient = add_email_recipient(email)
                        self._json(200, {"recipient": recipient})
                        return
                    if action == "remove" and remove_email_recipient:
                        remove_email_recipient(email)
                        self._json(200, {"removed": email})
                        return
                    raise ValueError("automatic SMTP recipient management is unavailable")
                changes: dict[str, str] = {}
                for field in SELECTABLE_FIELDS:
                    if field not in payload:
                        continue
                    value = str(payload[field]).strip()
                    if value or field in URL_FIELDS or field in {"email_notify_entity", "email_camera_entity", "schedule_time"}:
                        changes[field] = value
                    elif field == "openai_api_key":
                        # A blank key is meaningful only when the user explicitly
                        # selected the clear-key control in the Web UI.
                        continue
                if payload.get("clear_openai_key") is True:
                    changes["openai_api_key"] = ""
                if not changes:
                    raise ValueError("no model selection was supplied")
                if any(len(value) > 256 or any(ord(char) < 32 for char in value) for value in changes.values()):
                    raise ValueError("invalid model selection")
                if "image_provider" in changes and changes["image_provider"] not in {"openai", "lan", "comfyui"}:
                    raise ValueError("image_provider must be openai, lan, or comfyui")
                if "openai_image_quality" in changes and changes["openai_image_quality"] not in {"low", "medium", "high"}:
                    raise ValueError("openai_image_quality must be low, medium, or high")
                if "schedule_time" in changes and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", changes["schedule_time"]):
                    raise ValueError("schedule_time must use HH:MM")
                for field in URL_FIELDS:
                    if field not in changes or not changes[field]:
                        continue
                    parsed = urlsplit(changes[field])
                    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                        raise ValueError(f"{field} must be an HTTP(S) URL without embedded credentials")
                save_options(changes)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001 - do not expose Supervisor details
                safe_error = _safe_error(exc)
                log(f"Configuration UI save failed: {safe_error}")
                if path in {"/api/smtp", "/api/email-recipient", "/api/smtp-test", "/api/test-email"}:
                    self._json(502, {"error": safe_error})
                else:
                    self._json(502, {"error": "Home Assistant rejected the configuration update"})
                return
            self._json(200, {"saved": sorted(changes), "restart_required": True})

        def log_message(self, format: str, *args: object) -> None:
            # Access logs are intentionally quiet; operational failures use log().
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    import threading

    thread = threading.Thread(target=server.serve_forever, name="moment-config-ui", daemon=True)
    thread.start()
    return server
