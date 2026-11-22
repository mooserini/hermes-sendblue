# hermes-sendblue

Text your [Hermes Agent](https://hermes-agent.nousresearch.com/docs/) over iMessage, SMS, or RCS with [Sendblue](https://docs.sendblue.com/).

This is the Hermes Agent version of `openclaw-sendblue`: a third-party Hermes platform plugin that lives under `~/.hermes/plugins/sendblue` and plugs into `hermes gateway`.

## What It Supports

- Inbound messages through Sendblue polling or optional webhooks
- Outbound Hermes replies through `POST /api/send-message`
- Hermes allowlists and DM pairing via `SENDBLUE_ALLOWED_USERS`
- Typing indicators while Hermes is working, when Sendblue has an existing iMessage route to the contact
- De-duplication with a small SQLite store under `~/.hermes/platforms/sendblue`
- Cron/notification delivery with `deliver=sendblue` and `SENDBLUE_HOME_CHANNEL`
- Public CDN-style image URL delivery as Sendblue media attachments
- Local file attachments uploaded automatically via Sendblue's `/api/upload-file` (max 100 MB)

## Quick Start

### 1. Get Sendblue Credentials

Use the Sendblue CLI:

```bash
node --version  # must be v18 or newer
npm install -g @sendblue/cli
sendblue setup      # new Sendblue account
# or: sendblue login  # existing Sendblue account
sendblue show-keys
```

Alternatively, grab the same values from your dashboard at [dashboard.sendblue.com](https://dashboard.sendblue.com)

You need:

- API key
- API secret
- Your Sendblue phone number
- The personal phone number you will text from

### 2. Install Hermes Agent

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
source ~/.zshrc  # or source ~/.bashrc
hermes setup
```

If `hermes --help` says "IBC Relayer" or "Informal Systems", your shell is finding a different Hermes binary. Run `command -v hermes`; the per-user Nous Hermes Agent shim is normally `~/.local/bin/hermes`. Put that earlier in `PATH` and open a fresh terminal before continuing.

### 3. Install This Plugin

From this repo:

```bash
./install.sh
```

That symlinks the repo to `$HERMES_HOME/plugins/sendblue`, defaulting to:

```text
~/.hermes/plugins/sendblue
```

For a real-file install rather than a symlink, run the one-shot from this checkout:

```bash
SENDBLUE_INSTALLER_RESTART=0 ./install-sendblue-one-shot.sh
```

It fetches the public default branch, copies it into the selected `HERMES_HOME`,
and verifies that Hermes lists `sendblue-platform` as enabled. Set `HERMES_BIN`
to the exact CLI executable if you have several installations. Restart is disabled
in the example so you can configure credentials and the allowed sender first.

### 4. Enable and Configure

Third-party Hermes plugins are opt-in. Enable this one once:

```bash
hermes plugins enable sendblue-platform
```

The easiest path is Hermes' setup wizard:

```bash
hermes gateway setup
```

Or add these values to `$HERMES_HOME/.env`, defaulting to `~/.hermes/.env`:

```bash
SENDBLUE_API_KEY=your-api-key
SENDBLUE_API_SECRET=your-api-secret
SENDBLUE_PHONE_NUMBER=+15550101000
SENDBLUE_ALLOWED_USERS=+15550101001
SENDBLUE_HOME_CHANNEL=+15550101001
```

Then restart the gateway:

```bash
hermes gateway restart
```

Text your Sendblue number from a phone listed in `SENDBLUE_ALLOWED_USERS`. Hermes should reply in the same thread.

## Access Control

Hermes denies unknown users by default. You have three options:

```bash
# Recommended: static allowlist
SENDBLUE_ALLOWED_USERS=+15550101001,+15550101002

# Alternative: leave allowlist empty and approve pairing codes
hermes pairing approve sendblue PAIRCODE

# Development only: allow every sender
SENDBLUE_ALLOW_ALL_USERS=true
```

Because Hermes can run terminal commands, avoid open access on public numbers.

## Webhook Mode

Polling (the default) works without a public endpoint and checks Sendblue for new messages every 5 seconds. Tune this with `SENDBLUE_POLL_INTERVAL_SECONDS` (minimum 1 second).

For lower-latency delivery, enable webhooks:

```bash
SENDBLUE_WEBHOOK_ENABLED=true
SENDBLUE_WEBHOOK_HOST=127.0.0.1
SENDBLUE_WEBHOOK_PORT=3141
SENDBLUE_WEBHOOK_PATH=/webhook/sendblue
SENDBLUE_WEBHOOK_SECRET=use-a-random-secret-at-least-16-chars
```

Expose the local server with HTTPS, for example through Caddy, nginx, Cloudflare Tunnel, or ngrok. Configure the Sendblue receive webhook URL to:

```text
https://your-public-host/webhook/sendblue
```

Set the same secret in Sendblue. This adapter accepts it from `sb-signing-secret`, `x-sendblue-secret`, `x-webhook-secret`, or `Authorization: Bearer ...`. Webhook mode will not start with an empty secret or `change-me`.

Local file paths handed to the adapter (via `send_image` or cron `media_files`) are uploaded to Sendblue and delivered as attachments. Public `http(s)` URLs are passed through as-is — if you have signed or ephemeral URLs, host them somewhere stable first.

## Config.yaml Alternative

Environment variables are the simplest and work well with Hermes setup. You can also configure the platform in `$HERMES_HOME/config.yaml`, defaulting to `~/.hermes/config.yaml`:

```yaml
gateway:
  platforms:
    sendblue:
      enabled: true
      extra:
        api_key: "your-api-key"
        api_secret: "your-api-secret"
        phone_number: "+15550101000"
        poll_interval_seconds: 5
        webhook:
          enabled: false
```

## Cron Delivery

Set:

```bash
SENDBLUE_HOME_CHANNEL=+15550101001
```

Then Hermes cron jobs can target Sendblue:

```text
deliver=sendblue
```

The plugin includes a standalone sender so cron can deliver even when the cron process is separate from the live gateway process.

## Troubleshooting

Check plugin discovery:

```bash
ls -la "${HERMES_HOME:-$HOME/.hermes}/plugins/sendblue"
hermes plugins list
hermes gateway status
```

Check logs:

```bash
hermes logs gateway -n 100
```

Common fixes:

- Confirm phone numbers are E.164, such as `+15550101000`
- Confirm `SENDBLUE_ALLOWED_USERS` contains the phone you text from
- On Sendblue free plans, make sure the contact is verified
- If your first inbound test does not arrive, text the Sendblue number from your verified/allowed phone once, then restart the gateway
- In webhook mode, confirm your public URL forwards to the configured local port
- If another Hermes profile is using the same Sendblue line, stop that gateway first

## Known Issues

### Files with uppercase extensions can't be sent

If you try to attach a file with an uppercase extension like `.JPG`, `.PNG`, `.PDF`, or `.MP4`, the message will be delivered with the caption text but **no attachment**. There is no error — the agent just appears to "forget" the file.

**Workaround:** rename or copy the file to use a lowercase extension before sending.

```bash
cp MyPhoto.JPG myphoto.jpg
# then send myphoto.jpg
```

**Why:** This is an upstream bug in hermes-agent itself, not this plugin. Its `extract_media` regex in `gateway/platforms/base.py` is compiled without `re.IGNORECASE`, so paths with uppercase extensions never match. 

## Development

Static verification:

```bash
python3 -m compileall .
python3 -m unittest discover -s tests
```

The plugin intentionally uses only the Python standard library plus Hermes' gateway APIs.

## Research Notes

See [docs/research.md](docs/research.md) for the Hermes and Sendblue API notes used to build this adapter.

MIT License.
