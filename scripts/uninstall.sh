#!/bin/sh
set -eu

install_prefix=${XDG_DATA_HOME:-"$HOME/.local/share"}/buzz-agent-observability/install
command_dir="$HOME/.local/bin"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix) install_prefix=$2; shift 2 ;;
    --bin-dir) command_dir=$2; shift 2 ;;
    *) echo "unknown uninstall argument: $1" >&2; exit 2 ;;
  esac
done
case "$install_prefix" in ""|/) echo "refusing unsafe install prefix" >&2; exit 2 ;; esac
case "$install_prefix" in /*) ;; *) echo "install prefix must be absolute" >&2; exit 2 ;; esac
case "$command_dir" in /*) ;; *) echo "command directory must be absolute" >&2; exit 2 ;; esac

for command_name in buzz-observability buzz-model-proxy; do
  command_path="$command_dir/$command_name"
  if [ -L "$command_path" ]; then
    command_target=$(readlink "$command_path")
    case "$command_target" in "$install_prefix"/*) unlink "$command_path" ;; esac
  fi
done

if [ -e "$install_prefix" ]; then
  archived_prefix="$install_prefix.uninstalled.$(date -u +%Y%m%dT%H%M%SZ)"
  if [ -e "$archived_prefix" ]; then echo "uninstall archive already exists" >&2; exit 1; fi
  mv "$install_prefix" "$archived_prefix"
  echo "Moved the recoverable installation to $archived_prefix"
fi
echo "Configuration and telemetry data were preserved. No service files were modified."
