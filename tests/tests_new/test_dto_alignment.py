from datetime import datetime as dt

from custom_components.ramses_cc.helpers import dto_to_dict, extract_demand
from ramses_rf.enums import ThermalMode
from ramses_rf.models import (
    ActuatorCycleDTO,
    BdrStateDTO,
    JimStateDTO,
    OpenThermCounters,
    OpenThermFlags,
    OpenThermStateDTO,
    OpenThermTemperatures,
    ThermalDemandDTO,
    UfhCircuitDemandDTO,
)


def test_extract_demand_primitives() -> None:
    """Test extract_demand with floats and None."""
    # Arrange & Act & Assert
    assert extract_demand(None) is None
    assert extract_demand(0.75) == 0.75
    assert extract_demand(0) == 0.0


def test_extract_demand_dto() -> None:
    """Test extract_demand with ThermalDemandDTO and UfhCircuitDemandDTO."""
    # Arrange
    dto1 = ThermalDemandDTO(thermal_demand=0.5, mode=ThermalMode.HEAT)
    dto2 = UfhCircuitDemandDTO(
        ufh_index="0", thermal_demand=0.8, mode=ThermalMode.HEAT
    )

    # Act & Assert
    assert extract_demand(dto1) == 0.5
    assert extract_demand(dto2) == 0.8


def test_dto_to_dict_conversion() -> None:
    """Test dto_to_dict conversion for extra_state_attributes serialization."""
    # Arrange
    dto = ThermalDemandDTO(thermal_demand=0.6, mode=ThermalMode.HEAT)
    dto_list = [
        UfhCircuitDemandDTO(
            ufh_index="1", thermal_demand=0.4, mode=ThermalMode.HEAT
        )
    ]
    bdr_dto = BdrStateDTO(modulation_level=0.5, actuator_enabled=True)
    jim_dto = JimStateDTO(
        ch_active=True,
        dhw_active=False,
        flame_active=True,
        cooling_active=False,
        actuator_enabled=True,
    )
    cycle_dto = ActuatorCycleDTO(
        actuator_countdown=30,
        cycle_countdown=120,
        actuator_enabled=True,
        modulation_level=0.75,
    )
    timestamp = dt(2026, 8, 30, 12, 0, 0)
    otb_dto = OpenThermStateDTO(
        flags=OpenThermFlags(
            flame_active=True,
            ch_active=True,
            dhw_active=False,
            cooling_active=False,
        ),
        temperatures=OpenThermTemperatures(
            boiler_output=65.5,
            boiler_return=45.0,
            dhw=55.0,
            outside=18.5,
        ),
        counters=OpenThermCounters(burner_starts=120, burner_hours=350),
        ch_water_pressure=1.8,
        dhw_flow_rate=0.0,
        max_rel_modulation=1.0,
        rel_modulation_level=0.45,
        oem_code="00",
        last_updated=timestamp,
    )

    # Act
    converted_dto = dto_to_dict(dto)
    converted_list = dto_to_dict(dto_list)
    converted_bdr = dto_to_dict(bdr_dto)
    converted_jim = dto_to_dict(jim_dto)
    converted_cycle = dto_to_dict(cycle_dto)
    converted_otb = dto_to_dict(otb_dto)

    # Assert
    assert converted_dto == {
        "thermal_demand": 0.6,
        "mode": "heat",
        "ufh_index": None,
        "domain_id": None,
    }
    assert converted_list == [
        {"ufh_index": "1", "thermal_demand": 0.4, "mode": "heat"}
    ]
    assert converted_bdr == {
        "modulation_level": 0.5,
        "actuator_enabled": True,
        "last_updated": None,
    }
    assert converted_jim == {
        "modulation_level": None,
        "actuator_enabled": True,
        "last_updated": None,
        "ch_active": True,
        "dhw_active": False,
        "flame_active": True,
        "cooling_active": False,
    }
    assert converted_cycle == {
        "actuator_countdown": 30,
        "cycle_countdown": 120,
        "actuator_enabled": True,
        "modulation_level": 0.75,
    }
    assert converted_otb["flags"]["flame_active"] is True
    assert converted_otb["flags"]["ch_active"] is True
    assert converted_otb["flags"]["dhw_active"] is False
    assert converted_otb["temperatures"]["boiler_output"] == 65.5
    assert converted_otb["temperatures"]["boiler_return"] == 45.0
    assert converted_otb["counters"]["burner_starts"] == 120
    assert converted_otb["counters"]["burner_hours"] == 350
    assert converted_otb["ch_water_pressure"] == 1.8
    assert converted_otb["rel_modulation_level"] == 0.45
    assert converted_otb["last_updated"] == timestamp
