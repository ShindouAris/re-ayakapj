# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import datetime
import os
import pickle
import random
import string
import traceback
from base64 import b64decode
from typing import TYPE_CHECKING, Union, Optional

import disnake
import humanize
from disnake.ext import commands

from utils.db import DBModel
from utils.music.converters import perms_translations, time_format
from utils.music.errors import GenericError, NoVoice
from utils.music.interactions import SkinEditorMenu
from utils.music.models import LavalinkPlayer
from utils.others import send_idle_embed, CustomContext, select_bot_pool, pool_command, CommandArgparse, \
    SongRequestPurgeMode, update_inter, get_inter_guild_data

if TYPE_CHECKING:
    from utils.client import BotCore

channel_perms = ("send_messages", "embed_links", "read_messages")

thread_perms = ("send_messages_in_threads", "embed_links", "read_messages")

forum_perms = ("create_forum_threads", "send_messages_in_threads", "read_messages", "embed_links")

def check_channel_perm(channel: Union[disnake.StageChannel, disnake.VoiceChannel, disnake.ForumChannel, disnake.TextChannel]):

    if isinstance(channel, disnake.ForumChannel):
        missing_perms = [p for p, v in channel.permissions_for(channel.guild.me) if p in forum_perms and not v]
    elif isinstance(channel, disnake.Thread):
        missing_perms = [p for p,v in channel.parent.permissions_for(channel.guild.me) if p in thread_perms and not v]
    else:
        missing_perms = [p for p, v in channel.permissions_for(channel.guild.me) if p in channel_perms and not v]

    if missing_perms:
        raise GenericError(
            f"**{channel.guild.me.mention} không có những quyền cần thiết sau đây trên kênh {channel.mention}** ```ansi\n" +
            "\n".join(f"[0;33m{perms_translations.get(p, p)}[0m" for p in missing_perms) + "```")


class SkinSelector(disnake.ui.View):

    def __init__(
            self,
            ctx: Union[disnake.AppCmdInter, CustomContext],
            embed: disnake.Embed,
            select_opts: list,
            static_select_opts: list,
            global_select_opts: list = None,
            global_static_select_opts: list = None,
            global_mode=False,
    ):
        super().__init__(timeout=180)
        self.ctx = ctx
        self.interaction: Optional[disnake.MessageInteraction] = None
        self.global_mode = global_mode
        self.skin_selected = None
        self.static_skin_selected = None
        self.select_opts = select_opts
        self.static_select_opts = static_select_opts
        self.global_select_opts = global_select_opts
        self.global_static_select_opts = global_static_select_opts
        self.embed = embed

        if not global_mode:
            self.skin_selected = [s.value for s in select_opts if s.default][0]
            self.static_skin_selected = [s.value for s in static_select_opts if s.default][0]
        else:
            try:
                self.skin_selected = [s.value for s in global_select_opts if s.default][0]
            except IndexError:
                self.skin_selected = self.ctx.bot.default_skin
            try:
                self.static_skin_selected = [s.value for s in global_static_select_opts if s.default][0]
            except IndexError:
                self.static_skin_selected = self.ctx.bot.default_static_skin

        self.rebuild_selects()

    def rebuild_selects(self):

        self.clear_items()

        if not self.global_mode:
            self.embed.title = "Bộ chọn da (áp dụng cho bot đã chọn)"

            for s in self.select_opts:
                s.default = self.skin_selected == s.value

            for s in self.static_select_opts:
                s.default = self.static_skin_selected == s.value

            select_opts = self.select_opts
            static_select_opts = self.static_select_opts

        else:
            self.embed.title = "Bộ chọn da (áp dụng cho tất cả các bot máy chủ)"

            for s in self.global_select_opts:
                s.default = self.skin_selected == s.value

            for s in self.global_static_select_opts:
                s.default = self.static_skin_selected == s.value

            select_opts = self.global_select_opts
            static_select_opts = self.global_static_select_opts

        select_opts = disnake.ui.Select(options=select_opts, min_values=1, max_values=1)
        select_opts.callback = self.skin_callback
        self.add_item(select_opts)

        static_select_opts = disnake.ui.Select(options=static_select_opts, min_values=1, max_values=1)
        static_select_opts.callback = self.static_skin_callback
        self.add_item(static_select_opts)

        global_mode = disnake.ui.Button(label=("Vô hiệu hóa" if self.global_mode else "Cho phép") + " mode Global ", emoji="🌐")
        global_mode.callback = self.mode_callback
        self.add_item(global_mode)

        confirm_button = disnake.ui.Button(label="Lưu", emoji="💾")
        confirm_button.callback = self.confirm_callback
        self.add_item(confirm_button)

        cancel_button = disnake.ui.Button(label="Hủy", emoji="❌")
        cancel_button.callback = self.stop_callback
        self.add_item(cancel_button)

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:

        if inter.author.id == self.ctx.author.id:
            return True

        await inter.send(f"Chỉ {self.ctx.author.mention} mới có thể tương tác ở đây!", ephemeral=True)
        return False

    async def skin_callback(self, inter: disnake.MessageInteraction):
        self.skin_selected = inter.data.values[0]
        self.rebuild_selects()
        await inter.response.edit_message(view=self)

    async def static_skin_callback(self, inter: disnake.MessageInteraction):
        self.static_skin_selected = inter.data.values[0]
        self.rebuild_selects()
        await inter.response.edit_message(view=self)

    async def mode_callback(self, inter: disnake.MessageInteraction):
        self.global_mode = not self.global_mode
        self.rebuild_selects()
        await inter.response.edit_message(view=self, embed=self.embed)

    async def confirm_callback(self, inter: disnake.MessageInteraction):
        self.interaction = inter
        self.stop()

    async def stop_callback(self, inter: disnake.MessageInteraction):
        self.interaction = inter
        self.skin_selected = None
        self.stop()


