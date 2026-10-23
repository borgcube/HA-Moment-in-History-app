# Moment in history App

Moment in history generates one historical scene each day and writes the current image and event details under `/config/www/moment-in-history/`.

## Install

For the normal Home Assistant OS or Supervised installation:

1. Go to **Settings → Apps**.
2. Open the three-dot menu and choose **Repositories**.
3. Add `https://github.com/borgcube/HA-Moment-in-History-app`.
4. Install **Moment in history**, start it, and enable **Start on boot**.

The app container installs automatically after this. The remaining steps below configure Home Assistant Core services and the dashboard; the app does not edit dashboard storage or create helpers by writing unsupported internal files.

## Configure Home Assistant SMTP

The app can configure SMTP for you when you enable email delivery, or you can configure it first from **Settings → Devices & services**. You need an email account whose provider permits SMTP access. Obtain the provider's SMTP host, port, encryption mode, username, and password or app-specific password first.

1. Go to **Settings → Devices & services**.
2. Select **Add integration**, search for **SMTP**, and open it.
3. Enter the SMTP settings:

   - **Sender email**: the address that appears in the From field.
   - **Sender name**: the display name for the sender.
   - **Host**: your provider's SMTP hostname, such as `smtp.example.com`.
   - **Port**: normally `587` with **STARTTLS**, or `465` with **TLS**. Use the values supplied by your provider.
   - **Connection security**: choose `STARTTLS` when supported; otherwise choose `TLS` or the provider's documented option.
   - **Username**: usually the full sender email address.
   - **Password**: the SMTP password or app-specific password.
   - **Verify SSL certificate**: leave enabled for a normal trusted certificate.

4. Complete the integration setup and add at least one recipient when prompted.
5. To add more later, open the SMTP integration under **Settings → Devices & services**, select **Add recipient**, enter the address, and submit it.

Alternatively, open **Moment in history → Info → Open Web UI** (the Home Assistant App configuration page), enable **Send the daily image and event by email**, and complete the popup. The popup asks for the same SMTP values and the first recipient, submits them through Home Assistant's supported SMTP config flow, and waits for the new `notify.*` entity. SMTP passwords are passed through to Home Assistant and are not stored by this app.

The Email delivery section always exposes **Configure SMTP settings** or **Edit SMTP settings**, so SMTP can be configured or changed without leaving the app. Editing uses Home Assistant's SMTP reconfigure flow, preserves existing recipients, and asks for the password again when the provider requires authentication. **Send test email** uses the current image and event when available, or a generic setup message otherwise; it never generates an image. If SMTP or a saved recipient is missing, the page now opens setup or reports the exact next step.

Home Assistant creates a separate `notify.*` entity for each SMTP recipient. SMTP credentials remain owned by Home Assistant; this app only submits them to Home Assistant's supported config flow when you choose the in-app setup dialog and does not store them.

### Connect SMTP recipients to the app

Open **Moment in history → Info → Open Web UI**. This opens the App's Home Assistant configuration page; Ollama Open WebUI is not required. Enter or edit the primary and fallback Ollama API URLs there. The page automatically discovers models through Ollama `/api/tags` and OpenAI `/v1/models`, as well as existing `notify.*` entities, when it opens and when it returns to the foreground. Use **Refresh now** only for an immediate retry. Existing `notify.*` entities appear in the **Email delivery** list; enable **Send the daily image and event by email**, select one or more recipients, and save.

The same page can manage recipients after SMTP has been configured: type an email address and press **Space**, **Enter**, or **Add email**. The app asks Home Assistant to create the SMTP recipient and displays the resulting email-to-`notify.*` pill. Removing a pill removes only the SMTP recipient created through this app. If automatic recipient management is unavailable, add recipients in the SMTP integration and select their existing `notify.*` entities instead.

For provider-specific settings, see the [Home Assistant SMTP documentation](https://www.home-assistant.io/integrations/smtp/).

## Complete the dashboard setup

The app cannot safely create `input_boolean.disclose`, the local-file camera, or the Overview dashboard card automatically. Create these supported Home Assistant objects once:

1. Create a Toggle helper named **Disclose** under **Settings → Devices & services → Helpers**. Home Assistant creates `input_boolean.disclose`.
2. Create a **Local file** camera pointing to `/config/www/moment-in-history/moment-in-history.png`. This creates `camera.moment_in_history`.
3. Paste the dashboard card from [`examples/overview-dashboard.yaml`](../examples/overview-dashboard.yaml) into the Overview dashboard's **Manual** card editor.

The repository README contains the full configuration, email test script, archive option, and troubleshooting details.

## Configure providers and models

Open **Moment in history → Info → Open Web UI**. The page is the single place for primary and fallback Ollama URLs and text models, the OpenAI key/model/quality, the local image API or ComfyUI URLs, the local model and checkpoint, the daily schedule, and email delivery. These Web UI settings are also kept in the App's persistent data so removing them from the App Configuration schema does not lose them. Model lists are queried directly from Ollama `/api/tags` and OpenAI `/v1/models`; Ollama Open WebUI is not required.

For ComfyUI, choose `ComfyUI`, set the backend URL, set `comfyui_model` to a checkpoint filename under ComfyUI's `models/checkpoints` directory, and use the Image size and Image steps controls in the App Configuration tab. The Web UI queries ComfyUI's checkpoint API and suggests detected model files. Keep a remote ComfyUI deployment on loopback behind an authenticated or source-IP-restricted HTTPS reverse proxy; do not expose its raw port. On OpenClaw, use `https://openclaw.borgcube.com/comfyui`; ComfyUI's loopback listener is port `8188` and is not the URL Home Assistant should use.
