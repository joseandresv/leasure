from pathlib import Path

from PIL import Image, ImageChops

from scripts.make_icon import PNG_SIZE, write_icon_files

REPO_IMG = Path(__file__).resolve().parent.parent / "static" / "img"


def _thumb(path: Path, frame_size: int | None = None) -> Image.Image:
    img = Image.open(path)
    if frame_size:
        img.size = (frame_size, frame_size)
    return img.convert("RGB").resize((32, 32), Image.LANCZOS)


def _max_channel_diff(a: Image.Image, b: Image.Image) -> int:
    return max(ImageChops.difference(a, b).getextrema(), key=lambda pair: pair[1])[1]


def test_generator_writes_a_512px_png_and_a_multi_size_ico(tmp_path):
    png_path, ico_path = write_icon_files(tmp_path)

    png = Image.open(png_path)
    assert png.size == (PNG_SIZE, PNG_SIZE)

    sizes = Image.open(ico_path).info["sizes"]
    assert {(16, 16), (32, 32), (256, 256)} <= sizes


def test_committed_icon_matches_the_generator(tmp_path):
    """Regenerating must not change the icon — the .lnk and the favicon ship the files."""
    png_path, ico_path = write_icon_files(tmp_path)

    # Pillow's resampling is not byte-stable across versions, so compare pixels.
    assert _max_channel_diff(_thumb(png_path), _thumb(REPO_IMG / "leasure.png")) <= 8
    assert _max_channel_diff(_thumb(ico_path, 16), _thumb(REPO_IMG / "leasure.ico", 16)) <= 8
