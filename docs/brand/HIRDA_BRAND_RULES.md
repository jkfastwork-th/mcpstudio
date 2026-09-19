# HIRDA Brand Reference Lock v1

Status: **APPROVED / SOURCE OF TRUTH**

The approved HIRDA brand board from the 2026-09-19 design review is the human visual source of truth.

Machine-readable lock:

- `docs/brand/HIRDA_BRAND_REFERENCE_LOCK.json`
- source filename: `ChatGPT Image Sep 19, 2026, 03_18_05 PM.png`
- source dimensions: `1536 × 1024`
- source SHA-256: `0c3093bc30a03381e4f7c1d02dd1e4ba02c56e7973b25f7d930353d9e71eb9bf`

The SHA identifies the exact approved board. Do not substitute another generated board without an explicit brand review.

## Approved identity

- Name: **HIRDA**
- Tagline: **MANY AGENTS. ONE SYSTEM.**
- Mark: three-headed faceted/origami dragon.
- Sidebar: full monochrome lockup from the approved board, including dragon, HIRDA name and tagline.

## Core palette

- Primary: `#3569F4`
- Secondary: `#665AF7`
- Accent: `#7EA2FF`
- Dark background: `#0C1525`
- Light background: `#F4F7FB`
- Success: `#10A37F`
- Warning: `#D97706`
- Danger: `#DC2626`

## Typography

- UI / Thai: **Kanit**
- Technical values / code: **JetBrains Mono**

## Sidebar production assets

Use the image-derived lockups already approved for production:

- `static/brand/hirda-sidebar-lockup-light.webp` — transparent, dark artwork for light sidebar
- `static/brand/hirda-sidebar-lockup-dark.webp` — transparent, white artwork for dark sidebar

These WebP files are web-optimized copies of the approved clean lockups. They preserve the approved artwork; they are not redrawn, traced, or reconstructed.

Rules:

1. Use these images directly. Do not redraw, trace, simplify or reconstruct the dragon.
2. Do not split the wordmark or tagline out of the lockup.
3. Preserve aspect ratio with `object-fit: contain`.
4. Do not add a frame, replacement card, or glow frame around the artwork.
5. Light theme uses the light lockup on the light sidebar surface.
6. Dark theme uses the dark lockup on the dark sidebar surface.

## Theme reference behavior

The board contains explicit dark- and light-mode UI examples. A theme is considered conformant only when all of these follow the corresponding board variant:

- sidebar surface and lockup;
- active navigation treatment;
- header and page surface hierarchy;
- card border/radius hierarchy;
- runtime status badges;
- HIRDA palette;
- typography roles.

Provider colors (Claude/Codex/Hermes) are secondary accents. They never replace the HIRDA identity.

## Prohibited drift

Do not:

- generate a replacement production logo;
- create a new polygon/SVG interpretation for the live sidebar;
- silently change HIRDA core palette values;
- label provider accent schemes as the primary HIRDA brand;
- reuse third-party visual language as HIRDA's identity.

## Verification

`tests/test_brand_reference_lock.py` validates the machine lock against the live HTML/CSS.

Any intentional brand revision must update, in the same reviewed change:

1. the approved board;
2. `HIRDA_BRAND_REFERENCE_LOCK.json`;
3. this document;
4. affected production assets;
5. regression tests.
