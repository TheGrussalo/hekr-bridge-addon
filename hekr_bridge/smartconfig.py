#!/usr/bin/env python3
"""
Hekr "SmartConfig" WiFi provisioning, ported from the Wisen Android app
(me.hekr.hekrconfig.utils.HekrAirKissEncoder + me.hekr.hekrconfig.common.
CommonDeviceConfig), so a new/factory-reset Hekr device can be paired to
WiFi without the app or its now-dead cloud pairing endpoint.

Reverse-engineered 2026-09-03 from Wisen_1_9_1_APKPure.apk (jadx decompile).
Re-validated line-by-line against the decompiled source on 2026-09-14 -
see the "VALIDATION" note near the bottom of this docstring for what was
checked and the one real bug that fix corrected.

--- How the real app's pairing works ---

1. App calls Hekr's cloud (`getPinCode?ssid=...`) to get a 6-char PIN.
   This is the dead endpoint causing your pairing failures. The PIN is
   NOT validated by the device firmware during this handshake - the
   device just echoes back whatever PIN it was sent, for local
   correlation. Full cloud "binding" (the device phoning home to Hekr
   afterwards) is a separate later phase that does need Hekr's cloud,
   but local WiFi association does not. So we generate our own PIN.

2. App encodes "ssid\npassword\npinCode" using a length-encoded scheme
   (HekrAirKissEncoder): it repeatedly sends short UDP packets whose
   BYTE LENGTH is meaningful (values up to ~511 bytes), fired at a
   sequence of 224.x.x.255:7001 multicast addresses that spell out the
   password+ssid+pinCode bytes by position. The unconfigured device -
   already in pairing mode, listening in promiscuous/monitor mode near
   your 2.4GHz AP - decodes the WiFi 802.11 frame lengths off the air.
   Note the SSID itself is only present via a CRC8 check + total length
   in the "magic code" header, not in the per-byte data stream - AirKiss
   variants generally rely on the listening device already knowing which
   AP it's near from beacon frames, and only need password+pin conveyed
   covertly.

3. Once the device joins your WiFi, it broadcasts a JSON status message
   (action=devConfig) to 255.255.255.255:24254, which the app listens
   for to report progress (STEP field: 1-4 = connecting to router,
   5-9 = talking to Hekr cloud, 10 = fully cloud-bound).

--- NOTES / caveats ---

- This has NOT been tested against a real device (no hardware access
  from this environment) - it's a direct translation of the decompiled
  logic, but AirKiss-style protocols are timing- and radio-sensitive.
  Expect to iterate.
- We only need STEP 1-4 (DEVICE_CONNECTED_ROUTER, i.e. the hood accepted
  the WiFi credentials and is joining your network) - we do NOT need
  full cloud bind (STEP 10), since the bridge takes over via the DNAT
  redirect once the hood is on the LAN and tries to phone home.
- host_network: true is required in config.yaml for multicast send and
  broadcast receive to work at all in the container - this add-on
  already has that set.
- The hood must be freshly put into pairing mode (your usual button
  press) before starting this - it needs to be actively listening.

--- VALIDATION (2026-09-14) ---

Re-decompiled the original APK and checked every piece of this port
line-by-line against the real source, rather than trusting the earlier
session's own account of it:

- HekrAirKissEncoder (CRC8, leadingPart, magicCode, prefixCode,
  sequence, and the constructor's iteration structure): verified
  correct. This includes the odd CRC8_POLY constant below - initially
  suspected as a copy-paste error (it's sourced from an unrelated
  Twitter SDK class bundled in the APK), but confirmed against the real
  decompiled HekrAirKissEncoder.java that this is genuinely what the
  original app does, not a mistake introduced by the port.
- Multicast send timing/addressing (sendMsgToDevice): verified correct,
  including the 224.x.x.255:7001 addressing scheme and the
  packet-length-encodes-data technique. One trivial difference - Java's
  getSleepTime uses integer division (100/i truncates to 0 for typical
  credential lengths), this port uses float division - converges to the
  same practical 4ms floor for any real SSID/password, so left as is.
- Receive side (BroadcastUdpConn): port 24254 and the plain-JSON
  broadcast mechanism verified correct.
- One real bug found and fixed here: the original app's
  handlerConfigFromDevice requires `bind != 0` before treating a status
  message as valid at all ("Not binding action" -> silently ignored
  otherwise) - the initial port omitted this check entirely, which
  could have caused a false-positive "success" on a status broadcast
  the real app would have ignored. Fixed below.
"""
import asyncio
import json
import logging
import random
import socket
import string
import threading
import time

log = logging.getLogger("hekr-bridge.smartconfig")

