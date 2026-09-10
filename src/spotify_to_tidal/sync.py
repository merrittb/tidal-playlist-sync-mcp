#!/usr/bin/env python3

import asyncio
from .cache import failure_cache, track_match_cache
import datetime
from difflib import SequenceMatcher
from functools import partial
from typing import Callable, List, Sequence, Set, Mapping
import math
import requests
import sys
import spotipy
import tidalapi
from .tidalapi_patch import add_multiple_tracks_to_playlist, clear_tidal_playlist, get_all_favorites, get_all_playlists, get_all_playlist_tracks
import time
from tqdm.asyncio import tqdm as atqdm
from tqdm import tqdm
import traceback
import unicodedata

from .type import spotify as t_spotify

def _extract_track(item: dict):
    track = item.get('track')
    if track is None:
        track = item.get('item')
        if isinstance(track, dict) and 'track' in track:
            track = track['track'] if isinstance(track['track'], dict) else (None if len(track) <= 1 else track)
    return track

def normalize(s) -> str:
    return unicodedata.normalize('NFD', s).encode('ascii', 'ignore').decode('ascii')

def simple(input_string: str) -> str:
    # only take the first part of a string before any hyphens or brackets to account for different versions
    return input_string.split('-')[0].strip().split('(')[0].strip().split('[')[0].strip()

def isrc_match(tidal_track: tidalapi.Track, spotify_track) -> bool:
    if "isrc" in spotify_track["external_ids"]:
        return tidal_track.isrc == spotify_track["external_ids"]["isrc"]
    return False

def duration_match(tidal_track: tidalapi.Track, spotify_track, tolerance=2) -> bool:
    # the duration of the two tracks must be the same to within 2 seconds
    return abs(tidal_track.duration - spotify_track['duration_ms']/1000) < tolerance

def name_match(tidal_track, spotify_track) -> bool:
    def exclusion_rule(pattern: str, tidal_track: tidalapi.Track, spotify_track: t_spotify.SpotifyTrack):
        spotify_has_pattern = pattern in spotify_track['name'].lower()
        tidal_has_pattern = pattern in tidal_track.name.lower() or (not tidal_track.version is None and (pattern in tidal_track.version.lower()))
        return spotify_has_pattern != tidal_has_pattern

    # handle some edge cases
    if exclusion_rule("instrumental", tidal_track, spotify_track): return False
    if exclusion_rule("acapella", tidal_track, spotify_track): return False
    if exclusion_rule("remix", tidal_track, spotify_track): return False

    # the simplified version of the Spotify track name must be a substring of the Tidal track name
    # Try with both un-normalized and then normalized
    simple_spotify_track = simple(spotify_track['name'].lower()).split('feat.')[0].strip()
    return simple_spotify_track in tidal_track.name.lower() or normalize(simple_spotify_track) in normalize(tidal_track.name.lower())

def artist_match(tidal: tidalapi.Track | tidalapi.Album, spotify) -> bool:
    def split_artist_name(artist: str) -> Sequence[str]:
       if '&' in artist:
           return artist.split('&')
       elif ',' in artist:
           return artist.split(',')
       else:
           return [artist]

    def get_tidal_artists(tidal: tidalapi.Track | tidalapi.Album, do_normalize=False) -> Set[str]:
        result: list[str] = []
        for artist in tidal.artists:
            if do_normalize:
                artist_name = normalize(artist.name)
            else:
                artist_name = artist.name
            result.extend(split_artist_name(artist_name))
        return set([simple(x.strip().lower()) for x in result])

    def get_spotify_artists(spotify, do_normalize=False) -> Set[str]:
        result: list[str] = []
        for artist in spotify['artists']:
            if do_normalize:
                artist_name = normalize(artist['name'])
            else:
                artist_name = artist['name']
            result.extend(split_artist_name(artist_name))
        return set([simple(x.strip().lower()) for x in result])
    # There must be at least one overlapping artist between the Tidal and Spotify track
    # Try with both un-normalized and then normalized
    if get_tidal_artists(tidal).intersection(get_spotify_artists(spotify)) != set():
        return True
    return get_tidal_artists(tidal, True).intersection(get_spotify_artists(spotify, True)) != set()

