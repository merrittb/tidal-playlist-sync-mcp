import asyncio
from collections import Counter
import math
import random
import re
import sys
import yaml
import tidalapi
import spotipy

from fastmcp import FastMCP
from . import auth as _auth
from . import sync as _sync
from .tidalapi_patch import (
    add_multiple_tracks_to_playlist,
    clear_tidal_playlist,
    get_all_favorites,
    get_all_playlist_tracks,
    get_all_playlists,
)

mcp = FastMCP("Tidal")


class _StderrStdout:
    """Redirect print() to stderr while keeping .buffer pointed at the real stdout.

    stdio_server() (the MCP transport) captures sys.stdout.buffer when it starts,
    so the JSON stream must still flow through the real stdout file descriptor.
    Plain print() calls from tool handlers must not corrupt that stream.
    """
    def __init__(self, real_stdout):
        self.buffer = real_stdout.buffer
        self.encoding = real_stdout.encoding
        self.errors = getattr(real_stdout, 'errors', 'replace')
        self.name = '<stderr-redirect>'
        self.closed = False

    def write(self, s):
        sys.stderr.write(s)

    def flush(self):
        sys.stderr.flush()

    def fileno(self):
        return sys.stderr.fileno()

    def isatty(self):
        return False

    def readable(self):
        return False

    def writable(self):
        return True

# ---------------------------------------------------------------------------
# Audio analysis helpers
# ---------------------------------------------------------------------------

_KEY_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

_CAMELOT = {
    (0,1):'8B',  (1,1):'3B',  (2,1):'10B', (3,1):'5B',  (4,1):'12B', (5,1):'7B',
    (6,1):'2B',  (7,1):'9B',  (8,1):'4B',  (9,1):'11B', (10,1):'6B', (11,1):'1B',
    (0,0):'5A',  (1,0):'12A', (2,0):'7A',  (3,0):'2A',  (4,0):'9A',  (5,0):'4A',
    (6,0):'11A', (7,0):'6A',  (8,0):'1A',  (9,0):'8A',  (10,0):'3A', (11,0):'10A',
}

def _format_features(f: dict, name: str = None, artists: list = None) -> dict:
    key, mode = f.get('key', -1), f.get('mode', -1)
    result = {
        'spotify_id': f['id'],
        'bpm': round(f['tempo'], 1),
        'key': _KEY_NAMES[key] if 0 <= key <= 11 else 'unknown',
        'mode': 'major' if mode == 1 else 'minor' if mode == 0 else 'unknown',
        'camelot': _CAMELOT.get((key, mode), '?'),
        'energy': round(f['energy'], 3),
        'danceability': round(f['danceability'], 3),
        'valence': round(f['valence'], 3),
        'loudness_db': round(f['loudness'], 1),
        'time_signature': f['time_signature'],
    }
    if name is not None:
        result['name'] = name
    if artists is not None:
        result['artists'] = artists
    return result

async def _spotify_playlist_pages(spotify: spotipy.Spotify, playlist_id: str) -> list[dict]:
    """Collect all non-local tracks from a Spotify playlist across pages."""
    tracks = []
    page = await asyncio.to_thread(spotify.playlist_tracks, playlist_id, limit=100)
    while page:
        for item in page['items']:
            t = item.get('track')
            if t and not t.get('is_local') and t.get('id'):
                tracks.append(t)
        page = await asyncio.to_thread(spotify.next, page) if page.get('next') else None
    return tracks

async def _fetch_audio_features(spotify: spotipy.Spotify, ids: list[str]) -> list[dict]:
    """Fetch Spotify audio features in batches of 100, filtering None results."""
    results = []
    for i in range(0, len(ids), 100):
        batch = await asyncio.to_thread(spotify.audio_features, ids[i:i+100])
        results.extend(f for f in (batch or []) if f)
    return results

_spotify_session: spotipy.Spotify | None = None
_tidal_session: tidalapi.Session | None = None
_config: dict = {}
_sessions_initialized = False
_sessions_lock: asyncio.Lock | None = None


def _get_sessions_lock() -> asyncio.Lock:
    global _sessions_lock
    if _sessions_lock is None:
        _sessions_lock = asyncio.Lock()
    return _sessions_lock


async def _ensure_sessions() -> None:
    global _spotify_session, _tidal_session, _sessions_initialized
    if _sessions_initialized:
        return
    async with _get_sessions_lock():
        if _sessions_initialized:
            return
        print("Initializing Spotify session…", file=sys.stderr)
        _spotify_session = await asyncio.to_thread(_auth.open_spotify_session, _config["spotify"])
        print("Initializing Tidal session…", file=sys.stderr)
        _tidal_session = await asyncio.to_thread(_auth.open_tidal_session)
        if not _tidal_session.check_login():
            raise RuntimeError("Could not connect to Tidal")
        _sessions_initialized = True
        print("Sessions ready.", file=sys.stderr)


def _sessions() -> tuple[spotipy.Spotify, tidalapi.Session]:
    if _spotify_session is None or _tidal_session is None:
        raise RuntimeError("Sessions not initialized")
    return _spotify_session, _tidal_session


