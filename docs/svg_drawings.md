# SVG drawings

A sand table only understands gcode, so a vector drawing has to be converted before it can be
played. Sandypi does the conversion itself: drop an `.svg` file on the drawings page and it is
fitted to the table and stored like any other drawing.

The interesting part is that the conversion knows the size of *your* table, so it can also
simplify the drawing, shorten the moves between strokes and split the picture into layers.

## Just drop a file

The dropzone on the drawings page accepts `.svg` next to `.gcode`. The file is scaled to fill the
table, keeping its aspect ratio and centred in the usable area, and that is it.

## The conversion, in detail

### What is read from the file

Every shape element is supported (`path`, `line`, `polyline`, `polygon`, `rect` including rounded
corners, `circle`, `ellipse`), with the full path syntax: bezier curves, smooth curves and
elliptical arcs are flattened into polylines. Nested `transform` attributes and the `viewBox` are
applied, hidden elements (`display:none`) and `<defs>` are skipped.

The Y axis is flipped, because SVG grows downwards and a machine grows upwards.

Only the *outlines* are used: a filled shape is drawn as its outline, since a ball in sand has no
way to fill an area.

### Fitting

| Option | Meaning |
| --- | --- |
| `margin` | Border left empty on every side, in machine units |
| `keep_aspect` | `false` stretches the drawing to fill the table exactly |

### Simplification

`simplify_tolerance` drops the points that sit closer than the given distance to the line they
belong to (Ramer-Douglas-Peucker). It is applied **after** the drawing has been fitted, so the
value is in machine units: `1` means "nothing is off by more than 1 mm", whatever the scale of the
source file.

This matters more than it looks: a curve exported by Inkscape can easily carry thousands of points,
which the board has to chew through one `G1` at a time. `flatness` controls the opposite end, how
finely the curves are flattened in the first place.

### Ordering, start and end points

A sand table has no pen to lift. The ball ploughs a furrow wherever it goes, so the move between
two strokes is drawn exactly like the strokes themselves.

Everything else follows from that:

* the strokes are reordered so the connecting moves are as short as possible (`optimize_order`),
* a stroke is drawn backwards when its other end is closer (`allow_reverse`),
* `start_x`/`start_y` and `end_x`/`end_y` pin where the ball begins and finishes.

The start and end points are what makes it possible to chain drawings: end one where the next one
starts and the table never has to drag a scar across the finished picture.

## Layers

`layers` splits the drawing into several drawings, played one after the other.

Because the sand keeps what was drawn before, a layer only carries its own new strokes: playing
them in order builds the picture up progressively instead of drawing it all at once. Put them in a
playlist with a `timing` element in between and the picture grows over the day.

Two properties are guaranteed:

* **continuity** — a layer ends exactly where the next one starts, so the ball never jumps between
  layers. A single long stroke (a spiral, say) is cut in place and the cut point belongs to both
  halves.
* **no repetition** — nothing is drawn twice.

`layer_mode` chooses how to split:

* `length` (default) — layers of equal traced length. Works on any drawing, including one made of a
  single path.
* `document` — follows the layers of the SVG file itself (the `<g inkscape:groupmode="layer">`
  groups that Inkscape writes). Use it when you want to decide yourself what appears first. Falls
  back to `length` when the file has no layers.

## The API

The dropzone covers the simple case. For everything else there are two routes.

### `POST /api/svg/preview`

Converts and returns the result **without saving anything**, so an interface can show what the
options do before committing.

Send either a `file` field (multipart) or the document itself in an `svg` field, plus any of the
options below. The answer holds one entry per layer:

```json
{
  "success": true,
  "table": {"width": 1100, "height": 540},
  "layers": [
    [{"travel": false, "points": [[280, 0], [820, 0]]},
     {"travel": true,  "points": [[820, 0], [820, 540]]}]
  ],
  "start_point": [280, 0],
  "end_point": [280, 540],
  "path_length": 2160.0,
  "points_before": 1240,
  "points_after": 96
}
```

`travel` marks the moves between two strokes. They are drawn like everything else, but showing them
differently makes it obvious where the drawing has to jump.

`points_before`/`points_after` show what the simplification actually saved.

### `POST /api/svg/upload`

Same input, but the result is stored: one drawing per layer. Returns the ids in the order they have
to be played.

```json
{"success": true, "ids": [12, 13, 14], "layers": 3, "path_length": 8400.0}
```

### Options

All optional, all accepted as strings (they come from a form).

| Field | Default | Meaning |
| --- | --- | --- |
| `table_width`, `table_height` | from the device settings | Usable area |
| `margin` | `0` | Border left empty on every side |
| `keep_aspect` | `true` | `false` stretches the drawing |
| `simplify_tolerance` | `0` | Maximum error allowed when dropping points, in machine units |
| `flatness` | `0.2` | How finely curves are flattened, in source units |
| `optimize_order` | `true` | Reorder the strokes to shorten the connecting moves |
| `allow_reverse` | `true` | Allow drawing a stroke backwards |
| `start_x`, `start_y` | none | Where the ball starts (both are needed) |
| `end_x`, `end_y` | none | Where the ball finishes (both are needed) |
| `layers` | `1` | Number of layers to split the drawing into (max 100) |
| `layer_mode` | `length` | `length` or `document` |
| `feedrate` | none | `F` value written on the first move |

### Drawing by hand

The `svg` field accepts a document as a plain string rather than a file, which is all that is needed
to convert something drawn in the browser: collect the strokes of a pointer on a canvas, write them
as `<path d="M ... L ...">` and post the result to the same routes. The conversion, the
simplification (very welcome on a shaky hand-drawn line) and the layering work the same way.

## Using it from python

```python
from server.preprocessing.svg_to_gcode import ConversionOptions, convert_file

options = ConversionOptions(table_width=1100, table_height=540,
                            margin=20, simplify_tolerance=1.0, layers=5)
result = convert_file("drawing.svg", options)

for index, layer in enumerate(result.layers):
    with open("layer_{}.gcode".format(index + 1), "w") as f:
        f.write("\n".join(layer))
```
