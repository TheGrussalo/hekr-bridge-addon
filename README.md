# Hekr Bridge Add-ons for Home Assistant

A Home Assistant Supervisor add-on that bridges a **KKT KOLBE** range hood
(and likely other Hekr/WISEN-protocol Wi-Fi appliances) into Home Assistant
over MQTT — including full RGB colour control on models that support it
(e.g. the **HERMES RGBW**), which isn't covered by the original upstream
project this is based on.

KKT KOLBE discontinued the WISEN app, leaving these hoods reachable on
Wi-Fi but with no supported way to control them. This add-on sits as a
transparent proxy between the hood and the (still-functioning) Hekr cloud,
exposing full control to Home Assistant via MQTT auto-discovery.

Based on [markobel/kkt-kolbe-homeassistant](https://github.com/markobel/kkt-kolbe-homeassistant),
extended with reverse-engineered RGB colour support and packaged as a proper
Supervisor add-on.

## Installation

1. In Home Assistant: **Settings → Add-ons → Add-on Store**
2. Click the **⋮** menu (top right) → **Repositories**
3. Add this URL: `https://github.com/YOUR_USERNAME/YOUR_REPO_NAME`
4. Find **"KKT Kolbe Hekr Bridge"** under the newly added repository section, click **Install**

## Prerequisites

Before installing, you need:

- **`devTid`** and **`ctrlKey`** for your specific hood — see [Finding your device identifiers](#finding-your-device-identifiers) below
- A router where you can add a **DNAT rule** redirecting your hood's cloud traffic (port 83) to your Home Assistant box's IP
- The **Mosquitto broker** add-on (or any MQTT broker) running and reachable from Home Assistant

## Configuration

After installing, go to the add-on's **Configuration** tab and fill in:

| Option | Required | Notes |
|---|---|---|
| `hekr_dev_tid` | **Yes** | Unique to your device, e.g. `ESP_2M_XXXXXXXXXXXX` |
| `hekr_ctrl_key` | **Yes** | 32-character hex key, unique to your device |
| `hekr_cloud_host` | No | Defaults to `hub.hekreu.me` (EU Hekr cloud) — change if your device connects elsewhere |
| `mqtt_host` | No | Defaults to `localhost` — this add-on uses host networking, so Supervisor's internal `core-mosquitto` hostname won't resolve; use `localhost` or your HA box's own LAN IP |
| `mqtt_user` / `mqtt_pass` | Depends on broker | Leave blank if your broker allows anonymous access |
| `cmd_power` / `cmd_light` / `cmd_speed` / `cmd_color` | No | Protocol command IDs — the defaults (2/3/4/7) are confirmed working on the KKT KOLBE HERMES RGBW and FREE models. Other models may differ; see the REPL mapping method below. |

**This add-on requires host networking** (to listen on port 83 directly),
so it will not show under the standard "Network" configuration tab —
this is expected.

## Finding your device identifiers

You need your hood's `devTid`, `ctrlKey`, and the cloud host it connects to.
Capture the hood's own traffic (e.g. via a mirrored port or by running
`tcpdump` on your router):

```bash
tcpdump -i any -nn -s 0 -w hood.pcap 'host <YOUR_HOOD_IP>'
```

Power-cycle the hood (or force a reconnect) while capturing, then inspect
the JSON payloads:

```bash
tshark -r hood.pcap -Y 'tcp.len > 0' -T fields -e tcp.payload \
  | while read p; do printf "%b\n" "$(echo "$p" | sed 's/\(..\)/\\x\1/g')"; done
```

Look for a `devLoginResp` message containing `devTid` and `ctrlKey` — those
are the two required config values. The destination IP on port 83 is your
`hekr_cloud_host`.

## Redirecting the hood to the bridge

Once the add-on is running, redirect your hood's cloud-bound traffic to your
Home Assistant box's IP with a DNAT rule on your router, e.g.:

```bash
iptables -t nat -A PREROUTING -s <HOOD_IP> -d <HEKR_CLOUD_HOST_IP> \
  -p tcp --dport 83 -j DNAT --to-destination <HA_IP>:83
```

If your hood and Home Assistant box are on the **same subnet**, you'll also
need a masquerade rule to handle NAT hairpinning:

```bash
iptables -t nat -A POSTROUTING -s <YOUR_LAN_SUBNET> -d <HA_IP> \
  -p tcp --dport 83 -j MASQUERADE
```

Force a reconnect (power-cycle the hood, or flush its existing connection
on the router, e.g. `conntrack -D -s <HOOD_IP>`) so it picks up the new route.

## Entities exposed in Home Assistant

| Entity | Type | Notes |
|---|---|---|
| Power | `switch` | master on/off (fan + light) |
| Light | `switch` | white light only |
| Speed | `select` | 0–4 |
| Fan | `fan` | same as speed, with percentage control |
| **RGB Light** | `light` | full colour picker (HERMES RGBW and similar models) |
| Connected | `binary_sensor` | online/offline |
| Raw state | `sensor` | last raw hex frame (diagnostic) |
| Sequence | `sensor` | device frame counter (diagnostic) |

## Mapping commands for other models

If your model's cmdIds differ from the defaults, you can find them by
connecting to the add-on's log/console and watching `STATE CHG` lines while
testing values — see the comments in `hekr_bridge.py` for the confirmed
frame format (`48 [len] 02 [seq] [cmdId] [values...] [checksum]`).

## Disclaimer

Unofficial, community project. Not affiliated with KKT KOLBE or Hekr. Use at
your own risk.

## License

MIT
