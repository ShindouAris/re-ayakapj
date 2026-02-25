# -*- coding: utf-8 -*-
import asyncio
import datetime
import json
import logging
import os.path
import pickle
import re
import traceback
import zlib
from base64 import b64decode
from copy import deepcopy
from random import shuffle
from typing import Union, Optional
from urllib.parse import urlparse, parse_qs

import aiofiles
import aiohttp
import disnake
from aiohttp import ClientConnectorCertificateError
from disnake.ext import commands

import wavelink
from utils.client import BotCore
from colorama import *
from utils.db import DBModel
from utils.music.checks import check_voice, has_player, has_source, is_requester, is_dj, \
    can_send_message_check, check_requester_channel, can_send_message, can_connect, check_deafen, check_pool_bots, \
    check_channel_limit, check_stage_topic, check_queue_loading, check_player_perm
from utils.music.converters import time_format, fix_characters, string_to_seconds, URL_REG, \
    YOUTUBE_VIDEO_REG, google_search, percentage, music_source_image
from utils.music.errors import GenericError, MissingVoicePerms, NoVoice, PoolException, parse_error, \
    EmptyFavIntegration
from utils.music.interactions import VolumeInteraction, QueueInteraction, SelectInteraction, FavMenuView, ViewMode, \
    SetStageTitle
from utils.music.models import LavalinkPlayer, LavalinkTrack, LavalinkPlaylist, PartialTrack
from utils.music.spotify import process_spotify, spotify_regex_w_user
from utils.others import check_cmd, send_idle_embed, CustomContext, PlayerControls, queue_track_index, \
    pool_command, string_to_file, CommandArgparse, music_source_emoji_url, SongRequestPurgeMode, \
    select_bot_pool, get_inter_guild_data, ProgressBar