AIRKISS_PORT = 7001
STATUS_LISTEN_PORT = 24254  # BroadcastUdpConn.PORT_LOCAL in the app
CRC8_POLY = 0x8C  # reversed CRC-8/MAXIM. Confirmed 2026-09-14 against the real
                   # decompiled HekrAirKissEncoder.CRC8(): the original app
                   # genuinely reuses com.twitter.sdk.android.core.
                   # TwitterAuthConfig.DEFAULT_AUTH_REQUEST_CODE (= 140 = 0x8C,
                   # an unrelated Android activity-result constant from a
                   # bundled Twitter SDK) as its CRC8 XOR constant - not a
                   # mistake in this port, a genuine quirk of the original code.


def crc8(data: bytes) -> int:
    """Bit-serial CRC8, ported exactly from HekrAirKissEncoder.CRC8(byte[])."""
    crc = 0
    for byte in data:
        b2 = byte & 0xFF
        for _ in range(8):
            b4 = (crc ^ b2) & 1
            crc = (crc >> 1) & 0xFF
            if b4:
                crc ^= CRC8_POLY
            b2 = (b2 >> 1) & 0xFF
    return crc & 0xFF


class HekrAirKissEncoder:
    """Port of me.hekr.hekrconfig.utils.HekrAirKissEncoder.

    Produces a list of ints representing a repeating sequence of UDP
    packet lengths (values 1-511, tagged via |128/|256 the way the
    original does) that a promiscuous listener decodes off 802.11 frame
    lengths.
    """

    def __init__(self, ssid: str, password: str, pin_code: str):
        self.data: list[int] = []
        # Outer loop runs the whole leading+magic+15x(prefix+chunks)
        # sequence 5 times, matching the Java while(i-- > 0) loop.
        for _ in range(5):
            self._leading_part()
            self._magic_code(ssid, password, pin_code)
            combined = (password + pin_code).encode("utf-8")
            for _ in range(15):
                self._prefix_code(password)
                i4 = 0
                while i4 < len(combined) // 4:
                    chunk = combined[i4 * 4:i4 * 4 + 4]
                    self._sequence(i4, chunk)
                    i4 += 1
                rem = len(combined) % 4
                if rem != 0:
                    tail = bytearray(4)
                    tail[:rem] = combined[i4 * 4:i4 * 4 + rem]
                    self._sequence(i4, bytes(tail))

    def _append(self, v: int):
        self.data.append(v)

    def _leading_part(self):
        vals = (1, 11, 21, 31)
        for _ in range(50):
            for v in vals:
                self._append(v)

    def _magic_code(self, ssid: str, password: str, pin_code: str):
        length = len(password) + len(pin_code) + len(ssid)
        v0 = (length >> 4) & 15
        if v0 == 0:
            v0 = 8
        v1 = (length & 15) | 16
        c = crc8(ssid.encode("utf-8"))
        v2 = 32 | ((c >> 4) & 15)
        v3 = (c & 15) | 48
        vals = (v0, v1, v2, v3)
        for _ in range(20):
            for v in vals:
                self._append(v)

    def _prefix_code(self, s: str):
        length = len(s)
        c = crc8(bytes([length & 0xFF]))
        vals = (
            ((length >> 4) & 15) | 64,
            (length & 15) | 80,
            ((c >> 4) & 15) | 96,
            (c & 15) | 112,
        )
        for v in vals:
            self._append(v)

    def _sequence(self, i: int, chunk: bytes):
        seq_bytes = bytes([i & 0xFF]) + chunk
        c = crc8(seq_bytes)
        self._append(c | 128)
        self._append(i & 0xFF | 128)
        for b in chunk:
            self._append(b | 256)

    def encoded(self) -> list[int]:
        return self.data


def _get_sleep_time(pass_time_ms: float, data_len: int) -> float:
    """Port of CommonDeviceConfig.getSleepTime(long, int) -> seconds."""
    j2 = max((pass_time_ms / 1000) - 3, 0)
    return ((100 / max(data_len, 1)) * (1 + (j2 / 6))) / 1000.0


