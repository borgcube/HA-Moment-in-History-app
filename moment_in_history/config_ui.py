"""Small Home Assistant Ingress UI for discovering and selecting model IDs."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
MODEL_FIELDS = ("text_model", "fallback_text_model", "openai_image_model")
MAX_REQUEST_BYTES = 16 * 1024


def _request_json(url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "BORGcube-MomentInHistory/1.7",
            **(headers or {}),
        },
    )
    with urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("response was not an object")
    return payload


def _safe_error(exc: Exception) -> str:
    """Return a short error that cannot expose credentials or response bodies."""
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, (URLError, TimeoutError)):
        return "network or timeout error"
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
    families = details.get("families", []) if isinstance(details, dict) else []
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
    model_ids = {
        str(item.get("id", "")).strip()
        for item in payload.get("data", [])
        if isinstance(item, dict) and str(item.get("id", "")).strip()
    }
    return sorted(
        (model_id for model_id in model_ids if _is_image_model(model_id)),
        key=lambda value: value.casefold(),
    )


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
            "openai_image_model": str(options.get("openai_image_model", "")),
            "image_provider": str(options.get("image_provider", "openai")),
            "openai_key_present": bool(api_key),
        },
        "models": {
            "primary_ollama": [],
            "fallback_ollama": [],
            "openai_image": [],
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

    return result


def _page() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Moment in history — model selection</title>
  <style>
    :root { color-scheme: dark; font-family: system-ui, sans-serif; }
    body { margin: 0; background: #111827; color: #f3f4f6; }
    main { max-width: 760px; margin: auto; padding: 24px; }
    section { background: #1f2937; border-radius: 12px; padding: 18px; margin: 16px 0; }
    h1 { margin-top: 0; font-size: 1.55rem; }
    h2 { font-size: 1.1rem; margin: 0 0 8px; }
    p, label { color: #d1d5db; }
    label { display: block; margin: 14px 0 6px; }
    select, button { box-sizing: border-box; width: 100%; padding: 10px; border-radius: 8px; border: 1px solid #4b5563; font: inherit; }
    select { background: #111827; color: #f9fafb; }
    button { background: #d9a441; border-color: #d9a441; color: #111827; cursor: pointer; font-weight: 700; margin-top: 10px; }
    button.secondary { background: transparent; color: #f9fafb; }
    button:disabled, select:disabled { opacity: .55; cursor: not-allowed; }
    .url, .status, .error { overflow-wrap: anywhere; font-size: .9rem; }
    .url { color: #9ca3af; }
    .status { min-height: 1.4em; margin-top: 12px; }
    .error { color: #fca5a5; margin: 8px 0; }
    .hint { color: #9ca3af; font-size: .9rem; }
  </style>
</head>
<body>
<main>
  <h1>Moment in history — model selection</h1>
  <p>Refresh the lists to read the models currently advertised by each endpoint. Saving a selection updates the App options through Home Assistant and restarts the App.</p>

  <section>
    <h2>Ollama</h2>
    <div class="url" id="primary-url"></div>
    <label for="primary-model">Primary Ollama model</label>
    <select id="primary-model"></select>
    <div class="url" id="fallback-url"></div>
    <label for="fallback-model">Fallback Ollama model</label>
    <select id="fallback-model"></select>
  </section>

  <section>
    <h2>OpenAI Images</h2>
    <p class="hint" id="openai-status">The list is queried only when an OpenAI API key is configured.</p>
    <label for="openai-model">OpenAI image model</label>
    <select id="openai-model"></select>
  </section>

  <button class="secondary" id="refresh" type="button">Refresh available models</button>
  <button id="save" type="button">Save model selection</button>
  <div class="status" id="status" role="status"></div>
  <div id="errors"></div>
</main>
<script>
(function () {
  const fields = [
    { id: 'primary-model', config: 'text_model', result: 'primary_ollama', label: 'Saved value' },
    { id: 'fallback-model', config: 'fallback_text_model', result: 'fallback_ollama', label: 'Saved value' },
    { id: 'openai-model', config: 'openai_image_model', result: 'openai_image', label: 'Saved value' }
  ];
  const status = document.getElementById('status');
  const errors = document.getElementById('errors');
  function apiPath(path) {
    let base = window.location.pathname;
    if (!base.endsWith('/')) base += '/';
    return base + path;
  }
  function fill(field, current, models, disabled) {
    const select = document.getElementById(field.id);
    select.replaceChildren();
    if (current && !models.includes(current)) {
      const saved = new Option(current + ' (' + field.label + '; not currently advertised)', current);
      select.add(saved);
    }
    models.forEach(function (model) { select.add(new Option(model, model)); });
    if (current) select.value = current;
    if (!select.options.length) select.add(new Option('No models discovered', ''));
    select.disabled = disabled;
  }
  function render(data) {
    const config = data.config || {};
    const models = data.models || {};
    document.getElementById('primary-url').textContent = 'Endpoint: ' + (config.primary_ollama_url || 'not configured');
    document.getElementById('fallback-url').textContent = 'Endpoint: ' + (config.fallback_ollama_url || 'not configured');
    fill(fields[0], config.text_model, models.primary_ollama || [], false);
    fill(fields[1], config.fallback_text_model, models.fallback_ollama || [], false);
    fill(fields[2], config.openai_image_model, models.openai_image || [], !config.openai_key_present);
    document.getElementById('openai-status').textContent = config.openai_key_present
      ? 'Image models visible to the configured API key are listed below.'
      : 'Enter an OpenAI API key in App Configuration first, then refresh this page.';
    errors.replaceChildren();
    Object.keys(data.errors || {}).forEach(function (key) {
      const item = document.createElement('div');
      item.className = 'error';
      item.textContent = key + ': ' + data.errors[key];
      errors.appendChild(item);
    });
  }
  async function refresh() {
    status.textContent = 'Querying configured endpoints…';
    try {
      const response = await fetch(apiPath('api/models'), { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'model discovery failed');
      render(data);
      status.textContent = 'Model lists refreshed.';
    } catch (error) {
      status.textContent = 'Unable to query models: ' + error.message;
    }
  }
  async function save() {
    status.textContent = 'Saving model selection…';
    const values = {};
    fields.forEach(function (field) { values[field.config] = document.getElementById(field.id).value; });
    try {
      const response = await fetch(apiPath('api/config'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(values)
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'save failed');
      status.textContent = 'Saved. Home Assistant is restarting the App to apply the selection.';
    } catch (error) {
      status.textContent = 'Unable to save: ' + error.message;
    }
  }
  document.getElementById('refresh').addEventListener('click', refresh);
  document.getElementById('save').addEventListener('click', save);
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
) -> ThreadingHTTPServer:
    """Start the authenticated-by-Ingress configuration server."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "MomentInHistoryConfig/1.7"

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
            if path == "/api/models":
                try:
                    self._json(200, discover_models(get_options()))
                except Exception as exc:  # noqa: BLE001 - return a safe diagnostic
                    log(f"Configuration UI model discovery failed: {_safe_error(exc)}")
                    self._json(500, {"error": "model discovery failed"})
                return
            self._not_found()

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if not self._allowed_client():
                self._json(403, {"error": "Ingress access required"})
                return
            if urlsplit(self.path).path != "/api/config":
                self._not_found()
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_REQUEST_BYTES:
                    raise ValueError("invalid request size")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("request must be an object")
                changes = {
                    field: str(payload[field]).strip()
                    for field in MODEL_FIELDS
                    if field in payload and str(payload[field]).strip()
                }
                if not changes:
                    raise ValueError("no model selection was supplied")
                if any(len(value) > 256 or any(ord(char) < 32 for char in value) for value in changes.values()):
                    raise ValueError("invalid model selection")
                save_options(changes)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001 - do not expose Supervisor details
                log(f"Configuration UI save failed: {_safe_error(exc)}")
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
