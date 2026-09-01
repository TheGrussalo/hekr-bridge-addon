#!/usr/bin/env python3
"""
Hekr MITM proxy + MQTT bridge for KKT KOLBE range hoods (and other Hekr devices).

This proxy sits transparently between a Hekr WiFi device and the Hekr cloud,
exposing device state and controls to Home Assistant via MQTT auto-discovery.
It does NOT require any hardware modification of the device.

How it works:
  device <--TCP--> [this bridge] <--TCP--> Hekr cloud
The bridge forwards all traffic untouched, logs every message, publishes state
to MQTT and can inject commands (appSend) towards the device on demand.

You redirect the device's cloud traffic to this bridge using a DNAT rule on
your router (see README.md).

License: MIT
"""
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt

# === Configuration (via environment variables) ===
CLOUD_HOST = os.environ.get("HEKR_CLOUD_HOST", "128.1.42.23")
CLOUD_PORT = int(os.environ.get("HEKR_CLOUD_PORT", "83"))
LISTEN_PORT = int(os.environ.get("HEKR_LISTEN_PORT", "83"))

# Device identifiers - discover these by sniffing your device's traffic (README)
CTRL_KEY = os.environ.get("HEKR_CTRL_KEY", "")
DEV_TID = os.environ.get("HEKR_DEV_TID", "")

MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")

TOPIC_BASE = os.environ.get("MQTT_TOPIC_BASE", "cappa/kkt")
TOPIC_STATE = f"{TOPIC_BASE}/state"
TOPIC_AVAIL = f"{TOPIC_BASE}/availability"
TOPIC_CMD_POWER = f"{TOPIC_BASE}/power/set"
TOPIC_CMD_LIGHT = f"{TOPIC_BASE}/light/set"
TOPIC_CMD_SPEED = f"{TOPIC_BASE}/speed/set"
TOPIC_CMD_RGB_STATE = f"{TOPIC_BASE}/rgb/set"       # ON/OFF for the RGB light
TOPIC_CMD_RGB_COLOR = f"{TOPIC_BASE}/rgb/rgb/set"   # "R,G,B" string from HA colour picker
TOPIC_CMD_DEBUG_RAW = f"{TOPIC_BASE}/debug/raw/set"  # hex string, for protocol testing
TOPIC_CMD_TIME_SYNC = f"{TOPIC_BASE}/time/sync/set"   # any payload triggers a clock sync
TOPIC_CMD_FILTER_RESET = f"{TOPIC_BASE}/filter/reset/set"  # any payload triggers filter reset

HA_DISCOVERY_PREFIX = os.environ.get("HA_DISCOVERY_PREFIX", "homeassistant")
DEVICE_ID = os.environ.get("HA_DEVICE_ID", "cappa_kkt_kolbe")
DEVICE_NAME = os.environ.get("HA_DEVICE_NAME", "KKT KOLBE Hood")

# Mapped command IDs (KKT KOLBE FREE - may differ on other models)
CMD_POWER = int(os.environ.get("CMD_POWER", "2"))   # 0x02
CMD_LIGHT = int(os.environ.get("CMD_LIGHT", "3"))   # 0x03
CMD_SPEED = int(os.environ.get("CMD_SPEED", "4"))   # 0x04
# RGB colour, confirmed on KKT KOLBE HERMES RGBW: cmdId 0x07, 4-byte payload
# [mode, R, G, B]. mode=0x02 turns RGB on with the given colour, mode=0x01 turns
# it off. Status frame reports the current colour back in bytes 11/12/13.
CMD_COLOR = int(os.environ.get("CMD_COLOR", "7"))   # 0x07
# Clock/time-of-day, confirmed 2026-08-11: cmdId 0x08, plain 3-byte payload
# [hour, minute, second] with a real computed checksum (the reference
# implementation this was derived from used a hardcoded 0x00 checksum, which
# does not work on this hood). Not reported anywhere in the status frame, so
# there's no way to read the device's current clock value back - only set it.
CMD_TIME = int(os.environ.get("CMD_TIME", "8"))     # 0x08
# Filter/clean reminder reset, confirmed 2026-08-13: cmdId 0x06, plain 1-byte
# payload, value 0x00. Immediately clears status bytes 10 and 14 (the filter
# reminder flag) when the reminder is active. Previously unmapped despite
# extensive earlier testing; only produces an observable effect while the
# reminder is genuinely active, which is why earlier blind tests looked like
# no-ops.
CMD_FILTER_RESET = int(os.environ.get("CMD_FILTER_RESET", "6"))   # 0x06

