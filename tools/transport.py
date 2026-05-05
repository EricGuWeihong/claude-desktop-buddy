"""Transport abstraction for the buddy daemon.

Both USB serial and BLE NUS satisfy this interface so the daemon's
main loop can be transport-agnostic.
"""


class Transport:
    """Common interface for USB serial and BLE NUS transports."""

    def open(self) -> bool:
        """Connect to the device. Returns True on success."""
        raise NotImplementedError

    def close(self):
        """Disconnect and release resources."""
        raise NotImplementedError

    def fileno(self) -> int:
        """File descriptor for select().

        Must become readable when data arrives from the device.
        """
        raise NotImplementedError

    def read(self, max_bytes: int) -> bytes:
        """Read up to max_bytes. Non-blocking."""
        raise NotImplementedError

    def write(self, data: bytes):
        """Write data to the transport. Blocking."""
        raise NotImplementedError

    @property
    def is_connected(self) -> bool:
        raise NotImplementedError

    @property
    def port_name(self) -> str:
        """Human-readable identifier (e.g. '/dev/cu.usbmodem1234' or 'BLE Claude-A1B2')."""
        raise NotImplementedError
