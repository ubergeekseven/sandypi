#!/usr/bin/env python3
"""Converts an SVG drawing to gcode from the command line.

Same conversion the server does, without needing the server: handy to check what
a drawing will look like, or to try the options out, before uploading anything.

    $> python dev_tools/svg_to_gcode_cli.py drawing.svg --width 1100 --height 540

With `--layers` one file per layer is written next to the output, named
`<output>_1of5.gcode` and so on. Playing them in order builds the picture up.
"""

import argparse
import importlib.util
import os
import sys
import types


def load_modules():
    """Loads the two conversion modules without importing the whole server.

    `server/__init__.py` builds the flask app, opens the serial port and starts
    the led controller, none of which is wanted here, so the modules are loaded
    from their files with a stub package in between.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("server", "server.preprocessing"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)

    loaded = {}
    for name in ("svg_parser", "svg_to_gcode"):
        full = "server.preprocessing." + name
        spec = importlib.util.spec_from_file_location(
            full, os.path.join(root, "server", "preprocessing", name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[full] = module
        spec.loader.exec_module(module)
        loaded[name] = module
    return loaded["svg_to_gcode"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="the svg file to convert")
    parser.add_argument("-o", "--output", help="output file (default: the input with a .gcode suffix)")
    parser.add_argument("--width", type=float, default=1100, help="usable X travel of the table")
    parser.add_argument("--height", type=float, default=540, help="usable Y travel of the table")
    parser.add_argument("--margin", type=float, default=0, help="border to leave empty on every side")
    parser.add_argument("--simplify", type=float, default=0, metavar="TOLERANCE",
                        help="drop the points that are off by less than this, in machine units")
    parser.add_argument("--flatness", type=float, default=0.2, help="how finely curves are flattened")
    parser.add_argument("--stretch", action="store_true", help="fill the table instead of keeping the aspect ratio")
    parser.add_argument("--no-optimize", action="store_true", help="keep the strokes in document order")
    parser.add_argument("--layers", type=int, default=1, help="split the drawing into this many layers")
    parser.add_argument("--layer-mode", choices=("length", "document"), default="length",
                        help="split by traced length, or follow the layers of the svg file")
    parser.add_argument("--start", nargs=2, type=float, metavar=("X", "Y"), help="where the ball starts")
    parser.add_argument("--end", nargs=2, type=float, metavar=("X", "Y"), help="where the ball finishes")
    parser.add_argument("--feedrate", type=float, help="F value written on the first move")
    args = parser.parse_args(argv)

    gcode = load_modules()

    options = gcode.ConversionOptions(
        table_width=args.width, table_height=args.height, margin=args.margin,
        simplify_tolerance=args.simplify, flatness=args.flatness,
        keep_aspect=not args.stretch, optimize_order=not args.no_optimize,
        start_point=args.start, end_point=args.end,
        layers=args.layers, layer_mode=args.layer_mode, feedrate=args.feedrate)

    name = os.path.basename(args.input)
    try:
        result = gcode.convert_file(args.input, options, name=name)
    except Exception as e:
        parser.error("cannot convert '{}': {}".format(args.input, e))

    if not result.layers:
        parser.error("'{}' has no lines to draw".format(args.input))

    output = args.output or os.path.splitext(args.input)[0] + ".gcode"
    base, extension = os.path.splitext(output)

    for index, layer in enumerate(result.layers):
        if len(result.layers) > 1:
            path = "{}_{}of{}{}".format(base, index + 1, len(result.layers), extension)
        else:
            path = output
        with open(path, "w") as f:
            f.write("\n".join(layer) + "\n")
        print("{}  ({} lines)".format(path, len(layer)))

    print()
    print("table          {:g} x {:g}".format(options.table_width, options.table_height))
    print("points         {} -> {}".format(result.points_before, result.points_after))
    print("traced length  {:.1f}".format(result.traced_length))
    print("start          {:.1f}, {:.1f}".format(*result.start_point))
    print("end            {:.1f}, {:.1f}".format(*result.end_point))
    return 0


if __name__ == "__main__":
    sys.exit(main())
