---
name: remove-image-background
description: Use when one or more images, including green-screen images and sticker collages, need their backgrounds removed, saved as transparent PNGs, or split into individual transparent assets.
---

# Remove Image Background with rembg

Run rembg directly in an isolated `uv` environment. Do not create a background-removal helper script and do not modify the project environment.

## Remove a background

For one image:

```bash
uv run --no-project --python 3.12 --with 'rembg[cpu,cli]' rembg i INPUT_IMAGE OUTPUT.png
```

For every image in a directory, make a separate output directory and use rembg's folder command:

```bash
mkdir -p output
uv run --no-project --python 3.12 --with 'rembg[cpu,cli]' rembg p input output
```

Always keep source images unless the user explicitly asks to replace them. Use `.png` outputs so transparency can be represented. The first run may take longer while rembg downloads its model; subsequent runs reuse the cache.

Start with the default model. If a portrait has poor edges, retry with the portrait model:

```bash
uv run --no-project --python 3.12 --with 'rembg[cpu,cli]' rembg i -m birefnet-portrait INPUT_IMAGE OUTPUT.png
```

Inspect the resulting alpha channel and subject edges, especially around hair and areas affected by green spill.

## Split a sticker collage

After background removal, use the bundled cutter when the collage contains evenly spaced stickers in one horizontal row:

```bash
uv run --no-project --with pillow python .agents/skills/remove-image-background/scripts/cut_sticker_collage.py INPUT.png OUTPUT_DIR --count 5 --prefix sticker
```

The cutter divides the image into equal horizontal panels, finds each sticker from its alpha channel, adds optional transparent padding, and writes one PNG per sticker. Options:

- `--count`: required number of stickers.
- `--padding`: transparent pixels around the detected sticker; default `12`.
- `--prefix`: output filename prefix; default `sticker`.

Use this only for a single evenly spaced horizontal row. For irregular layouts, detect connected alpha components or crop manually instead.

## Apple Silicon

An M1 Pro does not need `onnxruntime-silicon`. The historical package is obsolete; official `onnxruntime` now provides macOS ARM64 support. The `rembg[cpu,cli]` command above installs the supported runtime in the temporary uv environment.
