#!/usr/bin/env python3

import http.server
import sys
import urllib.parse
from pathlib import Path
import spotipy
import tidalapi
import webbrowser
import yaml

__all__ = [
    'open_spotify_session',
    'open_tidal_session'
]

SPOTIFY_SCOPES = 'playlist-read-private user-library-read'

_CACHE_DIR = Path.home() / '.spotify_to_tidal'
_SPOTIFY_CACHE = str(_CACHE_DIR / 'spotify_token_cache')
_TIDAL_SESSION = str(_CACHE_DIR / 'tidal_session.yml')


def _ensure_cache_dir():
    _CACHE_DIR.mkdir(exist_ok=True)


def _capture_spotify_callback(port: int, timeout: float = 120.0) -> str | None:
    """Start a one-shot local HTTP server to capture the OAuth callback code."""
    captured: dict = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            captured['code'] = params.get('code', [None])[0]
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<h1>Spotify authenticated!</h1><p>You can close this tab.</p>')

        def log_message(self, *_):
            pass

    server = http.server.HTTPServer(('0.0.0.0', port), _Handler)
    server.timeout = timeout
    server.handle_request()
    server.server_close()
    return captured.get('code')


def open_spotify_session(config) -> spotipy.Spotify:
    _ensure_cache_dir()
    credentials_manager = spotipy.SpotifyOAuth(
        username=config['username'],
        scope=SPOTIFY_SCOPES,
        client_id=config['client_id'],
        client_secret=config['client_secret'],
        redirect_uri=config['redirect_uri'],
        requests_timeout=2,
        open_browser=False,
        cache_path=_SPOTIFY_CACHE,
    )

    cached = credentials_manager.get_cached_token()
    if cached and not credentials_manager.is_token_expired(cached):
        return spotipy.Spotify(oauth_manager=credentials_manager)

    # Need fresh auth — use a local callback server so no input() is needed.
    auth_url = credentials_manager.get_authorize_url()
    print(f'Opening Spotify auth URL in browser…', file=sys.stderr)
    print(f'If it does not open automatically, visit:\n  {auth_url}', file=sys.stderr)
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    parsed = urllib.parse.urlparse(config['redirect_uri'])
    port = parsed.port or 8888
    print(f'Waiting for Spotify callback on port {port}…', file=sys.stderr)
    code = _capture_spotify_callback(port)
    if not code:
        sys.exit('Error: did not receive Spotify OAuth callback within timeout')

    try:
        credentials_manager.get_access_token(code)
    except spotipy.SpotifyOauthError as e:
        sys.exit(f'Error exchanging Spotify token: {e}')

    return spotipy.Spotify(oauth_manager=credentials_manager)


def _save_tidal_session(session: tidalapi.Session):
    _ensure_cache_dir()
    with open(_TIDAL_SESSION, 'w') as f:
        yaml.dump({
            'session_id': session.session_id,
            'token_type': session.token_type,
            'access_token': session.access_token,
            'refresh_token': session.refresh_token,
        }, f)


def open_tidal_session(config=None) -> tidalapi.Session:
    _ensure_cache_dir()
    try:
        with open(_TIDAL_SESSION, 'r') as session_file:
            previous_session = yaml.safe_load(session_file)
    except OSError:
        previous_session = None

    session = tidalapi.Session(config=config) if config else tidalapi.Session()

    if previous_session:
        try:
            if session.load_oauth_session(
                token_type=previous_session['token_type'],
                access_token=previous_session['access_token'],
                refresh_token=previous_session['refresh_token'],
            ):
                _save_tidal_session(session)
                return session
        except Exception as e:
            print(f'Error loading previous Tidal session: {e}', file=sys.stderr)

    login, future = session.login_oauth()
    url = login.verification_uri_complete
    if not url.startswith('https://'):
        url = 'https://' + url
    print(f'Login with the browser: {url}', file=sys.stderr)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    future.result()
    _save_tidal_session(session)
    return session
