"""Support the ISY-994 controllers."""
import asyncio
from functools import partial
import logging
from typing import Optional
from urllib.parse import urlparse

from pyisy import ISY
from pyisy.constants import (
    COMMAND_FRIENDLY_NAME,
    EVENT_PROPS_IGNORED,
    ISY_VALUE_UNKNOWN,
    PROTO_GROUP,
    PROTO_INSTEON,
    PROTO_ZWAVE,
)
from pyisy.helpers import NodeProperty
from pyisy.nodes import Group
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.binary_sensor import (
    DEVICE_CLASSES_SCHEMA as BINARY_SENSOR_DCS,
)
from homeassistant.components.sensor import DEVICE_CLASSES_SCHEMA as SENSOR_DCS
from homeassistant.const import (
    CONF_BINARY_SENSORS,
    CONF_DEVICE_CLASS,
    CONF_HOST,
    CONF_ICON,
    CONF_ID,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PAYLOAD_OFF,
    CONF_PAYLOAD_ON,
    CONF_SENSORS,
    CONF_SWITCHES,
    CONF_TYPE,
    CONF_UNIT_OF_MEASUREMENT,
    CONF_USERNAME,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
import homeassistant.helpers.device_registry as dr
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_registry import async_get_registry
from homeassistant.helpers.typing import ConfigType, Dict

from .const import (
    CONF_IGNORE_STRING,
    CONF_ISY_VARIABLES,
    CONF_SENSOR_STRING,
    CONF_TLS_VER,
    DEFAULT_IGNORE_STRING,
    DEFAULT_OFF_VALUE,
    DEFAULT_ON_VALUE,
    DEFAULT_SENSOR_STRING,
    DOMAIN,
    ISY994_ISY,
    ISY994_NODES,
    ISY994_PROGRAMS,
    ISY994_VARIABLES,
    KEY_ACTIONS,
    KEY_FOLDER,
    KEY_MY_PROGRAMS,
    KEY_STATUS,
    MANUFACTURER,
    NODE_FILTERS,
    SCENE_DOMAIN,
    SUPPORTED_DOMAINS,
    SUPPORTED_PROGRAM_DOMAINS,
    SUPPORTED_VARIABLE_DOMAINS,
)

_LOGGER = logging.getLogger(__name__)

VAR_BASE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ID): cv.positive_int,
        vol.Required(CONF_TYPE): vol.All(cv.positive_int, vol.In([1, 2])),
        vol.Optional(CONF_ICON): cv.icon,
        vol.Optional(CONF_NAME): cv.string,
    }
)

SENSOR_VAR_SCHEMA = VAR_BASE_SCHEMA.extend(
    {
        vol.Optional(CONF_DEVICE_CLASS): SENSOR_DCS,
        vol.Optional(CONF_UNIT_OF_MEASUREMENT): cv.string,
    }
)

BINARY_SENSOR_VAR_SCHEMA = VAR_BASE_SCHEMA.extend(
    {
        vol.Optional(CONF_DEVICE_CLASS): BINARY_SENSOR_DCS,
        vol.Optional(CONF_PAYLOAD_ON, default=DEFAULT_ON_VALUE): vol.Coerce(int),
        vol.Optional(CONF_PAYLOAD_OFF, default=DEFAULT_OFF_VALUE): vol.Coerce(int),
    }
)

SWITCH_VAR_SCHEMA = VAR_BASE_SCHEMA.extend(
    {
        vol.Optional(CONF_PAYLOAD_ON, default=DEFAULT_ON_VALUE): vol.Coerce(int),
        vol.Optional(CONF_PAYLOAD_OFF, default=DEFAULT_OFF_VALUE): vol.Coerce(int),
    }
)

