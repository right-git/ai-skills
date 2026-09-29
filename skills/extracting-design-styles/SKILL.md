---
name: extracting-design-styles
description: Use when selected screenshots or visual references in a video project must become a reusable style specification and an approved visual reference.
---

# Extracting Design Styles

Turn selected screenshots into reusable visual rules. Describe the visual
language, not the source's copy, branding, content, or page layout.

First-version boundary: do not create a general icon library, copy source
assets, or introduce machine-readable design-token files.

## Outputs

Use the user's name when supplied; otherwise derive a concise descriptive name
from the references. Normalize either to safe lowercase kebab-case before
using it in a path. Reject empty names, `.`, `..`, path separators, and any
value that could escape `assets/styles/`. If normalization materially changes
the supplied name, tell the user; preserve the display title separately in
`DESIGN.md` when useful.

```text
assets/styles/<style-name>/DESIGN.md
assets/styles/<style-name>/preview.png
```

Inspect `preview.png` first when reusing an existing style. Read `DESIGN.md`
when exact values, font paths, or adaptation rules are needed.

## Analyze the references

Inspect only screenshots the user selected. For multiple screenshots, extract
shared rules first and label genuine variants. Do not turn one-off content or
layout into a universal rule.

If the style directory already exists, update it instead of creating a
duplicate. Preserve unrelated user content and surface conflicts between new
references and approved rules instead of silently replacing them.

Classify every conclusion:

- **Observed:** visible, measurable, or source-supported.
- **Estimated:** visually approximated.
- **Approved:** confirmed by the user in Storybook.

Never claim an exact typeface from appearance alone. Still images do not prove
motion timing or easing.

## Write `DESIGN.md`

Use every heading below:

```markdown
# <Style Name>
## Visual Identity
## Evidence
## Color System
## Typography
## Shape and Geometry
## Spacing and Density
## Surfaces and Depth
## Reusable Component Treatments
## Iconography and Imagery
## Motion and Editing Language
## Composition Principles
## Do / Avoid
## Open Questions
```

Keep each section implementation-oriented:

- **Visual Identity:** character, era, mood, and distinguishing traits.
- **Evidence:** selected screenshot filenames, status vocabulary, and material
  confidence limitations.
- **Color System:** semantic token table with value, usage, and status.
- **Typography:** role, family, weight, style, size/scale, line-height,
  tracking, casing, fallback, local file path, source, license, and status.
- **Shape and Geometry:** corners, borders, bevels, pixel treatment, and
  control shapes.
- **Spacing and Density:** base unit, gaps, padding ranges, alignment, and
  density.
- **Surfaces and Depth:** panels, overlays, separators, highlights, textures,
  and shadows.
- **Reusable Component Treatments:** general recipes for buttons, labels,
  cards, badges, counters, and windows; never the exact source composition.
- **Iconography and Imagery:** stroke, fill, pixelation, crop, texture, and
  illustration treatments; no icon-fetch feature.
- **Motion and Editing Language:** transitions, entrances, exits, rhythm,
  duration, easing, and camera only when evidenced or approved; still-only
  unsupported rules remain unspecified.
- **Composition Principles:** hierarchy, scale contrast, alignment,
  whitespace, repetition, and aspect-ratio adaptation; not source layout.
- **Do / Avoid:** concrete choices that preserve or break the visual style.
- **Open Questions:** unresolved decisions only; remove them after resolution.

## Fonts

Search `assets/fonts/` first. Shared fonts belong in
`assets/fonts/<Font Family>/`, never inside a style folder. Preserve existing
font directories and filenames; new directories use the canonical family name
and must not require renaming existing ones. If the exact font is known and
openly licensed, fetch only required files from Google Fonts or the creator's
official repository and keep its license. Use local files in both Storybook and
production; do not rely on remote font URLs at render time.

If identity is uncertain, propose a close open-source substitute as
**Estimated**. For commercial or restricted fonts, ask the user for licensed
files or approval to substitute. Do not use unofficial font-copy sites.

## Review in Storybook

**REQUIRED SUB-SKILL:** Use `storybook` in style-reference mode.

Create or update `Styles/<style-name>/Reference` from `DESIGN.md`. Share the
exact live review URL and revise the story and document together. Silence is
not approval.

After explicit approval, capture only the approved 1920 x 1080 story canvas,
without Storybook chrome. Write to a temporary sibling, verify its dimensions,
then atomically replace `assets/styles/<style-name>/preview.png`. Keep an
existing approved preview unchanged while revisions are pending.

## Artifact naming and verification

- Always name the specification `DESIGN.md` in uppercase.
- The rendered style image belongs beside it as `preview.png` under `assets/styles/<style-name>/`. A review capture left in the Storybook project is not a completed style artifact.
- Before reporting either artifact as present, verify its exact destination path, nonzero size, and (for `preview.png`) 1920 x 1080 dimensions. Do not confuse the generated style reference with the user's source screenshot; identify each explicitly.

## Completion checks

Confirm all required sections are complete, statuses are explicit, local font
paths resolve, the permanent story renders, the shared URL is reachable, and
`DESIGN.md`, the story, and `preview.png` agree. If Storybook is unavailable or
unreachable, report the exact blocker and stop without creating a substitute
preview.