def match(tidal_track, spotify_track) -> bool:
    if not spotify_track['id']: return False
    return isrc_match(tidal_track, spotify_track) or (
        duration_match(tidal_track, spotify_track)
        and name_match(tidal_track, spotify_track)
        and artist_match(tidal_track, spotify_track)
    )

def test_album_similarity(spotify_album, tidal_album, threshold=0.6):
    return SequenceMatcher(None, simple(spotify_album['name']), simple(tidal_album.name)).ratio() >= threshold and artist_match(tidal_album, spotify_album)

async def tidal_search(spotify_track, rate_limiter, tidal_session: tidalapi.Session) -> tidalapi.Track | None:
    def _search_for_track_in_album():
        # search for album name and first album artist
        if 'album' in spotify_track and 'artists' in spotify_track['album'] and len(spotify_track['album']['artists']):
            query = simple(spotify_track['album']['name']) + " " + simple(spotify_track['album']['artists'][0]['name'])
            album_result = tidal_session.search(query, models=[tidalapi.album.Album])
            for album in album_result['albums']:
                if album.num_tracks >= spotify_track['track_number'] and test_album_similarity(spotify_track['album'], album):
                    album_tracks = album.tracks()
                    if len(album_tracks) < spotify_track['track_number']:
                        if len(album_tracks) != album.num_tracks:
                            print(f"Tidal album '{album.name}' has mismatched track count (reported {album.num_tracks}, got {len(album_tracks)})")
                        continue
                    track = album_tracks[spotify_track['track_number'] - 1]
                    if match(track, spotify_track):
                        failure_cache.remove_match_failure(spotify_track['id'])
                        return track

    def _search_for_standalone_track():
        # if album search fails then search for track name and first artist
        query = simple(spotify_track['name']) + ' ' + simple(spotify_track['artists'][0]['name'])
        for track in tidal_session.search(query, models=[tidalapi.media.Track])['tracks']:
            if match(track, spotify_track):
                failure_cache.remove_match_failure(spotify_track['id'])
                return track
    await rate_limiter.acquire()
    album_search = await asyncio.to_thread( _search_for_track_in_album )
    if album_search:
        return album_search
    await rate_limiter.acquire()
    track_search = await asyncio.to_thread( _search_for_standalone_track )
    if track_search:
        return track_search

    # if none of the search modes succeeded then store the track id to the failure cache
    failure_cache.cache_match_failure(spotify_track['id'])

async def repeat_on_request_error(function, *args, remaining=5, **kwargs):
    # utility to repeat calling the function up to 5 times if an exception is thrown
    try:
        return await function(*args, **kwargs)
    except (tidalapi.exceptions.TooManyRequests, requests.exceptions.RequestException, spotipy.exceptions.SpotifyException) as e:
        if remaining:
            print(f"{str(e)} occurred, retrying {remaining} times")
        else:
            print(f"{str(e)} could not be recovered")

        if isinstance(e, requests.exceptions.RequestException) and not e.response is None:
            print(f"Response message: {e.response.text}")
            print(f"Response headers: {e.response.headers}")

        if not remaining:
            print("Aborting sync")
            print(f"The following arguments were provided:\n\n {str(args)}")
            print(traceback.format_exc())
            sys.exit(1)
        sleep_schedule = {5: 1, 4:10, 3:60, 2:5*60, 1:10*60} # sleep variable length of time depending on retry number
        time.sleep(sleep_schedule.get(remaining, 1))
        return await repeat_on_request_error(function, *args, remaining=remaining-1, **kwargs)


async def _fetch_all_from_spotify_in_chunks(fetch_function: Callable) -> List[dict]:
    output = []
    results = fetch_function(0)
    for item in results['items']:
        track = _extract_track(item)
        if track is not None:
            output.append(track)

    # Get all the remaining tracks in parallel
    if results['next']:
        offsets = [results['limit'] * n for n in range(1, math.ceil(results['total'] / results['limit']))]
        extra_results = await atqdm.gather(
            *[asyncio.to_thread(fetch_function, offset) for offset in offsets],
            desc="Fetching additional data chunks"
        )
        for extra_result in extra_results:
            for item in extra_result['items']:
                track = _extract_track(item)
                if track is not None:
                    output.append(track)

    return output


