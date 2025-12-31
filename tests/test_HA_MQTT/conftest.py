"""Global test configuration for HA MQTT tests."""

import sys
from typing import Any
from unittest.mock import MagicMock

# Attempt to import the transport module
try:
    import ramses_tx.transport
except ImportError:
    # If the module doesn't exist at all, mock it
    ramses_tx = MagicMock()
    sys.modules["ramses_tx"] = ramses_tx
    sys.modules["ramses_tx.transport"] = MagicMock()

# Check if the class is missing (Older ramses_rf version in CI)
if not hasattr(ramses_tx.transport, "CallbackTransport"):
    print("PATCHING: Injecting missing CallbackTransport into ramses_tx.transport")

    class MockCallbackTransport(MagicMock):
        """Mock transport to satisfy broker.py imports."""

        def __init__(self, protocol: Any, io_writer: Any, **kwargs: Any) -> None:
            super().__init__()
            self._protocol = protocol
            self._io_writer = io_writer
            self.extra = kwargs.get("extra", {})

        async def write_frame(self, frame: str) -> None:
            await self._io_writer(frame)

        def get_extra_info(self, name: str, default: Any = None) -> Any:
            return self.extra.get(name, default)

        # --- FIX: Add missing transport methods ---
        def pause_reading(self) -> None:
            pass

        def resume_reading(self) -> None:
            pass

        def close(self) -> None:
            pass

        def abort(self) -> None:
            pass

    # Inject the mock class into the real module
    ramses_tx.transport.CallbackTransport = MockCallbackTransport
