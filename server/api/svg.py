"""Upload and conversion of SVG drawings.

The table only understands gcode, so an SVG has to be converted before it can be
drawn. Doing it on the server (rather than asking the user to run Inkscape and a
gcode plugin) also means the conversion knows the size of the table and can fit,
simplify and reorder the drawing for it.

Two routes are exposed:

* `/api/svg/preview` converts without saving anything and returns the resulting
  polylines, so the interface can show the result while the options are tweaked;
* `/api/svg/upload` converts and stores the result as one drawing per layer.
"""

import io
import json
import traceback

from flask import jsonify, request

from server import app
from server.preprocessing.drawing_creator import preprocess_drawing
from server.preprocessing.svg_parser import SvgParseError
from server.preprocessing.svg_to_gcode import ConversionOptions, convert_file
from server.sockets_interface.socketio_callbacks import drawings_refresh
from server.utils import settings_utils

# a drawing with more layers than this is almost certainly a mistake in the form
MAX_LAYERS = 100
# generating a preview of a huge file would block the server for a long time
MAX_UPLOAD_BYTES = 16 * 1024 * 1024


def get_table_size():
    """Usable area of the table, taken from the device settings.

    Polar and scara tables are described by a radius: the drawing is fitted in
    the square that fits inside the circle, which is the area that can actually
    be reached whatever the angle.
    """
    device = settings_utils.get_only_values(settings_utils.load_settings()["device"])
    if str(device.get("type", "Cartesian")).lower() == "cartesian":
        return float(device.get("width", 100)), float(device.get("height", 100))
    radius = float(device.get("radius", 100))
    return 2 * radius, 2 * radius


def read_svg_from_request():
    """Returns the content of the uploaded SVG, from a file field or from a raw string.

    The drawing pad of the interface posts the strokes as an svg document in the
    `svg` field instead of uploading a file, so both are accepted.
    """
    if "file" in request.files:
        uploaded = request.files["file"]
        if uploaded and uploaded.filename:
            content = uploaded.read(MAX_UPLOAD_BYTES + 1)
            if len(content) > MAX_UPLOAD_BYTES:
                raise ValueError("The file is too big")
            return content, uploaded.filename
    values = form_values()
    svg = values.get("svg")
    if svg:
        if len(svg) > MAX_UPLOAD_BYTES:
            raise ValueError("The drawing is too big")
        return svg, values.get("name") or "drawing.svg"
    raise ValueError("No SVG drawing was sent")


def get_options(values):
    table_width, table_height = get_table_size()
    options = ConversionOptions.from_dict(values, table_width, table_height)
    if options.layers > MAX_LAYERS:
        raise ValueError("At most {} layers can be generated".format(MAX_LAYERS))
    if options.table_width <= 0 or options.table_height <= 0:
        raise ValueError("The size of the table is not configured: check the device settings")
    return options


def form_values():
    if request.is_json:
        return request.json or {}
    return request.form


@app.route("/api/svg/preview", methods=["POST"])
def api_svg_preview():
    """Converts an SVG and returns the polylines, without saving anything.

    The answer holds one entry per layer so that the interface can show how the
    drawing builds up, plus the numbers needed to judge the options (how many
    points were dropped by the simplification, how far the ball travels, ...).
    """
    try:
        content, name = read_svg_from_request()
        options = get_options(form_values())
        result = convert_file(content, options, name=name)
    except (SvgParseError, ValueError) as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        app.logger.error("Error while converting an SVG drawing")
        app.logger.error(traceback.format_exc())
        return jsonify({"success": False, "error": "Cannot convert the drawing: {}".format(e)}), 500

    if not result.layers:
        return jsonify({"success": False, "error": "The drawing has no lines to draw"}), 400

    return jsonify({
        "success": True,
        "name": name,
        "table": {"width": options.table_width, "height": options.table_height},
        "layers": [_polylines(layer) for layer in result.layers],
        "start_point": result.start_point,
        "end_point": result.end_point,
        "path_length": result.traced_length,
        "points_before": result.points_before,
        "points_after": result.points_after,
    })


@app.route("/api/svg/upload", methods=["POST"])
def api_svg_upload():
    """Converts an SVG and stores it, one drawing per layer.

    Returns the ids of the created drawings, in the order they have to be played
    to build the picture up.
    """
    try:
        content, name = read_svg_from_request()
        options = get_options(form_values())
        result = convert_file(content, options, name=name)
    except (SvgParseError, ValueError) as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        app.logger.error("Error while converting an SVG drawing")
        app.logger.error(traceback.format_exc())
        return jsonify({"success": False, "error": "Cannot convert the drawing: {}".format(e)}), 500

    if not result.layers:
        return jsonify({"success": False, "error": "The drawing has no lines to draw"}), 400

    base = name.rsplit(".", 1)[0] or "drawing"
    ids = []
    for index, layer in enumerate(result.layers):
        if len(result.layers) > 1:
            filename = "{}_{}of{}.gcode".format(base, index + 1, len(result.layers))
        else:
            filename = base + ".gcode"
        stream = io.StringIO("\n".join(layer) + "\n")
        ids.append(preprocess_drawing(filename, stream))

    drawings_refresh()
    return jsonify({
        "success": True,
        "ids": ids,
        "layers": len(result.layers),
        "path_length": result.traced_length,
        "start_point": result.start_point,
        "end_point": result.end_point,
    })


def _polylines(gcode_lines):
    """Turns a gcode layer back into polylines, for the preview.

    A `G0` marks the connection between two strokes. The ball traces it exactly
    like everything else, so it is returned as a polyline of its own with
    `travel` set, letting the interface show where the drawing has to jump
    without pretending those lines are invisible.
    """
    polylines = []
    current = None
    position = None

    for line in gcode_lines:
        if line.startswith(";"):
            continue
        parts = line.split()
        if not parts or parts[0] not in ("G0", "G1"):
            continue
        target = list(position) if position is not None else [0.0, 0.0]
        for part in parts[1:]:
            if part[0] == "X":
                target[0] = float(part[1:])
            elif part[0] == "Y":
                target[1] = float(part[1:])

        if parts[0] == "G0" and position is not None:
            polylines.append({"travel": True, "points": [list(position), list(target)]})
            # the stroke the travel leads to starts at the destination of the travel
            current = {"travel": False, "points": [list(target)]}
            polylines.append(current)
        elif current is None:
            current = {"travel": False, "points": [list(target)]}
            polylines.append(current)
        else:
            current["points"].append(list(target))
        position = target

    return [p for p in polylines if len(p["points"]) > 1]
