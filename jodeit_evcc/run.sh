#!/bin/sh
set -eu

# Persistent addon_config mount.
if [ ! -e /config/evcc.yaml ]; then
  : > /config/evcc.yaml
fi

exec evcc --config /config/evcc.yaml
