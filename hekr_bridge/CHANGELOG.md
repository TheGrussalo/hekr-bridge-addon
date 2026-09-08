# Changelog

## 1.5.5

### Fixed
- **RGB Light entity could still show "on" in Home Assistant when genuinely
  dark, in one specific case the 1.5.3/1.5.4 fix missed.** `inject_rgb()`
  stamped `last_rgb_cmd_at` (the "we recently sent a genuine RGB command,
  trust the next byte8 rise" grace window) on *any* RGB command, including
  `OFF`. Reproduced directly: an RGB-off command correctly turned RGB off,
  but ~2.8 seconds later a completely unrelated light-only command caused
  the usual spurious `byte8->2` bystander flip - and because the earlier
  RGB-off command's timestamp was still within the 5-second grace window,
  `resolve_rgb_on()` mistook that stale timestamp for justification and
  trusted the rise, when an OFF command can never explain a rise to `2` in
  the first place.
  - `last_rgb_cmd_at` is now only stamped for genuine ON-mode RGB commands
    (`mode=0x02`/`0x03`) - an OFF command no longer extends this window at
    all, so a subsequent unrelated byte8 rise can't piggyback on it.
  - Verified against the exact real sequence that exposed this (RGB-off,
    then an unrelated light-on ~2.8s later), plus the full existing test
    suite, both via direct unit tests and a full module import/run.

## 1.5.4

### Fixed
- **Every real device session was crashing immediately after the first
  status frame, dropping straight back to disconnected.** Regression in the
  1.5.3 release above: an editing mistake while refining `resolve_rgb_on()`
  accidentally deleted the `def state_diff(new):` line while leaving its
  body behind. The orphaned body was syntactically valid Python (absorbed
  as unreachable code at the end of `resolve_rgb_on()`), so it passed a
  syntax check cleanly - but `state_diff` no longer existed as a callable
  function anywhere in the module. `analyze()` calls `state_diff()` on
  every single status frame, so every real device session hit
  `NameError: name 'state_diff' is not defined` and was torn down
  immediately (`[dev->cloud] device read error, ending session`) - local
  MQTT control and cloud relay were both unaffected by anything in this
  release's actual RGB logic, since the crash happened immediately
  afterwards, before any of it mattered.
  - Restored the missing `state_diff` function definition.
  - Verified this time by actually importing and running the module against
    a real captured frame end-to-end, not just a syntax check - a syntax
    check alone cannot catch a function silently disappearing into dead
    code like this.

## 1.5.3

### Fixed
- **RGB Light entity showed "on" in Home Assistant when the strip was
  genuinely dark.** Root-caused through direct, methodical physical testing
  against the hood on 2026-09-08 (isolating fan, light, and RGB in turn,
  both via HA commands and genuine panel button presses) - status byte 8
  isn't a clean, standalone RGB-on indicator; it also changes as a side
  effect of unrelated commands, and correct handling differs by what
  triggered it:
  - **Fan/speed/power changes never legitimately touch RGB.** Confirmed
    twice: once via an HA-triggered power-on, once via a genuine physical
    fan-button press (speed cycled 0->1->2->3->1->0 with the RGB strip
    confirmed dark throughout) - `byte8` still rose to `2` on fan-on and
    never dropped back even once the fan turned off again. A `byte8->2`
    alongside a `speed`/`byte5` change is now always treated as spurious,
    regardless of whether the command came from us or the panel.
  - **Light-button changes are more subtle.** The physical Light button on
    the hood genuinely cycles white and RGB together as one 4-state action
    (confirmed directly: press 1 = both on, press 2 = white only, press 3 =
    RGB only, press 4 = both off), so a panel-driven light change can
    legitimately carry a real RGB change and should be trusted. But when
    the light change instead follows *our own* light-only command (`cmdId
    0x03` alone, which cannot invoke the panel's combined behaviour),
    `byte8` still spuriously rose to `2` in testing - so that specific case
    is still distrusted, tracked via a 5-second window after our own
    light-only commands.
  - A `byte8->2` with nothing else changed (or following one of our own
    genuine RGB commands within the last few seconds) is trusted as a real,
    standalone RGB change.
  - A transition toward `0` or `1` (implying off) is always trusted
    regardless - the failure mode there is at worst reporting off while
    it's still genuinely on, which is the safe direction, not the false-on
    this fix targets.
  - `rgb_on` is now included in the state-change diff log, so its corrected
    value is visible distinctly from the raw `byte8` field going forward.
  - An earlier version of this fix (same day) treated *any* accompanying
    light change as spurious, which would have wrongly suppressed the
    panel's genuine combined light+RGB button behaviour - corrected before
    release once that behaviour was confirmed through direct testing.

## 1.5.2

### Fixed
- **Supervisor's own TCP watchdog probe was being mistaken for the hood
  connecting, causing every entity to flicker Unavailable roughly every 2
  minutes.** `config.yaml`'s `watchdog: "tcp://[HOST]:83"` (added in 1.5.1)
  makes Supervisor open a plain TCP connection to the bridge's listen port
  on a timer to confirm the process is alive, then close it immediately
  without sending anything - completely normal for a TCP health check.
  `handle_device` didn't distinguish that from the real hood connecting: it
  treated *any* incoming connection as a full device session, immediately
  publishing MQTT availability online, then straight back offline the
  instant the empty probe closed. Traceable by source address in the
  logs - probes come from `172.30.32.x` (Supervisor's internal Docker
  network), the real hood connects from its actual LAN address
  (`192.168.1.72`).
  - `handle_device` now waits (up to 5s) for the peer to actually send
    data before treating the connection as a real device session at all.
    An empty read (immediate EOF, as the watchdog probe does) now closes
    quietly - no log line, no MQTT availability change, no cloud
    connection opened, no dashboard flicker.
  - The first real chunk of data is no longer discarded by this check -
    it's threaded through to `dev_to_cloud` as `prefetched` and processe

## 1.5.1

### Fixed
- **Cloud-relay failures could silently kill local control for hours,
  requiring a manual add-on restart to recover.** Root cause: `handle_device`
  ran the device-side and cloud-side relay as two tasks under
  `asyncio.wait(..., return_when=FIRST_COMPLETED)` — if the connection to
  the real Hekr cloud (`hub.hekreu.me:83`) failed or reset for *any* reason,
  the whole device session was torn down, including the local MQTT control
  path, even though that path never actually depended on the cloud leg.
  Traced to a real incident on 2026-08-31: a Home Assistant Core restart
  left this process's cloud-side connection broken without crashing the
  process itself, so Supervisor's watchdog (also enabled as of this
  release — see below) never kicked in, and every device session from then
  on died instantly with `[dev->cloud] forward error: Connection reset by
  peer` until the add-on was manually restarted three days later.
  - Device reads, state decoding (`analyze()`), and MQTT publishing are now
    fully independent of the cloud connection's health.
  - Relaying to the real Hekr cloud is now best-effort with its own
    reconnect loop (capped exponential backoff, 2s–30s) — a cloud-side
    failure logs a warning and retries indefinitely; it no longer touches
    the device session at all.
  - The device session now ends **only** when the device itself
    disconnects. The `Connected` binary sensor, which already only ever
    tracked device-side availability, is now actually accurate under
    cloud-side failures instead of going stale along with everything else.
  - Supervisor watchdog enabled in `config.yaml` (`watchdog: true`), so a
    genuine process crash now triggers an automatic restart regardless.

## 1.5.0

### Fixed
- **Power icon no longer falsely shows "on" when only the white light is
  on.** `cmdId 0x02` (Power) only ever has an effect when the fan is
  actually running — confirmed during the RGB investigation — so the
  entity now reflects fan speed alone, not light state.

### Changed
- **RGB's "last known colour" is now read directly from the hood's own
  status frame**, not cached separately by the add-on. The device reports
  its stored R/G/B on every status update regardless of on/off state, so
  this is simpler and always accurate than maintaining a local copy —
  automatically survives add-on restarts with no file needed, and stays
  in sync with colour changes made from the physical panel too.

## 1.4.0

### Fixed
- **Genuine RGB off, correctly solved this time.** The previous release
  (1.5.0) concluded that `mode=0x00` plus a `cmdId 0x03` (Light) toggle
  gave clean, fan-free RGB control. That was wrong — the status flag
  genuinely toggled cleanly, but the colour was never actually visible on
  that path, only confirmed by properly checking the physical hood rather
  than trusting the status byte alone.
  - **Final, verified design:** `mode=0x02` (cmdId 0x07) genuinely drives
    the RGB hardware — turning on always sends this with the target
    colour. `mode=0x00` genuinely turns RGB output off, confirmed dark
    including the physical panel indicator, while remembering the colour
    for next time. Both are sent directly, with **no reset, no fan
    involvement, and no black-colour workaround needed at any point.**
  - **`cmdId 0x03` (Light) is not involved in RGB on/off at all** — it
    turned out to be a red herring throughout this investigation; it only
    ever controls the independent white channel, in both directions.
  - On/off status shown in Home Assistant reads directly from status byte
    8 again, which is fully reliable under this design.
  - Removed: the fan-blip/Power-off reset logic and its associated
    adaptive fallback-to-black handling introduced in 1.5.0 — none of it
    is needed.

This closes out the RGB on/off investigation for real this time, confirmed
by direct visual checks against the physical hood at every step, including
two full repeated on/off cycles with no reset in between. No known
limitations remain in the project.

## 1.3.0

### Fixed
- **Genuine RGB off, fully solved — no more black-colour workaround.** After
  extensive investigation (including a systematic sweep testing every
  candidate command ID as a possible "unlock" step, per user's excellent
  suggestion to test whether unmapped commands sent *after* a colour change
  specifically unlocked Light-off, rather than just testing them in
  isolation), the actual missing piece turned out to be the colour-set
  command's own `mode` byte:
  - `mode=0x00` stores a colour and leaves the device **genuinely off**
    (status byte 8 -> 1, physical panel indicator included) — this had
    previously been assumed to behave identically to `mode=0x02` and was
    never distinguished from it.
  - `mode=0x02` stores a colour *and* turns it on, but doing so permanently
    "stickies" the RGB status flag — after using it, plain Light on/off
    (`cmdId 0x03`) silently stops cascading RGB, which is what drove the
    whole earlier black-colour/fan-blip investigation in the first place.
  - New colour-change sequence: `mode=0x00` (store) immediately followed by
    `cmdId 0x03` value `1` (display). From then on, plain on/off uses
    `cmdId 0x03` alone — no fan involvement, no colour resend needed (the
    device remembers its own last colour).
  - **Removed entirely:** the black-colour (`0,0,0`) off workaround, the
    fan-blip/Power-off reset trick, and the associated
    `last_nonzero_rgb`/"sticky colour" tracking code — none of it is needed
    anymore.
  - On/off status shown in Home Assistant now reads directly from status
    byte 8 again (safe now that it's reliably and predictably toggled),
    instead of being inferred from whether the colour is black.

This closes out the last open item from the entire reverse-engineering
project. No known limitations remain.

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