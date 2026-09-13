"""Tests for the SVG reader"""

from math import hypot

import pytest

from server.preprocessing.svg_parser import (
    IDENTITY, SvgParseError, apply, multiply, parse_length, parse_path, parse_svg, parse_transform)


def document(body, attributes='width="100" height="100" viewBox="0 0 100 100"'):
    return '<svg xmlns="http://www.w3.org/2000/svg" {}>{}</svg>'.format(attributes, body)


def flat(drawing):
    return drawing.subpaths


def approx_points(points):
    return [(pytest.approx(x, abs=1e-6), pytest.approx(y, abs=1e-6)) for x, y in points]


# ------------------------------------------------------------------------- transforms

def test_transform_translate_and_scale():
    matrix = parse_transform("translate(10, 5) scale(2)")
    assert apply(matrix, (1, 1)) == (12, 7)


def test_transform_order_is_left_to_right():
    # scale applied first, then the translation, as the spec requires
    assert apply(parse_transform("scale(2) translate(10, 0)"), (0, 0)) == (20, 0)
    assert apply(parse_transform("translate(10, 0) scale(2)"), (0, 0)) == (10, 0)


def test_transform_rotate_around_a_point():
    x, y = apply(parse_transform("rotate(90, 10, 10)"), (10, 0))
    assert (pytest.approx(x, abs=1e-9), pytest.approx(y, abs=1e-9)) == (20, 10)


def test_transform_matrix_and_empty():
    assert parse_transform("") == IDENTITY
    assert parse_transform("matrix(1 0 0 1 5 5)") == (1, 0, 0, 1, 5, 5)
    assert multiply(IDENTITY, IDENTITY) == IDENTITY


def test_nested_transforms_compose():
    drawing = parse_svg(document(
        '<g transform="translate(10,0)"><g transform="scale(2)">'
        '<line x1="0" y1="0" x2="5" y2="0"/></g></g>'))
    assert approx_points(flat(drawing)[0]) == [(10, 0), (20, 0)]


# ------------------------------------------------------------------------------ units

@pytest.mark.parametrize("value, expected", [
    ("10", 10), ("10px", 10), ("1in", 96), ("25.4mm", 96), ("2.54cm", 96), ("72pt", 96),
    ("50%", None), (None, None), ("garbage", None),
])
def test_parse_length(value, expected):
    result = parse_length(value)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected, abs=1e-6)


# ------------------------------------------------------------------------ path syntax

def test_absolute_and_relative_lines_agree():
    absolute = parse_path("M 0 0 L 10 0 L 10 10")
    relative = parse_path("m 0 0 l 10 0 l 0 10")
    assert absolute == relative


def test_implicit_lineto_after_moveto():
    # a moveto with several coordinate pairs continues with linetos
    assert parse_path("M 0 0 10 0 10 10")[0] == [(0, 0), (10, 0), (10, 10)]


