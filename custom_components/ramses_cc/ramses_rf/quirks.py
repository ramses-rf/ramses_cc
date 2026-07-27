"""Hardware-specific quirks, overrides, and quarantine lists.

RAMSES RF - Protocol Quirks and Schema Exceptions.

This file documents hard-won reverse-engineering knowledge where real-world
Honeywell/Resideo hardware violates its own protocol schemas.
These rules were historically hard-coded into the L3 transport dispatcher.

KNOWN EXCEPTIONS:
1. Cross-Domain Routing:
   Devices matching `msg.src.type == msg.dst.type` where both are HEAT_DEVICES
   can sometimes legally communicate in the HVAC domain (e.g., 22F3 codes).

2. Controller Promotions:
   `DEV_TYPE_MAP.PROMOTABLE_SLUGS` is required because devices will occasionally
   transmit packets outside their standard verb schemas, requiring L7 to
   "promote" their device class dynamically.

3. Verb/Code Schema Violations:
   - CTL / RQ / 3EF1: Controllers are known to illegally request 3EF1.
   - BDR / RQ / 3EF0: BDR91 relays are known to illegally request 3EF0.
   - W_  / 0001: General exception to the rule where W_ is transmitted unexpectedly.

"""

from __future__ import annotations

from typing import Any, Final

from .const import DevType
from .models import HvacState
from ..ramses_tx.const import (
    SZ_FAN_MODE,
    SZ_INDOOR_HUMIDITY,
    SZ_OUTDOOR_HUMIDITY,
    SZ_REL_HUMIDITY,
    MsgId,
)

# Map of device types to sets of OpenTherm MsgIds that are known to be unreliable
QUARANTINED_OT_MSG_IDS: Final[dict[str, set[MsgId]]] = {
    DevType.OTB: {MsgId._0E, MsgId._11},
}