import functools as _ft


def _tool(fn):
    """Decorator: registers fn as an MCP tool and ensures sessions init before each call."""
    @_ft.wraps(fn)
    async def _wrapper(*args, **kwargs):
        await _ensure_sessions()
        if asyncio.iscoroutinefunction(fn):
            return await fn(*args, **kwargs)
        return await asyncio.to_thread(fn, *args, **kwargs)
    return mcp.tool()(_wrapper)


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

@_tool
async def search_tracks(query: str) -> list[dict]:
    """Search Tidal for tracks matching a free-text query. Returns up to 25 results."""
    _, tidal = _sessions()
    results = await asyncio.to_thread(tidal.search, query, models=[tidalapi.media.Track])
    return [
        {
            "id": t.id,
            "name": t.name,
            "artists": [a.name for a in t.artists],
            "album": t.album.name if t.album else None,
            "duration_seconds": t.duration,
            "isrc": getattr(t, "isrc", None),
            "url": getattr(t, "share_url", None),
        }
        for t in results.get("tracks", [])
    ]


@_tool
async def get_tidal_playlists() -> list[dict]:
    """List all Tidal playlists owned by the current user."""
    _, tidal = _sessions()
    playlists = await get_all_playlists(tidal.user)
    return [
        {
            "id": str(p.id),
            "name": p.name,
            "num_tracks": p.num_tracks,
            "url": getattr(p, "share_url", None) or getattr(p, "listen_url", None),
        }
        for p in playlists
    ]


@_tool
async def get_playlist_tracks(playlist_id: str) -> list[dict]:
    """Get all tracks in a Tidal playlist by its UUID."""
    _, tidal = _sessions()
    playlist = await asyncio.to_thread(tidal.playlist, playlist_id)
    tracks = await get_all_playlist_tracks(playlist)
    return [
        {
            "id": t.id,
            "name": t.name,
            "artists": [a.name for a in t.artists],
            "album": t.album.name if t.album else None,
            "duration_seconds": t.duration,
            "isrc": getattr(t, "isrc", None),
        }
        for t in tracks
    ]


@_tool
async def get_favorites() -> list[dict]:
    """Get all tracks in the user's Tidal favorites (liked songs), ordered by date added."""
    _, tidal = _sessions()
    tracks = await get_all_favorites(tidal.user.favorites, order="DATE")
    return [
        {
            "id": t.id,
            "name": t.name,
            "artists": [a.name for a in t.artists],
            "album": t.album.name if t.album else None,
            "duration_seconds": t.duration,
        }
        for t in tracks
    ]


@_tool
async def get_spotify_playlists() -> list[dict]:
    """List all Spotify playlists owned by the current user."""
    spotify, _ = _sessions()
    playlists = await _sync.get_playlists_from_spotify(spotify, _config)
    return [
        {
            "id": p["id"],
            "name": p["name"],
            "num_tracks": p["tracks"]["total"],
            "uri": p["uri"],
        }
        for p in playlists
    ]


# ---------------------------------------------------------------------------
# Audio analysis tools
# ---------------------------------------------------------------------------

@_tool
async def get_spotify_playlist_tracks(spotify_playlist_id: str) -> list[dict]:
    """Get all tracks in a Spotify playlist with Spotify IDs and ISRCs. Use this to get track IDs before calling get_audio_features or get_recommendations."""
    spotify, _ = _sessions()
    tracks = await _spotify_playlist_pages(spotify, spotify_playlist_id)
    return [
        {
            'spotify_id': t['id'],
            'name': t['name'],
            'artists': [a['name'] for a in t.get('artists', [])],
            'album': t['album']['name'] if t.get('album') else None,
            'isrc': t.get('external_ids', {}).get('isrc'),
            'duration_seconds': t.get('duration_ms', 0) // 1000,
        }
        for t in tracks
    ]


@_tool
async def get_audio_features(spotify_track_ids: list[str]) -> list[dict]:
    """Get BPM, key, Camelot code, energy, valence, and danceability for a list of Spotify track IDs."""
    spotify, _ = _sessions()
    features = await _fetch_audio_features(spotify, spotify_track_ids)
    return [_format_features(f) for f in features]


