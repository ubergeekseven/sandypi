# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

A private fork of [texx00/sandypi](https://github.com/texx00/sandypi), a Flask + React server that
drives a sand table: it feeds gcode to a board over serial, manages drawings and playlists, and
controls the table lights.

The fork drives one specific table, and the defaults and docs assume it:

* cartesian, **1100 x 540** usable travel, **Grbl** firmware over USB serial
* lights on a **WLED** controller (ESP), shared with Home Assistant, reached over the network

Upstream is kept as a remote (`upstream`) to pull fixes from. Changes here are not meant to go back.

## Commands

```bash
# tests (from the repository root; FLASK_APP comes from .flaskenv)
python -m pytest server/tests -q
python -m pytest server/tests/test_svg_to_gcode.py -q            # one file
python -m pytest server/tests/test_wled.py::test_fill_sends_a_solid_color -q   # one test

# run the server for development
flask run --host=0.0.0.0                 # port 5000

# frontend dev server, with hot reload, talks to the flask server above
cd frontend && yarn start                # port 3000

# build and run a container from THIS source (see "Docker" below)
./docker/build_local.sh                  # then http://localhost:5100

# convert an svg without running anything else
python dev_tools/svg_to_gcode_cli.py drawing.svg --width 1100 --height 540 --simplify 1 --layers 4
```

There is no linter wired up: the flake8 step in `.github/workflows/python-app.yml` is commented out.

## Things that will bite you

**The frontend copy of the settings is generated.** `server/saves/default_settings.json` is the
schema *and* the defaults. `frontend/src/structure/tabs/settings/defaultSettings.js` is a generated
mirror of it. After editing the json you must run:

```bash
python dev_tools/build_default_settings.py
```

`server/tests/test_install.py::test_js_defaults` fails if you forget.

**Two build files are generated and gitignored.** `git_shash.json` (read at import time by
`software_updates.py`, so the server will not even start without it) and `frontend/.env`. Both come
from `python dev_tools/update_frontend_version_hash.py`. CI and `docker/build_local.sh` run it; a
fresh clone does not have them.

**The tests run against the real database file.** `server/tests/conftest.py` sets an in-memory
`SQLALCHEMY_DATABASE_URI` *after* SQLAlchemy is already bound, so it has no effect. Any test that
creates drawings must delete its own rows, or later tests see them — `test_buttons.py` starts a real
drawing as soon as the library is not empty, and the suite hangs. See the `created_drawings` fixture
in `test_api_svg.py`.

**Importing anything under `server.` boots the whole application.** `server/__init__.py` builds the
Flask app, opens the serial port and starts the feeder, queue, leds and buttons managers as
`app.feeder`, `app.qmanager`, `app.lmanager`, `app.bmanager` at import time. Code that only wants a
couple of modules has to load them by file path with a stub package in between — see
`load_modules()` in `dev_tools/svg_to_gcode_cli.py`.

## Architecture

### Settings

One json file describes every option: type, label, tip, `available_values`, and `depends_on` /
`depends_values` for conditional display. On startup `settings_utils.update_settings_file_version()`
merges `default_settings.json` into the user's `server/saves/saved_settings.json` with `match_dict`,
adding new keys while keeping existing values. Fields listed in `OVERWRITE_FIELDS` (labels, tips,
`available_values`) are always taken from the defaults, so existing installs pick up new choices.

`get_only_values()` flattens the `{"value": ...}` wrappers; most consumers call it and wrap the
result in a `DotMap`.

### Talking to the board

`feeder.py` owns the send loop and line buffering, `device_serial.py` the port. **When the port
cannot be opened it falls back to `emulator.py`**, which answers with the right ready message for the
configured firmware, acknowledges with realistic timing and tracks position — so the queue,
playlists and ETA all work with no hardware attached. Firmware differences (ack strings, buffer
query, emergency stop) live in `firmware_defaults.py`.

### Playlists

`queue_manager.py` runs elements; an element is a subclass of `GenericPlaylistElement`
(`server/database/generic_playlist_element.py`) that yields gcode lines from `execute()`. Adding one
means: subclass it, call `add_column_field()` for anything that needs to be queryable, register the
class in `_get_elements_types()` at the bottom of `playlist_elements.py`, and add the matching
frontend entry in `frontend/src/structure/tabs/playlists/elementsFactory.js`.

`LightsControl`, `PositioningElement` and `ClearElement` are registered but still empty stubs.

### Lights

`LedsController` picks a driver class from the `leds.type` setting and holds it as `self.driver`;
`is_available()` is simply "is there a driver". Drivers subclass `GenericLedDriver`.

Note that the base `__init__` calls `self.init_pixels()` at the end, so **a subclass must set its own
attributes before calling `super().__init__()`**. A driver that cannot reach its hardware raises from
`init_pixels()`, and the controller leaves `self.driver` as `None`.

`wled.py` is the odd one out: it ignores the GPIO pin and talks over the network, using the JSON API
for state that should stick and the realtime UDP protocol for per-pixel frames. It also starts
"holding" — swallowing writes and only reading the device state — until `release()` is called on the
first explicit request from the UI, so restarting the server does not blank a strip that Home
Assistant is also driving.

### Drawings and the SVG pipeline

`preprocessing/drawing_creator.py` stores an upload as `server/static/Drawings/<id>/<id>.gcode` plus
a preview rendered by `utils/gcode_converter.py` (which also handles the polar and scara coordinate
conversions).

For SVG input, `preprocessing/svg_parser.py` reads a document into polylines (standard library only —
a Pi Zero is a supported target) and `preprocessing/svg_to_gcode.py` fits, simplifies, orders and
layers them. `api/svg.py` exposes `/api/svg/preview` (converts without saving) and
`/api/svg/upload` (stores one drawing per layer).

The design rests on one invariant worth internalising: **a sand table has no pen to lift.** The ball
ploughs a furrow wherever it goes, so a "travel" move between two strokes is as visible as the
strokes. That is why subpaths get reordered and reversed to shorten connections, why drawings can be
given explicit start and end points to chain cleanly, and why layers are cut so that each one ends
exactly where the next begins.

## Docker

`docker/docker-compose.yml` pulls the published `texx00/sandypi` image and runs Watchtower to keep it
updated. On this fork that is wrong twice over: the container would not contain local changes, and
Watchtower would replace anything built by hand. Use `docker/docker-compose.local.yml` (via
`docker/build_local.sh`), which builds from source and leaves updates alone.

The local compose file also drops the `privileged: true`, the whole-`/dev` mount and the shutdown
sockets that upstream uses; a single `devices:` entry is enough once a real board is attached.

From inside a container, WLED must be addressed **by ip** — mDNS `.local` names do not resolve there.

## Conventions

Comments in this codebase explain *why*, not *what*, and sit above the code rather than at the end of
the line. Match the surrounding density. Upstream leaves `TODO` comments in place as a backlog;
`todos.md` collects the larger ones.

## Further reading

* `docs/testing_without_hardware.md` — what works with no board, and how to run it
* `docs/svg_drawings.md` — the conversion options and the two API routes
* `docs/hardware/leds.md` — LED wiring, and the WLED section
* `docs/migrating_an_existing_table.md` — moving a built table onto this version
* `docs/development.md` — upstream's dev setup notes (VS Code debugging, flask-migrate)
