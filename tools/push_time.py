"""Push current time to Claude Hardware Buddy over BLE.

Bypasses Claude Desktop entirely. Writes {"time":[epoch, tz_offset]}\\n
to the Nordic UART RX characteristic (6e400002-...).

Uses response=True (WRITE_REQ) so the ATT layer returns an ack/error —
response=False (WRITE_CMD) is silently dropped when the characteristic
requires encryption but the link isn't encrypted yet. Also subscribes
to the TX characteristic so firmware prints are visible here.

PREREQUISITE: Disconnect the device from Claude Desktop first (in the
Hardware Buddy panel), otherwise the central-collision will fail this
script's connect. Reconnect Claude Desktop after this script finishes.
"""
import asyncio, json, time, sys
from bleak import BleakScanner, BleakClient

NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX      = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX      = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

def _on_tx(_, data: bytearray):
    try:
        print(f"  [tx] {data.decode('utf-8', errors='replace').rstrip()}")
    except Exception:
        print(f"  [tx] {data!r}")

async def main():
    print("Scanning for Claude-XXXX buddy...")
    devices = await BleakScanner.discover(timeout=6.0, service_uuids=[NUS_SERVICE])
    target = None
    for d in devices:
        print(f"  found: {d.name!r}  {d.address}")
        if d.name and d.name.startswith("Claude-"):
            target = d
            break
    if not target:
        print("No Claude-* device found. Make sure:")
        print("  1. M5StickC is powered on (screen visible)")
        print("  2. Claude Desktop is DISCONNECTED from the buddy")
        sys.exit(1)

    print(f"\nConnecting to {target.name} ({target.address})...")
    async with BleakClient(target) as client:
        if not client.is_connected:
            print("Connect failed."); sys.exit(2)
        print("Connected.")

        # Subscribe to TX notifications to catch any firmware-side feedback.
        try:
            await client.start_notify(NUS_TX, _on_tx)
            print("Subscribed to TX notifications.")
        except Exception as e:
            print(f"  (could not subscribe to TX: {e})")

        # Give the link a moment to encrypt (macOS does this lazily on first
        # secured operation; subscribing to an encrypted CCCD above usually
        # kicks it off).
        await asyncio.sleep(1.0)

        # --- Diagnostic: force busy state ---
        # running=3 → derive() returns P_BUSY → Dragonite switches to the
        # busy animation (pulsing orange aura + 3 yellow stars at bottom).
        # This is visible even while the clock face is active because the
        # character sprite renders in the top half regardless.
        busy = json.dumps({"total": 5, "running": 3, "waiting": 0}) + "\n"
        print(f"\n[1/3] Force busy: {busy.strip()}")
        await client.write_gatt_char(NUS_RX, busy.encode("utf-8"), response=True)
        print("      ACKed. Dragonite should now BUSY animate (pulsing aura + stars).")
        await asyncio.sleep(4.0)

        # --- Release busy so clock can re-render ---
        idle = json.dumps({"total": 0, "running": 0, "waiting": 0}) + "\n"
        print(f"\n[2/3] Release: {idle.strip()}")
        await client.write_gatt_char(NUS_RX, idle.encode("utf-8"), response=True)
        await asyncio.sleep(1.0)

        # --- Time payload ---
        epoch = int(time.time())
        tz_offset = -time.timezone if time.daylight == 0 else -time.altzone
        payload = json.dumps({"time": [epoch, tz_offset]}) + "\n"
        print(f"\n[3/3] Time sync: {payload.strip()}")
        print(f"      local time: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        await client.write_gatt_char(NUS_RX, payload.encode("utf-8"), response=True)
        print("      Time ACKed.")

        print("\nDiagnostic readout:")
        print("  • Did Dragonite switch to BUSY (pulsing aura + yellow stars) during step 1?")
        print("      YES → JSON pipe works, RTC chip or library is the problem.")
        print("      NO  → BLE ACKs but bytes aren't reaching _applyJson.")
        print("  • Did clock face revert to expected %s after step 3?" % time.strftime('%H:%M'))
        await asyncio.sleep(2.0)

asyncio.run(main())