def apply_hvac_quirks(
    payload: dict[str, Any], current_state: HvacState | None, msg_code: str
) -> dict[str, Any]:
    """Resolve stateful FSM conflicts and structural anomalies for HVAC packets.

    Stateful quirks cannot be resolved by stateless parsers. They must be
    intercepted by comparing the incoming packet payload to the existing
    CQRS state immediately prior to hydration.

    :param payload: The flattened, canonical telemetry dictionary.
    :type payload: dict[str, Any]
    :param current_state: The existing Read-Model for the device, if any.
    :type current_state: HvacState | None
    :param msg_code: The hex opcode of the incoming message.
    :type msg_code: str
    :return: The safely mutated telemetry dictionary.
    :rtype: dict[str, Any]
    """
    mutated = dict(payload)

    # STRUCTURAL QUIRK: 12A0 Array Elements (Ventura V1x, Orcon, etc.)
    # The parser returns list elements with an 'hvac_idx'. We must map these
    # generic keys to their specific domain locations.
    # idx=00: indoor sensor  → indoor_humidity, indoor_temp
    # idx=01: supply sensor   → rel_humidity (parser key), supply_temp
    # idx=02: outdoor sensor  → outdoor_humidity, outdoor_temp
    #
    # parse_humidity_element returns different key names per idx:
    #   idx=00 → SZ_INDOOR_HUMIDITY ("indoor_humidity")
    #   idx=01 → SZ_REL_HUMIDITY ("rel_humidity")  — NOT "indoor_humidity"!
    #   idx=02 → SZ_OUTDOOR_HUMIDITY ("outdoor_humidity")
    #
    # HvacState has no supply_humidity field, so idx=01's rel_humidity is
    # dropped (not in the dispatcher's field list).  idx=02's outdoor_humidity
    # is already correct and needs no remapping.
    if msg_code == "12A0":
        idx = mutated.get("hvac_idx", "00")
        if idx == "00":
            if "temperature" in mutated:
                mutated["indoor_temp"] = mutated["temperature"]
        elif idx == "01":
            # parse_humidity_element returns "rel_humidity", not
            # "indoor_humidity" — the old code checked the wrong key.
            # There is no supply_humidity field in HvacState, so we
            # pop the key to prevent it overwriting indoor_humidity
            # from idx=00 via the dispatcher's "temperature" fallback.
            if SZ_REL_HUMIDITY in mutated:
                mutated.pop(SZ_REL_HUMIDITY)
            if "indoor_humidity" in mutated:  # safety: old parser path
                mutated.pop("indoor_humidity")
            if "temperature" in mutated:
                mutated["supply_temp"] = mutated.pop("temperature")
        elif idx == "02":
            # parse_humidity_element already returns outdoor_humidity for
            # idx=02, so no humidity remapping is needed.
            #
            # NOTE: idx=02 also includes a temperature field, but we do NOT
            # remap it to outdoor_temp.  The 12A0 array comes from a separate
            # HUM sensor, and the dispatcher routes it to the FAN's hvac_state
            # (as dst target).  If we remap temperature → outdoor_temp here,
            # it creates a second outdoor_temp source that conflicts with
            # 31DA's outdoor_temp, causing the sensor to bounce between the
            # two values every polling cycle.  31DA is the authoritative
            # source for outdoor_temp; 12A0 idx=02 only contributes
            # outdoor_humidity.  See ramses_cc#742.
            pass
        return mutated

    # QUIRK: 31DA humidity 0.0 → None (null-marker normalisation)
    # Some devices (e.g. Ventura V1x) send 0x00 for indoor/outdoor humidity in
    # 31DA when no sensor is present.  This parses as 0.0 (physically impossible
    # on Earth).  Normalise to None so both ingestion paths (dispatcher and
    # StateProjector) filter it out.  See ramses_cc#742.
    if msg_code == "31DA":
        for key in (SZ_INDOOR_HUMIDITY, SZ_OUTDOOR_HUMIDITY):
            if key in mutated and mutated[key] == 0.0:
                mutated[key] = None

    # QUIRK: 31D9 raw-hex fan_mode → None (semantic-value preservation)
    # For long-payload devices (Orcon, Brofer, etc.), the 31D9 parser sets
    # fan_mode = payload[4:6] — a raw hex byte like "04", "C8", "FF".  These
    # are NOT semantic names and conflict with the semantic fan_mode from
    # 22F4 ("off", "paused", "auto", "manual") or 22F1 (scheme-specific
    # names like "away", "low", "high", "boost").  The raw hex overwrites
    # the good semantic value every 31D9 broadcast cycle, causing fan_mode
    # to toggle (e.g. "auto" ↔ "04").
    #
    # Vasco/ClimaRad short payloads (msg.len == 3) are already converted to
    # semantic strings by the parser's _31D9_FAN_INFO_VASCO lookup, so they
    # won't match the hex pattern and are preserved.
    #
    # Drop any fan_mode that is a 2-char hex string (raw byte).  The
    # authoritative semantic fan_mode comes from 22F4 (polled) or 22F1
    # (command reply).  See ramses_cc issue 723.
    if msg_code == "31D9" and SZ_FAN_MODE in mutated:
        val = mutated[SZ_FAN_MODE]
        if isinstance(val, str) and len(val) == 2:
            try:
                int(val, 16)  # is it a raw hex byte?
                mutated[SZ_FAN_MODE] = None
            except ValueError:
                pass  # semantic string, keep it

    if not current_state:
        return mutated

    # QUIRK: Itho 31DA 'exhaust_fan_speed' Overwrite Prevention
    # Itho transmits actual fan speed in 31D9, but transmits 31DA with
    # a default zero byte [38:40]. We drop the zero if valid state exists.
    if msg_code == "31DA" and "exhaust_fan_speed" in mutated:
        if mutated["exhaust_fan_speed"] == 0.0:
            if (
                current_state.exhaust_fan_speed is not None
                and current_state.exhaust_fan_speed > 0
            ):
                mutated["exhaust_fan_speed"] = current_state.exhaust_fan_speed

    # QUIRK: 31DA 'fan_info' precedence over 22F1/22F4
    # 31DA snapshots include a fan_info byte that may be a null marker or an
    # unknown code for devices that report fan state via 22F1/22F4 instead.
    # We must not overwrite a valid, rich string from 22F1/22F4/31D9 with:
    #   - "" or "off"  (blank/null markers)
    #   - "-unknown 0xNN-"  (unrecognised codes, e.g. Ventura's 0x1F)
    if msg_code == "31DA" and "fan_info" in mutated:
        incoming = mutated["fan_info"]
        if incoming in ("off", "") or (
            isinstance(incoming, str) and incoming.startswith("-unknown")
        ):
            if (
                current_state.fan_info
                and current_state.fan_info
                not in (
                    "off",
                    "",
                )
                and not current_state.fan_info.startswith("-unknown")
            ):
                mutated["fan_info"] = current_state.fan_info

    # QUIRK: 31DA 'bypass_position' null-marker prevention
    # Some devices (e.g. Orcon) report bypass_position via 22F7, not
    # 31DA.  Their 31DA snapshot includes 0x00 for bypass_position, which
    # parses as 0.0 (a seemingly valid value).
    # We must not overwrite bypass_position from 22F7 with the 31DA null marker.
    # We identify these devices by checking another 22F7 field, e.g. bypass_mode.

    if msg_code == "31DA" and "bypass_position" in mutated:
        if mutated["bypass_position"] == 0.0:
            if (
                current_state.bypass_position != 0.0
                and current_state.bypass_mode is not None
                # would be set from 22F7
            ):
                mutated["bypass_position"] = current_state.bypass_position

    return mutated
