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

The App includes a Home Assistant **Ingress** configuration page. Open the App's **Info** tab and choose **Open Web UI**, then use **Refresh available models and notifications** to query:

- the primary Ollama endpoint's `/api/tags` response;
- the fallback Ollama endpoint's `/api/tags` response; and
- OpenAI's `/v1/models` endpoint, filtered to image-capable model IDs, when an OpenAI API key is configured.

Choose the primary text model, fallback text model, OpenAI image model, and one or more notification entities from the resulting dropdowns and select **Save selections**. The App saves the values through Home Assistant Supervisor and restarts so the new choices become active. The API key is used only inside the App and is never sent to the browser or written to the model-discovery response.

The ordinary App **Configuration** tab remains the source for endpoint URLs and the OpenAI API key. The model text fields remain in the schema for compatibility with existing installations, but the Ingress page is the recommended way to choose valid model IDs without typing them.

### Automatic updates from GitHub

After installing the App from this GitHub repository, open its **Info** tab and enable **Auto update** if you want Home Assistant Supervisor to update it automatically. This setting belongs to Supervisor, not the App's `config.yaml`; do not add `auto_update` to the App options. Supervisor periodically reloads the App repository and updates when the repository contains a newer App version.

Every code or configuration release must increment the `version` in `moment_in_history/config.yaml`. Supervisor preserves the App options and the generated files during an update. With `generate_on_restart: false`, the update restart will not request a new image.

## App configuration

Open the app's **Configuration** tab and set the values for your network. The following is an example of the option structure; it is documentation, not a file to paste into `configuration.yaml`:

```yaml
primary_ollama_url: http://ollama01.example.local:11434
fallback_ollama_url: http://ollama02.example.local:11434
text_model: qwen3.8:27b-mlx
fallback_text_model: gemma4:e4b

image_provider: openai
openai_api_key: "set this only in the App Configuration tab"
openai_image_model: gpt-image-2
openai_image_quality: high
openai_image_size: 1024x1024

# Used when image_provider is lan, or when OpenAI fails and a LAN fallback is configured.
image_backend_url: http://ollama02.example.local:8188
image_backend_fallback_url: ""
image_model: Tongyi-MAI/Z-Image-Turbo
comfyui_model: sd_xl_turbo_1.0.safetensors
image_width: 1024
image_height: 1024
image_steps: 9

# Optional Home Assistant SMTP delivery. Use one or more notify entities created
# by the Home Assistant SMTP integration; leave empty to disable.
email_notify_entity: notify.moment_in_history_recipient
email_camera_entity: camera.moment_in_history
archive_images: false

schedule_time: "00:05"
generate_on_restart: false
```

### Image providers

`openai` uses the OpenAI Images API and requires an OpenAI API key. A ChatGPT or Claude subscription is not an API key and cannot be used directly by this app.

`lan` calls a compatible image service at `image_backend_url`. The LAN service must expose `POST /generate`, accept a JSON body containing `prompt`, `width`, `height`, and `steps`, and return a PNG image. The repository does not include that separate image service.

`comfyui` calls the ComfyUI API at `image_backend_url`. It queues a native checkpoint/KSampler/VAEDecode/SaveImage workflow, polls `/history/{prompt_id}`, and downloads the PNG from `/view`. Set `comfyui_model` to the checkpoint filename in ComfyUI's `models/checkpoints` directory. The workflow uses `image_width`, `image_height`, and `image_steps`; SDXL Turbo is normally used with one step and square dimensions.

The OpenAI path automatically tries the configured LAN image backend if the OpenAI request fails. Ollama remains the text/ranking service for all image providers.

When a ComfyUI server is reachable through a reverse proxy, keep the ComfyUI process bound to loopback and restrict the proxy route to the Home Assistant host or trusted LAN. Do not expose an unauthenticated ComfyUI port directly to the Internet. The provider does not log API credentials or full backend URLs.

### Optional email delivery

Email delivery uses Home Assistant's built-in **SMTP** integration. The App can configure SMTP through Home Assistant's supported config flow when the user enables email delivery, or the integration can be set up manually first. The setup requires the sender address, SMTP host, port, encryption mode, credentials/app password, and recipient addresses; the App does not retain those values.

You can set it up before enabling email delivery:

