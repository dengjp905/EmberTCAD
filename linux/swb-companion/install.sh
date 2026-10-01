#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${AITCAD_APP_DIR:-$HOME/.local/share/aitcad/swb-companion}"
BIN_DIR="${AITCAD_BIN_DIR:-$HOME/.local/bin}"
APPLICATIONS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/aitcad"
CONNECTOR_DIR="${AITCAD_DATA_DIR:-$HOME/.local/share/aitcad}"
ENV_FILE="$CONFIG_DIR/environment"
CHECK_ONLY=0
NO_DESKTOP=0
PROJECT_ROOT="${AITCAD_PROJECT_ROOT:-}"
SENTAURUS_ROOT="${AITCAD_RUN_STROOT:-${STROOT:-}}"
HELPER_PYTHON="${AITCAD_HELPER_PYTHON:-}"
GTK_PYTHON="${AITCAD_GTK_PYTHON:-}"

usage() {
  cat <<'EOF'
Usage: ./install.sh [--check] [--project-root PATH] [--sentaurus-root PATH]
                    [--gtk-python PATH] [--helper-python PATH]
                    [--prefix PATH] [--no-desktop]

Installs EmberTCAD for the current user. --check changes nothing and reports
whether GTK3, Python 3, Sentaurus, and the workspace can be discovered.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) CHECK_ONLY=1 ;;
    --no-desktop) NO_DESKTOP=1 ;;
    --project-root) PROJECT_ROOT="${2:?missing path}"; shift ;;
    --sentaurus-root) SENTAURUS_ROOT="${2:?missing path}"; shift ;;
    --gtk-python) GTK_PYTHON="${2:?missing path}"; shift ;;
    --helper-python) HELPER_PYTHON="${2:?missing path}"; shift ;;
    --prefix)
      TARGET_DIR="${2:?missing path}/share/aitcad/swb-companion"
      BIN_DIR="${2:?missing path}/bin"
      shift
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

OS_ID="unknown"
OS_VERSION="unknown"
OS_RELEASE_FILE="${AITCAD_OS_RELEASE_FILE:-/etc/os-release}"
if [[ -r "$OS_RELEASE_FILE" ]]; then
  # shellcheck disable=SC1091
  source "$OS_RELEASE_FILE"
  OS_ID="${ID:-unknown}"
  OS_VERSION="${VERSION_ID:-unknown}"
fi

