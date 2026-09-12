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
        self.client = SpotifyClient()
        self.youtube_client = YouTube()
        self.soundcloud_client = SoundCloud()


    @staticmethod
    def get_song(url: str) -> Song:
        return Song.from_url(url)

    @staticmethod
    def _run(client, query) -> str | None:
        songData = client.get_results(query)
        return songData[0].url if len(songData) > 0 else None

    def search_song(self, query: str):

        result = self.client.search(q=query, limit=1)

        if not result['tracks']['items']:
            logging.warning(f"No song for query: {query}")
            return None

        return self.resolve_url(result['tracks']['items'][0]['external_urls']['spotify'])

    def resolve_url(self, url: str = None, force_use_sc: bool = False, force_use_yt: bool = False) -> str | None:
        if not url:
            raise NoUrl("No Url found!")

        if force_use_sc and force_use_yt:
            raise InvalidArgs("Only one `force_use_sc` or `force_use_yt` can be use at once!")

        # if not URL_REGEX.match(url):
        #     raise InvalidUrls("Invalid Urls")

        logging.info("Resolving url: %s", url)

        song = self.get_song(url)

        if force_use_sc:
            logging.warning("This client may return `None`, so you need to handle carefully")
            songData = self._run(self.soundcloud_client, song.name)
            return songData

        if force_use_yt:
            logging.warning("This client may NOT work perfectly and return `None`, so you need to handling carefully")
            songData = self._run(self.youtube_client, song.name)
            return songData

        logging.info("Fetching data from SoundCloud: %s - %p", song.name, song.url)
        songData = self._run(self.soundcloud_client, song.name)

        if songData:
            return songData
        else:
            logging.warning(f"Seem SoundCloud didn't return anything, falling back to youtube")
            return self._run(self.youtube_client, song.name)