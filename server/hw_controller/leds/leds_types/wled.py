"""Driver for WLED controllers (ESP8266/ESP32) reached over the network.

Unlike the other drivers in this package, WLED is not wired to a GPIO pin of the
host: the LEDs hang off a separate microcontroller that exposes a JSON API over
HTTP and a realtime UDP protocol. This driver therefore ignores the BCM pin and
talks to `host` instead.

Two transports are used, each for what it is good at:

* the **JSON API** (`POST /json/state`) for state that should stick: power,
  brightness, a solid colour, an effect or a preset. WLED saves this state and
  restores it after a reboot, and it is what Home Assistant sees as well.
* the **realtime UDP protocol** (DRGB/DRGBW/DNRGB on port 21324) for per-pixel
  animations. Realtime frames bypass the effect engine and are much cheaper than
  an HTTP request per frame, at the price of being transient: WLED reverts to its
  own state once frames stop arriving for `realtime_timeout` seconds.

Because the UI sends a colour on every movement of the colour picker, the JSON
writes are coalesced by a sender thread: only the most recent state is pushed and
never more often than MIN_JSON_INTERVAL.
"""

import json
import socket
import urllib.error
import urllib.request
from threading import Thread, Lock, Event

from server.hw_controller.leds.leds_types.generic_LED_driver import GenericLedDriver


# WLED realtime UDP protocol identifiers (first byte of the packet)
WARLS = 1       # index + rgb per led
DRGB = 2        # rgb for every led, starting from led 0
DRGBW = 3       # rgbw for every led, starting from led 0
DNRGB = 4       # start index (uint16 big endian) + rgb, allows to split long strips

DEFAULT_UDP_PORT = 21324
DEFAULT_HTTP_PORT = 80

# WLED drops UDP packets bigger than this, so long strips are sent in several DNRGB chunks
MAX_UDP_PAYLOAD = 1440

# minimum delay between two HTTP writes: the colour picker would otherwise flood the ESP
MIN_JSON_INTERVAL = 0.05


class WLEDUnreachable(Exception):
    """Raised when the WLED device does not answer during the initialization"""