find_executable() {
  local candidate
  for candidate in "$@"; do
    [[ -n "$candidate" ]] || continue
    if [[ "$candidate" == */* ]]; then
      [[ -x "$candidate" ]] && { printf '%s\n' "$candidate"; return 0; }
    elif command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

if [[ -z "$PROJECT_ROOT" ]]; then
  for candidate in "$HOME/STDB_class" "$HOME/STDB"; do
    if [[ -d "$candidate" ]]; then PROJECT_ROOT="$candidate"; break; fi
  done
  PROJECT_ROOT="${PROJECT_ROOT:-$HOME/STDB}"
fi
case "$PROJECT_ROOT" in
  /*) ;;
  *) PROJECT_ROOT="$PWD/$PROJECT_ROOT" ;;
esac
PROJECT_ROOT="${PROJECT_ROOT%/}"

if [[ -z "$SENTAURUS_ROOT" ]]; then
  if command -v swb >/dev/null 2>&1; then
    swb_path="$(command -v swb)"
    SENTAURUS_ROOT="$(cd "$(dirname "$swb_path")/.." && pwd)"
  else
    for candidate in /usr/synopsys/sentaurus/*; do
      if [[ -x "$candidate/bin/swb" ]]; then SENTAURUS_ROOT="$candidate"; break; fi
    done
  fi
fi

if [[ -z "$GTK_PYTHON" ]]; then
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import gi; gi.require_version("Gtk", "3.0")' >/dev/null 2>&1; then
      GTK_PYTHON="$(command -v "$candidate")"
      break
    fi
  done
fi
if [[ -n "$GTK_PYTHON" ]] && ! "$GTK_PYTHON" -c 'import gi; gi.require_version("Gtk", "3.0")' >/dev/null 2>&1; then
  GTK_PYTHON=""
fi

if [[ -z "$HELPER_PYTHON" ]]; then
  sentaurus_python=""
  for candidate in /usr/synopsys/sentaurus/X-*/tcad/*/linux64/bin/python3*; do
    if [[ -x "$candidate" ]]; then sentaurus_python="$candidate"; fi
  done
  HELPER_PYTHON="$(find_executable python3 "$sentaurus_python" || true)"
fi
if [[ -n "$HELPER_PYTHON" ]] && ! "$HELPER_PYTHON" -c 'import sys; assert sys.version_info >= (3, 6); import sqlite3, urllib.request' >/dev/null 2>&1; then
  HELPER_PYTHON=""
fi

release=""
if [[ -n "$SENTAURUS_ROOT" ]]; then
  if [[ -e "$SENTAURUS_ROOT/tcad/current" ]]; then
    release="$(basename "$(readlink -f "$SENTAURUS_ROOT/tcad/current")")"
  else
    release="$(basename "$SENTAURUS_ROOT")"
    release="${release/_/-}"
  fi
fi
failures=0
printf 'EmberTCAD environment check\n'
printf '  OS:              %s %s\n' "$OS_ID" "$OS_VERSION"
printf '  Workspace:       %s\n' "$PROJECT_ROOT"
printf '  Sentaurus root:  %s\n' "${SENTAURUS_ROOT:-NOT FOUND}"
printf '  GTK Python:      %s\n' "${GTK_PYTHON:-NOT FOUND}"
printf '  Helper Python 3: %s\n' "${HELPER_PYTHON:-NOT FOUND}"

case "$OS_ID" in
  centos|rhel|rocky|almalinux) ;;
  *) echo "  Warning: this OS is not in the current RHEL-compatible support matrix." ;;
esac
if [[ -z "$GTK_PYTHON" ]]; then
  echo "  Missing GTK3/PyGObject. CentOS 7: yum install pygobject3 gtk3; Rocky/Alma/CentOS Stream 8+: dnf install python3-gobject gtk3"
  failures=$((failures + 1))
fi
if [[ -z "$HELPER_PYTHON" ]]; then
  echo "  Missing Python 3.6+ with sqlite3 and urllib. Install python3 before EmberTCAD."
  failures=$((failures + 1))
fi
if [[ -z "$SENTAURUS_ROOT" || ! -x "$SENTAURUS_ROOT/bin/gtclsh" || ! -x "$SENTAURUS_ROOT/bin/gsub" ]]; then
  echo "  Sentaurus gtclsh/gsub was not found; pass --sentaurus-root PATH."
  failures=$((failures + 1))
fi
if [[ "$CHECK_ONLY" -eq 1 ]]; then
  [[ "$failures" -eq 0 ]] && echo "Environment is ready." || echo "Environment is not ready ($failures blocking issue(s))."
  exit "$failures"
fi
if [[ "$failures" -ne 0 ]]; then
  echo "Installation stopped. Fix the blocking checks above, then rerun." >&2
  exit 1
fi

mkdir -p "$TARGET_DIR/assets" "$BIN_DIR" "$APPLICATIONS_DIR" "$CONFIG_DIR" "$CONNECTOR_DIR" "$PROJECT_ROOT"
install -m 755 "$SOURCE_DIR/native_assistant.py" "$TARGET_DIR/native_assistant.py"
install -m 755 "$SOURCE_DIR/native_assistant_gtk3.py" "$TARGET_DIR/native_assistant_gtk3.py"
install -m 755 "$SOURCE_DIR/embertcad" "$TARGET_DIR/embertcad"
install -m 755 "$SOURCE_DIR/swb_live.py" "$TARGET_DIR/swb_live.py"
install -m 755 "$SOURCE_DIR/ai_agent.py" "$TARGET_DIR/ai_agent.py"
install -m 755 "$SOURCE_DIR/research_service.py" "$TARGET_DIR/research_service.py"
install -m 755 "$SOURCE_DIR/install.sh" "$TARGET_DIR/install.sh"
install -m 755 "$SOURCE_DIR/remove-swb-class-hook.sh" "$TARGET_DIR/remove-swb-class-hook.sh"
install -m 755 "$SOURCE_DIR/../../connector/aitcad_connector.py" "$CONNECTOR_DIR/connector.py"
install -m 644 "$SOURCE_DIR/../../LICENSE" "$TARGET_DIR/LICENSE"
install -m 644 "$SOURCE_DIR/../../NOTICE" "$TARGET_DIR/NOTICE"
install -m 644 "$SOURCE_DIR/aitcad.css" "$TARGET_DIR/aitcad.css"
install -m 644 "$SOURCE_DIR/assets/embertcad-logo.png" "$TARGET_DIR/assets/embertcad-logo.png"
ln -sfn "$TARGET_DIR/embertcad" "$BIN_DIR/EmberTCAD"
ln -sfn "$TARGET_DIR/embertcad" "$BIN_DIR/embertcad"
# Pre-release command names remain as compatibility aliases so upgrades do not
# break existing shell shortcuts or desktop sessions.
ln -sfn "$TARGET_DIR/embertcad" "$BIN_DIR/spark-ai"
ln -sfn "$TARGET_DIR/embertcad" "$BIN_DIR/aitcad-native"

{
  printf 'export AITCAD_APP_DIR=%q\n' "$TARGET_DIR"
  printf 'export AITCAD_PROJECT_ROOT=%q\n' "$PROJECT_ROOT"
  printf 'export AITCAD_RUN_STROOT=%q\n' "$SENTAURUS_ROOT"
  printf 'export AITCAD_SWB_API_STROOT=%q\n' "$SENTAURUS_ROOT"
  printf 'export AITCAD_HELPER_PYTHON=%q\n' "$HELPER_PYTHON"
  printf 'export AITCAD_GTK_PYTHON=%q\n' "$GTK_PYTHON"
  printf 'export AITCAD_SENTAURUS_RELEASE=%q\n' "$release"
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"

if [[ "$NO_DESKTOP" -eq 0 ]]; then
  sed -e "s|__EMBERTCAD_LAUNCHER__|$BIN_DIR/EmberTCAD|g" \
      -e "s|__EMBERTCAD_ICON__|$TARGET_DIR/assets/embertcad-logo.png|g" \
      "$SOURCE_DIR/embertcad.desktop.in" > "$APPLICATIONS_DIR/embertcad.desktop"
  chmod 644 "$APPLICATIONS_DIR/embertcad.desktop"
fi

# Remove launchers from pre-EmberTCAD prototypes. User projects, task history,
# reports, settings, and model credentials are never removed by the installer.
rm -f "$BIN_DIR/aitcad-swb" "$BIN_DIR/nova-tcad" \
      "$APPLICATIONS_DIR/aitcad-swb.desktop" "$APPLICATIONS_DIR/nova-tcad.desktop" \
      "$APPLICATIONS_DIR/spark-ai.desktop" \
      "$TARGET_DIR/install-swb-class-hook.sh" \
      "$TARGET_DIR/server.py" "$TARGET_DIR/aitcad-swb" \
      "$TARGET_DIR/static/index.html" "$TARGET_DIR/static/styles.css" "$TARGET_DIR/static/app.js" \
      "$TARGET_DIR/assets/nova-tcad-logo.png" "$TARGET_DIR/assets/maplefire-ai-logo.png" \
      "$TARGET_DIR/assets/spark-ai-logo.png" "$TARGET_DIR/aitcad-native"
rmdir "$TARGET_DIR/static" 2>/dev/null || true

echo "Installed EmberTCAD for $OS_ID $OS_VERSION."
echo "Workspace: $PROJECT_ROOT"
echo "Sentaurus: $release"
echo "Run independently: $BIN_DIR/EmberTCAD"
