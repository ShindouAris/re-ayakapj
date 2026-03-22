# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import traceback
from logging import getLogger
from typing import Optional, TYPE_CHECKING
from urllib.parse import quote

import spotipy
from spotipy import SpotifyClientCredentials

from utils.music.converters import fix_characters
from utils.music.errors import MissingSpotifyClient, GenericError
from utils.music.models import PartialPlaylist, PartialTrack

if TYPE_CHECKING:
    from utils.client import BotCore

spotify_regex = re.compile("https://open.spotify.com?.+(album|playlist|artist|track)/([a-zA-Z0-9]+)")
spotify_link_regex = re.compile(r"(?i)https?:\/\/spotify\.link\/?(?P<id>[a-zA-Z0-9]+)")
spotify_regex_w_user = re.compile("https://open.spotify.com?.+(album|playlist|artist|track|user)/([a-zA-Z0-9]+)")

_log = getLogger(__name__)

def query_spotify_track(func, url_id: str):
    return func(url_id)


async def process_spotify(bot: BotCore, requester: int, query: str):

    if spotify_link_regex.match(query):
        async with bot.session.get(query, allow_redirects=False) as r:
            if 'location' not in r.headers:
                raise GenericError("**Không có kết quả từ tìm kiếm của bạn...**")
            query = str(r.headers["location"])

    if not spotify_regex.match(query):
        return

    raise GenericError("**Hỗ trợ Spotify đã bị tắt do thay đổi giới hạn API. Vui lòng sử dụng YouTube hoặc SoundCloud.**")

    if not bot.spotify:
        if [n for n in bot.music.nodes.values() if n.version > 3 and "spotify" in n.info["sourceManagers"]]:
            return
        raise MissingSpotifyClient()

    url_type, url_id = matches.groups()

    if url_type == "track":

        result = await bot.loop.run_in_executor(None, lambda: bot.spotify.track(url_id))

        t = PartialTrack(
            uri=result["external_urls"]["spotify"],
            author=result["artists"][0]["name"] or "Unknown Artist",
            title=result["name"],
            thumb=result["album"]["images"][0]["url"],
            duration=result["duration_ms"],
            source_name="spotify",
            original_id=result["id"],
            requester=requester
        )

        t.info["extra"]["authors"] = [fix_characters(i['name']) for i in result['artists'] if f"feat. {i['name'].lower()}"
                                      not in result['name'].lower()]

        t.info["extra"]["authors_md"] = ", ".join(f"[`{a['name']}`]({a['external_urls']['spotify']})" for a in result["artists"])

        try:
            if result["album"]["name"] != result["name"]:
                t.info["extra"]["album"] = {
                    "name": result["album"]["name"],
                    "url": result["album"]["external_urls"]["spotify"]
                }
        except (AttributeError, KeyError):
            pass

        return [t]

    data = {
        'loadType': 'PLAYLIST_LOADED',
        'playlistInfo': {'name': ''},
        'sourceName': "spotify",
        'tracks_data': [],
        'is_album': False,
        "thumb": ""
    }

    if url_type == "album":

        result = await bot.loop.run_in_executor(None, lambda: bot.spotify.album(url_id))

        try:
            thumb = result["tracks"][0]["album"]["images"][0]["url"]
        except:
            thumb = ""

        if len(result["tracks"]) < 2:

            track = result["tracks"][0]

            return [PartialTrack(
                uri=track["external_urls"]["spotify"],
                author=track["artists"][0]["name"] or "Unknown Artist",
                title=track["name"],
                thumb=thumb,
                duration=track["duration_ms"],
                source_name="spotify",
                original_id=track["id"],
                requester=requester
            )]

        data["playlistInfo"]["name"] = result["name"]
        data["playlistInfo"]["is_album"] = True

        for t in result["tracks"]["items"]:
            t["album"] = result

        tracks_data = result["tracks"]["items"]

    elif url_type == "artist":

        result = await bot.loop.run_in_executor(None, lambda: bot.spotify.artist_top_tracks(url_id))

        try:
            data["playlistInfo"]["name"] = "Được chơi nhiều nhất của: " + \
                                           [a["name"] for a in result["tracks"][0]["artists"] if a["id"] == url_id][0]
        except IndexError:
            data["playlistInfo"]["name"] = "Được chơi nhiều nhất của: " + result["tracks"][0]["artists"][0]["name"]
        tracks_data = result["tracks"]

    elif url_type == "playlist":

        try:
            result = await bot.loop.run_in_executor(None, lambda: bot.spotify.playlist(url_id))
        except spotipy.SpotifyException as e:
            raise GenericError("**Đã xảy ra lỗi khi xử lý danh sách phát:** ```py"
                               f"{repr(e)}```")
        data["playlistInfo"]["name"] = result["name"]
        data["playlistInfo"]["thumb"] = result["images"][0]["url"]
        tracks_data = [t["track"] for t in result["tracks"]["items"]]

    else:
        raise GenericError(f"**Liên kết Spotify không được công nhận/hỗ trợ:**\n{query}")

    if not tracks_data:
        raise GenericError(f"**Không có kết quả nào cho liên kết Spotify được cung cấp..**")

    data["playlistInfo"]["selectedTrack"] = -1

    playlist = PartialPlaylist(data, url=query)

    for t in tracks_data:

        if not t:
            continue

        try:
            thumb = t["album"]["images"][0]["url"]
        except (IndexError, KeyError):
            thumb = ""

        track = PartialTrack(
            uri=t["external_urls"].get("spotify", f"https://www.youtube.com/results?search_query={quote(t['name'])}"),
            author=t["artists"][0]["name"] or "Unknown Artist",
            title=t["name"],
            thumb=thumb,
            duration=t["duration_ms"],
            source_name="spotify",
            original_id=t["id"],
            playlist=playlist,
            requester=requester
        )

        try:
            track.info["isrc"] = t["external_ids"]["isrc"]
        except KeyError:
            pass

        try:
            track.info["extra"]["album"] = {
                "name": t["album"]["name"],
                "url": t["album"]["external_urls"]["spotify"]
            }
        except (AttributeError, KeyError):
            pass

        if t["artists"][0]["name"]:
            track.info["extra"]["authors"] = [fix_characters(i['name']) for i in t['artists'] if f"feat. {i['name'].lower()}" not in t['name'].lower()]
            track.info["extra"]["authors_md"] = ", ".join(f"[`{fix_characters(a['name'])}`]({a['external_urls']['spotify']})" for a in t['artists'])
        else:
            track.info["extra"]["authors"] = ["Unknown Artist"]
            track.info["extra"]["authors_md"] = "`Unknown Artist`"

        playlist.tracks.append(track)

    return playlist


def spotify_client(config: dict) -> Optional[spotipy.Spotify]:
    if not config['SPOTIFY_CLIENT_ID']:
        _log.warning(
            f"Spotify: SPOTIFY_CLIENT_ID not configured in the ENV (or .env file).")
        return

    if not config['SPOTIFY_CLIENT_SECRET']:
        _log.warning(
            f"Spotify: SPOTIFY_CLIENT_SECRET not configured in the ENV "
             f" (or in .env file).")
        return

    try:
        _log.info("Setting Up Spotify Client...")
        return spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=config['SPOTIFY_CLIENT_ID'],
                client_secret=config['SPOTIFY_CLIENT_SECRET']
            )
        )

    except KeyError as e:
        _log.warning(
            f"Spotify APIKEY is not configured properly in the ENV (or .env), "
            f"Check and try again if you want to support the songs from Spotify (Err: {repr(e)})).")
        return

    except Exception as e:
        _log.error(f"An error occur while setting up Spotify:\n{traceback.format_exc()}).")
        return