ISY_VARIABLES_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_SENSORS, default=[]): vol.All(
            cv.ensure_list, [SENSOR_VAR_SCHEMA]
        ),
        vol.Optional(CONF_BINARY_SENSORS, default=[]): vol.All(
            cv.ensure_list, [BINARY_SENSOR_VAR_SCHEMA]
        ),
        vol.Optional(CONF_SWITCHES, default=[]): vol.All(
            cv.ensure_list, [SWITCH_VAR_SCHEMA]
        ),
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_HOST): cv.url,
                vol.Required(CONF_USERNAME): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
                vol.Optional(CONF_TLS_VER): vol.Coerce(float),
                vol.Optional(
                    CONF_IGNORE_STRING, default=DEFAULT_IGNORE_STRING
                ): cv.string,
                vol.Optional(
                    CONF_SENSOR_STRING, default=DEFAULT_SENSOR_STRING
                ): cv.string,
                vol.Optional(CONF_ISY_VARIABLES, default={}): ISY_VARIABLES_SCHEMA,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def _check_for_node_def(hass_isy_data: dict, node, single_domain: str = None) -> bool:
    """Check if the node matches the node_def_id for any domains.

    This is only present on the 5.0 ISY firmware, and is the most reliable
    way to determine a device's type.
    """
    if not hasattr(node, "node_def_id") or node.node_def_id is None:
        # Node doesn't have a node_def (pre 5.0 firmware most likely)
        return False

    node_def_id = node.node_def_id

    domains = SUPPORTED_DOMAINS if not single_domain else [single_domain]
    for domain in domains:
        if node_def_id in NODE_FILTERS[domain]["node_def_id"]:
            hass_isy_data[ISY994_NODES][domain].append(node)
            return True

    return False


def _check_for_insteon_type(
    hass_isy_data: dict, node, single_domain: str = None
) -> bool:
    """Check if the node matches the Insteon type for any domains.

    This is for (presumably) every version of the ISY firmware, but only
    works for Insteon device. "Node Server" (v5+) and Z-Wave and others will
    not have a type.
    """
    if not hasattr(node, "protocol") or node.protocol != PROTO_INSTEON:
        return False
    if not hasattr(node, "type") or node.type is None:
        # Node doesn't have a type (non-Insteon device most likely)
        return False

    device_type = node.type
    domains = SUPPORTED_DOMAINS if not single_domain else [single_domain]
    for domain in domains:
        if any(
            [
                device_type.startswith(t)
                for t in set(NODE_FILTERS[domain]["insteon_type"])
            ]
        ):

            # Hacky special-cases for certain devices with different domains
            # included as subnodes. Note that special-cases are not necessary
            # on ISY 5.x firmware as it uses the superior NodeDefs method

            # FanLinc, which has a light module as one of its nodes.
            if domain == "fan" and str(node.address[-1]) in ["1"]:
                hass_isy_data[ISY994_NODES]["light"].append(node)
                return True

            # Thermostats, which has a "Heat" and "Cool" sub-node on address 2 and 3
            if domain == "climate" and str(node.address[-1]) in ["2", "3"]:
                hass_isy_data[ISY994_NODES]["binary_sensor"].append(node)
                return True

            # IOLincs which have a sensor and relay on 2 different nodes
            if (
                domain == "binary_sensor"
                and device_type.startswith("7.")
                and str(node.address[-1]) in ["2"]
            ):
                hass_isy_data[ISY994_NODES]["switch"].append(node)

            # Smartenit EZIO2X4
            if (
                domain == "switch"
                and device_type.startswith("7.3.255.")
                and str(node.address[-1]) in ["9", "A", "B", "C"]
            ):
                hass_isy_data[ISY994_NODES]["binary_sensor"].append(node)

            hass_isy_data[ISY994_NODES][domain].append(node)
            return True

    return False


def _check_for_zwave_cat(hass_isy_data: dict, node, single_domain: str = None) -> bool:
    """Check if the node matches the ISY Z-Wave Category for any domains.

    This is for (presumably) every version of the ISY firmware, but only
    works for Z-Wave Devices with the devtype.cat property.
    """
    if not hasattr(node, "protocol") or node.protocol != PROTO_ZWAVE:
        return False

    if not hasattr(node, "zwave_props") or node.zwave_props is None:
        # Node doesn't have a device type category (non-Z-Wave device)
        return False

    device_type = node.zwave_props.category
    domains = SUPPORTED_DOMAINS if not single_domain else [single_domain]
    for domain in domains:
        if any(
            [device_type.startswith(t) for t in set(NODE_FILTERS[domain]["zwave_cat"])]
        ):

            hass_isy_data[ISY994_NODES][domain].append(node)
            return True

    return False


