"""A self contained SVG reader that turns a drawing into polylines.

Sandypi runs on very modest hardware (a Pi Zero is a supported target), so this
module deliberately avoids the usual svg/geometry stack and only uses the
standard library. It covers what vector drawings meant for a sand table actually
contain: the shape elements, the full path syntax including arcs, nested
transforms and the `viewBox`.

The output is always a list of subpaths, each one a list of `(x, y)` points in
user units with the Y axis pointing **up** (SVG has it pointing down, the
conversion is done here so that everything downstream is in table coordinates).
"""

import re
from math import acos, atan2, ceil, cos, degrees, hypot, isfinite, pi, radians, sin, sqrt
from xml.etree import ElementTree

SVG_NS = "http://www.w3.org/2000/svg"

# maximum distance between the flattened polyline and the true curve, in user units
DEFAULT_FLATNESS = 0.2
# guard against pathological curves
MAX_SUBDIVISION_DEPTH = 16

_NUMBER = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
_COMMAND = re.compile(r"[MmZzLlHhVvCcSsQqTtAa]")
_TRANSFORM = re.compile(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)")


class SvgParseError(Exception):
    """Raised when the file cannot be read as an SVG drawing"""


# --------------------------------------------------------------------------- matrices

# A transform is the tuple (a, b, c, d, e, f) of the SVG matrix:
#   | a c e |
#   | b d f |
#   | 0 0 1 |
IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def multiply(m, n):
    """Returns the matrix product m*n (m applied after n)"""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def apply(m, point):
    a, b, c, d, e, f = m
    x, y = point
    return (a * x + c * y + e, b * x + d * y + f)


def parse_transform(value):
    """Parses the content of a `transform` attribute into a single matrix"""
    if not value:
        return IDENTITY
    matrix = IDENTITY
    for name, args in _TRANSFORM.findall(value):
        numbers = [float(n) for n in _NUMBER.findall(args)]
        if name == "matrix" and len(numbers) == 6:
            step = tuple(numbers)
        elif name == "translate":
            tx = numbers[0] if numbers else 0.0
            ty = numbers[1] if len(numbers) > 1 else 0.0
            step = (1.0, 0.0, 0.0, 1.0, tx, ty)
        elif name == "scale":
            sx = numbers[0] if numbers else 1.0
            sy = numbers[1] if len(numbers) > 1 else sx
            step = (sx, 0.0, 0.0, sy, 0.0, 0.0)
        elif name == "rotate":
            angle = radians(numbers[0]) if numbers else 0.0
            step = (cos(angle), sin(angle), -sin(angle), cos(angle), 0.0, 0.0)
            if len(numbers) >= 3:   # rotation around a point
                cx, cy = numbers[1], numbers[2]
                step = multiply((1.0, 0.0, 0.0, 1.0, cx, cy), step)
                step = multiply(step, (1.0, 0.0, 0.0, 1.0, -cx, -cy))
        elif name == "skewX":
            from math import tan
            step = (1.0, 0.0, tan(radians(numbers[0] if numbers else 0.0)), 1.0, 0.0, 0.0)
        elif name == "skewY":
            from math import tan
            step = (1.0, tan(radians(numbers[0] if numbers else 0.0)), 0.0, 1.0, 0.0, 0.0)
        else:
            continue
        matrix = multiply(matrix, step)
    return matrix


# ----------------------------------------------------------------------------- curves

def _flatten_cubic(points, p0, p1, p2, p3, flatness, depth=0):
    """Appends a cubic bezier to `points` as a polyline, subdividing where it bends"""
    if depth >= MAX_SUBDIVISION_DEPTH or _is_flat(p0, p1, p2, p3, flatness):
        points.append(p3)
        return
    # de Casteljau split at t=0.5
    p01 = _mid(p0, p1)
    p12 = _mid(p1, p2)
    p23 = _mid(p2, p3)
    p012 = _mid(p01, p12)
    p123 = _mid(p12, p23)
    mid = _mid(p012, p123)
    _flatten_cubic(points, p0, p01, p012, mid, flatness, depth + 1)
    _flatten_cubic(points, mid, p123, p23, p3, flatness, depth + 1)


