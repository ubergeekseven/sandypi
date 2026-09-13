"""Tests for the conversion of SVG polylines into gcode"""

from math import hypot

import pytest

from server.preprocessing.svg_parser import parse_svg
from server.preprocessing.svg_to_gcode import (
    ConversionOptions, RDP_WINDOW, bounds, clamp, convert, convert_file, cut_at, fit,
    order_subpaths, path_length, simplify, simplify_subpaths, split_in_layers, to_gcode,
    total_length)


TABLE = (1100.0, 540.0)


def options(**kwargs):
    kwargs.setdefault("table_width", TABLE[0])
    kwargs.setdefault("table_height", TABLE[1])
    return ConversionOptions(**kwargs)


def coordinates(lines):
    """Extracts the (x, y) pairs from a gcode program"""
    points = []
    for line in lines:
        if line.startswith(";"):
            continue
        parts = line.split()
        x = next((float(p[1:]) for p in parts if p.startswith("X")), None)
        y = next((float(p[1:]) for p in parts if p.startswith("Y")), None)
        points.append((x, y))
    return points


# ------------------------------------------------------------------------- simplifying

def test_simplify_drops_the_points_on_a_straight_line():
    line = [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)]
    assert simplify(line, 0.1) == [(0, 0), (4, 0)]


def test_simplify_keeps_the_corners():
    corner = [(0, 0), (1, 0), (2, 0), (2, 1), (2, 2)]
    assert simplify(corner, 0.1) == [(0, 0), (2, 0), (2, 2)]


def test_simplify_keeps_a_deviation_above_the_tolerance():
    bump = [(0, 0), (1, 0.5), (2, 0)]
    assert simplify(bump, 0.1) == bump
    assert simplify(bump, 1.0) == [(0, 0), (2, 0)]


def test_simplify_with_no_tolerance_changes_nothing():
    points = [(0, 0), (1, 0.001), (2, 0)]
    assert simplify(points, 0) == points


def test_simplify_endpoints_are_never_dropped():
    wiggle = [(round(3 * (i % 7), 3), round(2 * (i % 5), 3)) for i in range(50)]
    result = simplify(wiggle, 0.5)
    assert result[0] == wiggle[0] and result[-1] == wiggle[-1]


