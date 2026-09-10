import asyncio
import math
import time
from typing import List
import requests
import tidalapi
from tqdm import tqdm
from tqdm.asyncio import tqdm as atqdm

def _remove_indices_from_playlist(playlist: tidalapi.UserPlaylist, indices: List[int], retries: int = 3):
    index_string = ",".join(map(str, indices))
    for attempt in range(retries):
        headers = {'If-None-Match': playlist._etag}
        try:
            playlist.request.request('DELETE', (playlist._base_url + '/items/%s') % (playlist.id, index_string), headers=headers)
            playlist._reparse()
            return
        except requests.exceptions.HTTPError as e:
            response = getattr(e, 'response', None)
            status = response.status_code if response is not None else 'unknown'
            if status == 412 and attempt < retries - 1:
                time.sleep(1)
                playlist._reparse()
            else:
                raise

def clear_tidal_playlist(playlist: tidalapi.UserPlaylist, chunk_size: int=20):
    remaining = playlist.num_tracks
    with tqdm(desc="Erasing existing tracks from Tidal playlist", total=remaining) as progress:
        while remaining > 0:
            to_delete = min(remaining, chunk_size)
            _remove_indices_from_playlist(playlist, range(to_delete))
            remaining -= to_delete
            progress.update(to_delete)
    
def _playlist_add_with_reparse(playlist: tidalapi.UserPlaylist, tracks: List[int]):
    playlist.add(tracks)
    playlist._reparse()

def add_multiple_tracks_to_playlist(playlist: tidalapi.UserPlaylist, track_ids: List[int], chunk_size: int=20):
    offset = 0
    with tqdm(desc="Adding new tracks to Tidal playlist", total=len(track_ids)) as progress:
        while offset < len(track_ids):
            count = min(chunk_size, len(track_ids) - offset)
            chunk = track_ids[offset:offset+count]
            try:
                _playlist_add_with_reparse(playlist, chunk)
                offset += count
                progress.update(count)
            except requests.exceptions.HTTPError as e:
                response = getattr(e, 'response', None)
                status = response.status_code if response is not None else 'unknown'
                text = response.text if response is not None else str(e)
                print(f"Tidal playlist add failed for chunk at offset {offset} with status {status}")
                print(f"Chunk ids: {chunk}")
                print(f"Response: {text}")
                if status in (429, 412):
                    print("Retryable Tidal error; sleeping briefly before retrying chunk")
                    time.sleep(2)
                    playlist._reparse()
                    try:
                        _playlist_add_with_reparse(playlist, chunk)
                        offset += count
                        progress.update(count)
                        continue
                    except requests.exceptions.HTTPError as e2:
                        response2 = getattr(e2, 'response', None)
                        status2 = response2.status_code if response2 is not None else 'unknown'
                        text2 = response2.text if response2 is not None else str(e2)
                        print(f"Chunk retry failed with status {status2}")
                        print(f"Response: {text2}")
                print("Retrying each track individually")
                for track_id in chunk:
                    try:
                        _playlist_add_with_reparse(playlist, [track_id])
                        progress.update(1)
                        offset += 1
                    except requests.exceptions.HTTPError as e2:
                        response2 = getattr(e2, 'response', None)
                        status2 = response2.status_code if response2 is not None else 'unknown'
                        text2 = response2.text if response2 is not None else str(e2)
                        print(f"Failed to add individual track {track_id}: status {status2}")
                        print(f"Response: {text2}")
                        offset += 1
                        progress.update(1)

async def _get_all_chunks(url, session, parser, params={}) -> List[tidalapi.Track]:
    """ 
        Helper function to get all items from a Tidal endpoint in parallel
        The main library doesn't provide the total number of items or expose the raw json, so use this wrapper instead
    """
    def _make_request(offset: int=0):
        new_params = params
        new_params['offset'] = offset
        return session.request.map_request(url, params=new_params)

    first_chunk_raw = _make_request()
    limit = first_chunk_raw['limit']
    total = first_chunk_raw['totalNumberOfItems']
    items = session.request.map_json(first_chunk_raw, parse=parser)

    if len(items) < total:
        offsets = [limit * n for n in range(1, math.ceil(total/limit))]
        extra_results = await atqdm.gather(
                *[asyncio.to_thread(lambda offset: session.request.map_json(_make_request(offset), parse=parser), offset) for offset in offsets],
            desc="Fetching additional data chunks"
        )
        for extra_result in extra_results:
            items.extend(extra_result)
    return items

async def get_all_favorites(favorites: tidalapi.Favorites, order: str = "NAME", order_direction: str = "ASC", chunk_size: int=100) -> List[tidalapi.Track]:
    """ Get all favorites from Tidal playlist in chunks """
    params = {
        "limit": chunk_size,
        "order": order,
        "orderDirection": order_direction,
    }
    return await _get_all_chunks(f"{favorites.base_url}/tracks", session=favorites.session, parser=favorites.session.parse_track, params=params)

async def get_all_playlists(user: tidalapi.User, chunk_size: int=10) -> List[tidalapi.Playlist]:
    """ Get all user playlists from Tidal in chunks """
    print(f"Loading playlists from Tidal user")
    params = {
        "limit": chunk_size,
    }
    return await _get_all_chunks(f"users/{user.id}/playlists", session=user.session, parser=user.playlist.parse_factory, params=params)

async def get_all_playlist_tracks(playlist: tidalapi.Playlist, chunk_size: int=20) -> List[tidalapi.Track]:
    """ Get all tracks from Tidal playlist in chunks """
    params = {
        "limit": chunk_size,
    }
    print(f"Loading tracks from Tidal playlist '{playlist.name}'")
    return await _get_all_chunks(f"{playlist._base_url%playlist.id}/tracks", session=playlist.session, parser=playlist.session.parse_track, params=params)