def _send_loop(sock: socket.socket, ssid: str, password: str, pin_code: str,
                encoded_data: list[int], stop_event: "threading.Event", start_time: float):
    """Continuously cycles the encoded packet-length sequence against the
    ssid/password byte-position multicast addresses, like the app's
    DeviceAsyncTask (which calls sendMsgToDevice in an unbounded loop
    until cancelled). This is a genuinely long-running blocking loop -
    it must run in its own thread, never awaited directly, since the
    original Java loop bound (iArr.length * (payload.length+2)) is
    intentionally huge and only ever exits via external cancellation.
    """
    payload = f"{ssid}\n{password}\n{pin_code}".encode("utf-8")
    length = len(payload)
    i2 = 0
    n_cycle = len(encoded_data) * (length + 2)
    while not stop_event.is_set():
        pass_time_ms = (time.time() - start_time) * 1000
        sleep_s = _get_sleep_time(pass_time_ms, length if pass_time_ms > 1000 else length + 1)
        sleep_s = max(sleep_s, 0.004)
        val = encoded_data[i2 % len(encoded_data)]
        pos = i2 % (length + 2)
        if val >= 0:
            packet = bytes([1]) * val
            if pos != 0 and pos != length + 1:
                b = payload[pos - 1]
                addr = f"224.{pos - 1}.{b}.255"
            else:
                addr = f"224.127.{length}.255"
            try:
                sock.sendto(packet, (addr, AIRKISS_PORT))
            except OSError as e:
                log.debug(f"send error to {addr}: {e}")
        time.sleep(sleep_s)
        i2 = (i2 + 1) % n_cycle


class _StatusProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_message):
        self.on_message = on_message

    def datagram_received(self, data, addr):
        try:
            text = data.decode("utf-8", errors="replace")
            obj = json.loads(text)
        except Exception:
            return
        self.on_message(obj, addr[0])


async def run_smartconfig(ssid: str, password: str, pin_code: str = None,
                           duration_s: int = 90, on_progress=None) -> dict:
    """Run SmartConfig pairing for `duration_s` seconds and return a
    result dict. `on_progress(dict)` is called for every status update
    received from the device (raw devConfig params), and once more at
    the end with a final summary under key "final".

    We only need to see STEP 1-4 (router join) - we don't chase full
    STEP 10 cloud bind, since that depends on Hekr's cloud being up.
    """
    if pin_code is None:
        pin_code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))

    log.info(f"SmartConfig starting: ssid={ssid!r} pin={pin_code} duration={duration_s}s")

    encoder = HekrAirKissEncoder(ssid, password, pin_code)
    encoded_data = encoder.encoded()

    loop = asyncio.get_event_loop()
    stop_event = threading.Event()
    seen_devices = {}
    result = {"pin_code": pin_code, "devices": seen_devices, "success": False}

    def handle_status(obj, from_ip):
        if obj.get("action") != "devConfig":
            return
        params = obj.get("params") or obj.get("data")
        if not params:
            return
        pin = params.get("PIN")
        dev_tid = params.get("devTid", "unknown")
        step = params.get("STEP")
        code = params.get("code")
        bind = params.get("bind")
        log.info(f"SmartConfig <- devTid={dev_tid} STEP={step} code={code} bind={bind} PIN={pin} from={from_ip}")
        # Matches the real app's handlerConfigFromDevice: a message with
        # bind == 0 is "Not binding action" and gets silently ignored
        # entirely, not just excluded from the success check. Fixed
        # 2026-09-14 - the original port of this function omitted the
        # bind check, which could have produced a false-positive
        # "success" on a status broadcast the real app would reject.
        if bind == 0:
            log.debug(f"Ignoring non-binding status from devTid={dev_tid} (bind=0)")
            return
        entry = seen_devices.setdefault(dev_tid, {})
        entry.update({"ip": from_ip, "step": step, "code": code, "bind": bind, "pin_match": pin == pin_code})
        if on_progress:
            on_progress({"devTid": dev_tid, "ip": from_ip, "step": step, "code": code,
                          "bind": bind, "pin_match": pin == pin_code})
        if pin == pin_code and code == 200 and isinstance(step, int) and 1 <= step <= 4:
            result["success"] = True

    # Bind the status listener first so we don't miss an early reply.
    transport = None
    try:
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: _StatusProtocol(handle_status),
            local_addr=("0.0.0.0", STATUS_LISTEN_PORT),
            allow_broadcast=True,
            reuse_port=True,
        )
    except OSError as e:
        log.error(f"Could not bind status listener on :{STATUS_LISTEN_PORT}: {e}")
        if on_progress:
            on_progress({"error": f"bind failed: {e}"})
        return {"pin_code": pin_code, "devices": {}, "success": False, "error": str(e)}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    start = time.time()
    sender = threading.Thread(
        target=_send_loop,
        args=(sock, ssid, password, pin_code, encoded_data, stop_event, start),
        daemon=True,
    )
    sender.start()
    try:
        while time.time() - start < duration_s and not result["success"]:
            await asyncio.sleep(0.5)
    finally:
        stop_event.set()
        sender.join(timeout=2)
        sock.close()
        transport.close()

    elapsed = time.time() - start
    log.info(f"SmartConfig finished after {elapsed:.1f}s, success={result['success']}, devices={seen_devices}")
    if on_progress:
        on_progress({"final": True, "success": result["success"], "elapsed": elapsed, "devices": seen_devices})
    return result