LOG_DIR = Path(os.environ.get("LOG_DIR", "/data"))
LOG_DIR.mkdir(exist_ok=True)
TRAFFIC_LOG = LOG_DIR / "mitm_traffic.jsonl"
STATE_LOG = LOG_DIR / "mitm_state.jsonl"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("hekr-bridge")

if not CTRL_KEY or not DEV_TID:
    log.error("HEKR_CTRL_KEY and HEKR_DEV_TID must be set. See README.md")
    sys.exit(1)


class Session:
    def __init__(self):
        self.dev_writer = None
        self.cloud_writer = None
        self.last_state = {}
        self.injected_msg_id = 90000
        self.connected_since = None
        self.mqtt_client = None
        self.event_loop = None


def get_last_colour():
    """Read the device's own remembered colour directly from its most
    recent status frame, rather than maintaining a separate local cache.
    The device reports R/G/B in every status frame regardless of RGB
    on/off state - this is the genuine source of truth, survives add-on
    restarts automatically (no file needed), and stays in sync with
    changes made from the physical panel too. Falls back to white only in
    the brief window before any status frame has ever been received (e.g.
    moments after a fresh restart)."""
    r = session.last_state.get("r")
    g = session.last_state.get("g")
    b = session.last_state.get("b")
    if r is None or g is None or b is None or (r, g, b) == (0, 0, 0):
        return (255, 255, 255)
    return (r, g, b)


session = Session()


def log_msg(direction, obj):
    try:
        with open(TRAFFIC_LOG, "a") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(),
                "dir": direction,
                "msg": obj,
            }, default=str) + "\n")
    except Exception:
        pass


def decode_raw(raw_hex):
    """Decode the device's raw status frame.

    Observed layout (KKT KOLBE HERMES RGBW / FREE), 17 bytes:
      [0]=0x48 magic  [1]=len  [2]=frame type  [3]=seq
      [4]=?  [5]=?  [6]=light(0/1)  [7]=speed(0..4)
      [8]=RGB mode (0=unset/1=off/2=on)
      [9]=?  [10]=filter reminder (0/1, confirmed 2026-08-13)
      [11]=R  [12]=G  [13]=B
      [14]=filter reminder, mirrors [10] (confirmed 2026-08-13)
      [15]=? (transient change flag)  [16]=checksum
    """
    if not raw_hex or len(raw_hex) < 34:
        return None
    try:
        b = bytes.fromhex(raw_hex)
    except ValueError:
        return None
    if b[0] != 0x48:
        return None
    return {
        "raw": raw_hex,
        "seq": b[3],
        "byte4": b[4],
        "byte5": b[5],
        "light": b[6],
        "speed": b[7],
        "byte8": b[8],
        "rgb_on": b[8] == 2,
        "r": b[11],
        "g": b[12],
        "b": b[13],
        "filter_needs_cleaning": b[10] == 1,
        "filter_block": b[9:15].hex(),
        "byte15": b[15],
        "checksum": b[16],
        "power_on": b[7] > 0,
    }


def state_diff(new):
    diffs = []
    for k in ("byte4", "byte5", "light", "speed", "byte8", "r", "g", "b", "byte15", "filter_needs_cleaning", "filter_block"):
        old_v = session.last_state.get(k)
        new_v = new.get(k)
        if old_v != new_v:
            diffs.append(f"{k}: {old_v}->{new_v}")
    return "; ".join(diffs)


# === MQTT ===

