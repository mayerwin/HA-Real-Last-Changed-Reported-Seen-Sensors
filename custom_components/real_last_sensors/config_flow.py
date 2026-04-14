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

    VERSION = 1

    def __init__(self):
        self._matched = []
        self._sensor_types = []

    @staticmethod
    def async_get_options_flow(config_entry):
        return RealLastSensorsOptionsFlow(config_entry)

    async def async_step_user(self, _=None):
        return self.async_show_menu(step_id="user", menu_options=["single", "pattern"])

    async def async_step_single(self, user_input=None):
        if user_input is not None:
            sensor_types = user_input.get(CONF_SENSOR_TYPES, [SENSOR_TYPE_CHANGED])
            return await self._create_or_update(
                user_input[CONF_SOURCE_ENTITY],
                sensor_types,
                user_input.get(CONF_NAME),
            )

        return self.async_show_form(
            step_id="single",
            data_schema=vol.Schema({
                vol.Required(CONF_SOURCE_ENTITY): selector.EntitySelector(),
                vol.Required(CONF_SENSOR_TYPES, default=[SENSOR_TYPE_CHANGED]): SENSOR_TYPE_SELECTOR,
                vol.Optional(CONF_NAME): str,
            }),
        )

    async def async_step_pattern(self, user_input=None):
        errors = {}
        if user_input is not None:
            pattern = user_input.get("pattern", "").strip()
            if not (matched := self._match_entities(pattern, user_input.get("regex", False))):
                errors["base"] = "no_pattern" if not pattern else "no_matches"
            else:
                self._matched = matched
                self._sensor_types = user_input.get(CONF_SENSOR_TYPES, [SENSOR_TYPE_CHANGED])
                return await self.async_step_confirm()

        return self.async_show_form(
            step_id="pattern",
            data_schema=vol.Schema({
                vol.Required("pattern"): str,
                vol.Optional("regex", default=False): bool,
                vol.Required(CONF_SENSOR_TYPES, default=[SENSOR_TYPE_CHANGED]): SENSOR_TYPE_SELECTOR,
            }),
            errors=errors,
        )

    async def async_step_confirm(self, user_input=None):
        if user_input is not None:
            return await self._create_bulk(self._matched, self._sensor_types)

        preview = "\n".join(f"- {e}" for e in self._matched[:30])
        if len(self._matched) > 30:
            preview += f"\n... and {len(self._matched) - 30} more"

        types_label = ", ".join(SENSOR_TYPE_LABELS[t] for t in self._sensor_types)
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "count": str(len(self._matched)),
                "entities": preview,
                "types": types_label,
            },
        )

    def _match_entities(self, pattern: str, use_regex: bool) -> list[str]:
        matched = []
        for eid in self.hass.states.async_entity_ids():
            if self._is_own_entity(eid):
                continue
            try:
                if use_regex and re.search(pattern, eid, re.IGNORECASE):
                    matched.append(eid)
                elif not use_regex and pattern.lower() in eid.lower():
                    matched.append(eid)
            except re.error:
                pass
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

    def _is_duplicated(self, entity_id: str, sensor_types: list[str]) -> bool:
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if sorted(entry.data.get(CONF_SENSOR_TYPES, [])) != sorted(sensor_types):
                continue
            tracked = entry.data.get(CONF_SOURCE_ENTITIES, [])
            if CONF_SOURCE_ENTITY in entry.data:
                tracked = list(tracked) + [entry.data[CONF_SOURCE_ENTITY]]
            if entity_id in tracked:
                return True
        return False

    async def _create_or_update(
        self, entity_id: str, sensor_types: list[str], name: str | None = None
    ):
        if self._is_own_entity(entity_id):
            return self.async_abort(reason="cannot_track_self")

        if self._is_duplicated(entity_id, sensor_types):
            return self.async_abort(reason="already_configured")

        entry = er.async_get(self.hass).async_get(entity_id)
        device_id = entry.device_id if entry else None

        if device_id and (existing := self._get_device_entry(device_id, sensor_types)):
            await self._update_entry(existing, [entity_id])
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
            CONF_SENSOR_TYPES: sensor_types,
        }
        if name:
            data[CONF_NAME] = name

        types_label = " + ".join(SENSOR_TYPE_LABELS[t] for t in sensor_types)
        return self.async_create_entry(
            title=name or f"{self._get_device_name(device_id) or entity_id} ({types_label})",
            data=data,
        )

    async def _update_entry(self, entry, new_entities: list[str]):
        new_data = dict(entry.data)
        new_data[CONF_SOURCE_ENTITIES] = self._get_entities_from_entry(entry) + new_entities
        new_data.pop(CONF_SOURCE_ENTITY, None)
        self.hass.config_entries.async_update_entry(entry, data=new_data)
        await self.hass.config_entries.async_reload(entry.entry_id)

    async def _create_bulk(self, entities: list[str], sensor_types: list[str]):
        ent_reg, by_device = er.async_get(self.hass), {}
        for eid in entities:
            entry = ent_reg.async_get(eid)
            by_device.setdefault(entry.device_id if entry else None, []).append(eid)

        created, added = 0, 0
        for dev_id, eids in by_device.items():
            valid_eids = [e for e in eids if not self._is_duplicated(e, sensor_types)]
            if not valid_eids:
                continue

            if dev_id and (existing := self._get_device_entry(dev_id, sensor_types)):
                current_ents = self._get_entities_from_entry(existing)
                to_add = [e for e in valid_eids if e not in current_ents]
                if to_add:
                    await self._update_entry(existing, to_add)
                    added += len(to_add)
            else:
                data = {
                    CONF_SOURCE_ENTITIES: valid_eids,
                    CONF_DEVICE_ID: dev_id,
                    CONF_SENSOR_TYPES: sensor_types,
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
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_exclude = self._entry.options.get(CONF_EXCLUDE_FROM_RECORDER, True)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_EXCLUDE_FROM_RECORDER, default=current_exclude
                ): selector.BooleanSelector(),
            }),
        )
