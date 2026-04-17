from __future__ import annotations
import re
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector, entity_registry as er, device_registry as dr
from homeassistant.util import slugify
from homeassistant.const import CONF_NAME
from .const import (
    DOMAIN,
    CONF_SOURCE_ENTITY,
    CONF_SOURCE_ENTITIES,
    CONF_DEVICE_ID,
    CONF_SENSOR_TYPES,
    CONF_EXCLUDE_FROM_RECORDER,
    SENSOR_TYPE_CHANGED,
    SENSOR_TYPE_SEEN,
    SENSOR_TYPES,
)

DEFAULT_SENSOR_TYPES = [SENSOR_TYPE_CHANGED, SENSOR_TYPE_SEEN]

SENSOR_TYPE_LABELS = {
    SENSOR_TYPE_CHANGED: "Last Changed",
    SENSOR_TYPE_SEEN: "Last Seen",
}

SENSOR_TYPE_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[
            selector.SelectOptionDict(value=t, label=SENSOR_TYPE_LABELS[t])
            for t in SENSOR_TYPES
        ],
        multiple=True,
        mode=selector.SelectSelectorMode.LIST,
    )
)


class RealLastSensorsFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Real Last Sensors."""

    VERSION = 2

    def __init__(self):
        self._matched: list[str] = []
        self._selected: list[str] = []
        self._sensor_types: list[str] = list(DEFAULT_SENSOR_TYPES)

    @staticmethod
    def async_get_options_flow(config_entry):
        return RealLastSensorsOptionsFlow(config_entry)

    async def async_step_user(self, _=None):
        return self.async_show_menu(step_id="user", menu_options=["single", "pattern"])

    async def async_step_single(self, user_input=None):
        if user_input is not None:
            sensor_types = user_input.get(CONF_SENSOR_TYPES, DEFAULT_SENSOR_TYPES)
            return await self._create_or_update(
                user_input[CONF_SOURCE_ENTITY],
                sensor_types,
                user_input.get(CONF_NAME),
            )

        return self.async_show_form(
            step_id="single",
            data_schema=vol.Schema({
                vol.Required(CONF_SOURCE_ENTITY): selector.EntitySelector(),
                vol.Required(CONF_SENSOR_TYPES, default=DEFAULT_SENSOR_TYPES): SENSOR_TYPE_SELECTOR,
                vol.Optional(CONF_NAME): str,
            }),
        )

    async def async_step_pattern(self, user_input=None):
        """Iterative pattern search.

        One step that the user re-submits as many times as needed:
          * type a pattern and submit to search (matches are added, auto-ticked)
          * untick anything unwanted
          * type another pattern and submit to add more matches
          * leave pattern empty and submit to create sensors
          * tick "Clear all matched" to wipe the accumulated list
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            pattern = user_input.get("pattern", "").strip()
            use_regex = user_input.get("regex", False)
            selected_now = list(user_input.get(CONF_SOURCE_ENTITIES, []) or [])
            self._sensor_types = user_input.get(
                CONF_SENSOR_TYPES, DEFAULT_SENSOR_TYPES
            )

            if user_input.get("clear_selection"):
                self._matched = []
                self._selected = []
            elif pattern:
                try:
                    new_matches = self._match_entities(pattern, use_regex)
                except re.error:
                    errors["base"] = "bad_regex"
                    new_matches = []

                added = [e for e in new_matches if e not in self._matched]
                self._matched.extend(added)
                self._selected = list(dict.fromkeys(selected_now + added))

                if not new_matches and "base" not in errors:
                    errors["base"] = "no_matches"
            else:
                if not selected_now:
                    errors["base"] = "no_selection"
                else:
                    return await self._create_bulk(
                        selected_now, self._sensor_types
                    )
                # Preserve user unticks even on error re-render.
                self._selected = selected_now

        schema_dict: dict = {
            vol.Optional("pattern", default=""): str,
            vol.Optional("regex", default=False): bool,
            vol.Required(
                CONF_SENSOR_TYPES,
                default=self._sensor_types or list(DEFAULT_SENSOR_TYPES),
            ): SENSOR_TYPE_SELECTOR,
        }
        if self._matched:
            schema_dict[
                vol.Optional(
                    CONF_SOURCE_ENTITIES, default=list(self._selected)
                )
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=e, label=e)
                        for e in self._matched
                    ],
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            )
            schema_dict[
                vol.Optional("clear_selection", default=False)
            ] = selector.BooleanSelector()

        return self.async_show_form(
            step_id="pattern",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={
                "count": str(len(self._matched)),
                "selected": str(len(self._selected)),
            },
        )

    def _match_entities(self, pattern: str, use_regex: bool) -> list[str]:
        matched = []
        compiled = re.compile(pattern, re.IGNORECASE) if use_regex else None
        for eid in self.hass.states.async_entity_ids():
            if self._is_own_entity(eid):
                continue
            if compiled is not None:
                if compiled.search(eid):
                    matched.append(eid)
            elif pattern.lower() in eid.lower():
                matched.append(eid)
        return sorted(matched)

    def _is_own_entity(self, eid: str) -> bool:
        entry = er.async_get(self.hass).async_get(eid)
        return entry is not None and entry.platform == DOMAIN

    def _get_device_entry(self, device_id: str, sensor_types: list[str]):
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_DEVICE_ID) != device_id:
                continue
            if sorted(entry.data.get(CONF_SENSOR_TYPES, [])) == sorted(sensor_types):
                return entry
        return None

    def _get_entities_from_entry(self, entry) -> list[str]:
        ents = list(entry.data.get(CONF_SOURCE_ENTITIES, []))
        if not ents and CONF_SOURCE_ENTITY in entry.data:
            ents = [entry.data[CONF_SOURCE_ENTITY]]
        return ents

    def _get_device_name(self, device_id: str) -> str:
        device = dr.async_get(self.hass).async_get(device_id)
        return device.name_by_user or device.name or device_id if device else device_id

    def _existing_types_for_entity(self, entity_id: str) -> set[str]:
        """Return the sensor types already tracked for this entity across all entries."""
        types: set[str] = set()
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entity_id in self._get_entities_from_entry(entry):
                types |= set(entry.data.get(CONF_SENSOR_TYPES, []))
        return types

    async def _create_or_update(
        self, entity_id: str, sensor_types: list[str], name: str | None = None
    ):
        if self._is_own_entity(entity_id):
            return self.async_abort(reason="cannot_track_self")

        existing = self._existing_types_for_entity(entity_id)

        if name:
            # A custom name means the user wants one consolidated entry under
            # that name. Take over from any prior entries tracking this source
            # so the new entry owns all sensor types with the new name.
            if existing:
                await self._take_over_entity(entity_id)
            effective_types = sensor_types
        else:
            effective_types = [t for t in sensor_types if t not in existing]
            if not effective_types:
                return self.async_abort(reason="already_configured")

        entry = er.async_get(self.hass).async_get(entity_id)
        device_id = entry.device_id if entry else None

        if not name and device_id and (
            existing_entry := self._get_device_entry(device_id, effective_types)
        ):
            await self._update_entry(existing_entry, [entity_id])
            return self.async_abort(
                reason="added_to_device",
                description_placeholders={"count": "1"},
            )

        if name:
            await self.async_set_unique_id(slugify(name))
            self._abort_if_unique_id_configured()

        data = {
            CONF_SOURCE_ENTITIES: [entity_id],
            CONF_DEVICE_ID: device_id,
            CONF_SENSOR_TYPES: effective_types,
        }
        if name:
            data[CONF_NAME] = name

        # Keep the custom name off the config entry title — HA renders entry
        # tiles in a device-like card, and reusing the custom name there makes
        # it look like a second device. The custom name lives on the sensor's
        # friendly name and entity_id instead.
        types_label = " + ".join(SENSOR_TYPE_LABELS[t] for t in effective_types)
        device_label = self._get_device_name(device_id) or entity_id
        return self.async_create_entry(
            title=f"{device_label} ({types_label})",
            data=data,
        )

    async def _take_over_entity(self, entity_id: str) -> None:
        """Remove entity_id (and its sensors) from any existing entries."""
        ent_reg = er.async_get(self.hass)
        prefix = f"{entity_id.replace('.', '_')}_"
        for entry in list(self.hass.config_entries.async_entries(DOMAIN)):
            tracked = self._get_entities_from_entry(entry)
            if entity_id not in tracked:
                continue

            for reg in list(ent_reg.entities.values()):
                if (
                    reg.config_entry_id == entry.entry_id
                    and reg.unique_id.startswith(prefix)
                ):
                    ent_reg.async_remove(reg.entity_id)

            remaining = [e for e in tracked if e != entity_id]
            if remaining:
                new_data = dict(entry.data)
                new_data[CONF_SOURCE_ENTITIES] = remaining
                new_data.pop(CONF_SOURCE_ENTITY, None)
                self.hass.config_entries.async_update_entry(entry, data=new_data)
                await self.hass.config_entries.async_reload(entry.entry_id)
            else:
                await self.hass.config_entries.async_remove(entry.entry_id)

    async def _update_entry(self, entry, new_entities: list[str]):
        new_data = dict(entry.data)
        new_data[CONF_SOURCE_ENTITIES] = self._get_entities_from_entry(entry) + new_entities
        new_data.pop(CONF_SOURCE_ENTITY, None)
        self.hass.config_entries.async_update_entry(entry, data=new_data)
        await self.hass.config_entries.async_reload(entry.entry_id)

    async def _create_bulk(self, entities: list[str], sensor_types: list[str]):
        ent_reg = er.async_get(self.hass)
        groups: dict[tuple[str | None, tuple[str, ...]], list[str]] = {}
        for eid in entities:
            existing = self._existing_types_for_entity(eid)
            remaining = tuple(t for t in sensor_types if t not in existing)
            if not remaining:
                continue
            entry = ent_reg.async_get(eid)
            dev_id = entry.device_id if entry else None
            groups.setdefault((dev_id, remaining), []).append(eid)

        created, added = 0, 0
        for (dev_id, remaining), eids in groups.items():
            remaining_list = list(remaining)
            if dev_id and (existing_entry := self._get_device_entry(dev_id, remaining_list)):
                current_ents = self._get_entities_from_entry(existing_entry)
                to_add = [e for e in eids if e not in current_ents]
                if to_add:
                    await self._update_entry(existing_entry, to_add)
                    added += len(to_add)
            else:
                data = {
                    CONF_SOURCE_ENTITIES: eids,
                    CONF_DEVICE_ID: dev_id,
                    CONF_SENSOR_TYPES: remaining_list,
                }
                self.hass.async_create_task(
                    self.hass.config_entries.flow.async_init(
                        DOMAIN,
                        context={"source": "import"},
                        data=data,
                    )
                )
                created += 1

        return self.async_abort(
            reason="bulk_created",
            description_placeholders={
                "created": str(created),
                "added": str(added),
            },
        )

    async def async_step_import(self, data: dict):
        ents = data.get(CONF_SOURCE_ENTITIES, [])
        dev_id = data.get(CONF_DEVICE_ID)
        sensor_types = data.get(CONF_SENSOR_TYPES, [SENSOR_TYPE_CHANGED])

        base_id = f"device_{dev_id}" if dev_id else ents[0]
        types_suffix = "_".join(sorted(sensor_types))
        await self.async_set_unique_id(f"{base_id}_{types_suffix}")
        self._abort_if_unique_id_configured()
        title = self._get_device_name(dev_id) if dev_id else ents[0]
        return self.async_create_entry(title=title, data=data)


