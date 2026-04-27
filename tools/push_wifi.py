"""Push WiFi credentials + timezone to Claude Hardware Buddy over BLE.

Writes {"wifi":{"ssid":"...","pass":"...","tz":<sec>}}\\n to the Nordic
UART RX characteristic. Firmware saves to NVS and (re)connects. On next
power-up the device syncs time via NTP automatically — no Claude Desktop
or push_time.py needed.

Usage:
    python3 push_wifi.py "MyNetwork" "MyPassword"
    python3 push_wifi.py           # prompts interactively

Timezone: auto-detected from Mac system clock via time.timezone.

PREREQUISITE: Disconnect the device from Claude Desktop first (in the
Hardware Buddy panel). Run from Terminal.app for Bluetooth permission.
"""
import asyncio, json, time, sys, getpass
from bleak import BleakScanner, BleakClient

NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX      = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX      = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

def _on_tx(_, data: bytearray):
    try:
        print(f"  [tx] {data.decode('utf-8', errors='replace').rstrip()}")
    except Exception:
        print(f"  [tx] {data!r}")

async def main(ssid: str, password: str):
    tz_offset = -time.timezone if time.daylight == 0 else -time.altzone
    print(f"WiFi: {ssid!r}")
    print(f"  tz_offset: {tz_offset} sec ({tz_offset//3600:+d}:{abs(tz_offset)%3600//60:02d})")

    print("\nScanning for Claude-XXXX buddy...")
    devices = await BleakScanner.discover(timeout=6.0, service_uuids=[NUS_SERVICE])
    target = None
    for d in devices:
        print(f"  found: {d.name!r}  {d.address}")
        if d.name and d.name.startswith("Claude-"):
            target = d
            break
    if not target:
        print("No Claude-* device found. Make sure it's powered on and")
        print("disconnected from Claude Desktop.")
        sys.exit(1)

    print(f"\nConnecting to {target.name} ({target.address})...")
    async with BleakClient(target) as client:
        if not client.is_connected:
            print("Connect failed."); sys.exit(2)
        print("Connected.")

        try:
            await client.start_notify(NUS_TX, _on_tx)
            print("Subscribed to TX notifications.")
        except Exception as e:
            print(f"  (could not subscribe to TX: {e})")

        await asyncio.sleep(1.0)

        payload = json.dumps({
            "wifi": {"ssid": ssid, "pass": password, "tz": tz_offset}
        }) + "\n"
        # Don't log the password contents.
        print(f"\nSending: wifi creds for {ssid!r} + tz={tz_offset}")
        await client.write_gatt_char(NUS_RX, payload.encode("utf-8"), response=True)
        print("Payload ACKed. Firmware will now connect + NTP sync.")
        print("Watch [tx] lines below for progress (connect, synced, etc):")

        # Hang around longer so we can observe the full connect+NTP cycle.
        await asyncio.sleep(15.0)
        print("\nDone. Creds are persisted to NVS — subsequent power-ups")
        print("will auto-sync time without needing this script again.")

if __name__ == "__main__":
    if len(sys.argv) == 3:
        ssid, password = sys.argv[1], sys.argv[2]
    elif len(sys.argv) == 1:
        ssid = input("SSID: ").strip()
        password = getpass.getpass("Password: ")
    else:
        print('Usage: python3 push_wifi.py ["SSID" "PASSWORD"]')
        sys.exit(64)
    if not ssid:
        print("SSID is required."); sys.exit(64)
    asyncio.run(main(ssid, password))
