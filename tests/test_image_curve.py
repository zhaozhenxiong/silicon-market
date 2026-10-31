import hashlib

import pytest
from PIL import Image, ImageDraw

from app.image_curve import extract_curve, make_payload


def chart(color="#25abe5", *, gap=False):
    image = Image.new("RGB", (708, 1562), "white")
    draw = ImageDraw.Draw(image)
    if gap:
        draw.line([(30, 800), (190, 770)], fill=color, width=3)
        draw.line([(510, 710), (680, 680)], fill=color, width=3)
    else:
        draw.line([(30, 800), (680, 680)], fill=color, width=3)
    # Unrelated coloured controls outside the reviewed plot must not enter it.
    draw.rectangle((40, 950, 670, 1000), fill="#25abe5")
    return image


def test_extracts_relative_direction_not_fake_prices_or_dates():
    result = extract_curve(chart())
    points = result["points"]
    assert len(points) >= 50 and points[0]["level"] < points[-1]["level"]
    assert result["quality"]["column_coverage"] > .95
    assert result["trace"]["price_calibrated"] is False
    assert all(set(point) == {"x", "level"} for point in points)
    assert points[-1]["x"]-points[0]["x"] > .9


def test_popularity_trace_is_not_a_price_trace_and_missing_trace_is_not_filled():
    with pytest.raises(ValueError, match="visible chart"):
        extract_curve(chart("#ef4b3c"))
    assert extract_curve(chart("#ef4b3c"), signal_kind="popularity_only")["points"]
    with pytest.raises(ValueError, match="visible chart"):
        extract_curve(chart(gap=True))


def test_selection_ring_and_vertical_crosshair_do_not_change_sampled_curve():
    plain = chart()
    marked = plain.copy()
    draw = ImageDraw.Draw(marked)
    draw.line((679, 670, 679, 850), fill="#25abe5", width=2)
    draw.ellipse((668, 670, 690, 692), outline="#25abe5", width=3)
    assert extract_curve(plain)["points"] == extract_curve(marked)["points"]


def test_flat_observed_trace_is_neutral_but_empty_image_is_missing():
    flat = Image.new("RGB", (708, 1562), "white")
    ImageDraw.Draw(flat).line((30, 750, 680, 750), fill="#25abe5", width=3)
    assert all(p["level"] == 50 for p in extract_curve(flat)["points"])
    with pytest.raises(ValueError):
        extract_curve(Image.new("RGB", (708, 1562), "white"))


def test_source_hash_and_path_cannot_be_replaced_by_unrelated_file(tmp_path):
    image = tmp_path / "source.png"
    chart().save(image)
    frame = dict(id=5, evidence_filename="source.png", evidence_hash=hashlib.sha256(image.read_bytes()).hexdigest(), recognition={"proposed_price":123})
    payload = make_payload(frame, tmp_path, axis_labels=[])
    assert payload["header_price"] == 123 and payload["source_hash"] == frame["evidence_hash"]
    image.write_bytes(b"replacement")
    with pytest.raises(ValueError, match="hash"):
        make_payload(frame, tmp_path, axis_labels=[])
    with pytest.raises(ValueError, match="basename"):
        make_payload({**frame,"evidence_filename":"../source.png"}, tmp_path, axis_labels=[])
