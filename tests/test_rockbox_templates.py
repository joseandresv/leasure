"""Device-page partials on Rockbox and stock cards, rendered with the device router's templates."""
import re

from routers.device import templates
from services.device import ROCKBOX_DB_REMINDER

FS_WARNING = "This card is formatted exFAT. Rockbox reads FAT32 cards only; use a FAT32 card for the Rockbox trial."


def _render(name: str, **context) -> str:
    return templates.env.get_template(f"partials/{name}").render(**context)


def _diff(**context) -> str:
    base = {"to_add": [], "already_synced": [], "on_device_only": [], "to_remove": [], "kept": [], "space": None}
    return _render("sync_diff.html", **{**base, **context})


def _rockbox_diff(**context) -> str:
    return _diff(**{"profile": "rockbox", "playlist_dir": "/Playlists", "db_reminder": ROCKBOX_DB_REMINDER,
                    **context})


def _drive(**extra) -> dict:
    return {"path": "/mnt/f", "label": "F:", "free_gb": 10, "total_gb": 32, "device_type": "removable",
            "has_music_files": True, **extra}


def _last_element(html: str) -> str:
    return re.findall(r"<(?:p|div|details)\b[^>]*>", html)[-1]


def test_device_list_labels_a_rockbox_card():
    html = _render("device_list.html", devices=[_drive(profile="rockbox")])
    assert '<span class="card-profile card-profile-rockbox">Rockbox card</span>' in html
    assert "Stock H2" not in html


def test_device_list_treats_a_drive_without_profile_as_stock():
    html = _render("device_list.html", devices=[_drive()])
    assert '<span class="card-profile card-profile-stock">Stock H2</span>' in html


def test_device_list_gives_no_card_label_to_the_system_drive():
    html = _render("device_list.html", devices=[_drive(device_type="system", profile="stock")])
    assert "card-profile" not in html


def test_rockbox_preview_names_the_card_and_what_changes():
    html = _rockbox_diff()
    assert "Card: <strong>Rockbox card</strong>" in html
    assert "Playlists go to /Playlists; each album folder gets a cover.jpg; /.rockbox is never touched." in html


def test_rockbox_preview_ends_with_the_database_reminder():
    html = _rockbox_diff(to_add=[{"title": "A", "artist": "B", "album": "C", "format": "m4a"}])
    assert _last_element(html) == '<p class="sync-reminder">'
    assert html.rstrip().endswith(f"{ROCKBOX_DB_REMINDER}</p>")


def test_rockbox_preview_shows_the_filesystem_warning_as_an_alert():
    html = _rockbox_diff(fs_warning=FS_WARNING)
    assert f'<p class="sync-warning" role="alert">{FS_WARNING}</p>' in html


def test_stock_preview_has_no_rockbox_notes():
    html = _diff(profile="stock", playlist_dir="/", fs_warning=None, db_reminder=None)
    assert "Card: <strong>Stock H2</strong>" in html
    assert "sync-reminder" not in html and "cover.jpg" not in html and "role=\"alert\"" not in html


def test_preview_without_profile_keys_still_renders():
    html = _diff(to_add=[{"title": "A", "artist": "B", "album": None, "format": "mp3"}])
    assert "Card:" not in html and "sync-reminder" not in html
    assert "<strong>1</strong> to add" in html


def test_preview_escapes_the_warning_and_reminder():
    html = _rockbox_diff(fs_warning="<b>x</b>", db_reminder="<script>y</script>")
    assert "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html
    assert "<script>y" not in html and "&lt;script&gt;y&lt;/script&gt;" in html


def test_sync_result_ends_with_the_reminder():
    html = _render("sync_result.html", synced=3, total_size_mb=12, playlists_generated=1,
                   errors=["boom"], reminder=ROCKBOX_DB_REMINDER)
    body = html[: html.rindex("</div>")]
    assert _last_element(body) == '<p class="sync-reminder">'
    assert body.rstrip().endswith(f"{ROCKBOX_DB_REMINDER}</p>")


def test_sync_result_on_stock_has_no_reminder():
    html = _render("sync_result.html", synced=3, total_size_mb=12, playlists_generated=0, errors=[], reminder=None)
    assert "sync-reminder" not in html


def test_device_files_lists_the_entries_left_alone():
    html = _render("device_files.html", artists={}, total_tracks=0, playlists=["Mix", "Playlists/Road"],
                   protected=[".rockbox", "System Volume Information"])
    assert '<p class="card-protected">Left alone: .rockbox, System Volume Information</p>' in html
    assert 'title="Playlists/Road">Playlists/Road</span>' in html


def test_device_files_without_protected_entries_shows_no_line():
    html = _render("device_files.html", artists={}, total_tracks=0, playlists=[])
    assert "Left alone" not in html
