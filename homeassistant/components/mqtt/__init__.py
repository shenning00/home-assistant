"""
Support for MQTT message handling.

For more details about this component, please refer to the documentation at
https://home-assistant.io/components/mqtt/
"""
import asyncio
import logging
import os
import socket
import time

import voluptuous as vol

from homeassistant.core import callback
from homeassistant.bootstrap import async_prepare_setup_platform
from homeassistant.config import load_yaml_config_file
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import template, config_validation as cv
from homeassistant.util.async import (
    run_coroutine_threadsafe, run_callback_threadsafe)
from homeassistant.const import (
    EVENT_HOMEASSISTANT_START, EVENT_HOMEASSISTANT_STOP, CONF_VALUE_TEMPLATE,
    CONF_USERNAME, CONF_PASSWORD, CONF_PORT, CONF_PROTOCOL, CONF_PAYLOAD)
from homeassistant.components.mqtt.server import HBMQTT_CONFIG_SCHEMA

_LOGGER = logging.getLogger(__name__)

DOMAIN = 'mqtt'

DATA_MQTT = 'mqtt'

SERVICE_PUBLISH = 'publish'
EVENT_MQTT_MESSAGE_RECEIVED = 'mqtt_message_received'

REQUIREMENTS = ['paho-mqtt==1.2']

CONF_EMBEDDED = 'embedded'
CONF_BROKER = 'broker'
CONF_CLIENT_ID = 'client_id'
CONF_DISCOVERY = 'discovery'
CONF_DISCOVERY_PREFIX = 'discovery_prefix'
CONF_KEEPALIVE = 'keepalive'
CONF_CERTIFICATE = 'certificate'
CONF_CLIENT_KEY = 'client_key'
CONF_CLIENT_CERT = 'client_cert'
CONF_TLS_INSECURE = 'tls_insecure'

CONF_BIRTH_MESSAGE = 'birth_message'
CONF_WILL_MESSAGE = 'will_message'

CONF_STATE_TOPIC = 'state_topic'
CONF_COMMAND_TOPIC = 'command_topic'
CONF_QOS = 'qos'
CONF_RETAIN = 'retain'

PROTOCOL_31 = '3.1'
PROTOCOL_311 = '3.1.1'

DEFAULT_PORT = 1883
DEFAULT_KEEPALIVE = 60
DEFAULT_QOS = 0
DEFAULT_RETAIN = False
DEFAULT_PROTOCOL = PROTOCOL_311
DEFAULT_DISCOVERY = False
DEFAULT_DISCOVERY_PREFIX = 'homeassistant'

ATTR_TOPIC = 'topic'
ATTR_PAYLOAD = 'payload'
ATTR_PAYLOAD_TEMPLATE = 'payload_template'
ATTR_QOS = CONF_QOS
ATTR_RETAIN = CONF_RETAIN

MAX_RECONNECT_WAIT = 300  # seconds


def valid_subscribe_topic(value, invalid_chars='\0'):
    """Validate that we can subscribe using this MQTT topic."""
    value = cv.string(value)
    if all(c not in value for c in invalid_chars):
        return vol.Length(min=1, max=65535)(value)
    raise vol.Invalid('Invalid MQTT topic name')


def valid_publish_topic(value):
    """Validate that we can publish using this MQTT topic."""
    return valid_subscribe_topic(value, invalid_chars='#+\0')


def valid_discovery_topic(value):
    """Validate a discovery topic."""
    return valid_subscribe_topic(value, invalid_chars='#+\0/')


_VALID_QOS_SCHEMA = vol.All(vol.Coerce(int), vol.In([0, 1, 2]))

CLIENT_KEY_AUTH_MSG = 'client_key and client_cert must both be present in ' \
                      'the mqtt broker config'