class WLED(GenericLedDriver):

    def __init__(self, leds_number, bcm_pin=None, host=None, http_port=DEFAULT_HTTP_PORT,
                 udp_port=DEFAULT_UDP_PORT, realtime=True, realtime_timeout=2,
                 request_timeout=2.0, rgbw=False, segment=0, takeover_on_start=False,
                 *argvs, **kargvs):
        """
        Args:
            leds_number: number of LEDs to drive. When the device reports its own
                count during the initialization that value wins, so the strip does
                not have to be described twice.
            bcm_pin: unused, kept to match the signature of the other drivers.
            host: hostname or ip address of the WLED device (an `http://host:port`
                url is accepted as well).
            http_port: port of the WLED web server.
            udp_port: port of the WLED realtime protocol.
            realtime: whether per-pixel updates may use the realtime UDP protocol.
            realtime_timeout: seconds after the last realtime frame before WLED
                goes back to its own effects. 255 means "until reset".
            request_timeout: timeout of the HTTP requests, in seconds.
            rgbw: True when the strip has a dedicated white channel.
            segment: index of the WLED segment to drive with the JSON API.
            takeover_on_start: when False (the default) the driver leaves the device
                alone until the user actually asks for a colour from the Sandypi UI.
                A WLED controller is usually shared with Home Assistant and blanking
                it on every restart of the server would be rude.
        """
        host, http_port = self._parse_host(host, http_port)
        if not host:
            raise ValueError("A WLED host (ip address or hostname) must be configured")

        self._host = host
        self._http_port = int(http_port)
        self._udp_port = int(udp_port)
        self._realtime = bool(realtime)
        self._realtime_timeout = int(realtime_timeout)
        self._request_timeout = float(request_timeout)
        self._segment = int(segment)
        self._socket = None
        self.device_info = {}
        # while held, writes are swallowed so that the state left by another
        # controller (Home Assistant, the WLED app, ...) survives a restart
        self._hold = not takeover_on_start

        # state pushed to the device by the sender thread
        self._json_mutex = Lock()
        self._pending_state = None
        self._wake = Event()
        self._sender_running = False
        self._sender_thread = None

        kargvs["colors"] = 4 if rgbw else 3
        super().__init__(leds_number, 0 if bcm_pin is None else bcm_pin, *argvs, **kargvs)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _parse_host(host, http_port):
        """Accepts a bare hostname/ip as well as a full url and splits off the port"""
        if host is None:
            return None, http_port
        host = str(host).strip()
        for prefix in ("http://", "https://"):
            if host.lower().startswith(prefix):
                host = host[len(prefix):]
        host = host.split("/")[0]                       # drops any path that was pasted in
        if ":" in host and not host.startswith("["):    # ipv6 literals are left alone
            host, _, port = host.rpartition(":")
            if port.isdigit():
                http_port = int(port)
        return host, http_port

    @property
    def base_url(self):
        return "http://{}:{}".format(self._host, self._http_port)

    def _request(self, path, payload=None):
        """Performs a GET (payload None) or a POST against the WLED JSON API"""
        url = self.base_url + path
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=self._request_timeout) as response:
            body = response.read().decode("utf-8")
        if not body:
            return {}
        try:
            return json.loads(body)
        except ValueError:
            return {}

    # -------------------------------------------------------------- json writes

    def _queue_state(self, state):
        """Stores the state to be pushed and wakes the sender thread up"""
        with self._json_mutex:
            if self._pending_state is None:
                self._pending_state = {}
            # merging so that a colour update does not drop a brightness update queued just before
            for key, value in state.items():
                if key == "seg" and "seg" in self._pending_state:
                    self._pending_state["seg"][0].update(value[0])
                else:
                    self._pending_state[key] = value
        self._wake.set()

    def _sender_loop(self):
        while self._sender_running:
            self._wake.wait(timeout=0.5)
            self._wake.clear()
            if not self._sender_running:
                break
            with self._json_mutex:
                state = self._pending_state
                self._pending_state = None
            if state is None:
                continue
            try:
                self._request("/json/state", state)
            except (urllib.error.URLError, OSError, ValueError) as e:
                self.logger.warning("Cannot reach the WLED device at {}: {}".format(self.base_url, e))
            # coalesces the updates generated while the request was in flight
            self._wake.wait(timeout=MIN_JSON_INTERVAL)

    def _start_sender(self):
        self._sender_running = True
        self._sender_thread = Thread(target=self._sender_loop, daemon=True)
        self._sender_thread.name = "wled_sender"
        self._sender_thread.start()

    def _stop_sender(self):
        self._sender_running = False
        self._wake.set()
        if self._sender_thread is not None:
            self._sender_thread.join(timeout=1)
            self._sender_thread = None

    # ------------------------------------------------------------ realtime udp

    def _send_realtime(self, colors):
        """Pushes a full frame of per-pixel colours with the WLED realtime protocol"""
        if self._socket is None:
            return
        rgbw = self.colors == 4
        bytes_per_led = 4 if rgbw else 3
        protocol = DRGBW if rgbw else DRGB
        # DRGB/DRGBW can only address the leds from index 0, so long strips use DNRGB chunks
        leds_per_packet = (MAX_UDP_PAYLOAD - 4) // bytes_per_led
        if len(colors) <= (MAX_UDP_PAYLOAD - 2) // bytes_per_led:
            packets = [(protocol, 0, colors)]
        else:
            packets = [(DNRGB, start, colors[start:start + leds_per_packet])
                       for start in range(0, len(colors), leds_per_packet)]

        for proto, start, chunk in packets:
            payload = bytearray((proto, min(255, self._realtime_timeout)))
            if proto == DNRGB:
                payload += start.to_bytes(2, "big")
            for color in chunk:
                for i in range(bytes_per_led):
                    payload.append(int(color[i]) & 0xFF if i < len(color) else 0)
            try:
                self._socket.sendto(bytes(payload), (self._host, self._udp_port))
            except OSError as e:
                self.logger.warning("Cannot send a realtime frame to WLED: {}".format(e))
                return

    # -------------------------------------------------------------- public api

    def _normalize_color(self, color):
        """Pads/truncates the colour to the channel count of the strip.

        Contrary to the other drivers the brightness is *not* baked into the
        colour: WLED applies its own master brightness, which both avoids the
        banding of a pre-multiplied colour at low brightness and keeps the value
        consistent with what Home Assistant reports.
        """
        if type(color[0]) in (list, tuple):
            return [self._normalize_color(c) for c in color]
        tmp = [0] * self.colors
        for i in range(min(self.colors, len(color))):
            tmp[i] = max(0, min(255, int(color[i])))
        return tuple(tmp)

    def fill(self, color):
        """Sets a solid colour on the whole strip through the JSON API"""
        normalized = self._normalize_color(color)
        self._original_colors[:] = [color] * self.leds_number
        self.pixels[:] = [normalized] * self.leds_number
        if self._hold:
            return
        self._queue_state({
            "on": any(normalized),
            "seg": [{"id": self._segment, "fx": 0, "col": [list(normalized)]}]
        })

    def show(self):
        """Pushes the current per-pixel buffer to the device as a realtime frame"""
        if self._realtime and not self._hold:
            self._send_realtime(self.pixels)

    def set_brightness(self, brightness):
        """Sets the WLED master brightness (the value is expected in the 0-1 range)"""
        brightness = min(1, max(0, brightness))
        if brightness != self.brightness:
            self.brightness = brightness
            if self._hold:
                return
            self._queue_state({"bri": int(round(brightness * 255)), "on": brightness > 0})

    def release(self):
        """Stops preserving the state the device had when the server started.

        Called on the first explicit request coming from the UI: from that moment
        on Sandypi owns the strip.
        """
        self._hold = False

    def set_effect(self, effect, speed=None, intensity=None, palette=None):
        """Starts one of the built-in WLED effects on the driven segment.

        Args:
            effect: index of the effect, or its name as listed by `get_effects()`.
        """
        if isinstance(effect, str):
            effects = self.get_effects()
            lowered = [e.lower() for e in effects]
            if effect.lower() not in lowered:
                raise ValueError("Unknown WLED effect: {}".format(effect))
            effect = lowered.index(effect.lower())
        segment = {"id": self._segment, "fx": int(effect)}
        if speed is not None:
            segment["sx"] = int(speed)
        if intensity is not None:
            segment["ix"] = int(intensity)
        if palette is not None:
            segment["pal"] = int(palette)
        self._queue_state({"on": True, "seg": [segment]})

    def set_preset(self, preset_id):
        """Recalls a preset saved on the WLED device"""
        self._queue_state({"on": True, "ps": int(preset_id)})

    def get_effects(self):
        """Returns the list of the effect names supported by the device"""
        try:
            return self._request("/json/effects")
        except (urllib.error.URLError, OSError, ValueError):
            return []

    def get_palettes(self):
        """Returns the list of the palette names supported by the device"""
        try:
            return self._request("/json/palettes")
        except (urllib.error.URLError, OSError, ValueError):
            return []

    def is_connected(self):
        try:
            self._request("/json/info")
            return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    # -------------------------------------------------- abstract methods override

    def init_pixels(self):
        try:
            info = self._request("/json/info")
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise WLEDUnreachable("No WLED device answered at {}: {}".format(self.base_url, e))

        self.device_info = info
        # the device knows how many leds it drives: trust it over the settings
        reported = info.get("leds", {}).get("count")
        if reported:
            self.leds_number = int(reported)
        # ...and whether they have a white channel
        if info.get("leds", {}).get("rgbw") is not None:
            self.colors = 4 if info["leds"]["rgbw"] else 3

        self.pixels = [(0,) * self.colors] * self.leds_number
        self._original_colors = [[0] * self.colors for _ in range(self.leds_number)]

        if self._realtime:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        self._start_sender()
        self.logger.info("Connected to WLED '{}' ({} leds) at {}".format(
            info.get("name", "unknown"), self.leds_number, self.base_url))

        if self._hold:
            self._seed_from_device()
        else:
            self.clear()

    def _seed_from_device(self):
        """Reads the current state of the device so that the UI shows it as it is"""
        try:
            state = self._request("/json/state")
        except (urllib.error.URLError, OSError, ValueError):
            return
        self.brightness = int(state.get("bri", 255)) / 255.0
        segments = state.get("seg") or []
        if segments and segments[0].get("col"):
            color = self._normalize_color(segments[0]["col"][0])
            self.pixels[:] = [color] * self.leds_number
            self._original_colors[:] = [list(color)] * self.leds_number

    def deinit(self):
        try:
            if not self._hold:
                self.clear()
        finally:
            self._stop_sender()
            if self._socket is not None:
                self._socket.close()
                self._socket = None
