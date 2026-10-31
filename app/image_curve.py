"""Extract a relative chart shape from an already-saved phone image.

No device input, screenshot capture, inferred currency amounts or exact daily
prices. The 0–100 vertical coordinate describes position within that image's
visible curve range. It is deliberately separate from verified price points.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from statistics import median

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
VERSION = "pixel-curve-v1"


def extract_curve(image, *, signal_kind="price_curve", columns_per_point=10):
    if signal_kind not in {"price_curve", "popularity_only"}:
        raise ValueError("unsupported signal kind")
    if type(columns_per_point) is not int or not 1 <= columns_per_point <= 40:
        raise ValueError("columns_per_point must be 1–40")
    image = image.convert("RGB")
    width, height = image.size
    if width < 300 or height < 600 or not .35 < width / height < .65:
        raise ValueError("unsupported screenshot geometry")
    # Explicitly reviewed layout v1; colour trace is confined to the chart.
    left, right = round(width * .0424), round(width * .959)
    top, bottom = round(height * .427), round(height * .544)
    end = right - max(10, round(width * .021))  # omit selection ring/crosshair
    pixels = image.load()
    raw, gap, longest_gap = [], 0, 0
    for x in range(left, end + 1):
        hits = []
        for y in range(top, bottom + 1):
            red, green, blue = pixels[x, y]
            visible = ((blue > 145 and green > 100 and blue-red > 40 and green-red > 25)
                       if signal_kind == "price_curve" else
                       (red > 160 and red-green > 45 and red-blue > 25))
            if visible:
                hits.append(y)
        if hits:
            raw.append((x, float(median(hits))))
            gap = 0
        else:
            gap += 1
            longest_gap = max(longest_gap, gap)
    sampled = end-left+1
    quality = dict(sampled_columns=sampled, valid_columns=len(raw),
                   column_coverage=round(len(raw)/sampled, 6), trace_gap_max_px=longest_gap)
    if len(raw) < 12 or len(raw)/sampled < .6:
        raise ValueError("insufficient visible chart trace; no synthetic curve")
    low, high = min(y for _, y in raw), max(y for _, y in raw)
    points = []
    for start in range(left, end+1, columns_per_point):
        bucket = [(x, y) for x, y in raw if start <= x < start+columns_per_point]
        if not bucket:
            continue
        x, y = median(x for x, _ in bucket), median(y for _, y in bucket)
        level = 50.0 if high-low < 3 else 100*(high-y)/(high-low)
        points.append(dict(x=round((x-left)/(right-left), 6), level=round(level, 4)))
    return dict(points=points, quality=quality,
                trace=dict(roi=[left, top, right, bottom], traced_until_x=end,
                           min_pixel_y=low, max_pixel_y=high, width=width, height=height,
                           vertical_unit="relative_curve_position", price_calibrated=False))


def axis_from_ocr(filename):
    command = [str(ROOT / "output/bin/mobile-ocr"), str(filename)]
    result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=25)
    lines = json.loads(result.stdout).get("lines", [])
    candidates = [line for line in lines if re.fullmatch(r"\d{2}[./]\d{2}", str(line.get("text", "")).strip())
                  and .50 < line.get("y", 0) < .65]
    groups = []
    for line in candidates:
        group = next((group for group in groups if abs(group[0]["y"]-line["y"]) < .015), None)
        if group is None:
            group = []
            groups.append(group)
        group.append(line)
    ticks = max(groups, key=len, default=[])
    if len(ticks) < 3:
        raise ValueError("unverified chart date axis")
    return sorted([dict(x=round(max(0, min(1, (line["x"]+line.get("width", 0)/2-.0424)/(.959-.0424))), 6),
                        label=line["text"].strip().replace("/", ".")) for line in ticks], key=lambda tick: tick["x"])


def make_payload(frame, evidence_dir, *, axis_labels, signal_kind="price_curve", notes=""):
    filename = frame["evidence_filename"]
    if Path(filename).name != filename:
        raise ValueError("evidence filename must be a basename")
    source = Path(evidence_dir) / filename
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if digest != frame["evidence_hash"]:
        raise ValueError("saved source image hash changed")
    with Image.open(source) as image:
        extracted = extract_curve(image, signal_kind=signal_kind)
    recognized = frame.get("recognition") or {}
    return dict(frame_id=frame["id"], source_hash=digest, extractor_version=VERSION,
                signal_kind=signal_kind, quality=extracted["quality"], points=extracted["points"],
                axis_labels=axis_labels, header_price=recognized.get("proposed_price") if signal_kind == "price_curve" else None,
                raw_metric_label="近7日成交均价" if signal_kind == "price_curve" else "搜索热度",
                notes=notes or "已存原图曲线的相对位置估算；无纵轴价格刻度，不推算日价或价格涨幅。")


def overlay(source, output, payload):
    with Image.open(source) as image:
        image = image.convert("RGB")
        draw = ImageDraw.Draw(image)
        extracted = extract_curve(image, signal_kind=payload["signal_kind"])
        trace, width, height = extracted["trace"], image.width, image.height
        left, top, right, bottom = trace["roi"]
        draw.rectangle((left, top, right, bottom), outline="#9141b5", width=2)
        for point in payload["points"]:
            x = left+point["x"]*(right-left)
            y = trace["max_pixel_y"]-point["level"]*(trace["max_pixel_y"]-trace["min_pixel_y"])/100
            draw.ellipse((x-2, y-2, x+2, y+2), fill="#b329a5")
        image.save(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="Saved /api/mobile/captures/frames JSON")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, default=ROOT / "data/market_evidence")
    args = parser.parse_args()
    frames = json.loads(args.manifest.read_text())["frames"]
    # Latest overview within each frozen target. Tooltip frames are not needed
    # to estimate the continuous visual trend and remain separate evidence.
    latest = {}
    for frame in frames:
        if frame.get("chart_x") is None:
            latest[frame["catalog_id"]] = frame
    payloads = []
    for frame in latest.values():
        signal = "popularity_only" if "搜索热度" in (frame.get("recognition") or {}).get("raw_label", "") else "price_curve"
        source = args.evidence_dir / frame["evidence_filename"]
        payloads.append(make_payload(frame, args.evidence_dir, axis_labels=axis_from_ocr(source), signal_kind=signal,
                                     notes=(frame.get("recognition") or {}).get("scope_text", "")+
                                     "；相对曲线位置估算，不是价格百分比或精确逐日价格。"))
    args.output.write_text(json.dumps(dict(extractor=VERSION, snapshots=payloads), ensure_ascii=False, indent=2))
    print(json.dumps(dict(output=str(args.output), snapshots=len(payloads), points=[len(p["points"]) for p in payloads])))


if __name__ == "__main__":
    main()
