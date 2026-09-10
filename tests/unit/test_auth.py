# tests/unit/test_auth.py

import pytest
import spotipy
import tidalapi
import yaml
import sys
from unittest import mock
from spotify_to_tidal.auth import open_spotify_session, open_tidal_session, _save_tidal_session, SPOTIFY_SCOPES
import spotify_to_tidal.auth as auth_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_paths(monkeypatch, tmp_path):
    """Redirect the module-level cache paths to a tmp dir and no-op _ensure_cache_dir."""
    monkeypatch.setattr(auth_module, '_SPOTIFY_CACHE', str(tmp_path / 'spotify_cache'))
    monkeypatch.setattr(auth_module, '_TIDAL_SESSION', str(tmp_path / 'tidal_session.yml'))
    monkeypatch.setattr(auth_module, '_ensure_cache_dir', lambda: None)


# ---------------------------------------------------------------------------
# open_spotify_session
# ---------------------------------------------------------------------------

def test_open_spotify_session_uses_cached_token(mocker, tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)

    mock_oauth_cls = mocker.patch('spotify_to_tidal.auth.spotipy.SpotifyOAuth', autospec=True)
    mock_spotify_cls = mocker.patch('spotify_to_tidal.auth.spotipy.Spotify', autospec=True)

    mock_oauth = mock_oauth_cls.return_value
    mock_oauth.get_cached_token.return_value = {'access_token': 'tok'}
    mock_oauth.is_token_expired.return_value = False

    config = {
        'username': 'user',
        'client_id': 'cid',
        'client_secret': 'csec',
        'redirect_uri': 'http://127.0.0.1:8888/callback',
    }
    result = open_spotify_session(config)

    mock_oauth_cls.assert_called_once_with(
        username='user',
        scope=SPOTIFY_SCOPES,
        client_id='cid',
        client_secret='csec',
        redirect_uri='http://127.0.0.1:8888/callback',
        requests_timeout=2,
        open_browser=False,
        cache_path=str(tmp_path / 'spotify_cache'),
    )
    mock_spotify_cls.assert_called_once_with(oauth_manager=mock_oauth)
    assert result == mock_spotify_cls.return_value


def test_open_spotify_session_new_auth_via_local_server(mocker, tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)

    mock_oauth_cls = mocker.patch('spotify_to_tidal.auth.spotipy.SpotifyOAuth', autospec=True)
    mocker.patch('spotify_to_tidal.auth.spotipy.Spotify', autospec=True)
    mocker.patch('spotify_to_tidal.auth.webbrowser.open')
    mocker.patch('spotify_to_tidal.auth._capture_spotify_callback', return_value='authcode123')

    mock_oauth = mock_oauth_cls.return_value
    mock_oauth.get_cached_token.return_value = None
    mock_oauth.get_authorize_url.return_value = 'https://accounts.spotify.com/authorize?...'

    config = {
        'username': 'user',
        'client_id': 'cid',
        'client_secret': 'csec',
        'redirect_uri': 'http://127.0.0.1:8888/callback',
    }
    open_spotify_session(config)

    mock_oauth.get_access_token.assert_called_once_with('authcode123')


def test_open_spotify_session_callback_timeout_exits(mocker, tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)

    mock_oauth_cls = mocker.patch('spotify_to_tidal.auth.spotipy.SpotifyOAuth', autospec=True)
    mocker.patch('spotify_to_tidal.auth.webbrowser.open')
    mocker.patch('spotify_to_tidal.auth._capture_spotify_callback', return_value=None)

    mock_oauth = mock_oauth_cls.return_value
    mock_oauth.get_cached_token.return_value = None
    mock_oauth.get_authorize_url.return_value = 'https://accounts.spotify.com/authorize?...'

    mock_exit = mocker.patch('sys.exit')

    config = {
        'username': 'user',
        'client_id': 'cid',
        'client_secret': 'csec',
        'redirect_uri': 'http://127.0.0.1:8888/callback',
    }
    open_spotify_session(config)
    mock_exit.assert_called_once()


def test_open_spotify_session_exchange_error_exits(mocker, tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)

    mock_oauth_cls = mocker.patch('spotify_to_tidal.auth.spotipy.SpotifyOAuth', autospec=True)
    mocker.patch('spotify_to_tidal.auth.webbrowser.open')
    mocker.patch('spotify_to_tidal.auth._capture_spotify_callback', return_value='code')

    mock_oauth = mock_oauth_cls.return_value
    mock_oauth.get_cached_token.return_value = None
    mock_oauth.get_authorize_url.return_value = 'https://accounts.spotify.com/authorize?...'
    mock_oauth.get_access_token.side_effect = spotipy.SpotifyOauthError('bad token')

    mock_exit = mocker.patch('sys.exit')

    config = {
        'username': 'user',
        'client_id': 'cid',
        'client_secret': 'csec',
        'redirect_uri': 'http://127.0.0.1:8888/callback',
    }
    open_spotify_session(config)
    mock_exit.assert_called_once()


