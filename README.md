# Moment in History for Home Assistant

Moment in History generates one historical scene every day for a Home Assistant dashboard. It selects the most consequential event from the day's **On This Day** events, describes the instant exactly ten seconds before it, and generates a dramatic high-contrast cartoon illustration. The image contains only the date; the event details are disclosed as readable plain text.

This repository is a Home Assistant App repository. The root `repository.yaml` is required by Supervisor; add the repository URL itself to **Settings → Apps → Install app → ⋮ → Repositories**.

The project is packaged as a Home Assistant App (formerly called an add-on). It keeps the orchestration and generated files on Home Assistant, uses Ollama for event selection and pre-event scene direction, and supports OpenAI Images or a LAN image service for the artwork.

## Features

- Britannica is attempted first for the daily event list, with Wikimedia's machine-readable On This Day feed as a fallback when Britannica is unavailable.
- Ollama ranks the complete event list and writes the one-sentence “ten seconds before” scene direction.
- OpenAI `gpt-image-2` is the default image backend, with optional LAN and ComfyUI backends.
- Two Ollama endpoints can be configured for text-generation failover.
- The date is composited after generation, so it is the only intentional text on the image.
- The disclosure card reads the current event metadata as plain text and avoids stale browser-cached details.

## Installation

### Home Assistant Apps repository — recommended

This is the normal installation path for Home Assistant OS and Home Assistant Supervised:

1. Open **Settings → Apps**.
2. Open the three-dot menu and choose **Repositories**.
3. Add this repository URL:

   `https://github.com/borgcube/HA-Moment-in-History-app`

4. Select **Add**.
5. Open the new **Moment in history** app, select **Install**, then start it.
6. Enable **Start on boot**.

Home Assistant Apps are only available with the Home Assistant OS or Supervised installation methods. The repository must be added to the Home Assistant App store; it is not installed by copying files into `custom_components`.

### HACS

HACS installs integrations, dashboard plugins, themes, templates, and similar Home Assistant configuration content. It does not install Home Assistant Apps/add-ons. Moment in History needs Supervisor features, a writable `/config` mapping, and a long-running container, so adding this repository to HACS would not install a working copy.

Use the **Apps repository** method above instead. The dashboard is made from built-in cards, so no HACS frontend resource is required.

### Manual App repository installation

If you maintain a local Home Assistant App repository:

1. Clone or download this repository.
2. Copy the `moment_in_history/` directory into your local App repository.
3. Add that local repository to **Settings → Apps → Repositories**.
4. Install and start **Moment in history** from the App store.

The app needs access to `/config` and writes generated files to `/config/www/moment-in-history/`. It is not a standalone Python script for Home Assistant Container/Core installations.

### Live model selection

The App includes a Home Assistant **Ingress** configuration page. Open the App's **Info** tab and choose **Open Web UI**. This is the App's Home Assistant configuration page, not Ollama Open WebUI; model discovery uses the provider APIs directly. The page automatically queries the configured endpoints when it opens, returns to the foreground, regains focus, and periodically while visible. Use **Refresh now** when you want to retry immediately:

- the primary Ollama endpoint's `/api/tags` response;
- the fallback Ollama endpoint's `/api/tags` response; and
- OpenAI's `/v1/models` endpoint, filtered to image-capable model IDs, when an OpenAI API key is configured.

Enter or edit the primary and fallback Ollama API URLs in this page. Choose the primary text model, fallback text model, OpenAI image model, image provider, image quality, local image backend, schedule, and one or more notification entities from the organized sections, then select **Save configuration**. The App saves these Web UI settings through Home Assistant Supervisor, keeps a persistent copy in its `/data` directory so they survive future schema changes, and restarts so the new choices become active. The OpenAI key is stored by Home Assistant and is never returned to the browser or written to the model-discovery response.

### Automatic updates from GitHub

After installing the App from this GitHub repository, open its **Info** tab and enable **Auto update** if you want Home Assistant Supervisor to update it automatically. This setting belongs to Supervisor, not the App's `config.yaml`; do not add `auto_update` to the App options. Supervisor periodically reloads the App repository and updates when the repository contains a newer App version.

Every code or configuration release must increment the `version` in `moment_in_history/config.yaml`. Supervisor preserves the App options and the generated files during an update. With **Regenerate on startup** disabled, an update restart will not request an extra image when today's image already exists.

