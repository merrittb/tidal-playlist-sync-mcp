import asyncio
from unittest.mock import MagicMock, call, patch

import pytest

from spotify_to_tidal.sync import (
    _extract_track,
    artist_match,
    duration_match,
    get_new_spotify_tracks,
    isrc_match,
    match,
    name_match,
    normalize,
    pick_tidal_playlist_for_spotify_playlist,
    populate_track_match_cache,
    simple,
    sync_playlist,
    test_album_similarity as album_similarity_passes,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_tidal_track(id=1, isrc="USRC12345678", duration=200, name="Test Track",
                     version=None, artists=None, available=True):
    track = MagicMock()
    track.id = id
    track.isrc = isrc
    track.duration = duration
    track.name = name
    track.version = version
    track.available = available
    if artists is None:
        artist = MagicMock()
        artist.name = "Test Artist"
        track.artists = [artist]
    else:
        track.artists = artists
    return track


def make_tidal_artist(name):
    a = MagicMock()
    a.name = name
    return a


def make_spotify_track(id="sp1", isrc="USRC12345678", duration_ms=200000,
                       name="Test Track", artist_names=None, album_name="Test Album",
                       track_number=1, type="track"):
    if artist_names is None:
        artist_names = ["Test Artist"]
    artists = [{"name": n} for n in artist_names]
    return {
        "id": id,
        "external_ids": {"isrc": isrc} if isrc else {},
        "duration_ms": duration_ms,
        "name": name,
        "artists": artists,
        "album": {
            "name": album_name,
            "artists": artists,
        },
        "track_number": track_number,
        "type": type,
    }


# ---------------------------------------------------------------------------
# _extract_track
# ---------------------------------------------------------------------------

def test_extract_track_from_track_key():
    item = {"track": {"name": "Song"}}
    assert _extract_track(item) == {"name": "Song"}


def test_extract_track_from_item_key():
    item = {"item": {"name": "Song"}}
    assert _extract_track(item) == {"name": "Song"}


def test_extract_track_nested_item_track():
    inner = {"track": {"name": "Nested"}, "extra": "data"}
    item = {"item": inner}
    assert _extract_track(item) == {"name": "Nested"}


def test_extract_track_returns_none_when_missing():
    assert _extract_track({}) is None


# ---------------------------------------------------------------------------
# normalize / simple
# ---------------------------------------------------------------------------

def test_normalize_strips_accents():
    assert normalize("café") == "cafe"


def test_normalize_plain_string_unchanged():
    assert normalize("hello") == "hello"


def test_simple_strips_hyphen_suffix():
    assert simple("Song - Remastered") == "Song"


def test_simple_strips_parenthetical():
    assert simple("Song (Live)") == "Song"


def test_simple_strips_bracket():
    assert simple("Song [Deluxe]") == "Song"


def test_simple_plain_string_unchanged():
    assert simple("Song") == "Song"


# ---------------------------------------------------------------------------
# isrc_match
# ---------------------------------------------------------------------------

def test_isrc_match_identical():
    tidal = make_tidal_track(isrc="USRC12345678")
    spotify = make_spotify_track(isrc="USRC12345678")
    assert isrc_match(tidal, spotify) is True


def test_isrc_match_different():
    tidal = make_tidal_track(isrc="USRC12345678")
    spotify = make_spotify_track(isrc="GBRC99999999")
    assert isrc_match(tidal, spotify) is False


def test_isrc_match_no_spotify_isrc():
    tidal = make_tidal_track(isrc="USRC12345678")
    spotify = make_spotify_track(isrc=None)
    assert isrc_match(tidal, spotify) is False


# ---------------------------------------------------------------------------
# duration_match
# ---------------------------------------------------------------------------

def test_duration_match_within_tolerance():
    tidal = make_tidal_track(duration=200)
    spotify = make_spotify_track(duration_ms=201000)  # 1 s off
    assert duration_match(tidal, spotify) is True


def test_duration_match_outside_tolerance():
    tidal = make_tidal_track(duration=200)
    spotify = make_spotify_track(duration_ms=203000)  # 3 s off
    assert duration_match(tidal, spotify) is False


def test_duration_match_exact():
    tidal = make_tidal_track(duration=180)
    spotify = make_spotify_track(duration_ms=180000)
    assert duration_match(tidal, spotify) is True


# ---------------------------------------------------------------------------
# name_match
# ---------------------------------------------------------------------------

def test_name_match_simple_substring():
    tidal = make_tidal_track(name="Test Track")
    spotify = make_spotify_track(name="Test Track")
    assert name_match(tidal, spotify) is True


def test_name_match_tidal_has_extra_suffix():
    tidal = make_tidal_track(name="Test Track (Remastered)")
    spotify = make_spotify_track(name="Test Track")
    assert name_match(tidal, spotify) is True


def test_name_match_normalized():
    tidal = make_tidal_track(name="cafe song")
    spotify = make_spotify_track(name="café song")
    assert name_match(tidal, spotify) is True


def test_name_match_fails_when_unrelated():
    tidal = make_tidal_track(name="Completely Different")
    spotify = make_spotify_track(name="Test Track")
    assert name_match(tidal, spotify) is False


def test_name_match_exclusion_instrumental():
    tidal = make_tidal_track(name="Test Track (Instrumental)")
    spotify = make_spotify_track(name="Test Track")
    assert name_match(tidal, spotify) is False


def test_name_match_exclusion_remix():
    tidal = make_tidal_track(name="Test Track (Remix)")
    spotify = make_spotify_track(name="Test Track")
    assert name_match(tidal, spotify) is False


def test_name_match_both_have_remix():
    # simple() strips parens from the Spotify name → "test track" is substring of "test track remix"
    tidal = make_tidal_track(name="Test Track Remix")
    spotify = make_spotify_track(name="Test Track (Remix)")
    assert name_match(tidal, spotify) is True


# ---------------------------------------------------------------------------
# artist_match
# ---------------------------------------------------------------------------

def test_artist_match_identical():
    tidal = make_tidal_track(artists=[make_tidal_artist("Test Artist")])
    spotify = make_spotify_track(artist_names=["Test Artist"])
    assert artist_match(tidal, spotify) is True


def test_artist_match_normalized():
    tidal = make_tidal_track(artists=[make_tidal_artist("Björk")])
    spotify = make_spotify_track(artist_names=["Bjork"])
    assert artist_match(tidal, spotify) is True


def test_artist_match_split_ampersand():
    tidal = make_tidal_track(artists=[make_tidal_artist("Artist A & Artist B")])
    spotify = make_spotify_track(artist_names=["Artist A"])
    assert artist_match(tidal, spotify) is True


def test_artist_match_no_overlap():
    tidal = make_tidal_track(artists=[make_tidal_artist("Wrong Artist")])
    spotify = make_spotify_track(artist_names=["Test Artist"])
    assert artist_match(tidal, spotify) is False


# ---------------------------------------------------------------------------
# match
# ---------------------------------------------------------------------------

def test_match_via_isrc():
    tidal = make_tidal_track(isrc="USRC12345678", duration=999, name="Anything")
    spotify = make_spotify_track(isrc="USRC12345678")
    assert match(tidal, spotify) is True


def test_match_via_fuzzy():
    tidal = make_tidal_track(isrc="XXXX00000000", duration=200,
                             name="Test Track", artists=[make_tidal_artist("Test Artist")])
    spotify = make_spotify_track(isrc="YYYY99999999", duration_ms=200500,
                                 name="Test Track", artist_names=["Test Artist"])
    assert match(tidal, spotify) is True


def test_match_no_spotify_id():
    tidal = make_tidal_track()
    spotify = make_spotify_track(id=None)
    spotify["id"] = None
    assert match(tidal, spotify) is False


def test_match_fails_wrong_duration_and_no_isrc():
    tidal = make_tidal_track(isrc="XXXX", duration=200,
                             name="Test Track", artists=[make_tidal_artist("Test Artist")])
    spotify = make_spotify_track(isrc=None, duration_ms=999000,
                                 name="Test Track", artist_names=["Test Artist"])
    assert match(tidal, spotify) is False


# ---------------------------------------------------------------------------
# album_similarity_passes
# ---------------------------------------------------------------------------

def album_similarity_passes_match():
    tidal_album = MagicMock()
    tidal_album.name = "Thriller"
    tidal_album.artists = [make_tidal_artist("Michael Jackson")]
    spotify_album = {"name": "Thriller", "artists": [{"name": "Michael Jackson"}]}
    assert album_similarity_passes(spotify_album, tidal_album) is True


def album_similarity_passes_no_match():
    tidal_album = MagicMock()
    tidal_album.name = "Some Other Album"
    tidal_album.artists = [make_tidal_artist("Other Artist")]
    spotify_album = {"name": "Thriller", "artists": [{"name": "Michael Jackson"}]}
    assert album_similarity_passes(spotify_album, tidal_album) is False


# ---------------------------------------------------------------------------
# populate_track_match_cache
# ---------------------------------------------------------------------------

def test_populate_track_match_cache_basic(mocker):
    mocker.patch("spotify_to_tidal.sync.track_match_cache")
    from spotify_to_tidal.sync import track_match_cache

    tidal = make_tidal_track(id=42, isrc="USRC12345678")
    spotify = make_spotify_track(id="sp1", isrc="USRC12345678")
    populate_track_match_cache([spotify], [tidal])

    track_match_cache.insert.assert_called_once_with(("sp1", 42))


def test_populate_track_match_cache_no_match(mocker):
    mocker.patch("spotify_to_tidal.sync.track_match_cache")
    from spotify_to_tidal.sync import track_match_cache

    tidal = make_tidal_track(id=42, isrc="XXXX00000000", duration=999)
    spotify = make_spotify_track(id="sp1", isrc="YYYY99999999", duration_ms=1000)
    populate_track_match_cache([spotify], [tidal])

    track_match_cache.insert.assert_not_called()


def test_populate_track_match_cache_unavailable_tidal_track(mocker):
    mocker.patch("spotify_to_tidal.sync.track_match_cache")
    from spotify_to_tidal.sync import track_match_cache

    tidal = make_tidal_track(id=42, isrc="USRC12345678", available=False)
    spotify = make_spotify_track(id="sp1", isrc="USRC12345678")
    populate_track_match_cache([spotify], [tidal])

    track_match_cache.insert.assert_not_called()


# ---------------------------------------------------------------------------
# get_new_spotify_tracks
# ---------------------------------------------------------------------------

def test_get_new_spotify_tracks_all_new(mocker):
    mock_cache = mocker.patch("spotify_to_tidal.sync.track_match_cache")
    mock_failure = mocker.patch("spotify_to_tidal.sync.failure_cache")
    mock_cache.get.return_value = None
    mock_failure.has_match_failure.return_value = False

    tracks = [make_spotify_track(id="sp1"), make_spotify_track(id="sp2")]
    result = get_new_spotify_tracks(tracks)
    assert result == tracks


def test_get_new_spotify_tracks_already_cached(mocker):
    mock_cache = mocker.patch("spotify_to_tidal.sync.track_match_cache")
    mock_failure = mocker.patch("spotify_to_tidal.sync.failure_cache")
    mock_cache.get.return_value = 99  # already matched
    mock_failure.has_match_failure.return_value = False

    tracks = [make_spotify_track(id="sp1")]
    result = get_new_spotify_tracks(tracks)
    assert result == []


def test_get_new_spotify_tracks_in_failure_cache(mocker):
    mock_cache = mocker.patch("spotify_to_tidal.sync.track_match_cache")
    mock_failure = mocker.patch("spotify_to_tidal.sync.failure_cache")
    mock_cache.get.return_value = None
    mock_failure.has_match_failure.return_value = True

    tracks = [make_spotify_track(id="sp1")]
    result = get_new_spotify_tracks(tracks)
    assert result == []


def test_get_new_spotify_tracks_skips_null_id(mocker):
    mocker.patch("spotify_to_tidal.sync.track_match_cache")
    mocker.patch("spotify_to_tidal.sync.failure_cache")

    track = make_spotify_track()
    track["id"] = None
    result = get_new_spotify_tracks([track])
    assert result == []


# ---------------------------------------------------------------------------
# pick_tidal_playlist_for_spotify_playlist
# ---------------------------------------------------------------------------

def test_pick_tidal_playlist_found():
    spotify_pl = {"name": "My Playlist"}
    tidal_pl = MagicMock()
    result = pick_tidal_playlist_for_spotify_playlist(spotify_pl, {"My Playlist": tidal_pl})
    assert result == (spotify_pl, tidal_pl)


def test_pick_tidal_playlist_not_found():
    spotify_pl = {"name": "My Playlist"}
    result = pick_tidal_playlist_for_spotify_playlist(spotify_pl, {})
    assert result == (spotify_pl, None)


# ---------------------------------------------------------------------------
# sync_playlist — Tidal-as-source-of-truth ordering
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def make_sync_playlist_mocks(mocker, spotify_tracks, old_tidal_tracks, cache_map=None):
    """
    Patch the heavy async/IO dependencies of sync_playlist.
    cache_map: {spotify_id: tidal_id} used for track_match_cache.get
    """
    mocker.patch("spotify_to_tidal.sync.get_tracks_from_spotify_playlist",
                 return_value=spotify_tracks)
    mocker.patch("spotify_to_tidal.sync.get_all_playlist_tracks",
                 return_value=old_tidal_tracks)
    mocker.patch("spotify_to_tidal.sync.search_new_tracks_on_tidal", return_value=[])
    mocker.patch("spotify_to_tidal.sync.populate_track_match_cache")
    mock_add = mocker.patch("spotify_to_tidal.sync.add_multiple_tracks_to_playlist")

    def cache_get(spotify_id):
        return (cache_map or {}).get(spotify_id)

    mocker.patch("spotify_to_tidal.sync.track_match_cache.get", side_effect=cache_get)
    return mock_add


def test_sync_playlist_appends_new_tracks(mocker):
    spotify_tracks = [make_spotify_track(id="sp1"), make_spotify_track(id="sp2")]
    old_tidal = [make_tidal_track(id=10)]  # sp1 already synced
    cache_map = {"sp1": 10, "sp2": 20}

    tidal_playlist = MagicMock()
    mock_add = make_sync_playlist_mocks(mocker, spotify_tracks, old_tidal, cache_map)

    _run(sync_playlist(MagicMock(), MagicMock(), {"name": "pl", "id": "x"}, tidal_playlist, {}))

    mock_add.assert_called_once_with(tidal_playlist, [20])


def test_sync_playlist_no_new_tracks_does_not_write(mocker):
    spotify_tracks = [make_spotify_track(id="sp1")]
    old_tidal = [make_tidal_track(id=10)]
    cache_map = {"sp1": 10}

    tidal_playlist = MagicMock()
    mock_add = make_sync_playlist_mocks(mocker, spotify_tracks, old_tidal, cache_map)

    _run(sync_playlist(MagicMock(), MagicMock(), {"name": "pl", "id": "x"}, tidal_playlist, {}))

    mock_add.assert_not_called()


def test_sync_playlist_preserves_tidal_order(mocker):
    # Spotify order: sp1, sp2, sp3; Tidal already has sp3 then sp1 (different order).
    # Only sp2 should be appended — sp3 and sp1 are NOT moved.
    spotify_tracks = [
        make_spotify_track(id="sp1"),
        make_spotify_track(id="sp2"),
        make_spotify_track(id="sp3"),
    ]
    old_tidal = [make_tidal_track(id=30), make_tidal_track(id=10)]  # sp3, sp1 in Tidal order
    cache_map = {"sp1": 10, "sp2": 20, "sp3": 30}

    tidal_playlist = MagicMock()
    mock_add = make_sync_playlist_mocks(mocker, spotify_tracks, old_tidal, cache_map)

    _run(sync_playlist(MagicMock(), MagicMock(), {"name": "pl", "id": "x"}, tidal_playlist, {}))

    mock_add.assert_called_once_with(tidal_playlist, [20])


def test_sync_playlist_deduplicates_spotify_tracks(mocker):
    # Two Spotify tracks that both map to the same Tidal ID
    spotify_tracks = [make_spotify_track(id="sp1"), make_spotify_track(id="sp2")]
    old_tidal = []
    cache_map = {"sp1": 10, "sp2": 10}

    tidal_playlist = MagicMock()
    mock_add = make_sync_playlist_mocks(mocker, spotify_tracks, old_tidal, cache_map)

    _run(sync_playlist(MagicMock(), MagicMock(), {"name": "pl", "id": "x"}, tidal_playlist, {}))

    mock_add.assert_called_once_with(tidal_playlist, [10])


def test_sync_playlist_creates_new_tidal_playlist(mocker):
    spotify_tracks = [make_spotify_track(id="sp1")]
    cache_map = {"sp1": 10}

    mocker.patch("spotify_to_tidal.sync.get_tracks_from_spotify_playlist",
                 return_value=spotify_tracks)
    mocker.patch("spotify_to_tidal.sync.search_new_tracks_on_tidal", return_value=[])
    mocker.patch("spotify_to_tidal.sync.populate_track_match_cache")
    mock_add = mocker.patch("spotify_to_tidal.sync.add_multiple_tracks_to_playlist")
    mocker.patch("spotify_to_tidal.sync.track_match_cache.get",
                 side_effect=lambda sid: cache_map.get(sid))

    new_playlist = MagicMock()
    tidal_session = MagicMock()
    tidal_session.user.create_playlist.return_value = new_playlist

    created = _run(sync_playlist(MagicMock(), tidal_session,
                                 {"name": "pl", "id": "x", "description": "desc"},
                                 None, {}))

    assert created is True
    tidal_session.user.create_playlist.assert_called_once_with("pl", "desc")
    mock_add.assert_called_once_with(new_playlist, [10])


def test_sync_playlist_skips_unmatched_tracks(mocker):
    # sp2 has no Tidal match
    spotify_tracks = [make_spotify_track(id="sp1"), make_spotify_track(id="sp2")]
    old_tidal = []
    cache_map = {"sp1": 10}  # sp2 not in cache

    tidal_playlist = MagicMock()
    mock_add = make_sync_playlist_mocks(mocker, spotify_tracks, old_tidal, cache_map)

    _run(sync_playlist(MagicMock(), MagicMock(), {"name": "pl", "id": "x"}, tidal_playlist, {}))

    mock_add.assert_called_once_with(tidal_playlist, [10])


def test_sync_playlist_returns_false_when_not_created(mocker):
    spotify_tracks = [make_spotify_track(id="sp1")]
    old_tidal = [make_tidal_track(id=10)]
    cache_map = {"sp1": 10}

    tidal_playlist = MagicMock()
    make_sync_playlist_mocks(mocker, spotify_tracks, old_tidal, cache_map)

    created = _run(sync_playlist(MagicMock(), MagicMock(),
                                 {"name": "pl", "id": "x"}, tidal_playlist, {}))
    assert created is False
