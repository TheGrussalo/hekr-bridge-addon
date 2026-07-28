# Changelog

## 1.0.1

### Fixed
- **MQTT client ID collision causing rapid reconnect loops.** The bridge used
  a hardcoded MQTT client ID (`hekr-bridge`), so if two instances connected to
  the same broker at once (e.g. testing the add-on while the original
  Docker-based bridge was still running), the broker would kick one off every
  time the other reconnected — visible as a `connected` / `published N HA
  discovery entities` loop repeating every ~2 seconds in the log. Each run now
  generates a unique client ID (`hekr-bridge-<random>`), so multiple instances
  no longer fight over the same connection.

## 1.0.0

### Added
- Initial Home Assistant Supervisor add-on packaging (`config.yaml`,
  `Dockerfile`, `run.sh`) — installs via **Settings → Add-ons → Add-on
  Store → Repositories** instead of requiring a separate Docker host.
- Full **RGB colour support** for the KKT KOLBE **HERMES RGBW** model,
  exposed as a proper Home Assistant `light` entity with a colour picker
  (`rgb_command_topic`/`rgb_state_topic`), in addition to the existing
  Power/Light/Speed entities from the original project.
  - Confirmed protocol: `cmdId 0x07`, 4-byte payload `[mode, R, G, B]`
    (`mode = 0x02` sets RGB on with the given colour, `mode = 0x01` turns
    it off).
  - Status frame bytes 11/12/13 report the current R/G/B values back,
    confirmed against all 9 panel colour presets.
- Sensitive config fields (`hekr_dev_tid`, `hekr_ctrl_key`, `mqtt_pass`,
  `mqtt_user`) left blank by default — set via the add-on's Configuration
  tab after install rather than committed to the repo.
- New REPL commands for testing/mapping: `rgb R G B`, `rgboff`.

Based on [markobel/kkt-kolbe-homeassistant](https://github.com/markobel/kkt-kolbe-homeassistant).