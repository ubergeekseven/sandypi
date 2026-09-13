"""Tests for the WLED led driver.

A minimal fake WLED device (HTTP JSON API + realtime UDP listener) is started on
localhost so that the driver can be exercised end to end without any hardware.
"""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from time import sleep, time

import pytest

from server.hw_controller.leds.leds_types.wled import WLED, WLEDUnreachable, DRGB, DNRGB


INFO = {"ver": "0.14.0", "name": "Fake table", "leds": {"count": 8, "rgbw": False}}


class FakeWLEDHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/json/info":
            self._reply(INFO)
        elif self.path == "/json/state":
            self._reply(self.server.state)
        elif self.path == "/json/effects":
            self._reply(["Solid", "Blink", "Rainbow"])
        elif self.path == "/json/palettes":
            self._reply(["Default", "Party"])
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode()) if length else {}
        self.server.received.append(body)
        self.server.state.update(body)
        self._reply({"success": True})


@pytest.fixture
def fake_wled():
    server = HTTPServer(("127.0.0.1", 0), FakeWLEDHandler)
    server.received = []
    server.state = {"on": True, "bri": 200, "seg": [{"id": 0, "col": [[10, 20, 30, 0]]}]}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def udp_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2)
    yield sock
    sock.close()


def make_driver(fake_wled, **kwargs):
    kwargs.setdefault("takeover_on_start", True)
    kwargs.setdefault("realtime", False)
    return WLED(1, host="127.0.0.1", http_port=fake_wled.server_port, **kwargs)


def wait_for(predicate, timeout=3):
    deadline = time() + timeout
    while time() < deadline:
        if predicate():
            return True
        sleep(0.02)
    return False


# --------------------------------------------------------------------- host parsing

@pytest.mark.parametrize("value, expected", [
    ("192.168.1.50", ("192.168.1.50", 80)),
    ("http://192.168.1.50", ("192.168.1.50", 80)),
    ("http://wled.local:8080/", ("wled.local", 8080)),
    ("  wled.local  ", ("wled.local", 80)),
    ("wled.local:81", ("wled.local", 81)),
])
def test_parse_host(value, expected):
    assert WLED._parse_host(value, 80) == expected


def test_missing_host_is_rejected():
    with pytest.raises(ValueError):
        WLED(10, host="")


def test_unreachable_device_raises():
    # port 1 is not going to answer: the controller relies on this to disable the driver
    with pytest.raises(WLEDUnreachable):
        WLED(10, host="127.0.0.1", http_port=1, request_timeout=0.3)


# ------------------------------------------------------------------------ discovery

def test_strip_description_comes_from_the_device(fake_wled):
    driver = make_driver(fake_wled)                          # the settings describe 1 led
    try:
        assert driver.leds_number == INFO["leds"]["count"]   # the settings said 1 led, the device says 8
        assert driver.colors == 3                            # rgbw is False on the fake device
        assert driver.device_info["name"] == "Fake table"
    finally:
        driver.deinit()


# --------------------------------------------------------------------- json writes

def test_fill_sends_a_solid_color(fake_wled):
    driver = make_driver(fake_wled)
    try:
        driver.fill((255, 128, 0))
        assert wait_for(lambda: any("seg" in r for r in fake_wled.received))
        state = [r for r in fake_wled.received if "seg" in r][-1]
        assert state["seg"][0]["col"] == [[255, 128, 0]]
        assert state["seg"][0]["fx"] == 0                    # a solid color must stop any running effect
        assert state["on"] is True
    finally:
        driver.deinit()


def test_black_turns_the_device_off(fake_wled):
    driver = make_driver(fake_wled)
    try:
        driver.fill((0, 0, 0))
        assert wait_for(lambda: any("seg" in r for r in fake_wled.received))
        assert [r for r in fake_wled.received if "seg" in r][-1]["on"] is False
    finally:
        driver.deinit()


def test_brightness_uses_the_native_wled_scale(fake_wled):
    driver = make_driver(fake_wled)
    try:
        driver.set_brightness(0.5)
        assert wait_for(lambda: any("bri" in r for r in fake_wled.received))
        assert [r for r in fake_wled.received if "bri" in r][-1]["bri"] == 128
        # the colour must not be pre-multiplied by the brightness: WLED does that itself
        assert driver._normalize_color((200, 200, 200)) == (200, 200, 200)
    finally:
        driver.deinit()


