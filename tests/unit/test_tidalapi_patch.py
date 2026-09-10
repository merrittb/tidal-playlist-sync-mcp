import asyncio
from unittest.mock import MagicMock, call, patch

import pytest
import requests

from spotify_to_tidal.tidalapi_patch import (
    _remove_indices_from_playlist,
    add_multiple_tracks_to_playlist,
    clear_tidal_playlist,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_http_error(status_code):
    response = MagicMock()
    response.status_code = status_code
    response.text = f"HTTP {status_code}"
    err = requests.exceptions.HTTPError()
    err.response = response
    return err


def make_playlist(num_tracks=0):
    pl = MagicMock()
    pl._etag = "etag-abc"
    pl.id = "playlist-1"
    pl._base_url = "playlists/%s"
    pl.num_tracks = num_tracks
    return pl


# ---------------------------------------------------------------------------
# _remove_indices_from_playlist
# ---------------------------------------------------------------------------

def test_remove_indices_success(mocker):
    pl = make_playlist()
    mocker.patch("spotify_to_tidal.tidalapi_patch.time.sleep")

    _remove_indices_from_playlist(pl, [0, 1, 2])

    pl.request.request.assert_called_once()
    pl._reparse.assert_called_once()


def test_remove_indices_retries_on_412(mocker):
    pl = make_playlist()
    mock_sleep = mocker.patch("spotify_to_tidal.tidalapi_patch.time.sleep")

    # Fail with 412 once, succeed on second attempt
    pl.request.request.side_effect = [make_http_error(412), None]

    _remove_indices_from_playlist(pl, [0], retries=2)

    assert pl.request.request.call_count == 2
    mock_sleep.assert_called_once_with(1)


def test_remove_indices_raises_on_non_412(mocker):
    pl = make_playlist()
    mocker.patch("spotify_to_tidal.tidalapi_patch.time.sleep")
    pl.request.request.side_effect = make_http_error(500)

    with pytest.raises(requests.exceptions.HTTPError):
        _remove_indices_from_playlist(pl, [0])


def test_remove_indices_raises_after_all_412_retries_exhausted(mocker):
    pl = make_playlist()
    mocker.patch("spotify_to_tidal.tidalapi_patch.time.sleep")
    pl.request.request.side_effect = make_http_error(412)

    with pytest.raises(requests.exceptions.HTTPError):
        _remove_indices_from_playlist(pl, [0], retries=2)

    assert pl.request.request.call_count == 2


# ---------------------------------------------------------------------------
# clear_tidal_playlist
# ---------------------------------------------------------------------------

def test_clear_tidal_playlist_calls_remove_in_chunks(mocker):
    pl = make_playlist(num_tracks=45)
    mock_remove = mocker.patch("spotify_to_tidal.tidalapi_patch._remove_indices_from_playlist")

    clear_tidal_playlist(pl, chunk_size=20)

    # 45 tracks: chunks of 20, 20, 5
    assert mock_remove.call_count == 3
    mock_remove.assert_any_call(pl, range(20))
    mock_remove.assert_any_call(pl, range(5))


def test_clear_tidal_playlist_empty_does_nothing(mocker):
    pl = make_playlist(num_tracks=0)
    mock_remove = mocker.patch("spotify_to_tidal.tidalapi_patch._remove_indices_from_playlist")

    clear_tidal_playlist(pl)

    mock_remove.assert_not_called()


# ---------------------------------------------------------------------------
# add_multiple_tracks_to_playlist
# ---------------------------------------------------------------------------

def test_add_multiple_tracks_success(mocker):
    pl = make_playlist()
    mocker.patch("spotify_to_tidal.tidalapi_patch.time.sleep")

    add_multiple_tracks_to_playlist(pl, [1, 2, 3], chunk_size=10)

    pl.add.assert_called_once_with([1, 2, 3])
    pl._reparse.assert_called_once()


def test_add_multiple_tracks_chunked(mocker):
    pl = make_playlist()
    mocker.patch("spotify_to_tidal.tidalapi_patch.time.sleep")

    add_multiple_tracks_to_playlist(pl, list(range(25)), chunk_size=10)

    # 25 tracks in chunks of 10, 10, 5
    assert pl.add.call_count == 3
    pl.add.assert_any_call(list(range(10)))
    pl.add.assert_any_call(list(range(10, 20)))
    pl.add.assert_any_call(list(range(20, 25)))


def test_add_multiple_tracks_retries_429(mocker):
    pl = make_playlist()
    mock_sleep = mocker.patch("spotify_to_tidal.tidalapi_patch.time.sleep")

    # First add fails with 429, retry succeeds
    pl.add.side_effect = [make_http_error(429), None]

    add_multiple_tracks_to_playlist(pl, [1, 2], chunk_size=10)

    assert pl.add.call_count == 2
    mock_sleep.assert_called_once_with(2)


def test_add_multiple_tracks_falls_back_to_individual_on_persistent_error(mocker):
    pl = make_playlist()
    mocker.patch("spotify_to_tidal.tidalapi_patch.time.sleep")

    # Chunk fails with 429 on both tries, then individual tracks succeed
    chunk_error = make_http_error(429)
    pl.add.side_effect = [chunk_error, chunk_error, None, None]

    add_multiple_tracks_to_playlist(pl, [10, 20], chunk_size=10)

    # Called for: first chunk attempt, retry, individual 10, individual 20
    assert pl.add.call_count == 4
    pl.add.assert_any_call([10])
    pl.add.assert_any_call([20])


def test_add_multiple_tracks_individual_failure_is_logged_not_raised(mocker, capsys):
    pl = make_playlist()
    mocker.patch("spotify_to_tidal.tidalapi_patch.time.sleep")

    err = make_http_error(429)
    # chunk fails twice, then each individual track also fails
    pl.add.side_effect = [err, err, err, err]

    # should not raise
    add_multiple_tracks_to_playlist(pl, [10, 20], chunk_size=10)

    output = capsys.readouterr().out
    assert "Failed to add individual track" in output