@_tool
async def analyze_playlist(spotify_playlist_id: str) -> dict:
    """Full audio analysis of a Spotify playlist: per-track BPM/key/energy/valence, summary stats, energy arc, key and Camelot distributions."""
    spotify, _ = _sessions()
    tracks = await _spotify_playlist_pages(spotify, spotify_playlist_id)
    ids = [t['id'] for t in tracks]
    features = await _fetch_audio_features(spotify, ids)
    track_map = {t['id']: t for t in tracks}

    analyzed = [
        _format_features(f, name=track_map[f['id']]['name'],
                         artists=[a['name'] for a in track_map[f['id']].get('artists', [])])
        for f in features if f['id'] in track_map
    ]

    if not analyzed:
        return {'error': 'No audio features available for this playlist'}

    bpms = [t['bpm'] for t in analyzed]
    energies = [t['energy'] for t in analyzed]
    valences = [t['valence'] for t in analyzed]
    danceabilities = [t['danceability'] for t in analyzed]

    return {
        'track_count': len(analyzed),
        'tracks': analyzed,
        'bpm': {'min': min(bpms), 'max': max(bpms), 'avg': round(sum(bpms) / len(bpms), 1)},
        'energy': {
            'min': round(min(energies), 3),
            'max': round(max(energies), 3),
            'avg': round(sum(energies) / len(energies), 3),
            'arc': energies,
        },
        'valence': {
            'min': round(min(valences), 3),
            'max': round(max(valences), 3),
            'avg': round(sum(valences) / len(valences), 3),
            'arc': valences,
        },
        'danceability': {'avg': round(sum(danceabilities) / len(danceabilities), 3)},
        'key_distribution': dict(Counter(f"{t['key']} {t['mode']}" for t in analyzed).most_common()),
        'camelot_distribution': dict(Counter(t['camelot'] for t in analyzed).most_common()),
    }


@_tool
async def get_recommendations(
    seed_spotify_track_ids: list[str],
    limit: int = 20,
    target_bpm: float | None = None,
    min_bpm: float | None = None,
    max_bpm: float | None = None,
    target_energy: float | None = None,
    target_valence: float | None = None,
    target_danceability: float | None = None,
) -> list[dict]:
    """Get Spotify track recommendations seeded from up to 5 track IDs. Audio features (energy, valence, danceability) are 0.0–1.0; BPM uses actual BPM values. Returns tracks with full audio features included."""
    spotify, _ = _sessions()
    kwargs: dict = {'limit': min(limit, 100), 'seed_tracks': seed_spotify_track_ids[:5]}
    if target_bpm is not None: kwargs['target_tempo'] = target_bpm
    if min_bpm is not None:    kwargs['min_tempo'] = min_bpm
    if max_bpm is not None:    kwargs['max_tempo'] = max_bpm
    if target_energy is not None:      kwargs['target_energy'] = target_energy
    if target_valence is not None:     kwargs['target_valence'] = target_valence
    if target_danceability is not None: kwargs['target_danceability'] = target_danceability

    results = await asyncio.to_thread(spotify.recommendations, **kwargs)
    rec_tracks = results.get('tracks', [])
    ids = [t['id'] for t in rec_tracks]
    features = await _fetch_audio_features(spotify, ids)
    features_map = {f['id']: f for f in features}

    return [
        {
            'name': t['name'],
            'artists': [a['name'] for a in t.get('artists', [])],
            'album': t['album']['name'] if t.get('album') else None,
            'preview_url': t.get('preview_url'),
            **(_format_features(features_map[t['id']]) if t['id'] in features_map else {'spotify_id': t['id']}),
        }
        for t in rec_tracks
    ]


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------

@_tool
def create_playlist(name: str, description: str = "") -> dict:
    """Create a new Tidal playlist and return its ID and share URL."""
    _, tidal = _sessions()
    playlist = tidal.user.create_playlist(name, description)
    return {
        "id": str(playlist.id),
        "name": playlist.name,
        "url": getattr(playlist, "share_url", None) or getattr(playlist, "listen_url", None),
    }


@_tool
def add_tracks_to_playlist(playlist_id: str, track_ids: list[int]) -> dict:
    """Append tracks to an existing Tidal playlist. track_ids are Tidal integer track IDs."""
    _, tidal = _sessions()
    playlist = tidal.playlist(playlist_id)
    add_multiple_tracks_to_playlist(playlist, track_ids)
    return {"added": len(track_ids), "playlist": playlist.name}


@_tool
def add_to_favorites(track_id: int) -> dict:
    """Add a single track to the user's Tidal favorites by Tidal track ID."""
    _, tidal = _sessions()
    tidal.user.favorites.add_track(track_id)
    return {"added_track_id": track_id}


# ---------------------------------------------------------------------------
# Sync tools
# ---------------------------------------------------------------------------

@_tool
async def sync_playlist(spotify_playlist_id: str) -> dict:
    """Sync a single Spotify playlist to Tidal. Creates the Tidal playlist if it doesn't exist."""
    spotify, tidal = _sessions()
    spotify_playlist = await asyncio.to_thread(spotify.playlist, spotify_playlist_id)
    # Fetch Tidal playlists directly (avoids asyncio.run inside async context)
    tidal_playlists_list = await get_all_playlists(tidal.user)
    tidal_playlists = {p.name: p for p in tidal_playlists_list}
    _, tidal_playlist = _sync.pick_tidal_playlist_for_spotify_playlist(spotify_playlist, tidal_playlists)
    created = await _sync.sync_playlist(spotify, tidal, spotify_playlist, tidal_playlist, _config)
    return {"synced": spotify_playlist["name"], "created_new": created}


@_tool
async def sync_favorites() -> dict:
    """Sync Spotify liked songs to Tidal favorites."""
    spotify, tidal = _sessions()
    await _sync.sync_favorites(spotify, tidal, _config)
    return {"status": "favorites synced"}


