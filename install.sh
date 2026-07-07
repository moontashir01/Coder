#!/usr/bin/env bash
# Installer for Coder — the offline AI coding assistant (macOS / Linux).
#
# Sets everything up so you can type `coder` in any project folder:
#   1. Finds (or installs) a compatible Python 3.11 / 3.12
#   2. Creates an isolated virtualenv and installs Coder into it
#   3. Registers a global `coder` command in ~/.local/bin
#   4. Ensures Ollama is installed, running, and has the required models
# Re-running is safe (idempotent).
#
# Usage:  ./install.sh            # full setup
#         ./install.sh --no-ollama
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv"
SHIM_DIR="$HOME/.local/bin"
LLM_MODEL="qwen2.5-coder:7b"
EMBED_MODEL="nomic-embed-text"
NO_OLLAMA=0
[ "${1:-}" = "--no-ollama" ] && NO_OLLAMA=1

c_cyan=$'\033[36m'; c_green=$'\033[32m'; c_yellow=$'\033[33m'; c_red=$'\033[31m'; c_off=$'\033[0m'
info() { printf '  %s\n' "$*"; }
ok()   { printf '  %sOK%s  %s\n' "$c_green" "$c_off" "$*"; }
warn() { printf '  %s!%s   %s\n' "$c_yellow" "$c_off" "$*"; }
die()  { printf '  %sX%s   %s\n' "$c_red" "$c_off" "$*"; exit 1; }
step() { printf '\n%s==> %s%s\n' "$c_cyan" "$*" "$c_off"; }

printf '\nCoder installer\nRepo: %s\n' "$ROOT"

OS="$(uname -s)"

# --- 1. Locate a compatible Python ---------------------------------------
step "Locating Python 3.11 or 3.12"
PY=""
pyver_ok() {  # $1 = interpreter; echoes it if version is 3.11/3.12
    "$1" -c 'import sys;exit(0 if sys.version_info[:2] in ((3,11),(3,12)) else 1)' 2>/dev/null \
        && echo "$1"
}
for cand in python3.12 python3.11 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        found="$(pyver_ok "$(command -v "$cand")" || true)"
        [ -n "$found" ] && { PY="$found"; break; }
    fi
done

if [ -z "$PY" ]; then
    warn "No Python 3.11/3.12 found — attempting to install 3.12."
    if [ "$OS" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
        brew install python@3.12
        PY="$(pyver_ok "$(brew --prefix)/bin/python3.12" || true)"
    elif command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update && sudo apt-get install -y python3.12 python3.12-venv || \
        sudo apt-get install -y python3.11 python3.11-venv
        PY="$(pyver_ok "$(command -v python3.12)" || pyver_ok "$(command -v python3.11)" || true)"
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y python3.12 || sudo dnf install -y python3.11
        PY="$(pyver_ok "$(command -v python3.12)" || pyver_ok "$(command -v python3.11)" || true)"
    fi
    [ -z "$PY" ] && die "Could not install Python 3.12 automatically. Install Python 3.11 or 3.12 and re-run ./install.sh"
fi
ok "Using Python: $PY ($("$PY" --version 2>&1))"

# --- 2. Create venv + install Coder --------------------------------------
step "Creating virtual environment (.venv)"
[ -d "$VENV" ] && { info "Existing .venv found — recreating."; rm -rf "$VENV"; }
"$PY" -m venv "$VENV"
VPY="$VENV/bin/python"
[ -x "$VPY" ] || die "venv creation failed."
ok "venv ready"

step "Installing Coder and dependencies (this can take a few minutes)"
"$VPY" -m pip install --upgrade pip --quiet
"$VPY" -m pip install -e "$ROOT" || die "pip install failed."
ok "Coder installed into the venv"

# --- 3. Register a global `coder` command --------------------------------
step "Registering the global 'coder' command"
mkdir -p "$SHIM_DIR"
cat > "$SHIM_DIR/coder" <<EOF
#!/usr/bin/env sh
exec "$VENV/bin/coder" "\$@"
EOF
chmod +x "$SHIM_DIR/coder"
ok "Shim written: $SHIM_DIR/coder"
case ":$PATH:" in
    *":$SHIM_DIR:"*) info "$SHIM_DIR is already on PATH" ;;
    *) warn "$SHIM_DIR is not on your PATH."
       info "Add this line to your ~/.bashrc or ~/.zshrc, then open a new terminal:"
       printf '        export PATH="%s:$PATH"\n' "$SHIM_DIR" ;;
esac

# --- 4. Ollama + models ---------------------------------------------------
if [ "$NO_OLLAMA" -eq 0 ]; then
    step "Setting up Ollama"
    if ! command -v ollama >/dev/null 2>&1; then
        warn "Ollama not found."
        if [ "$OS" = "Linux" ]; then
            info "Installing Ollama (official script)..."
            curl -fsSL https://ollama.com/install.sh | sh
        elif [ "$OS" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
            info "Installing Ollama via Homebrew..."
            brew install ollama
        else
            warn "Install Ollama from https://ollama.com/download then re-run ./install.sh"
        fi
    fi

    if command -v ollama >/dev/null 2>&1; then
        ollama_up() { curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; }
        if ! ollama_up; then
            info "Starting the Ollama server..."
            (ollama serve >/dev/null 2>&1 &)
            i=0; while [ $i -lt 20 ] && ! ollama_up; do sleep 1; i=$((i+1)); done
        fi
        if ollama_up; then
            ok "Ollama is running"
            have="$(ollama list 2>/dev/null || true)"
            for m in "$LLM_MODEL" "$EMBED_MODEL"; do
                if printf '%s' "$have" | grep -q "$m"; then
                    info "Model already present: $m"
                else
                    info "Pulling $m (large download)..."
                    ollama pull "$m"
                fi
            done
            ok "Models ready"
        else
            warn "Could not reach Ollama. Start it ('ollama serve'), then: ollama pull $LLM_MODEL && ollama pull $EMBED_MODEL"
        fi
    fi
else
    info "Skipping Ollama setup (--no-ollama)."
fi

# --- Done -----------------------------------------------------------------
printf '\n%s============================================================%s\n' "$c_green" "$c_off"
printf '%s Coder is installed.%s\n' "$c_green" "$c_off"
printf '%s============================================================%s\n\n' "$c_green" "$c_off"
printf ' Open a new terminal, cd into any project, and run:\n'
printf '     %scoder%s\n\n' "$c_cyan" "$c_off"
