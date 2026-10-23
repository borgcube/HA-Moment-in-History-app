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

SMTP must be configured once in Home Assistant before the app can send email. You need an email account whose provider permits SMTP access. Obtain the provider's SMTP host, port, encryption mode, username, and password or app-specific password first.

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

Home Assistant creates a separate `notify.*` entity for each SMTP recipient. This app does not store SMTP credentials or configure the SMTP server itself.

### Connect SMTP recipients to the app

Open **Moment in history → Info → Open Web UI** and select **Refresh available models and notifications**. Existing `notify.*` entities appear in the **Email delivery** list; select one or more and save.

The same page can manage recipients after SMTP has been configured: type an email address and press **Space**, **Enter**, or **Add email**. The app asks Home Assistant to create the SMTP recipient and displays the resulting email-to-`notify.*` pill. Removing a pill removes only the SMTP recipient created through this app. If automatic recipient management is unavailable, add recipients in the SMTP integration and select their existing `notify.*` entities instead.

For provider-specific settings, see the [Home Assistant SMTP documentation](https://www.home-assistant.io/integrations/smtp/).

## Complete the dashboard setup

The app cannot safely create `input_boolean.disclose`, the local-file camera, or the Overview dashboard card automatically. Create these supported Home Assistant objects once:

1. Create a Toggle helper named **Disclose** under **Settings → Devices & services → Helpers**. Home Assistant creates `input_boolean.disclose`.
2. Create a **Local file** camera pointing to `/config/www/moment-in-history/moment-in-history.png`. This creates `camera.moment_in_history`.
3. Paste the dashboard card from [`examples/overview-dashboard.yaml`](../examples/overview-dashboard.yaml) into the Overview dashboard's **Manual** card editor.

The repository README contains the full configuration, email test script, archive option, and troubleshooting details.