class PlayerSettings(disnake.ui.View):

    def __init__(self, ctx: Union[disnake.AppCmdInter, CustomContext], bot: BotCore, data: dict):
        super().__init__()
        self.ctx = ctx
        self.bot = bot
        self.enable_autoplay = data["autoplay"]
        self.check_other_bots_in_vc = data['check_other_bots_in_vc']
        self.enable_restrict_mode = data['enable_restrict_mode']
        self.default_player_volume = data['default_player_volume']
        self.player_purge_mode = data["player_controller"]["purge_mode"]
        self.message: Optional[disnake.Message] = None
        self.load_buttons()

    def load_buttons(self):

        self.clear_items()

        player_purge_select = disnake.ui.Select(
            placeholder="Chọn chế độ yêu cầu bài hát.",
            options=[
                disnake.SelectOption(
                    label="Xóa sau khi gửi tin nhắn",
                    description="Xóa khi gửi tin nhắn trên kênh yêu cầu bài hát",
                    value=SongRequestPurgeMode.on_message,
                    default=SongRequestPurgeMode.on_message == self.player_purge_mode
                ),
                disnake.SelectOption(
                    label="Xóa khi dừng trình phát",
                    description="Xóa khi bot được dừng",
                    value=SongRequestPurgeMode.on_player_stop,
                    default=SongRequestPurgeMode.on_player_stop == self.player_purge_mode
                ),
                disnake.SelectOption(
                    label="Xóa khi bắt đầu phát",
                    description="Xóa tin nhắn yêu cầu bài hát khi bắt đầu phát",
                    value=SongRequestPurgeMode.on_player_start,
                    default=SongRequestPurgeMode.on_player_start == self.player_purge_mode
                ),
                disnake.SelectOption(
                    label="Không xóa tin nhắn",
                    description="Duy trì tin nhắn được gửi trên kênh yêu cầu bài hát",
                    value=SongRequestPurgeMode.no_purge,
                    default=SongRequestPurgeMode.no_purge == self.player_purge_mode
                ),
            ]
        )

        player_purge_select.callback = self.purge_mode_callback
        self.add_item(player_purge_select)

        player_volume_select = disnake.ui.Select(
            placeholder="Chọn một âm lượng mặc định.",
            options=[
                        disnake.SelectOption(label=f"Âm lượng tiêu chuẩn: {i}", default=i == self.default_player_volume,
                                             value=str(i)) for i in range(5, 101, 5)
                    ] + [
                disnake.SelectOption(label=f"Âm lượng tiêu chuẩn: {i}", default=i == self.default_player_volume,
                                     description="Lưu ý: Trên 100% âm thanh có thể trở nên không ổn định.",
                                     value=str(i)) for i in range(110, 151, 10)
            ]
        )

        player_volume_select.callback = self.volume_callback
        self.add_item(player_volume_select)

        check_other_bots_button = disnake.ui.Button(label="Không kết nối với bot không tương thích.",
                                                    emoji="✅" if self.check_other_bots_in_vc else "🚫")
        check_other_bots_button.callback = self.check_other_bots_callback
        self.add_item(check_other_bots_button)

        restrict_mode_button = disnake.ui.Button(label="Hạn chế",
                                                    emoji="✅" if self.enable_restrict_mode else "🚫")
        restrict_mode_button.callback = self.restrict_mode_callback
        self.add_item(restrict_mode_button)

        check_autoplay_button = disnake.ui.Button(label="Autoplay.",
                                                    emoji="✅" if self.enable_autoplay else "🚫")
        check_autoplay_button.callback = self.autoplay_callback
        self.add_item(check_autoplay_button)

        close_button = disnake.ui.Button(label="Lưu/đóng", emoji="💾")
        close_button.callback = self.close_callback
        self.add_item(close_button)

    async def check_other_bots_callback(self, interaction: disnake.MessageInteraction):
        self.check_other_bots_in_vc = not self.check_other_bots_in_vc
        self.load_buttons()
        await interaction.response.edit_message(view=self)

    async def restrict_mode_callback(self, interaction: disnake.MessageInteraction):
        self.enable_restrict_mode = not self.enable_restrict_mode
        self.load_buttons()
        await interaction.response.edit_message(view=self)

    async def volume_callback(self, interaction: disnake.MessageInteraction):
        self.default_player_volume = int(interaction.data.values[0])
        self.load_buttons()
        await interaction.response.edit_message(view=self)

    async def purge_mode_callback(self, interaction: disnake.MessageInteraction):
        self.player_purge_mode = interaction.data.values[0]
        self.load_buttons()
        await interaction.response.edit_message(view=self)

    async def autoplay_callback(self, interaction: disnake.MessageInteraction):
        self.enable_autoplay = not self.enable_autoplay
        self.load_buttons()
        await interaction.response.edit_message(view=self)

    async def close_callback(self, interaction: disnake.MessageInteraction):

        try:
            if isinstance(self.ctx, CustomContext):
                await self.message.edit(content="Thay đổi đã được lưu thành công!", view=None, embed=None)
            else:
                await self.ctx.edit_original_message(content="Thay đổi đã được lưu thành công", view=None, embed=None)
        except Exception:
            traceback.print_exc()
        await self.save_data()
        self.stop()

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:

        if inter.author.id == self.ctx.author.id:
            return True

        await inter.send(f"Chỉ {self.ctx.author.mention} mới có thể tương tác ở đây!", ephemeral=True)
        return False

    async def save_data(self):
        guild_data = await self.bot.get_data(self.ctx.guild_id, db_name=DBModel.guilds)
        guild_data['autoplay'] = self.enable_autoplay
        guild_data['check_other_bots_in_vc'] = self.check_other_bots_in_vc
        guild_data['enable_restrict_mode'] = self.enable_restrict_mode
        guild_data['default_player_volume'] = int(self.default_player_volume)
        guild_data['player_controller']['purge_mode'] = self.player_purge_mode

        await self.bot.update_data(self.ctx.guild_id, guild_data, db_name=DBModel.guilds)

        try:
            player: LavalinkPlayer = self.bot.music.players[self.ctx.guild_id]
        except KeyError:
            pass
        else:
            player.purge_mode = self.player_purge_mode
            await player.process_save_queue()

    async def on_timeout(self):

        if isinstance(self.ctx, CustomContext):
            await self.message.edit(
                embed=disnake.Embed(description="**Đã hết thời gian...**", color=self.bot.get_color()), view=None
            )
        else:
            await self.ctx.edit_original_message(
                embed=disnake.Embed(description="**Đã hết thời gian...**", color=self.bot.get_color()), view=None
            )

        await self.save_data()

        self.stop()