def test_updates_are_coalesced(fake_wled):
    """Dragging the colour picker must not generate one http request per pixel of travel"""
    driver = make_driver(fake_wled)
    try:
        for i in range(60):
            driver.fill((i * 4, 0, 0))
        assert wait_for(lambda: fake_wled.received and fake_wled.received[-1]["seg"][0]["col"] == [[236, 0, 0]])
        sleep(0.3)
        assert len(fake_wled.received) < 60      # coalesced, the exact number depends on the timing
    finally:
        driver.deinit()


def test_effects_and_presets(fake_wled):
    driver = make_driver(fake_wled)
    try:
        assert driver.get_effects() == ["Solid", "Blink", "Rainbow"]
        assert driver.get_palettes() == ["Default", "Party"]

        driver.set_effect("Rainbow", speed=100, intensity=200, palette=1)
        assert wait_for(lambda: any(r.get("seg", [{}])[0].get("fx") == 2 for r in fake_wled.received))
        segment = [r for r in fake_wled.received if r.get("seg", [{}])[0].get("fx") == 2][-1]["seg"][0]
        assert (segment["sx"], segment["ix"], segment["pal"]) == (100, 200, 1)

        with pytest.raises(ValueError):
            driver.set_effect("Does not exist")

        driver.set_preset(3)
        assert wait_for(lambda: any(r.get("ps") == 3 for r in fake_wled.received))
    finally:
        driver.deinit()


# ------------------------------------------------------------------ realtime frames

def test_realtime_frame_is_a_single_drgb_packet(fake_wled, udp_listener):
    driver = WLED(8, host="127.0.0.1", http_port=fake_wled.server_port,
                  udp_port=udp_listener.getsockname()[1], realtime=True, takeover_on_start=True)
    try:
        for i in range(driver.leds_number):
            driver[i] = (i, 2 * i, 3 * i)
        driver.show()
        packet, _ = udp_listener.recvfrom(2048)
        assert packet[0] == DRGB
        assert packet[1] == driver._realtime_timeout
        assert list(packet[2:5]) == [0, 0, 0]
        assert list(packet[5:8]) == [1, 2, 3]
        assert len(packet) == 2 + 3 * driver.leds_number
    finally:
        driver.deinit()


def test_long_strips_are_split_in_dnrgb_chunks(fake_wled, udp_listener):
    driver = WLED(8, host="127.0.0.1", http_port=fake_wled.server_port,
                  udp_port=udp_listener.getsockname()[1], realtime=True, takeover_on_start=True)
    try:
        driver.leds_number = 900                             # longer than a single udp packet can carry
        driver.pixels = [(1, 2, 3)] * driver.leds_number
        driver.show()
        first, _ = udp_listener.recvfrom(2048)
        second, _ = udp_listener.recvfrom(2048)
        assert first[0] == DNRGB and second[0] == DNRGB
        assert int.from_bytes(first[2:4], "big") == 0
        assert int.from_bytes(second[2:4], "big") == (len(first) - 4) // 3
        assert len(first) <= 1472 and len(second) <= 1472
    finally:
        driver.deinit()


# ------------------------------------------------------------- shared device handling

def test_the_device_is_left_alone_until_released(fake_wled):
    """A WLED controller is usually shared with Home Assistant: restarting the server
    must not blank the lights."""
    driver = WLED(8, host="127.0.0.1", http_port=fake_wled.server_port,
                  realtime=False, takeover_on_start=False)
    try:
        # the state of the device was read, not overwritten
        assert driver.brightness == pytest.approx(200 / 255.0)
        assert driver.pixels[0] == (10, 20, 30)
        assert fake_wled.received == []

        driver.fill((255, 0, 0))
        driver.set_brightness(0.25)
        sleep(0.3)
        assert fake_wled.received == []

        driver.release()
        driver.fill((255, 0, 0))
        assert wait_for(lambda: any("seg" in r for r in fake_wled.received))
    finally:
        driver.deinit()


def test_takeover_on_start_clears_the_strip(fake_wled):
    driver = WLED(8, host="127.0.0.1", http_port=fake_wled.server_port,
                  realtime=False, takeover_on_start=True)
    try:
        assert wait_for(lambda: any("seg" in r for r in fake_wled.received))
        assert fake_wled.received[-1]["seg"][0]["col"] == [[0, 0, 0]]
    finally:
        driver.deinit()


def test_a_device_going_away_does_not_break_the_driver(fake_wled):
    driver = make_driver(fake_wled)
    try:
        assert driver.is_connected()
        fake_wled.shutdown()
        driver.fill((1, 2, 3))       # must not raise, the led thread would die with it
        sleep(0.3)
        assert not driver.is_connected()
    finally:
        driver.deinit()
