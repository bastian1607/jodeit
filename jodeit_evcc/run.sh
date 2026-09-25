#!/bin/sh
set -eu

# /config is the persistent Home Assistant addon_config mount.
# Keep an empty YAML file available so evcc can combine YAML and its database/UI config.
if [ ! -e /config/evcc.yaml ]; then
  : > /config/evcc.yaml
fi

exec evcc --config /config/evcc.yaml
