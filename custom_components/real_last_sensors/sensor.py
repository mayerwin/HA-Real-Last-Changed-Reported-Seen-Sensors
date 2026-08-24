from __future__ import annotations
import logging
from datetime import datetime, timedelta
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_state_report_event,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util, slugify
from homeassistant.const import STATE_UNKNOWN, STATE_UNAVAILABLE, CONF_NAME
from .const import (
    CONF_SOURCE_ENTITY,
    CONF_SOURCE_ENTITIES,
    CONF_DEVICE_ID,
    CONF_SENSOR_TYPES,
    SENSOR_TYPE_CHANGED,
    SENSOR_TYPE_SEEN,
    SENSOR_TYPE_UNAVAILABLE,
    CONF_UNAVAILABLE_DEBOUNCE,
    CONF_STARTUP_GRACE,
    DEFAULT_UNAVAILABLE_DEBOUNCE,
    DEFAULT_STARTUP_GRACE,
)

_LOGGER = logging.getLogger(__name__)

TYPE_LABELS = {
    SENSOR_TYPE_CHANGED: "Last Changed",
    SENSOR_TYPE_SEEN: "Last Seen",
    SENSOR_TYPE_UNAVAILABLE: "Last Unavailable",
}
TYPE_SUFFIXES = {
    SENSOR_TYPE_CHANGED: "last_changed",
    SENSOR_TYPE_SEEN: "last_seen",
    SENSOR_TYPE_UNAVAILABLE: "last_unavailable",
}
TYPE_ICONS = {
    SENSOR_TYPE_CHANGED: "mdi:clock-check-outline",
    SENSOR_TYPE_SEEN: "mdi:eye-check-outline",
    SENSOR_TYPE_UNAVAILABLE: "mdi:lan-disconnect",
}