def mqtt_publish_discovery():
    device = {
        "identifiers": [DEVICE_ID],
        "name": DEVICE_NAME,
        "manufacturer": "KKT KOLBE",
        "model": "Hekr ESP_2M hood",
        "sw_version": "hekr-bridge 1.0",
    }
    availability = [{
        "topic": TOPIC_AVAIL,
        "payload_available": "online",
        "payload_not_available": "offline",
    }]

    configs = [
        (
            f"{HA_DISCOVERY_PREFIX}/switch/{DEVICE_ID}/light/config",
            {
                "name": "Light",
                "unique_id": f"{DEVICE_ID}_light",
                "state_topic": TOPIC_STATE,
                "value_template": "{{ 'ON' if value_json.light == 1 else 'OFF' }}",
                "command_topic": TOPIC_CMD_LIGHT,
                "payload_on": "ON",
                "payload_off": "OFF",
                "icon": "mdi:lightbulb",
                "device": device,
                "availability": availability,
            },
        ),
        (
            f"{HA_DISCOVERY_PREFIX}/light/{DEVICE_ID}/rgb/config",
            {
                "name": "RGB Light",
                "unique_id": f"{DEVICE_ID}_rgb",
                "state_topic": TOPIC_STATE,
                "state_value_template": "{{ 'ON' if value_json.rgb_on else 'OFF' }}",
                "command_topic": TOPIC_CMD_RGB_STATE,
                "rgb_state_topic": TOPIC_STATE,
                "rgb_value_template": "{{ value_json.r }},{{ value_json.g }},{{ value_json.b }}",
                "rgb_command_topic": TOPIC_CMD_RGB_COLOR,
                "icon": "mdi:led-strip-variant",
                "device": device,
                "availability": availability,
            },
        ),
        (
            f"{HA_DISCOVERY_PREFIX}/button/{DEVICE_ID}/time_sync/config",
            {
                "name": "Sync Clock",
                "unique_id": f"{DEVICE_ID}_time_sync",
                "command_topic": TOPIC_CMD_TIME_SYNC,
                "icon": "mdi:clock-check-outline",
                "device": device,
                "availability": availability,
            },
        ),
        (
            f"{HA_DISCOVERY_PREFIX}/binary_sensor/{DEVICE_ID}/filter_needs_cleaning/config",
            {
                "name": "Filter Needs Cleaning",
                "unique_id": f"{DEVICE_ID}_filter_needs_cleaning",
                "state_topic": TOPIC_STATE,
                "value_template": "{{ 'ON' if value_json.filter_needs_cleaning else 'OFF' }}",
                "device_class": "problem",
                "icon": "mdi:air-filter",
                "device": device,
                "availability": availability,
            },
        ),
        (
            f"{HA_DISCOVERY_PREFIX}/button/{DEVICE_ID}/filter_reset/config",
            {
                "name": "Reset Filter Reminder",
                "unique_id": f"{DEVICE_ID}_filter_reset",
                "command_topic": TOPIC_CMD_FILTER_RESET,
                "icon": "mdi:air-filter",
                "device": device,
                "availability": availability,
            },
        ),
        (
            f"{HA_DISCOVERY_PREFIX}/switch/{DEVICE_ID}/power/config",
            {
                "name": "Power",
                "unique_id": f"{DEVICE_ID}_power",
                "state_topic": TOPIC_STATE,
                "value_template": "{{ 'ON' if value_json.power_on else 'OFF' }}",
                "command_topic": TOPIC_CMD_POWER,
                "payload_on": "ON",
                "payload_off": "OFF",
                "icon": "mdi:power",
                "device": device,
                "availability": availability,
            },
        ),
        (
            f"{HA_DISCOVERY_PREFIX}/select/{DEVICE_ID}/speed/config",
            {
                "name": "Speed",
                "unique_id": f"{DEVICE_ID}_speed",
                "state_topic": TOPIC_STATE,
                "value_template": "{{ value_json.speed | string }}",
                "command_topic": TOPIC_CMD_SPEED,
                "options": ["0", "1", "2", "3", "4"],
                "icon": "mdi:fan",
                "device": device,
                "availability": availability,
            },
        ),
        (
            f"{HA_DISCOVERY_PREFIX}/fan/{DEVICE_ID}/fan/config",
            {
                "name": "Fan",
                "unique_id": f"{DEVICE_ID}_fan",
                "state_topic": TOPIC_STATE,
                "state_value_template": "{{ 'ON' if value_json.speed > 0 else 'OFF' }}",
                "command_topic": TOPIC_CMD_POWER,
                "payload_on": "ON",
                "payload_off": "OFF",
                "percentage_state_topic": TOPIC_STATE,
                "percentage_value_template": "{{ (value_json.speed * 25) | int }}",
                "percentage_command_topic": TOPIC_CMD_SPEED,
                "percentage_command_template": "{{ (value | int / 25) | round(0) | int }}",
                "speed_range_min": 1,
                "speed_range_max": 100,
                "device": device,
                "availability": availability,
            },
        ),
        (
            f"{HA_DISCOVERY_PREFIX}/sensor/{DEVICE_ID}/raw/config",
            {
                "name": "Raw state",
                "unique_id": f"{DEVICE_ID}_raw",
                "state_topic": TOPIC_STATE,
                "value_template": "{{ value_json.raw }}",
                "icon": "mdi:code-string",
                "entity_category": "diagnostic",
                "device": device,
                "availability": availability,
            },
        ),
        (
            f"{HA_DISCOVERY_PREFIX}/sensor/{DEVICE_ID}/seq/config",
            {
                "name": "Sequence",
                "unique_id": f"{DEVICE_ID}_seq",
                "state_topic": TOPIC_STATE,
                "value_template": "{{ value_json.seq }}",
                "icon": "mdi:counter",
                "entity_category": "diagnostic",
                "device": device,
                "availability": availability,
            },
        ),
        (
            f"{HA_DISCOVERY_PREFIX}/binary_sensor/{DEVICE_ID}/online/config",
            {
                "name": "Connected",
                "unique_id": f"{DEVICE_ID}_online",
                "state_topic": TOPIC_AVAIL,
                "payload_on": "online",
                "payload_off": "offline",
                "device_class": "connectivity",
                "device": device,
            },
        ),
    ]

    for topic, payload in configs:
        session.mqtt_client.publish(topic, json.dumps(payload), retain=True, qos=1)
    log.info(f"MQTT: published {len(configs)} HA discovery entities")


