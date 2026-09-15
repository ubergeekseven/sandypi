# LEDs

## Compatible LEDs

At the moment, it is possible to use WB2812B (or SK6812) compatible led strips both in RGB and RGBW format.

It is also possible to use dimmable single color leds (PWM driven).

Strips driven by a [WLED](https://kno.wled.ge/) controller (ESP8266/ESP32) are supported as well.
In that case the LEDs are not wired to this device at all: see [the dedicated section](#wled) below.

## Installation

The software can handle different type of leds.
If digital leds are used, it is necessary to disable the audio module since it is using the same gpio channels used for controlling the LEDs. The following steps may be necessary:
- create the file `/etc/modprobe.d/snd-blacklist.conf` fill it with the following line: `blacklist snd_bcm2835`
- if the snd_bcm2835 is present also inside the `/etc/modules` file comment it out with the `#` character
- when running a headless setup check the `/boot/config.txt` file and add the following lines and then reboot the system:
```
hdmi_force_hotplug=1
hdmi_force_edid_audio=1
```
- if it is still not working correctly try to comment out `dtparam=audio=on` in the `/boot/config.txt` file and reboot

For more details check the [page of the library](https://pypi.org/project/rpi-ws281x/) used.

When the dimmable type is used it is necessary to follow also the [procedure to enable the hardware buttons](#buttons)

## Wiring

Connect the digital channel of the led strip to pin 18 or 12 ([BCM setup](https://www.google.com/search?q=raspberry+pi+bcm+pinout&oq=raspberry+pi+&aqs=chrome.1.69i57j69i59l3j35i39j69i65l3.103597j1j7&sourceid=chrome&ie=UTF-8)).

It is necessary to connect the leds ground wire to the pi ground wire to have a reliable connection. Be careful when you are using different power supplies for pi and LEDs: ground level might be different.

Usually, LED strips requires 5V to be controlled. The strip might work also with the direct connection to the 3.3V output of the raspberry pi but an adequate high frequency voltage shifter circuit can avoid troubles.

Dimmable LEDs can be connected to the same pins (only one color dimming is available at the moment)

## Light sensors

At the moment one light sensor type is supported (TSL2519).
Connect the sensor to the following pins (have a look [online](https://www.google.com/search?q=raspberry+pi+bcm+pinout&oq=raspberry+pi+&aqs=chrome.1.69i57j69i59l3j35i39j69i65l3.103597j1j7&sourceid=chrome&ie=UTF-8) where to find those pins):
 - 3.3V
 - GND
 - SDA
 - SCL

This sensor communicates with tje I2C connection with the Raspberry. In order to use this communication protocol it is necessary to enable the hardware I2C interface through the OS settings. This can be done with the following steps:
 - run the following command: `sudo raspi-config`
 - use the arrows to move down to the section `3 Interface Options` and press enter
 - use the arrows to move down to the section `P5 I2C` and press enter
 - select the `<Yes>` option using the right-left arrows and press enter
 - press `Esc` until you exit the menu
 - restart the software

Now the sensor should be working.
___

## WLED

If the LEDs of the table are already driven by a [WLED](https://kno.wled.ge/) controller there is
nothing to wire to the Raspberry Pi: Sandypi talks to the ESP over the network.
This is the recommended setup when the strip is also used by other systems (Home Assistant, the
WLED app, Alexa, ...) because all of them keep working at the same time.

### Setup

1. Flash and configure WLED on the ESP as usual, and check that the strip works from the WLED web page.
2. Give the controller a fixed address: either a DHCP reservation on the router, or the mDNS name
   configured in WLED (`wled-table.local`).
3. In Sandypi open the settings page, `Additional hardware` -> `LEDs` and:
   * set `Select a led type` to `WLED`
   * write the address of the controller in `WLED address` (`192.168.1.50` or `wled-table.local`,
     a full `http://host:port` url is accepted too)
4. Save the settings. The LEDs page can now drive the strip.

The number of LEDs and whether they have a white channel are read from the device itself, so the
`Leds number on the ...` fields are ignored for WLED.

### Options

| Option | Default | Meaning |
| --- | --- | --- |
| `WLED address` | empty | Hostname or ip of the controller |
| `Use the WLED realtime protocol` | on | Per-pixel animations are sent over UDP instead of HTTP |
| `Take over WLED on startup` | off | Whether Sandypi may change the lights when the server starts |

`Take over WLED on startup` is off by default on purpose. A WLED controller is normally shared with
other systems, so on startup Sandypi only *reads* the current state and leaves the lights as they
are. The strip is taken over the first time a colour is chosen from the LEDs page, and Home
Assistant keeps seeing every change because they all go through the same WLED state.

### How it talks to the device

Two transports are used, each for what it is good at:

* the **JSON API** (`POST /json/state`) for anything that should stick: power, brightness, a solid
  colour, an effect or a preset. WLED saves this state, restores it after a reboot and reports it to
  Home Assistant. Requests are coalesced, so dragging the colour picker does not flood the ESP.
* the **realtime UDP protocol** (DRGB/DRGBW, or DNRGB in chunks for strips longer than ~480 LEDs) for
  per-pixel animations. Frames are transient: WLED goes back to its own effect a couple of seconds
  after the last frame, which is exactly what is needed for an animation that follows the ball.

### Troubleshooting

* *"Cannot reach the WLED device at ..."* — the address is wrong or the ESP is unreachable from the
  machine running Sandypi. Check it by opening `http://<address>/json/info` in a browser: it must
  answer with a json document. When running in Docker, mDNS names (`*.local`) often do not resolve
  inside the container: use the ip address instead.
* *The lights do not react* — check that the LEDs page is showing the RGB picker (it follows the led
  type) and that the WLED device is not in a state Sandypi cannot override, like an active playlist
  on the ESP itself.
