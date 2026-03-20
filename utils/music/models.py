# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import datetime
import os
import pprint
import random
import aiohttp
import traceback
import uuid
from collections import deque
from time import time
from typing import Optional, Union, TYPE_CHECKING, List
from urllib import parse
from urllib.parse import quote

import disnake

import wavelink
from utils.db import DBModel
from utils.music.checks import can_connect
from utils.music.converters import fix_characters, time_format, get_button_style, YOUTUBE_VIDEO_REG
from utils.music.filters import AudioFilter
from utils.music.skin_utils import skin_converter
from utils.others import music_source_emoji, send_idle_embed, PlayerControls, SongRequestPurgeMode, \
    song_request_buttons

if TYPE_CHECKING:
    from utils.client import BotCore

exclude_tags = ["remix", "edit", "extend", "compilation", "mashup"]

thread_archive_time = {
    60: 30,
    24: 720,
    2880: 720,
    10080: 2880,
}


def get_start_pos(player, track, extra_milliseconds=0):
    if not track.is_stream:
        difference = (((disnake.utils.utcnow() + datetime.timedelta(milliseconds=int(extra_milliseconds))).timestamp() * 1000)) - player.last_update
        position = player.last_position + difference
        if 0 < position < track.duration:
            return min(position, track.duration)
    return 0


class PartialPlaylist:
    __slots__ = ('data', 'url', 'tracks')

    def __init__(self, data: dict, url: str):
        self.data = data
        self.url = url
        self.tracks = []

    @property
    def name(self):
        try:
            return self.data["playlistInfo"]["name"]
        except KeyError:
            return

    @property
    def thumb(self):
        try:
            return self.data["playlistInfo"]["thumb"]
        except KeyError:
            pass
        try:
            return self.tracks[0].thumb
        except:
            return ""


class PartialTrack:
    __slots__ = ('id', 'thumb', 'source_name', 'info', 'playlist', 'unique_id', 'ytid')

    def __init__(self, *, uri: str = "", title: str = "", author="", thumb: str = "", duration: int = 0,
                 requester: int = 0, track_loops: int = 0, source_name: str = "", autoplay: bool = False,
                 original_id: str = "", info: dict = None, playlist: PartialPlaylist = None):

        self.info = info or {
            "author": fix_characters(author)[:97],
            "title": title[:97],
            "uri": uri,
            "length": duration,
            "isStream": False,
            "isSeekable": True,
            "sourceName": source_name,
            "extra": {
                "original_id": original_id,
                "requester": requester,
                "track_loops": track_loops,
                "thumb": thumb,
                "autoplay": autoplay
            }
        }

        self.id = ""
        self.ytid = ""
        self.unique_id = str(uuid.uuid4().hex)[:10]
        self.thumb = self.info["extra"]["thumb"]
        self.playlist: Optional[PartialPlaylist] = playlist

    def __repr__(self):
        return f"{self.info['sourceName']} - {self.duration} - {self.authors_string} - {self.title}"

    @property
    def uri(self) -> str:
        return self.info["uri"]

    @property
    def url(self) -> str:
        return self.uri

    @property
    def search_uri(self):
        return f"https://www.youtube.com/results?search_query={quote(self.title)}"

    @property
    def title(self) -> str:
        return f"{self.author} - {self.single_title}"

    @property
    def name(self) -> str:
        return self.title

    @property
    def original_id(self) -> str:
        try:
            return self.info["extra"]["original_id"]
        except KeyError:
            return ""

    @property
    def single_title(self) -> str:
        return self.info["title"]

    @property
    def author(self) -> str:
        return self.info["author"]

    @property
    def authors_string(self) -> str:
        try:
            return ", ".join(self.info["extra"]["authors"])
        except KeyError:
            return self.author

    @property
    def authors_md(self) -> str:
        try:
            return self.info["extra"]["authors_md"]
        except KeyError:
            return ""

    @property
    def authors(self) -> List[str]:
        try:
            return self.info["extra"]["authors"]
        except KeyError:
            return [self.author]

    @property
    def lyrics(self) -> str:

        try:
            return self.info["extra"]["lyrics"]
        except KeyError:
            return ""

    @property
    def requester(self) -> int:
        return self.info["extra"]["requester"]

    @property
    def autoplay(self) -> bool:
        try:
            return self.info["extra"]["autoplay"]
        except KeyError:
            return False

    @property
    def track_loops(self) -> int:
        return self.info["extra"]["track_loops"]

    @property
    def is_stream(self) -> bool:
        return self.info["isStream"]

    @property
    def duration(self) -> int:
        return self.info["length"]

    @property
    def album_name(self) -> str:
        try:
            return self.info["extra"]["album"]["name"]
        except KeyError:
            return ""

    @property
    def album_url(self) -> str:
        try:
            return self.info["extra"]["album"]["url"]
        except KeyError:
            return ""

    @property
    def playlist_name(self) -> str:
        try:
            return self.playlist.name[:97]
        except AttributeError:
            return ""

    @property
    def playlist_url(self) -> str:
        try:
            return self.playlist.url
        except AttributeError:
            return ""


class LavalinkPlaylist:
    __slots__ = ('data', 'url', 'tracks')

    def __init__(self, data: dict, **kwargs):
        self.data = data
        self.url = kwargs.pop("url")

        encoded_name = kwargs.pop("encoded_name", "track")

        try:
            if self.data['tracks'][0]['info'].get("sourceName") == "youtube":
                try:
                    self.url = f"https://www.youtube.com/playlist?list={parse.parse_qs(parse.urlparse(self.url).query)['list'][0]}"
                except KeyError:
                    pass
        except IndexError:
            pass
        self.tracks = [LavalinkTrack(
            id_=track[encoded_name], info=track['info'], playlist=self, **kwargs) for track in data['tracks']]

    @property
    def name(self):
        return self.data["playlistInfo"]["name"]

    @property
    def thumb(self):
        try:
            return self.data["playlistInfo"]["thumb"]
        except KeyError:
            pass
        try:
            return self.tracks[0].thumb
        except:
            return ""


class LavalinkTrack(wavelink.Track):
    __slots__ = ('extra', 'playlist', 'unique_id')

    def __init__(self, *args, **kwargs):
        try:
            args[1]['title'] = fix_characters(args[1]['title'])[:97]
        except IndexError:
            pass
        super().__init__(*args, **kwargs)
        self.title = fix_characters(self.title)
        self.info["title"] = self.title
        self.unique_id = str(uuid.uuid4().hex)[:10]

        try:
            self.info['sourceName']
        except:
            self.info['sourceName'] = 'LavalinkTrack'

        try:
            self.info["extra"]
        except KeyError:
            self.info["extra"] = {
                "track_loops": kwargs.pop('track_loops', 0),
                "requester": kwargs.pop('requester', ''),
                "autoplay": kwargs.pop("autoplay", '')
            }

        self.playlist: Optional[LavalinkPlaylist] = kwargs.pop(
            "playlist", None)

        if self.info["sourceName"] == "youtube":
            self.info["extra"]["thumb"] = f"https://img.youtube.com/vi/{self.ytid}/mqdefault.jpg"
            if "list=" not in self.uri:
                try:
                    self.uri = f"{self.uri}&list={parse.parse_qs(parse.urlparse(self.playlist_url).query)['list'][0]}"
                    self.info["uri"] = self.uri
                except KeyError:
                    pass

        elif self.info["sourceName"] == "soundcloud":

            self.info["extra"]["thumb"] = self.info.get(
                "artworkUrl", "").replace('large.jpg', 't500x500.jpg')

            if "?in=" not in self.uri:
                try:
                    self.uri = f"{self.uri}?in=" + self.playlist_url.split("soundcloud.com/")[1]
                    self.info["uri"] = self.uri
                except:
                    pass

        else:
            self.info["extra"]["thumb"] = self.info.get("artworkUrl", "")

        self.thumb = self.info["extra"]["thumb"] or ""

    def __repr__(self):
        return f"{self.info['sourceName']} - {self.duration if not self.is_stream else 'stream'} - {self.authors_string} - {self.title}"

    @property
    def name(self) -> str:
        return self.title

    @property
    def single_title(self) -> str:
        return self.title

    @property
    def url(self) -> str:
        return self.info["uri"]

    @property
    def search_uri(self):
        return f"https://www.youtube.com/results?search_query={quote(self.title)}"

    @property
    def authors_md(self) -> str:
        return f"`{self.author}`"

    @property
    def authors_string(self) -> str:
        return f"{self.author}"

    @property
    def album_name(self) -> str:
        try:
            return self.info["extra"]["album"]["name"]
        except KeyError:
            return ""

    @property
    def album_url(self) -> str:
        try:
            return self.info["extra"]["album"]["url"]
        except KeyError:
            return ""

    @property
    def lyrics(self) -> str:
        try:
            return self.info["extra"]["lyrics"]
        except KeyError:
            return ""

    @property
    def requester(self) -> int:
        return self.info["extra"]["requester"]

    @property
    def autoplay(self) -> bool:
        try:
            return self.info["extra"]["autoplay"]
        except KeyError:
            return False

    @property
    def track_loops(self) -> int:
        return self.info["extra"]["track_loops"]

    @property
    def playlist_name(self) -> str:
        try:
            return self.playlist.name[:97]
        except AttributeError:
            return ""

    @property
    def playlist_url(self) -> str:
        try:
            return self.playlist.url
        except AttributeError:
            return ""