async def get_tracks_from_spotify_playlist(spotify_session: spotipy.Spotify, spotify_playlist):
    def _get_tracks_from_spotify_playlist(offset: int, playlist_id: str):
        return spotify_session.playlist_tracks(playlist_id=playlist_id, offset=offset)

    print(f"Loading tracks from Spotify playlist '{spotify_playlist['name']}'")
    items = await repeat_on_request_error( _fetch_all_from_spotify_in_chunks, lambda offset: _get_tracks_from_spotify_playlist(offset=offset, playlist_id=spotify_playlist["id"]))
    track_filter = lambda item: item.get('type', 'track') == 'track' # type may be 'episode' also
    sanity_filter = lambda item: ('album' in item
                                  and 'name' in item['album']
                                  and 'artists' in item['album']
                                  and len(item['album']['artists']) > 0
                                  and item['album']['artists'][0]['name'] is not None)
    final_tracks = list(filter(sanity_filter, filter(track_filter, items)))
    skipped = len(items) - len(final_tracks)
    if skipped:
        print(f"  {len(final_tracks)} tracks loaded ({skipped} skipped: episodes or incomplete metadata)")
    return final_tracks

def populate_track_match_cache(spotify_tracks_: Sequence[t_spotify.SpotifyTrack], tidal_tracks_: Sequence[tidalapi.Track]):
    """ Populate the track match cache with all the existing tracks in Tidal playlist corresponding to Spotify playlist """
    def _populate_one_track_from_spotify(spotify_track: t_spotify.SpotifyTrack):
        for idx, tidal_track in list(enumerate(tidal_tracks)):
            if tidal_track.available and match(tidal_track, spotify_track):
                track_match_cache.insert((spotify_track['id'], tidal_track.id))
                tidal_tracks.pop(idx)
                return

    def _populate_one_track_from_tidal(tidal_track: tidalapi.Track):
        for idx, spotify_track in list(enumerate(spotify_tracks)):
            if tidal_track.available and match(tidal_track, spotify_track):
                track_match_cache.insert((spotify_track['id'], tidal_track.id))
                spotify_tracks.pop(idx)
                return

    # make a copy of the tracks to avoid modifying original arrays
    spotify_tracks = [t for t in spotify_tracks_]
    tidal_tracks = [t for t in tidal_tracks_]

    # first populate from the tidal tracks
    for track in tidal_tracks:
        _populate_one_track_from_tidal(track)
    # then populate from the subset of Spotify tracks that didn't match (to account for many-to-one style mappings)
    for track in spotify_tracks:
        _populate_one_track_from_spotify(track)

def get_new_spotify_tracks(spotify_tracks: Sequence[t_spotify.SpotifyTrack]) -> List[t_spotify.SpotifyTrack]:
    ''' Extracts only the tracks that have not already been seen in our Tidal caches '''
    results = []
    for spotify_track in spotify_tracks:
        if not spotify_track['id']: continue
        if not track_match_cache.get(spotify_track['id']) and not failure_cache.has_match_failure(spotify_track['id']):
            results.append(spotify_track)
    return results

