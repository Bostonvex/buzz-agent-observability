#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then echo "usage: $0 WHEEL" >&2; exit 2; fi
artifact=$1
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
test_root=$(mktemp -d "${TMPDIR:-/tmp}/buzz-observability-install-test.XXXXXX")
cleanup() { rm -rf -- "$test_root"; }
trap cleanup EXIT HUP INT TERM

install_prefix="$test_root/install"
command_dir="$test_root/bin"
data_dir="$test_root/data"
config_dir="$test_root/config"
mkdir -p "$data_dir" "$config_dir"

"$script_dir/install.sh" --prefix "$install_prefix" --bin-dir "$command_dir" --artifact "$artifact"
"$command_dir/buzz-observability" doctor \
  --database "$data_dir/telemetry.sqlite3" \
  --token-file "$config_dir/ingest-token" \
  --identity-salt-file "$config_dir/identity-salt"
"$command_dir/buzz-observability" backup \
  --database "$data_dir/telemetry.sqlite3" \
  --output "$data_dir/backup.sqlite3"

first_release=$(readlink "$install_prefix/current")
"$script_dir/upgrade.sh" --prefix "$install_prefix" --bin-dir "$command_dir" --artifact "$artifact"
second_release=$(readlink "$install_prefix/current")
if [ "$first_release" = "$second_release" ]; then echo "upgrade did not create a distinct release" >&2; exit 1; fi
"$script_dir/rollback.sh" --prefix "$install_prefix"
if [ "$(readlink "$install_prefix/current")" != "$first_release" ]; then echo "rollback did not restore the prior release" >&2; exit 1; fi
"$script_dir/uninstall.sh" --prefix "$install_prefix" --bin-dir "$command_dir"
if [ -e "$command_dir/buzz-observability" ] || [ ! -f "$data_dir/telemetry.sqlite3" ]; then
  echo "recoverable uninstall validation failed" >&2
  exit 1
fi
echo "install, doctor, backup, upgrade, rollback, and recoverable uninstall passed"
