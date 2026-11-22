#!/usr/bin/env bash
# install-sendblue-one-shot.sh
# One-shot installer for mooserini/hermes-sendblue (Hermes platform plugin)
# into an existing Hermes install.
#
# What it does, in order:
#   1. Resolves HERMES_HOME (env wins; default ~/.hermes) and the hermes binary.
#   2. Fetches a fresh copy of the public repo (git clone, tarball fallback).
#   3. Backs up any existing $HERMES_HOME/plugins/sendblue (timestamped move,
#      never a delete).
#   4. Copies the plugin in as REAL FILES (no symlink — house rule), minus .git.
#   5. Byte-compiles the plugin as a syntax smoke test (stdlib only, no pip).
#   6. Merges SendBlue keys into $HERMES_HOME/.env (backs .env up first;
#      appends only missing keys; values are never printed — status + length
#      only). Values come from the caller's environment if set, otherwise
#      commented placeholders are written for a human/agent to fill.
#   7. Enables the plugin (hermes plugins enable sendblue-platform).
#   8. Restarts the gateway ONLY if all three credential keys are non-empty
#      afterwards (override: SENDBLUE_INSTALLER_RESTART=0 to never restart,
#      =1 to force).
#   9. Prints a receipt: paths, source commit, hashes, env status, backups,
#      and the exact next steps.
#
# Hermes itself can also write configuration and prepare its dependency environment.
#
# Usage:
#   bash install-sendblue-one-shot.sh
#   SENDBLUE_API_KEY=... SENDBLUE_API_SECRET=... \
#   SENDBLUE_PHONE_NUMBER=+1... SENDBLUE_ALLOWED_USERS=+1... \
#     bash install-sendblue-one-shot.sh
#
# Author: Maren Frost, 2026-11-22, from the public repo README
# (github.com/mooserini/hermes-sendblue @ main).

set -euo pipefail
umask 077  # .env and its backups may contain credentials

REPO_URL="https://github.com/mooserini/hermes-sendblue.git"
TARBALL_URL="https://codeload.github.com/mooserini/hermes-sendblue/tar.gz/refs/heads/main"
PLUGIN_NAME="sendblue-platform"

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins/sendblue"
ENV_FILE="$HERMES_HOME/.env"
TS="$(date +%Y%m%d-%H%M%S)"