async def search_new_tracks_on_tidal(tidal_session: tidalapi.Session, spotify_tracks: Sequence[t_spotify.SpotifyTrack], playlist_name: str, config: dict):
    """ Generic function for searching for each item in a list of Spotify tracks which have not already been seen and adding them to the cache """
    async def _run_rate_limiter(semaphore):
        ''' Leaky bucket algorithm for rate limiting. Periodically releases items from semaphore at rate_limit'''
        _sleep_time = config.get('max_concurrency', 10)/config.get('rate_limit', 10)/4 # aim to sleep approx time to drain 1/4 of 'bucket'
        t0 = datetime.datetime.now()
        while True:
            await asyncio.sleep(_sleep_time)
            t = datetime.datetime.now()
            dt = (t - t0).total_seconds()
            new_items = round(config.get('rate_limit', 10)*dt)
            t0 = t
            [semaphore.release() for i in range(new_items)] # leak new_items from the 'bucket'

    # Extract the new tracks that do not already exist in the old tidal tracklist
    tracks_to_search = get_new_spotify_tracks(spotify_tracks)
    if not tracks_to_search:
        return []

    # Search for each of the tracks on Tidal concurrently
    task_description = "Searching Tidal for {}/{} tracks in Spotify playlist '{}'".format(len(tracks_to_search), len(spotify_tracks), playlist_name)
    semaphore = asyncio.Semaphore(config.get('max_concurrency', 10))
    rate_limiter_task = asyncio.create_task(_run_rate_limiter(semaphore))
    search_results = await atqdm.gather( *[ repeat_on_request_error(tidal_search, t, semaphore, tidal_session) for t in tracks_to_search ], desc=task_description )
    rate_limiter_task.cancel()

    # Add the search results to the cache
    song404 = []
    for idx, spotify_track in enumerate(tracks_to_search):
        if search_results[idx]:
            track_match_cache.insert( (spotify_track['id'], search_results[idx].id) )
        else:
            song404.append(f"{spotify_track['id']}: {','.join([a['name'] for a in spotify_track['artists']])} - {spotify_track['name']}")
            color = ('\033[91m', '\033[0m')
            print(color[0] + "Could not find the track " + song404[-1] + color[1])
    if song404:
        file_name = "songs not found.txt"
        header = f"==========================\nPlaylist: {playlist_name}\n==========================\n"
        with open(file_name, "a", encoding="utf-8") as file:
            file.write(header)
            for song in song404:
                file.write(f"{song}\n")
    return song404

async def sync_playlist(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, spotify_playlist, tidal_playlist: tidalapi.Playlist | None, config: dict):
    """ sync given playlist to tidal """
    # Get the tracks from both Spotify and Tidal, creating a new Tidal playlist if necessary
    spotify_tracks = await get_tracks_from_spotify_playlist(spotify_session, spotify_playlist)
    # Note: Even if no tracks are available, we still create the playlist to match Spotify structure
    # if len(spotify_tracks) == 0:
    #     return False # nothing to do

    created_playlist = False
    if tidal_playlist:
        old_tidal_tracks = await get_all_playlist_tracks(tidal_playlist)
    else:
        playlist_name = spotify_playlist.get('name', '<unknown>')
        playlist_description = spotify_playlist.get('description', '') or ''
        print(f"No playlist found on Tidal corresponding to Spotify playlist: '{playlist_name}', creating new playlist")
        try:
            tidal_playlist = tidal_session.user.create_playlist(playlist_name, playlist_description)
        except Exception as e:
            print(f"Failed to create Tidal playlist for '{playlist_name}': {e}")
            print(traceback.format_exc())
            raise
        created_playlist = True
        playlist_link = getattr(tidal_playlist, 'share_url', None) or getattr(tidal_playlist, 'listen_url', None)
        if playlist_link:
            print(f"Created Tidal playlist '{tidal_playlist.name}' (ID={tidal_playlist.id}) at {playlist_link}")
        else:
            print(f"Created Tidal playlist '{tidal_playlist.name}' (ID={tidal_playlist.id})")
        old_tidal_tracks = []

    # Search for any Spotify tracks not yet in Tidal, then append only the new ones.
    # Tidal is the source of truth for ordering — existing Tidal order is never changed.
    populate_track_match_cache(spotify_tracks, old_tidal_tracks)
    song404 = await search_new_tracks_on_tidal(tidal_session, spotify_tracks, spotify_playlist['name'], config)
    if song404 is None:
        song404 = []

    existing_tidal_ids = {t.id for t in old_tidal_tracks}
    seen = set(existing_tidal_ids)
    tracks_to_append = []
    for spotify_track in spotify_tracks:
        if not spotify_track.get('id'):
            continue
        tidal_id = track_match_cache.get(spotify_track['id'])
        if tidal_id and tidal_id not in seen:
            tracks_to_append.append(tidal_id)
            seen.add(tidal_id)

    total_tidal_tracks = len(existing_tidal_ids) + len(tracks_to_append)
    print(f"Computed {total_tidal_tracks} Tidal track ids for '{spotify_playlist['name']}'")
    if total_tidal_tracks == 0:
        print(f"No matched Tidal tracks found for Spotify playlist '{spotify_playlist['name']}'")

    if not tracks_to_append:
        print("No new tracks to append to Tidal playlist")
    else:
        print(f"Appending {len(tracks_to_append)} new tracks to Tidal playlist")
        add_multiple_tracks_to_playlist(tidal_playlist, tracks_to_append)

    print(f"Finished syncing playlist '{spotify_playlist['name']}' "
          f"({total_tidal_tracks} tracks total, {len(song404)} unmatched)")
    return created_playlist

