"""Turns the polylines read from an SVG into gcode for a sand table.

A sand table has no pen to lift: the ball ploughs a furrow wherever it goes, so
a "travel" move between two subpaths is just as visible as the drawing itself.
Everything in this module follows from that:

* subpaths are reordered (and reversed when it helps) to make the connecting
  moves as short as possible,
* the drawing can be given an explicit start and end point, so that several
  drawings can be chained without the ball jumping across the table between them,
* a drawing can be split into layers that are played one after the other, each
  one starting where the previous stopped, so that the picture builds up over
  time instead of appearing all at once.
"""

from math import ceil, hypot

from server.preprocessing.svg_parser import DEFAULT_FLATNESS, SvgDrawing, parse_svg

# lengths below this are treated as zero, to keep the rounding of the geometry
# from producing degenerate subpaths
EPSILON = 1e-9

# polylines longer than this are simplified in windows, see `simplify`
RDP_WINDOW = 1000


# ------------------------------------------------------------------------ simplifying

def simplify(points, tolerance):
    """Ramer-Douglas-Peucker, with the cost bounded for very long paths.

    Plain RDP degrades to O(n^2) when there is nothing to simplify, which is
    exactly what a noisy hand drawn stroke looks like. Splitting a long polyline
    into windows keeps the cost linear; the windows overlap by one point so the
    joins are seamless, and the only thing lost is the chance to drop a point
    sitting near a window boundary.
    """
    if tolerance <= 0 or len(points) < 3:
        return list(points)

    if len(points) > RDP_WINDOW:
        result = []
        for start in range(0, len(points) - 1, RDP_WINDOW - 1):
            window = points[start:start + RDP_WINDOW]
            if len(window) < 2:
                break
            simplified = _rdp(window, tolerance)
            result.extend(simplified if not result else simplified[1:])
        return result

    return _rdp(points, tolerance)


def _rdp(points, tolerance):
    """Ramer-Douglas-Peucker, iterative so that long paths cannot blow the stack"""
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]

    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        index, distance = _farthest(points, first, last)
        if distance > tolerance:
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))

    return [point for point, keeper in zip(points, keep) if keeper]


def _farthest(points, first, last):
    """Index and distance of the point of the range farthest from the chord"""
    ax, ay = points[first]
    bx, by = points[last]
    dx, dy = bx - ax, by - ay
    length = hypot(dx, dy)

    best_index, best_distance = first, -1.0
    for i in range(first + 1, last):
        px, py = points[i]
        if length == 0:
            distance = hypot(px - ax, py - ay)
        else:
            distance = abs(dy * px - dx * py + bx * ay - by * ax) / length
        if distance > best_distance:
            best_index, best_distance = i, distance
    return best_index, best_distance


def simplify_subpaths(subpaths, tolerance):
    result = []
    for subpath in subpaths:
        simplified = simplify(subpath, tolerance)
        # a subpath that traces nothing would only add a useless move to the gcode
        if len(simplified) > 1 and path_length(simplified) > 0:
            result.append(simplified)
    return result


# --------------------------------------------------------------------------- measuring

def path_length(points):
    return sum(hypot(points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1])
               for i in range(len(points) - 1))


def total_length(subpaths, include_travel=True, start=None):
    """Length actually traced by the ball, travel moves included"""
    total = 0.0
    position = start
    for subpath in subpaths:
        if include_travel and position is not None:
            total += hypot(subpath[0][0] - position[0], subpath[0][1] - position[1])
        total += path_length(subpath)
        position = subpath[-1]
    return total


def bounds(subpaths):
    points = [point for subpath in subpaths for point in subpath]
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


# ----------------------------------------------------------------------------- fitting

def fit(subpaths, table_width, table_height, margin=0.0, keep_aspect=True, center=True):
    """Scales and translates the drawing so that it fills the table.

    Args:
        table_width, table_height: usable area of the table, in machine units.
        margin: border to leave empty on every side, in machine units.
        keep_aspect: False stretches the drawing to fill the table exactly.
        center: centers the drawing in the usable area (only matters when the
            aspect ratio is kept).
    """
    box = bounds(subpaths)
    if box is None:
        return []
    min_x, min_y, max_x, max_y = box

    available_width = max(0.0, table_width - 2 * margin)
    available_height = max(0.0, table_height - 2 * margin)
    drawing_width = max_x - min_x
    drawing_height = max_y - min_y

    scale_x = available_width / drawing_width if drawing_width > 0 else 1.0
    scale_y = available_height / drawing_height if drawing_height > 0 else 1.0
    if keep_aspect:
        scale_x = scale_y = min(scale_x, scale_y)

    offset_x = margin
    offset_y = margin
    if center:
        offset_x += (available_width - drawing_width * scale_x) / 2.0
        offset_y += (available_height - drawing_height * scale_y) / 2.0

    return [[((x - min_x) * scale_x + offset_x, (y - min_y) * scale_y + offset_y)
             for x, y in subpath] for subpath in subpaths]


