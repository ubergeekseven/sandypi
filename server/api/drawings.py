import io

from flask import request, jsonify
from server import app
from server.preprocessing.drawing_creator import preprocess_drawing
from server.preprocessing.svg_parser import SvgParseError
from server.preprocessing.svg_to_gcode import convert_file
from server.sockets_interface.socketio_callbacks import drawings_refresh

GCODE_EXTENSIONS = ["gcode", "nc", "thr"]
ALLOWED_EXTENSIONS = GCODE_EXTENSIONS + ["svg"]

def extension(filename):
    return filename.rsplit('.', 1)[1].lower() if '.' in filename else ''

def allowed_file(filename):
    return extension(filename) in ALLOWED_EXTENSIONS

# Upload route for the dropzone to load new drawings
@app.route('/api/upload/', methods=['GET','POST'])
def api_upload():
    if request.method == "POST":
        if 'file' in request.files:
            file = request.files['file']
            if file and file.filename!= '' and allowed_file(file.filename):
                # an svg is not gcode: it is converted with the default options, fitted to the
                # table. Use /api/svg/upload to choose the options (simplification, layers, ...)
                if extension(file.filename) == "svg":
                    try:
                        file = convert_svg_with_defaults(file)
                    except (SvgParseError, ValueError) as e:
                        app.logger.error("Cannot convert '{}': {}".format(file.filename, e))
                        return jsonify(-1)

                # create entry in the database and preview image
                id = preprocess_drawing(file.filename, file)

                # refreshing list of drawings for all the clients
                drawings_refresh()
                return jsonify(id)
    return jsonify(-1)


def convert_svg_with_defaults(file):
    """Converts an uploaded svg to a gcode stream, fitted to the configured table"""
    from server.api.svg import get_options      # imported here to avoid a circular import

    options = get_options({})
    result = convert_file(file.read(), options, name=file.filename)
    if not result.layers:
        raise ValueError("the drawing has no lines to draw")
    stream = io.StringIO("\n".join(result.gcode) + "\n")
    stream.filename = file.filename.rsplit(".", 1)[0] + ".gcode"
    return stream
