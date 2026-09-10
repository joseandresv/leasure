"""Draw the Leasure app icon (Windows shortcut / browser favicon).

    python scripts/make_icon.py [output-dir]     # default: static/img

The motif is the H2 itself: an amber player body with a cyan screen and a dial
cut out of it, on the app's navy. Two colours plus the background so the shape
still reads at 16px in a taskbar.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

# static/css/theme.css: --bg-surface, --accent-amber, --accent-blue
NAVY = (12, 19, 34, 255)
AMBER = (255, 140, 0, 255)
CYAN = (56, 214, 255, 255)

PNG_SIZE = 512
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

# Geometry in a 512x512 frame; every size is a scaled copy of this drawing.
_BASE = 512
_SUPERSAMPLE = 4


def draw_icon(size: int = PNG_SIZE) -> Image.Image:
    """Render the icon at `size` px, antialiased by drawing large and downscaling."""
    scale = size * _SUPERSAMPLE / _BASE
    canvas = int(_BASE * scale)
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    def box(x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float, float]:
        return (x0 * scale, y0 * scale, x1 * scale, y1 * scale)

    draw.rounded_rectangle(box(0, 0, 512, 512), radius=112 * scale, fill=NAVY)
    draw.rounded_rectangle(box(126, 70, 386, 442), radius=46 * scale, fill=AMBER)
    draw.rounded_rectangle(box(158, 106, 354, 286), radius=20 * scale, fill=CYAN)
    draw.ellipse(box(210, 318, 302, 410), fill=NAVY)

    return img.resize((size, size), Image.LANCZOS)


def write_icon_files(out_dir: Path) -> tuple[Path, Path]:
    """Write leasure.png (512px) and multi-size leasure.ico; return both paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    png_path = out_dir / "leasure.png"
    ico_path = out_dir / "leasure.ico"

    master = draw_icon(PNG_SIZE)
    master.save(png_path, format="PNG")
    # Pillow downscales the master for each entry; drawing every size separately
    # keeps the small ones crisper.
    frames = [draw_icon(s) for s in ICO_SIZES]
    frames[-1].save(ico_path, format="ICO", sizes=[(s, s) for s in ICO_SIZES], append_images=frames[:-1])

    return png_path, ico_path


def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "static" / "img"
    for path in write_icon_files(out_dir):
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
