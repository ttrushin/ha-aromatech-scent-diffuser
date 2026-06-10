"""Sensor platform for AromaTech integration."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AromaTechConfigEntry
from .coordinator import AromaTechCoordinator
from .core.entity import AromaTechEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: AromaTechConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up AromaTech sensors from a config entry."""
    coordinator = config_entry.runtime_data
    entities: list[SensorEntity] = [AromaTechRssiSensor(coordinator)]

    if coordinator.info.battery:
        entities.append(AromaTechBatterySensor(coordinator))

    # Oil level sensors require a known total capacity to compute a percentage
    for slot, oil in enumerate(coordinator.state.oils, 1):
        if oil.total > 0:
            entities.append(AromaTechOilLevelSensor(coordinator, slot))

    async_add_entities(entities)


class AromaTechOilLevelSensor(AromaTechEntity, SensorEntity):
    """Remaining oil level for a single aroma slot."""

    _attr_icon = "mdi:water-percent"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: AromaTechCoordinator, slot: int) -> None:
        """Initialize the oil level sensor."""
        super().__init__(coordinator, f"oil_{slot}_level")
        self._slot = slot

    @property
    def name(self) -> str:
        """Return the sensor name, using the oil name when known."""
        oils = self.coordinator.state.oils
        oil_name = ""
        if self._slot <= len(oils):
            oil_name = oils[self._slot - 1].name
        if not oil_name:
            oil_name = (
                "Oil" if len(oils) <= 1 else f"Oil {self._slot}"
            )
        return f"{oil_name} level"

    @property
    def native_value(self) -> float | None:
        """Return the oil level percentage."""
        oils = self.coordinator.state.oils
        if self._slot > len(oils):
            return None
        return oils[self._slot - 1].percentage


class AromaTechBatterySensor(AromaTechEntity, SensorEntity):
    """Battery level of the diffuser."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: AromaTechCoordinator) -> None:
        """Initialize the battery sensor."""
        super().__init__(coordinator, "battery")

    @property
    def native_value(self) -> int:
        """Return the battery level."""
        return self.coordinator.state.battery_level


class AromaTechRssiSensor(AromaTechEntity, SensorEntity):
    """Bluetooth signal strength of the diffuser."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_translation_key = "rssi"

    def __init__(self, coordinator: AromaTechCoordinator) -> None:
        """Initialize the RSSI sensor."""
        super().__init__(coordinator, "rssi")

    @property
    def native_value(self) -> int | None:
        """Return the RSSI of the last advertisement."""
        return self.coordinator.rssi
