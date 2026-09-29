#!/usr/bin/env python3
import argparse
from pathlib import Path
from PIL import Image

parser = argparse.ArgumentParser(description="Cut an evenly spaced horizontal transparent sticker collage into individual PNG files.")
parser.add_argument("input", type=Path)
parser.add_argument("output_dir", type=Path)
parser.add_argument("--count", type=int, required=True)
parser.add_argument("--padding", type=int, default=12)
parser.add_argument("--prefix", default="sticker")
args = parser.parse_args()

if args.count < 1:
    parser.error("--count must be at least 1")
if args.padding < 0:
    parser.error("--padding cannot be negative")

image = Image.open(args.input).convert("RGBA")
args.output_dir.mkdir(parents=True, exist_ok=True)
width, height = image.size
for index in range(args.count):
    left = round(index * width / args.count)
    right = round((index + 1) * width / args.count)
    panel = image.crop((left, 0, right, height))
    bbox = panel.getchannel("A").getbbox()
    if bbox is None:
        raise SystemExit(f"No visible sticker found in panel {index + 1}")
    x0 = max(0, bbox[0] - args.padding)
    y0 = max(0, bbox[1] - args.padding)
    x1 = min(panel.width, bbox[2] + args.padding)
    y1 = min(panel.height, bbox[3] + args.padding)
    panel.crop((x0, y0, x1, y1)).save(args.output_dir / f"{args.prefix}-{index + 1}.png")
