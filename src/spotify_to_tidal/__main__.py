import yaml
import argparse
import sys

from . import sync as _sync
from . import auth as _auth

def main():
    parser = argparse.ArgumentParser(
        prog='spotify_to_tidal',
        description='Sync Spotify playlists and liked songs to Tidal.',
    )
    parser.add_argument('--config', default='config.yml', metavar='PATH',
                        help='config file to use (default: config.yml)')
    parser.add_argument('--uri', metavar='SPOTIFY_URI',
                        help='sync a single Spotify playlist by URI instead of all playlists')
    parser.add_argument('--sync-favorites', action=argparse.BooleanOptionalAction,
                        help='sync liked songs to Tidal favorites (overrides config default)')
    parser.add_argument('--refresh-session', action='store_true',
                        help='refresh the cached Tidal session tokens and exit')
    parser.add_argument('--fetch-tidal-urls', action='store_true',
                        help='print Tidal playlist URLs for mapped playlists and exit (no sync)')
    debug = parser.add_argument_group('debug')
    debug.add_argument('--test-create-tidal-playlist', action='store_true',
                       help='create a throwaway Tidal playlist to verify write access')
    debug.add_argument('--test-playlist-name', default='spotify_to_tidal_test_playlist',
                       metavar='NAME', help='name for the test playlist (default: spotify_to_tidal_test_playlist)')
    args = parser.parse_args()

    with open(args.config, 'r') as f:
        config = yaml.safe_load(f)

    if args.refresh_session:
        print("Refreshing Tidal session")
        tidal_session = _auth.open_tidal_session()
        if not tidal_session.check_login():
            sys.exit("Could not connect to Tidal — re-run without --refresh-session to re-authenticate")
        print("Tidal session refreshed successfully")
        sys.exit(0)

    print("Opening Spotify session")
    spotify_session = _auth.open_spotify_session(config['spotify'])
    print("Opening Tidal session")
    tidal_session = _auth.open_tidal_session()
    if not tidal_session.check_login():
        sys.exit("Could not connect to Tidal")
    tidal_user_id = getattr(tidal_session.user, 'id', None)
    tidal_username = getattr(tidal_session.user, 'username', None)
    print(f"Tidal session opened for user id={tidal_user_id}, username={tidal_username}")

    if args.fetch_tidal_urls:
        tidal_playlists = _sync.get_tidal_playlists_wrapper(tidal_session)
        if args.uri:
            spotify_playlist = spotify_session.playlist(args.uri)
            mapped_playlist = _sync.pick_tidal_playlist_for_spotify_playlist(spotify_playlist, tidal_playlists)
            _sync.print_tidal_playlist_urls([mapped_playlist])
        elif config.get('sync_playlists', None):
            _sync.print_tidal_playlist_urls(_sync.get_playlists_from_config(spotify_session, tidal_session, config))
        else:
            _sync.print_tidal_playlist_urls(_sync.get_user_playlist_mappings(spotify_session, tidal_session, config))

        _sync.print_all_tidal_playlist_urls(tidal_playlists.values())
        sys.exit(0)

    if args.test_create_tidal_playlist:
        _sync.test_create_tidal_playlist(tidal_session, args.test_playlist_name)
        sys.exit(0)
    if args.uri:
        # if a playlist ID is explicitly provided as a command line argument then use that
        spotify_playlist = spotify_session.playlist(args.uri)
        tidal_playlists = _sync.get_tidal_playlists_wrapper(tidal_session)
        tidal_playlist = _sync.pick_tidal_playlist_for_spotify_playlist(spotify_playlist, tidal_playlists)
        _sync.sync_playlists_wrapper(spotify_session, tidal_session, [tidal_playlist], config)
        sync_favorites = args.sync_favorites # only sync favorites if command line argument explicitly passed
    elif args.sync_favorites:
        sync_favorites = True # sync only the favorites
    elif config.get('sync_playlists', None):
        # if the config contains a sync_playlists list of mappings then use that
        _sync.sync_playlists_wrapper(spotify_session, tidal_session, _sync.get_playlists_from_config(spotify_session, tidal_session, config), config)
        sync_favorites = args.sync_favorites is None and config.get('sync_favorites_default', True)
    else:
        # otherwise sync all the user playlists in the Spotify account and favorites unless explicitly disabled
        _sync.sync_playlists_wrapper(spotify_session, tidal_session, _sync.get_user_playlist_mappings(spotify_session, tidal_session, config), config)
        sync_favorites = args.sync_favorites is None and config.get('sync_favorites_default', True)

    if sync_favorites:
        _sync.sync_favorites_wrapper(spotify_session, tidal_session, config)

    print("Synchronization complete")

if __name__ == '__main__':
    main()
    sys.exit(0)
