from __future__ import annotations
from datetime import datetime
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_state_report_event,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util, slugify
from homeassistant.const import STATE_UNKNOWN, STATE_UNAVAILABLE, CONF_NAME
from .const import (
    CONF_SOURCE_ENTITY,
    CONF_SOURCE_ENTITIES,
    CONF_DEVICE_ID,
    CONF_SENSOR_TYPES,
    SENSOR_TYPE_CHANGED,
    SENSOR_TYPE_SEEN,
)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up sensors for a config entry."""
    device_id = entry.data.get(CONF_DEVICE_ID)

    device_info = None
    if device_id:
        dev_reg = dr.async_get(hass)
        if device := dev_reg.async_get(device_id):
            if device.identifiers:
                device_info = dr.DeviceInfo(identifiers=device.identifiers)

    entities = []
    name = entry.data.get(CONF_NAME)
    if CONF_SOURCE_ENTITIES in entry.data:
        entities = entry.data[CONF_SOURCE_ENTITIES]
    elif CONF_SOURCE_ENTITY in entry.data:
        entities = [entry.data[CONF_SOURCE_ENTITY]]

    sensor_types = entry.data.get(CONF_SENSOR_TYPES, [SENSOR_TYPE_CHANGED])
    single_name = name if len(entities) == 1 else None

    sensors = []
    for entity_id in entities:
        for sensor_type in sensor_types:
            sensors.append(
                RealLastSensor(entity_id, sensor_type, single_name, device_info)
            )
    async_add_entities(sensors)


class RealLastSensor(RestoreEntity, SensorEntity):
    """Sensor that tracks when an entity last changed or was last seen."""

    _attr_should_poll = False
    _attr_device_class = "timestamp"

    def __init__(
        self,
        source_entity: str,
        sensor_type: str,
        name: str | None = None,
        device_info: dr.DeviceInfo | None = None,
    ):
        self._source = source_entity
        self._sensor_type = sensor_type
        self._attr_device_info = device_info

        friendly = source_entity.split(".")[-1].replace("_", " ").title()
        type_label = "Real Last Changed" if sensor_type == SENSOR_TYPE_CHANGED else "Last Seen"
        type_suffix = "real_last_changed" if sensor_type == SENSOR_TYPE_CHANGED else "last_seen"

        if name:
            self._attr_name = f"{name} {type_label}"
            self._attr_unique_id = f"{slugify(name)}_{type_suffix}"
        else:
            self._attr_name = f"{friendly} {type_label}"
            self._attr_unique_id = f"{source_entity.replace('.', '_')}_{type_suffix}"

        if sensor_type == SENSOR_TYPE_CHANGED:
            self._attr_icon = "mdi:clock-check-outline"
        else:
            self._attr_icon = "mdi:eye-check-outline"

        self._attr_native_value = None
        self._previous_state = None
        self._unsubs: list = []

    @property
    def extra_state_attributes(self):
        attrs = {"source_entity": self._source, "sensor_type": self._sensor_type}
        if self._sensor_type == SENSOR_TYPE_CHANGED:
            attrs["previous_valid_state"] = self._previous_state
        return attrs

    async def async_added_to_hass(self):
        await super().async_added_to_hass()

        if (state := await self.async_get_last_state()) is not None:
            self._attr_native_value = dt_util.parse_datetime(state.state)
            if self._sensor_type == SENSOR_TYPE_CHANGED:
                self._previous_state = state.attributes.get("previous_valid_state")

        if self._sensor_type == SENSOR_TYPE_CHANGED:
            self._setup_changed_tracking()
        else:
            self._setup_seen_tracking()

    def _setup_changed_tracking(self):
        """Track actual state value changes only."""

        @callback
        def on_state_change(event):
            old = event.data.get("old_state")
            new = event.data.get("new_state")
            if new is None or new.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                return
            if self._previous_state == new.state:
                return
            self._previous_state = new.state
            self._attr_native_value = datetime.now().astimezone()
            self.async_write_ha_state()

        self._unsubs = [
            async_track_state_change_event(self.hass, [self._source], on_state_change),
        ]

    def _setup_seen_tracking(self):
        """Track any valid state report (changed or unchanged)."""

        @callback
        def _update_timestamp():
            self._attr_native_value = datetime.now().astimezone()
            self.async_write_ha_state()

        @callback
        def on_state_change(event):
            new = event.data.get("new_state")
            if new is None or new.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                return
            _update_timestamp()

        @callback
        def on_state_report(event):
            state = self.hass.states.get(event.data["entity_id"])
            if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                return
            _update_timestamp()

        self._unsubs = [
            async_track_state_change_event(self.hass, [self._source], on_state_change),
            async_track_state_report_event(self.hass, [self._source], on_state_report),
        ]

    async def async_will_remove_from_hass(self):
        for unsub in self._unsubs:
            unsub()