class RealLastSensorsOptionsFlow(config_entries.OptionsFlow):
    """Options flow — shown when the user clicks Configure on an integration entry."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input=None):
        current_sources = self._get_current_sources()
        current_exclude = self._entry.options.get(CONF_EXCLUDE_FROM_RECORDER, True)

        if user_input is not None:
            kept = user_input.get(CONF_SOURCE_ENTITIES, [])
            exclude = user_input.get(CONF_EXCLUDE_FROM_RECORDER, True)

            if not kept:
                self.hass.async_create_task(
                    self.hass.config_entries.async_remove(self._entry.entry_id)
                )
                return self.async_abort(reason="entry_removed")

            removed = [e for e in current_sources if e not in kept]
            if removed:
                await self._cleanup_registry(removed)

            new_data = dict(self._entry.data)
            new_data[CONF_SOURCE_ENTITIES] = kept
            new_data.pop(CONF_SOURCE_ENTITY, None)

            self.hass.config_entries.async_update_entry(
                self._entry,
                data=new_data,
                options={CONF_EXCLUDE_FROM_RECORDER: exclude},
            )
            return self.async_abort(reason="options_updated")

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_SOURCE_ENTITIES, default=current_sources
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        multiple=True,
                        include_entities=current_sources,
                    )
                ),
                vol.Required(
                    CONF_EXCLUDE_FROM_RECORDER, default=current_exclude
                ): selector.BooleanSelector(),
            }),
        )

    def _get_current_sources(self) -> list[str]:
        data = self._entry.data
        if CONF_SOURCE_ENTITIES in data:
            return list(data[CONF_SOURCE_ENTITIES])
        if CONF_SOURCE_ENTITY in data:
            return [data[CONF_SOURCE_ENTITY]]
        return []

    async def _cleanup_registry(self, removed: list[str]) -> None:
        """Remove registry entries for source entities no longer tracked."""
        ent_reg = er.async_get(self.hass)
        prefixes = tuple(f"{e.replace('.', '_')}_" for e in removed)
        to_remove = [
            reg.entity_id
            for reg in list(ent_reg.entities.values())
            if reg.config_entry_id == self._entry.entry_id
            and reg.unique_id.startswith(prefixes)
        ]
        for eid in to_remove:
            ent_reg.async_remove(eid)
