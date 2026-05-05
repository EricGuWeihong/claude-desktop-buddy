"""USB serial transport for the buddy daemon.

Extracted from buddy_daemon.py's pyserial logic to satisfy the
Transport interface.
"""
import glob
from transport import Transport


def find_serial_port():
    """Return the first /dev/cu.usbmodem* path, or None."""
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    return ports[0] if ports else None


class USBTransport(Transport):
    """USB serial transport using pyserial."""

    SERIAL_BAUD = 115200

    def __init__(self):
        self._ser = None
        self._port = None

    def open(self) -> bool:
        import serial
        self._port = find_serial_port()
        if not self._port:
            return False
        self._ser = serial.Serial(
            self._port, self.SERIAL_BAUD, timeout=0,
            write_timeout=0,
        )
        return True

    def close(self):
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        self._port = None

    def fileno(self) -> int:
        return self._ser.fileno() if self._ser else -1

    def read(self, max_bytes: int) -> bytes:
        if not self._ser:
            return b""
        data = self._ser.read(max_bytes)
        # Drain remaining (matches existing daemon behavior).
        while True:
            try:
                more = self._ser.read(max_bytes)
                if not more:
                    break
                data += more
            except Exception:
                break
        return data

    def write(self, data: bytes):
        if self._ser:
            self._ser.write(data)

    @property
    def is_connected(self) -> bool:
        return self._ser is not None

    @property
    def port_name(self) -> str:
        return self._port or ""
