#!/bin/sh
# /llm_harness/setup.sh
set -eu

TEMP_DIR=""

# Always end with a status line, and keep the window open when run in a terminal (NO_PAUSE=1 skips the pause).
finish() {
    status=$?
    [ -z "$TEMP_DIR" ] || rm -rf "$TEMP_DIR"
    if [ "$status" -ne 0 ]; then
        echo ""
        echo "ERROR: Harness setup stopped (exit code $status). Read the messages above."
    fi
    if [ -t 0 ] && [ -t 1 ] && [ "${NO_PAUSE:-0}" != "1" ]; then
        printf '\nPress Enter to close...'
        read -r _ || true
    fi
}
trap finish EXIT
trap 'exit 130' HUP INT TERM

SOURCE_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
TARGET_DIR="$HOME/.llm_harness"

for file in harness.py workflow_commands.txt; do
    if [ ! -f "$SOURCE_DIR/$file" ]; then
        echo "ERROR: Missing $file in $SOURCE_DIR"
        exit 1
    fi
done

STAMP=$(date '+%Y%m%d-%H%M%S')
TEMP_DIR="$TARGET_DIR/.setup-tmp-$STAMP"
BACKUP_DIR="$TARGET_DIR/.backups/$STAMP"
mkdir -p "$TARGET_DIR" "$TEMP_DIR"

# Stage harness files before replacing the current installation.
cp "$SOURCE_DIR/harness.py" "$TEMP_DIR/harness.py"
cp "$SOURCE_DIR/workflow_commands.txt" "$TEMP_DIR/workflow_commands.txt"

install_cbm() {
    INSTALLER="$TEMP_DIR/cbm-install.sh"
    if command -v curl >/dev/null 2>&1; then
        if ! curl -fsSL --connect-timeout 5 --max-time 60 \
            https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/main/install.sh \
            -o "$INSTALLER"; then
            echo "WARNING: Could not download the CBM installer. Continuing without changing CBM."
            return 0
        fi
    elif command -v wget >/dev/null 2>&1; then
        if ! wget -qO "$INSTALLER" --timeout=60 \
            https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/main/install.sh; then
            echo "WARNING: Could not download the CBM installer. Continuing without changing CBM."
            return 0
        fi
    else
        echo "WARNING: Neither curl nor wget is available. Skipping CBM installation/update."
        return 0
    fi

    if ! bash "$INSTALLER" --skip-config; then
        echo "WARNING: CBM installer failed. Continuing with the existing CBM state."
        return 0
    fi
    rm -f "$INSTALLER"
}

# Find CBM without assuming the current shell's PATH has been refreshed.
CBM_BIN=""
if command -v codebase-memory-mcp >/dev/null 2>&1; then
    CBM_BIN=$(command -v codebase-memory-mcp)
elif [ -x "$HOME/.local/bin/codebase-memory-mcp" ]; then
    CBM_BIN="$HOME/.local/bin/codebase-memory-mcp"
elif [ -x "$HOME/.local/bin/codebase-memory-mcp.exe" ]; then
    CBM_BIN="$HOME/.local/bin/codebase-memory-mcp.exe"
fi

# Check GitHub's latest release first. If the network is unavailable, leave
# any existing CBM installation untouched and continue installing the harness.
LATEST=""
if command -v curl >/dev/null 2>&1; then
    RELEASE_JSON=$(curl -fsSL --connect-timeout 4 --max-time 10 \
        https://api.github.com/repos/DeusData/codebase-memory-mcp/releases/latest 2>/dev/null || true)
    LATEST=$(printf '%s' "$RELEASE_JSON" | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"v\{0,1\}\([0-9][0-9.]*\)".*/\1/p' | head -n 1)
elif command -v wget >/dev/null 2>&1; then
    RELEASE_JSON=$(wget -qO- --timeout=10 \
        https://api.github.com/repos/DeusData/codebase-memory-mcp/releases/latest 2>/dev/null || true)
    LATEST=$(printf '%s' "$RELEASE_JSON" | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"v\{0,1\}\([0-9][0-9.]*\)".*/\1/p' | head -n 1)
fi

if [ -z "$LATEST" ]; then
    if [ -n "$CBM_BIN" ]; then
        echo "CBM: Could not check the latest release (offline or GitHub unavailable). Keeping the installed version."
    else
        echo "CBM: Could not check the latest release (offline or GitHub unavailable). Skipping CBM installation."
    fi
else
    INSTALLED=""
    if [ -n "$CBM_BIN" ]; then
        VERSION_OUTPUT=$("$CBM_BIN" --version 2>&1 || true)
        INSTALLED=$(printf '%s' "$VERSION_OUTPUT" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1 || true)
    fi

    if [ -n "$CBM_BIN" ] && [ -n "$INSTALLED" ]; then
        # Return 0 when installed >= latest, 1 when installed < latest.
        if awk -v latest="$LATEST" -v installed="$INSTALLED" 'BEGIN {
            split(latest,a,"."); split(installed,b,".");
            for(i=1;i<=3;i++) {
                if ((b[i]+0) > (a[i]+0)) exit 0;
                if ((b[i]+0) < (a[i]+0)) exit 1;
            }
            exit 0;
        }'; then
            echo "CBM: Installed version $INSTALLED is already at least the latest release $LATEST. Skipping."
        else
            echo "CBM: Updating $INSTALLED to latest release $LATEST..."
            install_cbm
        fi
    elif [ -n "$CBM_BIN" ]; then
        echo "CBM: Installed, but its version could not be read safely. Leaving it untouched."
    else
        echo "CBM: Not installed. Installing latest release $LATEST..."
        install_cbm
    fi
fi


# Back up existing files when upgrading.
if [ -f "$TARGET_DIR/harness.py" ] || [ -f "$TARGET_DIR/workflow_commands.txt" ]; then
    mkdir -p "$BACKUP_DIR"
    for file in harness.py workflow_commands.txt; do
        if [ -f "$TARGET_DIR/$file" ]; then cp "$TARGET_DIR/$file" "$BACKUP_DIR/$file"; fi
    done
    echo "Existing harness installation detected. Upgrading..."
else
    echo "No existing harness installation detected. Installing..."
fi

mv "$TEMP_DIR/harness.py" "$TARGET_DIR/harness.py"
mv "$TEMP_DIR/workflow_commands.txt" "$TARGET_DIR/workflow_commands.txt"
chmod 644 "$TARGET_DIR/harness.py" "$TARGET_DIR/workflow_commands.txt"

echo ""
echo "Setup successful."
echo "Harness installed at: $TARGET_DIR"
[ ! -d "$BACKUP_DIR" ] || echo "Harness backup at: $BACKUP_DIR"
