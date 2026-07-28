#!/usr/bin/env bash
set -e

OPTIONS=/data/options.json

export HEKR_DEV_TID=$(jq -r '.hekr_dev_tid' "$OPTIONS")
export HEKR_CTRL_KEY=$(jq -r '.hekr_ctrl_key' "$OPTIONS")
export HEKR_CLOUD_HOST=$(jq -r '.hekr_cloud_host' "$OPTIONS")
export HEKR_CLOUD_PORT=$(jq -r '.hekr_cloud_port' "$OPTIONS")
export HEKR_LISTEN_PORT=$(jq -r '.hekr_listen_port' "$OPTIONS")

if [ -z "$HEKR_DEV_TID" ] || [ -z "$HEKR_CTRL_KEY" ]; then
  echo "ERROR: 'hekr_dev_tid' and 'hekr_ctrl_key' must be set in the add-on's Configuration tab."
  echo "See the repository README for how to capture these from your hood's own traffic."
  exit 1
fi

export MQTT_HOST=$(jq -r '.mqtt_host' "$OPTIONS")
export MQTT_PORT=$(jq -r '.mqtt_port' "$OPTIONS")
export MQTT_USER=$(jq -r '.mqtt_user' "$OPTIONS")
export MQTT_PASS=$(jq -r '.mqtt_pass' "$OPTIONS")
export MQTT_TOPIC_BASE=$(jq -r '.mqtt_topic_base' "$OPTIONS")

export HA_DISCOVERY_PREFIX="homeassistant"
export HA_DEVICE_ID=$(jq -r '.ha_device_id' "$OPTIONS")
export HA_DEVICE_NAME=$(jq -r '.ha_device_name' "$OPTIONS")

export CMD_POWER=$(jq -r '.cmd_power' "$OPTIONS")
export CMD_LIGHT=$(jq -r '.cmd_light' "$OPTIONS")
export CMD_SPEED=$(jq -r '.cmd_speed' "$OPTIONS")
export CMD_COLOR=$(jq -r '.cmd_color' "$OPTIONS")

export LOG_DIR=/data

echo "Starting Hekr bridge: dev_tid=${HEKR_DEV_TID} cloud=${HEKR_CLOUD_HOST}:${HEKR_CLOUD_PORT} listen=:${HEKR_LISTEN_PORT} mqtt=${MQTT_HOST}:${MQTT_PORT}"

exec python3 /app/hekr_bridge.py