class LavalinkPlayer(wavelink.Player):
    bot: BotCore

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.version = 1.1
        self.volume = kwargs.get("volume", 100)
        self.guild: disnake.Guild = kwargs.pop('guild')
        self.text_channel: Union[disnake.TextChannel,
        disnake.VoiceChannel, disnake.Thread] = kwargs.pop('channel')
        self.message: Optional[disnake.Message] = kwargs.pop('message', None)
        self.static: bool = kwargs.pop('static', False)
        self.skin: str = kwargs.pop("skin", None) or self.bot.default_skin
        self.skin_static: str = kwargs.pop("skin_static", None) or self.bot.default_static_skin
        self.custom_skin_data = kwargs.pop("custom_skin_data", {})
        self.custom_skin_static_data = kwargs.pop("custom_skin_static_data", {})
        self.queue: deque = deque()
        self.played: deque = deque(maxlen=20)
        self.queue_autoplay: deque = deque(maxlen=30)
        self.failed_tracks: deque = deque(maxlen=30)
        self.autoplay: bool = kwargs.pop("autoplay", False)
        self.nightcore: bool = False
        self.slowmo: bool = False
        self.filter3d: bool = False
        self.loop = False
        self.last_track: Optional[LavalinkTrack] = None
        self.locked: bool = False
        self.is_previows_music: bool = False
        self.interaction_cooldown: bool = False
        self.votes: set = set()
        self.dj: set = set()
        self.player_creator: Optional[int] = kwargs.pop('player_creator', None)
        self.filters: dict = {}
        self.idle_task: Optional[asyncio.Task] = None
        self.members_timeout_task: Optional[asyncio.Task] = None
        self.idle_endtime: Optional[datetime.datetime] = None
        self.hint_rate = self.bot.config["HINT_RATE"]
        self.command_log: str = ""
        self.command_log_emoji: str = ""
        self.is_closing: bool = False
        self.last_message_id: Optional[int] = kwargs.pop("last_message_id", None)
        self.keep_connected: bool = kwargs.pop("keep_connected", False)
        self.update: bool = False
        self.updating: bool = False
        self.auto_update: int = 0
        self.listen_along_invite = kwargs.pop("listen_along_invite", "")
        self.message_updater_task: Optional[asyncio.Task] = None
        # limitar apenas para dj's e staff's
        self.restrict_mode = kwargs.pop('restrict_mode', False)
        self.ignore_np_once = False  # não invocar player controller em determinadas situações
        self.allowed_mentions = disnake.AllowedMentions(users=False, everyone=False, roles=False)
        self.uptime = kwargs.pop("uptime", None) or int(disnake.utils.utcnow().timestamp())
        # ativar/desativar modo controller (apenas para uso em skins)
        self.controller_mode = True
        self.mini_queue_feature = False
        self.mini_queue_enabled = False
        self.is_resuming = False
        self.is_purging = False
        self.auto_pause = False
        self._session_resuming = kwargs.pop("session_resuming", False)
        self.last_channel: Optional[disnake.VoiceChannel] = None
        self._rpc_update_task: Optional[asyncio.Task] = None
        self._new_node_task: Optional[asyncio.Task] = None
        self._queue_updater_task: Optional[asyncio.Task] = None
        self.auto_skip_track_task: Optional[asyncio.Task] = None
        self.oauth_token = os.environ.get("PLUGINS_YOUTUBE_OAUTH_REFRESHTOKEN", None)

        stage_template = kwargs.pop("stage_title_template", None)

        self.stage_title_event = kwargs.pop("stage_title_event", None)

        if self.stage_title_event is None:
            self.stage_title_event = bool(stage_template)

        self.stage_title_template: str = stage_template or "Tocando: {track.title} | {track.author}"
        self.last_stage_title = ""

        self.purge_mode = kwargs.pop("purge_mode", SongRequestPurgeMode.on_message)

        if self.static and self.purge_mode in (SongRequestPurgeMode.on_message, SongRequestPurgeMode.on_player_start):
            self.bot.loop.create_task(self.channel_cleanup())

        self.temp_embed: Optional[disnake.Embed] = None
        self.prefix_info = kwargs.pop("prefix", "")

        self.start_time = disnake.utils.utcnow()

        self.initial_hints = [
            "None"
        ]

        self.retry_setup_hints = False

        hint_platforms = []

        if self.bot.config["USE_YTDL"]:
            hint_platforms.append("youtube, soundcloud")

        if self.bot.spotify:
            hint_platforms.append("spotify")

        if hint_platforms:
            self.initial_hints.append(
                "Bạn có thể thêm/tích hợp liên kết hồ sơ/kênh từ " + " và ".join(hint_platforms) + " để chơi "
                 f"danh sách phát công khai trên kênh/hồ sơ thông qua lệnh phát {self.prefix_info}(không bao gồm "
                 "tên/liên kết) hoặc lệnh /play (thông qua tự động hoàn tất tìm kiếm). Hãy thử sử dụng "
                 f"lệnh /fav_manager hoặc {self.prefix_info}favmanager."
            )

        try:
            self.initial_hints.extend(kwargs.pop("extra_hints"))
        except:
            pass

        if self.volume != 100:
            self.bot.loop.create_task(self.set_volume(self.volume))

        self.current_hint: str = ""
        self.last_data: dict = {}
        self.check_skins()
        self.setup_features()
        self.setup_hints()
        self.native_yt: bool = True

        self.bot.dispatch("player_create", player=self)

    def __str__(self) -> str:
        return f"{self.node.identifier} (v{self.node.version})"

    def __repr__(self):
        return f"<volume={self.volume} " \
               f"current_position={time_format(self.position) if self.position else 'Idling'} " \
               f"queue={len(self.queue)} loop={self.loop} EQ=\"{self.eq}\" guild=\"{self.guild.name}\" " \
               f"node=\"{self.node.identifier}\" keep_connected=\"{self.keep_connected}\">"

    @property
    def has_thread(self):
        try:
            if isinstance(self.message.channel.parent, disnake.ForumChannel):
                return
        except AttributeError:
            pass

        return self.message and self.message.thread  # and not (self.message.thread.locked or self.message.thread.archived)

    @property
    def controller_link(self):
        if self.controller_mode:
            try:
                return f" [`🎛️`](<{self.message.jump_url}>)"
            except AttributeError:
                try:
                    return f" [`#️⃣`](<{self.text_channel.jump_url}>)"
                except AttributeError:
                    pass
        return ""

    @property
    def position(self):

        if not self.current:
            return 0

        if not self.is_playing:
            return 0

        if self.paused and not self.auto_pause:
            return min(self.last_position, self.current.duration)

        difference = (time() * 1000) - self.last_update
        position = self.last_position + difference

        if position > self.current.duration:
            return 0

        return min(position, self.current.duration)

    async def update_state(self, state: dict) -> None:
        state = state['state']

        if not self.auto_pause:
            self.last_position = state.get('position', 0)

        self.last_update = time() * 1000
        self.position_timestamp = state.get('time', 0)
        self.ping = state.get('ping', None)

    async def report_error(self, embed: disnake.Embed, track: Union[LavalinkTrack, PartialTrack]):

        cog = self.bot.get_cog("Music")

        if cog and cog.error_report_queue:

            embed.description += f"\n**Nguồn:** `{track.info['sourceName']}`" \
                                 f"\n**Máy chủ:** `{disnake.utils.escape_markdown(self.guild.name)} [{self.guild.id}]`"

            try:
                embed.description += f"\n**Kênh:** `{disnake.utils.escape_markdown(self.guild.me.voice.channel.name)} [{self.guild.me.voice.channel.id}]`\n"
            except:
                pass

            embed.description += f"**Dữ liệu:** <t:{int(disnake.utils.utcnow().timestamp())}:F>"

            if self.guild.icon:
                embed.set_thumbnail(url=self.guild.icon.with_format("png").url)

            await cog.error_report_queue.put({"embed": embed})

    

    async def send_track_error(self, embed: disnake.Embed, track: Union[LavalinkTrack, PartialTrack]):

        embed.description += f"\n**Nguồn:** `{track.info['sourceName']}`" \
                                 f"\n**Máy chủ:** `{disnake.utils.escape_markdown(self.guild.name)} [{self.guild.id}]`"

        try:
            embed.description += f"\n**Kênh:** `{disnake.utils.escape_markdown(self.guild.me.voice.channel.name)} [{self.guild.me.voice.channel.id}]`\n"
        except:
            pass

        embed.description += f"**Dữ liệu:** <t:{int(disnake.utils.utcnow().timestamp())}:F>"

        if self.guild.icon:
            embed.set_thumbnail(url=self.guild.icon.with_format("png").url)

        webhook = self.bot.config["TRACK_ERROR_LOG"]
        async with aiohttp.ClientSession() as sc:
            wb = disnake.Webhook.from_url(webhook, session=sc)
            await wb.send(embed=embed, username=self.bot.user.name, avatar_url=self.bot.user.display_avatar.url)

    async def hook(self, event) -> None:

        if self.is_closing:
            return

        await self.bot.wait_until_ready()

        if isinstance(event, wavelink.TrackEnd):

            if event.node != self.node:
                return

            self.bot.dispatch("wavelink_track_end", self.node, event)

            if self.locked:
                return

            if event.reason == "FINISHED":
                self.set_command_log()

            elif event.reason == "STOPPED":

                if len(self.queue) == 0:
                    return

                self.ignore_np_once = True

            else:
                return

            try:
                self.message_updater_task.cancel()
            except:
                pass

            await self.track_end()

            self.update = False

            await self.process_next()

            return

        if isinstance(event, wavelink.TrackStart):

            if event.node != self.node:
                return

            self.start_time = disnake.utils.utcnow()

            if not self.current.autoplay:
                self.queue_autoplay.clear()

            if self.auto_pause:
                return

            if not self.text_channel:
                return

            if isinstance(self.text_channel, disnake.Thread):
                send_message_perm = self.text_channel.parent.permissions_for(self.guild.me).send_messages_in_threads
            else:
                send_message_perm = self.text_channel.permissions_for(self.guild.me).send_messages

            if not send_message_perm:
                self.text_channel = None
                return

            if not self.guild.me.voice:
                try:
                    await self.bot.wait_for(
                        "voice_state_update", check=lambda m, b, a: m == self.guild.me and m.voice, timeout=7
                    )
                except asyncio.TimeoutError:
                    self.update = True
                    return

            try:
                await self.process_save_queue()
            except:
                traceback.print_exc()

            await asyncio.sleep(2)
            await self.update_stage_topic()
            return

        if isinstance(event, wavelink.TrackException):

            track = self.current or self.last_track
            node_info = f"`{event.node.identifier}`" if event.node.identifier == self.node.identifier else f"`{self.node.identifier} | {event.node.identifier}`"
            embed = disnake.Embed(
                description=f"**Không thể phát nhạc:\n[{track.title}]({track.uri or track.search_uri})** ```java\n{event.message}```\n"
                            f"**Gây ra bởi:** ```java\n{event.cause[:200]}```\n"
                            f"**Mức độ lỗi:** `{event.severity}`\n"
                            f"**Máy chủ âm nhạc:** {node_info}",
                color=disnake.Colour.red())

            error_format = pprint.pformat(event.data)

            async def send_report():

                print(("-" * 50) + f"\nLỗi phát nhạc: {track.uri or track.search_uri}\n"
                                   f"Máy chủ: {self.node.identifier}\n"
                                   f"{error_format}\n" + ("-" * 50))

                await self.report_error(embed, track)

            if event.node.identifier != self.node.identifier:
                await send_report()
                return

            if self.locked:
                self.set_command_log(
                    text=f"Bản sao của bài hát thất bại (cố gắng chơi lại): [`{fix_characters(track.title, 15)}`]({track.uri or track.search_uri}). **Gây ra:** `{event.cause}`")
                self.update = True
                await send_report()
                return

            self.locked = True

            self.current = None

            error_403 = False
            video_not_available = False

            cooldown = 10

            if event.cause.startswith((
                    "java.net.SocketTimeoutException: Read timed out",
                    "java.net.SocketException: Network is unreachable"
            )) \
                or (video_not_available:=event.cause.startswith((
                "com.sedmelluq.discord.lavaplayer.tools.FriendlyException: This video is not available",
                "com.sedmelluq.discord.lavaplayer.tools.FriendlyException: YouTube WebM streams are currently not supported."
            ))
            or event.message == "Video returned by YouTube isn't what was requested"):

                try:
                    self._new_node_task.cancel()
                except:
                    pass

                await send_report()

                if video_not_available:
                    self.native_yt = False
                    self.current = None
                    self.queue.appendleft(track)
                    self.locked = False
                    self.set_command_log(
                        text=f"Do sự cố kỹ thuật trên máy chủ `{self.node.identifier}` nên người chơi đang sử dụng phương pháp thay thế để tải nhạc từ YouTube"
                             "(Có lẽ bài hát được phát sẽ khác với mong đợi).",
                        emoji="⚠️"
                    )
                    await self.process_next(start_position=self.position)

                else:
                    self._new_node_task = self.bot.loop.create_task(self._wait_for_new_node(
                        f"Máy chủ nhạc **{self.node.identifier}** hiện không khả dụng"
                        f"(đang chờ máy chủ mới có sẵn)."))
                return

            if (event.error == "This IP address has been blocked by YouTube (429)" or
                #event.message == "Video returned by YouTube isn't what was requested" or
                (error_403 := event.cause.startswith(("java.lang.RuntimeException: Not success status code: 403",
                                                      "java.io.IOException: Invalid status code for video page response: 400")))
            ):

                if error_403 and self.node.retry_403:

                    if not hasattr(self, 'retries_403'):
                        self.retries_403 = {"last_time": None, 'counter': 0}

                    if not self.retries_403["last_time"] or ((disnake.utils.utcnow() - self.retries_403["last_time"]).total_seconds() > self.bot.pool.config.get("ERROR_403_RETRIES", 7)):
                        self.retries_403 = {"last_time": disnake.utils.utcnow(), 'counter': 0}
                        if self.auto_pause:
                            self.update = True
                        else:
                            await self.play(track, start=get_start_pos(self, track, self.bot.pool.config.get("ERROR_403_ADDITIONAL_MILLISECONDS", 430)))
                            await asyncio.sleep(3)
                        self.locked = False
                        self.update = True
                        return

                    elif self.retries_403["counter"] < 3:
                        self.retries_403["counter"] += 1
                        await asyncio.sleep(3)
                        self.retries_403["last_time"] = disnake.utils.utcnow()

                        if self.is_closing:
                            return

                        self.locked = False
                        self.set_command_log(
                            text=f'Lỗi 403 của YouTube xảy ra trong quá trình phát lại âm nhạc hiện tại. Thử lại: {self.retries_403["counter"]}/5...')
                        if not self.auto_pause:
                            self.update = True
                        else:
                            await self.play(track, start=get_start_pos(self, track, self.bot.pool.config.get("ERROR_403_ADDITIONAL_MILLISECONDS", 430)))
                            self.update = True
                        await send_report()
                        return

                    self.queue.append(track)

                self.retries_403 = {"last_time": None, 'counter': 0}

                if track.info["sourceName"] == "youtube" or (self.bot.config["PARTIALTRACK_SEARCH_PROVIDER"] == "ytsearch" and
                                                             track.info["sourceName"] == "spotify"):

                    await send_report()

                    # self.node.available = False

                    if self.node._closing:
                        return

                    await asyncio.sleep(3)

                    current_node: wavelink.Node = self.bot.music.nodes[self.node.identifier]
                    # current_node.close()

                    for player_id in list(self.node.players):

                        p = self.node.players[player_id]

                        node = [n for n in self.bot.music.nodes.values() if n.available and n.is_available]
                        p.current = p.last_track
                        if node:
                            await p.change_node(node[0].identifier)
                            p.set_command_log(f"Người chơi đã được kết nối lại trên một máy chủ âm nhạc mới: **{p.node.identifier}**.")

                            p.update = True
                            p.locked = False
                        else:
                            try:
                                p._new_node_task.cancel()
                            except:
                                pass
                            p._new_node_task = p.bot.loop.create_task(p._wait_for_new_node(
                                f"Máy chủ **{current_node.identifier}** đã nhận giới hạn của YouTube nên không khả dụng"
                                 f"vào lúc này (đang chờ máy chủ mới có sẵn)."))
                    return

            await send_report()

            start_position = 0

            if event.cause.startswith((
                    "java.lang.IllegalStateException: Failed to get media URL: 2000: An error occurred while decoding track token",
                    "java.lang.RuntimeException: Not success status code: 204",
                    "java.net.SocketTimeoutException: Connect timed out",
                    "java.lang.IllegalArgumentException: Invalid bitrate",
                    "java.net.UnknownHostException:",
                    "java.lang.IllegalStateException: Error from decoder",
                    "java.lang.IllegalStateException: Current position is beyond this element",
                    "com.sedmelluq.discord.lavaplayer.tools.io.PersistentHttpStream$PersistentHttpException: Not success status code: 403",
            )):

                if not hasattr(self, 'retries_general_errors'):
                    self.retries_general_errors = {'counter': 6, 'last_node': self.node.identifier, "last_time": disnake.utils.utcnow()}

                embed = None

                self.queue.appendleft(track)

                if self.retries_general_errors["counter"] < 1 and self.node.identifier == self.retries_general_errors["last_node"] and (disnake.utils.utcnow() - self.retries_general_errors["last_time"]).total_seconds() < 180:

                    try:
                        self._new_node_task.cancel()
                    except:
                        pass
                    self._new_node_task = self.bot.loop.create_task(self._wait_for_new_node(ignore_node=self.node.identifier))
                    return

                self.retries_general_errors["last_time"] = disnake.utils.utcnow()

                if self.retries_general_errors['last_node'] == self.node.identifier:
                    self.retries_general_errors['counter'] -= 1
                else:
                    self.retries_general_errors = {'counter': 6, 'last_node': self.node.identifier, "last_time": disnake.utils.utcnow()}

                start_position = get_start_pos(self, track)

                cooldown = 4

            elif event.cause == "java.lang.InterruptedException":
                embed = None
                self.queue.appendleft(track)
                try:
                    self._new_node_task.cancel()
                except:
                    pass
                self._new_node_task = self.bot.loop.create_task(self._wait_for_new_node())
                return

            elif not track.track_loops:
                self.failed_tracks.append(track)

            elif self.keep_connected and not track.autoplay and len(self.queue) > 15:
                self.queue.append(track)

            if isinstance(self.text_channel, disnake.Thread):
                send_message_perm = self.text_channel.parent.permissions_for(self.guild.me).send_messages_in_threads
            else:
                send_message_perm = self.text_channel.permissions_for(self.guild.me).send_messages

            if embed and self.text_channel and send_message_perm:
                await self.text_channel.send(embed=embed, delete_after=10)

            await asyncio.sleep(cooldown)

            self.locked = False
            await self.process_next(start_position=start_position)
            return

        if isinstance(event, wavelink.WebsocketClosed):

            if event.code == 1000:
                return

            if not self.guild.me:
                await self.destroy(force=True)
                return

            try:
                vc = self.last_channel or self.guild.me.voice.channel
            except AttributeError:
                vc = None

            if event.code == 4014 and self.guild.me.voice:
                pass
            else:
                self.bot.log.warning(
                    f"\nLỗi kênh thoại!"
                    f"\nBot: {self.bot.user} [{self.bot.user.id}] | " + (
                        "Online" if self.bot.is_ready() else "Offline") +
                    f"\nGuild: {self.guild.name} [{self.guild.id}]"
                    f"\nChannel: {vc.name} [{vc.id}]"
                    f"\nServer: {self.node.identifier} | code: {event.code} | reason: {event.reason}\n"
                )

            if self.is_closing:
                return

            if event.code in (
                    4000,  # internal error
                    1006,
                    1001,
                    4016,  # Connection started elsewhere
                    4005,  # Already authenticated.
                    4006,  # Session is no longer valid.
            ):
                try:
                    vc_id = self.guild.me.voice.channel.id
                except AttributeError:
                    vc_id = self.last_channel.id

                await asyncio.sleep(3)

                if self.is_closing:
                    return

                await self.connect(vc_id)
                return

            if event.code == 4014:
                await asyncio.sleep(1)
                if self.guild and self.guild.me.voice:
                    return
                self.set_command_log(f"Trình phát đã bị tắt do mất kết nối với kênh {self.last_channel.mention}...")
                await self.destroy(force=True)
                return

        if isinstance(event, wavelink.TrackStuck):

            try:
                self.message_updater_task.cancel()
            except:
                pass

            await self.track_end()

            self.update = False

            try:
                self.set_command_log(text=f"Bài hát [{fix_characters(self.current.single_title, 25)}]({self.current.uri}) bị lỗi.", emoji="⚠️")
            except:
                pass

            await self.process_next()

            return

        print(f"Unknown Wavelink event: {repr(event)}")

    async def channel_cleanup(self):

        try:
            parent = self.text_channel.parent
        except AttributeError:
            pass
        else:
            if isinstance(parent, disnake.ForumChannel) and self.text_channel.owner_id == self.bot.user.id and \
                    self.text_channel.message_count > 1:
                try:
                    await self.text_channel.purge(
                        check=lambda m: m.channel.id != m.id and (not m.pinned or not m.is_system()))
                except:
                    pass
                return

        try:
            self.last_message_id = int(self.last_message_id)
        except TypeError:
            return

        if self.last_message_id != self.text_channel.last_message_id:

            if isinstance(self.text_channel, disnake.Thread):
                check = (lambda m: m.id != self.last_message_id and not not m.pinned and (
                            not m.is_system() or m.type != disnake.MessageType.channel_name_change))
            else:
                check = (lambda m: m.id != self.last_message_id and not m.pinned)

            try:
                await self.text_channel.purge(check=check)
            except:
                print(f"Không xóa được tin nhắn kênh {self.text_channel} [ID: {self.text_channel.id}]:\n"
                      f"{traceback.format_exc()}")
                pass

    async def connect(self, channel_id: int, self_mute: bool = False, self_deaf: bool = False):
        self.last_channel = self.bot.get_channel(channel_id)
        await super().connect(channel_id, self_mute=self_mute, self_deaf=True)

    def process_hint(self):

        if random.choice([x for x in range(self.hint_rate)]) == 0:
            self.current_hint = ''
        else:
            self.current_hint = ""

    def setup_features(self):

        try:
            (self.bot.player_static_skins[self.skin_static]
             if self.static else self.bot.player_skins[self.skin]).setup_features(self)
        except:
            self.auto_update = 0
            self.controller_mode = True

    def setup_hints(self):

        hints = list(self.initial_hints)

        if self.static:
            hints.append("Có thể ghim bài hát/danh sách phát vào tin nhắn của trình phát khi ở "
                          "chờ/không hoạt động để cho phép các thành viên nghe chúng một cách công khai. Để thực hiện việc này, hãy sử dụng "
                          f"command /fav_manager hoặc {self.prefix_info}favmanager (chỉ những thành viên có quyền "
                          "quản lý máy chủ có thể sử dụng tính năng này).")

        elif self.bot.intents.message_content and self.controller_mode:
            hints.append("Khi tạo cuộc trò chuyện/chuỗi trong tin nhắn của người chơi, chế độ yêu cầu bài hát sẽ được kích hoạt "
                          "trong đó (cho phép bạn yêu cầu nhạc chỉ bằng cách gửi tên/link bài hát trong cuộc trò chuyện).")

        if len(self.bot.pool.bots) > 1:

            bots_in_guild = 0
            bots_outside_guild = 0

            for b in self.bot.pool.bots:

                if b == self.bot:
                    continue

                try:
                    self.bot.user.id
                except AttributeError:
                    self.retry_setup_hints = True
                    continue

                if b.get_guild(self.guild.id):
                    bots_in_guild += 1
                else:
                    bots_outside_guild += 1

            if bots_in_guild:
                hints.append(
                    "Nếu thành viên nào muốn sử dụng tôi trên kênh thoại khác mà không cần phải đợi tôi"
                     f"ngắt kết nối hoặc làm gián đoạn tôi khỏi kênh hiện tại, có nhiều {bots_in_guild} bot trên máy chủ hơn "
                     "hoạt động với cùng hệ thống/lệnh của tôi (sử dụng cùng tiền tố/lệnh gạch chéo)."
                     f"Thử nhập kênh thoại khác với kênh hiện tại của tôi và sử dụng lệnh "
                     f"{self.prefix_info}chơi hoặc /play."
                )

            elif bots_outside_guild:
                hints.append(
                    "Nếu thành viên nào muốn sử dụng tôi trên kênh thoại khác mà không cần phải đợi tôi"
                     f"ngắt kết nối hoặc làm gián đoạn tôi khỏi kênh hiện tại. Bạn có thể thêm thêm {bots_outside_guild} bot nữa không "
                     f"các tính năng bổ sung trên máy chủ hiện tại hoạt động với cùng hệ thống/lệnh của tôi (sử dụng cùng một "
                     f"lệnh tiền tố/dấu gạch chéo). Sử dụng lệnh mời hoặc /invite {self.prefix_info} để thêm chúng."
                )

        if self.controller_mode:
            hints.append(
                "Khi nhấp vào biểu tượng cảm xúc này 🎛️ trong thông báo của một số lệnh, bạn sẽ được chuyển hướng đến trình điều khiển người chơi."
            )

        random.shuffle(hints)
        # self.hints = cycle(hints)

    def check_skins(self):
        if self.skin.startswith("> custom_skin: "):
            if self.skin[15:] not in  self.custom_skin_data:
                self.skin = self.bot.default_skin
        elif self.skin not in self.bot.player_skins:
            self.skin = self.bot.default_skin

        if self.skin_static.startswith("> custom_skin: "):
            if self.skin_static[15:] not in  self.custom_skin_static_data:
                self.skin_static = self.bot.default_static_skin
        elif self.skin_static not in self.bot.player_static_skins:
            self.skin_static = self.bot.default_static_skin

    async def members_timeout(self, check: bool, force: bool = False, idle_timeout = None):

        update_log = False

        if self.auto_pause:

            # Keep auto-pause state untouched when channel is still empty.
            if not check:
                return

            if self.current:
                try:
                    await self.resolve_track(self.current)
                    self.paused = False
                    await self.play(self.current, start=0 if self.current.is_stream else self.position)
                except Exception:
                    traceback.print_exc()
            self.auto_pause = False
            update_log = True

        if check:

            try:
                if update_log:
                    self.set_command_log(emoji="🔰", text="Bài hát đã được tiếp tục phát")
                    if self.current:
                        await asyncio.sleep(1.5)
                        await self.invoke_np(rpc_update=True)
                    else:
                        await self.process_next()
                await self.update_stage_topic()
            except Exception:
                traceback.print_exc()
            return

        if not force:

            try:
                vc = self.guild.me.voice.channel
            except AttributeError:
                vc = self.last_channel

            if [m for m in vc.members if not m.bot and not (m.voice.deaf or m.voice.self_deaf)]:
                try:
                    self.auto_skip_track_task.cancel()
                except:
                    pass
                return
            
            if self.auto_pause:
                return

            if not idle_timeout:
                idle_timeout = self.bot.config["WAIT_FOR_MEMBERS_TIMEOUT"]

            await asyncio.sleep(idle_timeout)

            if [m for m in vc.members if not m.bot and not (m.voice.deaf or m.voice.self_deaf)]:
                try:
                    self.auto_skip_track_task.cancel()
                except:
                    pass
                return

        if self.keep_connected:

            if self.paused or self.auto_pause:
                return

            self.auto_pause = True
            await self.set_pause(True)
            self.set_command_log(text=f"Trình phát đã bị tạm dừng do thiếu thành viên trên kênh. "
                                      f"Bài hát sẽ tự động tiếp tục khi một thành viên tham gia kênh "
                                      f"<#{self.channel_id}>.", emoji="🔋")
            await self.invoke_np()

        else:

            if self.is_closing:
                return

            msg = f"**Trình phát đã bị tắt vì thiếu thành viên trên kênh" + (f"<#{self.guild.me.voice.channel.id}>"
                                                                               if self.guild.me.voice else '') + "...**"
            self.command_log = msg
            if not self.static and not self.has_thread:
                embed = disnake.Embed(
                    description=msg, color=self.bot.get_color(self.guild.me))
                try:
                    await self.text_channel.send(embed=embed, allowed_mentions=self.allowed_mentions)
                except:
                    pass

            await self.playerdestroy()

    async def get_autoqueue_tracks(self):

        try:
            return self.queue_autoplay.popleft()
        except:
            pass

        if self.locked:
            return None

        tracks_search = []

        for t in self.played + self.queue_autoplay:

            if len(tracks_search) > 4:
                break

            if t.duration < 90000:
                continue

            tracks_search.append(t)

        track = None
        tracks = []
        tracks_ytsearch = []

        exception = None

        if tracks_search:

            tracks_search.reverse()

            self.locked = True

            for track_data in tracks_search:

                if track_data.info["sourceName"] == "spotify" and self.bot.spotify:
                    track_ids = list(set(t.original_id for t in tracks_search if t.info["sourceName"] == "spotify"))[:5]

                    result = None

                    for i in range(3):
                        try:
                            result = await self.bot.loop.run_in_executor(None, lambda: self.bot.spotify.recommendations(seed_tracks=track_ids))
                            break
                        except Exception as e:
                            self.set_command_log(emoji="⚠️", text=f"Không tải được bài hát đề xuất từ Spotify, đang thử lại: {i+1} trên 3.")
                            self.update = True
                            traceback.print_exc()
                            exception = e
                            await asyncio.sleep(5)

                    if result:

                        tracks = []

                        for t in result["tracks"]:

                            try:
                                thumb = t["album"]["images"][0]["url"]
                            except (IndexError,KeyError):
                                thumb = ""

                            partial_track = PartialTrack(
                                    uri=t["external_urls"]["spotify"],
                                    author=t["artists"][0]["name"] or "Unknown Artist",
                                    title=t["name"],
                                    thumb=thumb,
                                    duration=t["duration_ms"],
                                    source_name="spotify",
                                    original_id=t["id"],
                                    requester=self.bot.user.id,
                                    autoplay=True,
                                )

                            partial_track.info["extra"]["authors"] = [fix_characters(i['name']) for i in t['artists'] if
                                                          f"feat. {i['name'].lower()}"
                                                          not in t['name'].lower()]

                            partial_track.info["extra"]["authors_md"] = ", ".join(
                                f"[`{a['name']}`]({a['external_urls']['spotify']})" for a in t["artists"])

                            try:
                                if t["album"]["name"] != t["name"]:
                                    partial_track.info["extra"]["album"] = {
                                        "name": t["album"]["name"],
                                        "url": t["album"]["external_urls"]["spotify"]
                                    }
                            except (AttributeError, KeyError):
                                pass

                            tracks.append(partial_track)

                if not tracks:
                    if track_data.info["sourceName"] == "youtube":
                        query = f"https://www.youtube.com/watch?v={track_data.ytid}&list=RD{track_data.ytid}"
                    else:
                        query = f"ytmsearch:{track_data.author}"

                    try:
                        tracks = await self.node.get_tracks(
                            query, track_cls=LavalinkTrack, playlist_cls=LavalinkPlaylist, autoplay=True,
                            requester=self.bot.user.id
                        )
                    except Exception as e:
                        if [err for err in ("Could not find tracks from mix", "Could not read mix page") if err in str(e)]:
                            try:
                                tracks_ytsearch = await self.node.get_tracks(
                                    f"ytsearch:\"{track_data.author}\"",
                                    track_cls=LavalinkTrack, playlist_cls=LavalinkPlaylist, autoplay=True,
                                    requester=self.bot.user.id)
                                track = track_data
                            except Exception as e:
                                exception = e
                                continue
                        else:
                            print(traceback.format_exc())
                            exception = e
                            await asyncio.sleep(1.5)
                            continue

                track = track_data
                break

            if not tracks:
                tracks = tracks_ytsearch
                tracks.reverse()

            if not tracks:
                self.locked = False

                if exception:
                    if isinstance(exception, wavelink.TrackLoadError):
                        error_msg = f"**Gây ra:** ```java\n{exception.cause}```\n" \
                                    f"**Tin nhắn:** `\n{exception.message}`\n" \
                                    f"**Mức độ:** `{exception.severity}`\n" \
                                    f"**Máy chủ âm nhạc:** `{self.node.identifier}`"
                    else:
                        error_msg = f"**Chi tiết:** ```py\n{repr(exception)}```"
                else:
                    error_msg = "Không có kết quả liên quan đến các bài hát được chơi..."

                try:
                    embed = disnake.Embed(
                        description=f"**Không lấy được dữ liệu tự động phát:\n"
                                    f"{error_msg}",
                        color=disnake.Colour.red())
                    await self.text_channel.send(embed=embed, delete_after=10)
                except:
                    traceback.print_exc()
                await asyncio.sleep(7)
                return

        try:
            tracks = tracks.tracks
        except AttributeError:
            pass

        try:
            tracks = [t for t in tracks if not [u for u in tracks_search if t.uri.startswith(u.uri)]]
        except:
            pass

        if track:

            info = {
                "title": track.title,
                "uri": track.uri
            }

            tracks_final = []

            for t in tracks:

                if t.is_stream:
                    continue

                if t.duration < 90000:
                    continue

                if track.ytid and track.ytid == t.ytid:
                    continue

                if not isinstance(t, PartialTrack):
                    t = LavalinkTrack(id_=t.id, info=t.info, autoplay=True, requester=self.bot.user.id)

                t.info["extra"]["related"] = info
                tracks_final.append(t)

            tracks.clear()
            self.queue_autoplay.extend(tracks_final)

        self.locked = False

        try:
            return self.queue_autoplay.popleft()
        except:
            return None

    async def process_next(self, start_position: Union[int, float] = 0, inter: disnake.MessageInteraction = None,
                           force_np=False, clear_autoqueue = True):

        if self.locked or self.is_closing:
            return

        if not self.node or not self.node.is_available:
            try:
                self._new_node_task.cancel()
            except:
                pass
            self._new_node_task = self.bot.loop.create_task(self._wait_for_new_node())
            return

        await self.bot.wait_until_ready()

        if not self.is_connected:
            return

        if not self.guild.me.voice:
            if self.last_channel:
                await self.connect(self.last_channel.id)
            else:
                return

        try:
            self.idle_task.cancel()
            self.idle_task = None
        except:
            pass

        try:
            track = self.queue.popleft()

        except:

            try:

                track = None

                if self.autoplay or self.keep_connected:
                    try:
                        track = await self.get_autoqueue_tracks()
                        clear_autoqueue = False
                    except:
                        traceback.print_exc()
                        self.locked = False
                        await asyncio.sleep(60)
                        if not self.current and (self.autoplay or self.keep_connected):
                            await self.process_next()
                        return

                if not track:
                    await self.stop()
                    self.idle_endtime = disnake.utils.utcnow() + datetime.timedelta(seconds=self.bot.config["IDLE_TIMEOUT"])
                    self.last_track = None
                    self.idle_task = self.bot.loop.create_task(self.idling_mode())
                    return

            except Exception:
                clear_autoqueue = False
                traceback.print_exc()
                track = None

        if not track:
            await self.process_next()
            return

        self.locked = True

        temp_id = None

        if isinstance(track, PartialTrack):

            if not track.id:
                try:
                    await self.resolve_track(track)
                except Exception as e:
                    try:
                        await self.text_channel.send(
                            embed=disnake.Embed(
                                description=f"Đã xảy ra sự cố khi xử lý bài hát [{track.title}]({track.uri})... "
                                            f"```py\n{repr(e)}```",
                                color=self.bot.get_color()
                            )
                        )
                    except:
                        traceback.print_exc()

                    self.locked = False

                    await self.process_next()
                    return

                if not track.id:
                    try:
                        await self.text_channel.send(
                            embed=disnake.Embed(
                                description=f"Bài hát [{track.title}]({track.uri}) không có sẵn...\n"
                                            f"Chuyển sang bài hát tiếp theo...",
                                color=self.bot.get_color()
                            ), delete_after=10
                        )
                    except:
                        traceback.print_exc()

                    await asyncio.sleep(10)

                    self.locked = False

                    await self.process_next()
                    return

        if not self.native_yt and (track.info["sourceName"] == "youtube" or track.info.get("sourceNameOrig") == "youtube"):

            if track.is_stream or track.duration > 600000:
                self.failed_tracks.append(track)
                await self.process_next()
                return

            temp_id = track.info.get("temp_id")

            if not temp_id:

                tracks = []

                exceptions = ""

                for provider in self.node.search_providers:
                    if provider in ("ytsearch", "ytmsearch"):
                        continue
                    try:
                        tracks = await self.node.get_tracks(f"{provider}:{track.title}")
                    except:
                        exceptions += f"{traceback.format_exc()}\n"
                        await asyncio.sleep(1)
                        continue

                try:
                    tracks = tracks.tracks
                except AttributeError:
                    pass

                if not [i in track.title.lower() for i in exclude_tags]:
                    final_result = []
                    for t in tracks:
                        if not any((i in t.title.lower()) for i in exclude_tags):
                            final_result.append(t)
                            break
                    tracks = final_result or tracks

                if not tracks:
                    if exceptions:
                        print(exceptions)
                    self.failed_tracks.append(track)
                    self.current = track
                    self.set_command_log(emoji="⚠️", text="Bài hát hiện tại sẽ bị bỏ qua do không có kết quả "
                                                          "trong chế độ tìm kiếm thay thế của YouTube.")
                    await self.invoke_np()
                    await asyncio.sleep(13)
                    self.locked = False
                    await self.process_next()
                    return

                temp_id = tracks[0].id

                track.info["temp_id"] = temp_id

        elif not track.id:

            if "&list=" in track.uri and (link_re := YOUTUBE_VIDEO_REG.match(track.uri)):
                query = link_re.group()
            else:
                query = track.uri

            try:
                t = await self.node.get_tracks(query, track_cls=LavalinkTrack, playlist_cls=LavalinkPlaylist)
            except Exception as e:
                traceback.print_exc()
                if "Video returned by YouTube isn't what was requested" in str(e):
                    self._new_node_task = self.bot.loop.create_task(self._wait_for_new_node(ignore_node=self.node.identifier))
                    return
                kwargs = {}
                if self.purge_mode == SongRequestPurgeMode.on_message:
                    kwargs["delete_after"] = 11
                try:
                    await self.text_channel.send(
                        embed=disnake.Embed(
                            description=f"**Đã có lỗi xảy ra khi cố gắng tìm bài hát:** [{track.title}]({track.uri}) ```py\n{repr(e)}```"
                        ),
                    **kwargs)
                except:
                    pass
                embed = disnake.Embed(
                    description=f"**Thất bại trog khi lấy thông tin PartialTrack:\n[{track.title}]({track.uri or track.search_uri})** ```py\n{repr(e)}```\n"
                                f"**Máy chủ âm nhạc:** `{self.node.identifier}`",
                    color=disnake.Colour.red())
                await self.report_error(embed, track)
                await asyncio.sleep(7)
                self.locked = False
                await self.process_next()
                return

            try:
                t = t.tracks
            except:
                pass

            if not t:
                try:
                    await self.text_channel.send(
                        embed=disnake.Embed(
                            description=f"Bài hát [{track.title}]({track.uri}) không có sẵn...\n"
                                        "Chuyển sang bài hát tiếp theo...",
                            color=self.bot.get_color()
                        ), delete_after=10
                    )
                except:
                    traceback.print_exc()

                await asyncio.sleep(10)

                self.locked = False

                await self.process_next()
                return

            track.id = t[0].id

        if clear_autoqueue:
            self.queue_autoplay.clear()

        self.last_track = track

        self.is_previows_music = False

        self.locked = False

        if track.is_stream:
            start_position = 0

        self.current = track
        self.last_update = 0
        self.last_position = start_position
        self.position_timestamp = 0
        self.paused = False if not self.auto_pause else True

        self.process_hint()

        if self.auto_pause:
            self.last_update = time() * 1000
            self.current = track
        else:
            await self.play(track, start=start_position, temp_id=temp_id)

            if self.loop != "current" or force_np or (not self.controller_mode and self.current.track_loops == 0):

                if start_position:
                    await asyncio.sleep(1)

                await self.invoke_np(
                    interaction=inter,
                    force=True if (self.static or not self.loop or not self.is_last_message()) else False,
                    rpc_update=True
                )

    async def process_idle_message(self):

        controller_opts = [
            disnake.SelectOption(
                emoji="<:add_music:588172015760965654>", value=PlayerControls.add_song, label="Thêm bài hát",
                description=f"Phát nhạc/danh sách phát mới."
            ),
            disnake.SelectOption(
                emoji="⭐", value=PlayerControls.enqueue_fav, label="Thêm bài hát từ yêu thích",
                description=f"Thêm yêu thích trong dòng."
            ),
        ]

        if (played := len(self.played)) or self.last_track:

            try:
                play_txt = self.played[-1].title
            except:
                play_txt = self.last_track.title

            controller_opts.extend(
                [
                    disnake.SelectOption(
                        emoji="⏮️", value=PlayerControls.back, label="Quay trở lại",
                        description=f"Nghe lại: {play_txt[:31]}"
                    ),
                    disnake.SelectOption(
                        label="Bật tự động phát (AutoPilot)", emoji="📼",
                        value=PlayerControls.autoplay,
                        description=f"Phát nhạc liên quan đến: {play_txt[:19]}"
                    ),
                ]
            )

        if played > 1:
            controller_opts.append(
                disnake.SelectOption(
                    emoji="↪️", value=PlayerControls.readd, label="Chơi lại tất cả bài hát",
                    description=f"Chơi lại tất cả các bài hát ({played})"
                )
            )

        controller_opts.append(
            disnake.SelectOption(
                emoji="<:stop:1140221258575925358>", value=PlayerControls.stop, label="Tắt nhạc",
                description=f"Tắt nhạc"
            ),
        )

        components = [
            disnake.ui.Select(
                placeholder="Thực hiện một hành động:", options=controller_opts,
                custom_id="musicplayer_dropdown_idle", min_values=0, max_values=1
            )
        ]

        guild_data = await self.bot.get_data(self.guild.id, db_name=DBModel.guilds)

        opts = [disnake.SelectOption(label=k, value=k, description=v['description']) for k, v in
                guild_data["player_controller"]["fav_links"].items()]

        if opts:
            components.append(
                disnake.ui.Select(
                    placeholder="Phát nhạc/danh sách phát của máy chủ.",
                    options=opts, custom_id="player_guild_pin"
                )
            )

        embed = disnake.Embed(
            description=f"**Không có bài hát nào trong hàng đợi... Thêm bài hát hoặc sử dụng một trong các tùy chọn bên dưới.",
            color=self.bot.get_color(self.guild.me)
        )

        if not self.keep_connected:
            embed.description += "\n\nLưu ý:** `Người chơi sẽ tự động tắt` " \
                        f"<t:{int((disnake.utils.utcnow() + datetime.timedelta(seconds=self.bot.config['IDLE_TIMEOUT'])).timestamp())}:R> " \
                        f"`nếu không có hành động nào được thực hiện..`"

        kwargs = {
            "embed": embed,
            "content": "None",
            "components": components,
            "allowed_mentions": self.allowed_mentions
        }

        try:
            if isinstance(self.text_channel.parent, disnake.ForumChannel) and self.static:
                kwargs["content"] = "💤 Chờ đợi những bài hát mới..."
        except:
            pass

        try:
            if self.has_thread or self.static or self.text_channel.last_message_id == self.message.id:
                try:
                    await self.message.edit(**kwargs)
                except:
                    traceback.print_exc()
                    if self.text_channel:
                        self.message = await self.text_channel.send(**kwargs)
                send_message = False
            else:
                send_message = True
        except AttributeError:
            send_message = True

        if send_message:
            try:
                await self.message.delete()
            except:
                pass
            self.message = await self.text_channel.send(**kwargs)

    async def idling_mode(self):

        try:
            vc = self.guild.me.voice.channel
        except AttributeError:
            vc = self.last_channel

        try:
            await self.process_rpc(vc)
        except:
            traceback.print_exc()

        try:
            await self.process_idle_message()
        except:
            traceback.print_exc()

        try:
            await self.update_stage_topic()
        except:
            pass

        if self.keep_connected:
            return
        
        if self.autoplay: #! Test fix: bot tự ngắt kể cả khi trong chế độ autoplay
            return

        await asyncio.sleep(self.bot.config["IDLE_TIMEOUT"])

        msg = "💤 **⠂Trình phát đã bị tắt do không hoạt động...**"

        try:
            if self.static or self.has_thread:
                self.command_log = msg
            else:
                embed = disnake.Embed(
                    description=msg, color=self.bot.get_color(self.guild.me))
                self.bot.loop.create_task(self.text_channel.send(
                    embed=embed, delete_after=120, allowed_mentions=self.allowed_mentions))
        except:
            traceback.print_exc()

        await self.playerdestroy()

    def set_command_log(self, text="", emoji=""):
        self.command_log = text
        self.command_log_emoji = emoji

    async def update_stage_topic(self, reconnect=True, clear=False):

        if not self.guild.me.voice:
            if reconnect:
                await self.connect(self.last_channel.id)
            return

        if not self.stage_title_event:
            return

        if clear:
            if isinstance(self.guild.me.voice.channel, disnake.VoiceChannel) and self.last_stage_title:
                await self.bot.edit_voice_channel_status(status=None, channel_id=self.guild.me.voice.channel.id)
            return

        msg = None

        if self.current:

            requester = self.guild.get_member(self.current.requester)

            if requester:
                requester_name = str(requester.display_name)
            else:
                requester_name = "Thành viên không xác định"

            if not self.current.is_stream and (not self.auto_pause or not self.paused):
                if isinstance(self.guild.me.voice.channel, disnake.StageChannel):
                    timestamp = str(self.current.duration)
                else:
                    timestamp = f"<t:{int((disnake.utils.utcnow() + datetime.timedelta(milliseconds=self.current.duration - self.position)).timestamp())}:R>"
            else:
                timestamp = ("tạm dừng " if (self.paused) else "🔴 ") + f"<t:{int(disnake.utils.utcnow().timestamp())}:R>"

            msg = self.stage_title_template \
                .replace("{track.title}", self.current.single_title) \
                .replace("{track.author}", self.current.authors_string) \
                .replace("{track.duration}",
                         time_format(self.current.duration) if not self.current.is_stream else "Livestream") \
                .replace("{track.source}", self.current.info.get("sourceName", "không xác định")) \
                .replace("{track.playlist}", self.current.playlist_name or "Không có danh sách phát") \
                .replace("{requester.name}", requester_name) \
                .replace("{requester.id}", str(self.current.requester)) \
                .replace("{track.timestamp}", timestamp)

        if isinstance(self.guild.me.voice.channel, disnake.StageChannel):

            if not self.guild.me.guild_permissions.manage_guild:
                return

            if msg is not None:
                msg = msg.replace("{track.emoji}", "♪")

                if len(msg) > 110:
                    msg = msg[:107] + "..."

            if not msg:
                msg = "Trạng thái: Đang chờ nhạc mới."

            if not self.guild.me.voice.channel.instance:
                func = self.guild.me.voice.channel.create_instance
            elif msg == self.last_stage_title:
                return
            else:
                func = self.guild.me.voice.channel.instance.edit

            await func(topic=msg)

        else:  # voicechannel

            if msg is not None:

                msg = msg.replace("{track.emoji}", music_source_emoji(self.current.info["sourceName"]))

                if len(msg) > 496:
                    msg = msg[:496] + "..."

            if msg == self.last_stage_title:
                return

            try:
                await self.bot.edit_voice_channel_status(status=msg, channel_id=self.guild.me.voice.channel.id)
            except Exception as e:
                if isinstance(e, disnake.Forbidden):
                    self.stage_title_event = False
                    self.set_command_log(emoji="❌", text="Trạng thái tự động đã bị tắt do không có quyền thay đổi trạng thái.")
                    self.update = True
                print(traceback.format_exc())

        self.last_stage_title = msg

    def start_message_updater_task(self):
        try:
            self.message_updater_task.cancel()
        except AttributeError:
            pass
        self.message_updater_task = self.bot.loop.create_task(self.message_updater())

    async def invoke_np(self, force=False, interaction=None, rpc_update=False):

        if not self.text_channel:
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer()
            except:
                pass
            return

        if not self.current:
            if not force:
                try:
                    if not interaction.response.is_done():
                        await interaction.response.defer()
                except:
                    pass
            else:
                await self.process_idle_message()
            return

        if self.updating and not force:
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer()
            except:
                pass
            return

        if rpc_update:

            try:
                await self.process_rpc()
            except:
                traceback.print_exc()

        if self.retry_setup_hints:
            self.setup_hints()
            self.retry_setup_hints = False

        try:
            if self.static:
                if self.skin_static.startswith("> custom_skin: "):
                    data = skin_converter(self.custom_skin_static_data[self.skin_static[15:]], player=self)
                else:
                    data = self.bot.player_static_skins[self.skin_static].load(self)

            else:
                if self.skin.startswith("> custom_skin: "):
                    data = skin_converter(self.custom_skin_data[self.skin[15:]], player=self)
                else:
                    data = self.bot.player_skins[self.skin].load(self)
        except OverflowError:
            await self.process_next()
            return

        if data == self.last_data:

            try:
                if not interaction.response.is_done():
                    await interaction.response.defer()
            except:
                pass
            return

        self.last_data = data

        try:
            if self.static and isinstance(self.text_channel.parent, disnake.ForumChannel):
                data["content"] = f"`{'▶️' if not self.paused else '⏸️'} {fix_characters(self.current.title, 50)}` |\n\n" + (data.get("content") or "")
        except:
            pass

        fallback_data = None
        v2_mode = False
        v2_send_flags = None
        v2_components = data.pop("components_v2", None)

        if self.bot.config.get("ENABLE_COMPONENTS_V2") and v2_components:
            fallback_data = dict(data)
            data = dict(data)
            data["components"] = v2_components
            data.pop("embeds", None)
            v2_mode = True
            v2_send_flags = disnake.MessageFlags(is_components_v2=True)

        if not self.controller_mode:

            if self.temp_embed:
                data["embeds"].insert(0, self.temp_embed)
                self.temp_embed = None

            self.updating = True

            if self.keep_connected:
                if not self.is_last_message():
                    await self.destroy_message()
                elif self.text_channel:
                    if not self.text_channel.permissions_for(self.guild.me).send_messages:
                        self.text_channel = None
                        self.message = None
                    else:
                        try:
                            await self.message.edit(allowed_mentions=self.allowed_mentions, **data)
                        except disnake.Forbidden:
                            self.message = None
                            self.text_channel = None
                        except:
                            self.message = await self.text_channel.send(allowed_mentions=self.allowed_mentions, **data)

            else:
                try:
                    self.message_updater_task.cancel()
                except:
                    pass
                self.message = await self.text_channel.send(allowed_mentions=self.allowed_mentions, **data)

            self.updating = False

        else:

            if data.get("components") is None:

                data["components"] = [
                    disnake.ui.Button(
                        emoji="⏯️", custom_id=PlayerControls.pause_resume, style=get_button_style(self.paused)),
                    disnake.ui.Button(
                        emoji="⏮️", custom_id=PlayerControls.back),
                    disnake.ui.Button(
                        emoji="⏹️", custom_id=PlayerControls.stop),
                    disnake.ui.Button(
                        emoji="⏭️", custom_id=PlayerControls.skip),
                    disnake.ui.Button(
                        emoji="<:music_queue:703761160679194734>", custom_id=PlayerControls.queue,
                        disabled=not self.queue),
                    disnake.ui.Select(
                        placeholder="Mais opções:",
                        custom_id="musicplayer_dropdown_inter",
                        min_values=0, max_values=1,
                        options=[
                            disnake.SelectOption(
                                label="Adicionar música", emoji="<:add_music:588172015760965654>",
                                value=PlayerControls.add_song,
                                description="Adicionar uma música/playlist na fila."
                            ),
                            disnake.SelectOption(
                                label="Adicionar favorito na fila", emoji="⭐",
                                value=PlayerControls.enqueue_fav,
                                description="Adicionar um de seus favoritos na fila."
                            ),
                            disnake.SelectOption(
                                label="Adicionar nos seus favoritos", emoji="💗",
                                value=PlayerControls.add_favorite,
                                description="Adicionar a música atual nos seus favoritos."
                            ),
                            disnake.SelectOption(
                                label="Tocar do inicio", emoji="⏪",
                                value=PlayerControls.seek_to_start,
                                description="Voltar o tempo da música atual para o inicio."
                            ),
                            disnake.SelectOption(
                                label=f"Volume: {self.volume}%", emoji="🔊",
                                value=PlayerControls.volume,
                                description="Ajustar volume."
                            ),
                            disnake.SelectOption(
                                label="Misturar", emoji="🔀",
                                value=PlayerControls.shuffle,
                                description="Misturar as músicas da fila."
                            ),
                            disnake.SelectOption(
                                label="Readicionar", emoji="🎶",
                                value=PlayerControls.readd,
                                description="Readicionar as músicas tocadas de volta na fila."
                            ),
                            disnake.SelectOption(
                                label="Repetição", emoji="🔁",
                                value=PlayerControls.loop_mode,
                                description="Ativar/Desativar repetição da música/fila."
                            ),
                            disnake.SelectOption(
                                label=("Desativar" if self.nightcore else "Ativar") + " o efeito nightcore", emoji="🇳",
                                value=PlayerControls.nightcore,
                                description="Efeito que aumenta velocidade e tom da música."
                            ),
                            disnake.SelectOption(
                                label=("Desativar" if self.autoplay else "Ativar") + " a reprodução automática",
                                emoji="🔄",
                                value=PlayerControls.autoplay,
                                description="Sistema de reprodução de música automática quando a fila tiver vazia."
                            ),
                            disnake.SelectOption(
                                label=("Desativar" if self.restrict_mode else "Ativar") + " o modo restrito",
                                emoji="🔐",
                                value=PlayerControls.restrict_mode,
                                description="Apenas DJ's/Staff's podem usar comandos restritos."
                            ),
                        ]
                    ),
                ]

                if self.current.ytid and self.node.support_lyric:
                    data["components"][5].options.append(
                        disnake.SelectOption(
                            label="Visualizar letras", emoji="📃",
                            value=PlayerControls.lyrics,
                            description="Obter letra da música atual."
                        )
                    )

                if self.mini_queue_feature:
                    data["components"][5].options.append(
                        disnake.SelectOption(
                            label="Mini-fila do player", emoji="<:music_queue:703761160679194734>",
                            value=PlayerControls.miniqueue,
                            description="Ativar/Desativar a mini-fila do player."
                        )
                    )

                if isinstance(self.last_channel, disnake.VoiceChannel):
                    txt = "Desativar" if self.stage_title_event else "Ativar"
                    data["components"][5].options.append(
                        disnake.SelectOption(
                            label=f"{txt} status automático", emoji="📢",
                            value=PlayerControls.stage_announce,
                            description=f"{txt} o status automático do canal de voz."
                        )
                    )

                if not self.static and not self.has_thread:
                    data["components"][5].options.append(
                        disnake.SelectOption(
                            label="Song-Request Thread", emoji="💬",
                            value=PlayerControls.song_request_thread,
                            description="Criar uma thread/conversa temporária para pedir músicas usando apenas o nome/link."
                        )
                    )

            self.updating = True

            if interaction:
                try:
                    if interaction.response.is_done():
                        await interaction.message.edit(allowed_mentions=self.allowed_mentions, **data)
                    else:
                        await interaction.response.edit_message(allowed_mentions=self.allowed_mentions,
                                                                **data)
                except Exception as e:
                    self.bot.log.warning(
                        f"components v2 edit failed in guild {self.guild.id} ({self.guild.name}): {repr(e)}"
                    )
                    if fallback_data:
                        try:
                            if interaction.response.is_done():
                                await interaction.message.edit(allowed_mentions=self.allowed_mentions, **fallback_data)
                            else:
                                await interaction.response.edit_message(
                                    allowed_mentions=self.allowed_mentions,
                                    **fallback_data,
                                )
                        except:
                            traceback.print_exc()
                    traceback.print_exc()
                self.updating = False
                self.start_message_updater_task()
                return

            else:

                if v2_mode and self.message and not self.has_thread and not self.static:
                    msg_flags = getattr(self.message, "flags", None)
                    if not (msg_flags and getattr(msg_flags, "is_components_v2", False)):
                        try:
                            await self.destroy_message()
                        except Exception:
                            pass

                if self.message and (
                        self.ignore_np_once or self.has_thread or self.static or not force or self.is_last_message()):

                    self.ignore_np_once = False

                    try:

                        try:
                            await self.message.edit(allowed_mentions=self.allowed_mentions, **data)
                            await asyncio.sleep(0.5)
                            
                        except asyncio.CancelledError:
                            pass
                            return
                        except Exception as e:
                            self.bot.log.warning(
                                f"components edit failed in guild {self.guild.id} ({self.guild.name}): {repr(e)}"
                            )
                            if fallback_data:
                                try:
                                    await self.message.edit(allowed_mentions=self.allowed_mentions, **fallback_data)
                                    await asyncio.sleep(0.5)
                                    self.start_message_updater_task()
                                    self.updating = False
                                    return
                                except:
                                    pass
                            self.text_channel = self.bot.get_channel(self.text_channel.id)

                            if not self.text_channel:
                                self.message = None
                                return

                            if isinstance(self.text_channel, disnake.Thread):

                                if not self.text_channel.parent.permissions_for(
                                        self.guild.me).send_messages_in_threads or not self.text_channel.permissions_for(
                                        self.guild.me).read_messages:
                                    return

                                if self.text_channel.locked:

                                    if not self.text_channel.parent.permissions_for(self.guild.me).manage_threads:
                                        self.text_channel = None
                                        self.message = None
                                        return
                                    else:
                                        await self.text_channel.edit(archived=False, locked=False)

                                elif self.text_channel.archived:

                                    if self.text_channel.owner_id == self.bot.user.id:
                                        await self.text_channel.edit(archived=False)
                                    else:
                                        await self.text_channel.send("Đang hủy lưu trữ chủ đề.", delete_after=2)

                                #elif ((
                                #              self.text_channel.archive_timestamp - disnake.utils.utcnow()).total_seconds() / 60) < (
                                #thread_archive_time[self.text_channel.auto_archive_duration]):
                                #    await self.text_channel.send("Evitando o tópico auto-arquivar...", delete_after=2)

                            elif not self.text_channel.permissions_for(
                                    self.guild.me).send_messages or not self.text_channel.permissions_for(
                                    self.guild.me).read_messages:
                                return

                        self.start_message_updater_task()
                        self.updating = False
                        return
                    except Exception as e:
                        self.updating = False
                        if self.static or self.has_thread:
                            self.set_command_log(
                                f"{(interaction.author.mention + ' ') if interaction else ''}Có lỗi trong tương tác: {repr(e)}",
                                "⚠️")
                            self.update = True
                            return

            await self.destroy_message()

            if not self.static:
                try:
                    send_data = dict(data)
                    if v2_mode and v2_send_flags:
                        send_data["flags"] = v2_send_flags
                    self.message = await self.text_channel.send(
                        allowed_mentions=self.allowed_mentions,
                        **send_data,
                    )
                except Exception as e:
                    self.bot.log.warning(
                        f"components send failed in guild {self.guild.id} ({self.guild.name}): {repr(e)}"
                    )
                    if fallback_data:
                        try:
                            self.message = await self.text_channel.send(
                                allowed_mentions=self.allowed_mentions,
                                **fallback_data,
                            )
                        except:
                            pass

            self.start_message_updater_task()

        self.updating = False

    async def set_pause(self, pause: bool) -> None:
        await super().set_pause(pause)

    async def destroy_message(self):

        try:
            self.message_updater_task.cancel()
        except:
            pass

        if self.static:
            return

        try:
            if self.guild.me:
                await self.message.delete()
        except:
            pass

        self.message = None

    def is_last_message(self):

        try:
            return self.last_message_id == self.message.id
        except AttributeError:
            return

    async def message_updater(self):

        while True:

            if not self.text_channel or not self.controller_mode:
                pass

            elif self.auto_update and not self.current.is_stream:

                await asyncio.sleep(self.auto_update)

                try:
                    await self.invoke_np()
                except:
                    pass

                continue

            elif self.update:

                try:
                    await self.invoke_np()
                except:
                    pass

                self.update = False

                await asyncio.sleep(5)

            await asyncio.sleep(10)

    async def update_message(self, interaction: disnake.Interaction = None, force=False, rpc_update=False):

        if rpc_update:
            await self.process_rpc()

        if force or (interaction and not interaction.response.is_done()):
            if self.controller_mode:
                await self.invoke_np(interaction=interaction)

        else:
            self.update = True

    async def cleanup(self, inter: disnake.MessageInteraction = None):

        self.queue.clear()
        self.played.clear()

        try:
            self.members_timeout_task.cancel()
        except:
            pass

        try:
            self.auto_skip_track_task.cancel()
        except:
            pass

        try:
            self._queue_updater_task.cancel()
        except:
            pass

        try:
            vc = self.guild.voice_client.channel
        except:
            vc = self.last_channel

        try:
            await self.process_rpc(vc, close=True)
        except:
            pass

        if self.guild.me:

            self.bot.loop.create_task(self.update_stage_topic(reconnect=False, clear=True))

            if self.static:

                try:
                    await send_idle_embed(inter or self.message, self.command_log, bot=self.bot)
                except:
                    pass

                if self.purge_mode == SongRequestPurgeMode.on_player_stop:
                    await self.channel_cleanup()

            else:

                try:
                    if self.has_thread:

                        try:
                            if inter.message.id == self.message.id:
                                func = inter.response.edit_message
                            else:
                                func = self.message.edit
                        except AttributeError:
                            try:
                                func = self.message.edit
                            except AttributeError:
                                func = None

                        if func:
                            try:
                                await func(
                                    embed=disnake.Embed(
                                        description=self.command_log,
                                        color=self.bot.get_color(self.guild.me)
                                    ), allowed_mentions=self.allowed_mentions,
                                    components=song_request_buttons
                                )
                                channel: disnake.Thread = self.bot.get_channel(self.message.id)

                                if channel.parent.permissions_for(self.guild.me).send_messages_in_threads:
                                    try:
                                        await channel.send(
                                            embed=disnake.Embed(
                                                color=self.bot.get_color(self.guild.me),
                                                description="**Phiên đặt hàng âm nhạc của cuộc trò chuyện hiện tại đã bị đóng.**",
                                            )
                                        )
                                    except:
                                        pass

                                if channel.owner.id == self.bot.user.id or channel.parent.permissions_for(
                                        self.guild.me).manage_threads:
                                    kwargs = {"archived": True, "locked": True}
                                else:
                                    kwargs = {}

                                await channel.edit(**kwargs)
                            except Exception:
                                print(
                                    f"Thất bại khi lưu trữ luồng máy chủ: {self.guild.name}\n{traceback.format_exc()}")

                    elif inter:
                        try:
                            await inter.response.edit_message(
                                content=None,
                                embed=disnake.Embed(
                                    description=f"🛑 ⠂{self.command_log}",
                                    color=self.bot.get_color(self.guild.me)),
                                components=song_request_buttons
                            )
                        except Exception:
                            await self.destroy_message()

                    else:
                        await self.destroy_message()
                except Exception:
                    pass

        try:
            self.message_updater_task.cancel()
        except:
            pass
        self.message_updater_task = None

        try:
            self._new_node_task.cancel()
        except:
            pass
        self._new_node_task = None

        try:
            self.idle_task.cancel()
        except:
            pass
        self.idle_task = None

    async def auto_skip_track(self):

        if not self.controller_mode or not self.current:
            return

        while True:

            try:

                try:
                    await self.process_save_queue()
                except:
                    pass

                try:
                    if self.current.is_stream:
                        return
                except AttributeError:
                    pass

                try:
                    await asyncio.sleep((self.current.duration - self.position) / 1000)
                except AttributeError:
                    return

                self.set_command_log()

                try:
                    await self.track_end()
                except Exception:
                    pass

                try:
                    await self.process_next()
                except:
                    print(traceback.format_exc())

                try:
                    await self.invoke_np(force=True)
                except:
                    pass

                try:
                    await self.update_stage_topic()
                except Exception:
                    pass

            except asyncio.CancelledError:
                return

            except Exception:
                return

    async def resolve_track(self, track: PartialTrack):

        if track.id:
            return

        try:

            exceptions = []

            try:
                to_search = track.info["search_uri"]
                check_duration = False
            except KeyError:
                to_search = f"{self.bot.config['PARTIALTRACK_SEARCH_PROVIDER']}:" + (f"\"{track.info['isrc']}\"" if track.info.get("isrc") else f"{track.single_title.replace(' - ', ' ')} - {track.authors_string}")
                check_duration = True

            try:
                tracks = (await self.node.get_tracks(to_search, track_cls=LavalinkTrack, playlist_cls=LavalinkPlaylist))
            except wavelink.TrackNotFound as e:
                exceptions.append(e)
                tracks = []

            if not tracks and self.bot.config['PARTIALTRACK_SEARCH_PROVIDER'] not in ("ytsearch", "ytmsearch", "scsearch"):

                if track.info.get("isrc"):
                    try:
                        tracks = await self.node.get_tracks(f"ytsearch:\"{track.info['isrc']}\"",track_cls=LavalinkTrack, playlist_cls=LavalinkPlaylist)
                    except Exception as e:
                        exceptions.append(e)

                if not tracks:
                    try:
                        tracks = await self.node.get_tracks(
                            f"ytsearch:{track.single_title.replace(' - ', ' ')} - {track.authors_string}")
                    except Exception as e:
                        exceptions.append(e)

            try:
                tracks = tracks.tracks
            except AttributeError:
                pass

            if not tracks:
                if exceptions:
                    print("Không xử lý được bài hát:\n" + "\n".join(repr(e) for e in exceptions))
                return

            selected_track = None

            for t in tracks:

                if t.is_stream:
                    continue

                if any((i in t.title.lower() and i not in track.title.lower()) for i in exclude_tags):
                    continue

                if check_duration and ((t.duration - 10000) < track.duration < (t.duration + 10000)):
                    selected_track = t
                    break

            if not selected_track:
                selected_track = tracks[0]

            track.id = selected_track.id
            track.info["length"] = selected_track.duration

        except Exception as e:
            traceback.print_exc()
            embed = disnake.Embed(
                description=f"**Không lấy được thông tin PartialTrack:\n[{track.title}]({track.uri or track.search_uri})** ```py\n{repr(e)}```\n"
                             f"**Máy chủ âm nhạc:** `{self.node.identifier}`",
                color=disnake.Colour.red())
            await self.send_track_error(embed, track)
            return

        return

    async def _wait_for_new_node(self, txt: str = None, ignore_node=None):

        self.locked = True

        try:
            self.auto_skip_track_task.cancel()
        except:
            pass

        original_log = self.command_log
        original_log_emoji = self.command_log_emoji

        self.set_command_log(
            txt or "Không có máy chủ âm nhạc có sẵn. Tôi sẽ thực hiện một số nỗ lực để kết nối với máy chủ âm nhạc mới.",
            emoji="⏰"
        )
        self.update = True

        while True:

            nodes = sorted([n for n in self.bot.music.nodes.values() if n.is_available and n.identifier != ignore_node],
                              key=lambda n: n.stats.players)
            if not nodes:
                await asyncio.sleep(5)
                continue

            node = nodes[0]

            self.native_yt = True           

            try:
                await self.change_node(node.identifier)
                self.locked = False
            except:
                await asyncio.sleep(5)
                continue

            if not self.guild.me.voice:
                try:
                    can_connect(self.last_channel, self.guild, bot=self.bot)
                except Exception as e:
                    self.set_command_log(f"Người chơi đã bị chấm dứt do lỗi: {e}")
                    await self.destroy()
                    return
                await self.connect(self.last_channel.id)

            self.set_command_log(emoji=original_log_emoji, text=original_log)

            try:
                if self.auto_pause:
                    self.auto_skip_track_task = self.bot.loop.create_task(self.auto_skip_track())
                else:
                    await self.invoke_np(force=True)
            except:
                pass

            self._new_node_task = None
            return

    async def _send_rpc_data(self, users: List[int], stats: dict):

        for u in users:

            stats["user"] = u

            try:
                token = self.bot.pool.rpc_token_cache[u]
            except KeyError:
                data = await self.bot.get_global_data(id_=u, db_name=DBModel.users)
                token = data["token"]

            if self.bot.config["ENABLE_RPC_AUTH"] and not token:
                continue

            stats["token"] = token

            try:
                await self.bot.ws_client.send(stats)
            except Exception:
                print(traceback.format_exc())

    async def process_rpc(
            self,
            voice_channel: Union[disnake.VoiceChannel, disnake.StageChannel] = None,
            close=False,
            users: List[int] = None,
            wait: bool = False
    ):
        try:
            if not voice_channel and not close:

                try:
                    voice_channel = self.bot.get_channel(self.channel_id) or self.bot.get_channel(
                        self.guild.voice_client.channel.id)
                except AttributeError:
                    voice_channel = self.last_channel

            if not users:
                try:
                    users = voice_channel.voice_states
                except AttributeError:
                    return

            thumb = self.bot.user.display_avatar.replace(
                size=512, static_format="png").url

            users = [u for u in users if u != self.bot.user.id]

            if close:

                stats = {
                    "op": "close",
                    "bot_id": self.bot.user.id,
                    "bot_name": str(self.bot.user),
                    "thumb": thumb,
                    "auth_enabled": self.bot.config["ENABLE_RPC_AUTH"]
                }

                if wait:
                    await self._send_rpc_data(users, stats)
                else:
                    try:
                        self._rpc_update_task.cancel()
                    except:
                        pass
                    self._rpc_update_task = self.bot.loop.create_task(self._send_rpc_data(users, stats))
                return

            if self.is_closing:
                return

            stats = {
                "op": "update",
                "track": None,
                "bot_id": self.bot.user.id,
                "bot_name": str(self.bot.user),
                "thumb": thumb,
                "auth_enabled": self.bot.config["ENABLE_RPC_AUTH"],
                "listen_along_invite": self.listen_along_invite
            }

            if not self.current:

                try:
                    bot_public = self.bot.appinfo.bot_public
                except AttributeError:
                    bot_public = False

                stats.update(
                    {
                        "op": "idle",
                        "bot_id": self.bot.user.id,
                        "invite_permissions": self.bot.config["INVITE_PERMISSIONS"],
                        "bot_name": str(self.bot.user),
                        "public": bot_public,
                        "support_server": self.bot.config["SUPPORT_SERVER"],
                    }
                )

                try:
                    stats["idle_endtime"] = int(self.idle_endtime.timestamp())
                except:
                    pass

            else:

                track: Union[LavalinkTrack, PartialTrack] = self.current

                stats["track"] = {
                    "source": track.info["sourceName"],
                    "thumb": track.thumb if len(track.thumb) < 257 else "",
                    "title": track.single_title,
                    "url": track.uri,
                    "author": track.authors_string,
                    "stream": track.is_stream,
                    "position": self.position,
                    "paused": self.is_paused,
                    "requester_id": track.requester,
                    "loop": self.current.track_loops or self.loop,
                    "queue": len(self.queue),
                    "247": self.keep_connected,
                    "autoplay": self.current.autoplay
                }

                if self.current.is_stream:
                    stats["track"]["duration"] = self.start_time.timestamp()
                else:
                    stats["track"]["duration"] = track.duration

                if track.playlist_name:
                    stats["track"].update(
                        {
                            "playlist_name": track.playlist_name,
                            "playlist_url": track.playlist_url,
                        }
                    )

                if track.album_name:
                    stats["track"].update(
                        {
                            "album_name": track.album_name,
                            "album_url": track.album_url,
                        }
                    )

            if wait:
                await self._send_rpc_data(users, stats)
            else:

                try:
                    self._rpc_update_task.cancel()
                except:
                    pass

                self._rpc_update_task = self.bot.loop.create_task(self._send_rpc_data(users, stats))

        except Exception:
            traceback.print_exc()

    async def process_save_queue(self, create_task=True):

        if self._session_resuming:
            await asyncio.sleep(10)
            self._session_resuming = False
            return

        if self._queue_updater_task and self.bot.config["PLAYER_SESSIONS_MONGODB"] and self.bot.config["MONGO"]:
            return

        cog = self.bot.get_cog("PlayerSession")

        if not cog:
            return

        try:
            self._queue_updater_task.cancel()
        except:
            pass

        await cog.save_info(self)

        if create_task:
            self._queue_updater_task = self.bot.loop.create_task(cog.queue_updater_task(self))

    async def track_end(self):

        self.votes.clear()

        self.locked = True

        await asyncio.sleep(0.5)

        if self.last_track:

            if not self.last_track.is_stream and self.last_track.duration < 60000:
                await asyncio.sleep(7)

            if self.loop == "current":
                self.queue.appendleft(self.last_track)
            elif self.is_previows_music:
                self.queue.insert(1, self.last_track)
                self.is_previows_music = False
            elif self.last_track.track_loops:
                self.last_track.info["extra"]["track_loops"] -= 1
                self.queue.insert(0, self.last_track)
            elif self.loop == "queue": # or self.keep_connected:
                if self.is_previows_music:
                    self.queue.insert(1, self.last_track)
                    self.is_previows_music = False
                else:
                    self.queue.append(self.last_track)
            elif not self.last_track.autoplay:
                self.played.append(self.last_track)

        elif self.is_previows_music:
            self.is_previows_music = False

        self.locked = False

    async def playerdestroy(self, *, force: bool = False, inter: disnake.MessageInteraction = None):
        self.bot.loop.create_task(self.process_destroy(force=force, inter=inter))

    async def process_destroy(self, force: bool = False, inter: disnake.MessageInteraction = None):

        if self.is_closing:
            return

        self.is_closing = True

        await self.cleanup(inter)

        try:
            channel = self.guild.voice_client.channel
        except AttributeError:
            channel = self.last_channel

        if isinstance(channel,
                      disnake.StageChannel) and self.stage_title_event and self.guild.me and self.guild.me.guild_permissions.manage_channels:

            if channel.instance:
                try:
                    await channel.instance.delete()
                except Exception:
                    traceback.print_exc()

        await super().destroy(force=force, guild=self.guild)
        try: 
            self.bot.log.info(f"{inter.bot.user.name}#{inter.bot.user.discriminator} - Player destroy at {inter.guild.name}")
        except AttributeError:
                self.bot.log.info(f"Player destroy at {self.guild.name}")

        self.bot.dispatch("player_destroy", player=self)

    #######################
    #### Filter Stuffs ####
    #######################

    async def seek(self, position: int = 0) -> None:
        self.last_position = position
        await super().seek(position=position)

    async def set_distortion(self, sin_offset: float = 0, sin_scale: float = 1.0, cos_offset: float = 0,
                             cos_scale: float = 1.0, tan_offset: float = 0, tan_scale: float = 1.0,
                             offset: float = 0, scale: float = 1.0, enabled: bool = True):

        if enabled:
            return await self.set_filter(
                AudioFilter.distortion(sin_offset, sin_scale, cos_offset, cos_scale, tan_offset, tan_scale, offset,
                                       scale))

        try:
            del self.filters['distortion']
        except KeyError:
            pass

        await self.update_filters()

    async def set_timescale(self, speed: float = 1.0, pitch: float = 1.0, rate: float = 1.0, enabled: bool = True):
        if enabled:
            return await self.set_filter(AudioFilter.timescale(speed=speed, pitch=pitch, rate=rate))

        try:
            del self.filters['timescale']
        except KeyError:
            pass

        await self.update_filters()

    async def set_karaoke(self, level: float = 1.0, mono_level: float = 1.0, filter_band: float = 220.0,
                          filter_width: float = 100.0, enabled: bool = True):
        if enabled:
            return await self.set_filter(
                AudioFilter.karaoke(level=level, mono_level=mono_level, filter_band=filter_band,
                                    filter_width=filter_width))

        try:
            del self.filters['karaoke']
        except KeyError:
            pass

        await self.update_filters()

    async def set_tremolo(self, frequency: float = 2.0, depth: float = 0.0, enabled: bool = True):
        if enabled:
            return await self.set_filter(AudioFilter.tremolo(frequency=frequency, depth=depth))

        try:
            del self.filters['tremolo']
        except KeyError:
            pass

        await self.update_filters()

    async def set_vibrato(self, frequency: float = 2.0, depth: float = 0.0, enabled: bool = True):
        if enabled:
            return await self.set_filter(AudioFilter.vibrato(frequency=frequency, depth=depth))

        try:
            del self.filters['vibrato']
        except KeyError:
            pass

        await self.update_filters()

    async def set_rotation(self, sample_rate: int = 5, enabled: bool = True):
        if enabled:
            return await self.set_filter(AudioFilter.rotation(sample_rate=sample_rate))

        try:
            del self.filters['rotation']
        except KeyError:
            pass

        await self.update_filters()

    async def set_lowpass(self, smoothing: float = 20.0, enabled: bool = True):
        if enabled:
            return await self.set_filter(AudioFilter.low_pass(smoothing=smoothing))

        try:
            del self.filters['lowpass']
        except KeyError:
            pass

        await self.update_filters()

    async def set_channelmix(self, left_to_left: float = 1.0, left_to_right: float = 0, right_to_left: float = 0,
                             right_to_right: float = 1.0, enabled: bool = True):
        if enabled:
            return await self.set_filter(
                AudioFilter.channel_mix(left_to_left=left_to_left, left_to_right=left_to_right,
                                        right_to_left=right_to_left, right_to_right=right_to_right))

        try:
            del self.filters['channelmix']
        except KeyError:
            pass

        await self.update_filters()

    async def set_eq(self, equalizer: wavelink.Equalizer):

        await self.set_filter(AudioFilter.equalizer(bands=equalizer.eq))
        self._equalizer = equalizer

    async def update_filters(self):
        if self.node.version == 3:
            await self.node._send(op="filters", **self.filters, guildId=str(self.guild_id))
        else:
            await self.node.update_player(guild_id=self.guild_id, data={"filters": self.filters})

    async def set_filter(self, filter_type: AudioFilter):

        self.filters.update(filter_type.filter)
        await self.update_filters()

        return filter_type
    
    async def turnallfilteroff(self):
        
        await self.set_rotation(enabled=False)
        await self.set_timescale(enabled=False)
        
        await self.update_filters()


def music_mode(bot: BotCore):
    return wavelink.Client(bot=bot)