MQTT_WILL_BIRTH_SCHEMA = vol.Schema({
    vol.Required(ATTR_TOPIC): valid_publish_topic,
    vol.Required(ATTR_PAYLOAD, CONF_PAYLOAD): cv.string,
    vol.Optional(ATTR_QOS, default=DEFAULT_QOS): _VALID_QOS_SCHEMA,
    vol.Optional(ATTR_RETAIN, default=DEFAULT_RETAIN): cv.boolean,
}, required=True)

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Optional(CONF_CLIENT_ID): cv.string,
        vol.Optional(CONF_KEEPALIVE, default=DEFAULT_KEEPALIVE):
            vol.All(vol.Coerce(int), vol.Range(min=15)),
        vol.Optional(CONF_BROKER): cv.string,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
        vol.Optional(CONF_USERNAME): cv.string,
        vol.Optional(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_CERTIFICATE): cv.isfile,
        vol.Inclusive(CONF_CLIENT_KEY, 'client_key_auth',
                      msg=CLIENT_KEY_AUTH_MSG): cv.isfile,
        vol.Inclusive(CONF_CLIENT_CERT, 'client_key_auth',
                      msg=CLIENT_KEY_AUTH_MSG): cv.isfile,
        vol.Optional(CONF_TLS_INSECURE): cv.boolean,
        vol.Optional(CONF_PROTOCOL, default=DEFAULT_PROTOCOL):
            vol.All(cv.string, vol.In([PROTOCOL_31, PROTOCOL_311])),
        vol.Optional(CONF_EMBEDDED): HBMQTT_CONFIG_SCHEMA,
        vol.Optional(CONF_WILL_MESSAGE): MQTT_WILL_BIRTH_SCHEMA,
        vol.Optional(CONF_BIRTH_MESSAGE): MQTT_WILL_BIRTH_SCHEMA,
        vol.Optional(CONF_DISCOVERY, default=DEFAULT_DISCOVERY): cv.boolean,
        vol.Optional(CONF_DISCOVERY_PREFIX,
                     default=DEFAULT_DISCOVERY_PREFIX): valid_discovery_topic,
    }),
}, extra=vol.ALLOW_EXTRA)

SCHEMA_BASE = {
    vol.Optional(CONF_QOS, default=DEFAULT_QOS): _VALID_QOS_SCHEMA,
}

MQTT_BASE_PLATFORM_SCHEMA = cv.PLATFORM_SCHEMA.extend(SCHEMA_BASE)

# Sensor type platforms subscribe to MQTT events
MQTT_RO_PLATFORM_SCHEMA = MQTT_BASE_PLATFORM_SCHEMA.extend({
    vol.Required(CONF_STATE_TOPIC): valid_subscribe_topic,
    vol.Optional(CONF_VALUE_TEMPLATE): cv.template,
})

# Switch type platforms publish to MQTT and may subscribe
MQTT_RW_PLATFORM_SCHEMA = MQTT_BASE_PLATFORM_SCHEMA.extend({
    vol.Required(CONF_COMMAND_TOPIC): valid_publish_topic,
    vol.Optional(CONF_RETAIN, default=DEFAULT_RETAIN): cv.boolean,
    vol.Optional(CONF_STATE_TOPIC): valid_subscribe_topic,
    vol.Optional(CONF_VALUE_TEMPLATE): cv.template,
})


# Service call validation schema
MQTT_PUBLISH_SCHEMA = vol.Schema({
    vol.Required(ATTR_TOPIC): valid_publish_topic,
    vol.Exclusive(ATTR_PAYLOAD, CONF_PAYLOAD): object,
    vol.Exclusive(ATTR_PAYLOAD_TEMPLATE, CONF_PAYLOAD): cv.string,
    vol.Optional(ATTR_QOS, default=DEFAULT_QOS): _VALID_QOS_SCHEMA,
    vol.Optional(ATTR_RETAIN, default=DEFAULT_RETAIN): cv.boolean,
}, required=True)


def _build_publish_data(topic, qos, retain):
    """Build the arguments for the publish service without the payload."""
    data = {ATTR_TOPIC: topic}
    if qos is not None:
        data[ATTR_QOS] = qos
    if retain is not None:
        data[ATTR_RETAIN] = retain
    return data


def publish(hass, topic, payload, qos=None, retain=None):
    """Publish message to an MQTT topic."""
    data = _build_publish_data(topic, qos, retain)
    data[ATTR_PAYLOAD] = payload
    hass.services.call(DOMAIN, SERVICE_PUBLISH, data)


def publish_template(hass, topic, payload_template, qos=None, retain=None):
    """Publish message to an MQTT topic using a template payload."""
    data = _build_publish_data(topic, qos, retain)
    data[ATTR_PAYLOAD_TEMPLATE] = payload_template
    hass.services.call(DOMAIN, SERVICE_PUBLISH, data)


