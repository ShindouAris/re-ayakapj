# -*- coding: utf-8 -*-
import datetime
import itertools
from os.path import basename
import disnake

from utils.music.converters import fix_characters, time_format, music_source_image, get_button_style
from utils.music.models import LavalinkPlayer
from utils.others import PlayerControls

class DefaultProgressbarSkin:

    __slots__ = ("name", "preview")

    def __init__(self):
        self.name = basename(__file__)[:-3]
        self.preview = "https://cdn.discordapp.com/attachments/554468640942981147/1047184550230495272/skin_progressbar.png"

    def setup_features(self, player: LavalinkPlayer):
        player.mini_queue_feature = True
        player.controller_mode = True
        player.auto_update = 10
        player.hint_rate = player.bot.config["HINT_RATE"]
        player.static = False

    def load(self, player: LavalinkPlayer) -> dict:

        data = {
            "content": None,
            "embeds": []
        }

        embed = disnake.Embed(color=player.bot.get_color(player.guild.me))
        embed_queue = None

        if not player.paused:
            embed.set_author(
                name=f"Playing from {player.current.info['sourceName']}:",
                icon_url=music_source_image(player.current.info["sourceName"])
            )
        else:
            embed.set_author(
                name="Paused",
                icon_url="https://i.ibb.co/xYj7ysN/pause.png"
            )


        duration1 = "> 🔴 **⠂Current:** `Livestream`\n" if player.current.is_stream else \
            (f"> <:timeout:1155781760571949118> **⠂Current:** `{time_format(player.current.duration)} [`" +
             f"<t:{int((disnake.utils.utcnow() + datetime.timedelta(milliseconds=player.current.duration - player.position)).timestamp())}:R>`]`\n"
             if not player.paused else '')

        vc_txt = ""
        src_name = fix_characters(player.current.info['sourceName'], limit=16)

        match src_name:
            case "spotify":
                s_name = "Spotify"
                src_emoji = "<:spo:1197427989630156843>"
            case "youtube":
                s_name = "YouTube"
                src_emoji = "<:Youtube:1197428387917082735>"
            case "soundcloud":
                s_name = "SoundCloud"
                src_emoji = "<:soundcloud:1197427982499856435>"
            case "dezzer":
                s_name = "Dezzer"
                src_emoji = "<:deezer:1197427994533314600>"
            case "twitch":
                s_name = "Twitch"
                src_emoji = "<:Twitch:1197427999981703238>"
            case "applemusic":
                s_name = "Apple Music"
                src_emoji = "<:applemusic:1232560350449242123>"
            case "http":
                s_name = "HTTP"
                src_emoji = "<:link:1372085354424832000>"
            case _:
                s_name = "Unknown"
                src_emoji = "<:LogoModSystem:1155781711024635934>"




        txt = f"[`{fix_characters(player.current.single_title, limit=21)}`]({player.current.uri})\n\n" \
              f"{duration1}" \
              f"> {src_emoji} **⠂Source:** [`{s_name}`]({player.current.uri})\n" \
              f"> <:author:1140220381320466452> **⠂Author:** {player.current.authors_md}\n" \
              f"> <:volume:1140221293950668820> **⠂Volume:** `{player.volume}%`\n" \
              f"> <:host:1140221179920138330> **⠂MusicServer:** {player}\n" \
              f"> 🌐 **⠂Vùng:** {player.node.region.title()}" \

        if not player.ping:
            txt += f"\n > <a:loading:1204300257874288681> **⠂Retrieving data from the server**"
        else:
            if player.ping in range(0, 100):
                txt += f"\n> <:emoji_57:1173431627607715871> **⠂Latency:** `{player.ping}ms`"
            elif player.ping in range(101, 300):
                txt += f"\n> <:emoji_58:1173431708071247983> **⠂Latency:** `{player.ping}ms`"
            elif player.ping in range(301, 1000):
                txt += f"\n> <:emoji_59:1173431772017590332> **⠂Latency:** `{player.ping}ms`"

        if not player.current.autoplay:
            txt += f"\n> ✋ **⠂Requested by:** <@{player.current.requester}>"
        else:
            try:
                mode = f" [`AutoPlay`]({player.current.info['extra']['related']['uri']})"
            except:
                mode = "`AutoPlay`"
            txt += f"\n> 👍 **⠂Requested by:** {mode}"


        try:
            vc_txt += f"\n> <:star3:1155781751914889236> **⠂Listener:** `{len(player.guild.me.voice.channel.members) - 1}`"
        except AttributeError:
            pass

        try:
            vc_txt += f"\n> 🔊 **⠂Channel:** {player.guild.me.voice.channel.mention}"
        except AttributeError:
            pass

        if player.current.track_loops:
            txt += f"\n> <:loop:1140220877401772092> **⠂Loop:** `{player.current.track_loops}` " \

        if player.loop:
            if player.loop == 'current':
                e = '<:loop:1140220877401772092>'
                m = 'Current'
            else:
                e = '<:loop:1140220877401772092>'
                m = 'Queue'
            txt += f"\n> {e} **⠂LoopMode:** `{m}`"

        if player.nightcore:
            txt += f"\n> <:nightcore:1140227024108130314> **⠂Nightcore:** `enabled`"

        if player.current.album_name:
            txt += f"\n> <:soundcloud:1140277420033843241> **⠂Album:** [`{fix_characters(player.current.album_name, limit=16)}`]({player.current.album_url})"

        if player.current.playlist_name:
            txt += f"\n> <:library:1140220586640019556> **⠂Playlist:** [`{fix_characters(player.current.playlist_name, limit=16)}`]({player.current.playlist_url})"

        if (qlenght:=len(player.queue)) and not player.mini_queue_enabled:
            txt += f"\n> <:musicalbum:1183394320292790332> **⠂Song in queue:** `{qlenght}`"

        if player.keep_connected:
            txt += f"\n> <:247:1140230869643169863> **⠂24/7:** `enabled`"

        if player.restrict_mode:
            txt += f"\n> <:restrictions:1183393857858191451> **⠂Restricted:** `Enable`"

        txt += f"{vc_txt}\n"

        if player.command_log:
            txt += f"> {player.command_log_emoji}``Last interaction``{player.command_log_emoji}\n"
            txt += f"> {player.command_log}\n"

        if qlenght and player.mini_queue_enabled:

            queue_txt = "\n".join(
                f"`{(n + 1):02}) [{time_format(t.duration) if not t.is_stream else '🔴 Livestream'}]` [`{fix_characters(t.title, 38)}`]({t.uri})"
                for n, t in (enumerate(itertools.islice(player.queue, 3)))
            )

            embed_queue = disnake.Embed(title=f"Queue Left:  {qlenght}", color=player.bot.get_color(player.guild.me),
                                        description=f"\n{queue_txt}")

            if not player.loop and not player.keep_connected and not player.paused and not player.current.is_stream:

                queue_duration = 0

                for t in player.queue:
                    if not t.is_stream:
                        queue_duration += t.duration

                if queue_duration:
                    embed_queue.description += f"\n`[⌛ Ends after` <t:{int((disnake.utils.utcnow() + datetime.timedelta(milliseconds=(queue_duration + (player.current.duration if not player.current.is_stream else 0)) - player.position)).timestamp())}:R> `⌛]`"

            embed_queue.set_image(url="https://i.ibb.co/wKwpJZQ/ayakapfp-Banner2.gif")

        embed.description = txt
        # embed.set_image(url=player.current.thumb if player.is_paused == False else "https://i.ibb.co/wKwpJZQ/ayakapfp-Banner2.gif")
        embed.set_thumbnail(url=player.current.thumb)
        embed.set_footer(
            text=f"Chisadin music system || {time_format(player.position)} / {time_format(player.current.duration)}" if not player.current.is_stream else "Chisadin music system || Live" if not player.paused else "Chisadin music system || Paused",
            icon_url="https://i.ibb.co/YtHsQWH/1125034330088034334.webp",
        )

        data["embeds"] = [embed_queue, embed] if embed_queue else [embed]

        data["components"] = [
            disnake.ui.Button(emoji="<:stop:1140221258575925358>", custom_id=PlayerControls.stop, style=disnake.ButtonStyle.red),
            disnake.ui.Button(emoji="⏮️", custom_id=PlayerControls.back, style=disnake.ButtonStyle.green),
            disnake.ui.Button(emoji="⏯️", custom_id=PlayerControls.pause_resume, style=get_button_style(player.paused)),
            disnake.ui.Button(emoji="⏭️", custom_id=PlayerControls.skip, style=disnake.ButtonStyle.green),
            disnake.ui.Button(emoji="<:addsong:1140220013580664853>", custom_id=PlayerControls.add_song, style=disnake.ButtonStyle.green, label="Add Song", disabled= True if player.paused else False),
            disnake.ui.Select(
                placeholder="Another choice:",
                custom_id="musicplayer_dropdown_inter",
                min_values=0, max_values=1,
                options=[
                    disnake.SelectOption(
                        label="Add music", emoji="<:add_music:588172015760965654>",
                        value=PlayerControls.add_song,
                        description="Add a song/playlist to the queue."
                    ),
                    disnake.SelectOption(
                        label="Add to your favorites", emoji="💗",
                        value=PlayerControls.add_favorite,
                        description="Add the current song to your favorites."
                    ),
                    disnake.SelectOption(
                        label="Seek to start", emoji="⏪",
                        value=PlayerControls.seek_to_start,
                        description="Time skip the current song back to 00:00."
                    ),
                    disnake.SelectOption(
                        label="Volume", emoji="🔊",
                        value=PlayerControls.volume,
                        description="Adjust the volume"
                    ),
                    disnake.SelectOption(
                        label="Mix songs in queue", emoji="🔀",
                        value=PlayerControls.shuffle,
                        description="Mix songs in queue."
                    ),
                    disnake.SelectOption(
                        label="Play all played songs again", emoji="🎶",
                        value=PlayerControls.readd,
                        description="Play all played songs again."
                    ),
                    disnake.SelectOption(
                        label="Repeat mode", emoji="🔁",
                        value=PlayerControls.loop_mode,
                        description="Enable/Disable/Repeat Queue."
                    ),
                    disnake.SelectOption(
                        label=("Disable" if player.autoplay else "Enable") + " autopilot mode", emoji="🔄",
                        value=PlayerControls.autoplay,
                        description="The system adds music automatically when the line is empty."
                    ),
                    disnake.SelectOption(
                        label=("Disable" if player.nightcore else "Enable") + " Nightcore effect", emoji="<:nightcore:1140227024108130314>",
                        value=PlayerControls.nightcore,
                        description="Nightcore effect"
                    ),
                    disnake.SelectOption(
                        label=("Disable" if player.restrict_mode else "Enable") + " restricted mode", emoji="🔐",
                        value=PlayerControls.restrict_mode,
                        description="Only DJ/Staff can use restricted commands."
                    ),
                    disnake.SelectOption(
                        label=" Song list", emoji="<:music_queue:703761160679194734>",
                        value=PlayerControls.queue,
                        description="Shows you a list that only you can see"
                    ),
                    disnake.SelectOption(
                        label=("Enable" if not player.keep_connected else "Disable") + " 24/7 mode", emoji="<:247:1140230869643169863>",
                        value=PlayerControls.keep_connected,
                        description="24/7 non-stop running mode."
                    ),
                ]
            ),
        ]

        if player.current.ytid and player.node.lyric_support:
            data["components"][5].options.append(
                disnake.SelectOption(
                    label="Lyrics", emoji="📃",
                    value=PlayerControls.lyrics,
                    description="Get Lyrics"
                )
            )

        if player.mini_queue_feature:
            data["components"][5].options.append(
                disnake.SelectOption(
                    label="Mini playlist", emoji="<:music_queue:703761160679194734>",
                    value=PlayerControls.miniqueue,
                    description="Enable/Disable Player mini playlist."
                )
            )

        if not player.static and not player.has_thread:
            data["components"][5].options.append(
                disnake.SelectOption(
                    label="Topic requests song", emoji="💬",
                    value=PlayerControls.song_request_thread,
                    description="Create a topic/temporary chat to add music just by pointing by name/link."
                )
            )

        return data

def load():
    return DefaultProgressbarSkin()
