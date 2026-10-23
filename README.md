# Moment in History for Home Assistant

Moment in History generates one historical scene every day for a Home Assistant dashboard. It selects the most consequential event from the day's **On This Day** events, describes the instant exactly ten seconds before it, and generates a dramatic high-contrast cartoon illustration. The image contains only the date; the event details are disclosed as readable plain text.

The project is packaged as a Home Assistant App (formerly called an add-on). It keeps the orchestration and generated files on Home Assistant, uses Ollama for event selection and pre-event scene direction, and supports OpenAI Images or a LAN image service for the artwork.

## Features

- Britannica is attempted first for the daily event list, with Wikimedia's machine-readable On This Day feed as a fallback when Britannica is unavailable.
- Ollama ranks the complete event list and writes the one-sentence “ten seconds before” scene direction.
- OpenAI `gpt-image-2` is the default image backend, with an optional LAN backend fallback.
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
image_width: 1024
image_height: 1024
image_steps: 9

# Optional SMTP delivery. Leave email_recipients empty to disable email.
email_recipients: "you@example.com, second@example.com"
email_smtp_host: smtp.example.com
email_smtp_port: 587
email_smtp_username: "you@example.com"
email_smtp_password: "set this only in the App Configuration tab"
email_from: "you@example.com"
email_smtp_use_tls: true
email_smtp_use_ssl: false

schedule_time: "00:05"
generate_on_restart: false
```

### Image providers

`openai` uses the OpenAI Images API and requires an OpenAI API key. A ChatGPT or Claude subscription is not an API key and cannot be used directly by this app.

`lan` calls a compatible image service at `image_backend_url`. The LAN service must expose `POST /generate`, accept a JSON body containing `prompt`, `width`, `height`, and `steps`, and return a PNG image. The repository does not include that separate image service.

The OpenAI path automatically tries the configured LAN image backend if the OpenAI request fails. Ollama remains the text/ranking service in either case.

### Optional email delivery

Set `email_recipients` to a comma-separated list to send each newly generated image by email. The message contains the event title, the readable “ten seconds before” text, the source link, and `moment-in-history.png` as an attachment. Email is sent directly through the configured SMTP server; STARTTLS on port 587 is the default. For implicit TLS on port 465, set `email_smtp_use_tls: false` and `email_smtp_use_ssl: true`. If delivery fails, the app records the error in `generation.log` and keeps the image generation successful.

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
