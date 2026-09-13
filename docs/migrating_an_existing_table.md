# Migrating an existing table

This page is for the case where a table is already built and running, on an older Sandypi or on
something else entirely, and has to be moved to the current version. The goal is to get back to a
working table with the settings you already know, and to leave out the parts of the software you
do not need.

## 1. What actually has to be carried over

Very little. A table is described by a handful of numbers, and all of them live in
`server/saves/saved_settings.json` on the old install:

| What | Where it goes now |
| --- | --- |
| Usable X and Y travel | `device.width`, `device.height` |
| Board firmware | `device.firmware` |
| Serial port and baudrate | `serial.port`, `serial.baud` |
| Anything sent at connection / before / after a drawing | `scripts.connected`, `scripts.before`, `scripts.after` |
| The drawings themselves | `.gcode` files, re-uploaded |

If the old SD card still reads, copy `server/saves/saved_settings.json` and
`server/static/Drawings/` off it before anything else. The drawings folder holds one directory per
drawing, each with the `.gcode` file inside — those files can be dropped straight onto the
dropzone of the new install.

If the card is gone, the numbers above are all you need to type in by hand.

## 2. Settings for a cartesian GRBL table

Open the settings page and fill in:

**Serial port settings**
* `Serial port` — the port of the board (`/dev/ttyUSB0` or `/dev/ttyACM0` on a Pi). Use
  `Save and connect` to try it straight away.
* `Serial baudrate` — `115200` for a stock GRBL build.

**Device type**
* `Select firmware type` — `Grbl`
* `Select device type` — `Cartesian`
* `Device width` — the usable X travel, in the units the board works in
* `Device height` — the usable Y travel

The width and height are what the SVG conversion fits drawings into, so they should be the area the
ball can actually reach, not the outside size of the table. Measure it once by jogging to each
limit from the manual page.

> A worked example: a rectangular table with 1100 x 540 of usable travel gets
> `device.width = 1100` and `device.height = 540`. Drawings made for a square table will be
> centred in a 540 x 540 area unless `keep_aspect` is turned off.

**Scripts**

This is where a GRBL table usually needs a couple of lines. They are sent verbatim, one command per
line:

* `On connection` — anything the board needs once per session. `$X` to clear the alarm state after
  a reset, `G21` for millimetres, `G90` for absolute positioning, and `$H` if the machine has
  limit switches and should home.
* `Before drawing` — where the ball should be before a drawing starts.
* `After drawing` — where it should be parked.

The exact contents depend on the machine: start from what the old install had in the same fields.

## 3. Checking the machine before trusting it with a drawing

From the manual control page, send single commands and watch the ball:

1. `?` — GRBL answers with its state and position. If it says `Alarm`, send `$X`.
2. `G91 G0 X10` then `G91 G0 X-10` — a small relative move and back. Confirms direction and units.
3. `G90 G0 X0 Y0` and `G90 G0 X<width> Y<height>` — the two opposite corners. If the ball stops
   short or the board alarms, the travel numbers in the settings are wrong.

Do this before queueing a real drawing: the software clamps the gcode it generates to the
configured size, so wrong numbers there mean wrongly clipped drawings.

## 4. What you can leave out

The software carries several optional pieces. None of them is required to draw:

| Part | Needed? |
| --- | --- |
| Docker | No. The [manual installation](old_installation.md) works and is easier to modify while developing |
| Automatic updates | No. Off by default; on a fork it should stay off, since it pulls from upstream |
| GPIO LED drivers | No, if the lights are driven by [WLED](hardware/leds.md#wled) or nothing at all |
| Light sensor | No |
| Hardware buttons | No. The section is hidden when no GPIO is available |
| `server/autodetect` folder | No. Only a convenience for dropping files in by hand |

Turning something off is a matter of not configuring it: an unconfigured led type or an empty
buttons list is simply skipped at startup, and the corresponding sections disappear from the
interface.

## 5. Lights

If the LEDs are on a WLED controller there is nothing to wire to the Pi and nothing to rebuild when
the Pi is replaced. Set the led type to `WLED` and give it the address of the ESP: see
[the LEDs page](hardware/leds.md#wled). Home Assistant keeps working at the same time — both talk
to the same WLED state.

## 6. Bringing the drawings back

Drop the old `.gcode` files onto the dropzone of the drawings page. They are re-analysed on upload
(preview image and path length), so nothing else has to be copied.

Gcode written for a differently sized table is **not** rescaled on upload: it is stored as it is.
If the old table had different dimensions, re-generate the drawings from their source instead, or
convert the original `.svg` files, which does fit them to the table — see
[SVG drawings](svg_drawings.md).

## 7. When the hardware itself is being replaced

A few things worth doing while the table is open anyway:

* Give the new controller a fixed address (DHCP reservation), so the WLED address in the settings
  never goes stale.
* Power the board and the Pi from supplies that can actually hold up under load. A browning-out Pi
  corrupts SD cards, which is the usual reason an old install "just died".
* Put the database and the drawings on something other than the SD card if possible: `DB_PATH` can
  point the database elsewhere.
* Note the working numbers somewhere outside the machine. The settings file is the one thing that
  is painful to reconstruct, and it is a small json file.