# ---------------------------------------------------------------------------
# _save_tidal_session
# ---------------------------------------------------------------------------

def test_save_tidal_session_writes_correct_fields(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)

    mock_session = mock.MagicMock()
    mock_session.session_id = 'sess-id'
    mock_session.token_type = 'Bearer'
    mock_session.access_token = 'acc-tok'
    mock_session.refresh_token = 'ref-tok'

    _save_tidal_session(mock_session)

    data = yaml.safe_load((tmp_path / 'tidal_session.yml').read_text())
    assert data == {
        'session_id': 'sess-id',
        'token_type': 'Bearer',
        'access_token': 'acc-tok',
        'refresh_token': 'ref-tok',
    }


# ---------------------------------------------------------------------------
# open_tidal_session — cached session path
# ---------------------------------------------------------------------------

def test_open_tidal_session_loads_existing_session(mocker, tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)

    session_data = {
        'session_id': 'old-sess',
        'token_type': 'Bearer',
        'access_token': 'old-acc',
        'refresh_token': 'old-ref',
    }
    (tmp_path / 'tidal_session.yml').write_text(yaml.dump(session_data))

    mock_session = mocker.MagicMock()
    mock_session.session_id = 'new-sess'
    mock_session.token_type = 'Bearer'
    mock_session.access_token = 'new-acc'
    mock_session.refresh_token = 'new-ref'
    mock_session.load_oauth_session.return_value = True

    mocker.patch('spotify_to_tidal.auth.tidalapi.Session', return_value=mock_session)

    result = open_tidal_session()

    mock_session.load_oauth_session.assert_called_once_with(
        token_type='Bearer',
        access_token='old-acc',
        refresh_token='old-ref',
    )
    assert result is mock_session
    saved = yaml.safe_load((tmp_path / 'tidal_session.yml').read_text())
    assert saved['access_token'] == 'new-acc'


def test_open_tidal_session_falls_through_when_load_fails(mocker, tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)

    session_data = {
        'session_id': 'old-sess',
        'token_type': 'Bearer',
        'access_token': 'old-acc',
        'refresh_token': 'old-ref',
    }
    (tmp_path / 'tidal_session.yml').write_text(yaml.dump(session_data))

    mock_session = mocker.MagicMock()
    mock_session.session_id = 'new-sess'
    mock_session.token_type = 'Bearer'
    mock_session.access_token = 'new-acc'
    mock_session.refresh_token = 'new-ref'
    mock_session.load_oauth_session.side_effect = Exception('token expired')
    mock_session.login_oauth.return_value = (
        mocker.MagicMock(verification_uri_complete='https://tidal.com/login?code=abc'),
        mocker.MagicMock(),
    )

    mocker.patch('spotify_to_tidal.auth.tidalapi.Session', return_value=mock_session)
    mocker.patch('spotify_to_tidal.auth.webbrowser.open')

    open_tidal_session()

    mock_session.login_oauth.assert_called_once()


# ---------------------------------------------------------------------------
# open_tidal_session — no cached session (first run)
# ---------------------------------------------------------------------------

def test_open_tidal_session_no_cache_triggers_oauth(mocker, tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)  # no tidal_session.yml present

    mock_session = mocker.MagicMock()
    mock_session.session_id = 'new-sess'
    mock_session.token_type = 'Bearer'
    mock_session.access_token = 'new-acc'
    mock_session.refresh_token = 'new-ref'

    login_mock = mocker.MagicMock()
    login_mock.verification_uri_complete = 'https://tidal.com/login?code=xyz'
    future_mock = mocker.MagicMock()
    mock_session.login_oauth.return_value = (login_mock, future_mock)

    mocker.patch('spotify_to_tidal.auth.tidalapi.Session', return_value=mock_session)
    mocker.patch('spotify_to_tidal.auth.webbrowser.open')

    result = open_tidal_session()

    mock_session.login_oauth.assert_called_once()
    future_mock.result.assert_called_once()
    assert result is mock_session
    saved = yaml.safe_load((tmp_path / 'tidal_session.yml').read_text())
    assert saved['access_token'] == 'new-acc'


def test_open_tidal_session_prepends_https_if_missing(mocker, tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)

    mock_session = mocker.MagicMock()
    mock_session.session_id = 's'
    mock_session.token_type = 'Bearer'
    mock_session.access_token = 'a'
    mock_session.refresh_token = 'r'

    login_mock = mocker.MagicMock()
    login_mock.verification_uri_complete = 'tidal.com/login?code=xyz'  # no https://
    mock_session.login_oauth.return_value = (login_mock, mocker.MagicMock())

    mocker.patch('spotify_to_tidal.auth.tidalapi.Session', return_value=mock_session)
    mock_browser = mocker.patch('spotify_to_tidal.auth.webbrowser.open')

    open_tidal_session()

    mock_browser.assert_called_once_with('https://tidal.com/login?code=xyz')
