# Testing without the table

Everything except actually pushing the ball around can be tried on any machine: a
laptop, a spare Pi, a container. The software falls back to a built-in board
emulator when no controller is connected, so drawings, playlists, the queue and the
time estimates all behave as they would on the real thing.

This is the quickest way to work on a table that is not built yet, or not rebuilt yet.

## What works, and what does not

| | Without a board |
| --- | --- |
| Uploading drawings, previews, path length | works |
| SVG to gcode: fitting, simplification, ordering, layers | works |
| Playlists, queue, shuffle, timing elements, ETA | works |
| Manual gcode console | works, answered by the emulator |
| WLED lights | works, the controller is on the network |
| GPIO LED strips, hardware buttons, light sensor | needs the real Pi hardware |
| The ball moving | needs the real board |

## The quickest check of all: the converter on its own

No server, no container, nothing to install beyond python:

```bash
$> python dev_tools/svg_to_gcode_cli.py drawing.svg --width 1100 --height 540 --simplify 1
drawing.gcode  (271 lines)

table          1100 x 540
points         2000 -> 270
traced length  6040.1
start          540.3, 260.2
end            814.7, 266.2
```

Add `--layers 4` to write one file per layer. `--help` lists the rest: margin,
stretching, start and end points, flatness, feedrate.

The resulting `.gcode` opens in any gcode previewer, and is the same output the
server produces.

## Running the server in Docker

The `docker-compose.yml` in the docker folder pulls the published `texx00/sandypi`
image and lets Watchtower keep it updated. That is right for following upstream, but
it means **your own changes are not in the container**, and Watchtower would replace
anything you built with the upstream image.

To run this repository instead, from the main sandypi folder:

```bash
$> ./docker/build_local.sh
```

That writes the two generated version files, builds the image, and starts it with
volumes for the persistent data. The interface is then on `http://<host>:5100`.

On Windows, run it from Git Bash or WSL rather than from `cmd` or PowerShell.

Other useful commands:

```bash
$> docker compose -f docker/docker-compose.local.yml logs -f      # follow the logs
$> docker compose -f docker/docker-compose.local.yml up -d --build # rebuild after a change
$> docker compose -f docker/docker-compose.local.yml down          # stop
$> docker compose -f docker/docker-compose.local.yml down -v       # stop and wipe the data
```

The first build takes a while: it builds the React frontend with yarn and installs
the python dependencies. On a Pi it takes considerably longer than on a laptop, so
if you have the choice, build on the laptop.

## Pointing it at a fake board

In the settings page, under `Serial port settings`, pick `FAKE` and save.

`FAKE` is also what you get when the configured port cannot be opened: the log says
`Serial not available ... (Will use the fake serial)` and the emulator takes over. It
answers with the right ready message for the configured firmware (`Grbl` or
`Marlin`), acknowledges commands with realistic timing and tracks the position, so
the queue advances and the ETA counts down as if a table were drawing.

Set `Select device type`, `Device width` and `Device height` to the real table's
values even while testing: they are what drawings are fitted to, so a preview made
against the wrong size is not worth much.

## Pointing it at the real WLED controller

The lights need no emulation, they just need to be reachable. Set the led type to
`WLED` and give it the address of the ESP.

One thing to watch for in a container: **use the ip address, not the `.local` name**.
mDNS names do not resolve inside the container, since it resolves through Docker
rather than through avahi on the host. A DHCP reservation on the router is the easy
fix. Outbound HTTP and the realtime UDP both work from the default bridge network,
nothing has to be opened.

Check the controller is reachable from inside the container with:

```bash
$> docker exec sandypi python -c "import urllib.request,json; \
   print(json.load(urllib.request.urlopen('http://192.168.1.50/json/info'))['name'])"
```

Remember that `Take over WLED on startup` is off by default, so the strip keeps
whatever Home Assistant left on it until a colour is picked in the LEDs page. That
is usually what you want, but it does mean nothing visibly happens at startup.

## Attaching the real board later

Find a name that survives a reboot:

```bash
$> ls /dev/serial/by-id/
usb-1a86_USB_Serial-if00-port0
```

Then uncomment the `devices:` block in `docker/docker-compose.local.yml`, put that
path in it, and restart. The port then shows up as `/dev/ttyUSB0` in the settings.

The upstream compose file mounts all of `/dev` and runs the container privileged
instead. That works, but a single device entry is enough and gives the container a
lot less of the host.

## Trying the conversion API by hand

```bash
# convert without saving anything, and see what the options do
$> curl -s -F file=@drawing.svg -F simplify_tolerance=1 -F layers=3 \
     http://localhost:5100/api/svg/preview | python -m json.tool | head -30

# convert and store, one drawing per layer
$> curl -s -F file=@drawing.svg -F layers=3 http://localhost:5100/api/svg/upload
{"success": true, "ids": [12, 13, 14], "layers": 3, "path_length": 8400.0}
```

The full list of options is in [SVG drawings](svg_drawings.md).

## Running the tests

Not Docker, but worth knowing while developing:

```bash
$> python -m pytest server/tests -q
```

One catch: `server/tests/conftest.py` sets an in-memory database *after* SQLAlchemy
is already bound, so the tests actually run against the real `database.db` file. A
test that creates drawings has to clean up its own rows, or later tests will see them
(the buttons test, for one, starts a drawing as soon as the library is not empty).