def _source_entity_name(hass: HomeAssistant, entity_id: str) -> str:
    """Derive the source entity's own name from its entity_id slug.

    Deliberately ignores the source's friendly-name override: the entity_id
    is the user's latest explicit choice, while the override may still hold a
    stale name from before a rename. Users who want a different label should
    use this integration's custom name option.
    """
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get(entity_id)

    device_name = None
    if entry and entry.device_id:
        device = dr.async_get(hass).async_get(entry.device_id)
        if device:
            device_name = device.name_by_user or device.name

    slug = entity_id.split(".", 1)[1]
    if device_name:
        device_slug = slugify(device_name)
        if device_slug and slug.startswith(device_slug + "_"):
            slug = slug[len(device_slug) + 1:]
    return slug.replace("_", " ").title() if slug else entity_id


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Set up sensors for a config entry."""
    device_id = entry.data.get(CONF_DEVICE_ID)

    source_device_info = None
    if device_id:
        dev_reg = dr.async_get(hass)
        if device := dev_reg.async_get(device_id):
            # Pass both identifiers and connections so HA merges with the
            # source device regardless of which one upstream registered with.
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
    debounce = timedelta(
        seconds=entry.options.get(
            CONF_UNAVAILABLE_DEBOUNCE, DEFAULT_UNAVAILABLE_DEBOUNCE
        )
    )
    grace = timedelta(
        seconds=entry.options.get(CONF_STARTUP_GRACE, DEFAULT_STARTUP_GRACE)
    )
    single_custom_name = custom_name if len(entities) == 1 else None
    has_custom_name = bool(single_custom_name)

    ent_reg = er.async_get(hass)

    sensors = []
    for entity_id in entities:
        if ent_reg.async_get(entity_id) is None:
            _LOGGER.debug(
                "Skipping %s: not in entity registry (renamed or removed upstream)",
                entity_id,
            )
            continue

        source_name = single_custom_name or _source_entity_name(hass, entity_id)
        source_object_id = entity_id.split(".", 1)[1]
        for sensor_type in sensor_types:
            type_suffix = TYPE_SUFFIXES[sensor_type]
            type_label = TYPE_LABELS[sensor_type]

            if has_custom_name:
                desired_object_id = slugify(f"{source_name} {type_label}")
            else:
                desired_object_id = f"{source_object_id}_{type_suffix}"

            sensors.append(
                RealLastSensor(
                    entity_id,
                    sensor_type,
                    source_name,
                    source_device_info,
                    has_custom_name=has_custom_name,
                    desired_object_id=desired_object_id,
                    debounce=debounce,
                    grace=grace,
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
        debounce: timedelta | None = None,
        grace: timedelta | None = None,
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

        # suggested_object_id only applies on first registration, so any
        # later user rename via HA's UI is preserved.
        if desired_object_id:
            self._attr_suggested_object_id = desired_object_id

        self._attr_native_value = None
        self._previous_state = None
        self._unsubs: list = []

        # Last Unavailable bookkeeping
        self._last_available: datetime | None = None
        self._outage_duration: float | None = None
        self._outage_ongoing = False
        self._pending_drop: datetime | None = None
        self._cancel_debounce = None
        self._grace_until: datetime | None = None
        self._started_at: datetime | None = None
        self._debounce = (
            debounce
            if debounce is not None
            else timedelta(seconds=DEFAULT_UNAVAILABLE_DEBOUNCE)
        )
        self._grace = (
            grace if grace is not None else timedelta(seconds=DEFAULT_STARTUP_GRACE)
        )

    @property
    def extra_state_attributes(self):
        attrs = {"source_entity": self._source, "sensor_type": self._sensor_type}
        if self._sensor_type == SENSOR_TYPE_CHANGED:
            attrs["previous_valid_state"] = self._previous_state
        elif self._sensor_type == SENSOR_TYPE_UNAVAILABLE:
            source = self.hass.states.get(self._source) if self.hass else None
            attrs["currently_unavailable"] = (
                source is not None and source.state == STATE_UNAVAILABLE
            )
            attrs["outage_ongoing"] = self._outage_ongoing
            attrs["last_available"] = (
                self._last_available.isoformat() if self._last_available else None
            )
            attrs["last_outage_duration_seconds"] = self._outage_duration
        return attrs

    async def async_added_to_hass(self):
        await super().async_added_to_hass()

        if (state := await self.async_get_last_state()) is not None:
            self._attr_native_value = dt_util.parse_datetime(state.state)
            if self._sensor_type == SENSOR_TYPE_CHANGED:
                self._previous_state = state.attributes.get("previous_valid_state")
            elif self._sensor_type == SENSOR_TYPE_UNAVAILABLE:
                self._outage_ongoing = bool(state.attributes.get("outage_ongoing"))
                if raw := state.attributes.get("last_available"):
                    self._last_available = dt_util.parse_datetime(raw)
                self._outage_duration = state.attributes.get(
                    "last_outage_duration_seconds"
                )

        if self._sensor_type == SENSOR_TYPE_CHANGED:
            self._setup_changed_tracking()
        elif self._sensor_type == SENSOR_TYPE_UNAVAILABLE:
            self._setup_unavailable_tracking()
        else:
            self._setup_seen_tracking()

    def _setup_changed_tracking(self):
        """Track actual state value changes only."""

        @callback
        def on_state_change(event):
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

    def _setup_unavailable_tracking(self):
        """Track when the source last dropped to unavailable.

        Two filters keep this from degenerating into a restart clock:
        a startup grace period, during which drops are ignored outright
        (integrations mark their entities unavailable until they have loaded),
        and a debounce, so only a drop that is still standing when it
        elapses is recorded. The timestamp committed is the moment
        of the drop, not the moment the debounce elapsed.
        """
        @callback
        def _end_of_grace(_now) -> None:
            """Catch a real drop that happened while the grace period was open.

            Only adopt it if the source went unavailable well after HA had
            settled; a drop stamped at startup is indistinguishable from an
            integration that never finished loading, and in that case the
            restored timestamp is the better answer.
            """
            self._grace_until = None
            source = self.hass.states.get(self._source)
            if source is None or source.state != STATE_UNAVAILABLE:
                return
            if self._outage_ongoing or self._started_at is None:
                return
            if source.last_changed <= self._started_at + self._debounce:
                return
            self._attr_native_value = source.last_changed
            self._outage_ongoing = True
            self.async_write_ha_state()

        if self.hass.state is not CoreState.running:
            self._grace_until = dt_util.utcnow() + self._grace

            @callback
            def _on_started(_hass) -> None:
                self._started_at = dt_util.utcnow()
                self._grace_until = self._started_at + self._grace
                self.async_on_remove(
                    async_call_later(self.hass, self._grace, _end_of_grace)
                )

            self.async_on_remove(async_at_started(self.hass, _on_started))
        elif (
            self._attr_native_value is None
            and (source := self.hass.states.get(self._source)) is not None
            and source.state == STATE_UNAVAILABLE
        ):
            # Entry added for an already-offline entity: HA has been up long
            # enough that the source's own last_changed is trustworthy, so
            # adopt it rather than starting blank.
            self._attr_native_value = source.last_changed
            self._outage_ongoing = True

        @callback
        def _commit_drop(_now) -> None:
            self._cancel_debounce = None
            source = self.hass.states.get(self._source)
            if source is None or source.state != STATE_UNAVAILABLE:
                self._pending_drop = None
                return
            self._attr_native_value = self._pending_drop
            self._pending_drop = None
            self._outage_ongoing = True
            self.async_write_ha_state()

        @callback
        def _cancel_pending() -> None:
            if self._cancel_debounce is not None:
                self._cancel_debounce()
                self._cancel_debounce = None
            self._pending_drop = None

        @callback
        def on_state_change(event):
            new = event.data.get("new_state")
            if new is None:
                # Source entity removed, not an outage.
                _cancel_pending()
                return

            if new.state == STATE_UNAVAILABLE:
                if self._outage_ongoing or self._cancel_debounce is not None:
                    return
                if self._grace_until and dt_util.utcnow() < self._grace_until:
                    _LOGGER.debug(
                        "Ignoring %s going unavailable during startup grace",
                        self._source,
                    )
                    return
                self._pending_drop = new.last_changed
                self._cancel_debounce = async_call_later(
                    self.hass, self._debounce, _commit_drop
                )
                return

            # Back to any non-unavailable state: a pending drop was a blip.
            _cancel_pending()
            if self._outage_ongoing:
                self._outage_ongoing = False
                self._last_available = new.last_changed
                if self._attr_native_value is not None:
                    self._outage_duration = round(
                        (self._last_available - self._attr_native_value).total_seconds()
                    )
                self.async_write_ha_state()

        self._unsubs = [
            async_track_state_change_event(self.hass, [self._source], on_state_change),
        ]

    async def async_will_remove_from_hass(self):
        if self._cancel_debounce is not None:
            self._cancel_debounce()
            self._cancel_debounce = None
        for unsub in self._unsubs:
            unsub()
