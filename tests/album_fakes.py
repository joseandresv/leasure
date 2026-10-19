"""Library rows and MusicBrainz / Deezer payloads for the album-info tests (no network)."""

from models import Track

HOT_FUSS = ["Jenny Was a Friend of Mine", "Mr. Brightside", "Smile Like You Mean It", "Somebody Told Me",
            "All These Things That I've Done", "Andy, You're a Star", "On Top", "Change Your Mind",
            "Believe Me Natalie", "Midnight Show", "Everything Will Be Alright"]
MBID = "11111111-1111-1111-1111-111111111111"
MBID_2 = "22222222-2222-2222-2222-222222222222"
MBID_3 = "33333333-3333-3333-3333-333333333333"


def duration(i: int) -> int:
    return 200_000 + i * 7_000


async def seed(db, **fields) -> int:
    values = {"title": "Mr. Brightside", "artist": "The Killers", "album": "Hot Fuss", "album_artist": "The Killers",
              "status": "done", "quality": "aac_256", "format": "m4a", **fields}
    async with db() as session:
        track = Track(**values)
        session.add(track)
        await session.commit()
        return track.id


async def seed_album(db, titles=HOT_FUSS, numbers=None, **common) -> list[int]:
    """One done row per title, numbered 1..n (or `numbers`), with the durations the fake releases carry."""
    ids = []
    for i, title in enumerate(titles):
        number = numbers[i] if numbers else i + 1
        ids.append(await seed(db, title=title, track_number=number, duration_ms=duration(number - 1),
                              file_path=None, **common))
    return ids


def mb_release(mbid=MBID, title="Hot Fuss", artist="The Killers", discs=None, fmt="Digital Media", rg_id="rg-hot",
               primary="Album", secondary=(), front=True, date="2004-06-07"):
    discs = discs or [[(t, duration(i), None) for i, t in enumerate(HOT_FUSS)]]
    return {
        "id": mbid, "title": title, "status": "Official", "date": date, "country": "XW", "barcode": "602498617373",
        "artist-credit": [{"name": artist, "artist": {"name": artist}}],
        "label-info": [{"catalog-number": "B0002468-02", "label": {"name": "Island"}}],
        "release-group": {"id": rg_id, "primary-type": primary, "secondary-types": list(secondary)},
        "genres": [{"name": "alternative rock", "count": 4}, {"name": "indie rock", "count": 2}],
        "cover-art-archive": {"front": front},
        "media": [{"position": d + 1, "format": fmt, "track-count": len(tracks), "tracks": [
            {"position": p + 1, "number": str(p + 1), "title": t, "length": length,
             "recording": {"id": f"rec-{d}-{p}", "title": t, "length": length, "isrcs": [isrc] if isrc else []}}
            for p, (t, length, isrc) in enumerate(tracks)]} for d, tracks in enumerate(discs)],
    }


def mb_hit(mbid=MBID, title="Hot Fuss", artist="The Killers", track_count=11, score=100, rg_id="rg-hot",
           formats=("Digital Media",), status="Official", country="XW", date="2004-06-07", primary="Album",
           secondary=(), script="Latn"):
    return {"id": mbid, "score": score, "title": title, "status": status, "country": country, "date": date,
            "track-count": track_count, "artist-credit": [{"name": artist, "artist": {"name": artist}}],
            "release-group": {"id": rg_id, "primary-type": primary, "secondary-types": list(secondary)},
            "media": [{"format": f, "track-count": track_count} for f in formats],
            "text-representation": {"script": script}}


class FakeAPI:
    """Routes a get_checked(path, params) call to a canned answer or exception; records every call."""

    def __init__(self, routes: dict | None = None):
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, dict]] = []
        self.on_call = None

    async def __call__(self, path, params):
        self.calls.append((path, dict(params)))
        if self.on_call:
            self.on_call(path)
        answer = self.routes.get(path)
        if callable(answer):
            answer = answer(params)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]


def deezer_album(album_id=302127, title="Hot Fuss", artist="The Killers", nb_tracks=11, record_type="album"):
    return {"id": album_id, "title": title, "upc": "602498617373", "release_date": "2004-06-07", "label": "Island",
            "genres": {"data": [{"name": "Rock"}]}, "nb_tracks": nb_tracks, "duration": 2700,
            "cover_xl": "https://cdn-images.dzcdn.net/images/cover/abc/1000x1000-000000-80-0-0.jpg",
            "artist": {"name": artist}, "record_type": record_type, "explicit_lyrics": False}


def deezer_tracks(titles=HOT_FUSS, disk=1, start=0, isrc=True):
    return [{"id": 3000 + start + i, "title": t, "duration": duration(start + i) // 1000, "track_position": i + 1,
             "disk_number": disk, "isrc": f"USIR1040{start + i:04d}" if isrc else "",
             "artist": {"name": "The Killers"}} for i, t in enumerate(titles)]


def deezer_hit(album_id=302127, title="Hot Fuss", artist="The Killers", nb_tracks=11, record_type="album",
               explicit=False):
    return {"id": album_id, "title": title, "nb_tracks": nb_tracks, "record_type": record_type,
            "explicit_lyrics": explicit, "artist": {"name": artist}}