@asyncio.coroutine
def async_subscribe(hass, topic, msg_callback, qos=DEFAULT_QOS):
    """Subscribe to an MQTT topic."""
    @callback
    def async_mqtt_topic_subscriber(event):
        """Match subscribed MQTT topic."""
        if not _match_topic(topic, event.data[ATTR_TOPIC]):
            return

        hass.async_run_job(msg_callback, event.data[ATTR_TOPIC],
                           event.data[ATTR_PAYLOAD], event.data[ATTR_QOS])

    async_remove = hass.bus.async_listen(
        EVENT_MQTT_MESSAGE_RECEIVED, async_mqtt_topic_subscriber)

    yield from hass.data[DATA_MQTT].async_subscribe(topic, qos)
    return async_remove


def subscribe(hass, topic, msg_callback, qos=DEFAULT_QOS):
    """Subscribe to an MQTT topic."""
    async_remove = run_coroutine_threadsafe(
        async_subscribe(hass, topic, msg_callback, qos),
        hass.loop
    ).result()

    def remove():
        """Remove listener convert."""
        run_callback_threadsafe(hass.loop, async_remove).result()

    return remove


@asyncio.coroutine
def _async_setup_server(hass, config):
    """Try to start embedded MQTT broker.

    This method is a coroutine.
    """
    conf = config.get(DOMAIN, {})

    server = yield from async_prepare_setup_platform(
        hass, config, DOMAIN, 'server')

    if server is None:
        _LOGGER.error("Unable to load embedded server")
        return None

    success, broker_config = \
        yield from server.async_start(hass, conf.get(CONF_EMBEDDED))

    return success and broker_config


@asyncio.coroutine
def _async_setup_discovery(hass, config):
    """Try to start the discovery of MQTT devices.

    This method is a coroutine.
    """
    conf = config.get(DOMAIN, {})

    discovery = yield from async_prepare_setup_platform(
        hass, config, DOMAIN, 'discovery')

    if discovery is None:
        _LOGGER.error("Unable to load MQTT discovery")
        return None

    success = yield from discovery.async_start(
        hass, conf[CONF_DISCOVERY_PREFIX], config)

    return success


@asyncio.coroutine
def async_setup(hass, config):
    """Start the MQTT protocol service."""
    conf = config.get(DOMAIN, {})

    client_id = conf.get(CONF_CLIENT_ID)
    keepalive = conf.get(CONF_KEEPALIVE)

    # Only setup if embedded config passed in or no broker specified
    if CONF_EMBEDDED not in conf and CONF_BROKER in conf:
        broker_config = None
    else:
        broker_config = yield from _async_setup_server(hass, config)

    if CONF_BROKER in conf:
        broker = conf[CONF_BROKER]
        port = conf[CONF_PORT]
        username = conf.get(CONF_USERNAME)
        password = conf.get(CONF_PASSWORD)
        certificate = conf.get(CONF_CERTIFICATE)
        client_key = conf.get(CONF_CLIENT_KEY)
        client_cert = conf.get(CONF_CLIENT_CERT)
        tls_insecure = conf.get(CONF_TLS_INSECURE)
        protocol = conf[CONF_PROTOCOL]
    elif broker_config:
        # If no broker passed in, auto config to internal server
        broker, port, username, password, certificate, protocol = broker_config
        # Embedded broker doesn't have some ssl variables
        client_key, client_cert, tls_insecure = None, None, None
    else:
        err = "Unable to start MQTT broker."
        if conf.get(CONF_EMBEDDED) is not None:
            # Explicit embedded config, requires explicit broker config
            err += " (Broker configuration required.)"
        _LOGGER.error(err)
        return False

    # For cloudmqtt.com, secured connection, auto fill in certificate
    if certificate is None and 19999 < port < 30000 and \
       broker.endswith('.cloudmqtt.com'):
        certificate = os.path.join(os.path.dirname(__file__),
                                   'addtrustexternalcaroot.crt')

    will_message = conf.get(CONF_WILL_MESSAGE)
    birth_message = conf.get(CONF_BIRTH_MESSAGE)

    try:
        hass.data[DATA_MQTT] = MQTT(
            hass, broker, port, client_id, keepalive, username, password,
            certificate, client_key, client_cert, tls_insecure, protocol,
            will_message, birth_message)
    except socket.error:
        _LOGGER.exception("Can't connect to the broker. "
                          "Please check your settings and the broker itself")
        return False

    @asyncio.coroutine
    def async_stop_mqtt(event):
        """Stop MQTT component."""
        yield from hass.data[DATA_MQTT].async_stop()

    @asyncio.coroutine
    def async_start_mqtt(event):
        """Launch MQTT component when Home Assistant starts up."""
        yield from hass.data[DATA_MQTT].async_start()
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, async_stop_mqtt)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, async_start_mqtt)

    success = yield from hass.data[DATA_MQTT].async_connect()

    if not success:
        return False

    @asyncio.coroutine
    def async_publish_service(call):
        """Handle MQTT publish service calls."""
        msg_topic = call.data[ATTR_TOPIC]
        payload = call.data.get(ATTR_PAYLOAD)
        payload_template = call.data.get(ATTR_PAYLOAD_TEMPLATE)
        qos = call.data[ATTR_QOS]
        retain = call.data[ATTR_RETAIN]
        try:
            if payload_template is not None:
                payload = \
                    template.Template(payload_template, hass).async_render()
        except template.jinja2.TemplateError as exc:
            _LOGGER.error(
                "Unable to publish to '%s': rendering payload template of "
                "'%s' failed because %s",
                msg_topic, payload_template, exc)
            return

        yield from hass.data[DATA_MQTT].async_publish(
            msg_topic, payload, qos, retain)

    descriptions = yield from hass.loop.run_in_executor(
        None, load_yaml_config_file, os.path.join(
            os.path.dirname(__file__), 'services.yaml'))

    hass.services.async_register(
        DOMAIN, SERVICE_PUBLISH, async_publish_service,
        descriptions.get(SERVICE_PUBLISH), schema=MQTT_PUBLISH_SCHEMA)

    if conf.get(CONF_DISCOVERY):
        yield from _async_setup_discovery(hass, config)

    return True