async def sync_favorites(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, config: dict):
    """ sync user favorites to tidal """
    async def get_tracks_from_spotify_favorites() -> List[dict]:
        _get_favorite_tracks = lambda offset: spotify_session.current_user_saved_tracks(offset=offset)    
        tracks = await repeat_on_request_error( _fetch_all_from_spotify_in_chunks, _get_favorite_tracks)
        tracks.reverse()
        return tracks

    def get_new_tidal_favorites() -> List[int]:
        existing_favorite_ids = set([track.id for track in old_tidal_tracks])
        new_ids = []
        for spotify_track in spotify_tracks:
            match_id = track_match_cache.get(spotify_track['id'])
            if match_id and not match_id in existing_favorite_ids:
                new_ids.append(match_id)
        return new_ids

    print("Loading favorite tracks from Spotify")
    spotify_tracks = await get_tracks_from_spotify_favorites()
    print("Loading existing favorite tracks from Tidal")
    old_tidal_tracks = await get_all_favorites(tidal_session.user.favorites, order='DATE')
    populate_track_match_cache(spotify_tracks, old_tidal_tracks)
    await search_new_tracks_on_tidal(tidal_session, spotify_tracks, "Favorites", config)
    new_tidal_favorite_ids = get_new_tidal_favorites()
    if new_tidal_favorite_ids:
        for tidal_id in tqdm(new_tidal_favorite_ids, desc="Adding new tracks to Tidal favorites"):
            tidal_session.user.favorites.add_track(tidal_id)
    else:
        print("No new tracks to add to Tidal favorites")

def sync_playlists_wrapper(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, playlists, config: dict):
    playlist_count = len(playlists)
    print(f"Starting playlist sync for {playlist_count} playlists")
    created_count = 0
    for idx, (spotify_playlist, tidal_playlist) in enumerate(playlists, start=1):
        print(f"Syncing playlist {idx}/{playlist_count}: '{spotify_playlist['name']}'")
        created = asyncio.run(sync_playlist(spotify_session, tidal_session, spotify_playlist, tidal_playlist, config) )
        if created:
            created_count += 1
    print(f"Playlist sync complete: {playlist_count} playlists processed, {created_count} created")


def sync_favorites_wrapper(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, config):
    print("Starting favorites sync")
    asyncio.run(main=sync_favorites(spotify_session=spotify_session, tidal_session=tidal_session, config=config))
    print("Favorites sync complete")

def get_tidal_playlists_wrapper(tidal_session: tidalapi.Session) -> Mapping[str, tidalapi.Playlist]:
    tidal_user_id = getattr(tidal_session.user, 'id', None)
    tidal_username = getattr(tidal_session.user, 'username', None)
    tidal_playlists = asyncio.run(get_all_playlists(tidal_session.user))
    print(f"Loaded {len(tidal_playlists)} Tidal playlists for user '{tidal_username}'")
    return {playlist.name: playlist for playlist in tidal_playlists}

def print_tidal_playlist_urls(playlists):
    print(f"Fetching Tidal URLs for {len(playlists)} playlists")
    found_count = 0
    for spotify_playlist, tidal_playlist in playlists:
        playlist_name = spotify_playlist.get('name', '<unknown>')
        print(f"Spotify playlist: {playlist_name}")
        if tidal_playlist:
            found_count += 1
            playlist_link = getattr(tidal_playlist, 'share_url', None) or getattr(tidal_playlist, 'listen_url', None)
            print(f"  Tidal playlist: {tidal_playlist.name} (ID={tidal_playlist.id})")
            print(f"  URL: {playlist_link or 'URL not available'}")
        else:
            print("  Tidal playlist: not found")
    print(f"Done fetching Tidal URLs: {found_count}/{len(playlists)} mapped to existing Tidal playlists")


