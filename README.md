# HA Real Last Changed/Reported/Seen Sensors

A Home Assistant custom integration that creates persistent timestamp sensors tracking when entities last **changed state** or were last **seen** (reported any valid state) — surviving restarts.

## The problem

Home Assistant's built-in `last_changed` and `last_reported` attributes reset on restart and update when entities transition through unknown/unavailable states. This has been a long-standing pain point:
[2019](https://community.home-assistant.io/t/retain-last-state-change-data-of-a-sensor-after-reboot/125148) [2020](https://community.home-assistant.io/t/what-the-heck-is-with-the-latest-state-change-not-being-kept-after-restart/219480) [2022](https://community.home-assistant.io/t/persistent-version-of-last-changed-for-the-ui/467163) [2024](https://community.home-assistant.io/t/wth-there-is-no-new-last-attribute-that-retains-restart/802413)

## Features

Choose which sensor type(s) to create per entity — or select both:

| Sensor type | What it tracks | Use case |
|---|---|---|
| **Last Changed** | When the state *value* actually changed (ignoring unknown/unavailable) | "When did the temperature last change?" |
| **Last Seen** | When the entity last *reported* any valid state, even if unchanged | "Is this sensor still alive?" |

- Persists across Home Assistant restarts via `RestoreEntity`
- Ignores unknown/unavailable transitions (e.g. during restarts)
- Supports single entity tracking or bulk pattern matching (substring or regex)
- Automatically groups sensors by device
- Option to exclude sensors from the recorder (enabled by default)

## Installation

### HACS (Custom Repository)
1. Install [HACS](https://hacs.xyz/) if you haven't already.
2. In HACS, go to **Integrations** > three-dot menu > **Custom repositories**.
3. Add `https://github.com/mayerwin/HA-Real-Last-Changed-Reported-Seen-Sensors` and select **Integration** as the category.
4. Search for **"Real Last"** and click **Download**.
5. Restart Home Assistant.
6. *(Optional — required for [recorder exclusion](#recorder-exclusion))* Add to your `configuration.yaml`:
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```
   > The `patternWarning` shown by Studio Code Server on the `packages` key is a false positive — the feature works correctly.

### Manual
1. Download this repository.
2. Copy `custom_components/real_last_sensors/` to your Home Assistant `config/custom_components/` directory.
3. Restart Home Assistant.
4. *(Optional — required for [recorder exclusion](#recorder-exclusion))* See step 6 above.

## Configuration

1. Go to **Settings > Devices & Services > Add Integration**.
2. Search for **"Real Last Changed/Reported/Seen Sensors"**.
3. Choose **Single entity** or **Pattern matching**.
4. Select which sensor type(s) to create: **Last Changed**, **Last Seen**, or both.

### Viewing and managing sensors

All entries are visible at **Settings > Devices & Services > Real Last Sensors**. From there you can:
- Click an entry to see all sensors grouped under their device
- Click **Configure** to change options (e.g. recorder exclusion)
- Click the three-dot menu to **Delete** an entry and all its sensors

### Recorder exclusion

By default, the integration excludes its sensors from the recorder so they don't fill up your database. It does this by writing a YAML package file to `config/packages/` — picked up automatically by HA on every restart once [packages support is enabled](#hacs-custom-repository).

To disable recorder exclusion for a specific entry, go to **Settings > Devices & Services > Real Last Sensors**, click **Configure** on the entry, and uncheck **Exclude from recorder**.

> **Note:** HA's recorder filter is applied at startup and cannot be changed at runtime. A restart is required after toggling this option. Existing history for the affected sensors is purged immediately when the option is enabled.

## How it works

### Last Changed
Listens to `EVENT_STATE_CHANGED` and records the timestamp whenever the state value actually changes to a new valid value (not unknown/unavailable). Tracks the `previous_valid_state` as an attribute.

### Last Seen
Listens to both `EVENT_STATE_CHANGED` and `EVENT_STATE_REPORTED` — these are mutually exclusive events in Home Assistant's state machine. Together they catch every report from an entity, whether the value changed or not. Only valid states (not unknown/unavailable) are considered.

## Credits

This integration is based on [Real Last Changed](https://github.com/HamletDuFromage/ha_real-last-changed) by [@HamletDuFromage](https://github.com/HamletDuFromage), which provided the original "Last Changed" functionality and the config flow with pattern matching support.