def _check_for_uom_id(
    hass_isy_data: dict, node, single_domain: str = None, uom_list: list = None
) -> bool:
    """Check if a node's uom matches any of the domains uom filter.

    This is used for versions of the ISY firmware that report uoms as a single
    ID. We can often infer what type of device it is by that ID.
    """
    if not hasattr(node, "uom") or node.uom is None:
        # Node doesn't have a uom (Scenes for example)
        return False

    node_uom = set(map(str.lower, node.uom))

    if uom_list:
        if node_uom.intersection(uom_list):
            hass_isy_data[ISY994_NODES][single_domain].append(node)
            return True
    else:
        domains = SUPPORTED_DOMAINS if not single_domain else [single_domain]
        for domain in domains:
            if node_uom.intersection(NODE_FILTERS[domain]["uom"]):
                hass_isy_data[ISY994_NODES][domain].append(node)
                return True

    return False


def _check_for_states_in_uom(
    hass_isy_data: dict, node, single_domain: str = None, states_list: list = None
) -> bool:
    """Check if a list of uoms matches two possible filters.

    This is for versions of the ISY firmware that report uoms as a list of all
    possible "human readable" states. This filter passes if all of the possible
    states fit inside the given filter.
    """
    if not hasattr(node, "uom") or node.uom is None:
        # Node doesn't have a uom (Scenes for example)
        return False

    node_uom = set(map(str.lower, node.uom))

    if states_list:
        if node_uom == set(states_list):
            hass_isy_data[ISY994_NODES][single_domain].append(node)
            return True
    else:
        domains = SUPPORTED_DOMAINS if not single_domain else [single_domain]
        for domain in domains:
            if node_uom == set(NODE_FILTERS[domain]["states"]):
                hass_isy_data[ISY994_NODES][domain].append(node)
                return True

    return False


def _is_sensor_a_binary_sensor(hass_isy_data: dict, node) -> bool:
    """Determine if the given sensor node should be a binary_sensor."""
    if _check_for_node_def(hass_isy_data, node, single_domain="binary_sensor"):
        return True
    if _check_for_insteon_type(hass_isy_data, node, single_domain="binary_sensor"):
        return True

    # For the next two checks, we're providing our own set of uoms that
    # represent on/off devices. This is because we can only depend on these
    # checks in the context of already knowing that this is definitely a
    # sensor device.
    if _check_for_uom_id(
        hass_isy_data, node, single_domain="binary_sensor", uom_list=["2", "78"]
    ):
        return True
    if _check_for_states_in_uom(
        hass_isy_data, node, single_domain="binary_sensor", states_list=["on", "off"]
    ):
        return True

    return False


def _categorize_nodes(
    hass_isy_data: dict, nodes, ignore_identifier: str, sensor_identifier: str
) -> None:
    """Sort the nodes to their proper domains."""
    for (path, node) in nodes:
        ignored = ignore_identifier in path or ignore_identifier in node.name
        if ignored:
            # Don't import this node as a device at all
            continue

        if isinstance(node, Group):
            hass_isy_data[ISY994_NODES][SCENE_DOMAIN].append(node)
            continue

        if sensor_identifier in path or sensor_identifier in node.name:
            # User has specified to treat this as a sensor. First we need to
            # determine if it should be a binary_sensor.
            if _is_sensor_a_binary_sensor(hass_isy_data, node):
                continue
            hass_isy_data[ISY994_NODES]["sensor"].append(node)
            continue

        # We have a bunch of different methods for determining the device type,
        # each of which works with different ISY firmware versions or device
        # family. The order here is important, from most reliable to least.
        if _check_for_node_def(hass_isy_data, node):
            continue
        if _check_for_insteon_type(hass_isy_data, node):
            continue
        if _check_for_zwave_cat(hass_isy_data, node):
            continue
        if _check_for_uom_id(hass_isy_data, node):
            continue
        if _check_for_states_in_uom(hass_isy_data, node):
            continue


