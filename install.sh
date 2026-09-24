#!/usr/bin/env bash
set -euo pipefail

readonly UUID='cmf-headphone-pro@dlc4sacchi.github.io'
readonly REPOSITORY='dlc4sacchi/gnome-shell-extension-cmf-headphone-pro'
readonly RELEASE_URL="https://github.com/${REPOSITORY}/releases/latest/download/${UUID}.shell-extension.zip"

for program in curl python3 gnome-shell gnome-extensions gsettings bluetoothctl; do
    if ! command -v "$program" >/dev/null 2>&1; then
        printf 'Missing required command: %s\n' "$program" >&2
        exit 1
    fi
done

if ! python3 -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); from gi.repository import Gtk, Adw' 2>/dev/null; then
    printf 'Python PyGObject, GTK 4, and libadwaita are required.\n' >&2
    exit 1
fi

bundle=$(mktemp --suffix=.zip)
trap 'rm -- "$bundle"' EXIT

printf 'Downloading the latest CMF Headphone Pro Controls release…\n'
curl --fail --location --silent --show-error --retry 3 \
    --proto '=https' --proto-redir '=https' \
    --output "$bundle" "$RELEASE_URL"

python3 - "$bundle" "$UUID" "$(gnome-shell --version)" <<'PY'
import json
import re
import sys
from zipfile import ZipFile

bundle, expected_uuid, shell_version = sys.argv[1:]
match = re.search(r"\b(\d+)(?:\.\d+)?\b", shell_version)
if not match:
    sys.exit(f"Could not determine GNOME Shell version: {shell_version}")

try:
    with ZipFile(bundle) as archive:
        metadata = json.loads(archive.read("metadata.json"))
        if archive.testzip() is not None:
            sys.exit("The downloaded extension ZIP is damaged.")
except (OSError, ValueError, KeyError) as error:
    sys.exit(f"Invalid extension ZIP: {error}")

if metadata.get("uuid") != expected_uuid:
    sys.exit("The downloaded ZIP has an unexpected extension UUID.")
if match.group(1) not in metadata.get("shell-version", []):
    sys.exit(f"This release does not support GNOME Shell {match.group(1)}.")
PY

gnome-extensions install --force "$bundle"

# GNOME Shell may not discover a newly installed extension until the next login.
if ! gnome-extensions enable "$UUID" >/dev/null 2>&1; then
    python3 - "$UUID" <<'PY'
import ast
import subprocess
import sys

schema, key = "org.gnome.shell", "enabled-extensions"
output = subprocess.check_output(["gsettings", "get", schema, key], text=True).strip()
if output.startswith("@as "):
    output = output[4:]
enabled = ast.literal_eval(output)
uuid = sys.argv[1]
if uuid not in enabled:
    enabled.append(uuid)
    subprocess.run(["gsettings", "set", schema, key, repr(enabled)], check=True)
PY
fi

printf 'Installed %s and enabled it for GNOME Shell.\n' "$UUID"
printf 'Log out and back in to load the update.\n'
