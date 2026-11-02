#!/bin/sh
task_plugin_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$task_plugin_dir/plugins/codex-usage-panel/floating.py" "$@"
