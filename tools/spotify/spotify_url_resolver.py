from tools.spotify.song import Song
from tools.spotify.audio.AudioProvider_youtube import YouTube
from tools.spotify.audio.AudioProvider_soundcloud import SoundCloud
from tools.spotify.client import SpotifyClient
import os
import logging as lg
from dotenv import load_dotenv

load_dotenv()

class NoUrl(Exception):
    pass

class InvalidArgs(Exception):
    pass

class InvalidUrls(Exception):
    pass

logging = lg.getLogger(__name__)

class Spotify_Worker:
    def __init__(self):
        # SpotifyClient.init(client_id=os.environ.get('SPOTIFY_CLIENT_ID'), client_secret=os.environ.get('SPOTIFY_CLIENT_SECRET'))
        pass

    @staticmethod
    def get_song(url: str) -> Song:
        return ""

    @staticmethod
    def _run(client, query) -> str | None:
        return ""

    def search_song(self, query: str):

        return None

    def resolve_url(self, url: str = None, force_use_sc: bool = False, force_use_yt: bool = False) -> str | None:
        return None