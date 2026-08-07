from custom_components.ramses_cc.helpers import dto_to_dict, extract_demand
from ramses_rf.enums import ThermalMode
from ramses_rf.models import ActuatorStateDTO, ThermalDemandDTO, UfhCircuitDemandDTO


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
    dto2 = UfhCircuitDemandDTO(ufx_idx="0", thermal_demand=0.8, mode=ThermalMode.HEAT)

    # Act & Assert
    assert extract_demand(dto1) == 0.5
    assert extract_demand(dto2) == 0.8


def test_dto_to_dict_conversion() -> None:
    """Test dto_to_dict conversion for extra_state_attributes serialization."""
    # Arrange
    dto = ThermalDemandDTO(thermal_demand=0.6, mode=ThermalMode.HEAT)
    dto_list = [
        UfhCircuitDemandDTO(ufx_idx="1", thermal_demand=0.4, mode=ThermalMode.HEAT)
    ]
    actuator_dto = ActuatorStateDTO(ch_active=True, ch_enabled=True)

    # Act
    converted_dto = dto_to_dict(dto)
    converted_list = dto_to_dict(dto_list)
    converted_actuator = dto_to_dict(actuator_dto)

    # Assert
    assert converted_dto == {
        "thermal_demand": 0.6,
        "mode": "heat",
        "ufx_idx": None,
        "domain_id": None,
    }
    assert converted_list == [{"ufx_idx": "1", "thermal_demand": 0.4, "mode": "heat"}]
    assert converted_actuator["ch_active"] is True
    assert converted_actuator["ch_enabled"] is True