def clamp(subpaths, table_width, table_height):
    """Keeps every point inside the machine limits, as a last safety net"""
    return [[(min(table_width, max(0.0, x)), min(table_height, max(0.0, y)))
             for x, y in subpath] for subpath in subpaths]


# ---------------------------------------------------------------------------- ordering

def order_subpaths(subpaths, start=None, allow_reverse=True):
    """Greedy nearest neighbour ordering, to keep the connecting scars short.

    Returns the reordered subpaths. A subpath may be reversed when starting from
    its other end is closer; this is free for the drawing and often halves the
    travel.
    """
    remaining = [list(subpath) for subpath in subpaths]
    if not remaining:
        return []

    ordered = []
    position = start
    if position is None:
        # no constraint: start from the subpath closest to the origin, so the
        # result is deterministic instead of depending on the document order
        position = min((subpath[0] for subpath in remaining), key=lambda p: hypot(p[0], p[1]))

    while remaining:
        best_index, best_distance, best_reversed = 0, None, False
        for index, subpath in enumerate(remaining):
            head = hypot(subpath[0][0] - position[0], subpath[0][1] - position[1])
            if best_distance is None or head < best_distance:
                best_index, best_distance, best_reversed = index, head, False
            if allow_reverse:
                tail = hypot(subpath[-1][0] - position[0], subpath[-1][1] - position[1])
                if tail < best_distance:
                    best_index, best_distance, best_reversed = index, tail, True
        chosen = remaining.pop(best_index)
        if best_reversed:
            chosen.reverse()
        ordered.append(chosen)
        position = chosen[-1]

    return ordered


def close_to(subpaths, end):
    """Appends a move to `end`, so that the next drawing knows where the ball is"""
    if not subpaths or end is None:
        return subpaths
    result = [list(subpath) for subpath in subpaths]
    if result[-1][-1] != tuple(end):
        result[-1].append((float(end[0]), float(end[1])))
    return result


# ---------------------------------------------------------------------------- layering

def cut_at(points, distance):
    """Splits a polyline at `distance` from its start.

    Returns `(head, tail)`. The cut point belongs to both halves, which is what
    makes a layered drawing continuous: the ball finishes the layer exactly where
    the next one picks it up.
    """
    if distance <= 0:
        return [points[0]], list(points)

    travelled = 0.0
    for index in range(len(points) - 1):
        (x0, y0), (x1, y1) = points[index], points[index + 1]
        segment = hypot(x1 - x0, y1 - y0)
        if segment == 0:
            continue
        if travelled + segment >= distance:
            ratio = (distance - travelled) / segment
            # when the cut lands on a vertex the point must not be duplicated,
            # or the halves would carry a zero length segment
            if ratio >= 1.0 - EPSILON:
                return points[:index + 2], points[index + 1:]
            if ratio <= EPSILON:
                return points[:index + 1], points[index:]
            cut = (x0 + (x1 - x0) * ratio, y0 + (y1 - y0) * ratio)
            return points[:index + 1] + [cut], [cut] + points[index + 1:]
        travelled += segment
    return list(points), [points[-1]]


def split_in_layers(subpaths, layers_number):
    """Splits an ordered drawing into layers of equal traced length.

    Every layer is a drawing of its own, and playing them in order builds the
    picture up progressively. Two properties matter here:

    * **continuity**: a layer ends exactly where the next one starts, so the ball
      never jumps across the table between two layers. A single long stroke (a
      spiral, say) is cut in place and the cut point is shared by both halves.
    * **no repetition**: the sand keeps what was drawn before, so a layer only
      carries its own new strokes.
    """
    layers_number = max(1, int(layers_number))
    subpaths = [list(subpath) for subpath in subpaths if len(subpath) > 1]
    if layers_number == 1 or not subpaths:
        return [subpaths] if subpaths else []

    target = sum(path_length(subpath) for subpath in subpaths) / layers_number
    if target <= 0:
        return [subpaths]

    layers = []
    current = []
    accumulated = 0.0

    for subpath in subpaths:
        remaining = subpath
        while True:
            if len(layers) == layers_number - 1:     # the last layer takes whatever is left
                current.append(remaining)
                break
            length = path_length(remaining)
            if accumulated + length < target:
                current.append(remaining)
                accumulated += length
                break
            head, tail = cut_at(remaining, target - accumulated)
            if len(head) > 1:
                current.append(head)
            if current:
                layers.append(current)
                current = []
                accumulated = 0.0
            if len(tail) > 1 and path_length(tail) > EPSILON:
                remaining = tail
                continue
            break

    if current:
        layers.append(current)
    return layers