class MQTT(object):
    """Home Assistant MQTT client."""

    def __init__(self, hass, broker, port, client_id, keepalive, username,
                 password, certificate, client_key, client_cert,
                 tls_insecure, protocol, will_message, birth_message):
        """Initialize Home Assistant MQTT client."""
        import paho.mqtt.client as mqtt

        self.hass = hass
        self.broker = broker
        self.port = port
        self.keepalive = keepalive
        self.topics = {}
        self.progress = {}
        self.birth_message = birth_message
        self._mqttc = None

        if protocol == PROTOCOL_31:
            proto = mqtt.MQTTv31
        else:
            proto = mqtt.MQTTv311

        if client_id is None:
            self._mqttc = mqtt.Client(protocol=proto)
        else:
            self._mqttc = mqtt.Client(client_id, protocol=proto)

        if username is not None:
            self._mqttc.username_pw_set(username, password)

        if certificate is not None:
            self._mqttc.tls_set(
                certificate, certfile=client_cert, keyfile=client_key)

        if tls_insecure is not None:
            self._mqttc.tls_insecure_set(tls_insecure)

        self._mqttc.on_subscribe = self._mqtt_on_subscribe
        self._mqttc.on_unsubscribe = self._mqtt_on_unsubscribe
        self._mqttc.on_connect = self._mqtt_on_connect
        self._mqttc.on_disconnect = self._mqtt_on_disconnect
        self._mqttc.on_message = self._mqtt_on_message

        if will_message:
            self._mqttc.will_set(will_message.get(ATTR_TOPIC),
                                 will_message.get(ATTR_PAYLOAD),
                                 will_message.get(ATTR_QOS),
                                 will_message.get(ATTR_RETAIN))

    def async_publish(self, topic, payload, qos, retain):
        """Publish a MQTT message.

        This method must be run in the event loop and returns a coroutine.
        """
        return self.hass.loop.run_in_executor(
            None, self._mqttc.publish, topic, payload, qos, retain)

    @asyncio.coroutine
    def async_connect(self):
        """Connect to the host. Does not process messages yet.

        This method must be run in the event loop and returns a coroutine.
        """
        result = yield from self.hass.loop.run_in_executor(
            None, self._mqttc.connect, self.broker, self.port, self.keepalive)

        if result != 0:
            import paho.mqtt.client as mqtt
            _LOGGER.error('Failed to connect: %s', mqtt.error_string(result))

        return not result

    def async_start(self):
        """Run the MQTT client.

        This method must be run in the event loop and returns a coroutine.
        """
        return self.hass.loop.run_in_executor(None, self._mqttc.loop_start)

    def async_stop(self):
        """Stop the MQTT client.

        This method must be run in the event loop and returns a coroutine.
        """
        def stop():
            """Stop the MQTT client."""
            self._mqttc.disconnect()
            self._mqttc.loop_stop()

        return self.hass.loop.run_in_executor(None, stop)

    @asyncio.coroutine
    def async_subscribe(self, topic, qos):
        """Subscribe to a topic.

        This method is a coroutine.
        """
        if not isinstance(topic, str):
            raise HomeAssistantError("topic need to be a string!")

        if topic in self.topics:
            return
        result, mid = yield from self.hass.loop.run_in_executor(
            None, self._mqttc.subscribe, topic, qos)

        _raise_on_error(result)
        self.progress[mid] = topic
        self.topics[topic] = None

    @asyncio.coroutine
    def async_unsubscribe(self, topic):
        """Unsubscribe from topic.

        This method is a coroutine.
        """
        result, mid = yield from self.hass.loop.run_in_executor(
            None, self._mqttc.unsubscribe, topic)

        _raise_on_error(result)
        self.progress[mid] = topic

    def _mqtt_on_connect(self, _mqttc, _userdata, _flags, result_code):
        """On connect callback.

        Resubscribe to all topics we were subscribed to and publish birth
        message.
        """
        import paho.mqtt.client as mqtt

        if result_code != mqtt.CONNACK_ACCEPTED:
            _LOGGER.error('Unable to connect to the MQTT broker: %s',
                          mqtt.connack_string(result_code))
            self._mqttc.disconnect()
            return

        old_topics = self.topics

        self.topics = {key: value for key, value in self.topics.items()
                       if value is None}

        for topic, qos in old_topics.items():
            # qos is None if we were in process of subscribing
            if qos is not None:
                self.hass.add_job(self.async_subscribe, topic, qos)

        if self.birth_message:
            self.hass.add_job(self.async_publish(
                self.birth_message.get(ATTR_TOPIC),
                self.birth_message.get(ATTR_PAYLOAD),
                self.birth_message.get(ATTR_QOS),
                self.birth_message.get(ATTR_RETAIN)))

    def _mqtt_on_subscribe(self, _mqttc, _userdata, mid, granted_qos):
        """Subscribe successful callback."""
        topic = self.progress.pop(mid, None)
        if topic is None:
            return
        self.topics[topic] = granted_qos[0]

    def _mqtt_on_message(self, _mqttc, _userdata, msg):
        """Message received callback."""
        try:
            payload = msg.payload.decode('utf-8')
        except (AttributeError, UnicodeDecodeError):
            _LOGGER.error("Illegal utf-8 unicode payload from "
                          "MQTT topic: %s, Payload: %s", msg.topic,
                          msg.payload)
        else:
            _LOGGER.debug("Received message on %s: %s",
                          msg.topic, payload)
            self.hass.bus.fire(EVENT_MQTT_MESSAGE_RECEIVED, {
                ATTR_TOPIC: msg.topic,
                ATTR_QOS: msg.qos,
                ATTR_PAYLOAD: payload,
            })

    def _mqtt_on_unsubscribe(self, _mqttc, _userdata, mid, granted_qos):
        """Unsubscribe successful callback."""
        topic = self.progress.pop(mid, None)
        if topic is None:
            return
        self.topics.pop(topic, None)

    def _mqtt_on_disconnect(self, _mqttc, _userdata, result_code):
        """Disconnected callback."""
        self.progress = {}
        self.topics = {key: value for key, value in self.topics.items()
                       if value is not None}

        # Remove None values from topic list
        for key in list(self.topics):
            if self.topics[key] is None:
                self.topics.pop(key)

        # When disconnected because of calling disconnect()
        if result_code == 0:
            return

        tries = 0
        wait_time = 0

        while True:
            try:
                if self._mqttc.reconnect() == 0:
                    _LOGGER.info("Successfully reconnected to the MQTT server")
                    break
            except socket.error:
                pass

            wait_time = min(2**tries, MAX_RECONNECT_WAIT)
            _LOGGER.warning(
                "Disconnected from MQTT (%s). Trying to reconnect in %s s",
                result_code, wait_time)
            # It is ok to sleep here as we are in the MQTT thread.
            time.sleep(wait_time)
            tries += 1


def _raise_on_error(result):
    """Raise error if error result."""
    if result != 0:
        import paho.mqtt.client as mqtt

        raise HomeAssistantError(
            'Error talking to MQTT: {}'.format(mqtt.error_string(result)))


def _match_topic(subscription, topic):
    """Test if topic matches subscription."""
    if subscription.endswith('#'):
        return (subscription[:-2] == topic or
                topic.startswith(subscription[:-1]))

    sub_parts = subscription.split('/')
    topic_parts = topic.split('/')

    return (len(sub_parts) == len(topic_parts) and
            all(a == b for a, b in zip(sub_parts, topic_parts) if a != '+'))
