from __future__ import annotations

import re
import traceback
from pathlib import Path
import time

import disnake
from disnake import FFmpegOpusAudio
from disnake.ext import commands, tasks
import asyncio

from utils.client import BotCore
import aiosqlite

LANGUAGE_LIST = ["English", "Tiếng Việt", "日本語", "русский", "中国人"]
DATA_TTS_DIR = Path("./data_tts")
AUTO_DISCONNECT_DELAY_SECONDS = 30
STALE_TTS_MAX_AGE_SECONDS = 10 * 60

from gtts import gTTS


def check_voice():

    async def predicate(inter):


        guild = inter.guild

        try:
            if not inter.author.voice:
                await inter.send("Bạn chưa vào voice channel")
                return False
        except AttributeError:
            pass

        if not guild.me.voice:

            perms = inter.author.voice.channel.permissions_for(guild.me)

            if not perms.connect:
                await inter.send("Tôi không có quyền kết nối vào kênh này")
                return False

        try:
            if inter.author.id not in guild.me.voice.channel.voice_states:
                return False
        except AttributeError:
            pass

        return True

    return commands.check(predicate)


async def save_lang_tts(guildID, language):
    async with aiosqlite.connect("langDB.sql") as comm:
        cur = await comm.cursor()
        await cur.execute("""INSERT INTO guildLang (guildID, language) VALUES (?, ?)""", (guildID, language))
        await comm.commit()

async def get_tts_lang(guildID):
    async with aiosqlite.connect("langDB.sql") as comm:
            mouse = await comm.cursor()
            await mouse.execute("SELECT language FROM guildLang WHERE guildID = ?", (guildID,))
            data = await mouse.fetchone()
            if not data:
                return "Tiếng Việt"

            return data[0]


async def setup_table() -> None:
    async with aiosqlite.connect("langDB.sql") as comm:
        mouse = await comm.cursor()
        await mouse.execute("""CREATE TABLE IF NOT EXISTS guildLang(
                                                    guildID INTEGER,
                                                    language TEXT DEFAULT 'Tiếng Việt')""")
        await comm.commit()


async def check_lang(lang):
    pattern = r"^[a-z]{2}$"
    return bool(re.match(pattern, lang))


async def convert_language(lang):
    langlist = {"English": "en",
                "Tiếng Việt": "vi",
                "日本語": "ja",
                "русский": "ru",
                "中国人": "zh"
                }
    return langlist.get(lang, "vi")

async def process_tts(text, guild_id, channel_id, lang):
    path = DATA_TTS_DIR / f"{guild_id}"
    file = path / Path(f"{channel_id}_tts.mp3")

    path.mkdir(exist_ok=True, parents=True)

    def _process():
        tts = gTTS(text, lang=lang)
        tts.save(str(file))
    try:
        await asyncio.to_thread(_process)
        return file
    except Exception:
        return None

class SessionManager:
    def __init__(self):
        self.sessions: dict[int, disnake.VoiceClient] = {}
        self.lock = asyncio.Lock()

    async def get_session(self, session_id: int) -> disnake.VoiceClient | None:
        async with self.lock:
            return self.sessions.get(session_id)

    async def delete_session(self, session_id: int):
        async with self.lock:
            self.sessions.pop(session_id, None)

    async def put_session(self, session_id: int, data: disnake.VoiceClient):
        async with self.lock:
            self.sessions[session_id] = data

    async def list_sessions(self) -> list[tuple[int, disnake.VoiceClient]]:
        async with self.lock:
            return list(self.sessions.items())

