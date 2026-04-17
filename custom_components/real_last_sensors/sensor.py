from __future__ import annotations
import logging
from datetime import datetime
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_state_report_event,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util, slugify
from homeassistant.const import STATE_UNKNOWN, STATE_UNAVAILABLE, CONF_NAME
from .const import (
    DOMAIN,
    CONF_SOURCE_ENTITY,
    CONF_SOURCE_ENTITIES,
    CONF_DEVICE_ID,
    CONF_SENSOR_TYPES,
    SENSOR_TYPE_CHANGED,
    SENSOR_TYPE_SEEN,
)

_LOGGER = logging.getLogger(__name__)

TYPE_LABELS = {
    SENSOR_TYPE_CHANGED: "Last Changed",
    SENSOR_TYPE_SEEN: "Last Seen",
}
TYPE_SUFFIXES = {
    SENSOR_TYPE_CHANGED: "last_changed",
    SENSOR_TYPE_SEEN: "last_seen",
}
TYPE_ICONS = {
    SENSOR_TYPE_CHANGED: "mdi:clock-check-outline",
    SENSOR_TYPE_SEEN: "mdi:eye-check-outline",
}


def _source_entity_name(hass: HomeAssistant, entity_id: str) -> str:
    """Derive the source entity's own name (without device prefix).

    Prefers the user's explicit name override, then the entity_id slug
    (stable across display-name changes), then original_name as a last
    resort. Using the slug as the default avoids carrying stale names
    from upstream integrations into our sensor names.
    """
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get(entity_id)

    device_name = None
    if entry and entry.device_id:
        device = dr.async_get(hass).async_get(entry.device_id)
        if device:
            device_name = device.name_by_user or device.name

    if entry and entry.name:
        name = entry.name
    else:
        slug = entity_id.split(".", 1)[1]
        if device_name:
            device_slug = slugify(device_name)
            if device_slug and slug.startswith(device_slug + "_"):
                slug = slug[len(device_slug) + 1:]
        name = slug.replace("_", " ").title() if slug else (
            entry.original_name if entry and entry.original_name else entity_id
        )

    if device_name and name.lower().startswith(device_name.lower()):
        stripped = name[len(device_name):].lstrip(" -_")
        if stripped:
            return stripped
    return name


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up sensors for a config entry."""
    device_id = entry.data.get(CONF_DEVICE_ID)

    source_device_info = None
    if device_id:
        dev_reg = dr.async_get(hass)
        if device := dev_reg.async_get(device_id):
            # Always merge with the source device. Identifiers + connections are
            # what HA uses to match to an existing device — passing both ensures
            # our entities attach to the source device rather than spawning a
            # new virtual device, regardless of whether the upstream integration
            # uses identifiers, connections, or both.
            if device.identifiers or device.connections:
                source_device_info = dr.DeviceInfo(
                    identifiers=device.identifiers,
                    connections=device.connections,
                )

    entities = []
    custom_name = entry.data.get(CONF_NAME)
    if CONF_SOURCE_ENTITIES in entry.data:
        entities = entry.data[CONF_SOURCE_ENTITIES]
    elif CONF_SOURCE_ENTITY in entry.data:
        entities = [entry.data[CONF_SOURCE_ENTITY]]

    sensor_types = entry.data.get(CONF_SENSOR_TYPES, [SENSOR_TYPE_CHANGED])
    single_custom_name = custom_name if len(entities) == 1 else None
    has_custom_name = bool(single_custom_name)

    ent_reg = er.async_get(hass)
    _LOGGER.warning(
        "sensor.async_setup_entry: entry=%s sources=%s types=%s has_custom_name=%s device_info=%s",
        entry.entry_id,
        entities,
        sensor_types,
        has_custom_name,
        source_device_info,
    )

    live_entities = []
    for entity_id in entities:
        if ent_reg.async_get(entity_id) is None:
            _LOGGER.warning(
                "  skipping %s: not in entity registry (renamed or removed upstream); "
                "update this integration entry to point at the new entity",
                entity_id,
            )
            continue
        live_entities.append(entity_id)

    sensors = []
    for entity_id in live_entities:
        source_reg_entry = ent_reg.async_get(entity_id)
        source_state = hass.states.get(entity_id)
        _LOGGER.warning(
            "  source %s: registry=%s state=%s",
            entity_id,
            "present" if source_reg_entry else "MISSING",
            "present" if source_state else "MISSING",
        )
        source_name = single_custom_name or _source_entity_name(hass, entity_id)
        source_object_id = entity_id.split(".", 1)[1]
        for sensor_type in sensor_types:
            type_suffix = TYPE_SUFFIXES[sensor_type]
            type_label = TYPE_LABELS[sensor_type]

            unique_id = f"{entity_id.replace('.', '_')}_{type_suffix}"
            if has_custom_name:
                desired_object_id = slugify(f"{source_name} {type_label}")
            else:
                desired_object_id = f"{source_object_id}_{type_suffix}"
            desired_entity_id = f"sensor.{desired_object_id}"

            existing = ent_reg.async_get_entity_id("sensor", DOMAIN, unique_id)
            _LOGGER.warning(
                "  %s: unique_id=%s desired=%s existing=%s",
                sensor_type,
                unique_id,
                desired_entity_id,
                existing,
            )
            if existing and existing != desired_entity_id:
                _LOGGER.warning(
                    "  wiping stale row %s (desired %s)",
                    existing,
                    desired_entity_id,
                )
                ent_reg.async_remove(existing)

            sensors.append(
                RealLastSensor(
                    entity_id,
                    sensor_type,
                    source_name,
                    source_device_info,
                    has_custom_name=has_custom_name,
                    desired_object_id=desired_object_id,
                )
            )
    async_add_entities(sensors)


class RealLastSensor(RestoreEntity, SensorEntity):
    """Sensor that tracks when an entity last changed or was last seen."""

    _attr_should_poll = False
    _attr_device_class = "timestamp"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        source_entity: str,
        sensor_type: str,
        source_name: str,
        device_info: dr.DeviceInfo | None = None,
        has_custom_name: bool = False,
        desired_object_id: str | None = None,
    ):
        self._source = source_entity
        self._sensor_type = sensor_type
        self._attr_device_info = device_info
        if has_custom_name:
            self._attr_has_entity_name = False

        type_label = TYPE_LABELS[sensor_type]
        type_suffix = TYPE_SUFFIXES[sensor_type]

        self._attr_name = f"{source_name} {type_label}"
        self._attr_unique_id = f"{source_entity.replace('.', '_')}_{type_suffix}"
        self._attr_icon = TYPE_ICONS[sensor_type]

        # Pin entity_id explicitly so HA doesn't fall back to a slug that
        # embeds stale device or source names.
        if desired_object_id:
            self.entity_id = f"sensor.{desired_object_id}"

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