def mqtt_publish_state():
    if not session.last_state:
        return
    session.mqtt_client.publish(
        TOPIC_STATE, json.dumps(session.last_state, default=str), retain=True, qos=0
    )


def mqtt_publish_availability(online: bool):
    if session.mqtt_client:
        session.mqtt_client.publish(
            TOPIC_AVAIL, "online" if online else "offline", retain=True, qos=1
        )


def on_mqtt_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        log.info(f"MQTT: connected to {MQTT_HOST}:{MQTT_PORT}")
        client.subscribe([
            (TOPIC_CMD_POWER, 1),
            (TOPIC_CMD_LIGHT, 1),
            (TOPIC_CMD_SPEED, 1),
            (TOPIC_CMD_RGB_STATE, 1),
            (TOPIC_CMD_RGB_COLOR, 1),
            (TOPIC_CMD_DEBUG_RAW, 1),
            (TOPIC_CMD_TIME_SYNC, 1),
            (TOPIC_CMD_FILTER_RESET, 1),
        ])
        mqtt_publish_discovery()
        if session.dev_writer is not None:
            mqtt_publish_availability(True)
        if session.last_state:
            mqtt_publish_state()
    else:
        log.error(f"MQTT: connection failed rc={rc}")


def on_mqtt_message(client, userdata, msg):
    topic = msg.topic
    payload = msg.payload.decode("utf-8", errors="replace").strip()
    log.info(f"MQTT cmd: {topic} = {payload!r}")
    loop = session.event_loop
    if loop is None:
        return

    async def handle():
        try:
            if topic == TOPIC_CMD_POWER:
                await inject_command(CMD_POWER, 1 if payload.upper() == "ON" else 0)
            elif topic == TOPIC_CMD_LIGHT:
                await inject_command(CMD_LIGHT, 1 if payload.upper() == "ON" else 0)
            elif topic == TOPIC_CMD_SPEED:
                try:
                    value = int(payload)
                except ValueError:
                    return
                if 0 <= value <= 4:
                    await inject_command(CMD_SPEED, value)
            elif topic == TOPIC_CMD_RGB_STATE:
                # Fully and finally verified 2026-08-13, after a couple of
                # incorrect conclusions along the way - see CHANGELOG.
                # mode=0x02 and mode=0x00 (both cmdId 0x07) are a clean,
                # symmetric on/off pair: mode=0x02 genuinely displays the
                # given colour, mode=0x00 genuinely turns the RGB output off
                # (status byte 8 -> 1, confirmed dark including the physical
                # panel indicator) - sent DIRECTLY, with no reset/fan/Light
                # involvement needed either way. Light (cmdId 0x03) turned
                # out to be a complete red herring for RGB - it only ever
                # controls the independent white channel.
                if payload.upper() == "OFF":
                    r, g, b = get_last_colour()
                    await inject_rgb(0x00, r, g, b)
                else:
                    r, g, b = get_last_colour()
                    await inject_rgb(0x02, r, g, b)
            elif topic == TOPIC_CMD_RGB_COLOR:
                # mode=0x02 directly - genuinely displays the given colour.
                try:
                    r, g, b = (int(x) for x in payload.split(","))
                except Exception:
                    log.error(f"Bad RGB payload: {payload!r}")
                    return
                await inject_rgb(0x02, r, g, b)
            elif topic == TOPIC_CMD_DEBUG_RAW:
                await inject_raw(payload.strip())
            elif topic == TOPIC_CMD_TIME_SYNC:
                payload_stripped = payload.strip()
                if payload_stripped and ":" in payload_stripped:
                    # Explicit "HH:MM:SS" (or "HH:MM") override, useful if the
                    # add-on container's own timezone doesn't match the hood's.
                    try:
                        parts = [int(x) for x in payload_stripped.split(":")]
                        h, m = parts[0], parts[1]
                        s = parts[2] if len(parts) > 2 else 0
                    except Exception:
                        log.error(f"Bad time sync payload: {payload_stripped!r}")
                        return
                else:
                    now = datetime.now()
                    h, m, s = now.hour, now.minute, now.second
                await inject_time(h, m, s)
            elif topic == TOPIC_CMD_FILTER_RESET:
                await inject_command(CMD_FILTER_RESET, 0x00)
        except Exception as e:
            log.error(f"MQTT cmd handling error: {e}")

    asyncio.run_coroutine_threadsafe(handle(), loop)