def test_horizontal_and_vertical():
    assert parse_path("M 0 0 H 10 V 10 h -10 v -10")[0] == [
        (0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]


def test_close_path_returns_to_the_start():
    subpaths = parse_path("M 0 0 L 10 0 L 10 10 Z")
    assert subpaths[0][0] == subpaths[0][-1] == (0, 0)


def test_multiple_subpaths():
    subpaths = parse_path("M 0 0 L 1 0 M 5 5 L 6 5")
    assert len(subpaths) == 2
    assert subpaths[1] == [(5, 5), (6, 5)]


def test_a_straight_cubic_needs_no_subdivision():
    points = parse_path("M 0 0 C 10 0 20 0 30 0")[0]
    assert points == [(0, 0), (30, 0)]
    assert all(y == 0 for _, y in points)


def test_a_curved_cubic_is_flattened_within_the_tolerance():
    points = parse_path("M 0 0 C 0 100 100 100 100 0", flatness=0.1)[0]
    assert len(points) > 8
    assert points[0] == (0, 0) and points[-1] == (100, 0)
    # the extreme of that symmetric curve sits at 3/4 of the control height
    assert max(y for _, y in points) == pytest.approx(75, abs=1)


def test_smooth_cubic_reflects_the_previous_control_point():
    reflected = parse_path("M 0 0 C 0 10 10 10 10 0 S 20 -10 20 0", flatness=0.01)[0]
    explicit = parse_path("M 0 0 C 0 10 10 10 10 0 C 10 -10 20 -10 20 0", flatness=0.01)[0]
    assert reflected == explicit


def test_quadratic_and_smooth_quadratic():
    quadratic = parse_path("M 0 0 Q 50 50 100 0", flatness=0.05)[0]
    assert quadratic[0] == (0, 0) and quadratic[-1] == (100, 0)
    assert max(y for _, y in quadratic) == pytest.approx(25, abs=0.5)

    smooth = parse_path("M 0 0 Q 25 25 50 0 T 100 0", flatness=0.05)[0]
    assert smooth[-1] == (100, 0)
    assert min(y for _, y in smooth) == pytest.approx(-12.5, abs=0.5)


def test_arc_draws_a_quarter_circle():
    points = parse_path("M 100 0 A 100 100 0 0 1 0 100", flatness=0.05)[0]
    assert points[0] == (100, 0)
    assert points[-1][0] == pytest.approx(0, abs=1e-6)
    assert points[-1][1] == pytest.approx(100, abs=1e-6)
    for x, y in points:
        assert hypot(x, y) == pytest.approx(100, abs=0.1)   # every point is on the circle


def test_arc_with_zero_radius_is_a_line():
    assert parse_path("M 0 0 A 0 0 0 0 1 10 10")[0] == [(0, 0), (10, 10)]


def test_arc_radii_too_small_are_scaled_up():
    # the radius cannot span the endpoints, the spec says to enlarge it
    points = parse_path("M 0 0 A 1 1 0 0 1 100 0", flatness=0.05)[0]
    assert points[-1][0] == pytest.approx(100, abs=1e-6)
    assert max(abs(y) for _, y in points) == pytest.approx(50, abs=1)


def test_empty_and_broken_path_data():
    assert parse_path("") == []
    assert parse_path("M 0 0") == []            # a single point is not a path
    assert parse_path("garbage") == []


# ----------------------------------------------------------------------------- shapes

def test_line_polyline_and_polygon():
    drawing = parse_svg(document(
        '<line x1="0" y1="0" x2="10" y2="0"/>'
        '<polyline points="0,5 10,5 20,5"/>'
        '<polygon points="0,10 10,10 10,20"/>'))
    line, polyline, polygon = flat(drawing)
    assert line == [(0, 0), (10, 0)]
    assert len(polyline) == 3
    assert polygon[0] == polygon[-1]            # a polygon is closed


def test_rect():
    drawing = parse_svg(document('<rect x="10" y="10" width="30" height="20"/>'))
    points = flat(drawing)[0]
    assert points[0] == points[-1]
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    assert (min(xs), max(xs)) == (10, 40)
    assert (min(ys), max(ys)) == (-30, -10)     # y is flipped


def test_rounded_rect_stays_inside_the_box():
    drawing = parse_svg(document('<rect x="0" y="0" width="40" height="20" rx="5"/>'))
    points = flat(drawing)[0]
    assert all(-0.001 <= x <= 40.001 for x, _ in points)
    assert all(-20.001 <= y <= 0.001 for _, y in points)
    # the corners are rounded, so no point sits exactly on them
    assert not any(abs(x) < 1e-6 and abs(y) < 1e-6 for x, y in points)


def test_circle_and_ellipse():
    drawing = parse_svg(document('<circle cx="50" cy="50" r="20"/><ellipse cx="50" cy="50" rx="30" ry="10"/>'))
    circle, ellipse = flat(drawing)
    for x, y in circle:
        assert hypot(x - 50, y + 50) == pytest.approx(20, abs=0.2)
    xs = [p[0] for p in ellipse]
    ys = [p[1] for p in ellipse]
    assert max(xs) - min(xs) == pytest.approx(60, abs=0.3)
    assert max(ys) - min(ys) == pytest.approx(20, abs=0.3)


def test_degenerate_shapes_are_skipped():
    drawing = parse_svg(document(
        '<rect x="0" y="0" width="0" height="10"/><circle cx="1" cy="1" r="0"/>'
        '<polyline points="1,1"/>'))
    assert flat(drawing) == []


# ------------------------------------------------------------------------- the document

def test_y_axis_is_flipped():
    drawing = parse_svg(document('<line x1="0" y1="0" x2="0" y2="10"/>'))
    assert flat(drawing)[0] == [(0, 0), (0, -10)]


def test_viewbox_is_mapped_on_the_viewport():
    drawing = parse_svg(document('<line x1="0" y1="0" x2="10" y2="0"/>',
                                 attributes='width="200" height="200" viewBox="0 0 100 100"'))
    assert approx_points(flat(drawing)[0]) == [(0, 0), (20, 0)]


def test_viewbox_offset_is_applied():
    drawing = parse_svg(document('<line x1="10" y1="10" x2="20" y2="10"/>',
                                 attributes='width="100" height="100" viewBox="10 10 100 100"'))
    assert approx_points(flat(drawing)[0]) == [(0, 0), (10, 0)]


def test_viewbox_without_a_viewport_is_the_coordinate_system():
    drawing = parse_svg(document('<line x1="0" y1="0" x2="10" y2="0"/>', attributes='viewBox="0 0 100 100"'))
    assert flat(drawing)[0] == [(0, 0), (10, 0)]


def test_hidden_elements_are_skipped():
    drawing = parse_svg(document(
        '<line x1="0" y1="0" x2="1" y2="0" display="none"/>'
        '<line x1="0" y1="0" x2="2" y2="0" style="display:none"/>'
        '<g display="none"><line x1="0" y1="0" x2="3" y2="0"/></g>'
        '<line x1="0" y1="0" x2="4" y2="0"/>'))
    assert flat(drawing) == [[(0, 0), (4, 0)]]


def test_defs_are_not_drawn():
    drawing = parse_svg(document('<defs><line x1="0" y1="0" x2="9" y2="0"/></defs>'
                                 '<line x1="0" y1="0" x2="1" y2="0"/>'))
    assert flat(drawing) == [[(0, 0), (1, 0)]]


def test_inkscape_layers_are_kept_apart():
    drawing = parse_svg(document(
        '<g inkscape:groupmode="layer" inkscape:label="Background">'
        '  <line x1="0" y1="0" x2="1" y2="0"/>'
        '</g>'
        '<g inkscape:groupmode="layer" inkscape:label="Detail">'
        '  <line x1="0" y1="1" x2="1" y2="1"/>'
        '  <line x1="0" y1="2" x2="1" y2="2"/>'
        '</g>',
        attributes='width="100" height="100" viewBox="0 0 100 100" '
                   'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape"'))
    assert [name for name, _ in drawing.layers] == ["Background", "Detail"]
    assert [len(paths) for _, paths in drawing.layers] == [1, 2]


def test_plain_groups_are_not_layers():
    drawing = parse_svg(document('<g><line x1="0" y1="0" x2="1" y2="0"/></g>'))
    assert len(drawing.layers) == 1


def test_bounds():
    drawing = parse_svg(document('<rect x="10" y="20" width="30" height="40"/>'))
    min_x, min_y, max_x, max_y = drawing.bounds()
    assert (min_x, max_x) == (10, 40)
    assert (min_y, max_y) == (-60, -20)


def test_empty_document_has_no_bounds():
    assert parse_svg(document("")).bounds() is None


# ------------------------------------------------------------------------------ errors

def test_invalid_xml_is_reported():
    with pytest.raises(SvgParseError):
        parse_svg("<svg><line")


def test_a_non_svg_document_is_reported():
    with pytest.raises(SvgParseError):
        parse_svg("<html><body/></html>")