def test_simplify_handles_a_closed_path():
    # first and last point coincide: the chord has zero length
    square = [(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]
    assert len(simplify(square, 0.1)) == 5


def test_simplify_survives_a_long_unsimplifiable_path():
    """A noisy stroke is the worst case for RDP: it must stay fast and not blow the stack"""
    zigzag = [(i, i % 2) for i in range(20000)]
    assert len(simplify(zigzag, 0.1)) == 20000  # nothing can be dropped, and nothing is lost


def test_simplify_windows_do_not_spoil_the_result():
    """Long paths are simplified in windows: the joins must stay almost free"""
    straight = [(i, 0) for i in range(5 * RDP_WINDOW)]
    result = simplify(straight, 0.1)
    assert result[0] == (0, 0) and result[-1] == straight[-1]
    assert len(result) < 10                     # a handful of window boundaries, not thousands
    assert all(y == 0 for _, y in result)


def test_simplify_subpaths_drops_what_collapses():
    # the second subpath traces nothing: it would only add a useless move
    assert simplify_subpaths([[(0, 0), (1, 0), (2, 0)], [(5, 5), (5, 5)]], 0.1) == [[(0, 0), (2, 0)]]


# --------------------------------------------------------------------------- measuring

def test_path_length():
    assert path_length([(0, 0), (3, 0), (3, 4)]) == 7


def test_total_length_counts_the_travel_between_subpaths():
    subpaths = [[(0, 0), (10, 0)], [(20, 0), (30, 0)]]
    assert total_length(subpaths, include_travel=False) == 20
    assert total_length(subpaths) == 30                  # the 10 units of travel are traced too


def test_total_length_from_a_start_point():
    assert total_length([[(10, 0), (20, 0)]], start=(0, 0)) == 20


def test_bounds_of_an_empty_drawing():
    assert bounds([]) is None


# ----------------------------------------------------------------------------- fitting

def test_fit_fills_the_table_keeping_the_aspect_ratio():
    square = [[(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]]
    fitted = fit(square, *TABLE)
    min_x, min_y, max_x, max_y = bounds(fitted)
    assert max_y - min_y == pytest.approx(540)           # the short side is the limit
    assert max_x - min_x == pytest.approx(540)           # still square
    assert (min_x + max_x) / 2 == pytest.approx(550)     # centered on the table


def test_fit_can_stretch():
    square = [[(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]]
    min_x, min_y, max_x, max_y = bounds(fit(square, *TABLE, keep_aspect=False))
    assert (max_x - min_x, max_y - min_y) == (pytest.approx(1100), pytest.approx(540))


def test_fit_respects_the_margin():
    square = [[(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]]
    min_x, min_y, max_x, max_y = bounds(fit(square, *TABLE, margin=20, keep_aspect=False))
    assert (min_x, min_y) == (pytest.approx(20), pytest.approx(20))
    assert (max_x, max_y) == (pytest.approx(1080), pytest.approx(520))


def test_fit_of_a_flat_drawing_does_not_divide_by_zero():
    horizontal = [[(0, 5), (10, 5)]]
    fitted = fit(horizontal, *TABLE)
    assert all(0 <= x <= TABLE[0] and 0 <= y <= TABLE[1] for x, y in fitted[0])


def test_clamp_keeps_everything_inside_the_machine():
    clamped = clamp([[(-10, -10), (2000, 2000)]], *TABLE)
    assert clamped == [[(0.0, 0.0), (1100.0, 540.0)]]


# ---------------------------------------------------------------------------- ordering

def test_ordering_shortens_the_travel():
    # three strokes given in the worst possible order
    subpaths = [[(0, 0), (10, 0)], [(500, 0), (510, 0)], [(20, 0), (30, 0)]]
    naive = total_length(subpaths, start=(0, 0))
    ordered = order_subpaths(subpaths, start=(0, 0))
    assert total_length(ordered, start=(0, 0)) < naive
    assert ordered[1][0] == (20, 0)              # the near stroke is drawn before the far one


def test_ordering_reverses_a_stroke_when_its_other_end_is_closer():
    subpaths = [[(0, 0), (10, 0)], [(100, 0), (11, 0)]]
    ordered = order_subpaths(subpaths, start=(0, 0))
    assert ordered[1][0] == (11, 0)              # drawn backwards, from the near end
    assert ordered[1][-1] == (100, 0)


def test_ordering_can_be_told_not_to_reverse():
    subpaths = [[(0, 0), (10, 0)], [(100, 0), (11, 0)]]
    ordered = order_subpaths(subpaths, start=(0, 0), allow_reverse=False)
    assert ordered[1][0] == (100, 0)


def test_ordering_is_deterministic_without_a_start_point():
    subpaths = [[(50, 50), (60, 50)], [(1, 1), (2, 2)]]
    assert order_subpaths(subpaths)[0][0] == (1, 1)


def test_ordering_keeps_every_subpath():
    subpaths = [[(i * 10, 0), (i * 10 + 5, 0)] for i in range(12)]
    assert len(order_subpaths(subpaths, start=(0, 0))) == 12


# ---------------------------------------------------------------------------- layering

@pytest.mark.parametrize("distance, head_end", [(0, (0, 0)), (5, (5, 0)), (10, (10, 0)), (99, (10, 0))])
def test_cut_at(distance, head_end):
    head, tail = cut_at([(0, 0), (10, 0)], distance)
    assert head[-1] == head_end
    assert tail[0] == head_end                  # the cut point is shared
    assert tail[-1] == (10, 0)


def test_cut_at_walks_across_the_segments():
    head, tail = cut_at([(0, 0), (10, 0), (10, 10)], 15)
    assert head[-1] == (10, 5)
    assert head[1] == (10, 0)                   # the corner is kept in the first half


def test_layers_split_the_drawing_without_cutting_a_stroke():
    subpaths = [[(i, 0), (i + 1, 0)] for i in range(12)]
    layers = split_in_layers(subpaths, 3)
    assert len(layers) == 3
    assert sum(len(layer) for layer in layers) == 12
    assert all(subpath in subpaths for layer in layers for subpath in layer)


def test_layers_are_balanced_by_traced_length():
    subpaths = [[(0, i), (10, i)] for i in range(10)]    # ten strokes of the same length
    layers = split_in_layers(subpaths, 5)
    assert [len(layer) for layer in layers] == [2, 2, 2, 2, 2]


def test_every_layer_starts_where_the_previous_one_stopped():
    subpaths = [[(i * 10, 0), (i * 10 + 5, 0)] for i in range(9)]
    layers = split_in_layers(subpaths, 3)
    for previous, following in zip(layers, layers[1:]):
        # the strokes are consecutive, so the ball is already where the next layer begins
        assert subpaths.index(following[0]) == subpaths.index(previous[-1]) + 1


def test_a_single_layer_is_the_whole_drawing():
    subpaths = [[(0, 0), (1, 1)]]
    assert split_in_layers(subpaths, 1) == [subpaths]


def test_a_single_stroke_can_be_cut_in_layers():
    """A spiral is one long path: layering has to be able to cut inside it"""
    spiral = [[(i, 0) for i in range(101)]]
    layers = split_in_layers(spiral, 4)
    assert len(layers) == 4
    assert layers[0][0][0] == (0, 0)
    assert layers[-1][-1][-1] == (100, 0)
    for previous, following in zip(layers, layers[1:]):
        assert previous[-1][-1] == following[0][0]      # the cut point belongs to both halves
    assert sum(path_length(s) for layer in layers for s in layer) == pytest.approx(100)


def test_an_empty_drawing_has_no_layers():
    assert split_in_layers([], 3) == []
    assert split_in_layers([[(1, 1)]], 3) == []


def test_layers_never_exceed_the_requested_number():
    subpaths = [[(0, 0), (1000, 0)]] + [[(i, 1), (i + 1, 1)] for i in range(5)]
    assert len(split_in_layers(subpaths, 3)) <= 3


# ------------------------------------------------------------------------------ gcode

def test_gcode_moves_and_feedrate():
    lines = to_gcode([[(0, 0), (10, 0)]], feedrate=3000, decimals=1)
    assert lines == ["G1 X0.0 Y0.0", "G1 X10.0 Y0.0 F3000.0"]


def test_gcode_feedrate_is_written_once():
    lines = to_gcode([[(0, 0), (1, 0), (2, 0)]], feedrate=3000)
    assert sum("F" in line for line in lines) == 1


def test_gcode_marks_the_connections_between_subpaths():
    lines = to_gcode([[(0, 0), (1, 0)], [(5, 0), (6, 0)]])
    assert lines[0].startswith("G1")             # the drawing starts
    assert lines[2].startswith("G0")             # the jump to the second stroke
    assert sum(line.startswith("G0") for line in lines) == 1


def test_gcode_header_is_commented():
    lines = to_gcode([[(0, 0), (1, 0)]], header=["hello", "world"])
    assert lines[:2] == ["; hello", "; world"]


def test_gcode_skips_degenerate_subpaths():
    assert to_gcode([[(0, 0)]]) == []


def test_gcode_decimals():
    assert to_gcode([[(0, 0), (1.23456, 0)]], decimals=2)[1] == "G1 X1.23 Y0.00"


# ------------------------------------------------------------------------ the options

def test_options_from_the_ui_form():
    values = {"margin": "10", "simplify_tolerance": "0.5", "keep_aspect": "false",
              "layers": "4", "start_x": "0", "start_y": "0", "feedrate": "2500"}
    result = ConversionOptions.from_dict(values, *TABLE)
    assert (result.table_width, result.table_height) == TABLE
    assert result.margin == 10
    assert result.keep_aspect is False
    assert result.layers == 4
    assert result.start_point == (0, 0)
    assert result.end_point is None
    assert result.feedrate == 2500


def test_options_ignore_garbage_and_fall_back():
    result = ConversionOptions.from_dict({"margin": "abc", "layers": ""}, *TABLE)
    assert result.margin == 0.0
    assert result.layers == 1


def test_options_need_both_coordinates_of_a_point():
    assert ConversionOptions.from_dict({"start_x": "10"}, *TABLE).start_point is None


# -------------------------------------------------------------------- the whole recipe

SQUARE = '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">' \
         '<rect x="10" y="10" width="80" height="80"/></svg>'


def test_conversion_fits_the_table():
    result = convert_file(SQUARE, options())
    points = [p for p in coordinates(result.gcode)]
    assert all(0 <= x <= TABLE[0] and 0 <= y <= TABLE[1] for x, y in points)
    xs = [x for x, _ in points]
    assert max(xs) - min(xs) == pytest.approx(540)


def test_conversion_reports_the_start_and_end_points():
    result = convert_file(SQUARE, options(start_point=(0, 0), end_point=(1100, 540)))
    assert result.start_point == (0, 0)
    assert result.end_point == (1100, 540)
    assert coordinates(result.gcode)[0] == (0, 0)
    assert coordinates(result.gcode)[-1] == (1100, 540)


def test_conversion_reports_the_traced_length():
    result = convert_file(SQUARE, options())
    assert result.traced_length == pytest.approx(4 * 540, abs=1)


def test_simplification_reduces_the_number_of_points():
    circle = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">' \
             '<circle cx="50" cy="50" r="40"/></svg>'
    detailed = convert_file(circle, options(flatness=0.01))
    coarse = convert_file(circle, options(flatness=0.01, simplify_tolerance=5))
    assert coarse.points_after < detailed.points_after
    assert coarse.points_before == detailed.points_before


def test_conversion_produces_the_requested_layers():
    body = "".join('<line x1="0" y1="{0}" x2="100" y2="{0}"/>'.format(i * 10) for i in range(10))
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">{}</svg>'.format(body)
    result = convert_file(svg, options(layers=5))
    assert len(result.layers) == 5
    assert all(layer for layer in result.layers)
    # played one after the other the layers are exactly the whole drawing
    assert result.gcode == [line for layer in result.layers for line in layer]


def test_layers_are_continuous():
    """The last point of a layer is where the next one starts: the ball does not jump"""
    body = "".join('<line x1="0" y1="{0}" x2="100" y2="{0}"/>'.format(i * 10) for i in range(10))
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">{}</svg>'.format(body)
    result = convert_file(svg, options(layers=4))
    for previous, following in zip(result.layers, result.layers[1:]):
        end = coordinates(previous)[-1]
        start = coordinates(following)[0]
        assert hypot(start[0] - end[0], start[1] - end[1]) < 200    # a short connecting move, not a jump


def test_layers_carry_a_comment():
    body = "".join('<line x1="0" y1="{0}" x2="100" y2="{0}"/>'.format(i * 10) for i in range(6))
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">{}</svg>'.format(body)
    result = convert_file(svg, options(layers=3), name="picture.svg")
    assert "; Layer 2 of 3" in result.layers[1]
    assert any("picture.svg" in line for line in result.layers[0])


def test_document_layers_are_used_when_asked():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" '
           'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" viewBox="0 0 100 100">'
           '<g inkscape:groupmode="layer" inkscape:label="one">'
           '<line x1="0" y1="0" x2="100" y2="0"/></g>'
           '<g inkscape:groupmode="layer" inkscape:label="two">'
           '<line x1="0" y1="50" x2="100" y2="50"/>'
           '<line x1="0" y1="60" x2="100" y2="60"/></g></svg>')
    result = convert_file(svg, options(layer_mode="document"))
    assert len(result.layers) == 2


def test_document_mode_falls_back_when_there_are_no_layers():
    result = convert_file(SQUARE, options(layer_mode="document", layers=2))
    assert len(result.layers) == 2       # no document layers: the length split is used instead


def test_an_empty_drawing_converts_to_nothing():
    result = convert_file('<svg xmlns="http://www.w3.org/2000/svg"/>', options())
    assert result.layers == []
    assert result.gcode == []
    assert result.start_point is None


def test_convert_accepts_plain_subpaths():
    result = convert([[(0, 0), (100, 0), (100, 100)]], options())
    assert result.gcode


def test_conversion_from_a_file_object(tmp_path):
    path = tmp_path / "drawing.svg"
    path.write_text(SQUARE)
    with open(path) as f:
        assert convert_file(f, options()).gcode