def mqtt_start():
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"hekr-bridge-{uuid.uuid4().hex[:8]}",
        protocol=mqtt.MQTTv311,
    )
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.will_set(TOPIC_AVAIL, "offline", retain=True, qos=1)
    client.on_connect = on_mqtt_connect
    client.on_message = on_mqtt_message
    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    except Exception as e:
        log.error(f"MQTT connect error: {e}")
        return None
    client.loop_start()
    session.mqtt_client = client
    return client


# === MITM core ===
#
# IMPORTANT: the device connection and the real-cloud connection are
# deliberately decoupled below (since 1.4.1). Local control (MQTT -> device,
# via inject_command/inject_rgb/etc, which writes directly to dev_writer) and
# state decoding (analyze(), which drives every HA entity) depend ONLY on the
# device being connected to us - never on whether relaying to the real Hekr
# cloud is currently working. A cloud-side hiccup (reset connection, cloud
# host briefly unreachable, etc) is handled as best-effort with its own
# reconnect+backoff loop, and can never end the device session or block a
# state update. Only the device itself disconnecting ends the session.
#
# This fixes a real incident (2026-08-31): a Home Assistant Core restart left
# this process's cloud-side connection in a bad state without crashing the
# process itself, so Supervisor never restarted it. Every subsequent device
# session hit a "Connection reset by peer" while relaying to the cloud, which
# under the old FIRST_COMPLETED coupling below tore down the device session
# too - even though local control never actually stopped working. The bridge
# now reconnects to the cloud on its own, indefinitely, without needing an
# external restart.

CLOUD_RECONNECT_BACKOFF_INITIAL = 2
CLOUD_RECONNECT_BACKOFF_MAX = 30


async def open_cloud():
    """Best-effort connect to the real Hekr cloud. Never raises."""
    try:
        reader, writer = await asyncio.open_connection(CLOUD_HOST, CLOUD_PORT)
        log.info(f"=== CLOUD CONNECT to {CLOUD_HOST}:{CLOUD_PORT} ok ===")
        return reader, writer
    except Exception as e:
        log.warning(f"[cloud] connect failed (will retry): {e}")
        return None, None


async def cloud_to_dev(dev_writer, cloud_state):
    """Relay cloud->device traffic, best-effort.

    Any failure here just drops the cloud side (cloud_state's reader/writer
    are cleared) and returns quietly - it never touches the device
    connection. dev_to_cloud() will reconnect to the cloud lazily on its next
    outbound chunk. This task is cancelled by handle_device() when the
    device session actually ends; it does not decide that on its own.
    """
    buf = b""
    while True:
        reader = cloud_state.get("reader")
        if reader is None:
            await asyncio.sleep(1)
            continue
        try:
            data = await reader.read(4096)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"[cloud->dev] read error (non-fatal, will reconnect): {e}")
            cloud_state["reader"] = None
            cloud_state["writer"] = None
            continue
        if not data:
            log.info("[cloud->dev] cloud closed the connection (non-fatal, will reconnect)")
            cloud_state["reader"] = None
            cloud_state["writer"] = None
            continue

        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            log_msg("cloud->dev", msg)
            analyze("cloud->dev", msg)

        try:
            dev_writer.write(data)
            await dev_writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Device side is dead - let dev_to_cloud's own read loop notice
            # and end the session; nothing to do here.
            return