say()  { printf '%s\n' "$*"; }
step() { printf '\n=== %s ===\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }

step "SCOPE"
say "HERMES_HOME : $HERMES_HOME"
say "Plugin dir  : $PLUGIN_DIR"
say "Env file    : $ENV_FILE"
say "Source      : $REPO_URL"
say "Enabling also updates Hermes configuration and may prepare dependencies."

# ---------------------------------------------------------------- preflight
step "PREFLIGHT"
HERMES_BIN="${HERMES_BIN:-}"
if [ -n "$HERMES_BIN" ]; then
  if [[ ! -f "$HERMES_BIN" || ! -x "$HERMES_BIN" ]]; then
    warn "HERMES_BIN is not an executable file: $HERMES_BIN"
    exit 1
  fi
  # Anchor relative paths so bare filenames cannot be resolved via PATH.
  [[ "$HERMES_BIN" == /* ]] || HERMES_BIN="$PWD/$HERMES_BIN"
elif command -v hermes >/dev/null 2>&1; then
  HERMES_BIN="$(command -v hermes)"
elif [ -x "$HOME/.local/bin/hermes" ]; then
  HERMES_BIN="$HOME/.local/bin/hermes"
fi
if [ -n "$HERMES_BIN" ]; then
  say "hermes      : $HERMES_BIN"
else
  warn "no hermes binary found on PATH or ~/.local/bin — plugin will be"
  warn "installed anyway; enable/restart steps will be printed, not run."
fi

command -v python3 >/dev/null 2>&1 || { warn "python3 required to verify plugin status"; exit 1; }

TMPDIR_WORK="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_WORK"' EXIT
SRC="$TMPDIR_WORK/src"
mkdir -p "$SRC"

# ---------------------------------------------------------------- fetch
step "FETCH"
SRC_COMMIT="unknown"
if command -v git >/dev/null 2>&1; then
  git clone --depth 1 "$REPO_URL" "$SRC/repo" >/dev/null 2>&1
  SRC_COMMIT="$(git -C "$SRC/repo" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  cp -R "$SRC/repo/." "$SRC/tree/" 2>/dev/null || { mkdir -p "$SRC/tree"; cp -R "$SRC/repo/." "$SRC/tree/"; }
  rm -rf "$SRC/tree/.git"
elif command -v curl >/dev/null 2>&1 && command -v tar >/dev/null 2>&1; then
  warn "git not found — falling back to the GitHub tarball."
  curl -fsSL "$TARBALL_URL" -o "$SRC/repo.tgz"
  mkdir -p "$SRC/tree"
  tar -xzf "$SRC/repo.tgz" -C "$SRC/tree" --strip-components 1
else
  warn "neither git nor curl+tar available — cannot fetch the repo. Aborting."
  exit 1
fi
say "Fetched source (commit: $SRC_COMMIT)"

for f in adapter.py plugin.yaml __init__.py; do
  if [ ! -f "$SRC/tree/$f" ]; then
    warn "fetched tree is missing $f — this does not look like the sendblue plugin. Aborting before touching the install."
    exit 1
  fi
done

# ---------------------------------------------------------------- install
step "INSTALL (real files, no symlink)"
if [ -e "$PLUGIN_DIR" ] || [ -L "$PLUGIN_DIR" ]; then
  # Keep old manifests outside discovery; allow multiple runs in one second.
  mkdir -p "$HERMES_HOME/backups"
  BACKUP_DIR="$(mktemp -d "$HERMES_HOME/backups/sendblue-$TS.XXXXXX")/sendblue"
  mv "$PLUGIN_DIR" "$BACKUP_DIR"
  say "Existing plugin moved aside: $BACKUP_DIR"
else
  BACKUP_DIR="(none — fresh install)"
fi
mkdir -p "$PLUGIN_DIR"
cp -R "$SRC/tree/." "$PLUGIN_DIR/"
FILE_COUNT="$(find "$PLUGIN_DIR" -type f | wc -l | tr -d ' ')"
say "Copied $FILE_COUNT files into $PLUGIN_DIR"

if command -v python3 >/dev/null 2>&1; then
  if python3 -m compileall -q "$PLUGIN_DIR" >/dev/null 2>&1; then
    say "Smoke test: python compileall OK (plugin is stdlib-only by design)."
  else
    warn "python compileall reported errors — plugin is in place anyway; check with: python3 -m compileall $PLUGIN_DIR"
  fi
fi

# ---------------------------------------------------------------- .env merge
step "ENV MERGE ($ENV_FILE)"
if [ -f "$ENV_FILE" ]; then
  ENV_BACKUP="$(mktemp "$ENV_FILE.bak-$TS.XXXXXX")"
  cp "$ENV_FILE" "$ENV_BACKUP"
  say "Backed up existing .env to $ENV_BACKUP"
else
  : > "$ENV_FILE"
  say "Created a fresh .env"
fi

ENV_STATUS=""
merge_key() {
  local key="$1" val="${2:-}"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    local cur
    cur="$(grep -E "^${key}=" "$ENV_FILE" | head -1 | cut -d= -f2-)"
    if [ -n "$cur" ]; then
      ENV_STATUS="${ENV_STATUS}\n  $key: already set in .env (len ${#cur}) — left untouched"
    else
      ENV_STATUS="${ENV_STATUS}\n  $key: present in .env but EMPTY — fill it"
    fi
  elif [ -n "$val" ]; then
    printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
    ENV_STATUS="${ENV_STATUS}\n  $key: written from caller environment (len ${#val})"
  else
    printf '# %s=  # TODO: fill before gateway restart\n' "$key" >> "$ENV_FILE"
    ENV_STATUS="${ENV_STATUS}\n  $key: MISSING — commented placeholder written"
  fi
}

# If the existing .env does not end in a newline, the first append would
# glue onto its last line — ensure a separator before merging anything.
if [ -s "$ENV_FILE" ] && [ -n "$(tail -c 1 "$ENV_FILE")" ]; then
  printf '\n' >> "$ENV_FILE"
  say "Added the missing trailing newline to .env before appending."
fi

merge_key SENDBLUE_API_KEY       "${SENDBLUE_API_KEY:-}"
merge_key SENDBLUE_API_SECRET    "${SENDBLUE_API_SECRET:-}"
merge_key SENDBLUE_PHONE_NUMBER  "${SENDBLUE_PHONE_NUMBER:-}"
merge_key SENDBLUE_ALLOWED_USERS "${SENDBLUE_ALLOWED_USERS:-}"
# Home channel is ONE phone number: default it to the FIRST allowlist
# entry only, never the whole comma-joined list.
FIRST_ALLOWED="$(printf '%s' "${SENDBLUE_ALLOWED_USERS:-}" | cut -d, -f1)"
merge_key SENDBLUE_HOME_CHANNEL  "${SENDBLUE_HOME_CHANNEL:-$FIRST_ALLOWED}"
# Poll mode is the default and needs no public endpoint. Make it explicit
# only if the caller asked for a different interval.
if [ -n "${SENDBLUE_POLL_INTERVAL_SECONDS:-}" ]; then
  merge_key SENDBLUE_POLL_INTERVAL_SECONDS "$SENDBLUE_POLL_INTERVAL_SECONDS"
fi
printf '%b\n' "$ENV_STATUS"

env_val() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- || true; }
CREDS_READY=1
for k in SENDBLUE_API_KEY SENDBLUE_API_SECRET SENDBLUE_PHONE_NUMBER; do
  [ -n "$(env_val "$k")" ] || CREDS_READY=0
done

# ---------------------------------------------------------------- enable
step "ENABLE PLUGIN"
if [ -z "$HERMES_BIN" ] || ! "$HERMES_BIN" plugins enable "$PLUGIN_NAME"; then
  warn "Installed, but not enabled. Gateway not restarted. Retry: hermes plugins enable $PLUGIN_NAME"
  exit 1
fi
if ! "$HERMES_BIN" plugins list --enabled --user --json | python3 -c '
import json, sys
rows = json.load(sys.stdin)
sys.exit(0 if any(r.get("name") == sys.argv[1] and r.get("status") == "enabled" for r in rows) else 1)
' "$PLUGIN_NAME"; then
  warn "Installed, but enablement not confirmed by Hermes. Gateway not restarted."
  exit 1
fi

# ---------------------------------------------------------------- restart
step "GATEWAY RESTART"
RESTART_MODE="${SENDBLUE_INSTALLER_RESTART:-auto}"
DO_RESTART=0
if [ "$RESTART_MODE" = "1" ]; then DO_RESTART=1; fi
if [ "$RESTART_MODE" = "auto" ] && [ "$CREDS_READY" = "1" ]; then DO_RESTART=1; fi
if [ "$RESTART_MODE" = "0" ]; then DO_RESTART=0; fi

if [ "$DO_RESTART" = "1" ] && [ -n "$HERMES_BIN" ]; then
  "$HERMES_BIN" gateway restart || { warn "Installed and enabled; gateway restart failed."; exit 1; }
elif [ "$DO_RESTART" = "1" ]; then
  say "Restart requested but no hermes binary found. Run: hermes gateway restart"
else
  say "Installed and enabled; gateway not restarted (credentials incomplete or restart disabled)."
  say "When ready: hermes gateway restart"
fi

# ---------------------------------------------------------------- receipt
step "RECEIPT"
say "Plugin dir : $PLUGIN_DIR ($FILE_COUNT files, real files, no symlink)"
say "Source     : $REPO_URL @ $SRC_COMMIT"
if command -v sha256sum >/dev/null 2>&1; then
  say "adapter.py sha256: $(sha256sum "$PLUGIN_DIR/adapter.py" | cut -d' ' -f1)"
  say "plugin.yaml sha256: $(sha256sum "$PLUGIN_DIR/plugin.yaml" | cut -d' ' -f1)"
elif command -v shasum >/dev/null 2>&1; then
  say "adapter.py sha256: $(shasum -a 256 "$PLUGIN_DIR/adapter.py" | cut -d' ' -f1)"
  say "plugin.yaml sha256: $(shasum -a 256 "$PLUGIN_DIR/plugin.yaml" | cut -d' ' -f1)"
fi
say "Plugin backup: $BACKUP_DIR"
say "Env backup   : ${ENV_BACKUP:-(none — fresh install)}"
say "Verified by Hermes: $PLUGIN_NAME is enabled."
say ""
say "NEXT STEPS"
say "  1. If any SENDBLUE_* key above is MISSING/EMPTY, fill it in: $ENV_FILE"
say "     Never paste credentials into chat or a transcript."
say "  2. hermes gateway restart"
say "  3. Text the SendBlue number from the allowed phone; expect a reply"
say "     in the same thread (poll mode, 5s default — no public endpoint)."
say "Done."