# ---------------------------------------------------------------------------
# DJ ordering helpers
# ---------------------------------------------------------------------------

def _parse_track_artists(track: dict) -> tuple[frozenset, bool]:
    """Return (all_credited_artists_lowercase, is_remix).
    Spotify includes featured artists in the artists array, but remixers often
    only appear in the track name as '(Artist X Remix)' — we extract those too.
    """
    artists = {a['name'].lower() for a in track.get('artists', [])}
    name = track.get('name', '')
    is_remix = bool(re.search(r'\bremix\b', name, re.IGNORECASE))
    remix_match = re.search(r'\(([^)]+?)\s+remix\)', name, re.IGNORECASE)
    if remix_match:
        for part in re.split(r'\s*[,&x]\s*', remix_match.group(1)):
            artists.add(part.strip().lower())
    return frozenset(artists), is_remix


def _camelot_compat(a: str, b: str) -> float:
    """Harmonic compatibility score 0–1 using the Camelot wheel."""
    if '?' in (a, b):
        return 0.5
    if a == b:
        return 1.0
    num_a, num_b = int(a[:-1]), int(b[:-1])
    wheel_dist = min(abs(num_a - num_b), 12 - abs(num_a - num_b))
    if num_a == num_b:          # relative major/minor
        return 0.9
    if wheel_dist == 1 and a[-1] == b[-1]:
        return 0.8
    if wheel_dist <= 2:
        return 0.4
    return 0.1


def _target_energy(pos: int, total: int, strategy: str) -> float:
    """Target energy value at playlist position pos (0-indexed) for a given strategy."""
    t = pos / max(total - 1, 1)
    if strategy == 'flow':
        # Gentle sine arc: open warm, peak at 60%, cool down
        return 0.45 + 0.35 * math.sin(math.pi * t)
    if strategy == 'party':
        # Build to peak, hold, breather around 75%, strong finish
        if t < 0.30: return 0.55 + 0.35 * (t / 0.30)
        if t < 0.70: return 0.90
        if t < 0.80: return 0.65
        return 0.85
    if strategy == 'vibe':
        # Slow emotional build — valence matters more than energy here
        return 0.35 + 0.40 * t
    return 0.65


def _score_transition(f_from: dict, f_to: dict, artist_score: float,
                      pos: int, total: int, strategy: str) -> float:
    camelot  = _camelot_compat(f_from['camelot'], f_to['camelot'])
    bpm_prox = max(0.0, 1.0 - abs(f_from['bpm'] - f_to['bpm']) / 25.0)
    target   = _target_energy(pos, total, strategy)
    arc_fit  = max(0.0, 1.0 - abs(f_to['energy'] - target) * 2.5)
    smoothness = max(0.0, 1.0 - abs(f_from['energy'] - f_to['energy']) / 0.4)
    return (
        artist_score * 3.5 +
        camelot      * 2.0 +
        bpm_prox     * 1.5 +
        arc_fit      * 1.2 +
        smoothness   * 0.8
    )


# ---------------------------------------------------------------------------
# Connection detection helpers
# ---------------------------------------------------------------------------

def _credited_artists(track: dict) -> list[dict]:
    """Return all artists credited on a track as {name, role} dicts.

    Spotify's artists array includes featured artists inconsistently, so we also
    parse feat./ft./featuring from the track name and extract remixers from
    '(Artist X Remix)' patterns.
    """
    name = track.get('name', '')
    primary_names = {a['name'] for a in track.get('artists', [])}
    credited = [{'name': a['name'], 'role': 'primary'} for a in track.get('artists', [])]

    # Featured artists sometimes only appear in the track title
    feat_match = re.search(r'\((?:feat\.?|ft\.?|featuring)\s+([^)]+)\)', name, re.IGNORECASE)
    if feat_match:
        for part in re.split(r'\s*[,&]\s*', feat_match.group(1)):
            feat_name = part.strip()
            if feat_name and feat_name not in primary_names:
                credited.append({'name': feat_name, 'role': 'featured'})
                primary_names.add(feat_name)

    remix_match = re.search(r'\(([^)]+?)\s+remix\)', name, re.IGNORECASE)
    if remix_match:
        for part in re.split(r'\s*[,&x]\s*', remix_match.group(1)):
            remixer = part.strip()
            if remixer and remixer not in primary_names:
                credited.append({'name': remixer, 'role': 'remixer'})

    return credited


def _base_title(name: str) -> str:
    """Strip remaster/live/remix/feat suffixes to get a comparable song identity."""
    name = re.sub(
        r'\s*[\(\[](feat\.?|ft\.?|featuring)[^\)\]]*[\)\]]', '', name, flags=re.IGNORECASE
    )
    name = re.sub(
        r'\s*[\(\[][^\)\]]*(remix|remaster|remastered|live|demo|acoustic|'
        r'radio edit|single version|album version|original mix|\d{4}\s*(?:remaster|mix))[^\)\]]*[\)\]]',
        '', name, flags=re.IGNORECASE,
    )
    return name.strip().lower()


# ---------------------------------------------------------------------------
# find_connections tool
# ---------------------------------------------------------------------------

