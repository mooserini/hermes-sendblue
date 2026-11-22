# hermes-sendblue

Text your [Hermes Agent](https://hermes-agent.nousresearch.com/docs/) over iMessage, SMS, or RCS with [Sendblue](https://docs.sendblue.com/).

This is the Hermes Agent version of `openclaw-sendblue`: a third-party Hermes platform plugin that lives under `~/.hermes/plugins/sendblue` and plugs into `hermes gateway`.

The adapter has two layers. `SendblueClient` in `adapter.py` handles the Sendblue HTTP API. The Hermes gateway adapter handles polling or webhook events, sender permissions, media caches and cron delivery. Another host can use the same HTTP endpoints, receive-webhook payloads and environment-variable conventions. To run the adapter itself, it needs the Hermes gateway interfaces imported by this module.

## What It Supports

- Inbound messages through Sendblue polling or optional webhooks
- Outbound Hermes replies through `POST /api/send-message`
- Hermes allowlists and DM pairing via `SENDBLUE_ALLOWED_USERS`
- Typing indicators while Hermes is working, when Sendblue has an existing iMessage route to the contact
- De-duplication with a small SQLite store under `~/.hermes/platforms/sendblue`
- Cron/notification delivery with `deliver=sendblue` and `SENDBLUE_HOME_CHANNEL`
- Public CDN-style image URL delivery as Sendblue media attachments
- Local file attachments uploaded automatically via Sendblue's `/api/upload-file` (adapter limit 100 MiB)
- Inbound media downloads up to 25 MiB per file, with local caching in Hermes
- Voice-note conversion with optional ffmpeg and configurable multi-bubble text replies

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

**Agent-assisted phone setup.** For a new account, start with `sendblue setup --phone <number> --no-wait`. For an existing account, use `sendblue login --phone <number> --no-wait`. Show the operator the destination and exact verification phrase printed by the CLI. After they text it from their phone, run the corresponding `sendblue setup --check` or `sendblue login --check` with the same CLI state. Exit code 3 means verification is pending. Keep credential output local.

For a separate Sendblue cloud sandbox, `sendblue sandbox init --no-wait` starts verification without an upfront phone number or API key. Resume with `sendblue setup --check`, then run `sendblue sandbox create` after verification succeeds. The Hermes gateway can run on your existing host without provisioning a Sendblue sandbox. [Sendblue's agent onboarding guide](https://docs.sendblue.com/llms.txt)

### 2. Install Hermes Agent

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
source ~/.zshrc  # or source ~/.bashrc
hermes setup
```

If `hermes --help` says "IBC Relayer" or "Informal Systems", your shell is finding a different Hermes binary. Run `command -v hermes`; the per-user Nous Hermes Agent shim is normally `~/.local/bin/hermes`. Put that earlier in `PATH` and open a fresh terminal before continuing.

### 3. Install This Plugin

To run this checkout, keep it in a permanent location and run

```bash
./install.sh
```

That symlinks the repo to `$HERMES_HOME/plugins/sendblue`, defaulting to:

```text
~/.hermes/plugins/sendblue
```

For a real-file install of **current upstream**, run the one-shot installer. It fetches the default branch at execution time, even when invoked from a pinned checkout.

```bash
SENDBLUE_INSTALLER_RESTART=0 ./install-sendblue-one-shot.sh
```

Set `HERMES_HOME` to the target profile and `HERMES_BIN` to the intended Hermes executable when you have multiple installations. The installer moves an existing plugin into the profile's `backups/` directory and saves a copy of an existing `.env` before merging. It preserves existing assignments (including empty ones) and adds a missing final newline before appending keys. If the caller supplies an allowlist and omits the home channel, the installer uses the first allowed number for a missing home-channel assignment.

The installer verifies that `hermes plugins list --enabled --user --json` reports `sendblue-platform` as enabled before restarting. If verification fails, it exits with the installed files in place. Restart mode `0` skips restart, `auto` restarts when the three credential and line assignments in `.env` are nonempty, and `1` requests restart even with incomplete credentials.

### 4. Enable and Configure

After a symlink install, enable the plugin and check the listing. The one-shot installer already does both.

```bash
hermes plugins enable sendblue-platform
hermes plugins list --enabled --user --json
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

### A caption arrives without its attachment

The adapter lowercases the extension in the upload filename. A local `Photo.JPG` can be uploaded without renaming the original. First check whether Hermes extracted the path from the reply and handed it to the adapter. A lowercase copy can help isolate a case-sensitive extractor on an older Hermes installation. If the upload was attempted, check that the gateway can read the file, that it fits the 100 MiB upload limit and that Sendblue returned a media URL.

### A voice note arrives as a file

The adapter classifies inbound audio as voice. With ffmpeg on the gateway's PATH, it attempts to convert incoming CAF to M4A for Hermes speech-to-text, and outgoing local voice audio to CAF for native iMessage playback. Failed conversion preserves the original audio format. Check the gateway log for a conversion warning. Incoming media that cannot be downloaded or classified falls back to a link.

## Reply timing

Interactive text replies use plain text and can split on paragraphs or sentences. Short one-liners usually stay in one bubble. Standalone cron delivery uses length-only splitting.

| Environment variable | Default | Effect |
| --- | --- | --- |
| `SENDBLUE_CHUNKED_REPLIES` | `true` | Enables conversational splitting. Set to `false` for length-only splitting. |
| `SENDBLUE_CHUNK_MAX` | `4` | Target maximum conversational beats, clamped to 1–8. Each beat is split again as needed to fit the 18,000-character message limit. |
| `SENDBLUE_CHUNK_DELAY_MIN_MS` | `1800` | Minimum configured pause between bubbles. The send loop applies a 1.4-second floor. |
| `SENDBLUE_CHUNK_DELAY_MAX_MS` | `4200` | Maximum configured inter-bubble pause. |
| `SENDBLUE_CHUNK_TYPING` | `true` | Requests typing indicators for multi-bubble replies when the route supports them. |

## Development

Static verification:

```bash
python3 -m compileall .
python3 -m unittest discover -s tests
```

The adapter uses Python 3.11 or later and Hermes gateway APIs. Core messaging uses the Python standard library. Voice-note conversion uses the optional ffmpeg executable. The inbound-media tests need a real Hermes core, and two conversion tests also need ffmpeg and audio fixtures.

## Using Sendblue from another agent via MCP

The Hermes gateway receives inbound messages and routes replies to the conversation. An MCP client can also call Sendblue on demand. Choose the integration that fits the client:

| Connection | Authentication | Tool scope |
| --- | --- | --- |
| [Sendblue-hosted MCP](https://github.com/sendblue-api/sendblue-grok-plugin) at `https://api.sendblue.co/mcp` | Streamable HTTP with OAuth. The operator chooses a workspace and grants read or send permission. | Check connectivity, list sending lines, read recent messages, send a text, check delivery status, and summarize messaging health. |
| [Local MCP package](https://docs.sendblue.com/mcp) launched with `npx -y sendblue-api-mcp` | The MCP process reads `SENDBLUE_API_API_KEY` and `SENDBLUE_API_API_SECRET`. | Includes messages, contacts, groups, and media uploads. Use `--operation=read` for reads or `--resource=messages` to select message tools. |

Hermes accepts those two MCP credential names as aliases for `SENDBLUE_API_KEY` and `SENDBLUE_API_SECRET`. Its gateway setup and installer use the shorter names. Each integration manages its own access: Hermes sender allowlists apply to incoming gateway conversations, and the hosted MCP uses its workspace's OAuth permissions.

## Research Notes

See [docs/research.md](docs/research.md) for the Hermes and Sendblue API notes used to build this adapter.

MIT License.
