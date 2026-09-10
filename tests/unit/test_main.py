import pytest
import yaml
from unittest.mock import MagicMock

from spotify_to_tidal.__main__ import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_config(tmp_path, extra=None):
    config = {
        'spotify': {
            'username': 'test_user',
            'client_id': 'test_client_id',
            'client_secret': 'test_client_secret',
            'redirect_uri': 'http://127.0.0.1/',
        }
    }
    if extra:
        config.update(extra)
    path = tmp_path / 'config.yml'
    path.write_text(yaml.dump(config))
    return str(path)


def setup_sessions(mocker, tidal_logged_in=True):
    mock_spotify = MagicMock()
    mock_tidal = MagicMock()
    mock_tidal.check_login.return_value = tidal_logged_in
    mocker.patch('spotify_to_tidal.__main__._auth.open_spotify_session', return_value=mock_spotify)
    mocker.patch('spotify_to_tidal.__main__._auth.open_tidal_session', return_value=mock_tidal)
    return mock_spotify, mock_tidal


def argv(tmp_path, *args, extra_config=None):
    path = write_config(tmp_path, extra_config)
    return ['prog', '--config', path] + list(args), path


# ---------------------------------------------------------------------------
# --refresh-session
# ---------------------------------------------------------------------------

def test_refresh_session_exits_zero_on_success(mocker, tmp_path, monkeypatch):
    args, _ = argv(tmp_path, '--refresh-session')
    monkeypatch.setattr('sys.argv', args)
    mock_tidal = MagicMock()
    mock_tidal.check_login.return_value = True
    mocker.patch('spotify_to_tidal.__main__._auth.open_tidal_session', return_value=mock_tidal)

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0


def test_refresh_session_exits_nonzero_when_login_fails(mocker, tmp_path, monkeypatch):
    args, _ = argv(tmp_path, '--refresh-session')
    monkeypatch.setattr('sys.argv', args)
    mock_tidal = MagicMock()
    mock_tidal.check_login.return_value = False
    mocker.patch('spotify_to_tidal.__main__._auth.open_tidal_session', return_value=mock_tidal)

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code != 0


def test_refresh_session_does_not_open_spotify(mocker, tmp_path, monkeypatch):
    args, _ = argv(tmp_path, '--refresh-session')
    monkeypatch.setattr('sys.argv', args)
    mock_tidal = MagicMock()
    mock_tidal.check_login.return_value = True
    mocker.patch('spotify_to_tidal.__main__._auth.open_tidal_session', return_value=mock_tidal)
    mock_spotify_open = mocker.patch('spotify_to_tidal.__main__._auth.open_spotify_session')

    with pytest.raises(SystemExit):
        main()
    mock_spotify_open.assert_not_called()


# ---------------------------------------------------------------------------
# Tidal login check
# ---------------------------------------------------------------------------

def test_exits_when_tidal_login_fails(mocker, tmp_path, monkeypatch):
    args, _ = argv(tmp_path)
    monkeypatch.setattr('sys.argv', args)
    setup_sessions(mocker, tidal_logged_in=False)

    with pytest.raises(SystemExit):
        main()


# ---------------------------------------------------------------------------
# --fetch-tidal-urls
# ---------------------------------------------------------------------------

def test_fetch_tidal_urls_with_uri(mocker, tmp_path, monkeypatch):
    args, _ = argv(tmp_path, '--fetch-tidal-urls', '--uri', 'spotify:playlist:abc')
    monkeypatch.setattr('sys.argv', args)
    mock_spotify, mock_tidal = setup_sessions(mocker)

    mock_spotify.playlist.return_value = {'name': 'My Playlist', 'id': 'abc'}
    mocker.patch('spotify_to_tidal.__main__._sync.get_tidal_playlists_wrapper', return_value={})
    mocker.patch('spotify_to_tidal.__main__._sync.pick_tidal_playlist_for_spotify_playlist',
                 return_value=({'name': 'My Playlist'}, MagicMock()))
    mock_print = mocker.patch('spotify_to_tidal.__main__._sync.print_tidal_playlist_urls')
    mocker.patch('spotify_to_tidal.__main__._sync.print_all_tidal_playlist_urls')

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    mock_print.assert_called_once()


def test_fetch_tidal_urls_with_sync_playlists_config(mocker, tmp_path, monkeypatch):
    args, _ = argv(tmp_path, '--fetch-tidal-urls',
                   extra_config={'sync_playlists': [{'spotify_id': 'sp1', 'tidal_id': 'td1'}]})
    monkeypatch.setattr('sys.argv', args)
    setup_sessions(mocker)

    mocker.patch('spotify_to_tidal.__main__._sync.get_tidal_playlists_wrapper', return_value={})
    mock_from_config = mocker.patch('spotify_to_tidal.__main__._sync.get_playlists_from_config',
                                    return_value=[])
    mocker.patch('spotify_to_tidal.__main__._sync.print_tidal_playlist_urls')
    mocker.patch('spotify_to_tidal.__main__._sync.print_all_tidal_playlist_urls')

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    mock_from_config.assert_called_once()


