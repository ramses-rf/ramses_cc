#!/usr/bin/env python3
"""RAMSES RF - a RAMSES-II protocol decoder & analyser."""

from ramses_rf import Device
from ramses_rf.binding_fsm import BindContext
from ramses_rf.device import Fakeable


def ensure_fakeable(dev: Device, make_fake: bool = True) -> None:
    """If a Device is not Fakeable (i.e. Fakeable, not _faked), make it so."""

    class _Fakeable(dev.__class__, Fakeable):
        pass

    if isinstance(dev, Fakeable):
        return

    dev.__class__ = _Fakeable
    assert isinstance(dev, Fakeable)

    setattr(dev, "_bind_context", BindContext(dev))  # noqa: B010

    if make_fake:
        dev._make_fake()