async def dev_to_cloud(dev_reader, dev_writer, cloud_state, prefetched=None):
    """Read from the device - this is the critical path.

    State decoding and MQTT publishing (via analyze(), below) always happen
    here, regardless of whether relaying to the real Hekr cloud is currently
    working. Relaying to the cloud is best-effort: on failure we drop the
    cloud connection and reconnect lazily on the next chunk, with a capped
    backoff - but we never stop reading from the device because of it.

    'prefetched' is the first chunk already consumed by handle_device (to
    tell a real device session apart from Supervisor's TCP watchdog probe,
    which connects and disconnects without sending anything) - it's
    processed exactly like any other chunk before the normal read loop
    begins, so nothing is lost.

    The ONLY way this returns is the device itself disconnecting (EOF) or a
    genuine error reading from it - which is the correct, and only, signal
    that the session should end.
    """
    buf = b""
    backoff = CLOUD_RECONNECT_BACKOFF_INITIAL

    pending_chunks = [prefetched] if prefetched else []
    while True:
        if pending_chunks:
            data = pending_chunks.pop(0)
        else:
            data = await dev_reader.read(4096)
            if not data:
                break  # device disconnected - only real end-of-session condition

        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            log_msg("dev->cloud", msg)
            analyze("dev->cloud", msg)  # always runs - independent of cloud relay health

        if cloud_state.get("writer") is None:
            reader, writer = await open_cloud()
            if writer is not None:
                cloud_state["reader"] = reader
                cloud_state["writer"] = writer
                backoff = CLOUD_RECONNECT_BACKOFF_INITIAL
            else:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, CLOUD_RECONNECT_BACKOFF_MAX)
                continue

        try:
            cloud_state["writer"].write(data)
            await cloud_state["writer"].drain()
        except Exception as e:
            log.warning(f"[dev->cloud] forward error (non-fatal, will reconnect): {e}")
            try:
                cloud_state["writer"].close()
            except Exception:
                pass
            cloud_state["reader"] = None
            cloud_state["writer"] = None


def analyze(direction, msg):
    action = msg.get("action")
    if direction == "dev->cloud" and action == "devSend":
        raw_hex = msg.get("params", {}).get("data", {}).get("raw", "")
        decoded = decode_raw(raw_hex)
        if decoded:
            diff = state_diff(decoded)
            if diff:
                log.info(f"STATE CHG: {diff}  raw={raw_hex}")
                try:
                    with open(STATE_LOG, "a") as f:
                        f.write(json.dumps({
                            "ts": datetime.now().isoformat(),
                            "diff": diff,
                            "state": decoded,
                        }, default=str) + "\n")
                except Exception:
                    pass
            session.last_state = decoded
            mqtt_publish_state()


async def handle_device(dev_reader, dev_writer):
    """Own the device connection. The device is fully functional locally
    (state decoding + MQTT control) the instant it connects here - the cloud
    connection is opened alongside it but is never required for that, and
    never gets to end this session on its own.

    Supervisor's own TCP watchdog (config.yaml: watchdog: "tcp://[HOST]:83")
    connects to this exact port on a timer to check the process is alive,
    then disconnects immediately without sending anything - source address
    172.30.32.x (Supervisor's internal Docker network), not the hood's real
    LAN address. Without filtering that out, every watchdog probe gets
    mistaken for the hood connecting: MQTT availability flips online then
    straight back offline, and every entity flickers Unavailable roughly
    every 2 minutes even though the hood itself is fine. So: wait for the
    peer to actually send something before treating the connection as a
    real device session at all. An empty read (EOF with no data) closes
    quietly - no log line, no availability change, no cloud connection.
    """
    peer = dev_writer.get_extra_info("peername")
    try:
        first_data = await asyncio.wait_for(dev_reader.read(4096), timeout=5)
    except (asyncio.TimeoutError, Exception):
        first_data = None

    if not first_data:
        try:
            dev_writer.close()
        except Exception:
            pass
        return

    log.info(f"=== DEVICE CONNECT from {peer} ===")

    session.dev_writer = dev_writer
    session.connected_since = time.time()
    mqtt_publish_availability(True)

    cloud_state = {"reader": None, "writer": None}
    reader, writer = await open_cloud()
    cloud_state["reader"] = reader
    cloud_state["writer"] = writer
    session.cloud_writer = writer  # best-effort snapshot; may legitimately be None

    t_cloud = asyncio.create_task(cloud_to_dev(dev_writer, cloud_state))
    try:
        await dev_to_cloud(dev_reader, dev_writer, cloud_state, prefetched=first_data)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.error(f"[dev->cloud] device read error, ending session: {e}")
    finally:
        t_cloud.cancel()
        try:
            await t_cloud
        except (asyncio.CancelledError, Exception):
            pass

        log.info(f"=== SESSION END {peer} ===")
        if session.dev_writer is dev_writer:
            session.dev_writer = None
            session.cloud_writer = None
            session.connected_since = None
            mqtt_publish_availability(False)
        for w in (dev_writer, cloud_state.get("writer")):
            if w is None:
                continue
            try:
                w.close()
            except Exception:
                pass