def _categorize_programs(hass_isy_data: dict, programs: dict) -> None:
    """Categorize the ISY994 programs."""
    for domain in SUPPORTED_PROGRAM_DOMAINS:
        try:
            folder = programs[KEY_MY_PROGRAMS][f"HA.{domain}"]
        except KeyError:
            pass
        else:
            for dtype, _, node_id in folder.children:
                if dtype != KEY_FOLDER:
                    continue
                entity_folder = folder[node_id]
                try:
                    status = entity_folder[KEY_STATUS]
                    assert status.dtype == "program", "Not a program"
                    if domain != "binary_sensor":
                        actions = entity_folder[KEY_ACTIONS]
                        assert actions.dtype == "program", "Not a program"
                    else:
                        actions = None
                except (AttributeError, KeyError, AssertionError):
                    _LOGGER.warning(
                        "Program entity '%s' not loaded due "
                        "to invalid folder structure.",
                        entity_folder.name,
                    )
                    continue

                entity = (entity_folder.name, status, actions)
                hass_isy_data[ISY994_PROGRAMS][domain].append(entity)


def _categorize_variables(
    hass_isy_data: dict, variables, domain_cfg: dict, domain: str
) -> None:
    """Categorize the ISY994 Variables."""
    if domain_cfg is None:
        return
    for isy_var in domain_cfg:
        vid = isy_var.get(CONF_ID)
        vtype = isy_var.get(CONF_TYPE)
        vname = ""
        try:
            vname = variables[vtype][vid].name
        except KeyError as err:
            _LOGGER.error("Error adding ISY Variable %s.%s: %s", vtype, vid, err)
            continue
        else:
            variable = (isy_var, vname, variables[vtype][vid])
            hass_isy_data[ISY994_VARIABLES][domain].append(variable)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the isy994 component from YAML."""
    isy_config: Optional[ConfigType] = config.get(DOMAIN)
    hass.data.setdefault(DOMAIN, {})
    config_entry = _async_find_matching_config_entry(hass)

    if not isy_config:
        if config_entry:
            # Config entry was created because user had configuration.yaml entry
            # They removed that, so remove entry.
            await hass.config_entries.async_remove(config_entry.entry_id)
        return True

    # Only import if we haven't before.
    if not config_entry:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_IMPORT},
                data=dict(isy_config),
            )
        )
        return True

    # Update the entry based on the YAML configuration, in case it changed.
    hass.config_entries.async_update_entry(config_entry, data=dict(isy_config))
    return True


@callback
def _async_find_matching_config_entry(hass):
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.source == config_entries.SOURCE_IMPORT:
            return entry


async def async_setup_entry(
    hass: HomeAssistant, entry: config_entries.ConfigEntry
) -> bool:
    """Set up the ISY 994 platform."""
    hass.data[DOMAIN][entry.entry_id] = {}
    hass_isy_data = hass.data[DOMAIN][entry.entry_id]
    hass_isy_data[ISY994_NODES] = {}
    for domain in SUPPORTED_DOMAINS:
        hass_isy_data[ISY994_NODES][domain] = []

    hass_isy_data[ISY994_PROGRAMS] = {}
    for domain in SUPPORTED_DOMAINS:
        hass_isy_data[ISY994_PROGRAMS][domain] = []

    hass_isy_data[ISY994_VARIABLES] = {}
    for domain in SUPPORTED_VARIABLE_DOMAINS:
        hass_isy_data[ISY994_VARIABLES][domain] = []

    isy_config = entry.data

    # Required
    user = isy_config[CONF_USERNAME]
    password = isy_config[CONF_PASSWORD]
    host = urlparse(isy_config[CONF_HOST])

    # Optional
    tls_version = isy_config.get(CONF_TLS_VER)
    ignore_identifier = isy_config.get(CONF_IGNORE_STRING)
    sensor_identifier = isy_config.get(CONF_SENSOR_STRING)
    isy_variables = isy_config.get(CONF_ISY_VARIABLES, {})

    if host.scheme == "http":
        https = False
        port = host.port or 80
    elif host.scheme == "https":
        https = True
        port = host.port or 443
    else:
        _LOGGER.error("isy994 host value in configuration is invalid")
        return False

    # Connect to ISY controller.
    isy = await hass.async_add_executor_job(
        partial(
            ISY,
            host.hostname,
            port,
            username=user,
            password=password,
            use_https=https,
            tls_ver=tls_version,
            log=_LOGGER,
            webroot=host.path,
        )
    )
    if not isy.connected:
        return False

    _categorize_nodes(hass_isy_data, isy.nodes, ignore_identifier, sensor_identifier)
    _categorize_programs(hass_isy_data, isy.programs)
    _categorize_variables(
        hass_isy_data, isy.variables, isy_variables.get(CONF_SENSORS), "sensor"
    )
    _categorize_variables(
        hass_isy_data,
        isy.variables,
        isy_variables.get(CONF_BINARY_SENSORS),
        "binary_sensor",
    )
    _categorize_variables(
        hass_isy_data, isy.variables, isy_variables.get(CONF_SWITCHES), "switch"
    )

    # Dump ISY Clock Information. Future: Add ISY as sensor to Hass with attrs
    _LOGGER.info(repr(isy.clock))

    hass_isy_data[ISY994_ISY] = isy
    await _async_get_or_create_isy_device_in_registry(hass, entry, isy)

    # Load platforms for the devices in the ISY controller that we support.
    for component in SUPPORTED_DOMAINS:
        hass.async_create_task(
            hass.config_entries.async_forward_entry_setup(entry, component)
        )

    def _start_auto_update() -> None:
        """Start isy auto update."""
        _LOGGER.debug("ISY Starting Event Stream and automatic updates.")
        isy.auto_update = True

    await hass.async_add_executor_job(_start_auto_update)

    return True


async def _async_get_or_create_isy_device_in_registry(
    hass: HomeAssistant, entry: config_entries.ConfigEntry, isy
) -> None:
    device_registry = await dr.async_get_registry(hass)

    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, isy.configuration["uuid"])},
        identifiers={(DOMAIN, isy.configuration["uuid"])},
        manufacturer=MANUFACTURER,
        name=isy.configuration["name"],  # Exposed in PyISY-beta v2.0.0.dev136
        model=isy.configuration["model"],  # Exposed in PyISY-beta v2.0.0.dev135
        sw_version=isy.configuration["firmware"],
    )


async def async_unload_entry(
    hass: HomeAssistant, entry: config_entries.ConfigEntry
) -> bool:
    """Unload a config entry."""
    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, component)
                for component in SUPPORTED_DOMAINS
            ]
        )
    )

    isy = hass.data[DOMAIN][entry.entry_id][ISY994_ISY]

    def _stop_auto_update() -> None:
        """Start isy auto update."""
        _LOGGER.debug("ISY Stopping Event Stream and automatic updates.")
        isy.auto_update = False

    await hass.async_add_executor_job(_stop_auto_update)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def migrate_old_unique_ids(hass, platform, devices):
    """Migrate to new controller-specific unique ids."""
    registry = await async_get_registry(hass)

    for device in devices:
        old_entity_id = registry.async_get_entity_id(
            platform, DOMAIN, device.old_unique_id
        )
        if old_entity_id is not None:
            _LOGGER.debug(
                "Migrating unique_id from [%s] to [%s]",
                device.old_unique_id,
                device.unique_id,
            )
            registry.async_update_entity(old_entity_id, new_unique_id=device.unique_id)

        old_entity_id_2 = registry.async_get_entity_id(
            platform, DOMAIN, device.unique_id.replace(":", "")
        )
        if old_entity_id_2 is not None:
            _LOGGER.debug(
                "Migrating unique_id from [%s] to [%s]",
                device.unique_id.replace(":", ""),
                device.unique_id,
            )
            registry.async_update_entity(
                old_entity_id_2, new_unique_id=device.unique_id
            )


class ISYDevice(Entity):
    """Representation of an ISY994 device."""

    _name: str = None

    def __init__(self, node) -> None:
        """Initialize the insteon device."""
        self._node = node
        self._attrs = {}
        self._change_handler = None
        self._control_handler = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to the node change events."""
        self._change_handler = self._node.status_events.subscribe(self.on_update)

        if hasattr(self._node, "control_events"):
            self._control_handler = self._node.control_events.subscribe(self.on_control)

    def on_update(self, event: object) -> None:
        """Handle the update event from the ISY994 Node."""
        self.schedule_update_ha_state()

    def on_control(self, event: NodeProperty) -> None:
        """Handle a control event from the ISY994 Node."""
        event_data = {
            "entity_id": self.entity_id,
            "control": event.control,
            "value": event.value,
            "formatted": event.formatted,
            "uom": event.uom,
            "precision": event.prec,
        }

        if event.value is None or event.control not in EVENT_PROPS_IGNORED:
            # New state attributes may be available, update the state.
            self.schedule_update_ha_state()

        self.hass.bus.fire("isy994_control", event_data)

    @property
    def device_info(self):
        """Return the device_info of the device."""
        if hasattr(self._node, "protocol") and self._node.protocol == PROTO_GROUP:
            # not a device
            return None
        uuid = self._node.isy.configuration["uuid"]
        device_info = {
            "name": self.name,
            "identifiers": {},
            "model": "Unknown",
            "manufacturer": "Unknown",
            "via_device": (DOMAIN, uuid),
        }
        if hasattr(self._node, "address"):
            device_info["name"] += f" ({self._node.address})"
        if hasattr(self._node, "primary_node"):
            device_info["identifiers"] = {(DOMAIN, f"{uuid}_{self._node.primary_node}")}
        # ISYv5 Device Types
        if hasattr(self._node, "node_def_id") and self._node.node_def_id is not None:
            device_info["model"] = self._node.node_def_id
            # Numerical Device Type
            if hasattr(self._node, "type") and self._node.type is not None:
                device_info["model"] += f" {self._node.type}"
        if hasattr(self._node, "protocol"):
            device_info["manufacturer"] = self._node.protocol
            if self._node.protocol == PROTO_ZWAVE:
                device_info[
                    "manufacturer"
                ] += f" mfr_id:{self._node.zwave_props.mfr_id}"
                device_info["model"] += (
                    f" Type:{self._node.zwave_props.mfr_id} "
                    f"ProductTypeID:{self._node.zwave_props.prod_type_id} "
                    f"ProductID:{self._node.zwave_props.product_id}"
                )
        # Note: sw_version is not exposed by the ISY for the individual devices.

        return device_info

    @property
    def unique_id(self) -> str:
        """Get the unique identifier of the device."""
        if hasattr(self._node, "address"):
            return f"{self._node.isy.configuration['uuid']}_{self._node.address}"
        return None

    @property
    def old_unique_id(self) -> str:
        """Get the old unique identifier of the device."""
        if hasattr(self._node, "address"):
            return self._node.address
        return None

    @property
    def name(self) -> str:
        """Get the name of the device."""
        return self._name or str(self._node.name)

    @property
    def should_poll(self) -> bool:
        """No polling required since we're using the subscription."""
        return False

    @property
    def value(self) -> int:
        """Get the current value of the device."""
        return self._node.status

    @property
    def state(self):
        """Return the state of the ISY device."""
        if self.value == ISY_VALUE_UNKNOWN:
            return STATE_UNKNOWN
        return super().state

    @property
    def device_state_attributes(self) -> Dict:
        """Get the state attributes for the device.

        The 'aux_properties' in the pyisy Node class are combined with the
        other attributes which have been picked up from the event stream and
        the combined result are returned as the device state attributes.
        """
        attr = {}
        if hasattr(self._node, "aux_properties"):
            # Cast as list due to RuntimeError if a new property is added while running.
            for name, value in list(self._node.aux_properties.items()):
                attr_name = COMMAND_FRIENDLY_NAME.get(name, name)
                attr[attr_name] = str(value.formatted).lower()

        # If a Group/Scene, set a property if the entire scene is on/off
        if hasattr(self._node, "group_all_on"):
            # pylint: disable=protected-access
            attr["group_all_on"] = "on" if self._node.group_all_on else "off"

        self._attrs.update(attr)
        return self._attrs
