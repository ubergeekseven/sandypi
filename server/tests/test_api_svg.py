"""Tests for the SVG upload and preview routes"""

import io
import json
import shutil
from pathlib import Path

import pytest

from server import app, db
from server.database.models import UploadedFiles
from server.utils import settings_utils


SQUARE = ('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'
          '<rect x="10" y="10" width="80" height="80"/></svg>')

STRIPES = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
           + "".join('<line x1="0" y1="{0}" x2="100" y2="{0}"/>'.format(i * 10) for i in range(10))
           + '</svg>')


@pytest.fixture(autouse=True)
def table_size(monkeypatch):
    """Pretends the table is the 1100x540 of a custom cartesian build"""
    settings = settings_utils.load_settings()
    settings["device"]["type"]["value"] = "Cartesian"
    settings["device"]["width"]["value"] = 1100
    settings["device"]["height"]["value"] = 540
    monkeypatch.setattr(settings_utils, "load_settings", lambda: settings)
    return (1100.0, 540.0)


@pytest.fixture
def created_drawings():
    """Removes the drawings created by a test, both the folder and the database row.

    The database is shared by the whole session, and a leftover drawing changes
    what the other tests see: the buttons test, for one, starts a random drawing
    as soon as the library is not empty.
    """
    created = []
    yield created
    for drawing_id in created:
        shutil.rmtree(Path(app.config["UPLOAD_FOLDER"]) / str(drawing_id), ignore_errors=True)
    if created:
        db.session.query(UploadedFiles).filter(UploadedFiles.id.in_(created)).delete(
            synchronize_session=False)
        db.session.commit()


def preview(client, **data):
    data.setdefault("svg", SQUARE)
    return client.post("/api/svg/preview", data=data)


def body(response):
    return json.loads(response.data)


# ---------------------------------------------------------------------------- preview

def test_preview_fits_the_table(client, table_size):
    result = body(preview(client))
    assert result["success"] is True
    assert (result["table"]["width"], result["table"]["height"]) == table_size
    points = [p for layer in result["layers"] for line in layer for p in line["points"]]
    assert all(0 <= x <= 1100 and 0 <= y <= 540 for x, y in points)


def test_preview_reports_the_conversion_numbers(client):
    result = body(preview(client))
    assert result["path_length"] == pytest.approx(4 * 540, abs=1)
    assert result["points_before"] > 0
    assert result["points_after"] > 0
    assert len(result["start_point"]) == 2


def test_preview_marks_the_travel_moves(client):
    result = body(preview(client, svg=STRIPES))
    for layer in result["layers"]:
        assert any(line["travel"] for line in layer)
        assert any(not line["travel"] for line in layer)
        # the polylines of a layer are a single continuous path: the ball never teleports
        for previous, following in zip(layer, layer[1:]):
            assert following["points"][0] == previous["points"][-1]


def test_preview_keeps_every_stroke(client):
    """A stroke reached through a travel must not be swallowed by it"""
    result = body(preview(client, svg=STRIPES))
    strokes = [line for layer in result["layers"] for line in layer if not line["travel"]]
    assert len(strokes) == 10


def test_preview_layers(client):
    result = body(preview(client, svg=STRIPES, layers=4))
    assert len(result["layers"]) == 4
    assert all(layer for layer in result["layers"])


def test_preview_honours_the_start_and_end_points(client):
    result = body(preview(client, start_x=0, start_y=0, end_x=1100, end_y=540))
    assert result["start_point"] == [0, 0]
    assert result["end_point"] == [1100, 540]


def test_preview_simplification_drops_points(client):
    circle = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle cx="50" cy="50" r="40"/></svg>'
    detailed = body(preview(client, svg=circle))
    coarse = body(preview(client, svg=circle, simplify_tolerance=10))
    assert coarse["points_after"] < detailed["points_after"]


def test_preview_accepts_an_uploaded_file(client):
    data = {"file": (io.BytesIO(SQUARE.encode()), "square.svg")}
    response = client.post("/api/svg/preview", data=data, content_type="multipart/form-data")
    assert body(response)["success"] is True
    assert body(response)["name"] == "square.svg"