# ------------------------------------------------------------------------------ gcode

def to_gcode(subpaths, feedrate=None, decimals=3, header=None, relative_travel_marker=True):
    """Renders the subpaths as gcode lines.

    A sand table draws while travelling, so the moves between two subpaths are
    emitted as `G0` only to mark them as connections: they are traced exactly
    like the `G1` moves and Sandypi previews them as such.
    """
    lines = []
    for comment in (header or []):
        lines.append("; " + comment)

    fmt = "{:." + str(int(decimals)) + "f}"

    def coordinates(point):
        return "X" + fmt.format(point[0]) + " Y" + fmt.format(point[1])

    first = True
    for subpath in subpaths:
        if len(subpath) < 2:
            continue
        command = "G0" if (relative_travel_marker and not first) else "G1"
        lines.append("{} {}".format(command, coordinates(subpath[0])))
        for point in subpath[1:]:
            line = "G1 " + coordinates(point)
            if first and feedrate:
                line += " F" + fmt.format(feedrate)
                feedrate = None     # the feedrate is modal, no need to repeat it
            lines.append(line)
            first = False
        first = False
    return lines


# -------------------------------------------------------------------------- the recipe

class ConversionOptions:
    """Everything the conversion can be told about a drawing.

    Attributes:
        table_width, table_height: usable area of the table, in machine units.
        margin: border left empty on every side.
        simplify_tolerance: points closer than this to the line they sit on are
            dropped. In machine units, applied after the drawing is fitted, so
            the value means the same thing whatever the size of the source file.
        flatness: how finely curves are flattened, in source units.
        keep_aspect: False stretches the drawing to fill the table.
        optimize_order: reorder the subpaths to shorten the connecting moves.
        allow_reverse: let the optimizer draw a stroke backwards when closer.
        start_point, end_point: where the ball should start and finish, in
            machine units. `None` leaves the choice to the optimizer.
        layers: number of layers to split the drawing into.
        layer_mode: "length" splits into layers of equal traced length,
            "document" uses the layers of the SVG file itself.
        feedrate: F value written on the first move. `None` writes none.
    """

    def __init__(self, table_width, table_height, margin=0.0, simplify_tolerance=0.0,
                 flatness=DEFAULT_FLATNESS, keep_aspect=True, optimize_order=True,
                 allow_reverse=True, start_point=None, end_point=None, layers=1,
                 layer_mode="length", feedrate=None):
        self.table_width = float(table_width)
        self.table_height = float(table_height)
        self.margin = float(margin)
        self.simplify_tolerance = float(simplify_tolerance)
        self.flatness = float(flatness)
        self.keep_aspect = bool(keep_aspect)
        self.optimize_order = bool(optimize_order)
        self.allow_reverse = bool(allow_reverse)
        self.start_point = tuple(start_point) if start_point else None
        self.end_point = tuple(end_point) if end_point else None
        self.layers = max(1, int(layers))
        self.layer_mode = layer_mode
        self.feedrate = feedrate

    @classmethod
    def from_dict(cls, values, table_width, table_height):
        """Builds the options from the (all optional, all strings) form fields of the UI"""
        def number(key, default=None):
            value = values.get(key, None)
            if value in (None, "", "null"):
                return default
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        def flag(key, default):
            value = values.get(key, None)
            if value is None:
                return default
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on")

        def point(prefix):
            x, y = number(prefix + "_x"), number(prefix + "_y")
            return (x, y) if x is not None and y is not None else None

        return cls(
            table_width=number("table_width", table_width),
            table_height=number("table_height", table_height),
            margin=number("margin", 0.0),
            simplify_tolerance=number("simplify_tolerance", 0.0),
            flatness=number("flatness", DEFAULT_FLATNESS),
            keep_aspect=flag("keep_aspect", True),
            optimize_order=flag("optimize_order", True),
            allow_reverse=flag("allow_reverse", True),
            start_point=point("start"),
            end_point=point("end"),
            layers=int(number("layers", 1) or 1),
            layer_mode=values.get("layer_mode") or "length",
            feedrate=number("feedrate"),
        )


