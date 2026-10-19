"""partials/library_albums.html, the Library panel's Albums view and the track rows' album links."""
import html as html_lib
import json
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)

KEY = "a-0123456789abcdef0123"
HOT_FUSS = {"key": KEY, "kind": "album", "name": "Hot Fuss", "artist": "The Killers", "year": 2004,
            "track_count": 9, "total_tracks": 11, "cover_url": f"/api/albums/{KEY}/cover?t=7",
            "quality_mix": [{"label": "FLAC", "count": 8}, {"label": "AAC 256", "count": 1}],
            "all_lossless": False, "info_source": "musicbrainz", "downloaded_at": "2026-10-01T10:00:00"}
LOOSE = {"key": "loose", "kind": "loose", "name": "Loose songs", "artist": "", "year": None, "track_count": 45,
         "total_tracks": None, "cover_url": None, "quality_mix": [], "all_lossless": False, "info_source": None,
         "downloaded_at": None}


def render(**context) -> str:
    base = {"albums": [HOT_FUSS, LOOSE], "q": "", "sort": "artist", "total": 2, "offset": 0, "limit": 120,
            "next_offset": None}
    return env.get_template("partials/library_albums.html").render(**{**base, **context})


def _cards(markup: str) -> list[str]:
    return re.findall(r'<button type="button" class="lib-album-card.*?</button>', markup, re.S)


def test_card_opens_the_album_in_the_library_modal():
    card = _cards(render())[0]

    assert f'hx-get="/api/albums/{KEY}/html"' in card and 'hx-target="#library-detail"' in card
    assert 'data-opens-detail="Loading album…"' in card


def test_card_shows_the_lazy_cover_counts_and_quality_mix():
    card = _cards(render())[0]

    assert f'src="/api/albums/{KEY}/cover?t=7"' in card and 'loading="lazy"' in card
    assert "9/11 songs" in card and ">FLAC<" in card and ">AAC 256<" in card
    assert 'title="8 FLAC, 1 AAC 256"' in card


def test_card_carries_its_key_for_the_cover_refresh():
    card = _cards(render())[0]

    assert f'data-album-key="{KEY}"' in card
    assert re.search(r'<span class="lib-album-cover">\s*<img src=', card)


def test_settled_album_info_reloads_that_cards_cover():
    js = (TEMPLATES.parent / "static" / "js" / "leasure.js").read_text(encoding="utf-8")

    hook = re.search(r"document\.addEventListener\('album-info:ready'.*?\n\}\);", js, re.S).group(0)
    assert "event.detail && event.detail.key" in hook
    assert ".lib-album-card[data-album-key=\"' + CSS.escape(key) + '\"] .lib-album-cover img" in hook
    assert "img.setAttribute('data-cover-fallback', '')" in hook
    assert "'/api/albums/' + encodeURIComponent(key) + '/cover?t=' + Date.now()" in hook


def test_all_lossless_album_is_marked():
    card = _cards(render(albums=[{**HOT_FUSS, "all_lossless": True}]))[0]

    assert "is-lossless" in card


def test_loose_card_uses_the_placeholder():
    card = _cards(render())[1]

    assert 'hx-get="/api/albums/loose/html"' in card and "is-loose" in card
    assert 'src="/static/img/placeholder.svg"' in card and "data-cover-fallback" not in card
    assert "45 songs" in card


def test_first_page_brings_the_grid_and_the_total():
    markup = render()

    assert '<div class="lib-album-grid">' in markup and "2 albums" in markup


def test_later_pages_bring_only_cards():
    markup = render(offset=120, next_offset=None)

    assert "lib-album-grid" not in markup and "lib-albums-total" not in markup and len(_cards(markup)) == 2


def test_load_more_keeps_search_and_sort():
    markup = render(total=300, next_offset=120)

    button = re.search(r'<button type="button" class="outline load-more-btn[^>]*>', markup).group(0)
    assert json.loads(html_lib.unescape(re.search(r"hx-vals='([^']*)'", button).group(1))) == {
        "offset": 120, "limit": 120}
    assert 'hx-include="#library-albums-q, #library-albums-sort"' in button and 'hx-target="this"' in button


def test_no_load_more_on_the_last_page():
    assert "load-more-btn" not in render()


def test_empty_search_says_nothing_matches():
    assert "No albums match “zzz”." in render(albums=[], total=0, q="zzz")


def test_empty_library_invites_a_download():
    assert "No albums yet." in render(albums=[], total=0)


def test_library_panel_has_the_albums_view_and_its_modal():
    html = (TEMPLATES / "panels" / "library.html").read_text(encoding="utf-8")

    grid = re.search(r'<div id="library-albums"[^>]*>', html).group(0)
    assert 'hx-get="/api/albums/html"' in grid and 'hx-trigger="intersect once"' in grid
    assert 'hx-include="#library-albums-q, #library-albums-sort"' in grid
    assert 'id="library-albums-q" name="q"' in html and 'id="library-albums-sort" name="sort"' in html
    assert re.search(r'<dialog id="library-detail-modal"[^>]*>.*?<div id="library-detail" class="modal-body">',
                     html, re.S)


def _track_row(**extra) -> str:
    row = {"id": 9, "title": "Mr. Brightside", "artist": "The Killers", "album": "Hot Fuss", "track_number": 2,
           "synced_at": None, "quality": "aac_256", "format": "m4a", **extra}
    return env.get_template("partials/library_tracks.html").render(tracks=[row])


def test_track_row_album_opens_the_album_view():
    markup = _track_row(album_key=KEY)

    assert f'hx-get="/api/albums/{KEY}/html"' in markup and 'hx-target="#library-detail"' in markup
    assert ">Hot Fuss</button>" in markup


def test_album_less_track_row_opens_the_loose_songs():
    markup = _track_row(album=None, album_key="loose")

    assert 'hx-get="/api/albums/loose/html"' in markup and ">Loose song</button>" in markup


def test_track_row_without_a_key_shows_plain_text():
    markup = _track_row()

    assert "/api/albums/" not in markup and "Hot Fuss" in markup