def print_all_tidal_playlist_urls(tidal_playlists):
    tidal_playlists = list(tidal_playlists)
    print(f"Listing all {len(tidal_playlists)} Tidal playlists")
    for playlist in tidal_playlists:
        playlist_link = getattr(playlist, 'share_url', None) or getattr(playlist, 'listen_url', None)
        print(f"{playlist.name} (ID={playlist.id}) -> {playlist_link or 'URL not available'}")
    print("Done listing all Tidal playlists")


def test_create_tidal_playlist(tidal_session: tidalapi.Session, title: str, description: str = "Created by spotify_to_tidal test"):
    print(f"Attempting to create Tidal playlist: '{title}'")
    try:
        playlist = tidal_session.user.create_playlist(title, description)
        playlist_link = getattr(playlist, 'share_url', None) or getattr(playlist, 'listen_url', None)
        print(f"Created playlist: {playlist.name} (ID={playlist.id})")
        print(f"URL: {playlist_link or 'URL not available'}")
        print("Test playlist creation succeeded")
    except Exception as e:
        print(f"Test playlist creation failed: {type(e).__name__}: {e}")
        print(traceback.format_exc())
        raise


def pick_tidal_playlist_for_spotify_playlist(spotify_playlist, tidal_playlists: Mapping[str, tidalapi.Playlist]):
    spotify_name = spotify_playlist['name']
    if spotify_name in tidal_playlists:
      # if there's an existing tidal playlist with the name of the current playlist then use that
      tidal_playlist = tidal_playlists[spotify_name]
      print(f"Found existing Tidal playlist '{spotify_name}' for Spotify playlist")
      return (spotify_playlist, tidal_playlist)
    else:
      print(f"No Tidal playlist found for Spotify playlist '{spotify_name}'")
      return (spotify_playlist, None)

def get_user_playlist_mappings(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, config):
    results = []
    spotify_playlists = asyncio.run(get_playlists_from_spotify(spotify_session, config))
    tidal_playlists = get_tidal_playlists_wrapper(tidal_session)
    for spotify_playlist in spotify_playlists:
        results.append( pick_tidal_playlist_for_spotify_playlist(spotify_playlist, tidal_playlists) )
    return results

async def get_playlists_from_spotify(spotify_session: spotipy.Spotify, config):
    # get all the playlists from the Spotify account
    playlists = []
    print("Loading Spotify playlists")
    first_results = spotify_session.current_user_playlists()
    exclude_list = set([x.split(':')[-1] for x in config.get('excluded_playlists', [])])
    playlists.extend([p for p in first_results['items']])
    user_id = spotify_session.current_user()['id']

    # get all the remaining playlists in parallel
    if first_results['next']:
        offsets = [ first_results['limit'] * n for n in range(1, math.ceil(first_results['total']/first_results['limit'])) ]
        extra_results = await atqdm.gather( *[asyncio.to_thread(spotify_session.current_user_playlists, offset=offset) for offset in offsets ] )
        for extra_result in extra_results:
            playlists.extend([p for p in extra_result['items']])

    # filter out playlists that don't belong to us or are on the exclude list
    my_playlist_filter = lambda p: p and p['owner']['id'] == user_id
    exclude_filter = lambda p: not p['id'] in exclude_list
    return list(filter( exclude_filter, filter( my_playlist_filter, playlists )))

def get_playlists_from_config(spotify_session: spotipy.Spotify, tidal_session: tidalapi.Session, config):
    # get the list of playlist sync mappings from the configuration file
    def get_playlist_ids(config):
        return [(item['spotify_id'], item['tidal_id']) for item in config['sync_playlists']]
    output = []
    for spotify_id, tidal_id in get_playlist_ids(config=config):
        try:
            spotify_playlist = spotify_session.playlist(playlist_id=spotify_id)
        except spotipy.SpotifyException as e:
            print(f"Error getting Spotify playlist {spotify_id}")
            raise e
        try:
            tidal_playlist = tidal_session.playlist(playlist_id=tidal_id)
        except Exception as e:
            print(f"Error getting Tidal playlist {tidal_id}")
            raise e
        output.append((spotify_playlist, tidal_playlist))
    return output