def _mid(a, b):
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def _is_flat(p0, p1, p2, p3, flatness):
    """Distance of the control points from the chord, the usual flatness criterion"""
    dx = p3[0] - p0[0]
    dy = p3[1] - p0[1]
    d1 = abs((p1[0] - p3[0]) * dy - (p1[1] - p3[1]) * dx)
    d2 = abs((p2[0] - p3[0]) * dy - (p2[1] - p3[1]) * dx)
    total = (d1 + d2) ** 2
    return total <= flatness * (dx * dx + dy * dy)


def _quadratic_to_cubic(p0, p1, p2):
    """A quadratic bezier is a cubic with the control points at 2/3 of the way"""
    c1 = (p0[0] + 2.0 / 3.0 * (p1[0] - p0[0]), p0[1] + 2.0 / 3.0 * (p1[1] - p0[1]))
    c2 = (p2[0] + 2.0 / 3.0 * (p1[0] - p2[0]), p2[1] + 2.0 / 3.0 * (p1[1] - p2[1]))
    return c1, c2


def _flatten_arc(points, start, rx, ry, rotation, large_arc, sweep, end, flatness):
    """Appends an elliptical arc to `points`, following the SVG endpoint parameterization"""
    if start == end:
        return
    rx, ry = abs(rx), abs(ry)
    if rx == 0 or ry == 0:          # degenerate arc: the spec says to draw a line
        points.append(end)
        return

    phi = radians(rotation % 360.0)
    cos_phi, sin_phi = cos(phi), sin(phi)

    # step 1: compute (x1', y1')
    dx2 = (start[0] - end[0]) / 2.0
    dy2 = (start[1] - end[1]) / 2.0
    x1p = cos_phi * dx2 + sin_phi * dy2
    y1p = -sin_phi * dx2 + cos_phi * dy2

    # correct out of range radii
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1:
        scale = sqrt(lam)
        rx *= scale
        ry *= scale

    # step 2: compute (cx', cy')
    numerator = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    denominator = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    factor = sqrt(max(0.0, numerator / denominator)) if denominator else 0.0
    if large_arc == sweep:
        factor = -factor
    cxp = factor * rx * y1p / ry
    cyp = -factor * ry * x1p / rx

    # step 3: back to the original coordinate system
    cx = cos_phi * cxp - sin_phi * cyp + (start[0] + end[0]) / 2.0
    cy = sin_phi * cxp + cos_phi * cyp + (start[1] + end[1]) / 2.0

    # step 4: the angles
    def angle(ux, uy, vx, vy):
        dot = ux * vx + uy * vy
        norm = hypot(ux, uy) * hypot(vx, vy)
        if norm == 0:
            return 0.0
        value = max(-1.0, min(1.0, dot / norm))
        result = acos(value)
        if ux * vy - uy * vx < 0:
            result = -result
        return result

    theta1 = angle(1.0, 0.0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    delta = angle((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and delta > 0:
        delta -= 2 * pi
    elif sweep and delta < 0:
        delta += 2 * pi

    # the number of segments is chosen so that the sagitta stays below the flatness
    radius = max(rx, ry)
    max_angle = 2 * acos(max(-1.0, min(1.0, 1.0 - flatness / radius))) if radius > flatness else pi / 2
    steps = max(2, int(ceil(abs(delta) / max(max_angle, 1e-3))))
    for i in range(1, steps + 1):
        theta = theta1 + delta * i / steps
        x = cos_phi * rx * cos(theta) - sin_phi * ry * sin(theta) + cx
        y = sin_phi * rx * cos(theta) + cos_phi * ry * sin(theta) + cy
        points.append((x, y))


# ------------------------------------------------------------------------ path syntax

def _tokenize_path(data):
    """Splits a `d` attribute into (command, [numbers]) pairs"""
    tokens = []
    index = 0
    length = len(data)
    while index < length:
        match = _COMMAND.search(data, index)
        if match is None:
            break
        command = match.group()
        start = match.end()
        next_match = _COMMAND.search(data, start)
        end = next_match.start() if next_match else length
        numbers = [float(n) for n in _NUMBER.findall(data[start:end])]
        tokens.append((command, numbers))
        index = end
    return tokens


# number of parameters consumed by every path command
_ARGUMENT_COUNT = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0}


def parse_path(data, flatness=DEFAULT_FLATNESS):
    """Converts a `d` attribute into a list of subpaths (lists of points)"""
    subpaths = []
    current = []
    position = (0.0, 0.0)
    subpath_start = (0.0, 0.0)
    previous_command = None
    previous_control = None

    def close_current():
        if len(current) > 1:
            subpaths.append(list(current))
        del current[:]

    for command, numbers in _tokenize_path(data):
        upper = command.upper()
        relative = command.islower()
        count = _ARGUMENT_COUNT[upper]

        if upper == "Z":
            if current:
                if current[0] != position:
                    current.append(current[0])
                position = subpath_start
                close_current()
            previous_command = upper
            previous_control = None
            continue

        if count and not numbers:
            continue

        # a command may carry several sets of arguments; after the first one an
        # implicit M becomes an L (and m becomes l), as required by the spec
        groups = [numbers[i:i + count] for i in range(0, len(numbers) - count + 1, count)]
        for group_index, group in enumerate(groups):
            effective = upper
            if upper == "M" and group_index > 0:
                effective = "L"

            if effective == "M":
                close_current()
                position = _point(group, position, relative)
                subpath_start = position
                current.append(position)
                previous_control = None
            elif effective == "L":
                position = _point(group, position, relative)
                current.append(position)
                previous_control = None
            elif effective == "H":
                x = group[0] + (position[0] if relative else 0.0)
                position = (x, position[1])
                current.append(position)
                previous_control = None
            elif effective == "V":
                y = group[0] + (position[1] if relative else 0.0)
                position = (position[0], y)
                current.append(position)
                previous_control = None
            elif effective in ("C", "S", "Q", "T"):
                if not current:
                    current.append(position)
                if effective == "C":
                    c1 = _point(group[0:2], position, relative)
                    c2 = _point(group[2:4], position, relative)
                    end = _point(group[4:6], position, relative)
                elif effective == "S":
                    c1 = _reflect(previous_control, position) if previous_command in ("C", "S") else position
                    c2 = _point(group[0:2], position, relative)
                    end = _point(group[2:4], position, relative)
                elif effective == "Q":
                    control = _point(group[0:2], position, relative)
                    end = _point(group[2:4], position, relative)
                    c1, c2 = _quadratic_to_cubic(position, control, end)
                    previous_control = control
                else:   # T
                    control = _reflect(previous_control, position) if previous_command in ("Q", "T") else position
                    end = _point(group[0:2], position, relative)
                    c1, c2 = _quadratic_to_cubic(position, control, end)
                    previous_control = control
                _flatten_cubic(current, position, c1, c2, end, flatness)
                if effective in ("C", "S"):
                    previous_control = c2
                position = end
            elif effective == "A":
                if not current:
                    current.append(position)
                rx, ry, rotation, large_arc, sweep = group[0], group[1], group[2], bool(group[3]), bool(group[4])
                end = _point(group[5:7], position, relative)
                _flatten_arc(current, position, rx, ry, rotation, large_arc, sweep, end, flatness)
                position = end
                previous_control = None
            previous_command = effective

    close_current()
    return subpaths


def _point(pair, origin, relative):
    x, y = pair[0], pair[1]
    if relative:
        return (origin[0] + x, origin[1] + y)
    return (x, y)


def _reflect(control, position):
    if control is None:
        return position
    return (2 * position[0] - control[0], 2 * position[1] - control[1])


# ----------------------------------------------------------------------------- shapes

def _number(element, name, default=0.0):
    value = element.get(name)
    if value is None:
        return default
    match = _NUMBER.search(value)
    return float(match.group()) if match else default


def _points_attribute(value):
    numbers = [float(n) for n in _NUMBER.findall(value or "")]
    return [(numbers[i], numbers[i + 1]) for i in range(0, len(numbers) - 1, 2)]


def shape_to_subpaths(element, tag, flatness):
    """Converts one of the SVG shape elements into subpaths"""
    if tag == "path":
        return parse_path(element.get("d", ""), flatness)

    if tag == "line":
        return [[(_number(element, "x1"), _number(element, "y1")),
                 (_number(element, "x2"), _number(element, "y2"))]]

    if tag == "polyline":
        points = _points_attribute(element.get("points"))
        return [points] if len(points) > 1 else []

    if tag == "polygon":
        points = _points_attribute(element.get("points"))
        if len(points) < 2:
            return []
        return [points + [points[0]]]

    if tag == "rect":
        x, y = _number(element, "x"), _number(element, "y")
        width, height = _number(element, "width"), _number(element, "height")
        if width <= 0 or height <= 0:
            return []
        rx = _number(element, "rx", -1.0)
        ry = _number(element, "ry", -1.0)
        if rx < 0 and ry < 0:
            rx = ry = 0.0
        elif rx < 0:
            rx = ry
        elif ry < 0:
            ry = rx
        rx = min(rx, width / 2.0)
        ry = min(ry, height / 2.0)
        if rx == 0 or ry == 0:
            return [[(x, y), (x + width, y), (x + width, y + height), (x, y + height), (x, y)]]
        # rounded rectangle, built as four lines joined by four arcs
        data = ("M{} {} H{} A{} {} 0 0 1 {} {} V{} A{} {} 0 0 1 {} {} "
                "H{} A{} {} 0 0 1 {} {} V{} A{} {} 0 0 1 {} {} Z").format(
            x + rx, y, x + width - rx,
            rx, ry, x + width, y + ry,
            y + height - ry,
            rx, ry, x + width - rx, y + height,
            x + rx,
            rx, ry, x, y + height - ry,
            y + ry,
            rx, ry, x + rx, y)
        return parse_path(data, flatness)

    if tag in ("circle", "ellipse"):
        cx, cy = _number(element, "cx"), _number(element, "cy")
        if tag == "circle":
            rx = ry = _number(element, "r")
        else:
            rx, ry = _number(element, "rx"), _number(element, "ry")
        if rx <= 0 or ry <= 0:
            return []
        # two half arcs, so that the ellipse is a single closed subpath
        data = "M{} {} A{} {} 0 1 0 {} {} A{} {} 0 1 0 {} {} Z".format(
            cx - rx, cy, rx, ry, cx + rx, cy, rx, ry, cx - rx, cy)
        return parse_path(data, flatness)

    return []


# ------------------------------------------------------------------------------ units

_UNITS = {"px": 1.0, "pt": 96.0 / 72.0, "pc": 16.0, "mm": 96.0 / 25.4, "cm": 96.0 / 2.54, "in": 96.0}


def parse_length(value, default=None):
    """Converts an SVG length to user units. Percentages cannot be resolved here."""
    if value is None:
        return default
    value = value.strip()
    match = _NUMBER.match(value)
    if not match:
        return default
    number = float(match.group())
    suffix = value[match.end():].strip().lower()
    if suffix in ("", "px", "user"):
        return number
    if suffix == "%":
        return default
    return number * _UNITS.get(suffix, 1.0)


def _viewbox_matrix(root):
    """Builds the matrix mapping the viewBox onto the viewport (xMidYMid meet)"""
    viewbox = root.get("viewBox")
    if not viewbox:
        return IDENTITY
    numbers = [float(n) for n in _NUMBER.findall(viewbox)]
    if len(numbers) != 4 or numbers[2] <= 0 or numbers[3] <= 0:
        return IDENTITY
    min_x, min_y, box_width, box_height = numbers

    width = parse_length(root.get("width"))
    height = parse_length(root.get("height"))
    if width is None and height is None:
        # without a viewport the viewBox *is* the coordinate system
        return IDENTITY
    if width is None:
        width = box_width * height / box_height
    if height is None:
        height = box_height * width / box_width

    preserve = (root.get("preserveAspectRatio") or "xMidYMid meet").split()
    align = preserve[0]
    meet_or_slice = preserve[1] if len(preserve) > 1 else "meet"

    scale_x = width / box_width
    scale_y = height / box_height
    if align != "none":
        scale = min(scale_x, scale_y) if meet_or_slice != "slice" else max(scale_x, scale_y)
        scale_x = scale_y = scale

    translate_x = -min_x * scale_x
    translate_y = -min_y * scale_y
    if align != "none":
        if "xMid" in align:
            translate_x += (width - box_width * scale_x) / 2.0
        elif "xMax" in align:
            translate_x += width - box_width * scale_x
        if "YMid" in align:
            translate_y += (height - box_height * scale_y) / 2.0
        elif "YMax" in align:
            translate_y += height - box_height * scale_y

    return (scale_x, 0.0, 0.0, scale_y, translate_x, translate_y)


# ------------------------------------------------------------------------------ reader

def _local_name(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def _is_hidden(element):
    if element.get("display") == "none":
        return True
    style = element.get("style") or ""
    return "display:none" in style.replace(" ", "")


def _layer_name(element, fallback):
    """Returns the name of an Inkscape/Illustrator layer, when the group is one"""
    for key, value in element.attrib.items():
        if _local_name(key) == "groupmode" and value == "layer":
            for label_key, label in element.attrib.items():
                if _local_name(label_key) == "label":
                    return label
            return element.get("id") or fallback
    return None


class SvgDrawing:
    """The polylines read from an SVG file, grouped by the layers of the document"""

    def __init__(self, layers, width=None, height=None):
        self.layers = layers                    # list of (name, [subpath, ...])
        self.width = width
        self.height = height

    @property
    def subpaths(self):
        """Every subpath of the drawing, in document order"""
        return [subpath for _, paths in self.layers for subpath in paths]

    def bounds(self):
        """Returns (min_x, min_y, max_x, max_y) or None when the drawing is empty"""
        points = [point for subpath in self.subpaths for point in subpath]
        if not points:
            return None
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return (min(xs), min(ys), max(xs), max(ys))


def parse_svg(source, flatness=DEFAULT_FLATNESS):
    """Reads an SVG document and returns an `SvgDrawing`.

    Args:
        source: a path, a file object or the content of the document.
        flatness: maximum distance between the polylines and the real curves,
            in user units.

    The Y axis is flipped so that the result is in the usual "Y goes up"
    convention of a machine, instead of the "Y goes down" of SVG.
    """
    try:
        if hasattr(source, "read"):
            root = ElementTree.parse(source).getroot()
        elif isinstance(source, bytes):
            root = ElementTree.fromstring(source)
        elif isinstance(source, str) and source.lstrip()[:1] == "<":
            root = ElementTree.fromstring(source)
        else:
            root = ElementTree.parse(source).getroot()
    except (ElementTree.ParseError, OSError) as e:
        # OSError covers a string that is neither xml nor the path of an existing file
        raise SvgParseError("The file is not a valid SVG document: {}".format(e))

    if _local_name(root.tag) != "svg":
        raise SvgParseError("The root element of the file is not <svg>")

    base = multiply(_viewbox_matrix(root), parse_transform(root.get("transform")))

    layers = []
    current = ("", [])

    def walk(element, matrix, layer):
        nonlocal current
        for child in element:
            tag = _local_name(child.tag)
            if tag in ("defs", "symbol", "clipPath", "mask", "marker", "metadata", "title", "desc"):
                continue    # not rendered, or rendered only through a <use> which is not supported
            if _is_hidden(child):
                continue
            child_matrix = multiply(matrix, parse_transform(child.get("transform")))

            if tag in ("g", "a", "switch"):
                name = _layer_name(child, "layer {}".format(len(layers) + 1))
                if name is not None:
                    if current[1]:
                        layers.append(current)
                    current = (name, [])
                    walk(child, child_matrix, name)
                    layers.append(current)
                    current = (layer, [])
                else:
                    walk(child, child_matrix, layer)
                continue

            for subpath in shape_to_subpaths(child, tag, flatness):
                transformed = [apply(child_matrix, point) for point in subpath]
                transformed = [p for p in transformed if isfinite(p[0]) and isfinite(p[1])]
                if len(transformed) > 1:
                    current[1].append(transformed)

    walk(root, base, "")
    if current[1]:
        layers.append(current)

    # flipping Y: SVG grows downwards, a table grows upwards
    flipped = [(name, [[(x, -y) for x, y in subpath] for subpath in paths]) for name, paths in layers]

    return SvgDrawing(flipped,
                      width=parse_length(root.get("width")),
                      height=parse_length(root.get("height")))
