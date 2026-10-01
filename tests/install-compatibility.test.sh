#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER="$ROOT/linux/swb-companion/install.sh"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

mkdir -p "$SCRATCH/bin" "$SCRATCH/sentaurus/bin" "$SCRATCH/home" "$SCRATCH/workspaces"
cat >"$SCRATCH/bin/fake-python" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$SCRATCH/bin/fake-python"
for tool in swb gtclsh gsub; do
  cp "$SCRATCH/bin/fake-python" "$SCRATCH/sentaurus/bin/$tool"
done

for platform in 'centos|7.9' 'rocky|8.10' 'rocky|9.6' 'almalinux|9.6'; do
  os_id="${platform%%|*}"
  os_version="${platform#*|}"
  os_release="$SCRATCH/os-release-$os_id-$os_version"
  printf 'ID=%s\nVERSION_ID=%s\n' "$os_id" "$os_version" >"$os_release"
  output="$({
    HOME="$SCRATCH/home" \
    AITCAD_OS_RELEASE_FILE="$os_release" \
    bash "$INSTALLER" --check \
      --project-root "$SCRATCH/workspaces/STDB" \
      --sentaurus-root "$SCRATCH/sentaurus" \
      --gtk-python "$SCRATCH/bin/fake-python" \
      --helper-python "$SCRATCH/bin/fake-python"
  } 2>&1)"
  grep -q "OS:              $os_id $os_version" <<<"$output"
  grep -q "Environment is ready." <<<"$output"
done

# A first-time user may intentionally point at a workspace whose parent does
# not exist yet.  --check must report that path instead of aborting while
# trying to canonicalize it; the real install creates the directory later.
nested_workspace="$SCRATCH/new-parent/new-workspace"
output="$(HOME="$SCRATCH/home" AITCAD_OS_RELEASE_FILE="$SCRATCH/os-release-rocky-9.6" \
  bash "$INSTALLER" --check --project-root "$nested_workspace" \
  --sentaurus-root "$SCRATCH/sentaurus" \
  --gtk-python "$SCRATCH/bin/fake-python" \
  --helper-python "$SCRATCH/bin/fake-python" 2>&1)"
grep -q "Workspace:       $nested_workspace" <<<"$output"
grep -q "Environment is ready." <<<"$output"

echo "INSTALL_COMPATIBILITY_OK centos7 rocky8 rocky9 almalinux9"