class MusicSettings(commands.Cog):

    emoji = "🔧"
    name = "Cài đặt"
    desc_prefix = f"[{emoji} {name}] | "

    def __init__(self, bot: BotCore):
        self.bot = bot
        self.invite_cooldown = commands.CooldownMapping.from_cooldown(rate=1, per=45, type=commands.BucketType.guild)

    player_settings_cd = commands.CooldownMapping.from_cooldown(1, 5, commands.BucketType.guild)
    player_settings_mc = commands.MaxConcurrency(1, per=commands.BucketType.guild, wait=False)

    @commands.has_guild_permissions(manage_guild=True)
    @commands.command(
        name="playersettings", aliases=["ps", "settings"],
        description="Thay đổi một số cài đặt tiêu chuẩn của người chơi.",
        cooldown=player_settings_cd, max_concurrency=player_settings_mc
    )
    async def player_settings_legacy(self, ctx: CustomContext):
        await self.player_settings.callback(self=self, interaction=ctx)

    @commands.slash_command(
        description=f"{desc_prefix}Thay đổi một số cài đặt tiêu chuẩn của người chơi.",
        default_member_permissions=disnake.Permissions(manage_guild=True)
    )
    async def player_settings(self, interaction: disnake.AppCmdInter):

        inter, bot = await select_bot_pool(interaction, return_new=True)

        if not bot:
            return

        await inter.response.defer(ephemeral=True)

        guild_data = await self.bot.get_data(inter.guild_id, db_name=DBModel.guilds)

        try:
            func = inter.store_message.edit
        except AttributeError:
            try:
                func = inter.edit_original_message
            except AttributeError:
                func = inter.send

        view = PlayerSettings(inter, bot, guild_data)

        view.message = await func(
            embed=disnake.Embed(
                description="**Điều chỉnh cài đặt trình phát mặc định:**",
                color=self.bot.get_color()
            ).set_author(name=str(bot.user), icon_url=bot.user.display_avatar.url), view=view
        )

        await view.wait()

    setup_cd = commands.CooldownMapping.from_cooldown(1, 20, commands.BucketType.guild)
    setup_mc =commands.MaxConcurrency(1, per=commands.BucketType.guild, wait=False)

    setup_args = CommandArgparse()
    setup_args.add_argument('-reset', '--reset', '-purge', '--purge', action="store_true",
                             help="Xóa tin nhắn kênh đã chọn (tối đa 100 tin nhắn, không hiệu quả trong diễn đàn).")

    @commands.has_guild_permissions(manage_guild=True)
    @commands.command(
        name="setup", aliases=["songrequestchannel", "sgrc"], usage="{prefix}{cmd} [id|#kênh]\nEx: {prefix}{cmd} #Anh-Ba-Phát-Nhạc",
        description="Tạo/Chọn một kênh chuyên dụng để yêu cầu bài hát và tạo một trình phát cố định.",
        cooldown=setup_cd, max_concurrency=setup_mc, extras={"flags": setup_args}
    )
    async def setup_legacy(
            self,
            ctx: CustomContext,
            channel: Union[disnake.TextChannel, disnake.VoiceChannel, disnake.ForumChannel, None] = None, *args
    ):

        args, unknown = ctx.command.extras['flags'].parse_known_args(args)

        await self.setup.callback(self=self, interaction=ctx, target=channel,
                                  purge_messages=args.reset)

    @commands.slash_command(
        description=f"{desc_prefix}Tạo/chọn một kênh chuyên dụng để yêu cầu bài hát và tạo một trình phát cố định.",
        default_member_permissions=disnake.Permissions(manage_guild=True), cooldown=setup_cd, max_concurrency=setup_mc
    )
    async def setup(
            self,
            interaction: disnake.AppCmdInter,
            target: Union[disnake.TextChannel, disnake.VoiceChannel, disnake.ForumChannel, disnake.StageChannel] = commands.Param(
                name="channel", default=None, description="Chọn một kênh"
            ),
            purge_messages: str = commands.Param(
                name="clear_message", default="no",
                description="Xóa tin nhắn kênh đã chọn (tối đa 100 tin nhắn, không hiệu quả trong diễn đàn).",
                choices=[
                    disnake.OptionChoice(
                        disnake.Localized("Yes", data={disnake.Locale.pt_BR: "Sim"}), "yes"
                    ),
                    disnake.OptionChoice(
                        disnake.Localized("No", data={disnake.Locale.pt_BR: "Não"}), "no"
                    )
                ],
            )
    ):

        inter, bot = await select_bot_pool(interaction, return_new=True)

        if not bot:
            return

        guild = bot.get_guild(inter.guild_id)

        channel = bot.get_channel(inter.channel.id)

        if target and bot != self.bot:
            target = bot.get_channel(target.id)

        channel_name = f'{bot.user.name} Chế độ Tĩnh'

        if isinstance(target, disnake.ForumChannel) and not isinstance(inter, CustomContext):

            await inter.response.send_modal(
                title="Chọn một tên cho bài đăng (tối đa 30 chữ.)",
                custom_id=str(inter.id),
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="Tên",
                        custom_id="forum_title",
                        min_length=4,
                        max_length=30,
                        value=channel_name[:30],
                        required=True
                    )
                ]
            )

            try:
                modal_inter: disnake.ModalInteraction = await inter.bot.wait_for("modal_submit", timeout=30,
                                                                           check=lambda i: i.data.custom_id == str(inter.id))
            except asyncio.TimeoutError:
                if isinstance(inter, disnake.MessageInteraction):
                    try:
                        await inter.delete_original_message()
                    except:
                        pass
                return

            if isinstance(inter, disnake.MessageInteraction):
                try:
                    await inter.delete_original_message()
                except:
                    pass

            update_inter(interaction, modal_inter)
            inter = modal_inter
            channel_name = inter.text_values["forum_title"]

        perms_dict = {
            "embed_links": True,
            "send_messages": True,
            "send_messages_in_threads": True,
            "read_messages": True,
            "create_public_threads": True,
            "read_message_history": True,
            "manage_messages": True,
            "manage_channels": True,
            "attach_files": True,
        }

        if guild.me.guild_permissions.administrator:
            perms_dict["manage_permissions"] = True

        channel_kwargs = {
            "overwrites": {
                guild.me: disnake.PermissionOverwrite(**perms_dict)
            }
        }

        await inter.response.defer(ephemeral=True)

        guild_data = await bot.get_data(inter.guild_id, db_name=DBModel.guilds)

        original_message = None
        existing_channel = True

        try:
            player: LavalinkPlayer = bot.music.players[guild.id]
            if player.static:
                original_message = player.message
        except KeyError:
            player = None

        if not original_message:

            try:
                channel_db = bot.get_channel(int(guild_data["player_controller"]["channel"])) or \
                             await bot.fetch_channel(int(guild_data["player_controller"]["channel"]))
                original_message = await channel_db.fetch_message(int(guild_data["player_controller"]["message_id"]))
            except:
                pass

        embed_archived = disnake.Embed(
            description=f"**Kênh yêu cầu âm nhạc này được cấu hình lại bởi thành viên {inter.author.mention}.**",
            color=bot.get_color(guild.me)
        )

        async def get_message(original_message, target):

            if original_message and original_message.channel != target and original_message.guild.id == target.guild.id:

                try:
                    if isinstance(original_message.channel.parent, disnake.ForumChannel):
                        await original_message.thread.delete(reason=f"Người chơi được cấu hình lại bởi {inter.author}.")
                        return
                except AttributeError:
                    pass
                except Exception:
                    traceback.print_exc()
                    return

                try:
                    await original_message.edit(content=None, embed=embed_archived, view=None)
                except:
                    pass

                try:
                    await original_message.thread.edit(
                        archived=True,
                        locked=True,
                        reason=f"Người chơi được cấu hình lại bởi {inter.author}."
                    )
                except:
                    pass

            else:
                return original_message

        if not target:
            try:
                id_ = inter.id
            except AttributeError:
                id_ = ""

            kwargs_msg = {}
            try:
                func = inter.edit_original_message
            except:
                try:
                    func = inter.store_message.edit
                except:
                    try:
                        func = inter.response.edit_message
                    except:
                        func = inter.send
                        kwargs_msg = {"ephemeral": True}

            buttons = [
                disnake.ui.Button(label="Tạo kênh văn bản", custom_id=f"text_channel_{id_}", emoji="💬", disabled=not guild.me.guild_permissions.manage_channels),
                disnake.ui.Button(label="Tạo kênh thoại", custom_id=f"voice_channel_{id_}", emoji="🔊", disabled=not guild.me.guild_permissions.manage_channels),
                disnake.ui.Button(label="Hủy bỏ", custom_id=f"voice_channel_cancel_{id_}", emoji="❌")
            ]

            if "COMMUNITY" in guild.features:
                buttons.insert(2, disnake.ui.Button(label="Tạo kênh giai đoạn", custom_id=f"stage_channel_{id_}",
                                  emoji="<:stagechannel:1077351815533826209>", disabled=not guild.me.guild_permissions.manage_channels))

            color = self.bot.get_color(guild.me)

            embeds = [
                disnake.Embed(
                    description="**Chọn một kênh " + ("Hoặc nhấp vào một trong các nút bên dưới để tạo một kênh mới để yêu cầu bài hát." if guild.me.guild_permissions.manage_channels else "dưới:") +'**' ,
                    color=color
                ).set_footer(text="Bạn chỉ có 45 giây để chọn một tùy chọn.")
            ]

            if not guild.me.guild_permissions.manage_channels:
                embeds.append(
                    disnake.Embed(
                        description=f"Các Nút Tạo kênh đã bị vô hiệu hóa do bot **{bot.user.mention}** "
                                    "Không có quyền ** Quản lý các kênh ** trên máy chủ.",
                        color=color
                    )
                )

            disnake.Embed(color=color).set_footer(
                text="Lưu ý: Nếu bạn muốn sử dụng kênh diễn đàn, bạn sẽ phải chọn một trong danh sách kênh "
                     "bên dưới (nếu bạn không có, bạn sẽ phải tạo một kênh diễn đàn theo cách thủ công và sử dụng cái này "
                     "lệnh một lần nữa."
            )

            msg_select = await func(
                embeds=embeds,
                components=[
                    disnake.ui.ChannelSelect(
                        custom_id=f"existing_channel_{id_}",
                        min_values=1, max_values=1,
                        channel_types=[
                            disnake.ChannelType.text,
                            disnake.ChannelType.voice,
                            disnake.ChannelType.stage_voice,
                            disnake.ChannelType.forum
                        ]
                    ),
                ] + buttons,
                **kwargs_msg
            )

            if isinstance(inter, CustomContext):
                bot_inter = bot
                check = (lambda i: i.message.id == msg_select.id and i.author.id == inter.author.id)
            else:
                bot_inter = inter.bot
                check = (lambda i: i.data.custom_id.endswith(f"_{id_}") and i.author.id == inter.author.id)

            done, pending = await asyncio.wait([
                bot_inter.loop.create_task(bot_inter.wait_for('button_click', check=check)),
                bot_inter.loop.create_task(bot_inter.wait_for('dropdown', check=check))
            ],
                timeout=45, return_when=asyncio.FIRST_COMPLETED)

            for future in pending:
                future.cancel()

            if not done:

                try:
                    inter.application_command.reset_cooldown(inter)
                except AttributeError:
                    try:
                        inter.command.reset_cooldown(inter)
                    except:
                        pass

                if msg_select:
                    func = msg_select.edit
                else:
                    try:
                        func = (await inter.original_message()).edit
                    except:
                        func = inter.message.edit

                try:
                    await func(
                        embed=disnake.Embed(
                            description="**Thời gian đã hết!**",
                            color=disnake.Color.red()
                        ),
                        components=None
                    )
                except disnake.NotFound:
                    pass
                except Exception:
                    traceback.print_exc()

                return

            inter_message = done.pop().result()

            update_inter(interaction, inter_message)

            if inter_message.data.custom_id.startswith("voice_channel_cancel"):

                await inter_message.response.edit_message(
                    embed=disnake.Embed(
                        description="**Đã hủy bỏ hoạt động...**",
                        color=self.bot.get_color(guild.me),
                    ), components=None
                )
                return

            if channel.category and channel.category.permissions_for(guild.me).send_messages:
                target = channel.category
            else:
                target = guild

            if inter_message.data.custom_id.startswith("existing_channel_"):
                target = bot.get_channel(int(inter_message.data.values[0]))
                existing_channel = True

            else:

                if not guild.me.guild_permissions.manage_channels:
                    raise GenericError(f"**Bot {bot.user.mention} không có quyền quản lý kênh để tạo kênh mới.**")

                await inter_message.response.defer()
                if inter_message.data.custom_id.startswith("voice_channel_"):
                    target = await target.create_voice_channel(f"{bot.user.name} player controller", **channel_kwargs)
                elif inter_message.data.custom_id.startswith("stage_channel_"):
                    target = await target.create_stage_channel(f"{bot.user.name} player controller", **channel_kwargs)
                else:
                    target = await target.create_text_channel(f"{bot.user.name} player controller", **channel_kwargs)

                existing_channel = False

            inter = inter_message

        if target == guild.public_updates_channel:
            raise GenericError("**Bạn không thể sử dụng kênh cập nhật Discord.**")

        if target == guild.rules_channel:
            raise GenericError("**Bạn không thể sử dụng kênh quy tắc.**")

        check_channel_perm(target)

        if isinstance(target, disnake.ForumChannel):

            channel_kwargs.clear()

            if not target.permissions_for(guild.me).create_forum_threads:
                raise GenericError(f"**{bot.user.mention} không có quyền đăng trên kênh {target.mention}.**")

            try:
                id_ = f"modal_{inter.id}"
            except AttributeError:
                id_ = f"modal_{inter.message.id}"

            if not inter.response.is_done():

                await inter.response.send_modal(
                    title="Xác định tên cho bài viết diễn đàn",
                    custom_id=id_,
                    components=[
                        disnake.ui.TextInput(
                            style=disnake.TextInputStyle.short,
                            label="Tên",
                            custom_id="forum_title",
                            min_length=4,
                            max_length=30,
                            value=channel_name[:30],
                            required=True
                        )
                    ]
                )

                try:
                    modal_inter: disnake.ModalInteraction = await inter.bot.wait_for("modal_submit", timeout=30, check=lambda i: i.custom_id == id_)
                except asyncio.TimeoutError:
                    try:
                        func = inter.edit_original_message
                    except AttributeError:
                        func = msg_select.edit
                    await func(embed=disnake.Embed(description="### Đã hết thời gian!", color=bot.get_color(guild.me)), view=None)
                    return

                try:
                    await msg_select.delete()
                except:
                    pass

                update_inter(interaction, modal_inter)
                inter = modal_inter

                await inter.response.defer()

                channel_name = inter.text_values["forum_title"]

            thread = None
            message = None

            for t in target.threads:
                if t.owner_id == bot.user.id:
                    try:
                        message = await t.fetch_message(t.id)
                    except disnake.NotFound:
                        continue
                    thread = t
                    thread_kw = {}
                    if thread.locked and target.permissions_for(target.guild.me).manage_threads:
                        thread_kw.update({"locked": False, "archived": False})
                    elif thread.archived:
                        thread_kw["archived"] = False
                    if thread_kw:
                        await t.edit(**thread_kw)
                    break

            if not thread and guild.me.guild_permissions.read_message_history:
                async for t in target.archived_threads(limit=100):
                    if t.owner_id == bot.user.id:
                        try:
                            message = await t.fetch_message(t.id)
                        except disnake.NotFound:
                            continue
                        thread = t
                        thread_kw = {}
                        if thread.locked and target.permissions_for(target.guild.me).manage_threads:
                            thread_kw.update({"locked": False, "archived": False})
                        elif thread.archived:
                            thread_kw["archived"] = False
                        if thread_kw:
                            await t.edit(**thread_kw)
                        break

            if not thread:

                if not target.permissions_for(guild.me).manage_threads:
                    raise GenericError(
                        f"**{bot.user.mention} không có quyền quản lý chủ đề trên kênh {target.mention}.**\n"
                         f"`Lưu ý: Bạn có thể tạm thời cấp cho tôi quyền này và sau khi sử dụng lại lệnh"
                         f"bạn có thể xóa quyền này.`")

                """if not target.permissions_for(guild.me).create_forum_threads:
                    raise GenericError(
                        f"**{bot.user.mention} não possui permissão para postar no canal {target.mention}.**")"""

                thread_wmessage = await target.create_thread(
                    name=channel_name,
                    content="Đăng cho yêu cầu âm nhạc.",
                    auto_archive_duration=10080,
                    slowmode_delay=5,
                )
                message = thread_wmessage.message

            message = await send_idle_embed(target=message, bot=bot, force=True,
                                            guild_data=guild_data)

            target = message.channel

            await get_message(original_message, target)

        else:

            if existing_channel and not guild.me.guild_permissions.administrator and not target.permissions_for(guild.me).manage_permissions:
                raise GenericError(f"**{guild.me.mention} không có sự cho phép của quản trị viên hoặc "
                                    f"quản lý quyền của kênh {target.mention}** để chỉnh sửa quyền "
                                    f"cần thiết để hệ thống yêu cầu nhạc hoạt động bình thường.\n\n"
                                    f"Nếu bạn không muốn cấp quyền quản trị viên hoặc chỉnh sửa quyền của"
                                    f" kênh {target.mention} để cho phép tôi quản lý các quyền của kênh. Bạn có thể sử dụng lệnh "
                                    f" mà không chọn kênh đích.")

            if not target.permissions_for(guild.me).read_messages:
                raise GenericError(f"{bot.user.mention} không có quyền gửi tin nhắn lên kênh {target.mention}")

            if purge_messages == "yes":
                await target.purge(limit=100, check=lambda m: m.author != guild.me or not m.thread)

            message = await get_message(original_message, target)

            if not message:

                async for m in target.history(limit=100):

                    if m.author == guild.me and m.thread:
                        message = m
                        break

        if existing_channel:
            try:
                await target.edit(**channel_kwargs)
            except:
                traceback.print_exc()

        channel = target

        msg = f"{inter.author.mention}, Hệ thống yêu cầu âm nhạc đã được định cấu hình trên kênh <#{channel.id}> bằng bot: {bot.user.mention}"

        if player and player.text_channel != target:
            if player.static:
                try:
                    await player.message.thread.edit(
                        archived=True,
                        locked=True,
                        reason=f"Người chơi được cấu hình lại bởi {inter.author}."
                    )
                except:
                    pass
            else:
                try:
                    await player.message.delete()
                except:
                    pass
            if not message or message.channel.id != channel.id:
                message = await send_idle_embed(channel, bot=bot, force=True, guild_data=guild_data)
            player.message = message
            player.static = True
            player.text_channel = channel
            await player.invoke_np(force=True)

        elif not message or message.channel.id != channel.id:
            message = await send_idle_embed(channel, bot=bot, force=True, guild_data=guild_data)

        if isinstance(channel, disnake.TextChannel):
            if not message.thread:
                if channel.permissions_for(guild.me).create_public_threads:
                    await message.create_thread(name="Song-Requests", auto_archive_duration=10080)
            else:
                thread_kw = {}
                if message.thread.locked and message.thread.permissions_for(guild.me).manage_threads:
                    thread_kw.update({"locked": False, "archived": False})
                elif message.thread.archived and message.thread.owner_id == bot.user.id:
                    thread_kw["archived"] = False
                if thread_kw:
                    await message.thread.edit(reason=f"Yêu cầu bài hát được kích hoạt lại bởi: {inter.author}.", **thread_kw)
        elif player and isinstance(channel, (disnake.VoiceChannel, disnake.StageChannel)) and player.guild.me.voice.channel != channel:
            await player.connect(channel.id)

        guild_data['player_controller']['channel'] = str(channel.id)
        guild_data['player_controller']['message_id'] = str(message.id)
        await bot.update_data(guild.id, guild_data, db_name=DBModel.guilds)

        reset_txt = f"{inter.prefix}reset" if isinstance(inter, CustomContext) else "/reset"

        embed = disnake.Embed(
            description=f"**{msg}**\n\nLưu ý: Nếu bạn muốn đảo ngược cài đặt này, chỉ cần sử dụng lệnh {reset_txt} hoặc "
                        f"Xóa kênh {channel.mention}",
            color=bot.get_color(guild.me)
        )

        if not inter.response.is_done():
            try:
                await inter.response.edit_message(embed=embed, components=None)
            except AttributeError:
                await inter.send(embed=embed)
        try:
            await inter.edit_original_message(embed=embed, components=None)
        except (AttributeError, disnake.InteractionNotEditable):
            try:
                await inter.response.edit_message(embed=embed, components=None)
            except:
                await inter.send(embed=embed, ephemeral=True)

    @commands.has_guild_permissions(manage_guild=True)
    @commands.bot_has_guild_permissions(manage_threads=True)
    @commands.command(
        name="reset",
        description="Đặt lại các cài đặt liên quan đến kênh yêu cầu bài hát.",
        cooldown=setup_cd, max_concurrency=setup_mc
    )
    async def reset_legacy(self, ctx: CustomContext, *, delete_channel: str = None):

        if delete_channel == "--delete":
            delete_channel = "sim"

        await self.reset.callback(self=self, interaction=ctx, delete_channel=delete_channel)

    @commands.slash_command(
        description=f"{desc_prefix}Đặt lại các cài đặt liên quan đến kênh yêu cầu bài hát.",
        default_member_permissions=disnake.Permissions(manage_guild=True), cooldown=setup_cd, max_concurrency=setup_mc
    )
    async def reset(
            self,
            interaction: disnake.AppCmdInter,
            delete_channel: str = commands.Param(
                name="delete_channel",
                description="Xóa kênh điều khiển trình phát", default=None, choices=["yes", "no"]
            )
    ):

        inter, bot = await select_bot_pool(interaction)

        if not bot:
            return

        await inter.response.defer(ephemeral=True)

        guild = bot.get_guild(inter.guild_id) or inter.guild

        if not guild.me.guild_permissions.manage_threads:
            raise GenericError(f"Tôi không có quyền **{perms_translations['manage_threads']}** trên máy chủ.")

        channel_inter = bot.get_channel(inter.channel.id)

        guild_data = None

        if inter.bot == bot:
            inter, guild_data = await get_inter_guild_data(inter, bot)

        if not guild_data:
            guild_data = await bot.get_data(inter.guild_id, db_name=DBModel.guilds)

        try:
            channel = bot.get_channel(int(guild_data['player_controller']['channel'])) or \
                      await bot.fetch_channel(int(guild_data['player_controller']['channel']))
        except:
            channel = None

        if not channel or channel.guild.id != inter.guild_id:
            raise GenericError(f"**Không có kênh đặt hàng âm nhạc được định cấu hình (hoặc kênh đã bị xóa).**")

        try:
            if isinstance(channel.parent, disnake.ForumChannel):
                await channel.delete(reason=f"{inter.author.id} resetou player")
                if channel_inter != channel:
                    await inter.edit_original_message("Bài viết đã bị xóa thành công!", embed=None, components=None)

                try:
                    player: LavalinkPlayer = bot.music.players[guild.id]
                except KeyError:
                    pass
                else:
                    player.static = False
                    player.message = None
                    player.text_channel = channel_inter
                    await player.invoke_np(force=True)

                return

        except AttributeError:
            pass

        try:
            original_message = await channel.fetch_message(int(guild_data["player_controller"]["message_id"]))
        except:
            original_message = None

        guild_data["player_controller"].update({
            "message_id": None,
            "channel": None
        })

        await self.bot.update_data(guild.id, guild_data, db_name=DBModel.guilds)

        try:
            func = inter.edit_original_message
        except AttributeError:
            try:
                func = inter.response.edit_message
            except AttributeError:
                func = inter.send

        await func(
            embed=disnake.Embed(
                color=self.bot.get_color(guild.me),
                description="**Kênh yêu cầu âm nhạc đã được thiết lập lại thành công.**"
            ), components=[]
        )

        try:
            player: LavalinkPlayer = bot.music.players[guild.id]
        except KeyError:
            pass
        else:
            player.static = False
            player.message = None
            player.text_channel = channel_inter
            await player.invoke_np(force=True)

        try:
            if delete_channel == "sim":
                await channel.delete(reason=f"Đặt lại người chơi: {inter.author}")

            elif original_message:
                await original_message.edit(
                    content=f"Kênh để đặt hàng nhạc được đặt lại bởi thành viên {inter.author.mention}.",
                    embed=None, components=[
                        disnake.ui.Button(label="Cấu hình lại kênh này", emoji="💠",
                                          custom_id="musicplayer_request_channel")
                    ]
                )
                await original_message.thread.edit(archived=True, reason=f"Đặt lại người chơi {inter.author}.")
        except Exception as e:
            traceback.print_exc()
            raise GenericError(
                "**Kênh yêu cầu âm nhạc được đặt lại của cơ sở dữ liệu nhưng xảy ra lỗi trong quá trình:** "
                f"```py\n{repr(e)}```"
            )

    djrole_cd = commands.CooldownMapping.from_cooldown(1, 7, commands.BucketType.guild)
    djrole_mc =commands.MaxConcurrency(1, per=commands.BucketType.guild, wait=False)

    @commands.has_guild_permissions(manage_guild=True)
    @commands.command(name="adddjrole",description="Thêm một vị trí vào danh sách DJ của máy chủ.",
                      usage="{prefix}{cmd} [id|tên|@role]\nEx: {prefix}{cmd} @AnhBa", cooldown=djrole_cd, max_concurrency=djrole_mc)
    async def add_dj_role_legacy(self, ctx: CustomContext, *, role: disnake.Role):
        await self.add_dj_role.callback(self=self, interaction=ctx, role=role)

    @commands.slash_command(
        description=f"{desc_prefix}Thêm một vị trí vào danh sách DJ của máy chủ.",
        default_member_permissions=disnake.Permissions(manage_guild=True), cooldown=djrole_cd, max_concurrency=djrole_mc
    )
    async def add_dj_role(
            self,
            interaction: disnake.ApplicationCommandInteraction,
            role: disnake.Role = commands.Param(name="role", description="Vai trò cần thêm")
    ):

        inter, bot = await select_bot_pool(interaction)
        guild = bot.get_guild(inter.guild_id) or inter.guild
        role = guild.get_role(role.id)

        if role == guild.default_role:
            await inter.send("Bạn không thể thêm vị trí này.", ephemeral=True)
            return

        guild_data = None

        if inter.bot == bot:
            inter, guild_data = await get_inter_guild_data(inter, bot)

        if not guild_data:
            guild_data = await bot.get_data(inter.guild_id, db_name=DBModel.guilds)

        if str(role.id) in guild_data['djroles']:
            await inter.send(f"Role {role.mention} đã nằm trong danh sách của DJ ", ephemeral=True)
            return

        guild_data['djroles'].append(str(role.id))

        await bot.update_data(guild.id, guild_data, db_name=DBModel.guilds)

        await inter.send(f"Role {role.mention} Nó đã được thêm vào danh sách của DJ.", ephemeral=True)

    @commands.has_guild_permissions(manage_guild=True)
    @commands.command(name="removedjrole", description="Xóa một vị trí khỏi danh sách DJ của máy chủ.",
                      usage="{prefix}{cmd} [id|tên|@role]\nEx: {prefix}{cmd} @Anhbadungcam",
                      cooldown=djrole_cd, max_concurrency=djrole_mc)
    async def remove_dj_role_legacy(self, ctx: CustomContext, *, role: disnake.Role):
        await self.remove_dj_role.callback(self=self, interaction=ctx, role=role)

    @commands.slash_command(
        description=f"{desc_prefix}Xóa một vị trí khỏi danh sách DJ của máy chủ.",
        default_member_permissions=disnake.Permissions(manage_guild=True), cooldown=djrole_cd, max_concurrency=djrole_mc
    )
    async def remove_dj_role(
            self,
            interaction: disnake.ApplicationCommandInteraction,
            role: disnake.Role = commands.Param(name="role", description="Vai trò cần xóa")
    ):

        inter, bot = await select_bot_pool(interaction)

        if not bot:
            return

        if inter.bot == bot:
            inter, guild_data = await get_inter_guild_data(inter, bot)
        else:
            guild_data = await bot.get_data(inter.guild_id, db_name=DBModel.guilds)

        if not guild_data['djroles']:

            await inter.send("Không có vị trí trong danh sách của DJ.", ephemeral=True)
            return

        guild = bot.get_guild(inter.guild_id) or inter.guild
        role = guild.get_role(role.id)

        if str(role.id) not in guild_data['djroles']:
            await inter.send(f"ROLE {role.mention} không có trong danh sách của DJ\n\n" + "ROLE:\n" +
                                              " ".join(f"<#{r}>" for r in guild_data['djroles']), ephemeral=True)
            return

        guild_data['djroles'].remove(str(role.id))

        await bot.update_data(guild.id, guild_data, db_name=DBModel.guilds)

        await inter.send(f"ROLE {role.mention} Nó đã bị xóa khỏi danh sách của DJ.", ephemeral=True)

    skin_cd = commands.CooldownMapping.from_cooldown(1, 20, commands.BucketType.guild)
    skin_mc =commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)

    @commands.has_guild_permissions(manage_guild=True)
    @commands.command(description="Thay đổi giao diện của người chơi.", name="changeskin", aliases=["skin", "skins"],
                      cooldown=skin_cd, max_concurrency=skin_mc)
    async def change_skin_legacy(self, ctx: CustomContext):

        await self.change_skin.callback(self=self, interaction=ctx)

    @commands.slash_command(
        description=f"{desc_prefix}Thay đổi giao diện của người chơi.", cooldown=skin_cd, max_concurrency=skin_mc,
        default_member_permissions=disnake.Permissions(manage_guild=True)
    )
    async def change_skin(self, interaction: disnake.AppCmdInter):

        inter, bot = await select_bot_pool(interaction, return_new=True)

        if not bot:
            return

        skin_list = [s for s in bot.player_skins if s not in bot.config["IGNORE_SKINS"].split()]
        static_skin_list = [s for s in bot.player_static_skins if s not in bot.config["IGNORE_STATIC_SKINS"].split()]

        await inter.response.defer(ephemeral=True)

        guild = bot.get_guild(inter.guild_id) or inter.guild

        add_skin_prefix = (lambda d: [f"> custom_skin: {i}" for i in d.keys()])

        guild_data = None

        if inter.bot == bot:
            inter, guild_data = await get_inter_guild_data(inter, bot)

        if not guild_data:
            guild_data = await bot.get_data(inter.guild_id, db_name=DBModel.guilds)

        try:
            global_data = inter.global_guild_data
        except AttributeError:
            global_data = await bot.get_global_data(guild.id, db_name=DBModel.guilds)
            inter.global_guild_data = global_data

        global_mode = global_data["global_skin"]

        selected = guild_data["player_controller"]["skin"] or bot.default_skin
        static_selected = guild_data["player_controller"]["static_skin"] or bot.default_static_skin

        global_selected = global_data["player_skin"] or bot.default_skin
        global_static_selected = global_data["player_skin_static"] or bot.default_static_skin

        skins_opts = [disnake.SelectOption(emoji="💠" if s.startswith("> custom_skin: ") else "🎨", label=f"Chế độ Bình Thường: {s.replace('> custom_skin: ', '')}", value=s, **{"default": True, "description": "skin atual"} if selected == s else {}) for s in skin_list + add_skin_prefix(global_data["custom_skins"])]
        static_skins_opts = [disnake.SelectOption(emoji="💠" if s.startswith("> custom_skin: ") else "🎨", label=f"Chế độ Tĩnh: {s.replace('> custom_skin: ', '')}", value=s, **{"default": True, "description": "skin atual"} if static_selected == s else {}) for s in static_skin_list + add_skin_prefix(global_data["custom_skins_static"])]

        global_skins_opts = [disnake.SelectOption(emoji="💠" if s.startswith("> custom_skin: ") else "🎨", label=f"Chế độ Bình Thường: {s.replace('> custom_skin: ', '')}", value=s, **{"default": True, "description": "skin atual"} if global_selected == s else {}) for s in skin_list + add_skin_prefix(global_data["custom_skins"])]
        global_static_skins_opts = [disnake.SelectOption(emoji="💠" if s.startswith("> custom_skin: ") else "🎨", label=f"Chế độ Tĩnh: {s.replace('> custom_skin: ', '')}", value=s, **{"default": True, "description": "skin atual"} if global_static_selected == s else {}) for s in static_skin_list + add_skin_prefix(global_data["custom_skins_static"])]

        embed = disnake.Embed(
            description="```ansi\n[31;1mChế độ Bình Thường:[0m``` " + ", ".join(f"[`[{s}]`]({bot.player_skins[s].preview})" for s in skin_list) + "\n\n" 
                        "```ansi\n[33;1mChế độ Tĩnh:[0m``` " + ", ".join(f"[`[{s}]`]({bot.player_static_skins[s].preview})" for s in static_skin_list) +
                        "\n\n`Lưu ý: Trong chế độ toàn cầu, giao diện sẽ được áp dụng trên toàn cầu trên tất cả các bot.`",
            colour=bot.get_color(guild.me)
        ).set_image("https://cdn.discordapp.com/attachments/554468640942981147/1082887587770937455/rainbow_bar2.gif")

        try:
            if bot.user.id != self.bot.user.id:
                embed.set_footer(text=f"Thông qua bot: {bot.user.display_name}", icon_url=bot.user.display_avatar.url)
        except AttributeError:
            pass

        select_view = SkinSelector(inter, embed, skins_opts, static_skins_opts, global_skins_opts, global_static_skins_opts, global_mode)

        try:
            func = inter.store_message.edit
        except:
            try:
                func = inter.edit_original_message
            except AttributeError:
                func = inter.send

        msg = await func(
            embed=embed,
            view=select_view
        )

        await select_view.wait()

        if select_view.skin_selected is None:
            await select_view.interaction.response.edit_message(
                view=None,
                embed=disnake.Embed(description="**Yêu cầu bị hủy bỏ.**", colour=bot.get_color(guild.me))
            )
            return

        if not select_view.interaction:
            try:
                msg = await inter.original_message()
            except AttributeError:
                pass
            for c in select_view.children:
                c.disabled = True
            await msg.edit(view=select_view)
            return

        update_inter(interaction, select_view.interaction)

        inter = select_view.interaction

        await inter.response.defer()

        try:
            global_data.update({"global_skin": select_view.global_mode})
        except:
            pass

        changed_skins_txt = ""

        if select_view.global_mode:
            try:
                global_data.update(
                    {
                        "player_skin": select_view.skin_selected,
                        "player_skin_static": select_view.static_skin_selected
                    }
                )
            except:
                pass
            else:
                await bot.update_global_data(inter.guild_id, global_data, db_name=DBModel.guilds)

            if global_selected != select_view.skin_selected:
                try:
                    changed_skins_txt += f"Global - Chế độ Bình Thường: [`{select_view.skin_selected}`]({self.bot.player_skins[select_view.skin_selected].preview})\n"
                except:
                    changed_skins_txt += f"Global - Chế độ Bình Thường: `{select_view.skin_selected.replace('> custom_skin: ', '[custom skin]: ')}`\n"

            if global_static_selected != select_view.static_skin_selected:
                try:
                    changed_skins_txt += f"Global - Chế độ Tĩnh: [`{select_view.static_skin_selected}`]({self.bot.player_static_skins[select_view.static_skin_selected].preview})\n"
                except:
                    changed_skins_txt += f"Global - Chế độ Tĩnh: `{select_view.static_skin_selected.replace('> custom_skin: ', '[custom skin]: ')}`\n"

        else:
            guild_data["player_controller"]["skin"] = select_view.skin_selected
            guild_data["player_controller"]["static_skin"] = select_view.static_skin_selected
            await bot.update_data(inter.guild_id, guild_data, db_name=DBModel.guilds)

            if selected != select_view.skin_selected:
                try:
                    changed_skins_txt += f"Chế độ Bình Thường: [`{select_view.skin_selected}`]({self.bot.player_skins[select_view.skin_selected].preview})\n"
                except:
                    changed_skins_txt += f"Chế độ Bình Thường: `{select_view.skin_selected.replace('> custom_skin: ', '[custom skin]: ')}`\n"

            if static_selected != select_view.static_skin_selected:
                try:
                    changed_skins_txt += f"Chế độ Tĩnh: [`{select_view.static_skin_selected}`]({self.bot.player_static_skins[select_view.static_skin_selected].preview})\n"
                except:
                    changed_skins_txt += f"Chế độ Tĩnh: `{select_view.static_skin_selected.replace('> custom_skin: ', '[custom skin]: ')}`\n"

        if global_mode != select_view.global_mode:
            changed_skins_txt += "Skin Global: `" + ("Kích hoạt" if select_view.global_mode else "Vô hiệu hóa") + "`\n"

        if not changed_skins_txt:
            txt = "**Không có thay đổi trong cài đặt giao diện...**"
        else:
            txt = f"**Giao diện trình phát của máy chủ đã được thay đổi thành công.**\n{changed_skins_txt}"

        kwargs = {
            "embed": disnake.Embed(
                description=txt,
                color=bot.get_color(guild.me)
            ).set_footer(text=f"{bot.user} - [{bot.user.id}]", icon_url=bot.user.display_avatar.with_format("png").url)
        }

        if isinstance(inter, CustomContext):
            await msg.edit(view=None, **kwargs)
        elif inter.response.is_done():
            await inter.edit_original_message(view=None, **kwargs)
        else:
            await inter.send(ephemeral=True, **kwargs)

        for b in self.bot.pool.bots:

            try:
                player: LavalinkPlayer = b.music.players[inter.guild_id]
            except KeyError:
                continue

            last_skin = str(player.skin)
            last_static_skin = str(player.skin_static)

            if player.static:

                if select_view.static_skin_selected == last_static_skin:
                    continue

            elif select_view.skin_selected == last_skin:
                continue

            try:
                await player.destroy_message()
            except:
                traceback.print_exc()

            player.skin = select_view.skin_selected
            player.skin_static = select_view.static_skin_selected

            for n, s in global_data["custom_skins"].items():
                if isinstance(s, str):
                    global_data["custom_skins"][n] = pickle.loads(b64decode(s))

            for n, s in global_data["custom_skins_static"].items():
                if isinstance(s, str):
                    global_data["custom_skins_static"][n] = pickle.loads(b64decode(s))

            player.custom_skin_data = global_data["custom_skins"]
            player.custom_skin_static_data = global_data["custom_skins_static"]
            player.setup_features()


            player.set_command_log(text=f"{inter.author.mention} Đổi giao diện trình phát!", emoji="🎨")

            try:
                if player.controller_mode and not [m for m in player.guild.me.voice.channel.members if not m.bot]:
                    try:
                        player.auto_skip_track_task.cancel()
                    except:
                        pass
                    player.auto_skip_track_task = b.loop.create_task(player.auto_skip_track())
            except:
                traceback.print_exc()

            await player.invoke_np(force=True)
            await asyncio.sleep(1.5)


    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.command(
        name="nodeinfo",
        aliases=["llservers", "ll"],
        description="Xem thông tin từ máy chủ âm nhạc."
    )
    async def nodeinfo_legacy(self, ctx: CustomContext):
        await self.nodeinfo.callback(self=self, interaction=ctx)

    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.slash_command(
        description=f"{desc_prefix}Xem thông tin từ các máy chủ âm nhạc (máy chủ Lavalink)."
    )
    async def nodeinfo(self, interaction: disnake.AppCmdInter):

        inter, bot = await select_bot_pool(interaction, return_new=True)

        if not bot:
            return

        guild = bot.get_guild(inter.guild_id) or inter.guild

        em = disnake.Embed(color=bot.get_color(guild.me), title="Máy chủ âm nhạc:")

        if not bot.music.nodes:
            em.description = "**Không có máy chủ.**"
            await inter.send(embed=em)
            return

        failed_nodes = set()

        for identifier, node in bot.music.nodes.items():

            if not node.available: continue

            try:
                current_player = node.players[inter.guild_id]
            except KeyError:
                current_player = None

            if not node.stats or not node.is_available:
                failed_nodes.add(node.identifier)
                continue

            txt = f" 🌐 Vùng: `{node.region.title()}`\n"

            used = humanize.naturalsize(node.stats.memory_used)
            total = humanize.naturalsize(node.stats.memory_allocated)
            free = humanize.naturalsize(node.stats.memory_free)
            cpu_cores = node.stats.cpu_cores
            cpu_usage = f"{node.stats.lavalink_load * 100:.2f}"
            started = node.stats.players

            txt += f' <:ram:1204300272957001778> RAM: `{used}/{free}`\n' \
                   f' <:ram:1204300272957001778> Tổng RAM: `{total}`\n' \
                   f' <:cpu:1146331051556339712> Số nhân CPU: `{cpu_cores}`\n' \
                   f' <:cpu:1146331051556339712> Mức sử dụng CPU: `{cpu_usage}%`\n' \
                   f' <a:z55:1203596745087520850> Phiên bản Lavalink `{node.version}`\n' \
                   f' <a:loading:1117802386333905017> Lần khởi động lại cuối cùng: <t:{int((disnake.utils.utcnow() - datetime.timedelta(milliseconds=node.stats.uptime)).timestamp())}:R>\n'

            if started:
                txt += "Players: "
                players = node.stats.playing_players
                idle = started - players
                if players:
                    txt += f'`[▶️{players}]`' + (" " if idle else "")
                if idle:
                    txt += f'`[💤{idle}]`'

                txt += "\n"

            if node.website:
                txt += f'[`Trang web của máy chủ`]({node.website})\n'

            status = "<a:aloadig:1138360673068384366>" if current_player else "<:upvote:1146329733102047242>"

            em.add_field(name=f'**{identifier}** {status}', value=txt)
            em.set_footer(text=f"{bot.user} - [{bot.user.id}]", icon_url=bot.user.display_avatar.with_format("png").url)

        embeds = [em]

        if failed_nodes:
            embeds.append(
                disnake.Embed(
                    title="**Máy chủ lỗi** ⁉️⁉️",
                    description=f"```ansi\n[31;1m" + "\n".join(failed_nodes) + "[0m\n```",
                    color=bot.get_color(guild.me)
                )
            )

        if isinstance(inter, disnake.MessageInteraction):
            await inter.response.edit_message(embeds=embeds, view=None)
        else:
            await inter.send(embeds=embeds, ephemeral=True)

    customskin_cd = commands.CooldownMapping.from_cooldown(1, 10, commands.BucketType.guild)
    customskin__mc =commands.MaxConcurrency(1, per=commands.BucketType.guild, wait=False)
    
    @commands.is_owner()
    @commands.has_guild_permissions(administrator=True)
    @commands.command(name="customskin", aliases=["setskin", "cskin", "cs", "ss"],
                      description="Tạo giao diện / mẫu của riêng bạn để sử dụng trong trình phát nhạc (Không khả dụng).",
                      cooldown=customskin_cd, max_concurrency=customskin__mc)
    async def customskin_legacy(self, ctx: CustomContext):
        await self.custom_skin.callback(self=self, inter=ctx)

    @commands.is_owner()
    @commands.slash_command(cooldown=customskin_cd, max_concurrency=customskin__mc,
                            description=f"{desc_prefix}Tạo giao diện / mẫu của riêng bạn để sử dụng trong trình phát nhạc (Không khả dụng).",
                            default_member_permissions=disnake.Permissions(administrator=True))
    async def custom_skin(self, inter: disnake.AppCmdInter):

        inter, bot = await select_bot_pool(inter, return_new=True)

        if not bot:
            return

        await inter.response.defer()

        global_data = await bot.get_global_data(inter.guild_id, db_name=DBModel.guilds)

        view = SkinEditorMenu(inter, bot, guild=bot.get_guild(inter.guild_id), global_data=global_data)

        if isinstance(inter, disnake.MessageInteraction):
            func = inter.edit_original_message
        else:
            func = inter.send
        view.message = await func(view=view, **view.build_embeds())
        await view.wait()

    @commands.Cog.listener("on_button_click")
    async def editor_placeholders(self, inter: disnake.MessageInteraction):

        if inter.data.custom_id != "skin_editor_placeholders" or not inter.guild:
            return

        await inter.send(
            ephemeral=True,
            embed=disnake.Embed(
                color=self.bot.get_color(inter.guild.me),
                description="### Placeholders para custom skins:\n```ansi\n"
                            "[34;1m{track.title}[0m -> Nome da música\n"
                            "[34;1m{track.title_25}[0m -> Nome da música (até 25 caracteres)\n"
                            "[34;1m{track.title_42}[0m -> Nome da música (até 42 caracteres)\n"
                            "[34;1m{track.title_25}[0m -> Nome da música (até 58 caracteres)\n"
                            "[34;1m{track.url}[0m -> Link da música\n"
                            "[34;1m{track.author}[0m -> Nome do Uploader/Artista da música\n"
                            "[34;1m{track.duration}[0m -> Tempo/Duração da música\n"
                            "[34;1m{track.thumb}[0m -> Link da miniatura/artowkr da música\n"
                            "[34;1m{playlist.name}[0m -> Nome da playlist de origem da música\n"
                            "[34;1m{playlist.url}[0m -> Link/Url da playlist de origem da música\n"
                            "[34;1m{player.loop.mode}[0m -> Modo de repetição do player\n"
                            "[34;1m{player.queue.size}[0m -> Quantidade de músicas na fila\n"
                            "[34;1m{player.volume}[0m -> Volume do player\n"
                            "[34;1m{player.autoplay}[0m -> Reprodução automática (Ativado/Desativado)\n"
                            "[34;1m{player.nightcore}[0m -> Efeito nightcore (Ativado/Desativado)\n"
                            "[34;1m{player.hint}[0m -> Dicas de uso do player\n"
                            "[34;1m{player.log.text}[0m -> Log do player\n"
                            "[34;1m{player.log.emoji}[0m -> Emoji do log do player\n"
                            "[34;1m{requester.global_name}[0m -> Nome global do membro que pediu a música.\n"
                            "[34;1m{requester.display_name}[0m -> Nome de exibição do membro que pediu a música.\n"
                            "[34;1m{requester.mention}[0m -> Menção do membro que pediu a música\n"
                            "[34;1m{requester.avatar}[0m -> Link do avatar do membro que pediu a música\n"
                            "[34;1m{guild.color}[0m -> Cor do maior cargo do bot no servidor\n"
                            "[34;1m{guild.icon}[0m -> Link do icone do servidor\n"
                            "[34;1m{guild.name}[0m -> Nome do servidor\n"
                            "[34;1m{guild.id}[0m -> ID do servidor\n"
                            "[34;1m{queue_format}[0m -> Músicas da fila pré-formatada (use o botão de configurar "
                            "placeholder caso queira alterar o estilo)\n"
                            "[34;1m{track.number}[0m -> Número da posição da música na fila (funcional junto com "
                            "o placeholder: [31;1m{queue_format}[0m)```"
            )
        )

