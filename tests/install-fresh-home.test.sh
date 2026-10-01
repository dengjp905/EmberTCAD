#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER="$ROOT/linux/swb-companion/install.sh"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

mkdir -p "$SCRATCH/bin" "$SCRATCH/sentaurus/bin" "$SCRATCH/home"
cat >"$SCRATCH/bin/fake-python" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$SCRATCH/bin/fake-python"
for tool in swb gtclsh gsub; do
  cp "$SCRATCH/bin/fake-python" "$SCRATCH/sentaurus/bin/$tool"
done

HOME="$SCRATCH/home" bash "$INSTALLER" \
  --project-root "$SCRATCH/home/new-parent/STDB" \
  --sentaurus-root "$SCRATCH/sentaurus" \
  --gtk-python "$SCRATCH/bin/fake-python" \
  --helper-python "$SCRATCH/bin/fake-python" \
  --prefix "$SCRATCH/prefix" >/dev/null

app="$SCRATCH/prefix/share/aitcad/swb-companion"
test -L "$SCRATCH/prefix/bin/EmberTCAD"
test -L "$SCRATCH/prefix/bin/embertcad"
test -L "$SCRATCH/prefix/bin/spark-ai"
test -L "$SCRATCH/prefix/bin/aitcad-native"
test -x "$app/embertcad"
test -f "$SCRATCH/home/.local/share/aitcad/connector.py"
test -f "$app/LICENSE"
test -f "$app/NOTICE"
test -f "$SCRATCH/home/.local/share/applications/embertcad.desktop"
test -f "$app/assets/embertcad-logo.png"
grep -q '^Name=EmberTCAD$' "$SCRATCH/home/.local/share/applications/embertcad.desktop"
grep -q '^GenericName=Open-source AI Assistant for Sentaurus TCAD$' "$SCRATCH/home/.local/share/applications/embertcad.desktop"
grep -q "$SCRATCH/prefix/bin/EmberTCAD" "$SCRATCH/home/.local/share/applications/embertcad.desktop"
test -d "$SCRATCH/home/new-parent/STDB"
test "$(stat -c %a "$SCRATCH/home/.config/aitcad/environment")" = 600
test ! -e "$app/server.py"
test ! -e "$app/aitcad-swb"
test ! -e "$app/static"

echo "FRESH_HOME_INSTALL_OK"