@_tool
async def find_connections(spotify_playlist_id: str) -> dict:
    """Analyze a Spotify playlist for musically meaningful track adjacencies.

    Detects structurally (from metadata alone):
    - shared_artists: guest vocalists/musicians/remixers appearing on multiple tracks
    - possible_covers: same normalized title by different primary artists
    - remix_pairs: original + remix of the same song both present in the playlist
    - remixer_as_artist: a track remixed by someone who also has their own track here

    Also returns a full track_list with all credited artists so Claude can apply its
    own knowledge to identify cover/tribute/band-lineage connections not visible in metadata.

    Use the returned spotify_ids to populate known_connections in reorder_playlist_for_dj.
    """
    spotify, _ = _sessions()
    raw_tracks = await _spotify_playlist_pages(spotify, spotify_playlist_id)

    # Build enriched track records
    track_list = []
    for t in raw_tracks:
        credited = _credited_artists(t)
        track_list.append({
            'spotify_id': t['id'],
            'name': t['name'],
            'base_title': _base_title(t['name']),
            'primary_artists': [c['name'] for c in credited if c['role'] == 'primary'],
            'featured_artists': [c['name'] for c in credited if c['role'] == 'featured'],
            'remixers': [c['name'] for c in credited if c['role'] == 'remixer'],
            'is_remix': bool(re.search(r'\bremix\b', t.get('name', ''), re.IGNORECASE)),
            'album': t['album']['name'] if t.get('album') else None,
        })

    # Index by spotify_id for lookups
    by_id = {t['spotify_id']: t for t in track_list}

    # --- 1. Shared artists: any artist (featured, remixer) on 2+ tracks ---
    artist_index: dict[str, list[dict]] = {}  # lowercase -> [{spotify_id, name, role, display_name}]
    for t in track_list:
        primary_lower = {a.lower() for a in t['primary_artists']}
        for role_key in ('primary_artists', 'featured_artists', 'remixers'):
            role = role_key.rstrip('s').replace('_artist', '')  # primary, featured, remixer
            for a in t[role_key]:
                key = a.lower()
                artist_index.setdefault(key, []).append({
                    'spotify_id': t['spotify_id'],
                    'track': t['name'],
                    'by': ', '.join(t['primary_artists']),
                    'role': role,
                    'display_name': a,
                })

    shared_artists = []
    for key, appearances in artist_index.items():
        track_ids = {a['spotify_id'] for a in appearances}
        if len(track_ids) < 2:
            continue
        roles = {a['role'] for a in appearances}
        # Only report if the artist appears in a non-primary role on at least one track
        # (same primary artist across their own songs is expected, not a connection)
        if roles == {'primary'}:
            continue
        display = appearances[0]['display_name']
        shared_artists.append({
            'artist': display,
            'connection_type': 'remix' if 'remixer' in roles else 'collaboration',
            'tracks': [
                {'spotify_id': a['spotify_id'], 'name': a['track'], 'by': a['by'], 'role': a['role']}
                for a in appearances
            ],
            'suggested_pair': [appearances[0]['spotify_id'], appearances[1]['spotify_id']],
        })

    # --- 2. Possible covers: same base title, different primary artist ---
    title_index: dict[str, list[dict]] = {}
    for t in track_list:
        title_index.setdefault(t['base_title'], []).append(t)

    possible_covers = []
    for base, group in title_index.items():
        if len(group) < 2:
            continue
        primary_sets = [frozenset(t['primary_artists']) for t in group]
        if len({frozenset(p) for p in primary_sets}) < 2:
            continue  # same artist, just alternate versions
        possible_covers.append({
            'normalized_title': base,
            'tracks': [
                {'spotify_id': t['spotify_id'], 'name': t['name'], 'by': ', '.join(t['primary_artists'])}
                for t in group
            ],
            'suggested_pairs': [[group[i]['spotify_id'], group[j]['spotify_id']]
                                 for i in range(len(group)) for j in range(i+1, len(group))],
        })

    # --- 3. Remix pairs: original + remix of the same song both in playlist ---
    originals = {t['base_title']: t for t in track_list if not t['is_remix']}
    remix_pairs = []
    for t in track_list:
        if not t['is_remix']:
            continue
        original = originals.get(t['base_title'])
        if original and original['spotify_id'] != t['spotify_id']:
            remix_pairs.append({
                'original': {'spotify_id': original['spotify_id'], 'name': original['name'],
                             'by': ', '.join(original['primary_artists'])},
                'remix':    {'spotify_id': t['spotify_id'], 'name': t['name'],
                             'by': ', '.join(t['primary_artists']), 'remixers': t['remixers']},
                'suggested_pair': [original['spotify_id'], t['spotify_id']],
            })

    # --- 4. Remixer-as-artist: remixer of track A is primary artist on track B ---
    primary_artist_lower = {
        a.lower(): t['spotify_id']
        for t in track_list
        for a in t['primary_artists']
    }
    remixer_as_artist = []
    for t in track_list:
        for remixer in t['remixers']:
            other_id = primary_artist_lower.get(remixer.lower())
            if other_id and other_id != t['spotify_id']:
                remixer_as_artist.append({
                    'artist': remixer,
                    'their_track': {'spotify_id': other_id, 'name': by_id[other_id]['name'],
                                    'by': ', '.join(by_id[other_id]['primary_artists'])},
                    'remix_track': {'spotify_id': t['spotify_id'], 'name': t['name'],
                                    'by': ', '.join(t['primary_artists'])},
                    'suggested_pair': [other_id, t['spotify_id']],
                })

    return {
        'track_list': [
            {
                'spotify_id': t['spotify_id'],
                'name': t['name'],
                'primary_artists': t['primary_artists'],
                'featured_artists': t['featured_artists'],
                'remixers': t['remixers'],
                'album': t['album'],
            }
            for t in track_list
        ],
        'shared_artists': shared_artists,
        'possible_covers': possible_covers,
        'remix_pairs': remix_pairs,
        'remixer_as_artist': remixer_as_artist,
        'summary': {
            'total_tracks': len(track_list),
            'shared_artist_connections': len(shared_artists),
            'possible_cover_groups': len(possible_covers),
            'remix_pairs': len(remix_pairs),
            'remixer_as_artist_links': len(remixer_as_artist),
        },
        'instructions_for_claude': (
            'Review the track_list and use your knowledge to identify additional connections '
            'not visible in metadata: covers/tributes, band-lineage links (member solo careers, '
            'side projects, successor bands), same-song different-era versions, and any artist '
            'who appears in another track\'s title or story. Add those as [spotify_id_a, spotify_id_b] '
            'pairs alongside the suggested_pair entries above when calling reorder_playlist_for_dj.'
        ),
    }