class RPCCog(commands.Cog):

    emoji = "🔧"
    name = "Hệ thống rpc (Owner ONlY)"
    desc_prefix = f"[{emoji} {name}] | "

    def __init__(self, bot: BotCore):
        self.bot = bot

    rpc_cd = commands.CooldownMapping.from_cooldown(1, 30, commands.BucketType.user)

    @commands.command(description="Kích hoạt/Tắt hệ thống hiện diện phong phú trong trạng thái của bạn.",
                      name="richpresence", aliases=["rich_presence", "rpc"], cooldown=rpc_cd)
    async def rich_presence_legacy(self, ctx: CustomContext):

        await self.rich_presence.callback(self=self, inter=ctx)

    @commands.slash_command(
        description=f"{desc_prefix}Kích hoạt/Tắt hệ thống hiện diện phong phú trong trạng thái của bạn.", cooldown=rpc_cd
    )
    async def rich_presence(self, inter: disnake.AppCmdInter):

        if not self.bot.config["ENABLE_RPC_COMMAND"] and not any([await b.is_owner(inter.author) for b in self.bot.pool.bots]):
            raise GenericError("**Lệnh này bị tắt trong cài đặt của tôi...**\n"
                                "Chỉ nhà phát triển của tôi mới có thể kích hoạt lệnh này một cách công khai.")

        if not self.bot.config["RPC_PUBLIC_URL"] and not self.bot.config["RPC_SERVER"]:
            raise GenericError("**RPC_SERVER không được định cấu hình trong ENV/ENVIRONMENTS (hoặc tệp .env)**")

        components = []

        embed = disnake.Embed(
            color=self.bot.get_color(),
            description="**Hướng dẫn nhỏ sử dụng ứng dụng để hiển thị nhạc bạn đang nghe qua RPC:\n\n"
                         "Tải ứng dụng (musicbot_rpc.zip)"
                         "[tại đây](https://github.com/zRitsu/Discord-MusicBot-RPC/releases).\n\n"
                         "Giải nén musicbot_rpc.zip và mở musicbot_rpc trong thư mục." \
                         "Thêm liên kết websocket bên dưới vào ứng dụng (tab: Cài đặt socket):** ```ansi\n" \
                        f"{(self.bot.config['RPC_PUBLIC_URL'] or self.bot.config['RPC_SERVER']).replace('$PORT', os.environ.get('PORT', '80'))}```"
        )

        embed.set_footer(text="Chỉ hỗ trợ discord app trên máy tính, chưa hỗ trợ discord web hay\n"
                         f" Discord app trên điện thoại")

        if self.bot.config["ENABLE_RPC_AUTH"]:

            embed.description += "\n**Cần phải tạo/tạo/nhập mã thông báo để cấp quyền truy cập RPC " \
                                  "(Kiểm tra các nút bên dưới), sao chép mã thông báo và trong ứng dụng (Tab: Cài đặt ổ cắm) " \
                                  "nhấp vào nút \"Dán mã thông báo\"**"

            components.extend(
                [
                    disnake.ui.Button(label="Tạo/Đặt lại mã thông báo", custom_id=f"rpc_gen.{inter.author.id}", emoji="🔑",
                                      row=0),
                    disnake.ui.Button(label="Nhập/Chỉnh sửa/Xem mã thông báo", custom_id=f"rpc_create.{inter.author.id}",
                                      emoji="✍️", row=0),
                    disnake.ui.Button(label="Xóa mã thông báo (Vô hiệu hóa)", custom_id=f"rpc_remove.{inter.author.id}",
                                      emoji="♻️", row=1),
                ]
            )

        embed.description += "\n\n**Bây giờ chỉ cần nhấp vào nút \"Bắt đầu hiện diện\" và nghe nhạc qua "\
                              "một số bot tương thích.**"

        embed.set_author(
            name=f"{inter.author.display_name}#{inter.author.discriminator} - [ {inter.author.id} ]",
            icon_url=inter.author.display_avatar.with_static_format("png").url
        )

        if isinstance(inter, CustomContext):
            components.append(
                disnake.ui.Button(label="Đóng", custom_id=f"rpc_close.{inter.author.id}", emoji="❌", row=1),
            )

        await inter.send(
            embed=embed,
            components=components,
            ephemeral=True
        )

    @commands.Cog.listener("on_button_click")
    async def rpc_button_event(self, inter: disnake.MessageInteraction):

        if not inter.data.custom_id.startswith("rpc_"):
            return

        button_id, user_id = inter.data.custom_id.split(".")

        if user_id != str(inter.author.id):
            await inter.send(f"Chỉ <@{user_id}> mới có thể sử dụng nút tin nhắn!", ephemeral=True)
            return

        if button_id == "rpc_gen":
            await inter.response.defer()

            try:
                data = inter.global_user_data
            except AttributeError:
                data = await self.bot.get_global_data(id_=user_id, db_name=DBModel.users)
                inter.global_user_data = data

            if data["token"]:
                await self.close_presence(inter)

            data["token"] = "".join(random.choice(string.ascii_letters + string.digits) for i in range(50))
            await self.bot.update_global_data(id_=user_id, data=data, db_name=DBModel.users)
            msg = f"Mã thông báo để sử dụng trong ứng dụng RPC (Rich Presence) đã được tạo thành công!\n\n" \
                   f"`Mã thông báo được tạo:` ||{data['token']}||"

        elif button_id == "rpc_create":

            kwargs = {}

            try:

                try:
                    data = inter.global_user_data
                except AttributeError:
                    data = await self.bot.get_global_data(id_=user_id, db_name=DBModel.users)
                    inter.global_user_data = data

                if len(data["token"]) == 50:
                    kwargs["value"] = data["token"]
            except:
                pass

            await inter.response.send_modal(
                title="Nhập mã thông báo",
                custom_id="rpc_token_create",
                components=[
                    disnake.ui.TextInput(
                        style=disnake.TextInputStyle.short,
                        label="Dán mã thông báo vào trường bên dưới:",
                        placeholder="Lưu ý: Vì lý do bảo mật, đừng bao giờ đưa mật khẩu cá nhân vào đây!",
                        custom_id="token_input",
                        min_length=50,
                        max_length=50,
                        required=True,
                        **kwargs
                    ),
                ]
            )

            if not inter.message.flags.ephemeral:
                await inter.message.delete()

            return

        elif button_id == "rpc_remove":

            await inter.response.defer()

            await self.close_presence(inter)

            try:
                data = inter.global_user_data
            except AttributeError:
                data = await self.bot.get_global_data(id_=user_id, db_name=DBModel.users)
                inter.global_user_data = data

            data["token"] = ""
            await self.bot.update_global_data(id_=user_id, data=data, db_name=DBModel.users)
            msg = "Mã thông báo đã được xóa thành công!\n" \
                   "Bây giờ hệ thống RPC sẽ bị vô hiệu hóa đối với người dùng của bạn."

        else: # button_id == "rpc_close"
            await inter.message.delete()
            return

        if inter.message.flags.ephemeral:
            await inter.edit_original_message(content=msg, embeds=[], components=[])
        else:
            await inter.send(f"{inter.author.mention}: {msg}", embeds=[], components=[], ephemeral=True)
            await inter.message.delete()

    async def close_presence(self, inter: Union[disnake.MessageInteraction, disnake.ModalInteraction]):

        for b in self.bot.pool.bots:
            try:
                player: LavalinkPlayer = b.music.players[inter.guild_id]
            except KeyError:
                continue

            try:
                if inter.author.id not in player.guild.me.voice.channel.voice_states:
                    continue
            except AttributeError:
                continue

            stats = {
                "op": "close",
                "bot_id": self.bot.user.id,
                "bot_name": str(self.bot.user),
                "thumb": self.bot.user.display_avatar.replace(size=512, static_format="png").url,
            }

            await player._send_rpc_data([inter.author.id], stats)

def setup(bot: BotCore):

    bot.add_cog(MusicSettings(bot))
    bot.add_cog(RPCCog(bot))