async def inject_command(cmd_id, value):
    """Inject an appSend command towards the device (as if from the cloud)."""
    if session.dev_writer is None:
        log.warning("Device not connected; command ignored")
        return False
    session.injected_msg_id += 1
    seq = session.injected_msg_id & 0xFF
    payload = bytes([0x48, 0x07, 0x02, seq, cmd_id, value])
    chk = sum(payload) & 0xFF
    raw = (payload + bytes([chk])).hex().upper()
    msg = {
        "msgId": session.injected_msg_id,
        "action": "appSend",
        "params": {
            "devTid": DEV_TID,
            "ctrlKey": CTRL_KEY,
            "appTid": "injected-mitm",
            "data": {"raw": raw},
        },
    }
    line = json.dumps(msg, separators=(",", ":")) + "\n"
    log.info(f">>> INJECT cmdId=0x{cmd_id:02X} value=0x{value:02X} raw={raw}")
    log_msg("INJECTED->dev", msg)
    session.dev_writer.write(line.encode())
    await session.dev_writer.drain()
    return True


async def inject_rgb(mode, r, g, b):
    """Inject an RGB colour command (confirmed cmdId 0x07, 4-byte payload).

    mode byte semantics, fully and finally verified 2026-08-13, after a
    couple of incorrect conclusions along the way - see CHANGELOG:
      0x02 = ON. Genuinely drives the RGB hardware, displaying the given
             colour immediately (status byte 8 -> 2).
      0x00 = OFF. Genuinely turns the RGB output off - confirmed dark,
             including the physical panel indicator (status byte 8 -> 1) -
             while remembering the given colour for next time. Works when
             sent directly at any time, no reset or fan involvement needed.
             mode=0x02 and mode=0x00 are a clean, symmetric on/off pair.
      0x01 = never acknowledged by the device across dozens of tests, in
             any context.
      0x03 = accepted, behaves identically to 0x02 in every test so far.

    Light (cmdId 0x03) is a separate, fully independent channel (the white
    LED) - it does not affect RGB in either direction, and is not involved
    in RGB on/off at all.

    r, g, b: 0-255 each.
    """
    if session.dev_writer is None:
        log.warning("Device not connected; RGB command ignored")
        return False
    r, g, b = max(0, min(255, int(r))), max(0, min(255, int(g))), max(0, min(255, int(b)))
    session.injected_msg_id += 1
    seq = session.injected_msg_id & 0xFF
    body = [0x48, 0, 0x02, seq, CMD_COLOR, mode, r, g, b]
    body[1] = len(body) + 1
    chk = sum(body) & 0xFF
    raw = bytes(body + [chk]).hex().upper()
    msg = {
        "msgId": session.injected_msg_id,
        "action": "appSend",
        "params": {
            "devTid": DEV_TID,
            "ctrlKey": CTRL_KEY,
            "appTid": "injected-mitm",
            "data": {"raw": raw},
        },
    }
    line = json.dumps(msg, separators=(",", ":")) + "\n"
    log.info(f">>> INJECT RGB mode=0x{mode:02X} r={r} g={g} b={b} raw={raw}")
    log_msg("INJECTED->dev", msg)
    session.dev_writer.write(line.encode())
    await session.dev_writer.drain()
    return True


