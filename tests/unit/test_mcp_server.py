import math
import pytest
from unittest.mock import MagicMock

import spotify_to_tidal.mcp_server as mcp_mod
from spotify_to_tidal.mcp_server import (
    _format_features,
    _parse_track_artists,
    _camelot_compat,
    _target_energy,
    _score_transition,
    _credited_artists,
    _base_title,
    _sessions,
    create_playlist,
    add_tracks_to_playlist,
    add_to_favorites,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_features(**kwargs):
    base = {
        'id': 'track1',
        'tempo': 128.0,
        'key': 5,       # F
        'mode': 1,      # major
        'energy': 0.8,
        'danceability': 0.75,
        'valence': 0.6,
        'loudness': -5.3,
        'time_signature': 4,
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# _format_features
# ---------------------------------------------------------------------------

def test_format_features_basic():
    f = make_features(key=0, mode=1, tempo=120.5)  # C major
    result = _format_features(f)
    assert result['bpm'] == 120.5
    assert result['key'] == 'C'
    assert result['mode'] == 'major'
    assert result['camelot'] == '8B'
    assert result['spotify_id'] == 'track1'
    assert 'name' not in result
    assert 'artists' not in result


def test_format_features_minor_mode():
    f = make_features(key=9, mode=0)  # A minor
    result = _format_features(f)
    assert result['mode'] == 'minor'
    assert result['key'] == 'A'


def test_format_features_unknown_key():
    f = make_features(key=-1, mode=-1)
    result = _format_features(f)
    assert result['key'] == 'unknown'
    assert result['mode'] == 'unknown'
    assert result['camelot'] == '?'


def test_format_features_with_name_and_artists():
    f = make_features()
    result = _format_features(f, name='My Song', artists=['Artist A'])
    assert result['name'] == 'My Song'
    assert result['artists'] == ['Artist A']


def test_format_features_rounds_values():
    f = make_features(tempo=128.123456, energy=0.8123456, loudness=-5.333)
    result = _format_features(f)
    assert result['bpm'] == 128.1
    assert result['energy'] == 0.812
    assert result['loudness_db'] == -5.3


# ---------------------------------------------------------------------------
# _parse_track_artists
# ---------------------------------------------------------------------------

def test_parse_track_artists_simple():
    track = {'name': 'Song Title', 'artists': [{'name': 'Artist A'}]}
    artists, is_remix = _parse_track_artists(track)
    assert 'artist a' in artists
    assert is_remix is False


def test_parse_track_artists_detects_remix():
    track = {'name': 'Song (Artist B Remix)', 'artists': [{'name': 'Artist A'}]}
    artists, is_remix = _parse_track_artists(track)
    assert is_remix is True
    assert 'artist b' in artists
    assert 'artist a' in artists


def test_parse_track_artists_multiple_remixers():
    track = {'name': 'Song (A & B Remix)', 'artists': [{'name': 'C'}]}
    artists, is_remix = _parse_track_artists(track)
    assert is_remix is True
    assert 'a' in artists
    assert 'b' in artists


# ---------------------------------------------------------------------------
# _camelot_compat
# ---------------------------------------------------------------------------

def test_camelot_compat_identical():
    assert _camelot_compat('8B', '8B') == 1.0


def test_camelot_compat_relative_major_minor():
    # Same number, different letter = relative major/minor
    assert _camelot_compat('8A', '8B') == 0.9


def test_camelot_compat_adjacent_same_type():
    assert _camelot_compat('8B', '9B') == 0.8


def test_camelot_compat_wheel_distance_2():
    assert _camelot_compat('8B', '10B') == 0.4


def test_camelot_compat_far():
    assert _camelot_compat('1B', '7B') == 0.1


def test_camelot_compat_unknown_returns_half():
    assert _camelot_compat('?', '8B') == 0.5
    assert _camelot_compat('8B', '?') == 0.5


def test_camelot_compat_wraps_around_wheel():
    # 1 and 12 are adjacent on the wheel
    assert _camelot_compat('1B', '12B') == 0.8


# ---------------------------------------------------------------------------
# _target_energy
# ---------------------------------------------------------------------------

def test_target_energy_flow_midpoint():
    # At midpoint, sine arc peaks
    result = _target_energy(5, 11, 'flow')  # t = 5/10 = 0.5
    expected = 0.45 + 0.35 * math.sin(math.pi * 0.5)
    assert abs(result - expected) < 0.001


def test_target_energy_flow_start():
    result = _target_energy(0, 11, 'flow')  # t = 0 → sin(0) = 0
    assert abs(result - 0.45) < 0.001


def test_target_energy_party_build():
    # t=0.15 < 0.30 → building phase
    result = _target_energy(0, 11, 'party')  # t=0
    assert result == pytest.approx(0.55, abs=0.01)


def test_target_energy_party_peak():
    # t=0.5 → peak (0.30 <= t < 0.70)
    result = _target_energy(5, 11, 'party')
    assert result == 0.90


def test_target_energy_party_breather():
    # t=0.75 → breather (0.70 <= t < 0.80)
    result = _target_energy(75, 101, 'party')  # t = 75/100 = 0.75
    assert result == 0.65


def test_target_energy_vibe_start():
    result = _target_energy(0, 11, 'vibe')
    assert result == pytest.approx(0.35, abs=0.001)


def test_target_energy_vibe_end():
    result = _target_energy(10, 11, 'vibe')  # t=1.0
    assert result == pytest.approx(0.75, abs=0.001)


def test_target_energy_unknown_strategy():
    assert _target_energy(5, 11, 'nonexistent') == 0.65


# ---------------------------------------------------------------------------
# _score_transition
# ---------------------------------------------------------------------------

def test_score_transition_returns_float():
    f_from = {'camelot': '8B', 'bpm': 128.0, 'energy': 0.7}
    f_to   = {'camelot': '8B', 'bpm': 130.0, 'energy': 0.75}
    score = _score_transition(f_from, f_to, artist_score=0.0, pos=5, total=10, strategy='flow')
    assert isinstance(score, float)
    assert score > 0


def test_score_transition_artist_score_increases_result():
    f_from = {'camelot': '8B', 'bpm': 128.0, 'energy': 0.7}
    f_to   = {'camelot': '8B', 'bpm': 130.0, 'energy': 0.75}
    score_no_artist = _score_transition(f_from, f_to, 0.0, 5, 10, 'flow')
    score_with_artist = _score_transition(f_from, f_to, 2.0, 5, 10, 'flow')
    assert score_with_artist > score_no_artist


# ---------------------------------------------------------------------------
# _credited_artists
# ---------------------------------------------------------------------------

def test_credited_artists_primary_only():
    track = {'name': 'Song', 'artists': [{'name': 'Artist A'}, {'name': 'Artist B'}]}
    result = _credited_artists(track)
    names = [r['name'] for r in result]
    assert 'Artist A' in names
    assert 'Artist B' in names
    assert all(r['role'] == 'primary' for r in result)


def test_credited_artists_feat_in_title():
    track = {'name': 'Song (feat. Guest)', 'artists': [{'name': 'Artist A'}]}
    result = _credited_artists(track)
    roles = {r['name']: r['role'] for r in result}
    assert roles['Artist A'] == 'primary'
    assert roles['Guest'] == 'featured'


def test_credited_artists_remix_in_title():
    track = {'name': 'Song (DJ Cool Remix)', 'artists': [{'name': 'Artist A'}]}
    result = _credited_artists(track)
    roles = {r['name']: r['role'] for r in result}
    assert roles.get('DJ Cool') == 'remixer'


def test_credited_artists_no_duplicate_for_known_artist():
    # If remixer name is already in primary artists, don't add again
    track = {'name': 'Song (Artist A Remix)', 'artists': [{'name': 'Artist A'}]}
    result = _credited_artists(track)
    assert sum(1 for r in result if r['name'] == 'Artist A') == 1


# ---------------------------------------------------------------------------
# _base_title
# ---------------------------------------------------------------------------

def test_base_title_strips_feat():
    assert _base_title('Song (feat. Someone)') == 'song'


def test_base_title_strips_remix():
    assert _base_title('Song (Artist Remix)') == 'song'


def test_base_title_strips_remaster():
    assert _base_title('Song (2011 Remaster)') == 'song'


def test_base_title_strips_live():
    assert _base_title('Song [Live]') == 'song'


def test_base_title_plain_lowercased():
    assert _base_title('My Song') == 'my song'


def test_base_title_no_double_strip():
    # Multiple parentheticals
    assert _base_title('Song (feat. X) (Remastered)') == 'song'


# ---------------------------------------------------------------------------
# _sessions
# ---------------------------------------------------------------------------

def test_sessions_raises_when_not_initialized(monkeypatch):
    monkeypatch.setattr(mcp_mod, '_spotify_session', None)
    monkeypatch.setattr(mcp_mod, '_tidal_session', None)
    with pytest.raises(RuntimeError, match="Sessions not initialized"):
        _sessions()


def test_sessions_returns_both_when_set(monkeypatch):
    mock_spotify = MagicMock()
    mock_tidal = MagicMock()
    monkeypatch.setattr(mcp_mod, '_spotify_session', mock_spotify)
    monkeypatch.setattr(mcp_mod, '_tidal_session', mock_tidal)
    s, t = _sessions()
    assert s is mock_spotify
    assert t is mock_tidal


# ---------------------------------------------------------------------------
# Write tool functions (formerly sync, now async via _tool wrapper)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_create_playlist_returns_id_name_url(monkeypatch):
    mock_tidal = MagicMock()
    mock_playlist = MagicMock()
    mock_playlist.id = 'pl-123'
    mock_playlist.name = 'New Playlist'
    mock_playlist.share_url = 'https://tidal.com/playlist/pl-123'
    mock_tidal.user.create_playlist.return_value = mock_playlist

    monkeypatch.setattr(mcp_mod, '_spotify_session', MagicMock())
    monkeypatch.setattr(mcp_mod, '_tidal_session', mock_tidal)
    monkeypatch.setattr(mcp_mod, '_sessions_initialized', True)

    result = await create_playlist('New Playlist', 'A description')

    mock_tidal.user.create_playlist.assert_called_once_with('New Playlist', 'A description')
    assert result['id'] == 'pl-123'
    assert result['name'] == 'New Playlist'
    assert result['url'] == 'https://tidal.com/playlist/pl-123'


@pytest.mark.anyio
async def test_add_tracks_to_playlist(monkeypatch, mocker):
    mock_tidal = MagicMock()
    mock_playlist = MagicMock()
    mock_playlist.name = 'My Playlist'
    mock_tidal.playlist.return_value = mock_playlist
    mock_add = mocker.patch('spotify_to_tidal.mcp_server.add_multiple_tracks_to_playlist')

    monkeypatch.setattr(mcp_mod, '_spotify_session', MagicMock())
    monkeypatch.setattr(mcp_mod, '_tidal_session', mock_tidal)
    monkeypatch.setattr(mcp_mod, '_sessions_initialized', True)

    result = await add_tracks_to_playlist('pl-123', [10, 20, 30])

    mock_add.assert_called_once_with(mock_playlist, [10, 20, 30])
    assert result['added'] == 3
    assert result['playlist'] == 'My Playlist'


@pytest.mark.anyio
async def test_add_to_favorites(monkeypatch):
    mock_tidal = MagicMock()

    monkeypatch.setattr(mcp_mod, '_spotify_session', MagicMock())
    monkeypatch.setattr(mcp_mod, '_tidal_session', mock_tidal)
    monkeypatch.setattr(mcp_mod, '_sessions_initialized', True)

    result = await add_to_favorites(42)

    mock_tidal.user.favorites.add_track.assert_called_once_with(42)
    assert result['added_track_id'] == 42