def test_fetch_tidal_urls_default(mocker, tmp_path, monkeypatch):
    args, _ = argv(tmp_path, '--fetch-tidal-urls')
    monkeypatch.setattr('sys.argv', args)
    setup_sessions(mocker)

    mocker.patch('spotify_to_tidal.__main__._sync.get_tidal_playlists_wrapper', return_value={})
    mock_mappings = mocker.patch('spotify_to_tidal.__main__._sync.get_user_playlist_mappings',
                                 return_value=[])
    mocker.patch('spotify_to_tidal.__main__._sync.print_tidal_playlist_urls')
    mocker.patch('spotify_to_tidal.__main__._sync.print_all_tidal_playlist_urls')

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    mock_mappings.assert_called_once()


# ---------------------------------------------------------------------------
# --test-create-tidal-playlist
# ---------------------------------------------------------------------------

def test_test_create_tidal_playlist_exits_zero(mocker, tmp_path, monkeypatch):
    args, _ = argv(tmp_path, '--test-create-tidal-playlist')
    monkeypatch.setattr('sys.argv', args)
    setup_sessions(mocker)
    mock_create = mocker.patch('spotify_to_tidal.__main__._sync.test_create_tidal_playlist')

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    mock_create.assert_called_once()


def test_test_create_tidal_playlist_uses_custom_name(mocker, tmp_path, monkeypatch):
    args, _ = argv(tmp_path, '--test-create-tidal-playlist', '--test-playlist-name', 'my-test')
    monkeypatch.setattr('sys.argv', args)
    mock_spotify, mock_tidal = setup_sessions(mocker)
    mock_create = mocker.patch('spotify_to_tidal.__main__._sync.test_create_tidal_playlist')

    with pytest.raises(SystemExit):
        main()
    mock_create.assert_called_once_with(mock_tidal, 'my-test')


# ---------------------------------------------------------------------------
# --uri (sync specific playlist)
# ---------------------------------------------------------------------------

def test_uri_sync_calls_sync_playlists_wrapper(mocker, tmp_path, monkeypatch):
    args, _ = argv(tmp_path, '--uri', 'spotify:playlist:abc')
    monkeypatch.setattr('sys.argv', args)
    mock_spotify, mock_tidal = setup_sessions(mocker)

    mock_spotify.playlist.return_value = {'name': 'My Playlist', 'id': 'abc'}
    mocker.patch('spotify_to_tidal.__main__._sync.get_tidal_playlists_wrapper', return_value={})
    mocker.patch('spotify_to_tidal.__main__._sync.pick_tidal_playlist_for_spotify_playlist',
                 return_value=({'name': 'My Playlist'}, None))
    mock_sync = mocker.patch('spotify_to_tidal.__main__._sync.sync_playlists_wrapper')
    mock_fav = mocker.patch('spotify_to_tidal.__main__._sync.sync_favorites_wrapper')

    main()
    mock_sync.assert_called_once()
    mock_fav.assert_not_called()  # no --sync-favorites flag


# ---------------------------------------------------------------------------
# --sync-favorites
# ---------------------------------------------------------------------------

def test_sync_favorites_only(mocker, tmp_path, monkeypatch):
    args, _ = argv(tmp_path, '--sync-favorites')
    monkeypatch.setattr('sys.argv', args)
    setup_sessions(mocker)
    mock_fav = mocker.patch('spotify_to_tidal.__main__._sync.sync_favorites_wrapper')

    main()
    mock_fav.assert_called_once()


# ---------------------------------------------------------------------------
# Default (all user playlists)
# ---------------------------------------------------------------------------

def test_default_syncs_all_user_playlists(mocker, tmp_path, monkeypatch):
    args, _ = argv(tmp_path, extra_config={'sync_favorites_default': False})
    monkeypatch.setattr('sys.argv', args)
    setup_sessions(mocker)

    mock_mappings = mocker.patch('spotify_to_tidal.__main__._sync.get_user_playlist_mappings',
                                 return_value=[])
    mock_sync = mocker.patch('spotify_to_tidal.__main__._sync.sync_playlists_wrapper')
    mock_fav = mocker.patch('spotify_to_tidal.__main__._sync.sync_favorites_wrapper')

    main()
    mock_mappings.assert_called_once()
    mock_sync.assert_called_once()
    mock_fav.assert_not_called()


def test_default_syncs_favorites_when_config_enables_it(mocker, tmp_path, monkeypatch):
    args, _ = argv(tmp_path, extra_config={'sync_favorites_default': True})
    monkeypatch.setattr('sys.argv', args)
    setup_sessions(mocker)

    mocker.patch('spotify_to_tidal.__main__._sync.get_user_playlist_mappings', return_value=[])
    mocker.patch('spotify_to_tidal.__main__._sync.sync_playlists_wrapper')
    mock_fav = mocker.patch('spotify_to_tidal.__main__._sync.sync_favorites_wrapper')

    main()
    mock_fav.assert_called_once()


# ---------------------------------------------------------------------------
# Config-based sync_playlists
# ---------------------------------------------------------------------------

def test_config_sync_playlists_uses_get_playlists_from_config(mocker, tmp_path, monkeypatch):
    args, _ = argv(tmp_path, extra_config={
        'sync_playlists': [{'spotify_id': 'sp1', 'tidal_id': 'td1'}],
        'sync_favorites_default': False,
    })
    monkeypatch.setattr('sys.argv', args)
    setup_sessions(mocker)

    mock_from_config = mocker.patch('spotify_to_tidal.__main__._sync.get_playlists_from_config',
                                    return_value=[])
    mock_sync = mocker.patch('spotify_to_tidal.__main__._sync.sync_playlists_wrapper')

    main()
    mock_from_config.assert_called_once()
    mock_sync.assert_called_once()