async def inject_time(hour, minute, second):
    """Inject a clock-set command (confirmed cmdId 0x08, 3-byte payload).

    [hour, minute, second], each 0-255 but expected in normal time ranges.
    No confirmation is possible - the clock is never reported in the status
    frame, so this is fire-and-forget. Verify visually on the hood's display.
    """
    if session.dev_writer is None:
        log.warning("Device not connected; time command ignored")
        return False
    hour, minute, second = int(hour) & 0xFF, int(minute) & 0xFF, int(second) & 0xFF
    session.injected_msg_id += 1
    seq = session.injected_msg_id & 0xFF
    body = [0x48, 0, 0x02, seq, CMD_TIME, hour, minute, second]
    body[1] = len(body) + 1
    chk = sum(body) & 0xFF
    raw = bytes(body + [chk]).hex().upper()
    msg = {
        "msgId": session.injected_msg_id,
        "action": "appSend",
        "params": {
            "devTid": DEV_TID,
            "ctrlKey": CTRL_KEY,
            "appTid": "injected-mitm",
            "data": {"raw": raw},
        },
    }
    line = json.dumps(msg, separators=(",", ":")) + "\n"
    log.info(f">>> INJECT TIME {hour:02d}:{minute:02d}:{second:02d} raw={raw}")
    log_msg("INJECTED->dev", msg)
    session.dev_writer.write(line.encode())
    await session.dev_writer.drain()
    return True


async def inject_raw(raw_hex):
    if session.dev_writer is None:
        return False
    session.injected_msg_id += 1
    msg = {
        "msgId": session.injected_msg_id,
        "action": "appSend",
        "params": {
            "devTid": DEV_TID,
            "ctrlKey": CTRL_KEY,
            "appTid": "injected-mitm",
            "data": {"raw": raw_hex.upper()},
        },
    }
    line = json.dumps(msg, separators=(",", ":")) + "\n"
    log.info(f">>> INJECT raw={raw_hex.upper()}")
    session.dev_writer.write(line.encode())
    await session.dev_writer.drain()
    return True


async def cli_repl():
    """Interactive REPL for testing and mapping command IDs.

    Commands:
      speed N        set fan speed 0..4
      light 0/1      light off/on
      power 0/1      master power off/on
      cmd HH HH      raw cmdId + value (hex), e.g. 'cmd 05 01'
      raw HEX        send a full raw payload
      rgb R G B      set RGB colour (decimal 0-255 each), e.g. 'rgb 57 140 105'
      rgboff         turn RGB off (keeps last colour remembered)
      state          print last decoded state
      conn           connection status
      quit
    """
    loop = asyncio.get_event_loop()
    print("\nCommands: speed N | light 0/1 | power 0/1 | cmd HH HH | raw HEX | rgb R G B | rgboff | state | conn | quit\n")
    while True:
        try:
            line = await loop.run_in_executor(None, input, "> ")
        except (EOFError, KeyboardInterrupt):
            break
        line = line.strip().lower()
        if not line:
            continue
        try:
            parts = line.split()
            cmd = parts[0]
            if cmd == "speed":
                await inject_command(CMD_SPEED, int(parts[1]))
            elif cmd == "light":
                await inject_command(CMD_LIGHT, int(parts[1]))
            elif cmd == "power":
                await inject_command(CMD_POWER, int(parts[1]))
            elif cmd == "cmd":
                await inject_command(int(parts[1], 16), int(parts[2], 16))
            elif cmd == "raw":
                await inject_raw(parts[1])
            elif cmd == "rgb":
                await inject_rgb(0x02, int(parts[1]), int(parts[2]), int(parts[3]))
            elif cmd == "rgboff":
                r, g, b = get_last_colour()
                await inject_rgb(0x00, r, g, b)
            elif cmd == "time":
                await inject_time(int(parts[1]), int(parts[2]), int(parts[3]) if len(parts) > 3 else 0)
            elif cmd == "filterreset":
                await inject_command(CMD_FILTER_RESET, 0x00)
            elif cmd == "state":
                print(json.dumps(session.last_state, indent=2, default=str))
            elif cmd == "conn":
                if session.dev_writer:
                    age = int(time.time() - session.connected_since)
                    print(f"connected for {age}s")
                else:
                    print("NOT connected")
            elif cmd in ("quit", "exit"):
                break
            else:
                print("Usage: speed N | light 0/1 | power 0/1 | cmd HH HH | raw HEX | rgb R G B | rgboff | state | conn | quit")
        except Exception as e:
            print(f"Error: {e}")


async def main():
    session.event_loop = asyncio.get_event_loop()
    mqtt_start()
    srv = await asyncio.start_server(handle_device, "0.0.0.0", LISTEN_PORT)
    log.info(f"Hekr bridge listening on 0.0.0.0:{LISTEN_PORT} -> {CLOUD_HOST}:{CLOUD_PORT}")
    async with srv:
        await asyncio.gather(srv.serve_forever(), cli_repl())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass