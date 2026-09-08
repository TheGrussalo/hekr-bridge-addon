# Home Assistant Add-on: KKT Kolbe Hekr Bridge

## How it works

This add-on sits as a transparent (MITM) proxy between your KKT KOLBE
Hekr-protocol range hood and the Hekr cloud. Your hood's Wi-Fi traffic is
redirected (via a router DNAT rule) to this add-on instead of the real
Hekr cloud. The add-on:

- Decodes the hood's status frames and publishes them to Home Assistant
  over MQTT (auto-discovery — entities appear automatically).
- Accepts commands from Home Assistant over MQTT and re-encodes them into
  the hood's native frame format.
- Optionally relays traffic on to the real Hekr cloud as well, so the
  original WISEN app (if you still use it) keeps working.

No data leaves your network unless you keep cloud relaying enabled.

## Prerequisites

Before installing, gather:

- **`devTid`** and **`ctrlKey`** for your specific hood — see
  [Finding your device identifiers](#finding-your-device-identifiers) below.
- A router where you can add a **DNAT rule** redirecting your hood's cloud
  traffic (port 83) to your Home Assistant box's IP.
- An MQTT broker reachable from Home Assistant (the **Mosquitto broker**
  add-on works well).

## Installation

1. Settings → Add-ons → Add-on Store → ⋮ → Repositories.
2. Add `https://github.com/TheGrussalo/hekr-bridge-addon`.
3. Install **KKT Kolbe Hekr Bridge** from the new repository section.
4. Fill in the Configuration tab (see below), then start the add-on.

**This add-on uses host networking** (to listen on port 83 directly), so it
will not appear under the standard "Network" configuration tab — this is
expected, not a bug.

## Configuration

| Option | Required | Description |
|---|---|---|
| `hekr_dev_tid` | **Yes** | Unique device ID for your hood, e.g. `ESP_2M_XXXXXXXXXXXX`. |
| `hekr_ctrl_key` | **Yes** | 32-character hex key, unique to your device. |
| `hekr_cloud_host` | No | Real Hekr cloud host to relay to. Defaults to `hub.hekreu.me` (EU). |
| `hekr_cloud_port` | No | Port the real Hekr cloud listens on. Defaults to `83`. |
| `hekr_listen_port` | No | Port this add-on listens on for the hood. Defaults to `83`. |
| `mqtt_host` | No | Defaults to `localhost`. Host networking means Supervisor's internal `core-mosquitto` hostname won't resolve — use `localhost` or your HA box's own LAN IP. |
| `mqtt_port` | No | Defaults to `1883`. |
| `mqtt_user` / `mqtt_pass` | Depends on broker | Leave blank if your broker allows anonymous access. |
| `mqtt_topic_base` | No | Base MQTT topic. Defaults to `cappa/kkt`. |
| `ha_device_id` | No | Internal device identifier shown in Home Assistant. |
| `ha_device_name` | No | Friendly device name shown in Home Assistant. |
| `cmd_power` / `cmd_light` / `cmd_speed` / `cmd_color` | No | Protocol command IDs. Defaults (`2`/`3`/`4`/`7`) are confirmed working on the KKT KOLBE HERMES RGBW and FREE models. Other models may differ — see [Mapping commands for other models](#mapping-commands-for-other-models). |

## Finding your device identifiers

Capture the hood's own traffic (e.g. via a mirrored port or `tcpdump` on
your router):

```
tcpdump -i any -nn -s 0 -w hood.pcap 'host <YOUR_HOOD_IP>'
```

Power-cycle the hood (or force a reconnect) while capturing, then inspect
the JSON payloads:

```
tshark -r hood.pcap -Y 'tcp.len > 0' -T fields -e tcp.payload \
  | while read p; do printf "%b\n" "$(echo "$p" | sed 's/\(..\)/\\x\1/g')"; done
```

Look for a `devLoginResp` message containing `devTid` and `ctrlKey` — those
are the two required config values. The destination IP on port 83 is your
`hekr_cloud_host`.

## Redirecting the hood to the bridge

Once the add-on is running, redirect your hood's cloud-bound traffic to
your Home Assistant box's IP with a DNAT rule on your router:

```
iptables -t nat -A PREROUTING -s <HOOD_IP> -d <HEKR_CLOUD_HOST_IP> \
  -p tcp --dport 83 -j DNAT --to-destination <HA_IP>:83
```

If your hood and Home Assistant box are on the **same subnet**, you'll also
need a masquerade rule to handle NAT hairpinning:

```
iptables -t nat -A POSTROUTING -s <YOUR_LAN_SUBNET> -d <HA_IP> \
  -p tcp --dport 83 -j MASQUERADE
```

Force a reconnect (power-cycle the hood, or flush its existing connection
on the router, e.g. `conntrack -D -s <HOOD_IP>`) so it picks up the new
route.

## Entities exposed in Home Assistant

| Entity | Type | Notes |
|---|---|---|
| Power | `switch` | Master on/off (fan + light). |
| Light | `switch` | White light only. |
| Speed | `select` | 0–4. |
| Fan | `fan` | Same as speed, with percentage control. |
| **RGB Light** | `light` | Full colour picker (HERMES RGBW and similar models). |
| Connected | `binary_sensor` | Online/offline. |
| Filter Needs Cleaning | `binary_sensor` | Physical panel's clean-filter indicator. |
| Reset Filter Reminder | `button` | Clears the filter warning. |
| Sync Clock | `button` | Sets the hood's clock to the current time. |
| Raw state | `sensor` | Last raw hex frame (diagnostic). |
| Sequence | `sensor` | Device frame counter (diagnostic). |

## Mapping commands for other models

If your model's `cmdId`s differ from the defaults, connect to the add-on's
log/console and watch `STATE CHG` lines while testing values — see the
comments in `hekr_bridge.py` for the confirmed frame format
(`48 [len] 02 [seq] [cmdId] [values...] [checksum]`).

## Troubleshooting

- **No entities appear in Home Assistant** — confirm the MQTT broker is
  reachable at `mqtt_host`/`mqtt_port` from this add-on (remember: with host
  networking, `core-mosquitto` won't resolve) and that discovery is enabled
  on your broker/HA MQTT integration.
- **Hood never connects** — verify the DNAT rule is actually catching
  traffic (`tcpdump` on the HA box's listen port) and that the hood has
  been power-cycled or its connection flushed since the rule was added.
- **Entities flicker Unavailable periodically** — this was a known issue
  (Supervisor's TCP watchdog probe being mistaken for the hood connecting)
  fixed in 1.5.2. Update to the latest version.
- **Colour picked in Home Assistant doesn't stick / turns white** — fixed
  in 1.5.6. Update to the latest version.

See [CHANGELOG.md](https://github.com/TheGrussalo/hekr-bridge-addon/blob/main/hekr_bridge/CHANGELOG.md)
for the full history of protocol findings and fixes.

## Disclaimer

Unofficial, community project. Not affiliated with KKT KOLBE or Hekr. Use
at your own risk.
