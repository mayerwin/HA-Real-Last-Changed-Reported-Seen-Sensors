DOMAIN = "real_last_sensors"
CONF_SOURCE_ENTITY = "source_entity"      # V1 - single
CONF_SOURCE_ENTITIES = "source_entities"  # V2 - list
CONF_DEVICE_ID = "device_id"
CONF_SENSOR_TYPES = "sensor_types"
CONF_EXCLUDE_FROM_RECORDER = "exclude_from_recorder"

SENSOR_TYPE_CHANGED = "last_changed"
SENSOR_TYPE_SEEN = "last_seen"
SENSOR_TYPE_UNAVAILABLE = "last_unavailable"
SENSOR_TYPES = [SENSOR_TYPE_CHANGED, SENSOR_TYPE_SEEN, SENSOR_TYPE_UNAVAILABLE]

CONF_UNAVAILABLE_DEBOUNCE = "unavailable_debounce"
CONF_STARTUP_GRACE = "startup_grace"

# Seconds. A drop must still be standing after the debounce before it counts,
# and drops are ignored entirely until HA has been running for the grace period.
DEFAULT_UNAVAILABLE_DEBOUNCE = 60
DEFAULT_STARTUP_GRACE = 300