# ---------------------------------------------------------------------------
# DJ reorder tool
# ---------------------------------------------------------------------------

@_tool
async def reorder_playlist_for_dj(
    tidal_playlist_id: str,
    spotify_playlist_id: str,
    strategy: str = "flow",
    known_connections: list[list[str]] | None = None,
    pinned_tracks: dict[str, str] | None = None,
    change_up_probability: float = 0.12,
    dry_run: bool = False,
) -> dict:
    """Reorder a Tidal playlist for optimal listening flow.

    Layers of logic (highest priority first):
    1. Artist connections — shared featured artists, remixers from track name, or user-supplied
       cover/connection pairs. These create 'easter egg' transitions for attentive listeners.
    2. Harmonic mixing — Camelot wheel compatibility to avoid key clashes.
    3. BPM proximity — smooth tempo transitions.
    4. Energy arc — shaped by strategy: 'flow' (sine arc), 'party' (build→peak→breather→finish),
       'vibe' (slow emotional build).
    5. Occasional change-ups — skips the optimal pick ~12% of the time for variety.

    known_connections: list of [spotify_id_a, spotify_id_b] pairs that must be adjacent.
      Use this for covers, alternate versions, or any pair Claude identifies as meaningfully linked.
    pinned_tracks: dict of {"position": "spotify_id"} locking specific tracks to specific
      0-indexed positions before the algorithm runs. Use after a dry_run review to anchor
      specific opener, closer, or mid-set moments. The algorithm fills in everything else
      optimally around the pins.
    dry_run: return suggested order without modifying the Tidal playlist.
    """
    spotify, tidal = _sessions()

    # --- Gather data ---
    tidal_playlist = await asyncio.to_thread(tidal.playlist, tidal_playlist_id)
    tidal_tracks = await get_all_playlist_tracks(tidal_playlist)
    isrc_to_tidal = {t.isrc: t.id for t in tidal_tracks if t.isrc}

    spotify_tracks = await _spotify_playlist_pages(spotify, spotify_playlist_id)
    matched = [t for t in spotify_tracks
               if t.get('external_ids', {}).get('isrc') in isrc_to_tidal]

    if not matched:
        return {'error': 'No tracks matched between the two playlists via ISRC'}

    raw_features = await _fetch_audio_features(spotify, [t['id'] for t in matched])
    features_map = {f['id']: _format_features(f) for f in raw_features}
    matched = [t for t in matched if t['id'] in features_map]

    if not matched:
        return {'error': 'Spotify returned no audio features for any matched track'}

    # --- Build artist connection graph ---
    track_artists: dict[str, frozenset] = {}
    track_is_remix: dict[str, bool] = {}
    for t in matched:
        artists, is_remix = _parse_track_artists(t)
        track_artists[t['id']] = artists
        track_is_remix[t['id']] = is_remix

    forced: set[frozenset] = {frozenset(p) for p in (known_connections or []) if len(p) == 2}

    def artist_connection_score(id_a: str, id_b: str) -> float:
        if frozenset([id_a, id_b]) in forced:
            return 5.0  # cover / user-specified: trump everything
        shared = track_artists[id_a] & track_artists[id_b]
        if not shared:
            return 0.0
        return 2.5 if (track_is_remix[id_a] or track_is_remix[id_b]) else 2.0

    # --- Greedy nearest-neighbour ordering ---
    total = len(matched)
    by_id = {t['id']: t for t in matched}
    remaining = {t['id'] for t in matched}
    # Pins: position (0-indexed int) → spotify_id; skip invalid IDs silently
    pins: dict[int, str] = {
        int(k): v for k, v in (pinned_tracks or {}).items()
        if v in remaining
    }

    def opener_score(tid: str) -> float:
        f = features_map[tid]
        return -abs(f['energy'] - 0.55) - abs(f['bpm'] - 115) / 30.0

    # Position 0: use pin if provided, else auto-select a warm opener
    if 0 in pins:
        current = pins[0]
    else:
        current = max(remaining, key=opener_score)
    ordered = [current]
    remaining.remove(current)
    transitions = []

    while remaining:
        pos = len(ordered)

        # Honor pin at this position if the pinned track is still available
        if pos in pins and pins[pos] in remaining:
            pick = pins[pos]
            note = 'pinned'
        else:
            scores = {
                cid: _score_transition(
                    features_map[current], features_map[cid],
                    artist_connection_score(current, cid),
                    pos, total, strategy,
                )
                for cid in remaining
            }
            ranked = sorted(remaining, key=lambda x: scores[x], reverse=True)

            if len(ranked) > 2 and random.random() < change_up_probability:
                pick = random.choice(ranked[1:3])
                note = 'change-up'
            else:
                pick = ranked[0]
                ac = artist_connection_score(current, pick)
                if ac >= 5.0:
                    note = 'cover/connection'
                elif ac >= 2.0:
                    note = 'artist connection'
                elif _camelot_compat(features_map[current]['camelot'], features_map[pick]['camelot']) >= 0.8:
                    note = 'harmonic'
                else:
                    note = 'flow'

        fc, fp = features_map[current], features_map[pick]
        transitions.append({
            'from': by_id[current]['name'],
            'to': by_id[pick]['name'],
            'type': note,
            'bpm_delta': round(fp['bpm'] - fc['bpm'], 1),
            'camelot': f"{fc['camelot']}→{fp['camelot']}",
            'energy_delta': round(fp['energy'] - fc['energy'], 3),
        })
        ordered.append(pick)
        remaining.remove(pick)
        current = pick

    # --- Build result ---
    def tidal_id(spotify_track: dict) -> int | None:
        return isrc_to_tidal.get(spotify_track.get('external_ids', {}).get('isrc'))

    ordered_tracks = [
        {
            'position': i + 1,
            'name': by_id[sid]['name'],
            'artists': [a['name'] for a in by_id[sid].get('artists', [])],
            'bpm': features_map[sid]['bpm'],
            'camelot': features_map[sid]['camelot'],
            'energy': features_map[sid]['energy'],
            'tidal_id': tidal_id(by_id[sid]),
        }
        for i, sid in enumerate(ordered)
    ]

    result: dict = {
        'strategy': strategy,
        'track_count': len(ordered_tracks),
        'tracks': ordered_tracks,
        'transitions': transitions,
        'artist_connections_found': sum(
            1 for t in transitions if t['type'] in ('artist connection', 'cover/connection')
        ),
    }

    if not dry_run:
        tidal_ids = [tid for tid in (tidal_id(by_id[sid]) for sid in ordered) if tid is not None]
        await asyncio.to_thread(clear_tidal_playlist, tidal_playlist)
        await asyncio.to_thread(add_multiple_tracks_to_playlist, tidal_playlist, tidal_ids)
        result['written_to_tidal'] = True
    else:
        result['written_to_tidal'] = False

    return result