1. Go to **Settings → Devices & services → Add integration → SMTP**.
2. Enter the SMTP account details and complete the integration setup.
3. Add one or more recipients to the SMTP integration. Home Assistant creates a `notify.*` entity for each recipient.
4. Open **Moment in history → Info → Open Web UI** and refresh the model and notification lists. All current `notify.*` entities are available in the multi-select under **Email delivery**.

Alternatively, enable **Send the daily image and event by email** in the App Web UI. If no SMTP integration is found, the App opens a setup dialog for the same sender, host, port, encryption, credentials, and first recipient fields. It submits the values through Home Assistant's supported SMTP config-flow API and waits for the resulting `notify.*` entity. Credentials are sent directly to Home Assistant and are never saved in the App's `/data` mapping or returned to the browser. Once SMTP exists, type an email address and press Space, Enter, or **Add email** to create additional recipients. Removing a managed pill removes the corresponding SMTP recipient through Home Assistant. If the installed Home Assistant version does not expose the supported config-flow API, automatic management is disabled and the existing `notify.*` dropdown remains available. This follows Home Assistant's documented config-flow and subentry model rather than editing `.storage` files directly; see the [Home Assistant config-flow documentation](https://developers.home-assistant.io/docs/core/integration/config_flow/).

The App also accepts a comma-separated list of recipient entity IDs, for example `notify.moment_in_history_recipient,notify.second_recipient`. The App no longer stores SMTP hostnames, usernames, passwords, TLS settings, or raw email addresses. If no recipient entity is selected, email delivery remains disabled.

The App calls Home Assistant's `smtp.send_message` action only after the new PNG has been written and atomically moved into place. The message contains the event, “ten seconds before” text, source link, and the current dashboard image through `email_camera_entity`. If generation fails, no email is attempted. If email delivery fails after successful generation, the error is logged without discarding the generated image.

Home Assistant's SMTP integration must be able to reach and authenticate to your mail provider. Its current action sends to the selected `notify.*` entities; adding multiple recipients targets each selected recipient. See the [Home Assistant SMTP documentation](https://www.home-assistant.io/integrations/smtp/) for provider-specific settings and recipient management.

The supplied [email test script](examples/moment-in-history-email.yaml) reads the existing `event.json` using Home Assistant's `file.read_file` action. If both event text and the image camera are available, it sends them; during first-time setup it sends a generic setup message without an attachment. Add the script before adding its dashboard button. The File integration requires `/config/www/moment-in-history` in `homeassistant.allowlist_external_dirs`.

If you manage `configuration.yaml` manually, add this allowlist entry and restart Home Assistant before using the test script:

```yaml
homeassistant:
  allowlist_external_dirs:
    - /config/www/moment-in-history
```

### Optional image archive

Set `archive_images: true` to keep dated copies under `/config/www/moment-in-history/archive/YYYY/MM/`. Archiving is disabled by default so updates do not consume extra storage unexpectedly. A generated PNG is typically about **2 MB**, so archiving costs approximately 2 MB per day, or about 60 MB per 30-day month. The App logs the actual size of every archived image.

### Restart and update behavior

`generate_on_restart` defaults to `false`. An App update or restart keeps the Supervisor configuration and the generated files under `/config/www/moment-in-history/`; it does not request a new image. The normal daily schedule remains active. Set `generate_on_restart: true` only when you intentionally want the App to generate immediately when its daemon starts. The older `run_on_startup` option remains in the schema for compatibility with existing installations, but it no longer controls generation.

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
| `archive/YYYY/MM/YYYY-MM-DD.png` | Optional dated image archive |

## Schedule and source fallback

The app checks every 30 seconds and generates after `schedule_time` if today's generation has not completed. With `generate_on_restart` disabled, a restart before or after the scheduled time does not create an extra image; the next scheduled run handles the next daily generation. Britannica is attempted first. If it returns an anti-bot page, an HTTP error, or no parseable event list, the Wikimedia On This Day feed is used.

## Troubleshooting

### The disclosure text is from yesterday

Reload the Home Assistant dashboard once. The supplied card uses `event.html?v=2`, and the page fetches `event.json` with a unique query on every load.

### No image is generated

Check the app log and verify:

- Both Ollama URLs are reachable from the Home Assistant App network.
- The configured text model exists on the primary or fallback Ollama host.
- `openai_api_key` is present when `image_provider` is `openai`.
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