## App Configuration

The app's **Configuration** tab is intentionally small. It contains only settings that affect image output size, local diffusion work, storage, and startup behavior:

| Setting | Purpose |
| --- | --- |
| Image size | Dropdown with the OpenAI-compatible sizes `1024x1024`, `1536x1024`, and `1024x1536`. The selected dimensions are also used by local image backends. |
| Image steps | Advanced local-backend control for ComfyUI diffusion sampling. OpenAI ignores it; leave the default unless the selected checkpoint needs another value. |
| Archive images | Keeps dated copies under `/config/www/moment-in-history/archive/`; each image is approximately 2 MB. The Web UI links to an archive gallery. |
| Regenerate today's event | One-shot request for a fresh image for today's event. It resets automatically after a successful generation. |
| Regenerate on startup | Creates a fresh image every time the app starts, even when today's image already exists. It is disabled by default because each restart consumes an image-generation credit. |

Provider URLs, models, the OpenAI key, image quality, schedule time, email, and ComfyUI settings are configured in **Info → Open Web UI**. Existing installations keep their saved values and can edit them there without regenerating the current image. On the first 1.9.x start, any existing Web UI values are copied into the App's persistent data before the reduced App schema is applied.

### Image providers

Configure the provider in **Info → Open Web UI**. `openai` uses the OpenAI Images API and requires an OpenAI API key. A ChatGPT or Claude subscription is not an API key and cannot be used directly by this app.

`lan` calls a compatible image service at `image_backend_url`. The LAN service must expose `POST /generate`, accept a JSON body containing `prompt`, `width`, `height`, and `steps`, and return a PNG image. The repository does not include that separate image service.

`comfyui` calls the ComfyUI API at `image_backend_url`. It queues a native checkpoint/KSampler/VAEDecode/SaveImage workflow, polls `/history/{prompt_id}`, and downloads the PNG from `/view`. Set `comfyui_model` to the checkpoint filename in ComfyUI's `models/checkpoints` directory. The Web UI discovers available checkpoints from ComfyUI and offers them as suggestions. The workflow uses the selected Image size and Image steps; SDXL Turbo is normally used with one step and square dimensions. A configured `image_backend_fallback_url` is also tried for ComfyUI if the primary endpoint fails.

For a remote ComfyUI deployment, keep the service bound to loopback and expose it through an authenticated reverse-proxy URL, for example `https://comfyui.example.com/comfyui`. Use the proxy URL in the Web UI rather than the private loopback port. A compatible local image API can be used as the primary or fallback URL instead; it should expose `/health` and `/generate`, while ComfyUI exposes its own checkpoint routes. The Web UI detects the service type from its API response and avoids treating a compatible local API as ComfyUI.

The OpenAI path automatically tries the configured LAN image backend if the OpenAI request fails. Ollama remains the text/ranking service for all image providers.

When a ComfyUI server is reachable through a reverse proxy, keep the ComfyUI process bound to loopback and restrict the proxy route to the Home Assistant host or trusted LAN. Do not expose an unauthenticated ComfyUI port directly to the Internet. The provider does not log API credentials or full backend URLs.

### Optional email delivery

Email delivery uses Home Assistant's built-in **SMTP** integration. The App can configure SMTP through Home Assistant's supported config flow when the user enables email delivery, or the integration can be set up manually first. The setup requires the sender address, SMTP host, port, encryption mode, credentials/app password, and recipient addresses; the App does not retain those values.

You can set it up before enabling email delivery:

1. Go to **Settings → Devices & services → Add integration → SMTP**.
2. Enter the SMTP account details and complete the integration setup.
3. Add one or more recipients to the SMTP integration. Home Assistant creates a `notify.*` entity for each recipient.
4. Open **Moment in history → Info → Open Web UI**. The model and notification lists are queried automatically; use **Refresh now** only if you need an immediate retry. All current `notify.*` entities are available in the multi-select under **Email delivery**.