# ---------------------------------------------------------------------------
# Transition analysis tool
# ---------------------------------------------------------------------------

@_tool
async def analyze_transitions(
    tidal_playlist_id: str,
    spotify_playlist_id: str,
    known_connections: list[list[str]] | None = None,
) -> dict:
    """Score every consecutive transition in the current Tidal playlist order.

    Flags weak transitions (key clash, BPM spike, energy lurch) and missed
    connection opportunities where two connected tracks aren't adjacent.
    Use this after reorder_playlist_for_dj or on any existing playlist to find
    rough edges. Returns flagged transitions with specific fix suggestions.

    known_connections: same format as reorder_playlist_for_dj — pass the same
      list so missed connections can be detected.
    """
    spotify, tidal = _sessions()

    # Get current Tidal order (preserving sequence)
    tidal_playlist = await asyncio.to_thread(tidal.playlist, tidal_playlist_id)
    tidal_tracks = await get_all_playlist_tracks(tidal_playlist)
    isrc_sequence = [t.isrc for t in tidal_tracks if t.isrc]
    isrc_to_tidal = {t.isrc: t.id for t in tidal_tracks if t.isrc}

    # Match to Spotify tracks via ISRC to get audio features
    spotify_tracks = await _spotify_playlist_pages(spotify, spotify_playlist_id)
    isrc_to_spotify = {
        t.get('external_ids', {}).get('isrc'): t
        for t in spotify_tracks
        if t.get('external_ids', {}).get('isrc')
    }

    ordered_spotify = [isrc_to_spotify[isrc] for isrc in isrc_sequence if isrc in isrc_to_spotify]
    if not ordered_spotify:
        return {'error': 'No tracks matched between playlists via ISRC'}

    raw_features = await _fetch_audio_features(spotify, [t['id'] for t in ordered_spotify])
    features_map = {f['id']: _format_features(f) for f in raw_features}

    # Build connection awareness
    forced: set[frozenset] = {frozenset(p) for p in (known_connections or []) if len(p) == 2}
    track_artists: dict[str, frozenset] = {}
    track_is_remix: dict[str, bool] = {}
    for t in ordered_spotify:
        artists, is_remix = _parse_track_artists(t)
        track_artists[t['id']] = artists
        track_is_remix[t['id']] = is_remix

    def has_connection(id_a: str, id_b: str) -> str | None:
        if frozenset([id_a, id_b]) in forced:
            return 'known_connection'
        shared = track_artists.get(id_a, frozenset()) & track_artists.get(id_b, frozenset())
        if shared:
            return 'artist_overlap'
        return None

    # Score every consecutive pair
    total = len(ordered_spotify)
    all_transitions = []
    weak = []
    missed_connections = []

    connected_ids = {frozenset(p) for p in (known_connections or []) if len(p) == 2}
    placed_ids = [t['id'] for t in ordered_spotify if t['id'] in features_map]

    for i in range(len(placed_ids) - 1):
        id_a, id_b = placed_ids[i], placed_ids[i + 1]
        if id_a not in features_map or id_b not in features_map:
            continue
        fa, fb = features_map[id_a], features_map[id_b]
        name_a = ordered_spotify[i]['name']
        name_b = ordered_spotify[i + 1]['name']

        camelot_score = _camelot_compat(fa['camelot'], fb['camelot'])
        bpm_delta = abs(fa['bpm'] - fb['bpm'])
        energy_delta = abs(fa['energy'] - fb['energy'])
        connection = has_connection(id_a, id_b)

        issues = []
        if camelot_score < 0.4:
            issues.append(f"key clash ({fa['camelot']}→{fb['camelot']})")
        if bpm_delta > 25:
            issues.append(f"BPM jump ({fa['bpm']}→{fb['bpm']}, Δ{bpm_delta:.0f})")
        if energy_delta > 0.35:
            issues.append(f"energy lurch (Δ{energy_delta:.2f})")

        entry = {
            'position': i + 1,
            'from': name_a,
            'to': name_b,
            'camelot': f"{fa['camelot']}→{fb['camelot']}",
            'bpm_delta': round(fb['bpm'] - fa['bpm'], 1),
            'energy_delta': round(fb['energy'] - fa['energy'], 3),
            'connection': connection,
            'issues': issues,
        }
        all_transitions.append(entry)
        if issues:
            weak.append(entry)

    # Find connected pairs that aren't adjacent
    id_positions = {sid: i for i, sid in enumerate(placed_ids)}
    for pair in connected_ids:
        pair_list = list(pair)
        if len(pair_list) != 2:
            continue
        id_a, id_b = pair_list
        if id_a not in id_positions or id_b not in id_positions:
            continue
        dist = abs(id_positions[id_a] - id_positions[id_b])
        if dist > 1:
            name_a = ordered_spotify[id_positions[id_a]]['name'] if id_a in id_positions else id_a
            name_b = ordered_spotify[id_positions[id_b]]['name'] if id_b in id_positions else id_b
            missed_connections.append({
                'track_a': {'position': id_positions[id_a] + 1, 'name': name_a},
                'track_b': {'position': id_positions[id_b] + 1, 'name': name_b},
                'distance': dist,
                'suggestion': f"Move one of these to be adjacent (currently {dist} apart)",
            })

    overall_score = round(
        sum(
            _camelot_compat(features_map[placed_ids[i]]['camelot'],
                            features_map[placed_ids[i+1]]['camelot'])
            for i in range(len(placed_ids) - 1)
            if placed_ids[i] in features_map and placed_ids[i+1] in features_map
        ) / max(len(placed_ids) - 1, 1),
        3,
    )

    return {
        'total_transitions': len(all_transitions),
        'weak_transitions': weak,
        'weak_count': len(weak),
        'missed_connections': missed_connections,
        'overall_harmonic_score': overall_score,
        'summary': (
            f"{len(weak)} weak transitions out of {len(all_transitions)}; "
            f"{len(missed_connections)} known connection(s) not adjacent; "
            f"average harmonic score {overall_score:.2f}/1.0"
        ),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    global _spotify_session, _tidal_session, _config

    # Redirect print() to stderr for the entire process lifetime so that log
    # output from tool handlers never corrupts the MCP JSON stream on stdout.
    # _StderrStdout keeps .buffer pointing at the real stdout so that
    # stdio_server() (the MCP transport) still writes JSON to the right fd.
    sys.stdout = _StderrStdout(sys.stdout)

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yml", help="path to config.yml")
    args = parser.parse_args()
    with open(args.config, "r") as f:
        _config = yaml.safe_load(f)
    print("Config loaded — starting MCP server (sessions init on first tool call)", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
