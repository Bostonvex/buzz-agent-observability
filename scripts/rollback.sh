#!/bin/sh
set -eu

install_prefix=${XDG_DATA_HOME:-"$HOME/.local/share"}/buzz-agent-observability/install
while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix) install_prefix=$2; shift 2 ;;
    *) echo "unknown rollback argument: $1" >&2; exit 2 ;;
  esac
done
case "$install_prefix" in ""|/) echo "refusing unsafe install prefix" >&2; exit 2 ;; esac
case "$install_prefix" in /*) ;; *) echo "install prefix must be absolute" >&2; exit 2 ;; esac
if [ ! -L "$install_prefix/previous" ] || [ ! -L "$install_prefix/current" ]; then
  echo "no previous release is available" >&2
  exit 1
fi
previous_target=$(readlink "$install_prefix/previous")
current_target=$(readlink "$install_prefix/current")
case "$previous_target" in "$install_prefix"/releases/*) ;; *) echo "previous release is outside the managed prefix" >&2; exit 1 ;; esac
case "$current_target" in "$install_prefix"/releases/*) ;; *) echo "current release is outside the managed prefix" >&2; exit 1 ;; esac
if [ ! -d "$previous_target" ]; then echo "previous release directory is missing" >&2; exit 1; fi
ln -sfn "$previous_target" "$install_prefix/current"
ln -sfn "$current_target" "$install_prefix/previous"
echo "Rolled back to $(basename "$previous_target")"
echo "Restart the service manually after running the doctor command."
