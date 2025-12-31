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

        # Accept generic *args and **kwargs to handle any constructor signature
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__()

            # Extract protocol (usually the first arg)
            if args:
                self._protocol = args[0]
            else:
                self._protocol = kwargs.get("protocol")

            # Look for 'callback' (used by broker.py) OR 'io_writer'
            self._io_writer = kwargs.get("callback") or kwargs.get("io_writer")

            # Fallback: check 2nd arg if it exists
            if not self._io_writer and len(args) > 1:
                self._io_writer = args[1]

            self.extra = kwargs.get("extra", {})

        async def write_frame(self, frame: str) -> None:
            if self._io_writer:
                await self._io_writer(frame)

        def get_extra_info(self, name: str, default: Any = None) -> Any:
            return self.extra.get(name, default)

        # FIX: Added **kwargs to accept 'dtm' and other optional args
        def receive_frame(self, frame: str, **kwargs: Any) -> None:
            if self._protocol and hasattr(self._protocol, "data_received"):
                self._protocol.data_received(frame)

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
