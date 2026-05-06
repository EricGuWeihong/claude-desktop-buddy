"""BLE NUS transport for the buddy daemon.

Runs bleak's async API in a dedicated daemon thread and exposes a
synchronous, select()-compatible interface via a self-pipe so the
existing daemon event loop needs zero structural changes.

NUS (Nordic UART Service) UUIDs match the firmware's ble_bridge.cpp.
"""
import asyncio
import concurrent.futures
import os
import queue
import threading
import time

from transport import Transport

# Nordic UART Service UUIDs (must match firmware ble_bridge.cpp).
NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX      = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # write target
NUS_TX      = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # notify source

_CONNECT_TIMEOUT = 15.0
_SCAN_TIMEOUT    = 8.0


_LOG_FILE = os.path.expanduser("~/.claude/buddy_ble.log")

def _log(msg):
    ts = time.strftime("%H:%M:%S")
    line = f"[buddy_ble] {ts} {msg}"
    print(line, flush=True)
    try:
        with open(_LOG_FILE, "a") as _lf:
            _lf.write(line + "\n")
    except Exception:
        pass


class BLETransport(Transport):
    """Synchronous wrapper around bleak async BLE NUS connection.

    Runs an asyncio event loop in a daemon thread. All bleak operations
    are dispatched via loop.call_soon_threadsafe() and synchronized
    with concurrent.futures.Future.
    """

    def __init__(self, name_prefix="Claude-", cached_address=None):
        self._name_prefix = name_prefix
        self._cached_address = cached_address
        self._client = None
        self._address = None
        self._loop = None
        self._thread = None
        self._rx_queue = queue.Queue()
        self._rpipe, self._wpipe = os.pipe()
        self._write_lock = threading.Lock()
        self._connected = False

    # ---------- async methods (run on BLE thread) ----------

    async def _scan_and_connect(self):
        from bleak import BleakScanner, BleakClient

        def _disconnect_callback(client):
            _log(f"BLE disconnected ({self._address})")
            self._connected = False

        # Fast path: try cached address first.
        if self._cached_address:
            try:
                _log(f"Trying cached address {self._cached_address}")
                self._client = BleakClient(
                    self._cached_address, timeout=_CONNECT_TIMEOUT,
                    disconnected_callback=_disconnect_callback,
                )
                await self._client.connect()
                if self._client.is_connected:
                    self._address = self._cached_address
                    _log(f"Cached address connected")
                    return True
            except Exception as e:
                _log(f"Cached address failed: {e}")
                self._client = None

        # Scan for devices advertising NUS.
        _log("Scanning for BLE devices...")
        devices = await BleakScanner.discover(
            timeout=_SCAN_TIMEOUT, service_uuids=[NUS_SERVICE],
        )
        for d in devices:
            if d.name and d.name.startswith(self._name_prefix):
                _log(f"Found {d.name} ({d.address})")
                self._client = BleakClient(
                    d, timeout=_CONNECT_TIMEOUT,
                    disconnected_callback=_disconnect_callback,
                )
                await self._client.connect()
                if self._client.is_connected:
                    self._address = d.address
                    _log(f"Connected to {d.name} ({d.address})")
                    return True

        _log("No matching BLE device found")
        return False

    async def _subscribe_notify(self):
        await self._client.start_notify(NUS_TX, self._on_tx_callback)
        mtu = self._client.mtu_size
        _log(f"Subscribed to TX notifications, MTU={mtu}")

    def _on_tx_callback(self, sender, data: bytearray):
        self._rx_queue.put_nowait(bytes(data))
        _log(f"RX: {len(data)} bytes, preview={bytes(data[:40])!r}")
        try:
            os.write(self._wpipe, b"\x01")
        except OSError:
            pass

    async def _write(self, data: bytes):
        await self._client.write_gatt_char(NUS_RX, data, response=True)

    async def _disconnect(self):
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass

    # ---------- sync interface ----------

    def _run_async(self, coro):
        """Dispatch async coro to BLE thread and wait for result."""
        future = concurrent.futures.Future()

        async def _wrap():
            try:
                result = await coro
                future.set_result(result)
            except Exception as e:
                future.set_exception(e)

        self._loop.call_soon_threadsafe(
            lambda: self._loop.create_task(_wrap()),
        )
        return future.result(timeout=_CONNECT_TIMEOUT)

    def _ble_event_loop(self, ready_event):
        """Entry point for the BLE thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        ready_event.set()  # signal that loop is ready
        self._loop.run_forever()

    def open(self) -> bool:
        """Start BLE thread, scan/connect, subscribe to notifications."""
        ready_event = threading.Event()
        self._thread = threading.Thread(
            target=self._ble_event_loop, args=(ready_event,), daemon=True,
        )
        self._thread.start()

        # Wait for the BLE thread's event loop to be running before
        # dispatching coroutines — otherwise call_soon_threadsafe calls
        # are lost (coroutine never awaited).
        ready_event.wait(timeout=5)

        try:
            ok = self._run_async(self._scan_and_connect())
            if not ok:
                return False
            self._run_async(self._subscribe_notify())
            self._connected = True
            return True
        except Exception:
            return False

    def close(self):
        """Disconnect and stop the BLE thread."""
        self._connected = False
        if self._loop and self._loop.is_running():
            try:
                self._run_async(self._disconnect())
            except Exception:
                pass
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5.0)
        try:
            os.close(self._rpipe)
        except OSError:
            pass
        try:
            os.close(self._wpipe)
        except OSError:
            pass

    def fileno(self) -> int:
        if not self._connected:
            return -1
        return self._rpipe

    def read(self, max_bytes: int) -> bytes:
        # Drain self-pipe wake byte.
        try:
            os.read(self._rpipe, 256)
        except OSError:
            pass
        # Drain all available items from the queue.
        result = bytearray()
        while len(result) < max_bytes:
            try:
                chunk = self._rx_queue.get_nowait()
                result.extend(chunk)
            except queue.Empty:
                break
        return bytes(result)

    def write(self, data: bytes):
        with self._write_lock:
            if self._loop and self._loop.is_running():
                try:
                    self._run_async(self._write(data))
                except Exception as e:
                    self._connected = False
                    _log(f"BLE write failed: {e}")

    def check_connected(self) -> bool:
        """Check actual BLE client state (detects stale connections)."""
        if self._client:
            try:
                actual = self._client.is_connected
                if not actual:
                    self._connected = False
                return actual
            except Exception:
                self._connected = False
                return False
        return self._connected

    @property
    def is_connected(self) -> bool:
        """Actively probe BLE client — don't trust cached state."""
        return self.check_connected()

    @property
    def port_name(self) -> str:
        return f"BLE {self._address}" if self._address else ""

    @property
    def address(self) -> str:
        return self._address