Alternatively, enable **Send the daily image and event by email** in the App Web UI. If no SMTP integration is found, the App opens a setup dialog for the same sender, host, port, encryption, credentials, and first recipient fields. It submits the values through Home Assistant's supported SMTP config-flow API and waits for the resulting `notify.*` entity. Credentials are sent directly to Home Assistant and are never saved in the App's `/data` mapping or returned to the browser. Once SMTP exists, type an email address and press Space, Enter, or **Add email** to create additional recipients. Removing a managed pill removes the corresponding SMTP recipient through Home Assistant. If the installed Home Assistant version does not expose the supported config-flow API, automatic management is disabled and the existing `notify.*` dropdown remains available. Configuration changes continue to use Home Assistant's documented config-flow and subentry model; the App only reads the local config-entry store for the narrow case of testing an existing SMTP password that Home Assistant intentionally omits from its API. See the [Home Assistant config-flow documentation](https://developers.home-assistant.io/docs/core/integration/config_flow/).

The App also accepts a comma-separated list of recipient entity IDs, for example `notify.moment_in_history_recipient,notify.second_recipient`. The App no longer stores SMTP hostnames, usernames, passwords, TLS settings, or raw email addresses. If no recipient entity is selected, email delivery remains disabled.

The App calls Home Assistant's `smtp.send_message` action only after the new PNG has been written and atomically moved into place. The message contains the event, “ten seconds before” text, source link, and the current dashboard image through `email_camera_entity`. The HTML version embeds the image in the message and presents the event text in a disclosure-style **Show event details** section; the complete plain-text version is retained as the fallback for email clients that do not support expandable HTML. Home Assistant waits for its SMTP client to submit the message, so a successful call means the SMTP server accepted it; it does not provide an inbox delivery receipt. If generation fails, no email is attempted. If the SMTP action fails, the error is logged without discarding the generated image.

Home Assistant's SMTP integration must be able to reach and authenticate to your mail provider. Its current action sends to the selected `notify.*` entities; adding multiple recipients targets each selected recipient. See the [Home Assistant SMTP documentation](https://www.home-assistant.io/integrations/smtp/) for provider-specific settings and recipient management.

The Web UI always provides **Configure SMTP settings** or **Edit SMTP settings**, plus **Test connection** and **Send test email**. **Test connection** checks the entered SMTP server and encryption details without saving or sending mail. When editing an existing integration, leaving the password blank makes the App use Home Assistant's saved password internally for the connection test; it is never returned to the browser, stored by the App, or written to logs. A newly entered password takes precedence. **Edit SMTP settings** accepts Home Assistant's successful `reconfigure_successful` response as well as older `create_entry` responses. **Send test email** opens a recipient chooser so you can select existing `notify.*` entities or add an SMTP address for that test; it does not require saving daily delivery settings. It reads the existing `event.json` and current camera image when available, otherwise sends a generic setup message; it never generates an image. The success message means the SMTP server accepted the message via Home Assistant; inbox delivery still requires checking the recipient or provider logs. If setup is incomplete, the page opens the SMTP form or reports the exact missing configuration step. The optional [dashboard test script](examples/moment-in-history-email.yaml) remains available for users who prefer a Home Assistant dashboard button. The File integration requires `/config/www/moment-in-history` in `homeassistant.allowlist_external_dirs` when using that script.

If you manage `configuration.yaml` manually, add this allowlist entry and restart Home Assistant before using the test script:

```yaml
homeassistant:
  allowlist_external_dirs:
    - /config/www/moment-in-history
```

### Optional image archive

Set **Archive images** to true in App Configuration to keep dated copies under `/config/www/moment-in-history/archive/YYYY/MM/`. Open **Info → Open Web UI → Browse archived images** to view the dated thumbnail gallery; each thumbnail opens the full PNG and, when metadata is available, expands to show the event, the ten-seconds-before description, context, and source. Archiving is disabled by default so updates do not consume extra storage unexpectedly. A generated PNG is typically about **2 MB**, so archiving costs approximately 2 MB per day, or about 60 MB per 30-day month. The App logs the actual size of every archived image.

### Restart and update behavior

`regenerate_on_startup` defaults to `false`. An App update or restart keeps the Supervisor configuration and the generated files under `/config/www/moment-in-history/`; it does not request a new image when today's image already exists. The app always performs its normal startup schedule check: if the configured time has passed and today's image is missing, it generates the missed event automatically. Set **Regenerate today's event** to true for a one-time replacement of today's image; the app resets that option after generation succeeds. Set **Regenerate on startup** to true only when you intentionally want a fresh image on every app restart, including restarts where today's image already exists. The old `generate_on_restart` option is still read as a compatibility fallback for existing installations.

