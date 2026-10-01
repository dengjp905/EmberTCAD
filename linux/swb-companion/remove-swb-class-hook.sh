#!/usr/bin/env bash
set -euo pipefail

BASHRC="$HOME/.bashrc"
if [[ ! -f "$BASHRC" ]]; then
  echo "No .bashrc found; nothing to detach."
  exit 0
fi

function_block="$(sed -n '/^function swb_class()/,/^}/p' "$BASHRC")"
if [[ "$function_block" != *"aitcad-native"* && "$function_block" != *"nova-tcad"* && "$function_block" != *"spark-ai"* && "$function_block" != *"embertcad"* && "$function_block" != *"EmberTCAD"* ]]; then
  echo "SWB already starts independently."
  exit 0
fi

backup="$BASHRC.spark-ai-detach-backup-$(date +%Y%m%d-%H%M%S)"
temporary="$(mktemp "$BASHRC.spark-ai-detach.XXXXXX")"
cp -p "$BASHRC" "$backup"

awk '
  /^function swb_class\(\)/ { inside=1 }
  inside && /(\.local\/bin\/)?(aitcad-native|nova-tcad|spark-ai|embertcad|EmberTCAD)/ { next }
  { print }
  inside && /^}/ { inside=0 }
' "$BASHRC" > "$temporary"
chmod --reference="$BASHRC" "$temporary"
mv "$temporary" "$BASHRC"

function_block="$(sed -n '/^function swb_class()/,/^}/p' "$BASHRC")"
if [[ "$function_block" == *"aitcad-native"* || "$function_block" == *"nova-tcad"* || "$function_block" == *"spark-ai"* || "$function_block" == *"embertcad"* || "$function_block" == *"EmberTCAD"* ]]; then
  cp -p "$backup" "$BASHRC"
  echo "Detach verification failed; restored $BASHRC" >&2
  exit 1
fi

echo "Detached EmberTCAD from swb_class. Backup: $backup"
echo "Use 'EmberTCAD' to launch EmberTCAD independently."