def test_preview_rejects_a_broken_document(client):
    response = preview(client, svg="<svg><path d=")
    assert response.status_code == 400
    assert body(response)["success"] is False


def test_preview_rejects_an_empty_drawing(client):
    response = preview(client, svg='<svg xmlns="http://www.w3.org/2000/svg"/>')
    assert response.status_code == 400
    assert "no lines" in body(response)["error"]


def test_preview_without_a_drawing(client):
    response = client.post("/api/svg/preview", data={})
    assert response.status_code == 400


def test_preview_refuses_too_many_layers(client):
    response = preview(client, layers=1000)
    assert response.status_code == 400
    assert "layers" in body(response)["error"]


# ----------------------------------------------------------------------------- upload

def test_upload_creates_one_drawing(client, created_drawings):
    result = body(client.post("/api/svg/upload", data={"svg": SQUARE, "name": "square.svg"}))
    created_drawings.extend(result["ids"])
    assert result["success"] is True
    assert len(result["ids"]) == 1

    folder = Path(app.config["UPLOAD_FOLDER"]) / str(result["ids"][0])
    gcode = (folder / "{}.gcode".format(result["ids"][0])).read_text()
    assert "G1 X" in gcode


def test_upload_creates_one_drawing_per_layer(client, created_drawings):
    result = body(client.post("/api/svg/upload", data={"svg": STRIPES, "name": "stripes.svg", "layers": 3}))
    created_drawings.extend(result["ids"])
    assert result["layers"] == 3
    assert len(result["ids"]) == 3
    assert len(set(result["ids"])) == 3          # three distinct drawings


def test_uploaded_layers_are_continuous(client, created_drawings):
    """Playing the layers in order must not make the ball jump between them"""
    result = body(client.post("/api/svg/upload", data={"svg": STRIPES, "layers": 3}))
    created_drawings.extend(result["ids"])

    def last_and_first(drawing_id):
        folder = Path(app.config["UPLOAD_FOLDER"]) / str(drawing_id)
        lines = [l for l in (folder / "{}.gcode".format(drawing_id)).read_text().splitlines()
                 if l.startswith("G")]
        def point(line):
            parts = line.split()
            return (float(next(p[1:] for p in parts if p[0] == "X")),
                    float(next(p[1:] for p in parts if p[0] == "Y")))
        return point(lines[0]), point(lines[-1])

    points = [last_and_first(i) for i in result["ids"]]
    for (_, end), (start, _) in zip(points, points[1:]):
        assert end == start                      # the next layer starts exactly where this one stopped


def test_upload_rejects_a_broken_document(client):
    response = client.post("/api/svg/upload", data={"svg": "not an svg"})
    assert response.status_code == 400


# ---------------------------------------------------- the drawings dropzone accepts svg

def test_the_main_upload_route_converts_an_svg(client, created_drawings):
    data = {"file": (io.BytesIO(SQUARE.encode()), "square.svg")}
    response = client.post("/api/upload/", data=data, content_type="multipart/form-data")
    drawing_id = json.loads(response.data)
    created_drawings.append(drawing_id)
    assert drawing_id != -1

    folder = Path(app.config["UPLOAD_FOLDER"]) / str(drawing_id)
    assert (folder / "{}.gcode".format(drawing_id)).exists()
    gcode = (folder / "{}.gcode".format(drawing_id)).read_text()
    assert "G1 X" in gcode


def test_the_main_upload_route_rejects_a_broken_svg(client):
    data = {"file": (io.BytesIO(b"<svg><path d="), "broken.svg")}
    response = client.post("/api/upload/", data=data, content_type="multipart/form-data")
    assert json.loads(response.data) == -1


def test_the_main_upload_route_still_refuses_unknown_extensions(client):
    data = {"file": (io.BytesIO(b"whatever"), "notes.txt")}
    response = client.post("/api/upload/", data=data, content_type="multipart/form-data")
    assert json.loads(response.data) == -1