class Music(commands.Cog):

    emoji = "🎶"
    name = "Âm nhạc"
    desc_prefix = f"[{emoji} {name}] | "

    search_sources_opts = [
        disnake.OptionChoice("Youtube", "ytsearch"),
        disnake.OptionChoice("Youtube Music", "ytmsearch"),
        disnake.OptionChoice("Soundcloud", "scsearch"),
    ]

    playlist_opts = [
        disnake.OptionChoice("Trộn danh sách phát", "shuffle"),
        disnake.OptionChoice("Đảo ngược danh sách phát", "reversed"),
    ]

    sources = {
        "yt": "ytsearch",
        "y": "ytsearch",
        "ytb": "ytsearch",
        "youtube": "ytsearch",
        "ytm": "ytmsearch",
        "ytmsc": "ytmsearch",
        "ytmusic": "ytmsearch",
        "youtubemusic": "ytmsearch",
        "sc": "scsearch",
        "scd": "scsearch",
        "soundcloud": "scsearch",
    }

    audio_formats = ("audio/mpeg", "audio/ogg", "audio/mp4", "audio/aac")

    def __init__(self, bot: BotCore):

        self.bot = bot

        self.extra_hints = bot.config["EXTRA_HINTS"].split("||")

        self.song_request_concurrency = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

        self.player_interaction_concurrency = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

        self.song_request_cooldown = commands.CooldownMapping.from_cooldown(rate=1, per=300,
                                                                            type=commands.BucketType.member)

        self.music_settings_cooldown = commands.CooldownMapping.from_cooldown(rate=3, per=15,
                                                                              type=commands.BucketType.guild)

        if self.bot.config["AUTO_ERROR_REPORT_WEBHOOK"]:
            self.error_report_queue = asyncio.Queue()
            self.error_report_task = bot.loop.create_task(self.error_report_loop())
        else:
            self.error_report_queue = None

    stage_cd = commands.CooldownMapping.from_cooldown(2, 45, commands.BucketType.guild)
    stage_mc = commands.MaxConcurrency(1, per=commands.BucketType.guild, wait=False)

    @is_dj()
    @has_source()
    @commands.has_guild_permissions(manage_guild=True)
    @pool_command(
        only_voiced=True, name="stageannounce", aliases=["stagevc", "togglestageannounce", "announce", "vcannounce",
                                                         "voicestatus", "setvcstatus", "setvoicestatus", "statusvc",
                                                         "vcstatus"],
        description="Kích hoạt hệ thống thông báo/trạng thái tự động của kênh bằng tên bài hát.",
        cooldown=stage_cd, max_concurrency=stage_mc, extras={"exclusive_cooldown": True},
        usage="{prefix}{cmd} <placeholders>\nEx: {track.author} - {track.title}"
    )
    async def stageannounce_legacy(self, ctx: CustomContext, *, template = ""):
        await self.stage_announce.callback(self=self, inter=ctx, template=template)

    @is_dj()
    @has_source()
    @commands.slash_command(
        description=f"{desc_prefix}Kích hoạt/chỉnh sửa hệ thống thông báo/trạng thái kênh tự động với tên bài hát.",
        extras={"only_voiced": True, "exclusive_cooldown": True}, cooldown=stage_cd, max_concurrency=stage_mc,
        default_member_permissions=disnake.Permissions(manage_guild=True)
    )
    async def stage_announce(
            self, inter: disnake.AppCmdInter,
            template: str = commands.Param(name="model", default="")
    ):

        if isinstance(template, commands.ParamInfo):
            template = ""

        try:
            bot = inter.music_bot
            guild = inter.music_guild
            author = guild.get_member(inter.author.id)
        except AttributeError:
            bot = inter.bot
            guild = inter.guild
            author = inter.author

        if not author.guild_permissions.manage_guild and not (await bot.is_owner(author)):
            raise GenericError("**Bạn không có quyền quản lý máy chủ để bật/tắt hệ thống này.**")

        await inter.response.defer(ephemeral=True)

        global_data = await self.bot.get_global_data(inter.guild_id, db_name=DBModel.guilds)

        if not template:
            view = SetStageTitle(ctx=inter, bot=bot, data=global_data, guild=guild)
            view.message = await inter.send(view=view, embed=view.build_embed())
            await view.wait()
        else:
            if not any(p in template for p in SetStageTitle.placeholders):
                raise GenericError(f"**Bạn nên sử dụng ít nhất một trình giữ chỗ hợp lệ:** {SetStageTitle.placeholder_text}")

            await inter.response.defer(ephemeral=True)

            player = bot.music.players[inter.guild_id]
            player.stage_title_event = True
            player.stage_title_template = template
            player.start_time = disnake.utils.utcnow()

            await player.update_stage_topic()

            await player.process_save_queue()

            player.set_command_log(text="Trạng thái tự động được kích hoạt", emoji="📢")

            player.update = True

            if isinstance(inter, CustomContext):
                await inter.send("**Trạng thái tự động được xác định thành công!**")
            else:
                await inter.edit_original_message("**Trạng thái tự động được xác định thành công!**")


    play_cd = commands.CooldownMapping.from_cooldown(3, 12, commands.BucketType.member)
    play_mc = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @check_voice()
    @can_send_message_check()
    @commands.message_command(name="add to queue", extras={"check_player": False},
                              cooldown=play_cd, max_concurrency=play_mc)
    async def message_play(self, inter: disnake.MessageCommandInteraction):

        if not inter.target.content:
            emb = disnake.Embed(description=f"Không có văn bản nào trong [tin nhắn]({inter.target.jump_url} đã chọn)...",
                                color=disnake.Colour.red())
            await inter.send(embed=emb, ephemeral=True)
            return

        await self.play.callback(
            self=self,
            inter=inter,
            query=inter.target.content,
            position=0,
            options="",
            manual_selection=False,
            source=None,
            repeat_amount=0,
            force_play="no",
        )

    @check_voice()
    @can_send_message_check()
    @commands.slash_command(name="search", extras={"check_player": False}, cooldown=play_cd, max_concurrency=play_mc,
                            description=f"{desc_prefix}Tìm kiếm và chọn một trong số kết quả để phát nhạc.")
    async def search(
            self,
            inter: disnake.AppCmdInter,
            query: str = commands.Param(name="search", desc="Tên hoặc liên kết của bài hát."),
            *,
            position: int = commands.Param(name="position", description=f"{desc_prefix}Đặt bài hát vào vị trí cụ thể",
                                        default=0),
            force_play: str = commands.Param(
                name="playnow",
                description="Phát nhạc ngay lập tức (thay vì thêm vào hàng đợi).",
                default="no",
                choices=[
                    disnake.OptionChoice(disnake.Localized("Có", data={disnake.Locale.pt_BR: "Sim"}), "yes"),
                    disnake.OptionChoice(disnake.Localized("Không", data={disnake.Locale.pt_BR: "Não"}), "no")
                ]
            ),
            options: str = commands.Param(name="options", description="Tùy chọn để xử lý danh sách phát",
                                        choices=playlist_opts, default=False),
            source: str = commands.Param(name="source",
                                        description="Chọn trang web để tìm kiếm nhạc (không phải liên kết)",
                                        choices=search_sources_opts,
                                        default="ytsearch"),
            repeat_amount: int = commands.Param(name="repeat", description="đặt số lần lặp lại.",
                                                default=0),
            server: str = commands.Param(name="server", desc="Sử dụng máy chủ nhạc cụ thể trong tìm kiếm.",
                                        default=None)
    ):

        await self.play.callback(
            self=self,
            inter=inter,
            query=query,
            position=position,
            force_play=force_play,
            options=options,
            manual_selection=True,
            source=source,
            repeat_amount=repeat_amount,
            server=server
        )

    @search.autocomplete("search")
    async def search_autocomplete(self, inter: disnake.Interaction, current: str):

        if not current:
            return []

        if URL_REG.match(current):
            return [current] if len(current) < 100 else []

        try:
            await check_pool_bots(inter, only_voiced=True)
            bot = inter.music_bot
        except GenericError:
            return [current[:99]]
        except:
            bot = inter.bot

        try:
            if not inter.author.voice:
                return []
        except AttributeError:
            return [current[:99]]

        return await google_search(bot, current)

    @is_dj()
    @has_player()
    @can_send_message_check()
    @commands.max_concurrency(1, commands.BucketType.guild)
    @commands.slash_command(
        extras={"only_voiced": True},
        description=f"{desc_prefix}Kết nối (hoặc chuyển sang một) kênh thoại."
    )
    async def connect(
            self,
            inter: disnake.AppCmdInter,
            channel: Union[disnake.VoiceChannel, disnake.StageChannel] = commands.Param(
                name="channel",
                description="Kênh để kết nối"
            )
    ):
        try:
            channel = inter.music_bot.get_channel(channel.id)
        except AttributeError:
            pass

        await self.do_connect(inter, channel)

    async def do_connect(
            self,
            ctx: Union[disnake.AppCmdInter, commands.Context, disnake.Message],
            channel: Union[disnake.VoiceChannel, disnake.StageChannel] = None,
            check_other_bots_in_vc: bool = False,
            bot: BotCore = None,
            me: disnake.Member = None,
            check_pool: bool = True,
    ):

        if not channel:
            try:
                channel = ctx.music_bot.get_channel(ctx.author.voice.channel.id) or ctx.author.voice.channel
            except AttributeError:
                channel = ctx.author.voice.channel

        if not bot:
            try:
                bot = ctx.music_bot
            except AttributeError:
                try:
                    bot = ctx.bot
                except:
                    bot = self.bot

        if not me:
            try:
                me = ctx.music_guild.me
            except AttributeError:
                me = ctx.guild.me

        try:
            guild_id = ctx.guild_id
        except AttributeError:
            guild_id = ctx.guild.id

        try:
            text_channel = ctx.music_bot.get_channel(ctx.channel.id)
        except AttributeError:
            text_channel = ctx.channel

        try:
            player = bot.music.players[guild_id]
        except KeyError:
            logging.debug(f"Player debug test 20: {bot.user} | {self.bot.user}")
            raise GenericError(
                f"**Trình phát bot {bot.user.mention} đã bị chấm dứt trước khi kết nối với kênh thoại"
                 f" (hoặc trình phát chưa được khởi chạy)...\nĐể đề phòng, hãy thử lại.**"
            )

        can_connect(channel, me.guild, check_other_bots_in_vc=check_other_bots_in_vc, bot=bot)

        deafen_check = True

        if isinstance(ctx, disnake.AppCmdInter) and ctx.application_command.name == self.connect.name:

            perms = channel.permissions_for(me)

            if not perms.connect or not perms.speak:
                raise MissingVoicePerms(channel)

            await player.connect(channel.id, self_deaf=True)

            if channel != me.voice and me.voice.channel:
                txt = [
                    f"đã chuyển tôi đến kênh <#{channel.id}>",
                     f"**Đã chuyển sang kênh thành công** <#{channel.id}>"
                ]

                deafen_check = False


            else:
                txt = [
                    f"đã kết nối tôi trên kênh <#{channel.id}>",
                     f"**Tôi đã kết nối với kênh** <#{channel.id}>"
                ]

            await self.interaction_message(ctx, txt, emoji="🔈", rpc_update=True)

        else:
            await player.connect(channel.id, self_deaf=True)

        try:
            player.members_timeout_task.cancel()
        except:
            pass

        if deafen_check and bot.config["GUILD_DEAFEN_WARN"]:

            retries = 0

            while retries < 5:

                if me.voice:
                    break

                await asyncio.sleep(1)
                retries += 0

            if not await check_deafen(me):
                await text_channel.send(
                    embed=disnake.Embed(
                        title="Để ý:",
                        description="Để duy trì sự riêng tư của bạn và giúp tôi tiết kiệm tiền"
                                     "các tính năng, tôi khuyên bạn nên tắt âm thanh kênh của mình bằng cách nhấp vào "
                                     "nhấp chuột phải vào tôi rồi chọn: tắt"
                                     "âm thanh trên máy chủ.",
                        color=self.bot.get_color(me),
                    ).set_image(
                        url="https://cdn.discordapp.com/attachments/554468640942981147/1012533546386210956/unknown.png"
                    ), delete_after=20
                )

        if isinstance(channel, disnake.StageChannel):

            while not me.voice:
                await asyncio.sleep(1)

            stage_perms = channel.permissions_for(me)

            if stage_perms.mute_members:
                await me.edit(suppress=False)
            else:
                embed = disnake.Embed(color=self.bot.get_color(me))

                embed.description = f"**Tôi cần một số nhân viên mời lên sân khấu phát biểu: "\
                                     f"[{channel.name}]({channel.jump_url}).**"

                embed.set_footer(
                    text="💡 Mẹo: để cho phép tôi tự động phát biểu trên sân khấu, bạn cần phải cấp cho tôi "
                          "quyền tắt tiếng thành viên (trên máy chủ hoặc chỉ trên kênh sân khấu đã chọn).")

                await text_channel.send(ctx.author.mention, embed=embed, delete_after=45)

    @can_send_message_check()
    @check_voice()
    @commands.bot_has_guild_permissions(send_messages=True)
    @commands.max_concurrency(1, commands.BucketType.member)
    @pool_command(name="addposition", description="Thêm nhạc vào một vị trí cụ thể trong hàng đợi.",
                  aliases=["adp", "addpos"], check_player=False, cooldown=play_cd, max_concurrency=play_mc,
                  usage="{prefix}{cmd} [posição(Nº)] [nome|link]\nEx: {prefix}{cmd} 2 sekai - burn me down")
    async def addpos_legacy(self, ctx: CustomContext, position: int, *, query: str):

        if position < 1:
            raise GenericError("**Số vị trí hàng đợi phải lớn hơn 1.**")
        await self.play.callback(self=self, inter=ctx, query=query, position=position, options=False,
                                 force_play="no", manual_selection=False,
                                 source=None, repeat_amount=0, server=None)

    stage_flags = CommandArgparse()
    stage_flags.add_argument('query', nargs='*', help="tên bài hát hoặc liên kết")
    stage_flags.add_argument('-position', '-pos', '-p', type=int, default=0, help='Đặt bài hát vào một vị trí cụ thể trong hàng đợi (nó sẽ bị bỏ qua nếu bạn sử dụng -next, v.v.).\nEx: -p 10')
    stage_flags.add_argument('-next', '-next', action='store_true', help='Thêm bài hát/danh sách phát ở đầu hàng đợi (tương đương với: -pos 1)')
    stage_flags.add_argument('-reverse', '-r', action='store_true', help='Đảo ngược thứ tự các bài hát đã thêm (chỉ hiệu quả khi thêm danh sách phát).')
    stage_flags.add_argument('-shuffle', '-sl', action='store_true', help='Trộn các bài hát đã thêm (chỉ hiệu quả khi thêm danh sách phát).')
    stage_flags.add_argument('-select', '-s', action='store_true', help='Chọn bài hát từ kết quả tìm thấy.')
    stage_flags.add_argument('-source', '-src', type=str, default="ytsearch", help='Tìm kiếm nhạc bằng một nguồn cụ thể [youtube/soundcloud, v.v.]')
    stage_flags.add_argument('-force', '-now', '-n', '-f', action='store_true', help='Phát bài hát đã thêm ngay lập tức (chỉ hiệu quả nếu có một bài hát hiện đang phát.) ' )
    stage_flags.add_argument('-loop', '-lp', type=int, default=0, help="Đặt số lần lặp lại của bài hát đã chọn.\nEx: -loop 5")
    stage_flags.add_argument('-server', '-sv', type=str, default=None, help='Sử dụng một máy chủ nhạc cụ thể.')

    @can_send_message_check()
    @commands.bot_has_guild_permissions(send_messages=True)
    @commands.max_concurrency(1, commands.BucketType.member)
    @pool_command(name="play", description="Phát nhạc trên kênh thoại.", aliases=["p"], return_first=True,
                  cooldown=play_cd, max_concurrency=play_mc, extras={"flags": stage_flags},
                  usage="{prefix}{cmd} [nome|link]\nEx: {prefix}{cmd} sekai - burn me down")
    async def play_legacy(self, ctx: CustomContext, *, flags: str = ""):

        args, unknown = ctx.command.extras['flags'].parse_known_args(flags.split())

        await self.play.callback(
            self = self,
            inter = ctx,
            query = " ".join(args.query + unknown),
            position= 1 if args.next else args.position if args.position > 0 else 0,
            options = "shuffle" if args.shuffle else "reversed" if args.reverse else None,
            force_play = "yes" if args.force else "no",
            manual_selection = args.select,
            source = self.sources.get(args.source),
            repeat_amount = args.loop,
            server = args.server
        )

    @can_send_message_check()
    @commands.bot_has_guild_permissions(send_messages=True)
    @pool_command(name="search", description="Tìm kiếm bài hát và chọn một bài từ kết quả để phát.",   
                  aliases=["sc"], return_first=True, cooldown=play_cd, max_concurrency=play_mc,
                  usage="{prefix}{cmd} [tên]\nVí dụ: {prefix}{cmd} sekai - burn me down")
    async def search_legacy(self, ctx: CustomContext, *, query):

        await self.play.callback(self=self, inter=ctx, query=query, position=0, options=False, force_play="no",
                                 manual_selection=True, source=None, repeat_amount=0, server=None)

    @can_send_message_check()
    @commands.slash_command(
        name="play_music_file",
        description=f"{desc_prefix}Phát tập tin nhạc trên kênh thoại.",
        extras={"return_first": True}, cooldown=play_cd, max_concurrency=play_mc
    )
    async def play_file(
            self,
            inter: Union[disnake.AppCmdInter, CustomContext],
            file: disnake.Attachment = commands.Param(
                name="file", description="Tệp âm thanh để phát hoặc thêm vào hàng đợi"
            ),
            position: int = commands.Param(name="position", description="Đặt bài hát ở một vị trí cụ thể",
                                           default=0),
            force_play: str = commands.Param(
                name="play_now",
                description="Phát bài hát ngay lập tức (thay vì thêm nó vào hàng đợi).",
                default="no",
                choices=[
                    disnake.OptionChoice(disnake.Localized("Yes", data={disnake.Locale.pt_BR: "Sim"}), "yes"),
                    disnake.OptionChoice(disnake.Localized("No", data={disnake.Locale.pt_BR: "Não"}), "no")
                ]
            ),
            repeat_amount: int = commands.Param(name="repeat", description="Số lần lặp lại.",
                                                default=0),
            server: str = commands.Param(name="server", desc="Sử dụng một máy chủ âm nhạc cụ thể khi tìm kiếm.",
                                         default=None),
    ):

        class DummyMessage:
            attachments = [file]

        try:
            thread = inter.message.thread
        except:
            thread = None
        inter.message = DummyMessage()
        inter.message.thread = thread

        await self.play.callback(self=self, inter=inter, query="", position=position, options=False, force_play=force_play,
                                 manual_selection=False, source=None, repeat_amount=repeat_amount, server=server)

    async def check_player_queue(self, user: disnake.User, bot: BotCore, guild_id: int, tracks: Union[list, LavalinkPlaylist] = None):

        count = self.bot.config["QUEUE_MAX_ENTRIES"]

        try:
            player: LavalinkPlayer = bot.music.players[guild_id]
        except KeyError:
            if count < 1:
                return tracks
            count += 1
        else:
            if count < 1:
                return tracks
            if len(player.queue) >= count and not (await bot.is_owner(user)):
                raise GenericError(f"**Hàng đợi đã đầy ({self.bot.config['QUEUE_MAX_ENTRIES']} bài hát).**")

        if tracks:

            if isinstance(tracks, list):
                if not await bot.is_owner(user):
                    tracks = tracks[:count]
            else:
                if not await bot.is_owner(user):
                    tracks.tracks = tracks.tracks[:count]

        return tracks

    @can_send_message_check()
    @commands.slash_command(
        description=f"{desc_prefix}Phát nhạc trên kênh thoại.",
        extras={"return_first": True}, cooldown=play_cd, max_concurrency=play_mc
    )
    async def play(
            self,
            inter: Union[disnake.AppCmdInter, CustomContext],
            query: str = commands.Param(name="search", desc="Tên bài hát hoặc liên kết."), *,
            position: int = commands.Param(name="position", description="Đặt bài hát ở một vị trí cụ thể",
                                           default=0),
            force_play: str = commands.Param(
                name="playnow",
                description="Phát bài hát ngay lập tức (thay vì thêm nó vào hàng đợi)..",
                default="no",
                choices=[
                    disnake.OptionChoice(disnake.Localized("Có", data={disnake.Locale.pt_BR: "Sim"}), "yes"),
                    disnake.OptionChoice(disnake.Localized("Không", data={disnake.Locale.pt_BR: "Não"}), "no")
                ]
            ),
            manual_selection: bool = commands.Param(name="manual_selection",
                                        description="Chọn bài hát một cách thủ công từ kết quả tìm kiếm",
                                        default=False),
            options: str = commands.Param(name="options", description="Tùy chọn xử lý danh sách phát",
                              choices=playlist_opts, default=False),
            source: str = commands.Param(name="source",
                             description="Chọn trang web để tìm kiếm nhạc (không phải liên kết)",
                             choices=search_sources_opts,
                             default="ytsearch"),
            repeat_amount: int = commands.Param(name="repeat", description="đặt số lần lặp lại.",
                                    default=0),
            server: str = commands.Param(name="server", desc="Sử dụng máy chủ nhạc cụ thể trong tìm kiếm.",
                             default=None),
    ):

        try:
            bot = inter.music_bot
            guild = inter.music_guild
        except AttributeError:
            bot = inter.bot
            guild = inter.guild

        msg = None
        inter, guild_data = await get_inter_guild_data(inter, bot)
        ephemeral = None

        if not inter.response.is_done():
            ephemeral = await self.is_request_channel(inter, data=guild_data, ignore_thread=True)
            await inter.response.defer(ephemeral=ephemeral)

        if not inter.author.voice:

            if not (c for c in guild.channels if c.permissions_for(inter.author).connect):
                raise GenericError(f"**Bạn chưa được kết nối với kênh thoại và không có kênh/giai đoạn thoại nào"
                                    "có sẵn trên máy chủ cấp cho bạn quyền kết nối.**")

            color = self.bot.get_color(guild.me)

            if isinstance(inter, CustomContext):
                func = inter.send
            else:
                func = inter.edit_original_message

            msg = await func(
                embed=disnake.Embed(
                    description=f"**{inter.author.mention} tham gia kênh thoại để phát nhạc của bạn.**\n"
                                 f"**Nếu bạn không kết nối với một kênh trong vòng 25 giây, thao tác này sẽ bị hủy.**",
                    color=color
                )
            )

            if msg:
                inter.store_message = msg

            try:
                await bot.wait_for("voice_state_update", timeout=25, check=lambda m, b, a: m.id == inter.author.id and m.voice)
            except asyncio.TimeoutError:
                try:
                    func = msg.edit
                except:
                    func = inter.edit_original_message
                await func(
                    embed=disnake.Embed(
                        description=f"**{inter.author.mention} thao tác đã bị hủy.**\n", color=color
                    )
                )
                return

            await asyncio.sleep(1)

            

            bot = None

            bot_missing_perms = []

            for b in sorted(inter.bot.pool.bots, key=lambda b: b.identifier):

                if not b.bot_ready:
                    continue

                if not (guild := b.get_guild(inter.guild_id)):
                    continue

                if not (author := guild.get_member(inter.author.id)):
                    continue

                inter.author = author

                if b.user.id in author.voice.channel.voice_states:
                    inter.music_bot = b
                    inter.music_guild = guild
                    bot = b
                    break

                channel = b.get_channel(inter.channel.id)

                if not channel:
                    continue

                if isinstance(channel, disnake.Thread):
                    send_message_perm = channel.parent.permissions_for(channel.guild.me).send_messages_in_threads
                else:
                    send_message_perm = channel.permissions_for(channel.guild.me).send_messages

                if not send_message_perm:
                    if not guild.me.voice:
                        bot_missing_perms.append(b)
                    continue

                if not guild.me.voice:
                    bot = b
                    break

            if bot_missing_perms:
                raise GenericError(f"**Có những bot âm nhạc có sẵn trên máy chủ nhưng chúng không được phép gửi tin nhắn trên kênh <#{inter.channel_id}>**:\n\n" + \
                ", ".join(b.user.mention for b in bot_missing_perms))

            if not bot:
                raise GenericError("**Không có bot có sẵn...**")

            try:
                inter.music_bot = bot
                inter.music_guild = guild
            except:
                pass

        else:
            channel = bot.get_channel(inter.channel.id)
            await check_pool_bots(inter, check_player=False)

        if force_play == "yes":
            await check_player_perm(inter=inter, bot=bot, channel=channel)

        can_send_message(channel, bot.user)

        if not guild.voice_client and not check_channel_limit(guild.me, inter.author.voice.channel):
            raise GenericError(f"**Kênh {inter.author.voice.channel.mention} đã đầy!**")

        await self.check_player_queue(inter.author, bot, guild.id)

        query = query.replace("\n", " ").strip()
        warn_message = None
        queue_loaded = False
        reg_query = None

        try:
            if isinstance(inter.message, disnake.Message):
                message_inter = inter.message
            else:
                message_inter = None
        except AttributeError:
            message_inter = None

        try:
            modal_message_id = int(inter.data.custom_id[15:])
        except:
            modal_message_id = None

        attachment: Optional[disnake.Attachment] = None

        try:
            voice_channel = bot.get_channel(inter.author.voice.channel.id)
        except AttributeError:
            raise NoVoice()

        try:
            player = bot.music.players[guild.id]

            if not server:
                node = player.node
            else:
                node = bot.music.get_node(server) or player.node

            guild_data = {}

        except KeyError:

            node = bot.music.get_node(server)

            if not node:
                node = await self.get_best_node(bot)

            guild_data = None

            if inter.bot == bot:
                inter, guild_data = await get_inter_guild_data(inter, bot)

            if not guild_data:
                guild_data = await bot.get_data(inter.guild_id, db_name=DBModel.guilds)

            if not guild.me.voice:
                can_connect(voice_channel, guild, guild_data["check_other_bots_in_vc"], bot=bot)

            static_player = guild_data['player_controller']

            if not inter.response.is_done():
                ephemeral = await self.is_request_channel(inter, data=guild_data, ignore_thread=True)
                await inter.response.defer(ephemeral=ephemeral)

            if static_player['channel']:
                channel, warn_message, message = await self.check_channel(guild_data, inter, channel, guild, bot)

        if ephemeral is None:
            ephemeral = await self.is_request_channel(inter, data=guild_data, ignore_thread=True)

        is_pin = None

        if not query:

            if self.bot.config["ENABLE_DISCORD_URLS_PLAYBACK"]:

                try:
                    attachment = inter.message.attachments[0]

                    if attachment.size > 8388608:
                        raise GenericError("**Tệp bạn gửi phải có kích thước nhỏ hơn hoặc bằng 8mb.**")

                    if attachment.content_type not in self.audio_formats:
                        raise GenericError("**Tệp bạn gửi không phải là định dạng âm nhạc hợp lệ...**")

                    query = attachment.url

                except IndexError:
                    pass

        try:
            user_data = inter.global_user_data
        except:
            user_data = await self.bot.get_global_data(inter.author.id, db_name=DBModel.users)
            try:
                inter.global_user_data = user_data
            except:
                pass

        if not query:

            embed = disnake.Embed(
                color=self.bot.get_color(guild.me),
                description="**Chọn một mục dưới đây:**\n"
                            f'Lưu ý: bạn chỉ có <t:{int((disnake.utils.utcnow() + datetime.timedelta(seconds=45)).timestamp())}:R> để chọn!'
            )

            try:
                if bot.user.id != self.bot.user.id:
                    embed.set_footer(text=f"Thông qua bot: {bot.user.display_name}", icon_url=bot.user.display_avatar.url)
            except AttributeError:
                pass

            kwargs = {
                "content": "",
                "embed": embed
            }

            try:
                if inter.message.author.bot:
                    kwargs["content"] = inter.author.mention
            except AttributeError:
                pass

            opts = [
                disnake.SelectOption(label="Yêu thích", value=">> [⭐ Yêu thích ⭐] <<", emoji="⭐"),
                disnake.SelectOption(label="Sử dụng tích hợp", value=">> [💠 Tích hợp 💠] <<", emoji="💠"),
            ]
            
            if os.path.isfile(f"./local_database/saved_queues_v1/users/{inter.author.id}.pkl"):
                opts.append(disnake.SelectOption(label="Hàng chờ đã lưu", value=">> [💾 Hàng chờ đã lưu 💾] <<", emoji="💾"))

            if user_data["last_tracks"]:
                opts.append(disnake.SelectOption(label="Thêm âm nhạc gần đây", value=">> [📑 Những bài hát gần đây 📑] <<", emoji="📑"))
                
            if isinstance(inter, disnake.MessageInteraction) and not inter.response.is_done():
                await inter.response.defer(ephemeral=ephemeral)

            if not guild_data:

                if inter.bot == bot:
                    inter, guild_data = await get_inter_guild_data(inter, bot)
                else:
                    inter, guild_data = await bot.get_data(inter.guild_id, db_name=DBModel.guilds)

            if guild_data["player_controller"]["fav_links"]:
                disnake.SelectOption(label="Yêu thích của máy chủ", value=">> [📌 Yêu thích của máy chủ 📌] <<", emoji="📌"),

            view = SelectInteraction(user=inter.author, timeout=45, opts=opts)

            try:
                await msg.edit(view=view, **kwargs)
            except AttributeError:
                try:
                    await inter.edit_original_message(view=view, **kwargs)
                except AttributeError:
                    msg = await inter.send(view=view, **kwargs)

            await view.wait()

            select_interaction = view.inter

            if not select_interaction or view.selected is False:

                text = "### Đã hết thời gian để chọn!" if view.selected is not False else "### Bị người dùng hủy."

                try:
                    await msg.edit(embed=disnake.Embed(description=text, color=self.bot.get_color(guild.me))
                                   )
                except AttributeError:
                    traceback.print_exc()
                    pass
                return

            if select_interaction.data.values[0] == "cancel":
                await msg.edit(
                    embed=disnake.Embed(
                        description="**Hủy bỏ lựa chọn!**",
                        color=self.bot.get_color(guild.me)
                    ),
                    components=None
                )
                return

            try:
                inter.store_message = msg
            except AttributeError:
                pass

            inter.token = select_interaction.token
            inter.id = select_interaction.id
            inter.response = select_interaction.response
            query = select_interaction.data.values[0]
            await inter.response.defer()

        fav_opts = []

        if query.startswith(">> [💠 Tích hợp 💠] <<"):
            query = ""
            for k, v in user_data["integration_links"].items():
                fav_opts.append(disnake.SelectOption(label=k[5:], value=f"> itg: {k}", description="[💠 Tích hợp 💠]", emoji=music_source_emoji_url(v)))

        elif query.startswith(">> [⭐ Yêu thích ⭐] <<"):
            query = ""
            for k, v in user_data["fav_links"].items():
                fav_opts.append(disnake.SelectOption(label=k, value=f"> fav: {k}", description="[⭐ Yêu thích ⭐]", emoji=music_source_emoji_url(v)))

        elif query.startswith(">> [📑 Những bài hát gần đây 📑] <<"):

            if not user_data["last_tracks"]:
                raise GenericError("**Bạn chưa yêu cầu bài hát nào trong lịch sử của bạn...**\n"
                                    "Chúng sẽ xuất hiện khi bạn thêm bài hát qua tìm kiếm hoặc liên kết.")

            query = ""
            for i, d in enumerate(user_data["last_tracks"]):
                fav_opts.append(disnake.SelectOption(label=d["name"], value=f"> lst: {i}", description="[📑 Những bài hát gần đây 📑]",
                                                     emoji=music_source_emoji_url(d["url"])))

        elif query.startswith(">> [📌 Yêu thích của máy chủ 📌] <<"):

            if not guild_data:

                if inter.bot == bot:
                    inter, guild_data = await get_inter_guild_data(inter, bot)
                else:
                    guild_data = await bot.get_data(inter.guild_id, db_name=DBModel.guilds)

            if not guild_data["player_controller"]["fav_links"]:
                raise GenericError("**Máy chủ không có liên kết cố định/yêu thích.**")
            
            for name, v in guild_data["player_controller"]["fav_links"].items():
                fav_opts.append(disnake.SelectOption(label=name, value=f"> pin: {name}", description="[📌 Yêu thích của máy chủ 📌]", emoji=music_source_emoji_url(v['url'])))

            is_pin = False

        if fav_opts:

            source = False

            if len(fav_opts) == 1:
                query = list(fav_opts)[0].value

            else:
                embed = disnake.Embed(
                    color=self.bot.get_color(guild.me),
                    description="**Chọn một mục bên dưới:**\n"
                                 f'Lưu ý: bạn chỉ có <t:{int((disnake.utils.utcnow() + datetime.timedelta(seconds=45)).timestamp())}:R> để chọn!'
                )

                try:
                    if bot.user.id != self.bot.user.id:
                        embed.set_footer(text=f"Thông qua: {bot.user.display_name}", icon_url=bot.user.display_avatar.url)
                except AttributeError:
                    pass

                kwargs = {
                    "content": "",
                    "embed": embed
                }

                try:
                    if inter.message.author.bot:
                        kwargs["content"] = inter.author.mention
                except AttributeError:
                    pass

                view = SelectInteraction(
                    user=inter.author,  timeout=45, opts=fav_opts
                )

                if isinstance(inter, disnake.MessageInteraction) and not inter.response.is_done():
                    await inter.response.defer(ephemeral=ephemeral)

                try:
                    func = msg.edit
                except AttributeError:
                    try:
                        if inter.response.is_done():
                            func = inter.edit_original_message
                        else:
                            func = inter.response.send_message
                            kwargs["ephemeral"] = ephemeral
                    except AttributeError:
                        kwargs["ephemeral"] = ephemeral
                        try:
                            func = inter.followup.send
                        except AttributeError:
                            func = inter.send

                msg = await func(view=view, **kwargs)

                await view.wait()

                select_interaction = view.inter

                if not select_interaction or view.selected is False:

                    embed = disnake.Embed(description="### Lựa chọn đã hết thời gian!" if view.selected is not False else "### Bị người dùng hủy.", color=self.bot.get_color(guild.me))

                    try:
                        await msg.edit(embed=embed)
                    except AttributeError:
                        try:
                            await select_interaction.response.edit_message(embed=embed)
                        except AttributeError:
                            traceback.print_exc()
                    return

                if select_interaction.data.values[0] == "cancel":
                    await msg.edit(
                        embed=disnake.Embed(
                            description="**Lựa chọn đã bị hủy bỏ**",
                            color=self.bot.get_color(guild.me)
                        ),
                        components=None
                    )
                    return

                try:
                    inter.store_message = msg
                except AttributeError:
                    pass

                inter.token = select_interaction.token
                inter.id = select_interaction.id
                inter.response = select_interaction.response
                query = select_interaction.data.values[0]

        elif not query:
            raise EmptyFavIntegration()

        loadtype = None
        tracks = []

        if query.startswith("> pin: "):
            if is_pin is None:
                is_pin = True
            if not guild_data:
                inter, guild_data = await get_inter_guild_data(inter, bot)
            query = guild_data["player_controller"]["fav_links"][query[7:]]['url']
            source = False

        elif query.startswith("> lst: "):
            query = user_data["last_tracks"][int(query[7:])]["url"]
            source = False

        elif query.startswith(("> fav: ", "> itg: ")):
            try:
                user_data = inter.global_user_data
            except AttributeError:
                user_data = await self.bot.get_global_data(inter.author.id, db_name=DBModel.users)
                try:
                    inter.global_user_data = user_data
                except:
                    pass

            if query.startswith("> fav:"):
                query = user_data["fav_links"][query[7:]]

            else:

                query = user_data["integration_links"][query[7:]]

                if (matches := spotify_regex_w_user.match(query)):

                    if not self.bot.spotify:
                        raise GenericError("**Hỗ trợ Spotify hiện không có sẵn...**")

                    url_type, user_id = matches.groups()

                    if url_type != "user":
                        raise GenericError("**Liên kết không được hỗ trợ bằng phương pháp này...**")

                    try:
                        await inter.response.defer(ephemeral=True)
                    except:
                        pass

                    result = await self.bot.loop.run_in_executor(None, lambda: self.bot.spotify.user_playlists(user_id))

                    info = {"entries": [{"title": t["name"], "url": t["external_urls"]["spotify"]} for t in result["items"]]}

                elif not self.bot.config["USE_YTDL"]:
                    raise GenericError("**Không có hỗ trợ cho loại yêu cầu này vào lúc này...**")

                else:

                    loop = self.bot.loop or asyncio.get_event_loop()

                    try:
                        await inter.response.defer(ephemeral=True)
                    except:
                        pass

                    info = await loop.run_in_executor(None, lambda: self.bot.pool.ytdl.extract_info(query, download=False))

                    try:
                        if not info["entries"]:
                            raise GenericError(f"**Nội dung không khả dụng (hoặc riêng tư):**\n{query}")
                    except KeyError:
                        raise GenericError("**Đã xảy ra lỗi khi cố gắng nhận kết quả cho tùy chọn đã chọn ... ** ")

                if len(info["entries"]) == 1:
                    query = info["entries"][0]['url']

                else:

                    emoji = music_source_emoji_url(query)

                    view = SelectInteraction(
                        user=inter.author,
                        opts=[
                            disnake.SelectOption(label=e['title'][:90], value=f"entrie_select_{c}",
                                                 emoji=emoji) for c, e in enumerate(info['entries'])
                        ], timeout=30)

                    embed = disnake.Embed(
                        description="**Chọn một danh sách phát bên dưới:**\n"
                                    f'Chọn một tùy chọn theo <t:{int((disnake.utils.utcnow() + datetime.timedelta(seconds=30)).timestamp())}:R> để tiếp tục.',
                        color=self.bot.get_color(guild.me)
                    )

                    kwargs = {}

                    try:
                        func = msg.edit
                    except AttributeError:
                        try:
                            func = inter.edit_original_message
                        except AttributeError:
                            kwargs["ephemeral"] = True
                            try:
                                func = inter.followup.send
                            except AttributeError:
                                func = inter.send

                    msg = await func(embed=embed, view=view, **kwargs)

                    await view.wait()

                    if not view.inter or view.selected is False:

                        try:
                            func = msg.edit
                        except:
                            func = view.inter.response.edit_message

                        await func(embed=disnake.Embed(color=self.bot.get_color(guild.me),
                            description="**Đã hết thời gian!**" if not view.selected is False else "### Bị người dùng hủy bỏ."))
                        return

                    query = info["entries"][int(view.selected[14:])]["url"]

                    if not isinstance(inter, disnake.ModalInteraction):
                        inter.token = view.inter.token
                        inter.id = view.inter.id
                        inter.response = view.inter.response
                    else:
                        inter = view.inter

            source = False

        elif query.startswith(">> [💾 Hàng chờ đã lưu 💾] <<"):

            try:
                async with aiofiles.open(f"./local_database/saved_queues_v1/users/{inter.author.id}.pkl", 'rb') as f:
                    f_content = await f.read()
                    try:
                        f_content = zlib.decompress(f_content)
                    except zlib.error:
                        pass
                    data = pickle.loads(f_content)
            except FileNotFoundError:
                raise GenericError("**Dòng đã lưu của bạn đã bị loại trừ...**")

            tracks = await self.check_player_queue(inter.author, bot, guild.id, self.bot.get_cog("PlayerSession").process_track_cls(data["tracks"])[0])
            node = await self.get_best_node(bot)
            queue_loaded = True
            source = False

        else:

            query = query.strip("<>")

            urls = URL_REG.findall(query)

            reg_query = {}

            if urls:
                query = urls[0]
                source = False

                if query.startswith("https://www.youtube.com/results"):
                    try:
                        query = f"ytsearch:{parse_qs(urlparse(query).query)['search_query'][0]}"
                    except:
                        raise GenericError(f"**Không có hỗ trợ cho liên kết thông tin:** {query}")
                    manual_selection = True

                elif query.startswith("https://www.youtube.com/live/"):
                    query = query.split("?")[0].replace("/live/", "/watch?v=")

                if not self.bot.config["ENABLE_DISCORD_URLS_PLAYBACK"] and "cdn.discordapp.com/attachments/" in query:
                    raise GenericError("**Hỗ trợ cho các liên kết Discord bị vô hiệu hóa.**")

                if query.startswith(("https://youtu.be/", "https://www.youtube.com/")):

                    for p in ("&ab_channel=", "&start_radio="):
                        if p in query:
                            try:
                                query = f'https://www.youtube.com/watch?v={re.search(r"v=([a-zA-Z0-9_-]+)", query).group(1)}'
                            except:
                                pass
                            break

                    if "&list=" in query and (link_re := YOUTUBE_VIDEO_REG.match(query)):

                        view = SelectInteraction(
                            user=inter.author,
                            opts=[
                                disnake.SelectOption(label="Bài hát", emoji="🎵",
                                                     description="Chỉ tải lên bài hát từ liên kết.", value="music"),
                                disnake.SelectOption(label="Playlist", emoji="🎶",
                                                     description="Tải danh sách bài hát hiện tại.", value="playlist"),
                            ], timeout=30)

                        embed = disnake.Embed(
                            description='**Liên kết chứa video có danh sách phát.**\n'
                                         f'Chọn một tùy chọn trong <t:{int((disnake.utils.utcnow() + datetime.timedelta(seconds=30)).timestamp())}:R> để tiếp tục.',
                            color=self.bot.get_color(guild.me)
                        )

                        try:
                            if bot.user.id != self.bot.user.id:
                                embed.set_footer(text=f"Thông qua bot: {bot.user.display_name}",
                                                 icon_url=bot.user.display_avatar.url)
                        except AttributeError:
                            pass

                        msg = await inter.send(embed=embed, view=view, ephemeral=ephemeral)

                        await view.wait()

                        if not view.inter or view.selected is False:

                            try:
                                func = inter.edit_original_message
                            except AttributeError:
                                func = msg.edit

                            mention = ""

                            try:
                                if inter.message.author.bot:
                                    mention = f"{inter.author.mention}, "
                            except AttributeError:
                                pass

                            await func(
                                content=f"{mention}{'thao tác đã bị hủy' if view.selected is not False else 'đã hết thời gian chờ'}" if view.selected is not False else "Đã bị hủy bởi người dùng.",
                                embed=None
                            )
                            return

                        if view.selected == "music":
                            query = link_re.group()

                        try:
                            inter.store_message = msg
                        except AttributeError:
                            pass

                        if not isinstance(inter, disnake.ModalInteraction):
                            inter.token = view.inter.token
                            inter.id = view.inter.id
                            inter.response = view.inter.response
                        else:
                            inter = view.inter

        if not inter.response.is_done():
            await inter.response.defer(ephemeral=ephemeral)

        if not queue_loaded:
            tracks, node = await self.get_tracks(query, inter.author, node=node, track_loops=repeat_amount, source=source, bot=bot)
            tracks = await self.check_player_queue(inter.author, bot, guild.id, tracks)

        try:
            player = bot.music.players[inter.guild_id]
        except KeyError:
            await check_pool_bots(inter, check_player=False)

            try:
                bot = inter.music_bot
                guild = inter.music_guild
                channel = bot.get_channel(inter.channel.id)
            except AttributeError:
                bot = inter.bot
                guild = inter.guild
                channel = inter.channel

            try:
                player = bot.music.players[inter.guild_id]
            except KeyError:
                player = None

                if not guild_data:

                    if inter.bot == bot:
                        inter, guild_data = await get_inter_guild_data(inter, bot)
                    else:
                        guild_data = await bot.get_data(inter.guild_id, db_name=DBModel.guilds)

                static_player = guild_data['player_controller']

                if static_player['channel']:
                    channel, warn_message, message = await self.check_channel(guild_data, inter, channel, guild, bot)

        if not player:
            player = await self.create_player(
                inter=inter, bot=bot, guild=guild, guild_data=guild_data, channel=channel,
                message_inter=message_inter, node=node, modal_message_id=modal_message_id
            )

        pos_txt = ""

        embed = disnake.Embed(color=disnake.Colour.red())

        embed.colour = self.bot.get_color(guild.me)

        position -= 1

        embed_description = ""

        if isinstance(tracks, list):

            if manual_selection and not queue_loaded and len(tracks) > 1:

                embed.description = f"**Chọn (các) bài hát mong muốn bên dưới:**"

                try:
                    func = inter.edit_original_message
                except AttributeError:
                    func = inter.send

                try:
                    add_id = f"_{inter.id}"
                except AttributeError:
                    add_id = ""

                tracks = tracks[:25]

                msg = await func(
                    embed=embed,
                    components=[
                        disnake.ui.Select(
                            placeholder='Kết quả:',
                            custom_id=f"track_selection{add_id}",
                            min_values=1,
                            max_values=len(tracks),
                            options=[
                                disnake.SelectOption(
                                    label=f"{n+1}. {t.title[:96]}",
                                    value=f"track_select_{n}",
                                    description=f"{t.author} [{time_format(t.duration)}]")
                                for n, t in enumerate(tracks)
                            ]
                        )
                    ]
                )

                def check_song_selection(i: Union[CustomContext, disnake.MessageInteraction]):

                    try:
                        return i.data.custom_id == f"track_selection_{inter.id}" and i.author == inter.author
                    except AttributeError:
                        return i.author == inter.author and i.message.id == msg.id

                try:
                    select_interaction: disnake.MessageInteraction = await self.bot.wait_for(
                        "dropdown",
                        timeout=45,
                        check=check_song_selection
                    )
                except asyncio.TimeoutError:
                    raise GenericError("Đã hết thời gian!")

                if len(select_interaction.data.values) > 1:

                    indexes = set(int(v[13:]) for v in select_interaction.data.values)

                    selected_tracks = []

                    for i in indexes:
                        for n, t in enumerate(tracks):
                            if i == n:
                                selected_tracks.append(t)
                                break

                    tracks = selected_tracks

                else:

                    tracks = tracks[int(select_interaction.data.values[0][13:])]

                if isinstance(inter, CustomContext):
                    inter.message = msg

                if reg_query is not None:
                    try:
                        reg_query = {"name": tracks.title, "url": tracks.uri}
                    except AttributeError:
                        reg_query = {"name": tracks[0].title, "url": tracks[0].uri}

            elif not queue_loaded:

                tracks = tracks[0]

                if tracks.info.get("sourceName") == "http":

                    if tracks.title == "Unknown title":
                        if attachment:
                            tracks.info["title"] = attachment.filename
                        else:
                            tracks.info["title"] = tracks.uri.split("/")[-1]
                        tracks.title = tracks.info["title"]

                    tracks.uri = ""

            if not isinstance(tracks, list):

                if force_play == "yes":
                    player.queue.insert(0, tracks)
                elif position < 0:
                    player.queue.append(tracks)
                else:
                    player.queue.insert(position, tracks)
                    pos_txt = f" tại vị trí {position + 1} của hàng đợi"

                duration = time_format(tracks.duration) if not tracks.is_stream else '🔴 Livestream'

                log_text = f"{inter.author.mention} đã thêm [`{fix_characters(tracks.title, 20)}`]({tracks.uri or tracks.search_uri}){pos_txt} `({duration})`."

                loadtype = "track"

                embed.set_author(
                    name=fix_characters(tracks.title, 35),
                    url=tracks.uri or tracks.search_uri,
                    icon_url=music_source_image(tracks.info['sourceName'])
                )
                embed.set_thumbnail(url=tracks.thumb)
                embed.description = f"`{fix_characters(tracks.author, 15)}`**┃**`{time_format(tracks.duration) if not tracks.is_stream else '🔴 Livestream'}`**┃**{inter.author.mention}"
                emoji = "🎵"
                if reg_query is not None:
                    reg_query = {"name": tracks.title, "url": tracks.uri}

            else:

                if options == "shuffle":
                    shuffle(tracks)

                if position < 0 or len(tracks) < 2:

                    if options == "reversed":
                        tracks.reverse()
                    for track in tracks:
                        player.queue.append(track)
                else:
                    if options != "reversed":
                        tracks.reverse()
                    for track in tracks:
                        player.queue.insert(position, track)

                    pos_txt = f" (Pos. {position + 1})"

                if queue_loaded:
                    log_text = f"{inter.author.mention} đã thêm `{len(tracks)} bài hát` qua: {query[7:]}."
                    title = f"Sử dụng các bài hát đã lưu từ {inter.author.display_name}"
                    icon_url = "https://i.ibb.co/51yMNPw/floppydisk.png"

                    tracks_playlists = {}

                    for t in tracks:
                        if t.playlist_name:
                            try:
                                tracks_playlists[t.playlist_url]["count"] += 1
                            except KeyError:
                                tracks_playlists[t.playlist_url] = {"name": t.playlist_name, "count": 1}

                    if tracks_playlists:
                        embed_description += "\n### Danh sách phát đã tải:\n" + "\n".join(f"[`{info['name']}`]({url}) `- {info['count']} bài hát` " for url, info in tracks_playlists.items()) + "\n"

                else:
                    query = fix_characters(query.replace(f"{source}:", '', 1), 25)
                    title = f"Tìm kiếm: {query}"
                    icon_url = music_source_image(tracks[0].info['sourceName'])
                    log_text = f"{inter.author.mention} đã thêm `{len(tracks)} bài hát` qua tìm kiếm: `{query}`{pos_txt}."

                total_duration = 0

                for t in tracks:
                    if not t.is_stream:
                        total_duration += t.duration

                embed.set_author(name="⠂" + title, icon_url=icon_url)
                embed.set_thumbnail(url=tracks[0].thumb)
                embed.description = f"`{len(tracks)} bài hát)`**┃**`{time_format(total_duration)}`**┃**{inter.author.mention}"
                emoji = "🎶"

        else:

            if options == "shuffle":
                shuffle(tracks.tracks)

            if position < 0 or len(tracks.tracks) < 2:

                if options == "reversed":
                    tracks.tracks.reverse()
                for track in tracks.tracks:
                    player.queue.append(track)
            else:
                if options != "reversed":
                    tracks.tracks.reverse()
                for track in tracks.tracks:
                    player.queue.insert(position, track)

                pos_txt = f" (Pos. {position + 1})"

            if tracks.tracks[0].info["sourceName"] == "youtube":
                try:
                    async with aiohttp.ClientSession().get((oembed_url:=f"https://www.youtube.com/oembed?url={query}")) as r:
                        try:
                            playlist_data = await r.json()
                        except:
                            raise Exception(f"{r.status} | {await r.text()}")
                    tracks.data["playlistInfo"]["thumb"] = playlist_data["thumbnail_url"]
                except Exception as e:
                    print(f"Không nhận được tác phẩm nghệ thuật từ danh sách phát: {oembed_url} | {repr(e)}")

            loadtype = "playlist"

            log_text = f"{inter.author.mention} đã thêm danh sách phát [`{fix_characters(tracks.name, 20)}`]({tracks.url}){pos_txt} `({len(tracks.tracks)})`."

            total_duration = 0

            for t in tracks.tracks:
                if not t.is_stream:
                    total_duration += t.duration

            try:
                embed.set_author(
                    name="⠂" + fix_characters(tracks.name, 35),
                    url=tracks.url,
                    icon_url=music_source_image(tracks.tracks[0].info['sourceName'])
                )
            except KeyError:
                embed.set_author(
                    name="⠂ Spotify Playlist",
                    icon_url=music_source_image(tracks.tracks[0].info['sourceName'])
                )
            embed.set_thumbnail(url=tracks.thumb)
            embed.description = f"`{len(tracks.tracks)} bài hát`**┃**`{time_format(total_duration)}`**┃**{inter.author.mention}"
            emoji = "🎶"

            if reg_query is not None:
                reg_query = {"name": tracks.name, "url": tracks.url}

        embed.description += player.controller_link

        if not is_pin:

            if not player.is_connected:
                try:
                    embed.description += f"\n`Kênh thoại` {voice_channel.mention}"
                except AttributeError:
                    pass

            embed.description += embed_description

            try:
                func = inter.edit_original_message
            except AttributeError:
                if msg:
                    func = msg.edit
                elif inter.message.author.id == bot.user.id:
                    func = inter.message.edit
                else:
                    func = inter.send

            try:
                if bot.user.id != self.bot.user.id:
                    embed.set_footer(text=f"Thông qua bot: {bot.user.display_name}", icon_url=bot.user.display_avatar.url)
            except AttributeError:
                pass

            if loadtype == "track":
                components = [
                    disnake.ui.Button(emoji="💗", label="Yêu thích", custom_id=PlayerControls.embed_add_fav),
                    disnake.ui.Button(emoji="▶️", label="Phát ngay", custom_id=PlayerControls.embed_forceplay),
                    disnake.ui.Button(emoji="<:add_music:588172015760965654>", label="Thêm vào hàng đợi",
                                      custom_id=PlayerControls.embed_enqueue_track),
                ]

            elif loadtype == "playlist":
                try:
                    self.bot.pool.enqueue_playlist_embed_cooldown.get_bucket(inter).update_rate_limit()
                except:
                    pass
                components = [
                    disnake.ui.Button(emoji="💗", label="Yêu thích", custom_id=PlayerControls.embed_add_fav),
                    disnake.ui.Button(emoji="<:add_music:588172015760965654>", label="Thêm vào hàng đợi",
                                      custom_id=PlayerControls.embed_enqueue_playlist)
                ]
            else:
                components = None

            await func(embed=embed, **{"components": components} if components else {"view": None})

        if not player.is_connected:

            try:
                guild_data["check_other_bots_in_vc"]
            except KeyError:
                if inter.bot == bot:
                    inter, guild_data = await get_inter_guild_data(inter, bot)
                else:
                    guild_data = await bot.get_data(inter.guild_id, db_name=DBModel.guilds)

            if not inter.author.voice:
                raise NoVoice()

            await self.do_connect(
                inter, channel=voice_channel,
                check_other_bots_in_vc=guild_data["check_other_bots_in_vc"],
                bot=bot, me=guild.me, check_pool=True
            )

        await self.process_music(inter=inter, force_play=force_play, ephemeral=ephemeral, user_data=user_data, player=player,
                                 log_text=log_text, emoji=emoji, warn_message=warn_message, reg_query=reg_query)

    @play.autocomplete("search")
    async def fav_add_autocomplete(self, inter: disnake.Interaction, query: str):

        if URL_REG.match(query):
            return [query] if len(query) < 100 else []

        favs = [">> [⭐ Yêu thích ⭐] <<", ">> [💠 Tích hợp 💠] <<", ">> [📌 Yêu thích của máy chủ 📌] <<"]

        if os.path.isfile(f"./local_database/saved_queues_v1/users/{inter.author.id}.pkl"):
            favs.append(">> [💾 Hàng chờ đã lưu 💾] <<")

        if not inter.guild:
            try:
                await check_pool_bots(inter, return_first=True)
            except:
                return [query] if len(query) < 100 else []

        try:
            vc = inter.author.voice
        except AttributeError:
            vc = True

        user_data = await self.bot.get_global_data(inter.author.id, db_name=DBModel.users)

        favs.extend([(f"{rec['url']} || {rec['name']}"[:100] if len(rec['url']) < 101 else rec['name'][:100]) for rec in user_data["last_tracks"]])

        if not vc or not query:
            return favs[:20]

        return await google_search(self.bot, query, max_entries=20) or favs[:20]

    skip_back_cd = commands.CooldownMapping.from_cooldown(2, 13, commands.BucketType.member)
    skip_back_mc = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    case_sensitive_args = CommandArgparse()
    case_sensitive_args.add_argument('-casesensitive', '-cs', action='store_true',
                             help="Tìm kiếm bài hát có cụm từ chính xác trong tên bài hát thay vì tìm kiếm từng chữ.")
    @check_stage_topic()
    @is_requester()
    @check_queue_loading()
    @check_voice()
    @pool_command(name="skip", aliases=["next", "n", "pular", "skipto"], cooldown=skip_back_cd,
                  max_concurrency=skip_back_mc, description=f"Bỏ qua bài hát hiện tại đang phát.",
                  extras={"flags": case_sensitive_args}, only_voiced=True,
                  usage="{prefix}{cmd} <termo>\nEx: {prefix}{cmd} sekai")
    async def skip_legacy(self, ctx: CustomContext, *, flags: str = ""):

        args, unknown = ctx.command.extras['flags'].parse_known_args(flags.split())

        if ctx.invoked_with == "skipto" and not unknown:
            raise GenericError("**Bạn phải thêm tên để sử dụng Skipto.**")

        await self.skip.callback(self=self, inter=ctx, query=" ".join(unknown), case_sensitive=args.casesensitive)

    @check_stage_topic()
    @is_requester()
    @check_queue_loading()
    @has_source()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Chuyển đến một bài hát cụ thể trong hàng đợi.",
        extras={"only_voiced": True}, cooldown=skip_back_cd, max_concurrency=skip_back_mc
    )
    async def skipto(
            self,
            inter: disnake.AppCmdInter,
            query: str = commands.Param(
                name="name",
                description="Tên của bài hát"
            ),
            case_sensitive: bool = commands.Param(
                name="exact_name", default=False,
                description="Tìm kiếm các bài hát với câu chính xác trong tên của bài hát thay vì tìm kiếm từng từ.",

            )
    ):

        await self.skip.callback(self=self, inter=inter, query=query, case_sensitive=case_sensitive)

    @check_stage_topic()
    @is_requester()
    @check_queue_loading()
    @has_source()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Bỏ qua bài hát hiện tại đang phát.",
        extras={"only_voiced": True}, cooldown=skip_back_cd, max_concurrency=skip_back_mc
    )
    async def skip(
            self,
            inter: disnake.AppCmdInter, *,
            query: str = commands.Param(
                name="name",
                description="Tên âm nhạc (hoàn chỉnh hoặc một phần của nó).",
                default=None,
            ),
            play_only: str = commands.Param(
                name=disnake.Localized("play_only", data={disnake.Locale.pt_BR: "tocar_apenas"}),
                choices=[
                    disnake.OptionChoice(
                        disnake.Localized("Yes", data={disnake.Locale.pt_BR: "Sim"}), "yes"
                    ),
                    disnake.OptionChoice(
                        disnake.Localized("No", data={disnake.Locale.pt_BR: "Não"}), "no"
                    )
                ],
                description="Chỉ phát bài hát ngay lập tức (không cần bỏ qua hàng đợi)",
                default="no"
            ),
            case_sensitive: bool = commands.Param(
                name="exact_name", default=False,
                description="Tìm kiếm các bài hát với tên chính xác trong tên của bài hát thay vì tìm kiếm từng từ.",

            )
    ):

        try:
            bot = inter.music_bot
            guild = inter.music_guild
        except AttributeError:
            bot = inter.bot
            guild = bot.get_guild(inter.guild_id)

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        ephemeral = await self.is_request_channel(inter)

        interaction = None

        if query:

            try:
                index = queue_track_index(inter, bot, query, case_sensitive=case_sensitive)[0][0]
            except IndexError:
                raise GenericError(f"**Không có bài hát nào phù hợp với tên: {query}**")

            track = player.queue[index]

            player.queue.append(player.last_track)
            player.last_track = None

            if player.loop == "current":
                player.loop = False

            if play_only == "yes":
                del player.queue[index]
                player.queue.appendleft(track)

            elif index > 0:
                player.queue.rotate(0 - index)

            player.set_command_log(emoji="⤵️", text=f"{inter.author.mention} đưa bài hát hiện tại để phát ngay lập tức.")

            embed = disnake.Embed(
                color=self.bot.get_color(guild.me),
                description= f"⤵️ **⠂{inter.author.mention} đưa bài hát**\n"
                             f"╰[`{fix_characters(track.title, 43)}`]({track.uri or track.search_uri}){player.controller_link} để phát ngay lập tức"
            )

            try:
                if bot.user.id != self.bot.user.id:
                    embed.set_footer(text=f"Thông qua: {bot.user.display_name}", icon_url=bot.user.display_avatar.url)
            except AttributeError:
                pass

            if isinstance(inter, disnake.MessageInteraction) and inter.data.custom_id == "queue_track_selection":
                await inter.response.edit_message(embed=embed, view=None)
            else:
                await inter.send(embed=embed, ephemeral=ephemeral)

        else:

            if isinstance(inter, disnake.MessageInteraction):
                player.set_command_log(text=f"{inter.author.mention} đã bỏ qua bài hát.", emoji="⏭️")
                if not inter.response.is_done():
                    try:
                        await inter.response.defer()
                    except:
                        pass
                interaction = inter
            else:

                player.set_command_log(emoji="⏭️", text=f"{inter.author.mention} Bỏ qua bài hát")

                embed = disnake.Embed(
                    color=self.bot.get_color(guild.me),
                    description=f"⏭️ **⠂{inter.author.mention} đã bỏ qua bài hát:\n"
                                f"╰[`{fix_characters(player.current.title, 43)}`]({player.current.uri or player.current.search_uri})**"
                                f"{player.controller_link}"
                )

                try:
                    if bot.user.id != self.bot.user.id:
                        embed.set_footer(text=f"Thông qua: {bot.user.display_name}", icon_url=bot.user.display_avatar.url)
                except AttributeError:
                    pass

                await inter.send(embed=embed, ephemeral=ephemeral)

            if player.loop == "current":
                player.loop = False

        try:
            (player.current or player.last_track).info["extra"]["track_loops"] = 0
        except AttributeError:
            pass

        await player.track_end()
        player.ignore_np_once = True
        await player.process_next(inter=interaction)

    @check_stage_topic()
    @is_dj()
    @check_queue_loading()
    @has_player()
    @check_voice()
    @pool_command(name="back", aliases=["b", "voltar"], description="Quay lại bài hát trước.", only_voiced=True,
                  cooldown=skip_back_cd, max_concurrency=skip_back_mc)
    async def back_legacy(self, ctx: CustomContext):
        await self.back.callback(self=self, inter=ctx)

    @check_stage_topic()
    @is_dj()
    @has_player()
    @check_queue_loading()
    @check_voice()
    @commands.max_concurrency(1, commands.BucketType.member)
    @commands.slash_command(
        description=f"{desc_prefix}Quay lại bài hát trước.",
        extras={"only_voiced": True}, cooldown=skip_back_cd, max_concurrency=skip_back_mc
    )
    async def back(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if not len(player.queue) and (player.keep_connected or not len(player.played)):
            await player.seek(0)
            await self.interaction_message(inter, "trở lại phần đầu của bài hát.", emoji="⏪")
            return

        try:
            track = player.played.pop()
        except:
            track = player.queue.pop()
            

        if player.current and not player.current.autoplay:
            player.queue.appendleft(player.current)

        player.last_track = None

        player.queue.appendleft(track)

        if isinstance(inter, disnake.MessageInteraction):
            interaction = inter
            player.set_command_log(text=f"{inter.author.mention} trở lại với bài hát hiện tại.", emoji="⏮️")
            await inter.response.defer()
        else:

            interaction = None

            t = player.queue[0]

            txt = [
                "trở lại với bài hát hiện tại.",
                f"⏮️ **⠂{inter.author.mention} trở lại với bài hát:\n╰[`{fix_characters(t.title, 43)}`]({t.uri or t.search_uri})**"
            ]

            await self.interaction_message(inter, txt, emoji="⏮️", store_embed=True)

        if player.loop == "current":
            player.loop = False

        player.ignore_np_once = True

        if not player.current:
            await player.process_next(inter=interaction)
        else:
            player.is_previows_music = True
            await player.track_end()
            await player.process_next(inter=interaction, force_np=True)

    @check_stage_topic()
    @check_queue_loading()
    @has_source()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Bỏ phiếu để bỏ qua bài hát hiện tại.",
        extras={"only_voiced": True}
    )
    async def voteskip(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
            guild = inter.music_guild
        except AttributeError:
            bot = inter.bot
            guild = inter.guild

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        embed = disnake.Embed()

        if inter.author.id in player.votes:
            raise GenericError("**Bạn đã bỏ phiếu để bỏ qua âm nhạc hiện tại.**")

        embed.colour = self.bot.get_color(guild.me)

        txt = [
            f"Được bình chọn để bỏ qua âm nhạc hiện tại (phiếu bầu: {len(player.votes) + 1}/{self.bot.config['VOTE_SKIP_AMOUNT']}).",
            f"{inter.author.mention} Được bình chọn để bỏ qua âm nhạc hiện tại (phiếu bầu: {len(player.votes) + 1}/{self.bot.config['VOTE_SKIP_AMOUNT']}).",
        ]

        if len(player.votes) < self.bot.config.get('VOTE_SKIP_AMOUNT', 3):
            embed.description = txt
            player.votes.add(inter.author.id)
            await self.interaction_message(inter, txt, emoji="✋")
            return

        await self.interaction_message(inter, txt, emoji="✋")
        await player.track_end()
        await player.process_next()

    volume_cd = commands.CooldownMapping.from_cooldown(1, 7, commands.BucketType.member)
    volume_mc = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @is_dj()
    @has_source()
    @check_voice()
    @pool_command(name="volume", description="Điều chỉnh âm lượng âm nhạc.", aliases=["vol", "v"], only_voiced=True,
                  cooldown=volume_cd, max_concurrency=volume_mc, usage="{prefix}{cmd} [mức]\nEx: {prefix}{cmd} 50")
    async def volume_legacy(self, ctx: CustomContext, level: int):

        if not 4 < level < 151:
            raise GenericError("**Lựa chọn không hợp lệ! Chọn từ 5 đến 150**", self_delete=7)

        await self.volume.callback(self=self, inter=ctx, value=int(level))

    @is_dj()
    @has_source()
    @check_voice()
    @commands.slash_command(description=f"{desc_prefix}Điều chỉnh âm lượng âm nhạc.", extras={"only_voiced": True},
                            cooldown=volume_cd, max_concurrency=volume_mc)
    async def volume(
            self,
            inter: disnake.AppCmdInter, *,
            value: int = commands.Param(name="level", description="Chọn từ 5 đến 150", min_value=5.0, max_value=150.0)
    ):

        try:
            bot = inter.music_bot
            guild = inter.music_guild
        except AttributeError:
            bot = inter.bot
            guild = inter.guild

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        embed = disnake.Embed(color=disnake.Colour.red())

        if value is None:

            view = VolumeInteraction(inter)

            embed.colour = self.bot.get_color(guild.me)
            embed.description = "**Chọn mức âm lượng bên dưới:**"

            try:
                if bot.user.id != self.bot.user.id:
                    embed.set_footer(text=f"Thông qua: {bot.user.display_name}", icon_url=bot.user.display_avatar.url)
            except AttributeError:
                pass

            await inter.send(embed=embed, ephemeral=await self.is_request_channel(inter), view=view)
            await view.wait()
            if view.volume is None:
                return

            value = view.volume

        elif not 4 < value < 151:
            raise GenericError("Âm lượng phải nằm trong khoảng từ ** 5 ** đến ** 150**.")

        await player.set_volume(value)

        txt = [f"điều chỉnh Âm lượng thành **{value}%**", f"🔊 **⠂{inter.author.mention} điều chỉnh Âm lượng thành {value}%**"]
        await self.interaction_message(inter, txt, emoji="🔊")

    pause_resume_cd = commands.CooldownMapping.from_cooldown(2, 7, commands.BucketType.member)
    pause_resume_mc =commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @is_dj()
    @has_source()
    @check_voice()
    @pool_command(name="pause", aliases=["pausar"], description="Tạm dừng âm nhạc.", only_voiced=True,
                  cooldown=pause_resume_cd, max_concurrency=pause_resume_mc)
    async def pause_legacy(self, ctx: CustomContext):
        await self.pause.callback(self=self, inter=ctx)

    @is_dj()
    @has_source()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Tạm dừng bài hát.", extras={"only_voiced": True},
        cooldown=pause_resume_cd, max_concurrency=pause_resume_mc
    )
    async def pause(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if player.paused:
            raise GenericError("**Bài hát đã bị tạm dừng.**")

        await player.set_pause(True)

        txt = ["tạm dừng âm nhạc.", f"⏸️ **⠂{inter.author.mention} đã tạm dừng bài hát.**"]

        await self.interaction_message(inter, txt, rpc_update=True, emoji="⏸️")
        await player.update_stage_topic()

    @is_dj()
    @has_source()
    @check_voice()
    @pool_command(name="resume", aliases=["unpause"], description="Tiếp tục âm nhạc.", only_voiced=True,
                  cooldown=pause_resume_cd, max_concurrency=pause_resume_mc)
    async def resume_legacy(self, ctx: CustomContext):
        await self.resume.callback(self=self, inter=ctx)

    @is_dj()
    @has_source()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Tiếp tục âm nhạc.",
        extras={"only_voiced": True}, cooldown=pause_resume_cd, max_concurrency=pause_resume_mc
    )
    async def resume(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if not player.paused:
            raise GenericError("**Bài hát không bị tạm dừng.**")

        await player.set_pause(False)

        txt = ["tiếp tục bài hát.", f"▶️ **⠂{inter.author.mention} Tiếp tục bài hát**"]
        await self.interaction_message(inter, txt, rpc_update=True, emoji="▶️")
        await player.update_stage_topic()

    seek_cd = commands.CooldownMapping.from_cooldown(2, 10, commands.BucketType.member)
    seek_mc =commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @check_stage_topic()
    @is_dj()
    @check_queue_loading()
    @has_source()
    @check_voice()
    @pool_command(name="seek", aliases=["sk"], description="Tua bài hát đến một thời gian cụ thể.",
                  only_voiced=True, cooldown=seek_cd, max_concurrency=seek_mc,
                  usage="{prefix}{cmd} [time]\n"
                        "Ex 1: {prefix}{cmd} 10 (time 0:10)\n"
                        "Ex 2: {prefix}{cmd} 1:45 (time 1:45)")
    async def seek_legacy(self, ctx: CustomContext, *, position: str):
        await self.seek.callback(self=self, inter=ctx, position=position)

    @check_stage_topic()
    @is_dj()
    @check_queue_loading()
    @has_source()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Tua bài hát đến một thời gian cụ thể.",
        extras={"only_voiced": True}, cooldown=seek_cd, max_concurrency=seek_mc
    )
    async def seek(
            self,
            inter: disnake.AppCmdInter,
            position: str = commands.Param(name="time", description="Thời gian để tiến/trở lại (ex: 1:45 / 40 / 0:30)")
    ):

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if player.current.is_stream:
            raise GenericError("**Bạn không thể sử dụng lệnh này trong livestream.**")

        position = position.split(" | ")[0].replace(" ", ":")

        seconds = string_to_seconds(position)

        if seconds is None:
            raise GenericError(
                "**Bạn đã sử dụng một thời gian không hợp lệ !Sử dụng giây (1 hoặc 2 chữ số) hoặc ở định dạng (phút):(giây)**")

        milliseconds = seconds * 1000

        if milliseconds < 0:
            milliseconds = 0

        if milliseconds > player.position:

            emoji = "⏩"

            txt = [
                f"đã tua thời gian của bài hát đến: `{time_format(milliseconds)}`",
                f"{emoji} **⠂{inter.author.mention} tua thời gian của bài hát đến:** `{time_format(milliseconds)}`"
            ]

        else:

            emoji = "⏪"

            txt = [
                f" đã tua thời gian của bài hát trở lại: `{time_format(milliseconds)}`",
                f"{emoji} **⠂{inter.author.mention} đưa thời gian của bài hát trở lại:** `{time_format(milliseconds)}`"
            ]

        await player.seek(milliseconds)

        if player.paused:
            await player.set_pause(False)

        await self.interaction_message(inter, txt, emoji=emoji)

        await asyncio.sleep(2)
        await player.update_stage_topic()
        await player.process_rpc()

    @seek.autocomplete("time")
    async def seek_suggestions(self, inter: disnake.Interaction, query: str):

        try:
            if not inter.author.voice:
                return
        except AttributeError:
            pass

        if query:
            return [time_format(string_to_seconds(query)*1000)]

        try:
            await check_pool_bots(inter, only_voiced=True)
            bot = inter.music_bot
        except:
            return

        try:
            player: LavalinkPlayer = bot.music.players[inter.guild_id]
        except KeyError:
            return

        if not player.current or player.current.is_stream:
            return

        seeks = []

        if player.current.duration >= 90000:
            times = [int(n * 0.5 * 10) for n in range(20)]
        else:
            times = [int(n * 1 * 10) for n in range(20)]

        for p in times:
            percent = percentage(p, player.current.duration)
            seeks.append(f"{time_format(percent)} | {p}%")

        return seeks

    loop_cd = commands.CooldownMapping.from_cooldown(3, 5, commands.BucketType.member)
    loop_mc =commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @is_dj()
    @has_source()
    @check_voice()
    @pool_command(
        description=f"Chọn Chế độ lặp lại giữa: bài hát hiện tại / hàng / Tắt / Số lần (sử dụng số).",
        only_voiced=True, cooldown=loop_cd, max_concurrency=loop_mc,
        usage="{prefix}{cmd} <quantidade|modo>\nEx 1: {prefix}{cmd} 1\nEx 2: {prefix}{cmd} queue")
    async def loop(self, ctx: CustomContext, mode: str = None):

        if not mode:

            embed = disnake.Embed(
                description="**Chọn chế độ lặp lại:**",
                color=self.bot.get_color(ctx.guild.me)
            )

            msg = await ctx.send(
                ctx.author.mention,
                embed=embed,
                components=[
                    disnake.ui.Select(
                        placeholder="Chọn một tùy chọn:",
                        custom_id="loop_mode_legacy",
                        options=[
                            disnake.SelectOption(label="Bài hát hiện tại", value="current"),
                            disnake.SelectOption(label="Hàng đợi", value="queue"),
                            disnake.SelectOption(label="Vô hiệu hóa", value="off")
                        ]
                    )
                ]
            )

            try:
                select: disnake.MessageInteraction = await self.bot.wait_for(
                    "dropdown", timeout=30,
                    check=lambda i: i.message.id == msg.id and i.author == ctx.author
                )
            except asyncio.TimeoutError:
                embed.description = "Thời gian lựa chọn đã hết!"
                try:
                    await msg.edit(embed=embed, view=None)
                except:
                    pass
                return

            mode = select.data.values[0]
            ctx.store_message = msg

        if mode.isdigit():

            if len(mode) > 2 or int(mode) > 10:
                raise GenericError(f"**Số lượng không hợp lệ: {mode}**\n"
                                   "`Số lượng tối đa cho phép:10`")

            await self.loop_amount.callback(self=self, inter=ctx, value=int(mode))
            return

        if mode not in ('current', 'queue', 'off'):
            raise GenericError("Lựa chọn không hợp lệ! Chọn giữa: Hiện tại/Hàng đợi/TẮT")

        await self.loop_mode.callback(self=self, inter=ctx, mode=mode)

    @is_dj()
    @has_source()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Chọn Chế độ lặp lại giữa: Bài hát hiện tại / Hàng đợi hoặc Tắt.",
        extras={"only_voiced": True}, cooldown=loop_cd, max_concurrency=loop_mc
    )
    async def loop_mode(
            self,
            inter: disnake.AppCmdInter,
            mode: str = commands.Param(
                name="mode",
                choices=[
                    disnake.OptionChoice(
                        disnake.Localized("Bài hát hiện tại", data={disnake.Locale.pt_BR: "Música Atual"}), "current"
                    ),
                    disnake.OptionChoice(
                        disnake.Localized("Hàng đợi", data={disnake.Locale.pt_BR: "Fila"}), "queue"
                    ),
                    disnake.OptionChoice(
                        disnake.Localized("Tắt", data={disnake.Locale.pt_BR: "Desativar"}), "off"
                    ),
                ]
            )
    ):

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if mode == player.loop:
            raise GenericError("**Chế độ lặp lại đã chọn đang hoạt động...**")

        if mode == 'off':
            mode = False
            player.current.info["extra"]["track_loops"] = 0
            emoji = "⭕"
            txt = ['Vô hiệu hóa Lặp lại.', f"{emoji} **⠂{inter.author.mention} Vô hiệu hóa Lặp lại.**"]

        elif mode == "current":
            player.current.info["extra"]["track_loops"] = 0
            emoji = "🔂"
            txt = ["Đã kích hoạt Lặp lại của bài hát hiện tại.",
                   f"{emoji} **⠂{inter.author.mention} Đã kích hoạt Lặp lại của bài hát hiện tại.**"]

        else:  # queue
            emoji = "🔁"
            txt = ["đã kích hoạt Lặp lại của hàng đợi.", f"{emoji} **⠂{inter.author.mention} đã kích hoạt Lặp lại của hàng đợi.**"]

        player.loop = mode

        bot.loop.create_task(player.process_rpc())

        await self.interaction_message(inter, txt, emoji=emoji)

    @is_dj()
    @has_source()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Xác định số lần lặp lại của bài hát hiện tại.",
        extras={"only_voiced": True}, cooldown=loop_cd, max_concurrency=loop_mc,
    )
    async def loop_amount(
            self,
            inter: disnake.AppCmdInter,
            value: int = commands.Param(name="value", description="Số lần lặp lại.")
    ):

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        player.current.info["extra"]["track_loops"] = value

        txt = [
            f"cài đặt số lần lặp lại cho bài hát: "
            f"[`{(fix_characters(player.current.title, 25))}`]({player.current.uri or player.current.search_uri}) là **{value}** lần.",
            f"🔄 **⠂{inter.author.mention} đã cho bài hát lặp lại [{value}] lần:**\n"
            f"╰[`{player.current.title}`]({player.current.uri or player.current.search_uri})"
        ]

        await self.interaction_message(inter, txt, rpc_update=True, emoji="🔄")

    remove_mc = commands.MaxConcurrency(1, per=commands.BucketType.guild, wait=False)

    @is_dj()
    @has_player()
    @check_voice()
    @pool_command(name="remove", aliases=["r", "del"], description="Hủy bỏ một bài hát cụ thể khỏi hàng đợi.",
                  only_voiced=True, max_concurrency=remove_mc, extras={"flags": case_sensitive_args},
                  usage="{prefix}{cmd} [nome]\nEx: {prefix}{cmd} sekai")
    async def remove_legacy(self, ctx: CustomContext, *, flags: str = ""):

        args, unknown = ctx.command.extras['flags'].parse_known_args(flags.split())

        if not unknown:
            raise GenericError("**Bạn đã không thêm tên của bài hát.**")

        await self.remove.callback(self=self, inter=ctx, query=" ".join(unknown), case_sensitive=args.casesensitive)

    @is_dj()
    @has_player()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Xóa một bài hát cụ thể khỏi hàng đợi.",
        extras={"only_voiced": True}, max_concurrency=remove_mc
    )
    async def remove(
            self,
            inter: disnake.AppCmdInter,
            query: str = commands.Param(name="name", description="Nhập tên bài hát."),
            case_sensitive: bool = commands.Param(
                name="exact_name", default=False,
                description="Tìm kiếm bài hát với tên chính xác thay vì tìm kiếm từng từ trong tên của âm nhạc",

            )
    ):

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        try:
            index = queue_track_index(inter, bot, query, case_sensitive=case_sensitive)[0][0]
        except IndexError:
            raise GenericError(f"**Không có bài hát nào phù hợp với tên: {query}**")

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        track = player.queue[index]

        player.queue.remove(track)

        txt = [
            f"Xóa bài hát [`{(fix_characters(track.title, 25))}`]({track.uri or track.search_uri}) Khỏi hàng đợi.",
            f"♻️ **⠂{inter.author.mention} Đã loại bỏ bài hát: **\n╰[`{track.title}`]({track.uri or track.search_uri}) khỏi hàng đợi"
        ]

        await self.interaction_message(inter, txt, emoji="♻️")

        await player.update_message()

    queue_manipulation_cd = commands.CooldownMapping.from_cooldown(2, 15, commands.BucketType.guild)

    @is_dj()
    @has_player()
    @check_voice()
    @pool_command(name="readd", aliases=["readicionar", "rdd"], only_voiced=True, cooldown=queue_manipulation_cd,
                  max_concurrency=remove_mc, description="Đưa các bài hát đã chơi vào lại trong hàng.")
    async def readd_legacy(self, ctx: CustomContext):
        await self.readd_songs.callback(self=self, inter=ctx)

    @is_dj()
    @has_player()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Đưa các bài hát đã được chơi vào lại trong hàng.",
        extras={"only_voiced": True}, cooldown=queue_manipulation_cd, max_concurrency=remove_mc
    )
    async def readd_songs(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if not player.played and not player.failed_tracks:
            raise GenericError("**Không có bài hát nào được thêm vào.**")

        qsize = len(player.played) + len(player.failed_tracks)

        player.played.reverse()
        player.failed_tracks.reverse()
        player.queue.extend(player.failed_tracks)
        player.queue.extend(player.played)
        player.played.clear()
        player.failed_tracks.clear()

        txt = [
            f"Đưa {qsize} Bài hát vào lại trong hàng",
            f"🎶 **⠂{inter.author.mention} Đưa {qsize} bài hát vào lại trong hàng**"
        ]

        await self.interaction_message(inter, txt, emoji="🎶")

        await player.update_message()

        if not player.current:
            await player.process_next()
        else:
            await player.update_message()

    @is_dj()
    @has_player()
    @check_voice()
    @pool_command(name="rotate", aliases=["rt", "rotacionar"], only_voiced=True,
                  description="Xoay hàng đợi sang bài hát được chỉ định.",
                  cooldown=queue_manipulation_cd, max_concurrency=remove_mc, extras={"flags": case_sensitive_args},
                  usage="{prefix}{cmd} [nome]\nEx: {prefix}{cmd} sekai")
    async def rotate_legacy(self, ctx: CustomContext, *, flags: str = ""):

        args, unknown = ctx.command.extras['flags'].parse_known_args(flags.split())

        if not unknown:
            raise GenericError("**Bạn đã không thêm tên của bài hát.**")

        await self.rotate.callback(self=self, inter=ctx, query=" ".join(unknown), case_sensitive=args.casesensitive)

    @is_dj()
    @check_queue_loading()
    @has_player()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Xoay hàng đợi đến bài hát được chỉ định.",
        extras={"only_voiced": True}, cooldown=queue_manipulation_cd, max_concurrency=remove_mc
    )
    async def rotate(
            self,
            inter: disnake.AppCmdInter,
            query: str = commands.Param(name="name", description="Nhập tên bài hát."),
            case_sensitive: bool = commands.Param(
                name="exact_name", default=False,
                description="Tìm kiếm bài hát với tên chính xác thay vì tìm kiếm từng từ trong tên của bài hát",
            )
    ):

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        index = queue_track_index(inter, bot, query, case_sensitive=case_sensitive)

        if not index:
            raise GenericError(f"**Không có bài hát nào với tên: {query}**")

        index = index[0][0]

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        track = player.queue[index]

        if index <= 0:
            raise GenericError(f"**Bài hát **[`{track.title}`]({track.uri or track.search_uri}) đã là bài tiếp theo.")

        player.queue.rotate(0 - (index))

        txt = [
            f"Xoay hàng đợi sang bài hát [`{(fix_characters(track.title, limit=25))}`]({track.uri or track.search_uri}).",
            f"🔃 **⠂{inter.author.mention} Xoay dòng vào bài hát:**\n╰[`{track.title}`]({track.uri or track.search_uri})."
        ]

        await self.interaction_message(inter, txt, emoji="🔃")

        await player.update_message()

    song_request_thread_cd = commands.CooldownMapping.from_cooldown(1, 120, commands.BucketType.guild)

    @is_dj()
    @has_player()
    @check_voice()
    @commands.bot_has_guild_permissions(manage_threads=True)
    @pool_command(name="songrequestthread", aliases=["songrequest", "srt"], only_voiced=True,
                  description="Tạo một cuộc trò chuyện chủ đề/tạm thời cho các yêu cầu bài hát (yêu cầu âm nhạc)")
    async def song_request_thread_legacy(self, ctx: CustomContext):

        await self.song_request_thread.callback(self=self, inter=ctx)

    @is_dj()
    @has_player()
    @check_voice()
    @commands.slash_command(extras={"only_voiced": True}, cooldown=song_request_thread_cd,
                            description=f"{desc_prefix}Tạo một cuộc trò chuyện chủ đề/tạm thời cho các yêu cầu bài hát (yêu cầu âm nhạc)")
    async def song_request_thread(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
            guild = inter.music_guild
        except AttributeError:
            bot = inter.bot
            guild = inter.guild

        if not self.bot.intents.message_content:
            raise GenericError("**Tôi hiện không có ý định kiểm tra nội dung tin nhắn "
                               "Nội dung tin nhắn**")

        player: LavalinkPlayer = bot.music.players[guild.id]

        if player.static:
            raise GenericError("**Bạn không thể sử dụng lệnh này với kênh yêu cầu bài hát được cấu hình.**")

        if player.has_thread:
            raise GenericError("**Đã có một cuộc trò chuyện chủ đề/hoạt động trên người chơi.**")

        if not isinstance(player.text_channel, disnake.TextChannel):
            raise GenericError(f"**Trình điều khiển người chơi đang hoạt động trên một kênh không tương thích với "
                               f"Tạo chủ đề/cuộc trò chuyện.**")

        if not player.controller_mode:
            raise GenericError("**Giao diện hiện tại không tương thích với hệ thống yêu cầu bài hát"
                                "qua chủ đề/cuộc trò chuyện\n\n"
                                "Lưu ý:** `Hệ thống này yêu cầu giao diện sử dụng các nút.`")

        if not player.text_channel.permissions_for(guild.me).send_messages:
            raise GenericError(f"**{bot.user.mention} không có quyền gửi tin nhắn trên kênh {player.text_channel.mention}.**")

        if not player.text_channel.permissions_for(guild.me).create_public_threads:
            raise GenericError(f"**{bot.user.mention} không có quyền tạo chủ đề công khai.**")

        if not [m for m in player.guild.me.voice.channel.members if not m.bot and
                player.text_channel.permissions_for(m).send_messages_in_threads]:
            raise GenericError(f"**Không có thành viên nào trong kênh <#{player.channel_id}> có quyền gửi tin nhắn"
                                f"trong các chủ đề trên kênh {player.text_channel.mention}")

        await inter.response.defer(ephemeral=True)

        thread = await player.message.create_thread(name=f"Kênh yêu cầu nhạc của {bot.user.name} ", auto_archive_duration=10080)

        txt = [
            "Đã kích hoạt hệ thống trò chuyện tạm thời/chủ đề cho yêu cầu âm nhạc.",
            f"💬 **⠂{inter.author.mention} Đã tạo [Chủ đề/cuộc trò chuyện]({thread.jump_url}) tạm thời cho yêu cầu âm nhạc.**"
        ]

        await self.interaction_message(inter, txt, emoji="💬", defered=True, force=True)

    filtercd = commands.CooldownMapping.from_cooldown(1, 7, commands.BucketType.guild)
    filtermc = commands.MaxConcurrency(1, per=commands.BucketType.guild, wait=False)

    @is_dj()
    @has_source()
    @check_voice()
    @pool_command(name="nightcore", aliases=["nc"], only_voiced=True, cooldown=filtercd, max_concurrency=filtermc,
                  description="Kích hoạt/Vô hiệu hóa hiệu ứng Nightcore.")
    async def nightcore_legacy(self, ctx: CustomContext):

        await self.nightcore.callback(self=self, inter=ctx)

    @is_dj()
    @has_source()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Kích hoạt/Vô hiệu hóa hiệu ứng Nightcore.",
        extras={"only_voiced": True}, cooldown=filtercd, max_concurrency=filtermc,
    )
    async def nightcore(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        player.nightcore = not player.nightcore

        if player.nightcore:
            await player.turnallfilteroff()
            await asyncio.sleep(2)
            await player.set_timescale(pitch=1.2, speed=1.1)
            txt = "kích hoạt"
        else:
            await player.set_timescale(enabled=False)
            await player.update_filters()
            txt = "Vô hiệu hóa"

        txt = [f"{txt} Hiệu ứng Nightcore.", f"🇳 **⠂{inter.author.mention} {txt} Hiệu ứng Nightcore.**"]

        await self.interaction_message(inter, txt, emoji="🇳")
        
    @is_dj()
    @has_source()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Kích hoạt/Vô hiệu hóa hiệu ứng 3D.",
        extras={"only_voiced": True}, cooldown=filtercd, max_concurrency=filtermc,
    )
    async def filter3d(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        player.filter3d = not player.filter3d

        if player.filter3d:
            await player.turnallfilteroff()
            await asyncio.sleep(2)
            await player.set_rotation(sample_rate=0.3)
            txt = "kích hoạt"
        else:
            await player.set_rotation(enabled=False)
            await player.update_filters()
            txt = "Vô hiệu hóa"

        txt = [f"{txt} Hiệu ứng 3D.", f"🔊 **⠂{inter.author.mention} {txt} Hiệu ứng 3D.**"]

        await self.interaction_message(inter, txt, emoji="🔊")  
        
    @is_dj()
    @has_source()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Kích hoạt/Vô hiệu hóa hiệu ứng slowmo.",
        extras={"only_voiced": True}, cooldown=filtercd, max_concurrency=filtermc,
    )
    async def filter_slowmo(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        player.slowmo = not player.slowmo

        if player.slowmo:
            await player.turnallfilteroff()
            await asyncio.sleep(2)
            await player.set_timescale(speed=0.75, pitch=1.0, rate=0.8)
            txt = "kích hoạt"
        else:
            await player.set_timescale(enabled=False)
            await player.update_filters()
            txt = "Vô hiệu hóa"

        txt = [f"{txt} Hiệu ứng Slowmo.", f"🔊 **⠂{inter.author.mention} {txt} Hiệu ứng Slowmo.**"]

        await self.interaction_message(inter, txt, emoji="🔊")  

    controller_cd = commands.CooldownMapping.from_cooldown(1, 10, commands.BucketType.member)
    controller_mc = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @has_source()
    @check_voice()
    @pool_command(name="controller", aliases=["np", "ctl"], only_voiced=True, cooldown=controller_cd,
                  max_concurrency=controller_mc, description="Gửi bộ điều khiển người chơi đến một kênh cụ thể/hiện tại.")
    async def controller_legacy(self, ctx: CustomContext):
        await self.controller.callback(self=self, inter=ctx)

    @has_source()
    @check_voice()
    @commands.slash_command(description=f"{desc_prefix}Gửi bộ điều khiển người chơi đến một kênh cụ thể/hiện tại.",
                            extras={"only_voiced": True}, cooldown=controller_cd, max_concurrency=controller_mc)
    async def controller(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
            guild = inter.music_guild
            channel = bot.get_channel(inter.channel.id)
        except AttributeError:
            bot = inter.bot
            guild = inter.guild
            channel = inter.channel

        player: LavalinkPlayer = bot.music.players[guild.id]

        if player.static:
            raise GenericError("Lệnh này không thể được sử dụng trong chế độ trình phát cố định.")

        if player.has_thread:
            raise GenericError("**Lệnh này không thể được sử dụng với một cuộc trò chuyện tích cực trong"
                               f"[tin nhắn]({player.message.jump_url}) của người chơi.**")

        if not inter.response.is_done():
            await inter.response.defer(ephemeral=True)

        if channel != player.text_channel:

            await is_dj().predicate(inter)

            try:

                player.set_command_log(
                    text=f"{inter.author.mention} đưa điều khiển người chơi đến kênh {inter.channel.mention}.",
                    emoji="💠"
                )

                embed = disnake.Embed(
                    description=f"💠 **⠂{inter.author.mention} đưa điều khiển người chơi đến kênh:** {channel.mention}",
                    color=self.bot.get_color(guild.me)
                )

                try:
                    if bot.user.id != self.bot.user.id:
                        embed.set_footer(text=f"Thông qua bot: {bot.user.display_name}", icon_url=bot.user.display_avatar.url)
                except AttributeError:
                    pass

                await player.text_channel.send(embed=embed)

            except:
                pass

        await player.destroy_message()

        player.text_channel = channel

        await player.invoke_np()

        if not isinstance(inter, CustomContext):
            await inter.edit_original_message("**Người chơi đã được gửi lại thành công!**")

    @is_dj()
    @has_player()
    @check_voice()
    @commands.user_command(name=disnake.Localized("Add DJ", data={disnake.Locale.pt_BR: "Adicionar DJ"}),
                           extras={"only_voiced": True})
    async def adddj_u(self, inter: disnake.UserCommandInteraction):
        await self.add_dj(interaction=inter, user=inter.target)

    @is_dj()
    @has_player()
    @check_voice()
    @pool_command(name="adddj", aliases=["adj"], only_voiced=True,
                  description="Thêm một thành viên vào danh sách của DJ vào phiên người chơi hiện tại.",
                  usage="{prefix}{cmd} [id|tên|@user]\nEx: {prefix}{cmd} @anhbasin")
    async def add_dj_legacy(self, ctx: CustomContext, user: disnake.Member):
        await self.add_dj.callback(self=self, inter=ctx, user=user)

    @is_dj()
    @has_player()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Thêm thành viên vào danh sách của DJ vào phiên trình phát hiện tại.",
        extras={"only_voiced": True}
    )
    async def add_dj(
            self,
            inter: disnake.AppCmdInter, *,
            user: disnake.User = commands.Param(name="member", description="Thành viên được thêm vào.")
    ):

        error_text = None

        try:
            bot = inter.music_bot
            guild = inter.music_guild
            channel = bot.get_channel(inter.channel.id)
        except AttributeError:
            bot = inter.bot
            guild = inter.guild
            channel = inter.channel

        player: LavalinkPlayer = bot.music.players[guild.id]

        user = guild.get_member(user.id)

        if user.bot:
            error_text = "**Bạn thông thể thêm bot vào danh sách dj.**"
        elif user == inter.author:
            error_text = "**Bạn không thể tự thêm vào danh sách của DJ.**"
        elif user.guild_permissions.manage_channels:
            error_text = f"Bạn không thể thêm thành viên {user.mention} Trong danh sách DJ (anh ấy / cô ấy có quyền quản lý các kênh **)."
        elif user.id == player.player_creator:
            error_text = f"**Các thành viên {user.mention} là người tạo ra người chơi ...**"
        elif user.id in player.dj:
            error_text = f"**Các thành viên {user.mention} đã nằm trong danh sách của DJ**"

        if error_text:
            raise GenericError(error_text)

        player.dj.add(user.id)

        text = [f"thêm {user.mention} vào danh sách DJ.",
                f"<:Play_With_Me:1128555926417330207> **⠂{inter.author.mention} đã thêm {user.mention} Vào danh sách DJ**"]

        if (player.static and channel == player.text_channel) or isinstance(inter.application_command,
                                                                            commands.InvokableApplicationCommand):
            await inter.send(f"{user.mention} Đã thêm vào danh sách của DJ!{player.controller_link}")

        await self.interaction_message(inter, txt=text, emoji="<:Play_With_Me:1128555926417330207>")

    @is_dj()
    @has_player()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Xóa một thành viên khỏi danh sách của DJ tại phiên người chơi hiện tại.",
        extras={"only_voiced": True}
    )
    async def remove_dj(
            self,
            inter: disnake.AppCmdInter, *,
            user: disnake.User = commands.Param(name="member", description="Thành viên được thêm vào.")
    ):

        try:
            bot = inter.music_bot
            guild = inter.music_guild
            channel = bot.get_channel(inter.channel.id)
        except AttributeError:
            bot = inter.bot
            guild = inter.guild
            channel = inter.channel

        player: LavalinkPlayer = bot.music.players[guild.id]

        user = guild.get_member(user.id)

        if user.id == player.player_creator:
            if inter.author.guild_permissions.manage_guild:
                player.player_creator = None
            else:
                raise GenericError(f"**Thành viên {user.mention} là người đã yêu cầu phiên này.**")

        elif user.id not in player.dj:
            GenericError(f"Các thành viên {user.mention}  không ở trong danh sách DJ's")

        else:
            player.dj.remove(user.id)

        text = [f"LOẠI BỎ {user.mention} khỏi danh sách DJ's.",
                f"<:verify:1134033164151566460> **⠂{inter.author.mention} LOẠI BỎ {user.mention} khỏi danh sách DJ's.**"]

        if (player.static and channel == player.text_channel) or isinstance(inter.application_command,
                                                                            commands.InvokableApplicationCommand):
            await inter.send(f"{user.mention} Được thêm vào danh sách của DJ's!{player.controller_link}")

        await self.interaction_message(inter, txt=text, emoji="<:verify:1134033164151566460>")

    @is_dj()
    @has_player()
    @check_voice()
    @pool_command(name="stop", aliases=["leave", "parar"], only_voiced=True,
                  description="Dừng người chơi và ngắt kết nối tôi khỏi kênh giọng nói.")
    async def stop_legacy(self, ctx: CustomContext):
        await self.stop.callback(self=self, inter=ctx)

    @is_dj()
    @has_player()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Dừng người chơi và ngắt kết nối tôi khỏi kênh giọng nói.",
        extras={"only_voiced": True}
    )
    async def stop(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
            guild = inter.music_guild
            inter_destroy = inter if bot.user.id == self.bot.user.id else None
        except AttributeError:
            bot = inter.bot
            guild = inter.guild
            inter_destroy = inter

        player: LavalinkPlayer = bot.music.players[inter.guild_id]
        player.command_log = f"{inter.author.mention} **đã dừng người chơi!**"

        if isinstance(inter, disnake.MessageInteraction):
            await player.playerdestroy(inter=inter_destroy)
        else:

            embed = disnake.Embed(
                color=self.bot.get_color(guild.me),
                description=f"<:verify:1134033164151566460> **⠂{inter.author.mention} đã dừng người chơi.**"
            )

            try:
                if bot.user.id != self.bot.user.id:
                    embed.set_footer(text=f"Thông qua {bot.user.display_name}", icon_url=bot.user.display_avatar.url)
            except AttributeError:
                pass

            await inter.send(
                embed=embed,
                ephemeral=player.static and player.text_channel.id == inter.channel_id
            )
            await player.playerdestroy(inter=inter)

    @check_queue_loading()
    @has_player()
    @check_voice()
    @pool_command(
        name="savequeue", aliases=["sq", "svq"],
        only_voiced=True, cooldown=queue_manipulation_cd, max_concurrency=remove_mc,
        description="Thử nghiệm: Lưu bài hát hiện tại và hàng đợi để sử dụng chúng bất kỳ lúc nào."
    )
    async def savequeue_legacy(self, ctx: CustomContext):
        await self.save_queue.callback(self=self, inter=ctx)

    @check_queue_loading()
    @has_player()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Thử nghiệm: Lưu bài hát hiện tại và hàng đợi để sử dụng chúng bất kỳ lúc nào.",
        extras={"only_voiced": True}, cooldown=queue_manipulation_cd, max_concurrency=remove_mc
    )
    async def save_queue(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
            guild = inter.music_guild
        except AttributeError:
            bot = inter.bot
            guild = bot.get_guild(inter.guild_id)

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        tracks = []

        if player.current:
            player.current.info["id"] = player.current.id
            if player.current.playlist:
                player.current.info["playlist"] = {"name": player.current.playlist_name, "url": player.current.playlist_url}
            tracks.append(player.current.info)

        for t in player.queue:
            t.info["id"] = t.id
            if t.playlist:
                t.info["playlist"] = {"name": t.playlist_name, "url": t.playlist_url}
            tracks.append(t.info)

        if len(tracks) < 3:
            raise GenericError(f"**Bạn phải có ít nhất 3 bài hát để lưu (hiện tại và/hoặc trong hàng đợi)**")

        if not os.path.isdir(f"./local_database/saved_queues_v1/users"):
            os.makedirs(f"./local_database/saved_queues_v1/users")

        async with aiofiles.open(f"./local_database/saved_queues_v1/users/{inter.author.id}.pkl", "wb") as f:
            await f.write(
                zlib.compress(
                    pickle.dumps(
                        {
                            "tracks": tracks, "created_at": disnake.utils.utcnow(), "guild_id": inter.guild_id
                        }
                    )
                )
            )

        await inter.response.defer(ephemeral=True)

        global_data = await self.bot.get_global_data(guild.id, db_name=DBModel.guilds)

        try:
            slashcmd = f"</play:" + str(self.bot.pool.controller_bot.get_global_command_named("play", cmd_type=disnake.ApplicationCommandType.chat_input).id) + ">"
        except AttributeError:
            slashcmd = "/play"

        embed = disnake.Embed(
            color=bot.get_color(guild.me),
            description=f"### {inter.author.mention}: Hàng đợi đã được lưu thành công!!\n"
                         f"**Bài hát đã lưu:** `{len(tracks)}`\n"
                         "### Làm thế nào để sử dụng nó?\n"
                         f"* Sử dụng lệnh {slashcmd} (chọn trong phần tự động hoàn thành tìm kiếm)\n"
                         "* Nhấp vào nút phát/chọn/tích hợp trình phát yêu thích.\n"
                         f"* Sử dụng lệnh {global_data['prefix'] or self.bot.default_prefix}{self.play_legacy.name}"
                         "không bao gồm tên hoặc liên kết tới bài hát/video."
        )

        embed.set_footer(text="Lưu ý: Đây là một tính năng mang tính thử nghiệm, hàng đợi đã lưu có thể thay đổi hoặc "
                               "đã bị xóa trong các bản cập nhật trong tương lai")

        if isinstance(inter, CustomContext):
            await inter.reply(embed=embed)
        else:
            await inter.edit_original_response(embed=embed)


    @has_player()
    @check_voice()
    @commands.slash_command(name="queue", extras={"only_voiced": True})
    async def q(self, inter):
        pass

    @is_dj()
    @has_player()
    @check_voice()
    @pool_command(name="shuffle", aliases=["sf", "shf", "sff", "misturar"], only_voiced=True,
                  description="Trộn bài hát trong hàng đợi", cooldown=queue_manipulation_cd, max_concurrency=remove_mc)
    async def shuffle_legacy(self, ctx: CustomContext):
        await self.shuffle_.callback(self, inter=ctx)

    @is_dj()
    @q.sub_command(
        name="shuffle",
        description=f"{desc_prefix}Trộn bài hát trong hàng đợi",
        extras={"only_voiced": True}, cooldown=queue_manipulation_cd, max_concurrency=remove_mc)
    async def shuffle_(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if len(player.queue) < 3:
            raise GenericError("**Dòng phải có ít nhất 3 bài hát để được trộn lẫn.**")

        shuffle(player.queue)

        await self.interaction_message(
            inter,
            ["Trộn các bài hát trong hàng đợi.",
             f"🔀 **⠂{inter.author.mention} Trộn các bài hát trong hàng đợi.**"],
            emoji="🔀"
        )

    @is_dj()
    @has_player()
    @check_voice()
    @pool_command(name="reverse", aliases=["invert", "inverter", "rv"], only_voiced=True,
                  description="Đảo ngược thứ tự của các bài hát trong hàng", cooldown=queue_manipulation_cd, max_concurrency=remove_mc)
    async def reverse_legacy(self, ctx: CustomContext):
        await self.reverse.callback(self=self, inter=ctx)

    @is_dj()
    @q.sub_command(
        description=f"{desc_prefix}Đảo ngược thứ tự của các bài hát trong hàng",
        extras={"only_voiced": True}, cooldown=queue_manipulation_cd, max_concurrency=remove_mc
    )
    async def reverse(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if len(player.queue) < 2:
            raise GenericError("**Dòng phải có ít nhất 2 bài hát để đảo ngược thứ tự.**")

        player.queue.reverse()
        await self.interaction_message(
            inter,
            txt=["Đảo ngược thứ tự của các bài hát trong hàng.",
                 f"🔄 **⠂{inter.author.mention} Anh ấy đã đảo ngược thứ tự của các bài hát trong hàng.**"],
            emoji="🔄"
        )

    queue_show_mc = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @check_voice()
    @has_player()
    @check_voice()
    @pool_command(name="queue", aliases=["q", "fila"], description="Hiển thị các bài hát.",
                  only_voiced=True, max_concurrency=queue_show_mc)
    async def queue_show_legacy(self, ctx: CustomContext):
        await self.display.callback(self=self, inter=ctx)

    @commands.max_concurrency(1, commands.BucketType.member)
    @q.sub_command(
        description=f"{desc_prefix}Hiển thị các bài hát đang trong hàng đợi.", max_concurrency=queue_show_mc
    )
    async def display(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if not player.queue:
            raise GenericError("**Không có bài hát trong hàng đợi.**")

        view = QueueInteraction(player, inter.author)
        embed = view.embed

        try:
            if bot.user.id != self.bot.user.id:
                embed.set_footer(text=f"Sử dụng {bot.user.display_name}", icon_url=bot.user.display_avatar.url)
        except AttributeError:
            pass

        await inter.response.defer(ephemeral=True)

        kwargs = {
            "embed": embed,
            "view": view
        }

        try:
            func = inter.followup.send
            kwargs["ephemeral"] = True
        except AttributeError:
            try:
                func = inter.edit_original_message
            except AttributeError:
                func = inter.send
                kwargs["ephemeral"] = True

        view.message = await func(**kwargs)

        await view.wait()

    adv_queue_flags = CommandArgparse()

    adv_queue_flags.add_argument('-songtitle', '-name', '-title', '-songname', nargs='+',
                             help="Bao gồm tên bài hát cụ thể.\nVí dụ: -name NCS", default=[])
    adv_queue_flags.add_argument('-uploader', '-author', '-artist', nargs='+', default=[],
                             help="Loại bỏ bài hát từ một nghệ sĩ, tác giả hoặc người tải lên cụ thể.\nVí dụ: -uploader sekai")
    adv_queue_flags.add_argument('-member', '-user', '-u', nargs='+', default=[],
                             help="Loại bỏ bài hát được yêu cầu bởi một người dùng cụ thể.\nVí dụ: -user @user")
    adv_queue_flags.add_argument('-duplicates', '-dupes', '-duplicate', action='store_true',
                             help="Loại bỏ các bài hát trùng lặp.")
    adv_queue_flags.add_argument('-playlist', '-list', '-pl', nargs='+', default=[],
                             help="Loại bỏ các bài hát từ một danh sách phát cụ thể.\nVí dụ: -playlist minhaplaylist")
    adv_queue_flags.add_argument('-minimaltime', '-mintime', '-min', '-minduration', '-minduration', default=None,
                             help="Loại bỏ các bài hát có thời lượng tối thiểu được chỉ định.\nVí dụ: -min 1:23.")
    adv_queue_flags.add_argument('-maxduration', '-maxtime', '-max', default=None,
                             help="Loại bỏ các bài hát có thời lượng tối đa được chỉ định.\nVí dụ: -max 1:23.")
    adv_queue_flags.add_argument('-amount', '-counter', '-count', '-c', type=int, default=None,
                             help="Xác định số lượng bài hát cần xử lý với tên được chỉ định.\nVí dụ: -amount 5")
    adv_queue_flags.add_argument('-startposition', '-startpos', '-start', type=int, default=0,
                             help="Loại bỏ các bài hát từ một vị trí bắt đầu cụ thể trong danh sách.\nVí dụ: -start 10")
    adv_queue_flags.add_argument('-endposition', '-endpos', '-end', type=int, default=0,
                             help="Loại bỏ các bài hát từ vị trí hiện tại đến một vị trí kết thúc cụ thể trong danh sách.\nVí dụ: -end 15")
    adv_queue_flags.add_argument('-absentmembers', '-absent', '-abs', action='store_true',
                             help="Loại bỏ các bài hát được thêm bởi các thành viên đã rời khỏi kênh.")


    clear_flags = CommandArgparse(parents=[adv_queue_flags])

    @is_dj()
    @has_player()
    @check_voice()
    @pool_command(name="clear", aliases=["limpar", "clearqueue"], description="Xóa hàng đợi hiện tại đang phát.",
                  only_voiced=True,
                  extras={"flags": clear_flags}, cooldown=queue_manipulation_cd, max_concurrency=remove_mc)
    async def clear_legacy(self, ctx: CustomContext, *, flags: str = ""):

        args, unknown = ctx.command.extras['flags'].parse_known_args(flags.split())

        await self.clear.callback(
            self=self, inter=ctx,
            song_name=" ".join(args.songtitle + unknown),
            song_author=" ".join(args.uploader),
            user=await commands.MemberConverter().convert(ctx, " ".join(args.member)) if args.member else None,
            duplicates=args.duplicates,
            playlist=" ".join(args.playlist),
            min_duration=args.minimaltime,
            max_duration=args.maxduration,
            amount=args.amount,
            range_start=args.startposition,
            range_end=args.endposition,
            absent_members=args.absentmembers
        )

    @check_queue_loading()
    @is_dj()
    @has_player()
    @check_voice()
    @q.sub_command(
        name="clear",
        description=f"{desc_prefix}Xóa hàng đợi hiện tại.",
        extras={"only_voiced": True}, cooldown=queue_manipulation_cd, max_concurrency=remove_mc
    )
    async def clear(
            self,
            inter: disnake.AppCmdInter,
            song_name: str = commands.Param(name="name_of_the_song", description="thêm tên bài hát.",
                                            default=None),
            song_author: str = commands.Param(name="uploader_name",
                                              description="Bao gồm tên của bạn trong tác giả/nghệ sĩ/người tải lên bài hát.", default=None),
            user: disnake.Member = commands.Param(name='user',
                                                  description="Bao gồm các bài hát được yêu cầu bởi người dùng đã chọn.",
                                                  default=None),
            duplicates: bool = commands.Param(name="duplicates", description="Bao gồm các bài hát trùng lặp",
                                              default=False),
            playlist: str = commands.Param(name="playlist",description="Thêm danh sách phát.", default=None),
            min_duration: str = commands.Param(name="initial_duration",
                                               description="bao gồm các bài hát có thời lượng tương tự/lớn hơn (ví dụ: 1:23).",
                                               default=None),
            max_duration: str = commands.Param(name="maximum_duration",
                                               description="bao gồm các bài hát có độ dài tối đa được chỉ định (ví dụ: 1:45).",
                                               default=None),
            amount: int = commands.Param(name="amount", description="Số lượng bài hát để di chuyển.",
                                         min_value=0, max_value=99, default=None),
            range_start: int = commands.Param(name="starting_position",
                                              description="bao gồm các bài hát được xếp hàng đợi từ một vị trí cụ thể"
                                                           "từ hàng đợi.",
                                              min_value=1.0, max_value=500.0, default=0),
            range_end: int = commands.Param(name="end_position",
                                            description="đưa các bài hát từ hàng đợi vào một vị trí cụ thể trong hàng đợi.",
                                            min_value=1.0, max_value=500.0, default=0),
            absent_members: bool = commands.Param(name="missing_members",
                                                  description="Bao gồm các bài hát được thêm bởi các thành viên bên ngoài kênh",
                                                  default=False)
    ):

        if min_duration and max_duration:
            raise GenericError(
                "Bạn chỉ được chọn một trong các tùy chọn: **thời lượng nhỏ nhất** hoặc **thời lượng lớn nhất**.")

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if not player.queue:
            raise GenericError("**Không có bài hát nào trong hàng đợi.**")

        if amount is None:
            amount = 0

        filters = []
        final_filters = set()

        txt = []
        playlist_hyperlink = set()

        tracklist = []

        if song_name:
            song_name = song_name.replace("️", "")
            filters.append('song_name')
        if song_author:
            song_author = song_author.replace("️", "")
            filters.append('song_author')
        if user:
            filters.append('user')
        if playlist:
            playlist = playlist.replace("️", "")
            filters.append('playlist')
        if min_duration:
            filters.append('time_below')
            min_duration = string_to_seconds(min_duration) * 1000
        if max_duration:
            filters.append('time_above')
            max_duration = string_to_seconds(max_duration) * 1000
        if absent_members:
            filters.append('absent_members')
        if duplicates:
            filters.append('duplicates')

        if not filters and not range_start and not range_end:
            player.queue.clear()
            txt = ['đã xóa hàng đợi nhạc.', f'♻️ **⠂{inter.author.mention} đã xóa hàng đợi nhạc.**']

        else:

            if range_start > 0 and range_end > 0:

                if range_start >= range_end:
                    raise GenericError("**Vị trí cuối cùng phải lớn hơn vị trí ban đầu!**")

                song_list = list(player.queue)[range_start - 1: -(range_end - 1)]
                txt.append(f"**Vị trí bắt đầu hàng đợi:** `{range_start}`\n"
                            f"**Vị trí cuối của hàng đợi:** `{range_end}`")

            elif range_start > 0:
                song_list = list(player.queue)[range_start - 1:]
                txt.append(f"**Vị trí xếp hàng ban đầu:** `{range_start}`")
            elif range_end > 0:
                song_list = list(player.queue)[:-(range_end - 1)]
                txt.append(f"**Vị trí cuối cùng của hàng đợi:** `{range_end}`")
            else:
                song_list = list(player.queue)

            deleted_tracks = 0

            duplicated_titles = set()

            amount_counter = int(amount) if amount > 0 else 0

            for t in song_list:

                if amount and amount_counter < 1:
                    break

                temp_filter = list(filters)

                if 'duplicates' in temp_filter:
                    if (title:=f"{t.author} - {t.title}".lower()) in duplicated_titles:
                        temp_filter.remove('duplicates')
                        final_filters.add('duplicates')
                    else:
                        duplicated_titles.add(title)

                if 'time_below' in temp_filter and t.duration >= min_duration:
                    temp_filter.remove('time_below')
                    final_filters.add('time_below')

                elif 'time_above' in temp_filter and t.duration <= max_duration:
                    temp_filter.remove('time_above')
                    final_filters.add('time_above')

                if 'song_name' in temp_filter:

                    title = t.title.replace("️", "").lower().split()

                    query_words = song_name.lower().split()

                    word_count = 0

                    for query_word in song_name.lower().split():
                        for title_word in title:
                            if query_word in title_word:
                                title.remove(title_word)
                                word_count += 1
                                break

                    if word_count == len(query_words):
                        temp_filter.remove('song_name')
                        final_filters.add('song_name')

                if 'song_author' in temp_filter and song_author.lower() in t.author.replace("️", "").lower():
                    temp_filter.remove('song_author')
                    final_filters.add('song_author')

                if 'user' in temp_filter and user.id == t.requester:
                    temp_filter.remove('user')
                    final_filters.add('user')

                elif 'absent_members' in temp_filter and t.requester not in player.guild.me.voice.channel.voice_states:
                    temp_filter.remove('absent_members')
                    final_filters.add('absent_members')

                playlist_link = None

                if 'playlist' in temp_filter:
                    if playlist == t.playlist_name.replace("️", "") or (isinstance(inter, CustomContext) and playlist.lower() in t.playlist_name.replace("️", "").lower()):
                        playlist_link = f"[`{fix_characters(t.playlist_name)}`]({t.playlist_url})"
                        temp_filter.remove('playlist')
                        final_filters.add('playlist')

                if not temp_filter:
                    tracklist.append(t)
                    player.queue.remove(t)
                    deleted_tracks += 1
                    if playlist_link:
                        playlist_hyperlink.add(playlist_link)

                    if amount:
                        amount_counter -= 1

            duplicated_titles.clear()

            if not deleted_tracks:
                await inter.send("Không tìm thấy bài hát nào!", ephemeral=True)
                return

            try:
                final_filters.remove("song_name")
                txt.append(f"**Gồm tên:** `{fix_characters(song_name)}`")
            except:
                pass

            try:
                final_filters.remove("song_author")
                txt.append(f"**Bao gồm tên trong người tải lên/nghệ sĩ:** `{fix_characters(song_author)}`")
            except:
                pass

            try:
                final_filters.remove("user")
                txt.append(f"**Yêu cầu của thành viên:** {user.mention}")
            except:
                pass

            try:
                final_filters.remove("playlist")
                txt.append(f"**Playlist:** {' | '.join(playlist_hyperlink)}")
            except:
                pass

            try:
                final_filters.remove("time_below")
                txt.append(f"**Với thời lượng ban đầu/bằng nhau:** `{time_format(min_duration)}`")
            except:
                pass

            try:
                final_filters.remove("time_above")
                txt.append(f"**Với thời lượng tối đa:** `{time_format(max_duration)}`")
            except:
                pass

            try:
                final_filters.remove("duplicates")
                txt.append(f"**Bài hát trùng lặp**")
            except:
                pass

            try:
                final_filters.remove("absent_members")
                txt.append("`Bài hát được yêu cầu bởi các thành viên đã rời kênh.`")
            except:
                pass

            msg_txt = f"### ♻️ ⠂{inter.author.mention} xóa {deleted_tracks} bài hát khỏi hàng đợi:\n" + "\n".join(f"[`{fix_characters(t.title, 45)}`]({t.uri})" for t in tracklist[:7])

            if (trackcount:=(len(tracklist) - 7)) > 0:
                msg_txt += f"\n`và nhiều {trackcount} bài hát khác.`"

            msg_txt += f"\n### ✅ ⠂(Các) bộ lọc được sử dụng:\n" + '\n'.join(txt)

            txt = [f"xóa (các) bài hát {deleted_tracks} khỏi hàng qua chức năng xóa.", msg_txt]

        try:
            kwargs = {"thumb": tracklist[0].thumb}
        except IndexError:
            kwargs = {}

        await self.interaction_message(inter, txt, emoji="♻️", **kwargs)


    move_queue_flags = CommandArgparse(parents=[adv_queue_flags])
    move_queue_flags.add_argument('-position', '-pos',
                           help="Chỉ định vị trí mục tiêu (tùy chọn).\nVí dụ: -pos 1",
                           type=int, default=None)
    move_queue_flags.add_argument('-casesensitive', '-cs',  action='store_true',
                           help="Tìm kiếm bài hát có cụm từ chính xác trong tên bài hát thay vì tìm kiếm từng chữ.")

    @check_queue_loading()
    @is_dj()
    @has_player()
    @check_voice()
    @pool_command(name="move", aliases=["movequeue", "moveadv", "moveadvanced", "moveq", "mq", "mv", "mover"],
                  description="Di chuyển bài hát từ hàng đợi.", only_voiced=True,
                  extras={"flags": move_queue_flags}, cooldown=queue_manipulation_cd, max_concurrency=remove_mc)
    async def move_legacy(self, ctx: CustomContext, position: Optional[int] = None, *, flags: str = ""):

        args, unknown = ctx.command.extras['flags'].parse_known_args(flags.split())

        if args.position:
            if position:
                unknown.insert(0, str(position))
            position = args.position

        if position is None:
            position = 1

        await self.do_move(
            inter=ctx,
            position=position,
            song_name=" ".join(unknown + args.songtitle),
            song_author=" ".join(args.uploader),
            user=await commands.MemberConverter().convert(ctx, " ".join(args.member)) if args.member else None,
            duplicates=args.duplicates,
            playlist=" ".join(args.playlist),
            min_duration=args.minimaltime,
            max_duration=args.maxduration,
            amount=args.amount,
            range_start=args.startposition,
            range_end=args.endposition,
            absent_members=args.absentmembers
        )

    @check_queue_loading()
    @is_dj()
    @has_player()
    @check_voice()
    @commands.slash_command(
        name="move",
        description=f"{desc_prefix}Di chuyển bài hát trong hàng đợi.",
        extras={"only_voiced": True}, cooldown=queue_manipulation_cd, max_concurrency=remove_mc
    )
    async def move(
            self,
            inter: disnake.AppCmdInter,
            position: int = commands.Param(name="position", description="Tùy chọn: Vị trí đích trong danh sách đợi.", min_value=1,
                                           max_value=999, default=1),
            song_name: str = commands.Param(name="song_name", description="Bao gồm tên cụ thể của bài hát.",
                                            default=None),
            song_author: str = commands.Param(name="uploader_name",
                                              description="Bao gồm tên của người tác giả hoặc người tải lên của bài hát.",
                                              default=None),
            user: disnake.Member = commands.Param(name='user',
                                                  description="Bao gồm những bài hát được yêu cầu bởi người dùng cụ thể.",
                                                  default=None),
            duplicates: bool = commands.Param(name="duplicates", description="Bao gồm các bài hát trùng lặp",
                                              default=False),
            playlist: str = commands.Param(description="Bao gồm tên của danh sách phát.", default=None),
            min_duration: str = commands.Param(name="initial_duration",
                                               description="Bao gồm các bài hát có thời lượng bắt đầu từ (ví dụ: 1:23).",
                                               default=None),
            max_duration: str = commands.Param(name="max_duration",
                                               description="Bao gồm các bài hát có thời lượng tối đa cụ thể (ví dụ: 1:45).",
                                               default=None),
            amount: int = commands.Param(name="quantity", description="Tùy chọn: Số lượng bài hát cần di chuyển.",
                                         min_value=0, max_value=99, default=None),
            range_start: int = commands.Param(name="start_position",
                                              description="Bao gồm các bài hát bắt đầu từ vị trí cụ thể trong danh sách.",
                                              min_value=1.0, max_value=500.0, default=0),
            range_end: int = commands.Param(name="position_end",
                                            description="Bao gồm các bài hát từ vị trí hiện tại đến vị trí kết thúc cụ thể trong danh sách.",
                                            min_value=1.0, max_value=500.0, default=0),
            absent_members: bool = commands.Param(name="absent_members",
                                                  description="Bao gồm các bài hát được thêm bởi những thành viên đã rời khỏi kênh",
                                                  default=False),
    ):

        await self.do_move(
            inter=inter, position=position, song_name=song_name, song_author=song_author, user=user,
            duplicates=duplicates, playlist=playlist, min_duration=min_duration, max_duration=max_duration,
            amount=amount, range_start=range_start, range_end=range_end, absent_members=absent_members
        )

    async def do_move(
            self, inter: Union[disnake.AppCmdInter, CustomContext], position: int = 1, song_name: str = None,
            song_author: str = None, user: disnake.Member = None, duplicates: bool = False, playlist: str = None,
            min_duration: str = None, max_duration: str = None, amount: int = None, range_start: int = 0,
            range_end: int = 0, absent_members: bool = False, case_sensitive=False
    ):

        if min_duration and max_duration:
            raise GenericError(
                "Bạn chỉ được chọn một trong các tùy chọn: **thời lượng nhỏ nhất** hoặc **thời lượng lớn nhất**.")

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if not player.queue:
            raise GenericError("**Không có bài hát trong dòng.**")

        filters = []
        final_filters = set()

        txt = []
        playlist_hyperlink = set()

        tracklist = []

        if song_name:
            song_name = song_name.replace("️", "")
            filters.append('song_name')
        if song_author:
            song_author = song_author.replace("️", "")
            filters.append('song_author')
        if user:
            filters.append('user')
        if playlist:
            playlist = playlist.replace("️", "")
            filters.append('playlist')
        if min_duration:
            filters.append('time_below')
            min_duration = string_to_seconds(min_duration) * 1000
        if max_duration:
            filters.append('time_above')
            max_duration = string_to_seconds(max_duration) * 1000
        if absent_members:
            filters.append('absent_members')
        if duplicates:
            filters.append('duplicates')

        if not filters and not range_start and not range_end:
            raise GenericError("**Bạn nên sử dụng ít nhất một tùy chọn để di chuyển**")

        indexes = None

        try:
            has_id = song_name.split(" || ID > ")[1]
        except:
            has_id = isinstance(inter, CustomContext)

        if range_start > 0 and range_end > 0:

            if range_start >= range_end:
                raise GenericError("**Vị trí cuối cùng phải lớn hơn vị trí bắt đầu!**")

            song_list = list(player.queue)[range_start - 1: -(range_end - 1)]
            txt.append(f"**Vị trí bắt đầu hàng đợi:** `{range_start}`\n"
                        f"**Vị trí cuối của hàng đợi:** `{range_end}`")

        elif range_start > 0:
            song_list = list(player.queue)[range_start - 1:]
            txt.append(f"**Vị trí bắt đầu của hàng đợi:**`{range_start}`")
        elif range_end > 0:
            song_list = list(player.queue)[:-(range_end - 1)]
            txt.append(f"**Vị trí cuối cùng của hàng đợi:** `{range_end}`")
        elif song_name and has_id and filters == ["song_name"] and amount is None:
            indexes = queue_track_index(inter, bot, song_name, match_count=1, case_sensitive=case_sensitive)
            for index, track in reversed(indexes):
                player.queue.remove(track)
                tracklist.append(track)
            song_list = []

        else:
            song_list = list(player.queue)

        if not tracklist:

            if amount is None:
                amount = 0

            duplicated_titles = set()

            amount_counter = int(amount) if amount > 0 else 0

            for t in song_list:

                if amount and amount_counter < 1:
                    break

                temp_filter = list(filters)

                if 'duplicates' in temp_filter:
                    if (title := f"{t.author} - {t.title}".lower()) in duplicated_titles:
                        temp_filter.remove('duplicates')
                        final_filters.add('duplicates')
                    else:
                        duplicated_titles.add(title)

                if 'time_below' in temp_filter and t.duration >= min_duration:
                    temp_filter.remove('time_below')
                    final_filters.add('time_below')

                elif 'time_above' in temp_filter and t.duration <= max_duration:
                    temp_filter.remove('time_above')
                    final_filters.add('time_above')

                if 'song_name' in temp_filter:

                    title = t.title.replace("️", "").lower().split()

                    query_words = song_name.lower().split()

                    word_count = 0

                    for query_word in song_name.lower().split():
                        for title_word in title:
                            if query_word in title_word:
                                title.remove(title_word)
                                word_count += 1
                                break

                    if word_count == len(query_words):
                        temp_filter.remove('song_name')
                        final_filters.add('song_name')

                if 'song_author' in temp_filter and song_author.lower() in t.author.replace("️", "").lower():
                    temp_filter.remove('song_author')
                    final_filters.add('song_author')

                if 'user' in temp_filter and user.id == t.requester:
                    temp_filter.remove('user')
                    final_filters.add('user')

                elif 'absent_members' in temp_filter and t.requester not in player.guild.me.voice.channel.voice_states:
                    temp_filter.remove('absent_members')
                    final_filters.add('absent_members')

                playlist_link = None

                if 'playlist' in temp_filter:
                    if playlist == t.playlist_name.replace("️", "") or (isinstance(inter, CustomContext) and playlist.lower() in t.playlist_name.replace("️", "").lower()):
                        playlist_link = f"[`{fix_characters(t.playlist_name)}`]({t.playlist_url})"
                        temp_filter.remove('playlist')
                        final_filters.add('playlist')

                if not temp_filter:

                    track = player.queue[player.queue.index(t)]
                    player.queue.remove(t)
                    tracklist.append(track)
                    if playlist_link:
                        playlist_hyperlink.add(playlist_link)

                    if amount:
                        amount_counter -= 1

            duplicated_titles.clear()

        if not tracklist:
            raise GenericError("Không tìm thấy bài hát nào với các bộ lọc đã chọn!")

        for t in reversed(tracklist):
            player.queue.insert(position-1, t)

        try:
            final_filters.remove("song_name")
            txt.append(f"**Bao gồm tên:** `{fix_characters(song_name)}`")
        except:
            pass

        try:
            final_filters.remove("song_author")
            txt.append(f"**Bao gồm tên trong người tải lên/nghệ sĩ:** `{fix_characters(song_author)}`")
        except:
            pass

        try:
            final_filters.remove("user")
            txt.append(f"**Yêu cầu của thành viên:** {user.mention}")
        except:
            pass

        try:
            final_filters.remove("playlist")
            txt.append(f"**Playlist:** {' | '.join(playlist_hyperlink)}")
        except:
            pass

        try:
            final_filters.remove("time_below")
            txt.append(f"**Với thời lượng ban đầu/bằng nhau:** `{time_format(min_duration)}`")
        except:
            pass

        try:
            final_filters.remove("time_above")
            txt.append(f"**Với thời lượng tối đa:** `{time_format(max_duration)}`")
        except:
            pass

        try:
            final_filters.remove("duplicates")
            txt.append(f"**Bài hát trùng lặp**")
        except:
            pass

        try:
            final_filters.remove("absent_members")
            txt.append("`Bài hát được yêu cầu bởi các thành viên đã rời kênh.`")
        except:
            pass

        if indexes:
            track = tracklist[0]
            txt = [
                f"đã chuyển bài hát [`{fix_characters(track.title, limit=25)}`]({track.uri or track.search_uri}) sang vị trí hàng đợi **[{position}]**.",
                 f"↪️ **⠂{inter.author.mention} đã chuyển một bài hát đến vị trí [{position}]:**\n"
                f"╰[`{fix_characters(track.title, limit=43)}`]({track.uri or track.search_uri})"
            ]

            await self.interaction_message(inter, txt, emoji="↪️")

        else:

            moved_tracks = len(tracklist)

            moved_tracks_txt = moved_tracks if moved_tracks == 1 else f"[{position}-{position+moved_tracks-1}]"

            msg_txt = f"### ↪️ ⠂{inter.author.mention} đã di chuyển {moved_tracks} bài hát tới vị trí {moved_tracks_txt} trong hàng đợi:\n" + "\n".join(f"`{position+n}.` [`{fix_characters(t.title, 45)}`]({t.uri})" for n, t in enumerate(tracklist[:7]))

            if (track_extra:=(moved_tracks - 7)) > 0:
                msg_txt += f"\n`và nhiều {track_extra} bài hát khác).`"

            msg_txt += f"\n### ✅ ⠂(Các) bộ lọc được sử dụng:\n" + '\n'.join(txt)

            txt = [f"đã di chuyển {moved_tracks} bài hát tới vị trí **[{position}]** trong hàng đợi.", msg_txt]

            await self.interaction_message(inter, txt, emoji="↪️", force=True, thumb=tracklist[0].thumb)

    @move.autocomplete("playlist")
    @clear.autocomplete("playlist")
    async def queue_playlist(self, inter: disnake.Interaction, query: str):

        try:
            if not inter.author.voice:
                return
        except:
            pass

        try:
            await check_pool_bots(inter, only_voiced=True)
            bot = inter.music_bot
        except:
            traceback.print_exc()
            return

        try:
            player = bot.music.players[inter.guild_id]
        except KeyError:
            return

        return list(set([track.playlist_name for track in player.queue if track.playlist_name and
                         query.lower() in track.playlist_name.lower()]))[:20]

    @rotate.autocomplete("name")
    @move.autocomplete("song_name")
    @skip.autocomplete("name")
    @skipto.autocomplete("name")
    @remove.autocomplete("name")
    async def queue_tracks(self, inter: disnake.AppCmdInter, query: str):

        try:
            if not inter.author.voice:
                return
        except AttributeError:
            pass

        try:
            if not await check_pool_bots(inter, only_voiced=True):
                return
        except PoolException:
            pass
        except:
            return

        try:
            player = inter.music_bot.music.players[inter.guild_id]
        except KeyError:
            return

        results = []

        count = 0

        for track in player.queue:

            if count == 20:
                break

            title = track.title.lower().split()

            query_words = query.lower().split()

            word_count = 0

            for query_word in query.lower().split():
                for title_word in title:
                    if query_word in title_word:
                        title.remove(title_word)
                        word_count += 1
                        break

            if word_count == len(query_words):
                results.append(f"{track.title[:81]} || ID > {track.unique_id}")
                count += 1

        return results or [f"{track.title[:81]} || ID > {track.unique_id}" for n, track in enumerate(player.queue)
                           if query.lower() in track.title.lower()][:20]

    @move.autocomplete("uploader_name")
    @clear.autocomplete("uploader_name")
    async def queue_author(self, inter: disnake.Interaction, query: str):

        try:
            await check_pool_bots(inter, only_voiced=True)
            bot = inter.music_bot
        except:
            return

        if not inter.author.voice:
            return

        try:
            player = bot.music.players[inter.guild_id]
        except KeyError:
            return

        if not query:
            return list(set([track.authors_string for track in player.queue]))[:20]
        else:
            return list(set([track.authors_string for track in player.queue if query.lower() in track.authors_string.lower()]))[:20]

    restrict_cd = commands.CooldownMapping.from_cooldown(2, 7, commands.BucketType.member)
    restrict_mc =commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @is_dj()
    @has_player()
    @check_voice()
    @pool_command(name="restrictmode", aliases=["rstc", "restrict", "restrito", "modorestrito"], only_voiced=True, cooldown=restrict_cd, max_concurrency=restrict_mc,
                  description="Kích hoạt/Vô hiệu hóa chế độ lệnh hạn chế yêu cầu DJ/nhân viên.")
    async def restrict_mode_legacy(self, ctx: CustomContext):

        await self.restrict_mode.callback(self=self, inter=ctx)

    @is_dj()
    @has_player()
    @check_voice()
    @commands.slash_command(
        description=f"{desc_prefix}Kích hoạt/Vô hiệu hóa chế độ lệnh hạn chế của các lệnh yêu cầu DJ/nhân viên.",
        extras={"only_voiced": True}, cooldown=restrict_cd, max_concurrency=restrict_mc)
    async def restrict_mode(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        player.restrict_mode = not player.restrict_mode

        msg = ["kích hoạt", "🔐"] if player.restrict_mode else ["vô hiệu hóa", "🔓"]

        text = [
            f"{msg[0]} Chế độ hạn chế của các lệnh người chơi (yêu cầu DJ/nhân viên).",
            f"{msg[1]} **⠂{inter.author.mention} {msg[0]} Chế độ hạn chế của các lệnh người chơi (yêu cầu DJ/nhân viên).**"
        ]

        await self.interaction_message(inter, text, emoji=msg[1])

    nonstop_cd = commands.CooldownMapping.from_cooldown(2, 15, commands.BucketType.member)
    nonstop_mc =commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @has_player()
    @check_voice()
    @pool_command(name="247", aliases=["nonstop"], only_voiced=True, cooldown=nonstop_cd, max_concurrency=nonstop_mc,
                  description="Kích hoạt/Tắt chế độ 24/7 của trình phát.")
    async def nonstop_legacy(self, ctx: CustomContext):
        await self.nonstop.callback(self=self, inter=ctx)

    @has_player()
    @check_voice()
    @commands.slash_command(
        name="247",
        description=f"{desc_prefix}Kích hoạt/Vô hiệu hóa Chế độ 24/7 của trình phát.",
        default_member_permissions=disnake.Permissions(manage_guild=True),
        extras={"only_voiced": True}, cooldown=nonstop_cd, max_concurrency=nonstop_mc
    )
    async def nonstop(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        player.keep_connected = not player.keep_connected

        msg = ["kích hoạt", "♾️"] if player.keep_connected else ["vô hiệu hóa", "❌"]

        text = [
            f"{msg[0]} Chế độ trình phát 24/7 (cài đặt).",
            f"{msg[1]} **⠂{inter.author.mention} {msg[0]} Chế độ 24/7 (cài đặt) của người chơi.**"
        ]

        if not len(player.queue):
            player.queue.extend(player.played)
            player.played.clear()

        await player.process_save_queue()

        if player.current:
            await self.interaction_message(inter, txt=text, emoji=msg[1])
            return

        await self.interaction_message(inter, text)

        await player.process_next()

    autoplay_cd = commands.CooldownMapping.from_cooldown(2, 15, commands.BucketType.member)
    autoplay_mc = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @has_player()
    @check_voice()
    @pool_command(name="autoplay", aliases=["ap", "aplay"], only_voiced=True, cooldown=autoplay_cd, max_concurrency=autoplay_mc,
                  description="Kích hoạt/tắt phát lại tự động bằng cách hoàn thành các dòng trong hàng đợi.")
    async def autoplay_legacy(self, ctx: CustomContext):
        await self.autoplay.callback(self=self, inter=ctx)

    @has_player()
    @check_voice()
    @commands.slash_command(
        name="autoplay",
        description=f"{desc_prefix}Kích hoạt/tắt phát lại tự động bằng cách hoàn thành các dòng trong hàng đợi.",
        extras={"only_voiced": True}, cooldown=autoplay_cd, max_concurrency=autoplay_mc
    )
    async def autoplay(self, inter: disnake.AppCmdInter):

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        player.autoplay = not player.autoplay

        msg = ["kích hoạt", "<:KleeParty:1136214064067006565>"] if player.autoplay else ["vô hiệu hóa", "<:verify:1134033164151566460>"]

        text = [f"{msg[0]}  Tự động phát.",
                f"{msg[1]} **⠂{inter.author.mention} {msg[0]}  Tự động phát.**"]

        if player.current:
            await self.interaction_message(inter, txt=text, emoji=msg[1])
            return

        await self.interaction_message(inter, text)

        await player.process_next()

    @check_voice()
    @has_player()
    @is_dj()
    @commands.cooldown(1, 10, commands.BucketType.guild)
    @commands.slash_command(
        description=f"{desc_prefix}Di chuyển người chơi sang một máy chủ âm nhạc khác."
    )
    async def change_node(
            self,
            inter: disnake.AppCmdInter,
            node: str = commands.Param(name="server", description="Máy chủ âm nhạc")
    ):

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        if node not in bot.music.nodes:
            raise GenericError(f"Máy chủ âm nhạc **{node}** không tìm thấy.")

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        if node == player.node.identifier:
            raise GenericError(f"Người chơi đã ở trên máy chủ âm nhạc **{node}**.")

        await player.change_node(node)
        player.native_yt = True

        await self.interaction_message(
            inter,
            [f"Di chuyển trình phát sang máy chủ âm nhạc **{node}**",
             f"**Người chơi đã được di chuyển đến máy chủ âm nhạc:** `{node}`"],
            emoji="🌎"
        )

    @search.autocomplete("server")
    @play.autocomplete("server")
    @change_node.autocomplete("server")
    async def node_suggestions(self, inter: disnake.Interaction, query: str):

        try:
            await check_pool_bots(inter)
            bot = inter.music_bot
        except GenericError:
            return
        except:
            bot = inter.bot

        try:
            node = bot.music.players[inter.guild_id].node
        except KeyError:
            node = None

        if not query:
            return [n.identifier for n in bot.music.nodes.values() if
                    n != node and n.available and n.is_available]

        return [n.identifier for n in bot.music.nodes.values() if n != node
                and query.lower() in n.identifier.lower() and n.available and n.is_available]

    @commands.command(aliases=["puptime"], description="Xem thông tin thời gian mà người chơi đang hoạt động trên máy chủ.")
    @commands.cooldown(1, 5, commands.BucketType.guild)
    async def playeruptime(self, ctx: CustomContext):

        uptime_info = []
        for bot in self.bot.pool.bots:
            try:
                player = bot.music.players[ctx.guild.id]
                uptime_info.append(f"🤖**Bot:** {bot.user.mention}\n"
                            f"<a:loading:1204300257874288681> **Uptime:** <t:{player.uptime}:R>\n"
                            f"<:K_NIGGA:1141183364561387671> **Kênh:** {player.guild.me.voice.channel.mention}")
            except KeyError:
                continue

        if not uptime_info:
            raise GenericError("**Không có người chơi đang hoạt động trên máy chủ.**")

        await ctx.reply(
            embed=disnake.Embed(
                title="**Player Uptime:**",
                description="\n-----\n".join(uptime_info),
                color=self.bot.get_color(ctx.guild.me)
            ), fail_if_not_exists=False
        )

    fav_import_export_cd = commands.CooldownMapping.from_cooldown(1, 15, commands.BucketType.member)
    fav_cd = commands.CooldownMapping.from_cooldown(3, 15, commands.BucketType.member)

    @commands.command(name="favmanager", aliases=["favs", "favoritos", "fvmgr", "favlist",
                                                  "integrations", "integrationmanager", "itg", "itgmgr", "itglist", "integrationlist",
                                                  "serverplaylist", "spl", "svp", "svpl"],
                      description="Quản lý yêu thích/tích hợp và liên kết của bạn từ máy chủ.", cooldown=fav_cd)
    async def fav_manager_legacy(self, ctx: CustomContext):
        await self.fav_manager.callback(self=self, inter=ctx)

    @commands.max_concurrency(1, commands.BucketType.member, wait=False)
    @commands.slash_command(
        description=f"{desc_prefix}Quản lý yêu thích/tích hợp và liên kết của bạn từ máy chủ.",
        cooldown=fav_cd)
    async def fav_manager(self, inter: disnake.AppCmdInter):

        bot = self.bot

        mode = ViewMode.fav_manager

        guild_data = None
        interaction = None

        if isinstance(inter, CustomContext):
            prefix = inter.clean_prefix

            if inter.invoked_with in ("serverplaylist", "spl", "svp", "svpl") and inter.author.guild_permissions.manage_guild:

                interaction, bot = await select_bot_pool(inter, return_new=True)

                mode = ViewMode.guild_fav_manager

                await interaction.response.defer(ephemeral=True)

                inter, guild_data = await get_inter_guild_data(inter, bot)

            elif inter.invoked_with in ("integrations", "integrationmanager", "itg", "itgmgr", "itglist", "integrationlist"):
                mode = ViewMode.integrations_manager

        else:
            try:
                global_data = inter.global_guild_data
            except AttributeError:
                global_data = await bot.get_global_data(inter.guild_id, db_name=DBModel.guilds)
                try:
                    inter.global_guild_data = global_data
                except:
                    pass
            prefix = global_data['prefix'] or bot.default_prefix

        if not interaction:
            interaction = inter

        if not interaction.response.is_done():
            await inter.response.defer(ephemeral=True)

        try:
            user_data = inter.global_user_data
        except AttributeError:
            user_data = await bot.get_global_data(inter.author.id, db_name=DBModel.users)
            try:
                inter.global_user_data = user_data
            except:
                pass

        view = FavMenuView(bot=bot, ctx=inter, data=user_data, prefix=prefix, mode=mode)
        view.guild_data = guild_data

        embed = view.build_embed()

        if not embed:
            await inter.send("**Không có hỗ trợ cho tính năng này vào lúc này...**\n\n"
                             "`Hỗ trợ cho Spotify và YTDL không được kích hoạt.`", ephemeral=True)
            return

        if isinstance(inter, CustomContext):
            try:
                view.message = inter.store_message
                await inter.store_message.edit(embed=embed, view=view)
            except:
                view.message = await inter.send(embed=embed, view=view)
        else:
            try:
                await inter.edit_original_message(embed=embed, view=view)
            except:
                await inter.response.edit_message(embed=embed, view=view)

        await view.wait()

    playerinfo_cd = commands.CooldownMapping.from_cooldown(1, 7, commands.BucketType.member)

    @commands.command(name="nowplaying", aliases=["nplaying"], cooldown=playerinfo_cd,
                      description="Hiển thị thông tin người chơi bạn đang hoạt động.")
    async def nowplaying_legacy(self, ctx: CustomContext):
        await self.nowplaying.callback(self=self, inter=ctx)

    @commands.slash_command(description=f"{desc_prefix}Hiển thị thông tin người chơi đang hoạt động.",
                            cooldown=playerinfo_cd)
    async def nowplaying(self, inter: disnake.AppCmdInter):
        await inter.response.defer(ephemeral=True)

        for bot in self.bot.pool.bots:

            for player_id in bot.music.players:

                player = bot.music.players[player_id]

                try:
                    vc = player.guild.me.voice.channel
                except AttributeError:
                    continue

                public_vc = "DISCOVERABLE" in player.guild.features and vc.permissions_for(player.guild.default_role).connect

                if inter.author.id in vc.voice_states:

                    if not player.current:
                        raise GenericError(f"**Hiện tại tôi không chơi thứ gì đó trên kênh {vc.mention}**")

                    if player.guild_id != inter.guild_id and not (await bot.is_owner(inter.author)):

                        current_guild = None

                        for b in self.bot.pool.bots:
                            if current_guild:=b.get_guild(inter.guild_id):
                                break

                        if not current_guild:
                            raise GenericError("**Bạn phải có ít nhất 1 bot tương thích được thêm vào máy chủ hiện tại.**")

                        member = current_guild.get_member(inter.author.id)

                        if not member.guild_permissions.manage_guild:
                            raise GenericError("**Bạn không có quyền quản lý máy chủ trên máy chủ hiện tại**")

                        member = player.guild.get_member(inter.author.id)

                        if not public_vc and not member.guild_permissions.manage_guild:
                            raise GenericError(f"**Bạn không có quyền quản lý máy chủ trong [kênh]({vc.jump_url}) trong đó bạn hiện đang được kết nối.**")

                    vc_name = vc.jump_url if public_vc else f"[`{fix_characters(vc.name)}`]({vc.jump_url})"

                    txt = f"### Thông tin người chơi mà người dùng {inter.author.mention} đang hoạt động:\n\n" \
                        f"> <:play:1140220726327136427> **⠂Bài hát hiện tại:** [`{fix_characters(player.current.title, 30)}`]({player.current.uri or player.current.search_uri})\n"
                    
                    txt += f"> <:gxplayer:1203882663958548492> **⠂Nguồn:** {player.current.info['sourceName']}\n"

                    if player.current.playlist:
                        txt += f"> <:playlist:1140220773051678811> **⠂Danh sách phát:** [`{fix_characters(player.current.playlist_name, 28)}`]({player.current.playlist_url})\n"

                    if player.queue:
                        txt += f"> <:musicalbum:1183394320292790332> **⠂Các bài hát trong hàng đợi** {len(player.queue)}\n"

                    txt += f"> 🔊 **⠂{'Kênh thoại' if isinstance(vc, disnake.VoiceChannel) else 'Sân khấu'}:** {vc_name}\n"\
                           f"> 🎧 **⠂Người nghe hiện tại:** `{len([m for m in vc.members if not m.bot and (not m.voice.self_deaf or not m.voice.deaf)])}`\n"\
                           f"> <:timeout:1155781760571949118> **⠂Hoạt động kể từ:** <t:{player.uptime}:f> - <t:{player.uptime}:R>\n"

                    embed = disnake.Embed(description=txt, color=self.bot.get_color(player.guild.me),)

                    if player.current.is_stream:
                        txt = "🔴 [34;1m⠂Phát sóng trực tiếp[0m"
                    else:
                        progress = ProgressBar(
                            player.position,
                            player.current.duration,
                            bar_count=20
                        )
                        txt = f"[34;1m[{time_format(player.position)}] {('=' * progress.start)}[0m🔴️[36;1m{'-' * progress.end} " \
                               f"[{time_format(player.current.duration)}][0m"

                    embed.description += f"```ansi\n{txt}```\n"

                    embed.set_author(name=bot.user.display_name,
                                     icon_url=bot.user.display_avatar.with_static_format("png").url)

                    embed.set_thumbnail(url=player.current.thumb)

                    try:
                        footer_kw = {"icon_url": player.guild.icon.with_static_format("png").url}
                    except AttributeError:
                        footer_kw = {}

                    embed.set_footer(text=f"Máy chủ: {player.guild.name} [ID: {player.guild.id}]", **footer_kw)
                    try:
                        await inter.edit_original_response(embed=embed)
                    except AttributeError:
                        await inter.send(embed=embed)
                    return

        raise GenericError("**Bạn không được kết nối với kênh thoại với trình phát đang hoạt động...**")

    @commands.Cog.listener("on_message_delete")
    async def player_message_delete(self, message: disnake.Message):

        if not message.guild:
            return

        try:

            player: LavalinkPlayer = self.bot.music.players[message.guild.id]

            if message.id != player.message.id:
                return

        except (AttributeError, KeyError):
            return

        thread = self.bot.get_channel(message.id)

        if not thread:
            return

        player.message = None
        await thread.edit(archived=True, locked=True, name=f"Tên: {thread.name}")

    @commands.Cog.listener('on_ready')
    async def resume_players_ready(self):

        if not self.bot.bot_ready:
            return

        for guild_id in list(self.bot.music.players):

            try:

                player: LavalinkPlayer = self.bot.music.players[guild_id]

                try:
                    channel_id = player.guild.me.voice.channel.id
                except AttributeError:
                    channel_id = player.channel_id

                vc = self.bot.get_channel(channel_id) or player.last_channel

                try:
                    player.guild.voice_client.cleanup()
                except:
                    pass

                if not vc:
                    print(
                        f"{self.bot.user} - {player.guild.name} [{guild_id}] - Trình phát không thể kết nối lại với kênh thoại.")
                    try:
                        await player.playerdestroy()
                    except:
                        traceback.print_exc()
                    continue

                await player.connect(vc.id)

                if not player.is_paused and not player.is_playing:
                    await player.process_next()
                print(f"{self.bot.user} - {player.guild.name} [{guild_id}] - Trình phát đã đuọc kết nối lại.")
            except:
                traceback.print_exc()

    async def is_request_channel(self, ctx: Union[disnake.AppCmdInter, disnake.MessageInteraction, CustomContext], *,
                                 data: dict = None, ignore_thread=False) -> bool:

        if isinstance(ctx, (CustomContext, disnake.MessageInteraction)):
            return True

        try:
            bot = ctx.music_bot
            channel_ctx = bot.get_channel(ctx.channel_id)
        except AttributeError:
            bot = ctx.bot
            channel_ctx = ctx.channel

        if not self.bot.check_bot_forum_post(channel_ctx):
            return True

        try:
            player: LavalinkPlayer = bot.music.players[ctx.guild_id]

            if not player.static:
                return False

            if isinstance(channel_ctx, disnake.Thread) and player.text_channel == channel_ctx.parent:
                return not ignore_thread

            return player.text_channel == channel_ctx

        except KeyError:

            try:
                guild_data = ctx.guild_data
            except AttributeError:
                guild_data = data or await bot.get_data(ctx.guild_id, db_name=DBModel.guilds)

            try:
                channel = bot.get_channel(int(guild_data["player_controller"]["channel"]))
            except:
                channel = None

            if not channel:
                return False

            if isinstance(channel_ctx, disnake.Thread) and channel == channel_ctx.parent:
                return not ignore_thread

            return channel.id == channel_ctx.id

    async def check_channel(
            self,
            guild_data: dict,
            inter: Union[disnake.AppCmdInter, CustomContext],
            channel: Union[disnake.TextChannel, disnake.VoiceChannel, disnake.Thread],
            guild: disnake.Guild,
            bot: BotCore
    ):

        static_player = guild_data['player_controller']

        warn_message = None
        message: Optional[disnake.Message] = None

        try:
            channel_db = bot.get_channel(int(static_player['channel'])) or await bot.fetch_channel(
                int(static_player['channel']))
        except (TypeError, disnake.NotFound):
            channel_db = None
        except disnake.Forbidden:
            channel_db = bot.get_channel(inter.channel_id)
            warn_message = f"Tôi không có quyền truy cập kênh <#{static_player['channel']}>, trình phát sẽ được sử dụng ở chế độ truyền thống."
            static_player["channel"] = None

        if not channel_db or channel_db.guild.id != inter.guild_id:
            await self.reset_controller_db(inter.guild_id, guild_data, inter)

        else:

            if channel_db.id != channel.id:

                try:
                    if isinstance(channel_db, disnake.Thread):

                        if not channel_db.parent:
                            await self.reset_controller_db(inter.guild_id, guild_data, inter)
                            channel_db = None

                        else:
                            if channel_db.owner != bot.user.id:

                                if not isinstance(channel_db.parent, disnake.ForumChannel):
                                    await self.reset_controller_db(inter.guild_id, guild_data, inter)
                                    channel_db = None
                                else:

                                    thread = None

                                    for t in channel_db.parent.threads:

                                        if t.owner_id == bot.user.id:
                                            try:
                                                message = await t.fetch_message(t.id)
                                            except disnake.NotFound:
                                                continue
                                            if not message or message.author.id != bot.user.id:
                                                continue
                                            thread = t
                                            break

                                    if not thread and guild.me.guild_permissions.read_message_history:
                                        async for t in channel_db.parent.archived_threads(limit=100):
                                            if t.owner_id == bot.user.id:
                                                try:
                                                    message = await t.fetch_message(t.id)
                                                except disnake.NotFound:
                                                    continue
                                                if not message or message.author.id != bot.user.id:
                                                    continue
                                                thread = t
                                                break

                                    if not thread:
                                        thread_wmessage = await channel_db.parent.create_thread(
                                            name=f"{bot.user} song-request",
                                            content="Đăng bài theo yêu cầu bài hát.",
                                            auto_archive_duration=10080,
                                            slowmode_delay=5,
                                        )
                                        channel_db = thread_wmessage.thread
                                        message = thread_wmessage.message
                                    else:
                                        channel_db = thread

                            thread_kw = {}

                            if channel_db.locked and channel_db.permissions_for(guild.me).manage_threads:
                                thread_kw.update({"locked": False, "archived": False})

                            elif channel_db.archived and channel_db.owner_id == bot.user.id:
                                thread_kw["archived"] = False

                            if thread_kw:
                                await channel_db.edit(**thread_kw)

                            elif isinstance(channel.parent, disnake.ForumChannel):
                                warn_message = f"**{bot.user.mention} không có quyền quản lý chủ đề " \
                                                 f"để hủy lưu trữ/mở khóa chủ đề: {channel_db.mention}**"

                except AttributeError:
                    pass

                if channel_db:

                    channel_db_perms = channel_db.permissions_for(guild.me)

                    channel = bot.get_channel(inter.channel.id)

                    if isinstance(channel, disnake.Thread):
                        send_message_perm = getattr(channel_db, "parent", channel_db).permissions_for(channel.guild.me).send_messages_in_threads
                    else:
                        send_message_perm = channel_db.permissions_for(channel.guild.me).send_messages

                    if not send_message_perm:
                        raise GenericError(
                            f"**{bot.user.mention} không có quyền gửi tin nhắn trên kênh <#{static_player['channel']}>**\n"
                             "Nếu bạn muốn đặt lại cấu hình kênh yêu cầu nhạc, hãy sử dụng lệnh /reset hoặc /setup"
                             "lại..."
                        )

                    if not channel_db_perms.embed_links:
                        raise GenericError(
                            f"**{bot.user.mention} không có quyền đính kèm liên kết/nhúng vào kênh <#{static_player['channel']}>**\n"
                             "Nếu bạn muốn đặt lại cấu hình kênh yêu cầu nhạc, hãy sử dụng lệnh /reset hoặc /setup"
                             "lại..."
                        )

        return channel_db, warn_message, message

    async def process_player_interaction(
            self,
            interaction: Union[disnake.MessageInteraction, disnake.ModalInteraction],
            command: Optional[disnake.AppCmdInter],
            kwargs: dict
    ):

        if not command:
            raise GenericError("không tìm thấy lệnh/thực hiện.")

        await check_cmd(command, interaction)

        await command(interaction, **kwargs)

        try:
            player: LavalinkPlayer = self.bot.music.players[interaction.guild_id]
            player.interaction_cooldown = True
            await asyncio.sleep(1)
            player.interaction_cooldown = False
            await command._max_concurrency.release(interaction)
        except (KeyError, AttributeError):
            pass

    @commands.Cog.listener("on_dropdown")
    async def guild_pin(self, interaction: disnake.MessageInteraction):

        if not self.bot.bot_ready:
            await interaction.send("Tôi vẫn đang khởi tạo...\nVui lòng đợi thêm một lát nữa...", ephemeral=True)
            return

        if interaction.data.custom_id != "player_guild_pin":
            return

        if not interaction.data.values:
            await interaction.response.defer()
            return

        if not interaction.user.voice:
            await interaction.send("Bạn phải tham gia một kênh thoại để sử dụng tính năng này.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild_data = await self.bot.get_data(interaction.guild_id, db_name=DBModel.guilds)

        try:
            query = interaction.data.values[0]
        except KeyError:
            await interaction.send("**Mục đã chọn không được tìm thấy trong cơ sở dữ liệu...**", ephemeral=True)
            await send_idle_embed(interaction.message, bot=self.bot, guild_data=guild_data, force=True)
            return

        kwargs = {
            "query": f"> pin: {query}",
            "position": 0,
            "options": False,
            "manual_selection": True,
            "source": "ytsearch",
            "repeat_amount": 0,
            "server": None,
            "force_play": "no"
        }

        try:
            await self.play.callback(self=self, inter=interaction, **kwargs)
        except Exception as e:
            self.bot.dispatch('interaction_player_error', interaction, e)

    @commands.Cog.listener("on_dropdown")
    async def player_dropdown_event(self, interaction: disnake.MessageInteraction):

        if not interaction.data.custom_id.startswith("musicplayer_dropdown_"):
            return

        if not interaction.values:
            await interaction.response.defer()
            return

        await self.player_controller(interaction, interaction.values[0])

    @commands.Cog.listener("on_button_click")
    async def player_button_event(self, interaction: disnake.MessageInteraction):

        if not interaction.data.custom_id.startswith("musicplayer_"):
            return

        await self.player_controller(interaction, interaction.data.custom_id)

    async def check_stage_title(self, inter, bot: BotCore, player: LavalinkPlayer):

        time_limit = 30 if isinstance(player.guild.me.voice.channel, disnake.VoiceChannel) else 120

        if player.stage_title_event and (time_:=int((disnake.utils.utcnow() - player.start_time).total_seconds())) < time_limit and not (await bot.is_owner(inter.author)):
            raise GenericError(
                f"**Bạn sẽ phải đợi {time_format((time_limit - time_) * 1000, use_names=True)} Để sử dụng chức năng này "
                f"với giai đoạn hoạt động quảng cáo tự động...**"
            )

    async def player_controller(self, interaction: disnake.MessageInteraction, control: str, **kwargs):

        if not self.bot.bot_ready:
            await interaction.send("Tôi vẫn đang khởi tạo...", ephemeral=True)
            return

        if not interaction.guild_id:
            await interaction.response.edit_message(components=None)
            return

        cmd_kwargs = {}

        cmd: Optional[disnake.AppCmdInter] = None

        if control in (
                PlayerControls.embed_forceplay,
                PlayerControls.embed_enqueue_track,
                PlayerControls.embed_enqueue_playlist,
        ):

            try:
                try:
                    if not (url:=interaction.message.embeds[0].author.url):
                        return
                except:
                    return

                try:
                    await self.player_interaction_concurrency.acquire(interaction)
                except:
                    raise GenericError("Có một bài hát hiện đang được xử lý...")

                bot: Optional[BotCore] = None
                player: Optional[LavalinkPlayer] = None
                channel: Union[disnake.TextChannel, disnake.VoiceChannel, disnake.StageChannel, disnake.Thread] = None
                author: Optional[disnake.Member] = None

                for b in sorted(self.bot.pool.bots, key=lambda b: b.identifier, reverse=True):

                    try:
                        p = b.music.players[interaction.guild_id]
                    except KeyError:
                        if c := b.get_channel(interaction.channel_id):
                            bot = b
                            channel = c
                            author = c.guild.get_member(interaction.author.id)
                        continue

                    if p.guild.me.voice and interaction.author.id in p.guild.me.voice.channel.voice_states:

                        if p.locked:
                            raise GenericError(
                                "**Không thể thực hiện hành động này trong khi đang xử lý bài hát"
                                 "(vui lòng đợi thêm vài giây và thử lại).**")

                        player = p
                        bot = b
                        channel = player.text_channel
                        author = channel.guild.get_member(interaction.author.id)
                        break

                if not channel:
                    raise GenericError("Không có bot nào vào thời điểm này.")

                try:
                    if not author.voice:
                        raise GenericError("Bạn phải tham gia một kênh thoại để sử dụng nút này....")
                except AttributeError:
                    raise GenericError("Bạn phải tham gia một kênh thoại để sử dụng nút này....")
                    pass

                try:
                    node = player.node
                except:
                    node: Optional[wavelink.Node] = None

                try:
                    interaction.author = author
                except AttributeError:
                    pass

                if PlayerControls.embed_forceplay:
                    await check_player_perm(inter=interaction, bot=bot, channel=channel)

                vc_id: int = author.voice.channel.id

                can_connect(channel=author.voice.channel, guild=channel.guild)

                if control == PlayerControls.embed_enqueue_playlist:

                    if retry_after := self.bot.pool.enqueue_playlist_embed_cooldown.get_bucket(interaction).update_rate_limit():
                        raise GenericError(
                            f"**Bạn sẽ phải đợi {int(retry_after)} giây để thêm danh sách phát vào trình phát hiện tại.**")

                    if not player:
                        player = await self.create_player(inter=interaction, bot=bot, guild=channel.guild,
                                                          channel=channel, node=node)

                    await self.check_player_queue(interaction.author, bot, interaction.guild_id)
                    result, node = await self.get_tracks(url, author, source=False, node=player.node, bot=bot)
                    result = await self.check_player_queue(interaction.author, bot, interaction.guild_id, tracks=result)
                    player.queue.extend(result.tracks)
                    await interaction.send(f"{interaction.author.mention}, danh sách phát [`{result.name}`](<{url}>) đã được thêm thành công!{player.controller_link}", ephemeral=True)
                    if not player.is_connected:
                        await player.connect(vc_id)
                    if not player.current:
                        await player.process_next()

                else:

                    track: Union[LavalinkTrack, PartialTrack] = None
                    seek_status = False

                    if player:

                        if control == PlayerControls.embed_forceplay and player.current and (player.current.uri.startswith(url) or url.startswith(player.current.uri)):
                            await self.check_stage_title(inter=interaction, bot=bot, player=player)
                            await player.seek(0)
                            player.set_command_log("quay lại phần đầu của bài hát.", emoji="⏪")
                            await interaction.send("Lệnh đã được thực hiện thành công", ephemeral=True)
                            await asyncio.sleep(3)
                            await player.update_stage_topic()
                            await asyncio.sleep(7)
                            seek_status = True

                        else:

                            for t in list(player.queue):
                                if t.uri.startswith(url) or url.startswith(t.uri):
                                    track = t
                                    player.queue.remove(t)
                                    break

                            if not track:
                                for t in list(player.played):
                                    if t.uri.startswith(url) or url.startswith(t.uri):
                                        track = t
                                        player.played.remove(t)
                                        break

                                if not track:

                                    for t in list(player.failed_tracks):
                                        if t.uri.startswith(url) or url.startswith(t.uri):
                                            track = t
                                            player.failed_tracks.remove(t)
                                            break
                                        
                            await interaction.send("Lệnh đã được thực hiện thành công", ephemeral=True)

                    if not seek_status:

                        if not track:

                            if (retry_after := self.bot.pool.enqueue_track_embed_cooldown.get_bucket(interaction).update_rate_limit()):
                                raise GenericError(
                                    f"**Bạn sẽ phải đợi {int(retry_after)} giây để thêm bài hát mới vào hàng đợi.**")

                            if control == PlayerControls.embed_enqueue_track:
                                await self.check_player_queue(interaction.author, bot, interaction.guild_id)

                            result, node = await self.get_tracks(url, author, source=False, node=node, bot=bot)

                            try:
                                track = result.tracks[0]
                            except:
                                track = result[0]

                        if control == PlayerControls.embed_enqueue_track:

                            if not player:
                                player = await self.create_player(inter=interaction, bot=bot, guild=channel.guild,
                                                                  channel=channel, node=node)
                            await self.check_player_queue(interaction.author, bot, interaction.guild_id)
                            player.queue.append(track)
                            player.update = True
                            await interaction.send(f"{author.mention}, bài hát [`{track.title}`](<{track.uri}>) đã được thêm vào hàng đợi.{player.controller_link}", ephemeral=True)
                            if not player.is_connected:
                                await player.connect(vc_id)
                            if not player.current:
                                await player.process_next()

                        else:
                            if not player:
                                player = await self.create_player(inter=interaction, bot=bot, guild=channel.guild,
                                                                  channel=channel, node=node)
                            else:
                                await self.check_stage_title(inter=interaction, bot=bot, player=player)
                            player.queue.insert(0, track)
                            if not player.is_connected:
                                await player.connect(vc_id)
                            await self.process_music(inter=interaction, player=player, force_play="yes")

            except Exception as e:
                self.bot.dispatch('interaction_player_error', interaction, e)
                if not isinstance(e, GenericError):
                    await asyncio.sleep(5)
            try:
                await self.player_interaction_concurrency.release(interaction)
            except:
                pass
            return

        if control == PlayerControls.embed_add_fav:

            try:
                embed = interaction.message.embeds[0]
            except IndexError:
                await interaction.send("Các nhúng của tin nhắn đã bị xóa ...", ephemeral=True)
                return

            if (retry_after := self.bot.pool.add_fav_embed_cooldown.get_bucket(interaction).update_rate_limit()):
                await interaction.send(
                    f"**Bạn sẽ phải đợi {int(retry_after)} giây để thêm mục yêu thích mới.**",
                    ephemeral=True)
                return


            user_data = await self.bot.get_global_data(interaction.author.id, db_name=DBModel.users)

            if self.bot.config["MAX_USER_FAVS"] > 0 and not (await self.bot.is_owner(interaction.author)):

                if (current_favs_size := len(user_data["fav_links"])) > self.bot.config["MAX_USER_FAVS"]:
                    await interaction.edit_original_message(f"Số lượng mục trong tệp yêu thích của bạn vượt quá "
                                                             f"số lượng tối đa cho phép ({self.bot.config['MAX_USER_FAVS']}).")
                    return

                if (current_favs_size + (user_favs := len(user_data["fav_links"]))) > self.bot.config["MAX_USER_FAVS"]:
                    await interaction.edit_original_message(
                        "Bạn không có đủ dung lượng để thêm tất cả dấu trang vào tệp của mình...\n"
                         f"Giới hạn hiện tại: {self.bot.config['MAX_USER_FAVS']}\n"
                         f"Số mục yêu thích đã lưu: {user_favs}\n"
                         f"Bạn có: {(current_favs_size + user_favs) - self.bot.config['MAX_USER_FAVS']}")
                    return

            fav_name = embed.author.name[1:]

            user_data["fav_links"][fav_name] = embed.author.url

            await self.bot.update_global_data(interaction.author.id, user_data, db_name=DBModel.users)

            global_data = await self.bot.get_global_data(interaction.guild_id, db_name=DBModel.guilds)

            try:
                cmd = f"</play:" + str(self.bot.pool.controller_bot.get_global_command_named("play",
                                                                                             cmd_type=disnake.ApplicationCommandType.chat_input).id) + ">"
            except AttributeError:
                cmd = "/play"

            try:
                interaction.message.embeds[0].fields[0].value = f"{interaction.author.mention} " + \
                                                                interaction.message.embeds[0].fields[0].value.replace(
                                                                    interaction.author.mention, "")
            except IndexError:
                interaction.message.embeds[0].add_field(name="**Các thành viên đã thích danh sách phát:**",
                                                        value=interaction.author.mention)

            await interaction.send(embed=disnake.Embed(
                description=f"[`{fav_name}`](<{embed.author.url}>) **đã được thêm vào mục yêu thích của bạn!**\n\n"
                             "**Cách sử dụng?**\n"
                             f"* Sử dụng lệnh {cmd} (chọn mục ưa thích trong quá trình tự động hoàn thành tìm kiếm)\n"
                             "* Nhấp vào nút phát/chọn/tích hợp trình phát yêu thích.\n"
                             f"* Sử dụng lệnh {global_data['prefix'] or self.bot.default_prefix}{self.play_legacy.name} mà không bao gồm tên hoặc liên kết tới bài hát/video.\n"


            ).set_footer(text=f"Nếu bạn muốn xem tất cả các mục yêu thích của mình, hãy sử dụng lệnh {global_data['prefix'] or self.bot.default_prefix}{self.fav_manager_legacy.name}"), ephemeral=True)

            if not interaction.message.flags.ephemeral:
                if not interaction.guild:
                    await (await interaction.original_response()).edit(embed=interaction.message.embeds[0])
                else:
                    await interaction.message.edit(embed=interaction.message.embeds[0])
            return

        if not interaction.guild:
            await interaction.response.edit_message(components=None)
            return

        try:

            if control == "musicplayer_request_channel":
                cmd = self.bot.get_slash_command("setup")
                cmd_kwargs = {"target": interaction.channel}
                await self.process_player_interaction(interaction, cmd, cmd_kwargs)
                return

            if control == PlayerControls.fav_manager:

                if str(interaction.user.id) not in interaction.message.content:
                    await interaction.send("Bạn không thể tương tác ở đây!", ephemeral=True)
                    return

                cmd = self.bot.pool.controller_bot.get_slash_command("fav_manager")
                await self.process_player_interaction(interaction, cmd, cmd_kwargs)
                return

            if control == PlayerControls.integration_manager:

                if str(interaction.user.id) not in interaction.message.content:
                    await interaction.send("Bạn không thể tương tác ở đây!", ephemeral=True)
                    return

                cmd = self.bot.pool.controller_bot.get_slash_command("integrations")
                await self.process_player_interaction(interaction, cmd, cmd_kwargs)
                return

            if control == PlayerControls.add_song:

                if not interaction.user.voice:
                    raise GenericError("**Bạn phải tham gia một kênh thoại để sử dụng nút này.**")

                await interaction.response.send_modal(
                    title="Yêu cầu một bài hát",
                    custom_id=f"modal_add_song",
                    components=[
                        disnake.ui.TextInput(
                            style=disnake.TextInputStyle.short,
                            label="Tên bài hát/liên kết.",
                            placeholder="Tên hoặc link youtube/spotify/soundcloud, v.v.",
                            custom_id="song_input",
                            max_length=150,
                            required=True
                        ),
                        disnake.ui.TextInput(
                            style=disnake.TextInputStyle.short,
                            label="Vị trí dòng (số).",
                            placeholder="Tùy chọn, nếu không được sử dụng sẽ được thêm vào cuối.",
                            custom_id="song_position",
                            max_length=3,
                            required=False
                        ),
                    ]
                )

                return

            if control == PlayerControls.enqueue_fav:

                if not interaction.user.voice:
                    raise GenericError("**Bạn phải tham gia một kênh thoại để sử dụng nút này.**")

                cmd_kwargs = {
                    "query": kwargs.get("query", ""),
                    "position": 0,
                    "options": False,
                    "source": None,
                    "repeat_amount": 0,
                    "server": None,
                    "force_play": "no"
                }

                cmd_kwargs["manual_selection"] = not cmd_kwargs["query"]

                cmd = self.bot.get_slash_command("play")

            else:

                try:
                    player: LavalinkPlayer = self.bot.music.players[interaction.guild_id]
                except KeyError:
                    await interaction.send("Không có người chơi đang hoạt động trên máy chủ...", ephemeral=True)
                    await send_idle_embed(interaction.message, bot=self.bot)
                    return

                if interaction.message != player.message:
                    return

                if player.interaction_cooldown:
                    raise GenericError("Thao tác quá nhanh, vui lòng thử lại")

                try:
                    vc = player.guild.me.voice.channel
                except AttributeError:
                    await player.playerdestroy(force=True)
                    return

                if control == PlayerControls.help_button:
                    embed = disnake.Embed(
                        description="📘 **THÔNG TIN VỀ CÁC NÚT** 📘\n\n"
                                     "⏯️ `= Tạm dừng/Tiếp tục nhạc.`\n"
                                     "⏮️ `= Quay lại bài hát đã phát trước đó.`\n"
                                     "⏭️ `= Chuyển sang bài hát tiếp theo.`\n"
                                     "🔀 `= Trộn các bài hát trong hàng đợi.`\n"
                                     "🎶 `= Thêm bài hát/danh sách phát/yêu thích.`\n"
                                     "⏹️ `= Dừng trình phát và ngắt kết nối khỏi kênh.`\n"
                                     "📑 `= Hiển thị hàng đợi nhạc.`\n"
                                     "🛠️ `= Thay đổi một số cài đặt của người chơi:`\n"
                                     "`âm lượng/hiệu ứng nightcore/lặp lại/chế độ hạn chế.`\n",
                        color=self.bot.get_color(interaction.guild.me)
                    )

                    await interaction.response.send_message(embed=embed, ephemeral=True)
                    return

                if not interaction.author.voice or interaction.author.voice.channel != vc:
                    raise GenericError(f"Bạn phải ở trên kênh <#{vc.id}> để sử dụng các nút trình phát.")

                if control == PlayerControls.miniqueue:
                    await is_dj().predicate(interaction)
                    player.mini_queue_enabled = not player.mini_queue_enabled
                    player.set_command_log(
                        emoji="📑",
                        text=f"{interaction.author.mention} {'kích hoạt' if player.mini_queue_enabled else 'vô hiệu hóa'} "
                             f"hàng đợi mini."
                    )
                    await player.invoke_np(interaction=interaction)
                    return

                try:
                    await self.player_interaction_concurrency.acquire(interaction)
                except commands.MaxConcurrencyReached:
                    raise GenericError(
                        "**Bạn có một tương tác ẩn!**\n`Nếu đó là một tin nhắn ẩn, tránh nhấp vào \"bỏ qua\".`")

                if control == PlayerControls.add_favorite:

                    if not player.current:
                        await interaction.send("**Hiện tại không có bài hát nào đang phát..**", ephemeral=True)
                        return

                    choices = {}
                    msg = ""

                    if player.current.uri:
                        choices["Track"] = {
                            "name": player.current.title,
                            "url": player.current.uri,
                            "emoji": "🎵"
                        }
                        msg += f"**Bài hát:** [`{player.current.title}`]({player.current.uri})\n"

                    if player.current.album_url:
                        choices["Album"] = {
                            "name": player.current.album_name,
                            "url": player.current.album_url,
                            "emoji": "💽"
                        }
                        msg += f"**Album:** [`{player.current.album_name}`]({player.current.album_url})\n"

                    if player.current.playlist_url:
                        choices["Playlist"] = {
                            "name": player.current.playlist_name,
                            "url": player.current.playlist_url,
                            "emoji": "<:music_queue:703761160679194734>"
                        }
                        msg += f"**Playlist:** [`{player.current.playlist_name}`]({player.current.playlist_url})\n"

                    if not choices:
                        try:
                            await self.player_interaction_concurrency.release(interaction)
                        except:
                            pass
                        await interaction.send(
                            embed=disnake.Embed(
                                color=self.bot.get_color(interaction.guild.me),
                                description="### Không có mục nào để ủng hộ âm nhạc hiện tại."
                            ), ephemeral=True
                        )
                        return

                    if len(choices) == 1:
                        select_type, info = list(choices.items())[0]

                    else:
                        view = SelectInteraction(
                            user=interaction.author, timeout=30,
                            opts=[disnake.SelectOption(label=k, description=v["name"][:50], emoji=v["emoji"]) for k,v in choices.items()]
                        )

                        await interaction.send(
                            embed=disnake.Embed(
                                color=self.bot.get_color(interaction.guild.me),
                                description=f"### Chọn một mục bài hát hiện tại để thêm vào mục yêu thích của bạn:"
                                            f"\n\n{msg}"
                            ), view=view, ephemeral=True
                        )

                        await view.wait()

                        select_interaction = view.inter

                        if not select_interaction or view.selected is False:
                            try:
                                await self.player_interaction_concurrency.release(interaction)
                            except:
                                pass
                            await interaction.edit_original_message(
                                embed=disnake.Embed(
                                    color=self.bot.get_color(interaction.guild.me),
                                    description="### Hoạt động bị hủy bỏ!"
                                ), view=None
                            )
                            return

                        interaction = select_interaction

                        select_type = view.selected
                        info = choices[select_type]

                    user_data = await self.bot.get_global_data(interaction.author.id, db_name=DBModel.users)

                    if self.bot.config["MAX_USER_FAVS"] > 0 and not (await self.bot.is_owner(interaction.author)):

                        if len(user_data["fav_links"]) >= self.bot.config["MAX_USER_FAVS"]:
                            await interaction.edit_original_message(
                                embed=disnake.Embed(
                                    color=self.bot.get_color(interaction.guild.me),
                                    description="Bạn không có đủ dung lượng để thêm tất cả dấu trang vào tệp của mình...\n"
                                                 f"Giới hạn hiện tại: {self.bot.config['MAX_USER_FAVS']}"
                                ), view=None)
                            return

                    user_data["fav_links"][fix_characters(info["name"], self.bot.config["USER_FAV_MAX_URL_LENGTH"])] = info["url"]

                    await self.bot.update_global_data(interaction.author.id, user_data, db_name=DBModel.users)

                    self.bot.dispatch("fav_add", interaction.user, user_data, f"[`{info['name']}`]({info['url']})")

                    global_data = await self.bot.get_global_data(interaction.author.id, db_name=DBModel.guilds)

                    try:
                        slashcmd = f"</play:" + str(self.bot.pool.controller_bot.get_global_command_named("play", cmd_type=disnake.ApplicationCommandType.chat_input).id) + ">"
                    except AttributeError:
                        slashcmd = "/play"

                    await interaction.edit_original_response(
                        embed=disnake.Embed(
                            color=self.bot.get_color(interaction.guild.me),
                            description="### Mục đã được thêm/chỉnh sửa thành công vào mục yêu thích của bạn:\n\n"
                                         f"**{select_type}:** [`{info['name']}`]({info['url']})\n\n"
                                         f"### Cách sử dụng nó?\n"
                                         f"* Sử dụng lệnh {slashcmd} (khi hoàn tất tìm kiếm tự động)\n"
                                         f"* Nhấp vào nút phát/chọn/tích hợp trình phát yêu thích.\n"
                                         f"* Sử dụng lệnh {global_data['prefix'] or self.bot.default_prefix}{self.play_legacy.name} mà không bao gồm tên hoặc liên kết tới bài hát/video."
                        ), view=None
                    )

                    try:
                        await self.player_interaction_concurrency.release(interaction)
                    except:
                        pass

                    return

                if control == PlayerControls.lyrics:
                    if not player.current:
                        try:
                            await self.player_interaction_concurrency.release(interaction)
                        except:
                            pass
                        await interaction.send("**Tôi không chơi thứ gì đó vào lúc này ...**", ephemeral=True)
                        return

                    if not player.current.ytid:
                        try:
                            await self.player_interaction_concurrency.release(interaction)
                        except:
                            pass
                        await interaction.send("Hiện tại chỉ có âm nhạc YouTube được hỗ trợ..", ephemeral=True)
                        return

                    not_found_msg = "Không có lời bài hát cho bài hát hiện tại..."

                    await interaction.response.defer(ephemeral=True, with_message=True)

                    if player.current.info["extra"].get("lyrics") is None:
                        player.current.info["extra"]["lyrics"] = await player.node.fetch_ytm_lyrics(player.current.ytid)
                    elif not player.current.info["extra"]["lyrics"]:
                        try:
                            await self.player_interaction_concurrency.release(interaction)
                        except:
                            pass
                        await interaction.edit_original_message(f"**{not_found_msg}**")
                        return

                    if not player.current.info["extra"]["lyrics"]:
                        try:
                            await self.player_interaction_concurrency.release(interaction)
                        except:
                            pass
                        player.current.info["extra"]["lyrics"] = {}
                        await interaction.edit_original_message(f"**{not_found_msg}**")
                        return

                    if player.current.info["extra"]["lyrics"] is not None:
                        if player.current.info["extra"]["lyrics"]["type"] == "text":
                            lyrics_string =  player.current.info["extra"]["lyrics"]["text"]
                        elif player.current.info["extra"]["lyrics"]["type"] == "timed":
                            lyric = []
                            for lines in player.current.info["extra"]["lyrics"]['lines']:
                                lyric.append(f"[{time_format(lines['range']['start'])}] {lines['line'].replace('♪',' 🎵 🎶 🎵 🎶 🎵 🎶 ')}")
                            lyrics_string = "\n".join(lyric)

                    try:
                        await self.player_interaction_concurrency.release(interaction)
                    except:
                        pass

                    await interaction.edit_original_message(
                        embed=disnake.Embed(
                            description=f"### Lời bài hát: [{player.current.title}]({player.current.uri})\n{lyrics_string}",
                            color=self.bot.get_color(player.guild.me)
                        ).set_thumbnail(player.current.thumb)
                    )
                    return

                if control == PlayerControls.volume:
                    cmd_kwargs = {"value": None}

                elif control == PlayerControls.queue:
                    cmd = self.bot.get_slash_command("queue").children.get("display")

                elif control == PlayerControls.shuffle:
                    cmd = self.bot.get_slash_command("queue").children.get("shuffle")

                elif control == PlayerControls.seek_to_start:
                    cmd = self.bot.get_slash_command("seek")
                    cmd_kwargs = {"position": "0"}

                elif control == PlayerControls.keep_connected:
                    cmd = self.bot.get_slash_command("247")

                elif control == PlayerControls.pause_resume:
                    control = PlayerControls.pause if not player.paused else PlayerControls.resume

                elif control == PlayerControls.loop_mode:

                    if player.loop == "current":
                        cmd_kwargs['mode'] = 'queue'
                    elif player.loop == "queue":
                        cmd_kwargs['mode'] = 'off'
                    else:
                        cmd_kwargs['mode'] = 'current'

                elif control == PlayerControls.skip:
                    cmd_kwargs = {"query": None, "play_only": "no"}

            if not cmd:
                cmd = self.bot.get_slash_command(control[12:])

            await self.process_player_interaction(
                interaction=interaction,
                command=cmd,
                kwargs=cmd_kwargs
            )

            try:
                await self.player_interaction_concurrency.release(interaction)
            except:
                pass

        except Exception as e:
            try:
                await self.player_interaction_concurrency.release(interaction)
            except:
                pass
            self.bot.dispatch('interaction_player_error', interaction, e)

    @commands.Cog.listener("on_modal_submit")
    async def song_request_modal(self, inter: disnake.ModalInteraction):

        if inter.custom_id.startswith("modal_add_song"):

            try:

                query = inter.text_values["song_input"]
                position = inter.text_values["song_position"]

                if position:
                    if not position.isdigit():
                        raise GenericError("**Vị trí của dòng phải là một số.**")
                    position = int(position)

                    if position < 1:
                        raise GenericError("**Số vị trí hàng đợi phải từ 1 trở lên.**")

                kwargs = {
                    "query": query,
                    "position": position or 0,
                    "options": False,
                    "manual_selection": True,
                    "source": None,
                    "repeat_amount": 0,
                    "server": None,
                    "force_play": "no",
                }

                await self.process_player_interaction(
                    interaction=inter,
                    command=self.bot.get_slash_command("play"),
                    kwargs=kwargs,
                )
            except Exception as e:
                self.bot.dispatch('interaction_player_error', inter, e)

    async def delete_message(self, message: disnake.Message, delay: int = None, ignore=False):

        if ignore:
            return

        try:
            is_forum = isinstance(message.channel.parent, disnake.ForumChannel)
        except AttributeError:
            is_forum = False

        if message.is_system() and is_forum:
            return

        if message.channel.permissions_for(message.guild.me).manage_messages or message.author.id == self.bot.user.id:

            try:
                await message.delete(delay=delay)
            except:
                traceback.print_exc()

    @commands.Cog.listener("on_song_request")
    async def song_requests(self, ctx: Optional[CustomContext], message: disnake.Message):

        if ctx.command or message.mentions:
            return

        if message.author.bot and not isinstance(message.channel, disnake.StageChannel):
            return

        try:
            data = await self.bot.get_data(message.guild.id, db_name=DBModel.guilds)
        except AttributeError:
            return

        player: Optional[LavalinkPlayer] = self.bot.music.players.get(message.guild.id)

        if player and isinstance(message.channel, disnake.Thread) and not player.static:

            try:
                if player.text_channel.id != message.id:
                    return
            except AttributeError:
                return

            if not player.controller_mode:
                return

            text_channel = message.channel

        else:

            static_player = data['player_controller']

            channel_id = static_player['channel']

            if not channel_id:
                return

            if isinstance(message.channel, disnake.Thread):
                if isinstance(message.channel.parent, disnake.TextChannel):
                    if str(message.channel.parent.id) != channel_id:
                        return
                elif str(message.channel.id) != channel_id:
                    return
            elif str(message.channel.id) != channel_id:
                return

            text_channel = self.bot.get_channel(int(channel_id)) or await self.bot.fetch_channel(int(channel_id))

            if not text_channel:
                await self.reset_controller_db(message.guild.id, data)
                return

            if isinstance(text_channel, disnake.Thread):
                send_message_perm = text_channel.parent.permissions_for(message.guild.me).send_messages_in_threads
            else:
                send_message_perm = text_channel.permissions_for(message.guild.me).send_messages

            if not send_message_perm:
                return

            if not self.bot.intents.message_content:

                if self.song_request_cooldown.get_bucket(message).update_rate_limit():
                    return

                await message.channel.send(
                    message.author.mention,
                    embed=disnake.Embed(
                        description="Thật không may, tôi không thể kiểm tra nội dung tin nhắn của bạn...\n"
                                    "Cố gắng thêm nhạc bằng cách sử dụng **/play ** hoặc nhấp vào một trong các nút bên dưới:",
                        color=self.bot.get_color(message.guild.me)
                    ),
                     delete_after=20
                )
                return

        if message.content.startswith("/") or message.is_system():
            await self.delete_message(message)
            return

        try:
            if isinstance(message.channel, disnake.Thread):

                if isinstance(message.channel.parent, disnake.ForumChannel):

                    if data['player_controller']["channel"] != str(message.channel.id):
                        return
                    if message.is_system():
                        await self.delete_message(message, ignore=data['player_controller']['purge_mode'] != SongRequestPurgeMode.on_message)

        except AttributeError:
            pass

        msg = None
        error = None
        has_exception = None

        try:
            if message.author.bot:
                if message.is_system() and not isinstance(message.channel, disnake.Thread):
                    await self.delete_message(message, ignore=data['player_controller']['purge_mode'] != SongRequestPurgeMode.on_message)
                if message.author.id == self.bot.user.id:
                    await self.delete_message(message, delay=15, ignore=data['player_controller']['purge_mode'] != SongRequestPurgeMode.on_message)
                return

            if not message.content:

                if message.type == disnake.MessageType.thread_starter_message:
                    return

                if message.is_system():
                    await self.delete_message(message)
                    return

                try:
                    attachment = message.attachments[0]
                except IndexError:
                    await message.channel.send(f"{message.author.mention} Bạn phải gửi tên/tên âm nhạc.")
                    return

                else:

                    if attachment.size > 18000000:
                        await message.channel.send(f"{message.author.mention} Tệp bạn đã gửi phải có kích thước "
                                                   f"kém hơn 18MB.")
                        return

                    if attachment.content_type not in self.audio_formats:
                        await message.channel.send(f"{message.author.mention} Tệp bạn đã gửi phải có kích thước "
                                                   f"kém hơn 18MB.")
                        return

                    message.content = attachment.url

            try:
                await self.song_request_concurrency.acquire(message)
            except:

                await message.channel.send(
                    f"{message.author.mention} ơi, bạn phải đợi bài hát mà bạn đã yêu cầu trước đó tải lên đã...",
                )

                await self.delete_message(message, ignore=data['player_controller']['purge_mode'] != SongRequestPurgeMode.on_message)
                return

            message.content = message.content.strip("<>")

            urls = URL_REG.findall(message.content)

            if not urls:
                source = self.bot.config["DEFAULT_SEARCH_PROVIDER"]

            else:
                source = False
                message.content = urls[0]

                if "&list=" in message.content:

                    view = SelectInteraction(
                        user=message.author,
                        opts=[
                            disnake.SelectOption(label="Bài hát", emoji="🎵",
                                                 description="Chỉ tải bài hát từ link", value="music"),
                            disnake.SelectOption(label="Playlist", emoji="🎶",
                                                 description="Tải danh sách phát vào và phát nó", value="playlist"),
                        ], timeout=30)

                    embed = disnake.Embed(
                        description="**Liên kết chứa video có danh sách phát.**\n"
                                     f'Chọn một tùy chọn trong <t:{int((disnake.utils.utcnow() + datetime.timedelta(seconds=30)).timestamp())}:R> để tiếp tục.',
                        color=self.bot.get_color(message.guild.me)
                    )

                    msg = await message.channel.send(message.author.mention, embed=embed, view=view)

                    await view.wait()

                    try:
                        await view.inter.response.defer()
                    except:
                        pass

                    if view.selected == "music":
                        message.content = YOUTUBE_VIDEO_REG.match(message.content).group()

            await self.parse_song_request(message, text_channel, data, response=msg, source=source)

        except GenericError as e:
            error = f"{message.author.mention}. {e}"

        except Exception as e:
            try:
                error_msg, full_error_msg, kill_process, components, mention_author = parse_error(ctx, e)
            except:
                has_exception = e
            else:
                if not error_msg:
                    has_exception = e
                    error = f"{message.author.mention} **Đã xảy ra lỗi khi cố gắng nhận kết quả cho tìm kiếm của bạn:** ```py\n{error_msg}```"
                else:
                    error = f"{message.author.mention}. {error_msg}"

        if error:

            await self.delete_message(message, ignore=data['player_controller']['purge_mode'] != SongRequestPurgeMode.on_message)

            try:
                if msg:
                    await msg.edit(content=error, embed=None, view=None)
                else:
                    await message.channel.send(error, delete_after=9)
            except:
                traceback.print_exc()

        await self.song_request_concurrency.release(message)

        if has_exception and self.bot.config["AUTO_ERROR_REPORT_WEBHOOK"]:

            cog = self.bot.get_cog("ErrorHandler")

            if not cog:
                return

            max_concurrency = cog.webhook_max_concurrency

            await max_concurrency.acquire(message)

            try:
                try:
                    error_msg, full_error_msg, kill_process, components, mention_author = parse_error(message, has_exception)
                except:
                    full_error_msg = has_exception

                embed = disnake.Embed(
                    title="Đã xảy ra lỗi trên máy chủ (yêu cầu bài hát):",
                    timestamp=disnake.utils.utcnow(),
                    description=f"```py\n{repr(has_exception)[:2030].replace(self.bot.http.token, 'mytoken')}```"
                )

                embed.set_footer(
                    text=f"{message.author} [{message.author.id}]",
                    icon_url=message.author.display_avatar.with_static_format("png").url
                )

                embed.add_field(
                    name="Máy chủ:", inline=False,
                    value=f"```\n{disnake.utils.escape_markdown(ctx.guild.name)}\nID: {ctx.guild.id}```"
                )

                embed.add_field(
                    name="Nội dung yêu cầu bài hát:", inline=False,
                    value=f"```\n{message.content}```"
                )

                embed.add_field(
                    name="Kênh văn bản:", inline=False,
                    value=f"```\n{disnake.utils.escape_markdown(ctx.channel.name)}\nID: {ctx.channel.id}```"
                )

                if vc := ctx.author.voice:
                    embed.add_field(
                        name="Kênh thoại (người dùng):", inline=False,
                        value=f"```\n{disnake.utils.escape_markdown(vc.channel.name)}" +
                              (f" ({len(vc.channel.voice_states)}/{vc.channel.user_limit})"
                               if vc.channel.user_limit else "") + f"\nID: {vc.channel.id}```"
                    )

                if vcbot := ctx.guild.me.voice:
                    if vcbot.channel != vc.channel:
                        embed.add_field(
                            name="Kênh thoại (bot):", inline=False,
                            value=f"{vc.channel.name}" +
                                  (f" ({len(vc.channel.voice_states)}/{vc.channel.user_limit})"
                                   if vc.channel.user_limit else "") + f"\nID: {vc.channel.id}```"
                        )

                if ctx.guild.icon:
                    embed.set_thumbnail(url=ctx.guild.icon.with_static_format("png").url)

                await cog.send_webhook(
                    embed=embed,
                    file=string_to_file(full_error_msg, "error_traceback_songrequest.txt")
                )

            except:
                traceback.print_exc()

            await asyncio.sleep(20)

            try:
                await max_concurrency.release(message)
            except:
                pass


    async def process_music(
            self, inter: Union[disnake.Message, disnake.MessageInteraction, disnake.AppCmdInter, CustomContext, disnake.ModalInteraction],
            player: LavalinkPlayer, force_play: str = "no", ephemeral=True, log_text = "", emoji="",
            warn_message: str = "", user_data: dict = None, reg_query: str = None
    ):

        if not player.current:
            if warn_message:
                player.set_command_log(emoji="⚠️", text=warn_message)
            await player.process_next()
        elif force_play == "yes":
            player.set_command_log(
                emoji="▶️",
                text=f"{inter.author.mention} đã thêm bài hát hiện tại để phát ngay lập tức."
            )
            await player.track_end()
            await player.process_next()
        #elif player.current.autoplay:
        #    player.set_command_log(text=log_text, emoji=emoji)
        #    await player.track_end()
        #    await player.process_next()
        else:
            if ephemeral:
                player.set_command_log(text=log_text, emoji=emoji)
            player.update = True

        if reg_query is not None:

            if not user_data:
                user_data = await self.bot.get_global_data(inter.author.id, db_name=DBModel.users)

            try:
                user_data["last_tracks"].remove(reg_query)
            except:
                pass

            if len(user_data["last_tracks"]) > 6:
                user_data["last_tracks"].pop(0)

            user_data["last_tracks"].append(reg_query)

            await self.bot.update_global_data(inter.author.id, user_data, db_name=DBModel.users)

    async def create_player(
            self,
            inter: Union[disnake.Message, disnake.MessageInteraction, disnake.AppCmdInter, CustomContext, disnake.ModalInteraction],
            bot: BotCore, guild: disnake.Guild, guild_data: dict = None, channel = None, message_inter = None,
            node: wavelink.Node = None, modal_message_id: int = None
    ):

        if not guild_data:
            inter, guild_data = await get_inter_guild_data(inter, bot)

        skin = guild_data["player_controller"]["skin"]
        static_skin = guild_data["player_controller"]["static_skin"]
        static_player = guild_data["player_controller"]

        if not channel:
            channel = bot.get_channel(getattr(inter, 'channel_id', inter.channel.id))

        if not node:
            node = await self.get_best_node(bot)

        try:
            global_data = inter.global_guild_data
        except AttributeError:
            global_data = await bot.get_global_data(guild.id, db_name=DBModel.guilds)
            try:
                inter.global_guild_data = global_data
            except:
                pass

        if global_data["global_skin"]:
            skin = global_data["player_skin"] or skin
            static_skin = global_data["player_skin_static"] or guild_data["player_controller"]["static_skin"]

        try:
            invite = global_data["listen_along_invites"][str(inter.channel.id)]
        except KeyError:
            invite = None

        else:

            try:
                invite = (await bot.fetch_invite(invite)).url
            except disnake.NotFound:
                invite = None
            except Exception:
                traceback.print_exc()
                invite = ""

            if invite is None:
                print(
                    f'{"-" * 15}\n'
                    f'Removendo invite: {invite} \n'
                    f'Servidor: {inter.guild.name} [{inter.guild_id}]\n'
                    f'Canal: {inter.channel.name} [{inter.channel.id}]\n'
                    f'{"-" * 15}'
                )
                del global_data["listen_along_invites"][str(inter.channel.id)]
                await self.bot.update_global_data(inter.guild_id, global_data, db_name=DBModel.guilds)

        for n, s in global_data["custom_skins"].items():
            if isinstance(s, str):
                global_data["custom_skins"][n] = pickle.loads(b64decode(s))

        for n, s in global_data["custom_skins_static"].items():
            if isinstance(s, str):
                global_data["custom_skins_static"][n] = pickle.loads(b64decode(s))

        try:
            guild_id =inter.guild.id
        except AttributeError:
            guild_id = inter.guild_id

        player: LavalinkPlayer = bot.music.get_player(
            guild_id=guild_id,
            cls=LavalinkPlayer,
            player_creator=inter.author.id,
            guild=guild,
            channel=channel,
            last_message_id=guild_data['player_controller']['message_id'],
            node_id=node.identifier,
            static=bool(static_player['channel']),
            skin=bot.check_skin(skin),
            skin_static=bot.check_static_skin(static_skin),
            custom_skin_data=global_data["custom_skins"],
            custom_skin_static_data=global_data["custom_skins_static"],
            extra_hints=self.extra_hints,
            restrict_mode=guild_data['enable_restrict_mode'],
            listen_along_invite=invite,
            volume=int(guild_data['default_player_volume']),
            autoplay=guild_data["autoplay"],
            prefix=global_data["prefix"] or bot.default_prefix,
            purge_mode=guild_data['player_controller']['purge_mode'],
            stage_title_template=global_data['voice_channel_status'],
        )

        if static_player['channel']:

            static_channel = bot.get_channel(int(static_player['channel'])) or await bot.fetch_channel(
                int(static_player['channel']))

            allowed_channel = None

            for ch in (static_channel, channel):

                if not ch: continue

                if isinstance(ch, disnake.Thread):
                    channel_check = ch.parent
                else:
                    channel_check = ch

                bot_perms = channel_check.permissions_for(guild.me)

                if bot_perms.read_message_history:
                    allowed_channel = ch
                    break

                elif bot_perms.manage_permissions:
                    overwrites = {
                        guild.me: disnake.PermissionOverwrite(
                            embed_links=True,
                            send_messages=True,
                            send_messages_in_threads=True,
                            read_messages=True,
                            create_public_threads=True,
                            read_message_history=True,
                            manage_messages=True,
                            manage_channels=True,
                            attach_files=True,
                        )
                    }

                    await channel_check.edit(overwrites=overwrites)
                    allowed_channel = ch
                    break

            player.text_channel = allowed_channel

            if not player.message and player.text_channel:
                try:
                    player.message = await player.text_channel.fetch_message(int(static_player['message_id']))
                except TypeError:
                    player.message = None
                except Exception:
                    traceback.print_exc()
                    if hasattr(player.text_channel, 'parent') and isinstance(player.text_channel.parent, disnake.ForumChannel) and str(
                            player.text_channel.id) == static_player['message_id']:
                        pass
                    elif player.static:
                        player.text_channel = None

        if not player.static and player.text_channel:

            if message_inter:
                player.message = message_inter
            elif modal_message_id:
                try:
                    player.message = await inter.channel.fetch_message(modal_message_id)
                except:
                    pass

            if not player.has_thread:
                player.message = None
            else:
                await self.thread_song_request(message_inter.thread, reopen=True, bot=bot)

        try:

            self.bot.log.info(f"{inter.bot.user.name}#{inter.bot.user.discriminator} - Player create at guild: {inter.guild.name}")
        except AttributeError:
            self.bot.log.info(f"Player create at guild: {inter.guild.name}")

        return player


    async def parse_song_request(self, message: disnake.Message, text_channel, data, *, response=None, attachment: disnake.Attachment=None, source=None):

        if not message.author.voice:
            raise GenericError("Bạn phài vào một kênh thoại để yêu cầu bài hát")

        can_connect(
            channel=message.author.voice.channel,
            guild=message.guild,
            check_other_bots_in_vc=data["check_other_bots_in_vc"],
            bot=self.bot
        )

        try:
            if message.guild.me.voice.channel != message.author.voice.channel:
                raise GenericError(
                    f"Bạn phải tham gia kênh <#{message.guild.me.voice.channel.id}> để yêu cầu bài hát.")
        except AttributeError:
            pass

        tracks, node = await self.get_tracks(message.content, message.author, source=source)
        tracks = await self.check_player_queue(message.author, self.bot, message.guild.id, tracks)

        try:
            message_id = int(data['player_controller']['message_id'])
        except TypeError:
            message_id = None

        try:
            player = self.bot.music.players[message.guild.id]
            destroy_message = True
        except KeyError:
            destroy_message = False
            player = await self.create_player(inter=message, bot=self.bot, guild=message.guild, channel=text_channel,
                                              node=node, guild_data=data)

        if not player.message:
            try:
                cached_message = await text_channel.fetch_message(message_id)
            except:
                cached_message = await send_idle_embed(message, bot=self.bot, guild_data=data)
                data['player_controller']['message_id'] = str(cached_message.id)
                await self.bot.update_data(message.guild.id, data, db_name=DBModel.guilds)

            player.message = cached_message

        embed = disnake.Embed(color=self.bot.get_color(message.guild.me))

        try:
            components = [disnake.ui.Button(emoji="🎛️", label="Đi đến giao diện điều khiển người chơi", url=player.message.jump_url)]
        except AttributeError:
            components = []

        if not isinstance(tracks, list):
            player.queue.extend(tracks.tracks)
            if (isinstance(message.channel, disnake.Thread) and
                    (not isinstance(message.channel.parent, disnake.ForumChannel) or
                     data['player_controller']['purge_mode'] != SongRequestPurgeMode.on_message)):
                embed.description = f"✋ **⠂ Yêu cầu bởi:** {message.author.mention}\n" \
                                    f"🎼 **⠂ Các bài hát:** `[{len(tracks.tracks)}]`"
                embed.set_thumbnail(url=tracks.tracks[0].thumb)
                embed.set_author(name="⠂" + fix_characters(tracks.tracks[0].playlist_name, 35), url=message.content,
                                 icon_url=music_source_image(tracks.tracks[0].info["sourceName"]))

                try:
                    embed.description += f"\n🔊 **⠂ Kênh thoại** {message.author.voice.channel.mention}"
                except AttributeError:
                    pass

                try:
                    self.bot.pool.enqueue_playlist_embed_cooldown.get_bucket(message).update_rate_limit()
                except:
                    pass

                components.extend(
                    [
                        disnake.ui.Button(emoji="💗", label="Yêu thích", custom_id=PlayerControls.embed_add_fav),
                        disnake.ui.Button(emoji="<:add_music:588172015760965654>", label="Thêm vào hàng đợi",custom_id=PlayerControls.embed_enqueue_playlist)
                    ]
                )

                if response:
                    await response.edit(content=None, embed=embed, components=components)
                else:
                    await message.reply(embed=embed, fail_if_not_exists=False, mention_author=False)

            elif data['player_controller']['purge_mode'] != SongRequestPurgeMode.on_message:

                txt = f"> 🎼 **⠂** [`{fix_characters(tracks.tracks[0].playlist_name, 35)}`](<{message.content}>) `[{len(tracks.tracks)} Bài hát]` {message.author.mention}"

                try:
                    txt += f" `|` {message.author.voice.channel.mention}"
                except AttributeError:
                    pass

                if response:
                    await response.edit(content=txt, embed=None, components=components)
                else:
                    await message.reply(txt, components=components, allowed_mentions=disnake.AllowedMentions(users=False, everyone=False, roles=False), fail_if_not_exists=False, mention_author=False)

            else:
                player.set_command_log(
                    text=f"{message.author.mention} Đã thêm danh sách phát [`{fix_characters(tracks.data['playlistInfo']['name'], 20)}`]"
                         f"({tracks.tracks[0].playlist_url}) `({len(tracks.tracks)})`.",
                    emoji="🎶"
                )
                if destroy_message:
                    await self.delete_message(message)

        else:
            track = tracks[0]

            if track.info.get("sourceName") == "http":

                if track.title == "Unknown title":
                    if attachment:
                        track.info["title"] = attachment.filename
                    else:
                        track.info["title"] = track.uri.split("/")[-1]
                    track.title = track.info["title"]

                track.uri = ""

            player.queue.append(track)
            if (isinstance(message.channel, disnake.Thread) and
                    (not isinstance(message.channel.parent, disnake.ForumChannel) or
                     data['player_controller']['purge_mode'] != SongRequestPurgeMode.on_message)):
                embed.description = f"💠 **⠂ Người tải lên:** `{track.author}`\n" \
                                    f"✋ **⠂ Yêu cầu bởi:** {message.author.mention}\n" \
                                    f"⏰ **⠂ Thời lượng:** `{time_format(track.duration) if not track.is_stream else '🔴 Livestream'}`"

                try:
                    embed.description += f"\n🔊 **⠂ Kênh thoại:** {message.author.voice.channel.mention}"
                except AttributeError:
                    pass

                try:
                    self.bot.pool.enqueue_track_embed_cooldown.get_bucket(message).update_rate_limit()
                except:
                    pass

                components.extend(
                    [
                        disnake.ui.Button(emoji="💗", label="Yêu thích", custom_id=PlayerControls.embed_add_fav),
                        disnake.ui.Button(emoji="<:play:914841137938829402>", label="Phát Ngay", custom_id=PlayerControls.embed_forceplay),
                        disnake.ui.Button(emoji="<:add_music:588172015760965654>", label="Thêm vào hàng đợi",
                                          custom_id=PlayerControls.embed_enqueue_track)
                    ]
                )

                embed.set_thumbnail(url=track.thumb)
                embed.set_author(name=fix_characters(track.title, 35), url=track.uri or track.search_uri, icon_url=music_source_image(track.info["sourceName"]))
                if response:
                    await response.edit(content=None, embed=embed, components=components)
                else:
                    await message.reply(embed=embed, fail_if_not_exists=False, mention_author=False, components=components)

            elif data['player_controller']['purge_mode'] != SongRequestPurgeMode.on_message:

                txt = f"> 🎵 **⠂** [`{fix_characters(track.title, 35)}`](<{track.uri}>) `[{time_format(track.duration) if not track.is_stream else '🔴 Livestream'}]` {message.author.mention}"

                try:
                    txt += f" `|` {message.author.voice.channel.mention}"
                except AttributeError:
                    pass

                if response:
                    await response.edit(content=txt, embed=None, components=components)
                else:
                    await message.reply(txt, components=components, allowed_mentions=disnake.AllowedMentions(users=False, everyone=False, roles=False), fail_if_not_exists=False, mention_author=False)

            else:
                duration = time_format(tracks[0].duration) if not tracks[0].is_stream else '🔴 Livestream'
                player.set_command_log(
                    text=f"{message.author.mention} thêm [`{fix_characters(tracks[0].title, 20)}`]({tracks[0].uri or tracks[0].search_uri}) `({duration})`.",
                    emoji="🎵"
                )
                if destroy_message:
                    await self.delete_message(message, ignore=data['player_controller']['purge_mode'] != SongRequestPurgeMode.on_message)

        if not player.is_connected:
            await self.do_connect(
                message,
                channel=message.author.voice.channel,
                check_other_bots_in_vc=data["check_other_bots_in_vc"]
            )

        if not player.current:
            await player.process_next()
        else:
            await player.update_message()

        await asyncio.sleep(1)

    async def cog_check(self, ctx: CustomContext) -> bool:

        return await check_requester_channel(ctx)

    def cog_unload(self):
        try:
            self.error_report_task.cancel()
        except:
            pass


    async def interaction_message(self, inter: Union[disnake.Interaction, CustomContext], txt, emoji: str = "✅",
                                  rpc_update: bool = False, data: dict = None, store_embed: bool = False, force=False,
                                  defered=False, thumb=None):

        try:
            txt, txt_ephemeral = txt
        except:
            txt_ephemeral = False

        try:
            bot = inter.music_bot
            guild = inter.music_guild
        except AttributeError:
            bot = inter.bot
            guild = inter.guild

        player: LavalinkPlayer = bot.music.players[inter.guild_id]

        component_interaction = isinstance(inter, disnake.MessageInteraction)

        ephemeral = await self.is_request_channel(inter, data=data)

        if ephemeral:
            player.set_command_log(text=f"{inter.author.mention} {txt}", emoji=emoji)
            player.update = True

        await player.update_message(interaction=inter if (bot.user.id == self.bot.user.id and component_interaction) \
            else False, rpc_update=rpc_update, force=force)

        if isinstance(inter, CustomContext):
            embed = disnake.Embed(color=self.bot.get_color(guild.me),
                                  description=f"{txt_ephemeral or txt}{player.controller_link}")

            if thumb:
                embed.set_thumbnail(url=thumb)

            try:
                if bot.user.id != self.bot.user.id:
                    embed.set_footer(text=f"Thông qua: {bot.user.display_name}", icon_url=bot.user.display_avatar.url)
            except AttributeError:
                pass

            if store_embed and not player.controller_mode and len(player.queue) > 0:
                player.temp_embed = embed

            else:
                try:
                    await inter.store_message.edit(embed=embed, view=None, content=None)
                except AttributeError:
                    await inter.send(embed=embed)

        elif not component_interaction:

            embed = disnake.Embed(
                color=self.bot.get_color(guild.me),
                description=(txt_ephemeral or f"{inter.author.mention} **{txt}**") + player.controller_link
            )

            if thumb:
                embed.set_thumbnail(url=thumb)

            try:
                if bot.user.id != self.bot.user.id:
                    embed.set_footer(text=f"Thông qua bot: {bot.user.display_name}", icon_url=bot.user.display_avatar.url)
            except AttributeError:
                pass

            if not inter.response.is_done():
                await inter.send(embed=embed, ephemeral=ephemeral)

            elif defered:
                await inter.edit_original_response(embed=embed)

    async def process_nodes(self, data: dict, start_local: bool = False):

        await self.bot.wait_until_ready()

        if str(self.bot.user.id) in self.bot.config["INTERACTION_BOTS_CONTROLLER"]:
            return

        for k, v in data.items():
            self.bot.loop.create_task(self.connect_node(v))

        if start_local:
            await self.connect_local_lavalink()

    @commands.Cog.listener("on_wavelink_node_connection_closed")
    async def node_connection_closed(self, node: wavelink.Node):

        retries = 0
        backoff = 7

        print(Fore.RED + f"❌ {self.bot.user} - [{node.identifier} / v{node.version}] Lost connection - reconnecting in {int(backoff)} seconds." + Style.RESET_ALL)

        for player in list(node.players.values()):

            try:
                player._new_node_task.cancel()
            except:
                pass

            player._new_node_task = player.bot.loop.create_task(player._wait_for_new_node())

        await asyncio.sleep(2)

        while True:

            if node.is_available:
                return

            if self.bot.config["LAVALINK_RECONNECT_RETRIES"] and retries == self.bot.config["LAVALINK_RECONNECT_RETRIES"]:
                print(f"❌ {self.bot.user} - [{node.identifier}] All attempts to reconnect failed...")
                return

            await self.bot.wait_until_ready()

            error = None

            try:
                async with aiohttp.ClientSession().get(f"{node.rest_uri}/v4/info", timeout=45, headers=node.headers) as r:
                    if r.status == 200:
                        node.version = 4
                        node.info = await r.json()
                    elif r.status != 404:
                        raise Exception(f"{self.bot.user} - [{r.status}]: {await r.text()}"[:300])
                    else:
                        node.version = 3
                    await node.connect()
                    return
            except Exception as e:
                error = repr(e)

            backoff *= 1.5
            self.bot.log.error(
                f' {self.bot.user} - Failed to reconnect to server [{node.identifier}] retry {int(backoff)}'
                f' sec. Erro: {error}'[:300])
            await asyncio.sleep(backoff)
            retries += 1

    @commands.Cog.listener("on_wavelink_node_ready")
    async def node_ready(self, node: wavelink.Node):
        self.bot.log.info(f'{self.bot.user} - Music server: [{node.identifier} / v{node.version}] is ready to use!')
        retries = 25
        while retries > 0:

            if not node._websocket.is_connected:
                return

            if not node.stats:
                await asyncio.sleep(5)
                retries -= 1
                continue

            if node.stats.uptime < 600000:
                node.open()
            return

    async def connect_node(self, data: dict):

        if data["identifier"] in self.bot.music.nodes:
            node = self.bot.music.nodes[data['identifier']]
            if not node.is_connected:
                await node.connect()
            return

        data = deepcopy(data)

        data['rest_uri'] = ("https" if data.get('secure') else "http") + f"://{data['host']}:{data['port']}"
        data['user_agent'] = self.bot.pool.current_useragent
        search = data.pop("search", True)
        node_website = data.pop('website', '')
        region = data.pop('region', 'unknown')
        heartbeat = int(data.pop('heartbeat', 30))
        search_providers = data.pop("search_providers", [self.bot.pool.config["DEFAULT_SEARCH_PROVIDER"]] + [s for s in ("ytsearch", "scsearch") if s != self.bot.pool.config["DEFAULT_SEARCH_PROVIDER"]])
        retry_403 = data.pop('retry_403', False)
        info = None

        try:
            max_retries = int(data.pop('retries'))
        except (TypeError, KeyError):
            max_retries = 0

        headers = {'Authorization': data['password']}

        if max_retries:

            backoff = 9
            retries = 1
            exception = None

            self.bot.log.info( f"🔰 {self.bot.user} - The music server starts: {data['identifier']}")

            while not self.bot.is_closed():
                if retries >= max_retries:
                    self.bot.log.error(
                        f" {self.bot.user} - All attempts to connect to server [{data['identifier']}] failed.")
                    return
                else:
                    await asyncio.sleep(backoff)
                    try:
                        async with aiohttp.ClientSession().get(f"{data['rest_uri']}/v4/info", timeout=45, headers=headers) as r:
                            if r.status == 200:
                                info = await r.json()
                                data["version"] = 4
                            elif r.status != 404:
                                raise Exception(f"{self.bot.user} - [{r.status}]: {await r.text()}"[:300])
                            break
                    except Exception as e:
                        exception = e
                        if data["identifier"] != "LOCAL":
                            self.bot.log.warning(f' {self.bot.user} - Failed to connect to server [{data["identifier"]}], '
                                   f'retry [{retries}/{max_retries}] in {backoff} seconds.')
                        backoff += 2
                        retries += 1
                        continue

        else:
            try:
                async with aiohttp.ClientSession().get(f"{data['rest_uri']}/v4/info", timeout=45, headers=headers) as r:
                    if r.status == 200:
                        data["version"] = 4
                        info = await r.json()
                    elif r.status != 404:
                        raise Exception(f"{self.bot.user} - [{r.status}]: {await r.text()}"[:300])
            except Exception as e:
                self.bot.log.error(f"Failed to connect to the server {data['identifier']}")
                return

        data["identifier"] = data["identifier"].replace(" ", "_")
        node = await self.bot.music.initiate_node(auto_reconnect=False, region=region, heartbeat=heartbeat, **data)
        node.info = info
        node.search = search
        node.website = node_website
        node.retry_403 = retry_403
        node.search_providers = search_providers

    async def get_tracks(
            self, query: str, user: disnake.Member, node: wavelink.Node = None,
            track_loops=0, use_cache=True, source=None, bot: BotCore = None):

        if not bot:
            bot = self.bot

        if not node:
            node = await self.get_best_node(bot)

        tracks = await process_spotify(self.bot, user.id, query)

        exceptions = set()

        if not tracks:

            if use_cache:
                try:
                    cached_tracks = self.bot.pool.playlist_cache[query]
                except KeyError:
                    pass
                else:

                    tracks = LavalinkPlaylist(
                        {
                            'loadType': 'PLAYLIST_LOADED',
                            'playlistInfo': {
                                'name': cached_tracks[0]["info"]["extra"]["playlist"]["name"],
                                'selectedTrack': -1
                            },
                            'tracks': cached_tracks
                        },
                        requester=user.id,
                        url=cached_tracks[0]["info"]["extra"]["playlist"]["url"]
                    )

            if not tracks:

                if node.search:
                    node_search = node
                else:
                    try:
                        node_search = \
                            sorted(
                                [n for n in bot.music.nodes.values() if n.search and n.available and n.is_available],
                                key=lambda n: len(n.players))[0]
                    except IndexError:
                        node_search = node

                if source is False:
                    providers = [node.search_providers[:1]]
                elif source:
                    providers = [s for s in (node.search_providers or [self.bot.config["DEFAULT_SEARCH_PROVIDER"]]) if s != source]
                    providers.insert(0, source)
                else:
                    source = True
                    providers = node.search_providers or [self.bot.config["DEFAULT_SEARCH_PROVIDER"]]

                for search_provider in providers:

                    search_query = f"{search_provider}:{query}" if source else query

                    try:
                        tracks = await node_search.get_tracks(
                            search_query, track_cls=LavalinkTrack, playlist_cls=LavalinkPlaylist, requester=user.id
                        )
                    except ClientConnectorCertificateError:
                        node_search.available = False

                        for n in self.bot.music.nodes.values():

                            if not n.available or not n.is_available:
                                continue

                            try:
                                tracks = await n.get_tracks(
                                    search_query, track_cls=LavalinkTrack, playlist_cls=LavalinkPlaylist, requester=user.id
                                )
                                node_search = n
                                break
                            except ClientConnectorCertificateError:
                                n.available = False
                                continue

                        if not node_search:
                            raise GenericError("**Không có máy chủ âm nhạc có sẵn.**")

                    except Exception as e:
                        logging.error(f"Xảy ra sự cố trong lúc tìm kiếm...\n{query}\n{traceback.format_exc()}")
                        exceptions.add(repr(e))

                    if tracks or not source:
                        break

        if not tracks:
            if exceptions:
                if "This playlist type is unviewable." in exceptions:
                    raise GenericError("**Đã xảy ra sự cố khi tìm kiếm kết quả cho bài hát của bạn:**\n ```Danh sách phát này không thể xem được :<```")
                else:
                    raise GenericError("**Đã xảy ra sự cố khi tìm kiếm kết quả cho bài hát của bạn**")
            raise GenericError("Không có kết quả cho tìm kiếm của bạn")

        if isinstance(tracks, list):
            tracks[0].info["extra"]["track_loops"] = track_loops

        else:

            if (selected := tracks.data['playlistInfo']['selectedTrack']) > 0:
                tracks.tracks = tracks.tracks[selected:] + tracks.tracks[:selected]

        return tracks, node

    async def connect_local_lavalink(self):

        if 'LOCAL' not in self.bot.music.nodes:

            localnode = {
                'host': '127.0.0.1',
                'port':  8090,
                'password': 'youshallnotpass',
                'identifier': 'LOCAL',
                'region': 'us_central',
                'retries': 120,
                'retry_403': True,
            }

            self.bot.loop.create_task(self.connect_node(localnode))

    @commands.Cog.listener("on_thread_create")
    async def thread_song_request(self, thread: disnake.Thread, reopen: bool = False, bot: BotCore = None):

        if not bot:
            bot=self.bot

        try:
            player: LavalinkPlayer = bot.music.players[thread.guild.id]
        except KeyError:
            return

        if player.static or player.message.id != thread.id:
            return

        if not thread.parent.permissions_for(thread.guild.me).send_messages_in_threads:
            await player.text_channel.send(
                embed=disnake.Embed(
                    color=self.bot.get_color(thread.guild.me),
                    description="**Tôi không có quyền gửi tin nhắn trong các cuộc trò chuyện trên kênh hiện tại để kích hoạt"
                                 "hệ thống yêu cầu bài hát...**\n\n"
                                 f"Các tin nhắn được gửi trong chuỗi {thread.mention} sẽ bị bỏ qua."
                ), delete_after=30
            )
            return

        embed = disnake.Embed(color=bot.get_color(thread.guild.me))

        if not self.bot.intents.message_content:
            embed.description = "**Để ý! Tôi không có mục đích message_content được nhà phát triển của tôi kích hoạt...\n" \
                                 "Chức năng yêu cầu âm nhạc ở đây có thể không mang lại kết quả như mong đợi...**"

        elif not player.controller_mode:
            embed.description = "**Giao diện/giao diện hiện tại không tương thích với hệ thống yêu cầu bài hát " \
                                "qua chủ đề/cuộc trò chuyện\n\n" \
                                "Lưu ý:** `Hệ thống này yêu cầu giao diện sử dụng các nút.`"

        else:
            if reopen:
                embed.description = "**Phần yêu cầu bài hát trong chủ đề này đã được mở lại trong chủ đề hiện tại.**"
            else:
                embed.description = "**Cuộc trò chuyện này sẽ tạm thời được sử dụng để yêu cầu bài hát.**\n\n" \
                                     "**Yêu cầu bài hát của bạn tại đây bằng cách gửi tên bài hát hoặc liên kết bài hát/video" \
                                     "đó là từ một trong những nền tảng được hỗ trợ sau:** " \
                                    "```ansi\n[31;1mYoutube[0m, [33;1mSoundcloud[0m, [32;1mSpotify[0m, [34;1mTwitch[0m```"

        await thread.send(embed=embed)

    @commands.Cog.listener("on_voice_state_update")
    async def player_vc_disconnect(
            self,
            member: disnake.Member,
            before: disnake.VoiceState,
            after: disnake.VoiceState
    ):
        try:
            player: LavalinkPlayer = self.bot.music.players[member.guild.id]
        except KeyError:
            return

        if member.bot:
          
            if player.bot.user.id == member.id and not after.channel and not player.is_closing:

                last_channel_id = int(player.last_channel.id)

                await asyncio.sleep(3)

                vc = self.bot.get_channel(last_channel_id)

                if not vc:

                    msg = "Kênh thoại đã bị xóa..."

                    if player.static:
                        player.set_command_log(msg)
                        await player.playerdestroy()

                    else:
                        embed = disnake.Embed(
                            description=msg,
                            color=self.bot.get_color(member))
                        try:
                            self.bot.loop.create_task(player.text_channel.send(embed=embed, delete_after=7))
                        except:
                            traceback.print_exc()
                        await player.playerdestroy()

                else:
                    while True:

                        try:
                            player = self.bot.music.players[member.guild.id]
                        except KeyError:
                            return

                        if player.guild.me.voice:
                            if isinstance(before.channel, disnake.StageChannel) \
                                    and member not in before.channel.speakers \
                                    and before.channel.permissions_for(member).manage_permissions:
                                try:
                                    await member.guild.me.edit(suppress=False)
                                except Exception:
                                    traceback.print_exc()
                            return

                        if player.is_closing:
                            return

                        if not player._new_node_task:

                            try:
                                can_connect(before.channel, player.guild, bot=player.bot)
                            except Exception as e:
                                player.set_command_log(f"Trình phát đã bị tắt vì đã xảy ra lỗi: {e}")
                                await player.playerdestroy()
                                return

                            try:
                                await player.connect(vc.id)
                                player.set_command_log(text="Tui thấy bạn đang cố ngắt kết nối tui khỏi kênh. "
                                                            "Hãy ngắt kết nối tui khỏi kênh bằng lệnh/nút: **stop**.",
                                                       emoji="⚠️")
                                player.update = True
                                await asyncio.sleep(5)
                                continue
                            except Exception:
                                traceback.print_exc()

                        await asyncio.sleep(30)
            return

        if before.channel == after.channel:
            try:
                vc = player.guild.me.voice.channel
            except AttributeError:
                pass
            else:
                try:
                    player.members_timeout_task.cancel()
                except:
                    pass
                try:
                    check = (m for m in vc.members if not m.bot and not (m.voice.deaf or m.voice.self_deaf))
                except:
                    check = None
                player.members_timeout_task = player.bot.loop.create_task(player.members_timeout(check=bool(check)))
            return

        try:
            player.members_timeout_task.cancel()
            player.members_timeout_task = None
        except AttributeError:
            pass

        if member.id == player.bot.user.id:

            for b in self.bot.pool.bots:
                if b == player.bot:
                    continue
                try:
                    try:
                        after.channel.voice_states[b.user.id]
                    except KeyError:
                        continue
                    if before.channel.permissions_for(member.guild.me).connect:
                        await asyncio.sleep(1)
                        await player.guild.voice_client.move_to(before.channel)
                    else:
                        player.set_command_log(text="Trình phát đã bị tắt vì bạn đã chuyển tôi sang kênh "
                                                     f"{after.channel.mention} trong đó bot {b.user.mention} "
                                                     "nó cũng được kết nối tạo ra sự không tương thích với "
                                                     "hệ thống đa giọng nói của tôi.", emoji="⚠️")
                        await player.playerdestroy()
                    return
                except AttributeError:
                    pass
                except Exception:
                    traceback.print_exc()

            if member.guild.voice_client and after.channel:
                player.guild.voice_client.channel = after.channel
                player.last_channel = after.channel

        try:
            check = [m for m in player.guild.me.voice.channel.members if not m.bot and not (m.voice.deaf or m.voice.self_deaf)]
        except:
            check = None

        if player.stage_title_event and member.bot and not player.is_closing:

            try:
                if isinstance(before.channel, disnake.StageChannel):

                    if before.channel.instance and member not in before.channel.members:
                        try:
                            await before.channel.instance.edit(topic="Tự động cập nhật bị vô hiệu :<")
                        except:
                            traceback.print_exc()
                        player.stage_title_event = False

                else:
                    if isinstance(before.channel, disnake.VoiceChannel) and member not in before.channel.members:
                        player.stage_title_event = False
                        if player.last_stage_title:
                            self.bot.loop.create_task(player.bot.edit_voice_channel_status(status=None, channel_id=before.channel.id))
            except Exception:
                traceback.print_exc()

        if member.bot and isinstance(after.channel, disnake.StageChannel) and after.channel.permissions_for(member).manage_permissions:
            await asyncio.sleep(1.5)
            if member not in after.channel.speakers:
                try:
                    await member.guild.me.edit(suppress=False)
                except Exception:
                    traceback.print_exc()


        if check:
            try:
                player.auto_skip_track_task.cancel()
            except AttributeError:
                pass
            player.auto_skip_track_task = None

        player.members_timeout_task = player.bot.loop.create_task(player.members_timeout(check=bool(check)))
        
        if not member.guild.me.voice:
            await asyncio.sleep(1)
            if not player.is_closing and not player._new_node_task:
                try:
                    await player.playerdestroy(force=True)
                except Exception:
                    traceback.print_exc()

        if player.auto_pause:
            return

        if player.is_closing or (member.bot and not before.channel):
            return

        channels = set()

        try:
            channels.add(before.channel.id)
        except:
            pass

        try:
            channels.add(after.channel.id)
        except:
            pass

        try:
            try:
                vc = player.guild.me.voice.channel
            except AttributeError:
                vc = player.last_channel

            if vc.id not in channels:
                return
        except AttributeError:
            pass

        if not after or before.channel != after.channel:

            try:
                vc = player.guild.me.voice.channel
            except AttributeError:
                vc = before.channel

            if vc:

                try:
                    await player.process_rpc(vc, users=[member.id], close=not player.guild.me.voice or after.channel != player.guild.me.voice.channel, wait=True)
                except AttributeError:
                    traceback.print_exc()
                    pass

                await player.process_rpc(vc, users=[m for m in vc.voice_states if (m != member.id)])

    async def reset_controller_db(self, guild_id: int, data: dict, inter: disnake.AppCmdInter = None):

        try:
            bot = inter.music_bot
        except AttributeError:
            bot = inter.bot

        data['player_controller']['channel'] = None
        data['player_controller']['message_id'] = None

        try:
            player: LavalinkPlayer = bot.music.players[guild_id]
        except KeyError:
            return

        player.static = False

        try:
            if isinstance(inter.channel.parent, disnake.TextChannel):
                player.text_channel = inter.channel.parent
            else:
                player.text_channel = inter.channel
        except AttributeError:
            player.text_channel = inter.channel

        try:
            await bot.update_data(guild_id, data, db_name=DBModel.guilds)
        except Exception:
            traceback.print_exc()

    async def get_best_node(self, bot: BotCore = None):

        if not bot:
            bot = self.bot

        try:
            return sorted(
                [n for n in bot.music.nodes.values() if n.stats and n.is_available and n.available],
                key=lambda n: n.stats.players
            )[0]

        except IndexError:
            try:
                node = bot.music.nodes['LOCAL']
            except KeyError:
                pass
            else:
                if not node._websocket.is_connected:
                    await node.connect()
                return node

            raise GenericError("**Không có máy chủ âm nhạc.**")

    async def error_report_loop(self):

        while True:

            data = await self.error_report_queue.get()

            async with aiohttp.ClientSession() as session:
                webhook = disnake.Webhook.from_url(self.bot.config["AUTO_ERROR_REPORT_WEBHOOK"], session=session)
                await webhook.send(username=self.bot.user.display_name + "Music Error log", avatar_url=self.bot.user.display_avatar.url, **data)

            await asyncio.sleep(15)


def setup(bot: BotCore):

    if bot.config["USE_YTDL"] and not hasattr(bot.pool, 'ytdl'):

        from yt_dlp import YoutubeDL

        bot.pool.ytdl = YoutubeDL(
            {
                'extract_flat': True,
                'quiet': True,
                'no_warnings': True,
                'lazy_playlist': True,
                'simulate': True,
                'cachedir': "./.ytdl_cache",
                'allowed_extractors': [
                    r'.*youtube.*',
                    r'.*soundcloud.*',
                ],
                'extractor_args': {
                    'youtube': {
                        'skip': [
                            'hls',
                            'dash',
                            'translated_subs'
                        ],
                        'player_skip': [
                            'js',
                            'configs',
                            'webpage'
                        ],
                        'player_client': ['android_creator'],
                        'max_comments': [0],
                    },
                    'youtubetab': {
                        "skip": ["webpage"]
                    }
                }
            }
        )

    bot.add_cog(Music(bot))
