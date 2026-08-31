#!/bin/sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
install_prefix=${XDG_DATA_HOME:-"$HOME/.local/share"}/buzz-agent-observability/install
command_dir="$HOME/.local/bin"
artifact=

while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix) install_prefix=$2; shift 2 ;;
    --bin-dir) command_dir=$2; shift 2 ;;
    --artifact) artifact=$2; shift 2 ;;
    *) echo "unknown install argument: $1" >&2; exit 2 ;;
  esac
done

case "$install_prefix" in ""|/) echo "refusing unsafe install prefix" >&2; exit 2 ;; esac
case "$command_dir" in ""|/) echo "refusing unsafe command directory" >&2; exit 2 ;; esac
case "$install_prefix" in /*) ;; *) echo "install prefix must be absolute" >&2; exit 2 ;; esac
case "$command_dir" in /*) ;; *) echo "command directory must be absolute" >&2; exit 2 ;; esac
if [ -z "$artifact" ]; then echo "--artifact must name a verified wheel" >&2; exit 2; fi

mkdir -p "$install_prefix/releases" "$command_dir"
for managed_link in "$install_prefix/current" "$install_prefix/previous"; do
  if [ -e "$managed_link" ] || [ -L "$managed_link" ]; then
    if [ ! -L "$managed_link" ]; then echo "managed release link is not a symlink" >&2; exit 1; fi
    managed_target=$(readlink "$managed_link")
    case "$managed_target" in "$install_prefix"/releases/*) ;; *) echo "managed release link points outside the install prefix" >&2; exit 1 ;; esac
  fi
done
for command_name in buzz-observability buzz-model-proxy; do
  command_path="$command_dir/$command_name"
  if [ -e "$command_path" ] || [ -L "$command_path" ]; then
    if [ ! -L "$command_path" ]; then echo "refusing to replace an unmanaged command" >&2; exit 1; fi
    command_target=$(readlink "$command_path")
    case "$command_target" in "$install_prefix"/*) ;; *) echo "refusing to replace an unmanaged command link" >&2; exit 1 ;; esac
  fi
done
release_version=$(python3 "$project_root/scripts/artifact-version.py" "$artifact")
release_dir=$(mktemp -d "$install_prefix/releases/$release_version-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
cleanup() { if [ -n "${release_dir:-}" ] && [ -d "$release_dir" ]; then rm -rf -- "$release_dir"; fi; }
trap cleanup EXIT HUP INT TERM

python3 -m venv "$release_dir/venv"
"$release_dir/venv/bin/python" -m pip install --disable-pip-version-check --no-deps "$artifact"
installed_version=$("$release_dir/venv/bin/python" -c 'import importlib.metadata; print(importlib.metadata.version("buzz-agent-observability"))')
if [ "$installed_version" != "$release_version" ]; then echo "installed package version does not match wheel metadata" >&2; exit 1; fi

if [ -L "$install_prefix/current" ]; then
  current_target=$(readlink "$install_prefix/current")
  case "$current_target" in "$install_prefix"/releases/*) ln -sfn "$current_target" "$install_prefix/previous" ;; esac
fi
ln -sfn "$release_dir" "$install_prefix/current"
ln -sfn "$install_prefix/current/venv/bin/buzz-observability" "$command_dir/buzz-observability"
ln -sfn "$install_prefix/current/venv/bin/buzz-model-proxy" "$command_dir/buzz-model-proxy"

echo "Installed Buzz Agent Observability $release_version"
echo "Commands: $command_dir/buzz-observability and $command_dir/buzz-model-proxy"
echo "No service was enabled or started."
release_dir=