class TTS(commands.Cog):
    emoji = "🔊"
    name = "Text to speech"
    desc_prefix = f"[{emoji} {name}] | "

    def __init__(self, bot: BotCore):
        self.bot = bot
        self.session_manager = SessionManager()
        self.disconnect_tasks: dict[int, asyncio.Task] = {}
        self.cleanup_stale_tts_files.start()

    @staticmethod
    def _get_tts_file_path(guild_id: int, channel_id: int) -> Path:
        return DATA_TTS_DIR / f"{guild_id}" / f"{channel_id}_tts.mp3"

    async def _delete_tts_file(self, guild_id: int, channel_id: int) -> None:
        file_path = self._get_tts_file_path(guild_id, channel_id)
        try:
            await asyncio.to_thread(file_path.unlink, True)
        except FileNotFoundError:
            return
        except Exception as e:
            self.bot.log.error(f"Failed to delete TTS file {file_path}: {e}")

    async def _cancel_disconnect_task(self, channel_id: int) -> None:
        task = self.disconnect_tasks.pop(channel_id, None)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _disconnect_and_cleanup(self, guild_id: int, channel_id: int) -> None:
        await self._cancel_disconnect_task(channel_id)

        vc = await self.session_manager.get_session(channel_id)
        if vc and vc.is_connected():
            try:
                await vc.disconnect(force=True)
            except Exception as e:
                self.bot.log.error(f"Failed to disconnect voice client in channel {channel_id}: {e}")

        await self.session_manager.delete_session(channel_id)
        await self._delete_tts_file(guild_id, channel_id)

    async def _delayed_disconnect(self, guild_id: int, channel_id: int) -> None:
        try:
            await asyncio.sleep(AUTO_DISCONNECT_DELAY_SECONDS)
            vc = await self.session_manager.get_session(channel_id)
            if not vc or not vc.is_connected() or not vc.channel:
                await self.session_manager.delete_session(channel_id)
                return

            if any(not member.bot for member in vc.channel.members):
                return

            await self._disconnect_and_cleanup(guild_id, channel_id)
        except asyncio.CancelledError:
            return
        except Exception as e:
            self.bot.log.error(f"Delayed disconnect failed for channel {channel_id}: {e}")
        finally:
            active_task = self.disconnect_tasks.get(channel_id)
            if active_task is asyncio.current_task():
                await self.disconnect_tasks.pop(channel_id, None)

    async def _schedule_delayed_disconnect(self, guild_id: int, channel_id: int) -> None:
        await self._cancel_disconnect_task(channel_id)
        self.disconnect_tasks[channel_id] = asyncio.create_task(self._delayed_disconnect(guild_id, channel_id))

    def _on_playback_finished(self, guild_id: int, channel_id: int, err: Exception | None) -> None:
        if err:
            self.bot.log.error(f"Playback failed in channel {channel_id}: {err}")

        loop = self.bot.loop
        if loop.is_closed():
            return

        future = asyncio.run_coroutine_threadsafe(self._delete_tts_file(guild_id, channel_id), loop)

        def _log_future_error(fut):
            try:
                fut.result()
            except Exception as e:
                self.bot.log.error(f"Playback cleanup failed in channel {channel_id}: {e}")

        future.add_done_callback(_log_future_error)

    async def _delete_stale_tts_files(self, max_age_seconds: int = STALE_TTS_MAX_AGE_SECONDS) -> None:
        if not DATA_TTS_DIR.exists():
            return

        active_channels: set[int] = set()
        for channel_id, vc in await self.session_manager.list_sessions():
            if vc.is_connected() and (vc.is_playing() or vc.is_paused()):
                active_channels.add(channel_id)

        now = time.time()
        for file_path in DATA_TTS_DIR.glob("*/*_tts.mp3"):
            try:
                channel_id = int(file_path.stem.removesuffix("_tts"))
            except ValueError:
                continue

            if channel_id in active_channels:
                continue

            try:
                file_age = now - file_path.stat().st_mtime
            except FileNotFoundError:
                continue

            if file_age < max_age_seconds:
                continue

            try:
                await asyncio.to_thread(file_path.unlink, True)
            except Exception as e:
                self.bot.log.error(f"Failed stale cleanup for file {file_path}: {e}")

    async def _disconnect_all_sessions(self) -> None:
        sessions = await self.session_manager.list_sessions()
        for channel_id, vc in sessions:
            guild_id = vc.guild.id
            await self._disconnect_and_cleanup(guild_id, channel_id)

    @tasks.loop(minutes=2)
    async def cleanup_stale_tts_files(self):
        await self._delete_stale_tts_files()

    @cleanup_stale_tts_files.before_loop
    async def before_cleanup_stale_tts_files(self):
        await self.bot.wait_until_ready()

    def cog_unload(self):
        self.cleanup_stale_tts_files.cancel()
        for task in self.disconnect_tasks.values():
            if not task.done():
                task.cancel()
        self.disconnect_tasks.clear()

        if not self.bot.loop.is_closed():
            self.bot.loop.create_task(self._disconnect_all_sessions())

    @commands.Cog.listener("on_ready")
    async def initalize(self):
        try:
            await setup_table()
            await self._delete_stale_tts_files()
        except Exception as e:
            self.bot.log.error(f"TTS initialization failed: {e}")

    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.command(description=f"{desc_prefix}Tạo âm thanh từ văn bản", aliases=["s", "speak"])
    async def say(self, inter: commands.Context, *, content = None):

        if content is None:
            return


        if not inter.author.voice:
            await inter.send("Bạn chưa vào voice channel")
            return

        if not inter.guild.me.voice:

            perms = inter.author.voice.channel.permissions_for(inter.guild.me)

            if not perms.connect:
                await inter.send("Tui không có quyền kết nối vào kênh này")
                return

        if len(content) > 300:
            await inter.send("Bạn đang gửi nội dung dài, sẽ tốn một chút thời gian để bot xử lý...", delete_after=10)

        lang = await get_tts_lang(inter.author.guild.id)
        convlang = await convert_language(lang)

        # Task

        channel = inter.author.voice.channel

        vc = inter.author.guild.voice_client # type: ignore

        if not vc:
            embed = disnake.Embed(description="Đang kết nối vào kênh thoại...", color=disnake.Color.green())
            m = await inter.send(embed=embed)
            try:
                vc: disnake.VoiceClient = await channel.connect(reconnect=False)
            except Exception as e:
                await m.edit(embed=disnake.Embed(description="Đã có lỗi xảy ra khi kết nối vào kênh thoại, hãy báo cáo với quản trị bot!"))
                self.bot.log.error(f"TTS voice connect failed: {e}")
                return
            await m.edit(embed=disnake.Embed(description="Đã kết nối thành công!, hãy ngắt kết nối sau khi đã sử dụng xong"))

        channel_id = inter.guild.me.voice.channel.id
        await self.session_manager.put_session(channel_id, vc)
        await self._cancel_disconnect_task(channel_id)
        guild_id = inter.guild.id

        if vc.is_playing():
            await inter.send("Đang còn người sử dụng, chờ chút nè...", delete_after=10); return

        file = await process_tts(content, guild_id, channel_id, convlang)

        if not file:
            await inter.send("Đã xảy ra lỗi khi tạo mẫu âm thanh nói, hãy báo cáo với quản trị bot")
            return

        try:
            vc.play(FFmpegOpusAudio(str(file)), after=lambda err: self._on_playback_finished(guild_id, channel_id, err))
        except disnake.errors.ClientException as e:
            if "ffmpeg.exe was not found." in str(e) or "ffmpeg was not found." in str(e):
                await inter.send(f"Đã có lỗi xảy ra, vui lòng báo cho chủ sở hữu bot!")
                self.bot.log.error("Không có ffmpeg hoặc hệ thống không hỗ trợ ffmpeg, vui lòng kiểm tra lại")
            return
        except Exception:
            traceback.print_exc()
            await inter.channel.send(f"Không thể phát, đã có một sự cố nào đó xảy ra")

    @commands.command(description=f"{desc_prefix} Ngắt kết nối bot khỏi kênh", aliases=["stoptts"])
    async def tts_stop(self, ctx: disnake.ApplicationCommandInteraction):

        vc = ctx.author.guild.voice_client
        if vc:
            if ctx.author.id not in ctx.guild.me.voice.channel.voice_states:
                await ctx.send("Bạn không ở cùng kênh thoại với bot!.", delete_after=7)
                return

            channel = vc.channel
            if channel:
                await self._disconnect_and_cleanup(ctx.guild.id, channel.id)
            else:
                await vc.disconnect(force=True)
            await ctx.send("Đã ngắt kết nối, cảm ơn đã sử dụng ♥.", delete_after=3)
        else:
            await ctx.channel.send("Không có bot đang được sử dụng trên máy chủ")

    @commands.cooldown(1, 15, commands.BucketType.guild)
    @commands.has_guild_permissions(manage_channels=True)
    @commands.slash_command(name = "tts_language", description=f"{desc_prefix} Change language for tts module", options=[disnake.Option('language', description='Language', required=True)])
    async def tts_language(self, ctx: disnake.ApplicationCommandInteraction, language: str = None):
        if language not in LANGUAGE_LIST:
            await ctx.send("Ngôn ngữ nhập vào không hợp lệ!", ephemeral=True)
            return

        await ctx.response.defer(ephemeral=True)
        await save_lang_tts(ctx.author.guild.id, language)
        await ctx.edit_original_response(f"Language changed to: {language}")

    @tts_language.autocomplete('language')
    async def get_lang(self, inter: disnake.Interaction, lang: str):
        lang = lang.lower()
        if not lang:
            return [lang for lang in LANGUAGE_LIST]

        return [lang for lang in LANGUAGE_LIST if lang.lower() == lang.lower()]

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: disnake.Member, before: disnake.VoiceState, after: disnake.VoiceState):
        # Handle member disconnect from tts channel, to disconnect the bot to save resource
        if before.channel is None:
            return

        if member.bot and self.bot.user and member.id == self.bot.user.id:
            await self.session_manager.delete_session(before.channel.id)
            return

        session = await self.session_manager.get_session(before.channel.id)
        if not session:
            return

        if not session.channel or before.channel.id != session.channel.id:
            return

        if any(not voice_member.bot for voice_member in before.channel.members):
            await self._cancel_disconnect_task(before.channel.id)
            return

        await self._schedule_delayed_disconnect(before.channel.guild.id, before.channel.id)

def setup(bot: BotCore):
    bot.add_cog(TTS(bot))