class ConversionResult:
    """The gcode produced for a drawing, one entry per layer"""

    def __init__(self, layers, start_point, end_point, traced_length, points_before, points_after):
        self.layers = layers                    # list of lists of gcode lines
        self.start_point = start_point
        self.end_point = end_point
        self.traced_length = traced_length
        self.points_before = points_before
        self.points_after = points_after

    @property
    def gcode(self):
        """The whole drawing as a single gcode program"""
        return [line for layer in self.layers for line in layer]


def prepare(subpaths, options):
    """Fits, simplifies and orders the subpaths, without generating the gcode yet"""
    subpaths = [list(subpath) for subpath in subpaths if len(subpath) > 1]
    if not subpaths:
        return []

    subpaths = fit(subpaths, options.table_width, options.table_height,
                   margin=options.margin, keep_aspect=options.keep_aspect)
    # simplifying after the fit means the tolerance is in machine units, so the
    # same value gives the same result whatever the scale of the source file
    subpaths = simplify_subpaths(subpaths, options.simplify_tolerance)
    if options.optimize_order:
        subpaths = order_subpaths(subpaths, start=options.start_point,
                                  allow_reverse=options.allow_reverse)
    if options.start_point is not None and subpaths:
        # the very first move must come from the requested start point
        if subpaths[0][0] != options.start_point:
            subpaths.insert(0, [options.start_point, subpaths[0][0]])
    subpaths = close_to(subpaths, options.end_point)
    return clamp(subpaths, options.table_width, options.table_height)


def convert(drawing, options, name=None):
    """Converts an `SvgDrawing` (or a list of subpaths) into gcode.

    Returns a `ConversionResult` whose `layers` holds one gcode program per
    layer. Playing the layers in order builds the picture up progressively, and
    each one starts where the previous stopped.
    """
    if isinstance(drawing, SvgDrawing):
        document_layers = drawing.layers
        subpaths = drawing.subpaths
    else:
        document_layers = None
        subpaths = list(drawing)

    points_before = sum(len(subpath) for subpath in subpaths)
    prepared = prepare(subpaths, options)
    if not prepared:
        return ConversionResult([], None, None, 0.0, points_before, 0)

    if options.layer_mode == "document" and document_layers and len(document_layers) > 1:
        # the subpaths were reordered globally, so the document layers are
        # rebuilt by counting how many subpaths each one contributed
        groups = _regroup_by_document_layers(prepared, document_layers, options)
    else:
        groups = split_in_layers(prepared, options.layers)

    layers = []
    position = options.start_point
    for index, group in enumerate(groups):
        header = []
        if name:
            header.append("Generated by Sandypi from {}".format(name))
        if len(groups) > 1:
            header.append("Layer {} of {}".format(index + 1, len(groups)))
        layers.append(to_gcode(group, feedrate=options.feedrate, header=header))
        position = group[-1][-1]

    return ConversionResult(
        layers=layers,
        start_point=prepared[0][0],
        end_point=prepared[-1][-1],
        traced_length=total_length(prepared, start=options.start_point),
        points_before=points_before,
        points_after=sum(len(subpath) for subpath in prepared),
    )


def _regroup_by_document_layers(prepared, document_layers, options):
    """Splits the prepared subpaths following the layers of the SVG document.

    The optimizer works on the whole drawing, so the subpaths no longer match the
    document order one to one. What is preserved is *how many* subpaths each
    document layer contributed, which is enough to cut the ordered list in groups
    of the right size while keeping the continuity between them.
    """
    sizes = [len(paths) for _, paths in document_layers if paths]
    total = sum(sizes)
    if not total:
        return [prepared]

    # `prepare` may have dropped or added subpaths (simplification, start move)
    scale = len(prepared) / float(total)
    groups = []
    index = 0
    for position, size in enumerate(sizes):
        if position == len(sizes) - 1:
            count = len(prepared) - index
        else:
            count = int(round(size * scale))
            count = min(count, len(prepared) - index - (len(sizes) - position - 1))
            count = max(count, 1)
        if count <= 0:
            continue
        groups.append(prepared[index:index + count])
        index += count
    return [group for group in groups if group]


def convert_file(source, options, name=None):
    """Reads an SVG from `source` and converts it, in one call"""
    drawing = parse_svg(source, flatness=options.flatness)
    return convert(drawing, options, name=name)
