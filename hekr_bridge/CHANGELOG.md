# Changelog

## 1.2.0

### Added
- **Filter/clean reminder support, fully solved.** After weeks of this being
  an open investigation, both the indicator and the reset command are now
  confirmed and built in:
  - **"Filter Needs Cleaning"** binary sensor — reads status bytes 10 and 14
    (previously unmapped; both flip together, `0` -> `1`, when the physical
    panel's clean-filter indicator lights up). Confirmed against a real,
    organically-triggered filter warning on 2026-08-13.
  - **"Reset Filter Reminder"** button — sends `cmdId 0x06`, a single `0x00`
    byte. Confirmed to immediately clear both indicator bytes and the
    physical panel icon on the same real warning.
  - `cmdId 0x06` had been tested extensively in earlier sessions (bare,
    1-byte, and extended payloads) with no observable effect every time.
    That wasn't a wrong guess — that command only produces a visible result
    while a filter warning is genuinely active, and every earlier test ran
    with no warning present. Retesting the exact same command against a real
    warning confirmed it immediately.
  - New REPL command for testing: `filterreset`.


## 1.1.0

### Added
- **Clock sync support.** New "Sync Clock" button entity sets the hood's
  clock to the current time. Confirmed protocol: `cmdId 0x08`, 3-byte
  payload `[hour, minute, second]` with a properly computed checksum (a
  reference implementation this was derived from used a hardcoded `0x00`
  checksum, which does not work on this hood).
  - The clock is fire-and-forget — it's never reported back in the status
    frame, so there's no way to read the hood's current time, only set it.
  - New topic `cappa/kkt/time/sync/set`: any payload syncs to the add-on's
    own system time; an explicit `"HH:MM:SS"` (or `"HH:MM"`) payload
    overrides this, useful if the add-on container's timezone doesn't
    match the hood's.
  - New REPL command for testing: `time HH MM SS`.

### Investigated, not resolved
- **cmdId 0x06** remains unidentified. A community protocol table suggested
  it may correspond to a filter/"cleaning" reset, but every tested payload
  shape (bare, 1-byte value, extended 2-byte) got no response. Since a
  "clear filter reminder" command might only produce a visible effect while
  a filter warning is actually active, this is inconclusive rather than
  ruled out — worth revisiting once the filter-clean indicator next appears.

## 1.0.2

### Fixed
- **RGB "off" did nothing.** The dedicated off-flag (`mode=0x01`) was assumed
  to work by symmetry with the working "on" flag (`mode=0x02`), but logs
  showed the device never once acknowledged it. "Off" now sets colour to
  black (`0,0,0`) via the same `mode=0x02` command that's confirmed reliable.
  (A genuine non-colour "off" command may still exist — see the new debug
  topic below, added to help track it down properly.)
- **Turning back on required re-picking a colour.** A plain "on" (no colour
  attached) now restores the last real colour automatically, instead of
  needing the colour set explicitly every time.
- **Colour memory now works regardless of source.** Previously only colours
  set from Home Assistant were remembered; colours set via the hood's own
  physical panel are now tracked too, so "on" restores the right colour no
  matter which one last changed it.

### Added
- New debug MQTT topic (`cappa/kkt/debug/raw/set`) for injecting raw test
  frames directly — restores the ability to probe unmapped commands now that
  the bridge runs as a Supervisor add-on rather than an interactive Docker
  container.

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