### Startup and runtime logging

The App writes `generation.log` under `/config/www/moment-in-history/` and also mirrors operational messages to standard output so they appear in the Home Assistant App log. On startup it reports the App version, timezone, polling interval, schedule, restart behavior, configured text and image endpoints, whether an OpenAI key is present, email status, and output paths. Credentials and API keys are never written to either log. It then reports whether today's image is already present, whether it is waiting for the daily schedule, or whether generation is starting.

Never commit an API key to this repository, a dashboard YAML file, or a public issue.

## Home Assistant setup

### 1. Create the disclosure helper

The recommended method is **Settings → Devices & services → Helpers → Create Helper → Toggle**. Set its name to **Disclose**. Home Assistant creates:

```text
input_boolean.disclose
```

If you manage helpers in YAML, the equivalent example is:

```yaml
# input_boolean.yaml, or under input_boolean: in configuration.yaml
disclose:
  name: Disclose
  icon: mdi:book-open-variant
```

Do not define the helper in both YAML and the UI.

### 2. Create the local-file camera

Create a **Local file** integration from **Settings → Devices & services → Add Integration** with:

- Name: `Moment in history`
- File path: `/config/www/moment-in-history/moment-in-history.png`

This creates `camera.moment_in_history`. Current Home Assistant versions configure this integration through the UI rather than through a `camera:` YAML platform entry.

### 3. Add the dashboard card

Open the Overview dashboard, choose **Edit dashboard → Add card → Manual**, and paste:

```yaml
type: vertical-stack
cards:
  - type: heading
    heading: Moment in history
    icon: mdi:history
  - type: picture-entity
    entity: camera.moment_in_history
    name: ""
    show_name: false
    show_state: false
    show_entity_picture: false
    tap_action:
      action: url
      url_path: /local/moment-in-history/event.html?v=2
  - type: tile
    entity: input_boolean.disclose
    name: Disclose
    icon: mdi:book-open-variant
    features_position: bottom
  - type: conditional
    conditions:
      - entity: input_boolean.disclose
        state: "on"
    card:
      type: iframe
      url: /local/moment-in-history/event.html?v=2
```

The `?v=2` query is intentional. Home Assistant serves `/local` files with a long cache lifetime; the event page itself also fetches `event.json` with a fresh cache-busting query so the text follows the current image.

## Generated files

The app writes these files under `/config/www/moment-in-history/`:

| File | Purpose |
| --- | --- |
| `moment-in-history.png` | Current dashboard artwork |
| `event.json` | Current structured event metadata |
| `event.html` | Plain-text disclosure page |
| `generation.log` | Daily generation log |
| `archive.html` | Image archive gallery |
| `archive/index.json` | Archive gallery index |
| `archive/YYYY/MM/YYYY-MM-DD.png` | Optional dated image archive |
| `archive/YYYY/MM/YYYY-MM-DD.json` | Optional archived event text and source metadata |

## Schedule and source fallback

The app checks every 30 seconds and generates after `schedule_time` if today's generation has not completed. A restart after the scheduled time automatically catches up when today's image is missing. **Regenerate on startup** overrides the normal daily guard and creates a fresh image even when today's image is already present. Britannica is attempted first. If it returns an anti-bot page, an HTTP error, or no parseable event list, the Wikimedia On This Day feed is used.

## Troubleshooting

### The disclosure text is from yesterday

Reload the Home Assistant dashboard once. The supplied card uses `event.html?v=2`, and the page fetches `event.json` with a unique query on every load.

### No image is generated

Check the app log and verify:

- Both Ollama URLs are reachable from the Home Assistant App network.
- The configured text model exists on the primary or fallback Ollama host.
- An OpenAI API key is present in the Web UI when `image_provider` is `openai`.
- The LAN image service is reachable when using `lan` or when relying on LAN fallback.

### The camera entity is missing

Confirm that the Local file integration points to `/config/www/moment-in-history/moment-in-history.png`, then reload or restart Home Assistant after creating it.

## Development

The App source is in `moment_in_history/`. The regression test covers the cache-refresh behavior of the disclosure page:

```bash
python3 -m pip install -r moment_in_history/requirements.txt
python3 -m unittest discover -s tests -v
python3 -m py_compile moment_in_history/moment_in_history.py
```

## License

MIT. See [LICENSE](LICENSE).
