# -*- coding: utf-8 -*-
from __future__ import annotations

import collections.abc
import logging
from copy import deepcopy
from typing import TYPE_CHECKING, Union, Optional, List, Dict, Any

import disnake
from disnake.ext import commands
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from utils.database.models import (
    Base,
    GuildConfig,
    GuildGlobalConfig,
    UserConfig,
    UserGlobalConfig,
    DefaultConfig,
    PlayerSession,
    GuildTTSLang,
    default_player_controller,
)

if TYPE_CHECKING:
    from utils.client import BotCore

logger = logging.getLogger(__name__)


class DBModel:
    guilds = "guilds"
    users = "users"
    default = "default"


db_models = {
    DBModel.guilds: {
        "ver": 1.10,
        "player_controller": {
            "channel": None,
            "message_id": None,
            "skin": None,
            "static_skin": None,
            "fav_links": {},
            "purge_mode": "on_message",
        },
        "autoplay": False,
        "check_other_bots_in_vc": False,
        "enable_restrict_mode": False,
        "default_player_volume": 100,
        "enable_prefixed_commands": True,
        "djroles": [],
    },
    DBModel.users: {
        "ver": 1.0,
        "fav_links": {},
    },
}

global_db_models = {
    DBModel.users: {
        "ver": 1.4,
        "fav_links": {},
        "integration_links": {},
        "token": "",
        "custom_prefix": "",
        "last_tracks": [],
    },
    DBModel.guilds: {
        "ver": 1.4,
        "prefix": "",
        "global_skin": False,
        "player_skin": None,
        "player_skin_static": None,
        "voice_channel_status": "",
        "custom_skins": {},
        "custom_skins_static": {},
        "listen_along_invites": {},
    },
    DBModel.default: {"ver": 1.0, "extra_tokens": {}},
}


async def get_prefix(bot: BotCore, message: disnake.Message):
    if str(message.content).startswith((f"<@!{bot.user.id}> ", f"<@{bot.user.id}> ")):
        return commands.when_mentioned(bot, message)

    try:
        user_prefix = bot.pool.user_prefix_cache[message.author.id]
    except KeyError:
        user_data = await bot.get_global_data(message.author.id, db_name=DBModel.users)
        bot.pool.user_prefix_cache[message.author.id] = user_data["custom_prefix"]
        user_prefix = user_data["custom_prefix"]

    if user_prefix and message.content.startswith(user_prefix):
        return user_prefix

    if not message.guild:
        return commands.when_mentioned_or(bot.default_prefix)

    try:
        guild_prefix = bot.pool.guild_prefix_cache[message.guild.id]
    except KeyError:
        data = await bot.get_global_data(message.guild.id, db_name=DBModel.guilds)
        guild_prefix = data.get("prefix")

    if not guild_prefix:
        guild_prefix = bot.config.get("DEFAULT_PREFIX") or "!!"

    return guild_prefix


def update_values(d, u):
    for k, v in u.items():
        if isinstance(v, collections.abc.Mapping):
            sub_d = d.get(k)
            if not isinstance(sub_d, collections.abc.Mapping):
                sub_d = {}
            d[k] = update_values(sub_d, v)
        else:
            d[k] = v
    return d


class PostgresDatabase:
    def __init__(self, database_url: str):
        # Đảm bảo sử dụng asyncpg driver
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif database_url.startswith("postgresql://") and not database_url.startswith(
            "postgresql+asyncpg://"
        ):
            database_url = database_url.replace(
                "postgresql://", "postgresql+asyncpg://", 1
            )

        engine_kwargs = {"pool_pre_ping": True}
        if not database_url.startswith("sqlite"):
            engine_kwargs.update({
                "pool_size": 10,
                "max_overflow": 20,
            })

        self.engine: AsyncEngine = create_async_engine(
            database_url,
            **engine_kwargs,
        )
        self.session_maker = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def init_tables(self):
        """Khởi tạo bảng dự phòng nếu chưa chạy qua alembic upgrade head"""
        try:
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        except Exception as e:
            logger.warning(f"Tạo bảng tự động gặp cảnh báo (có thể đã tồn tại): {e}")

    async def close(self):
        await self.engine.dispose()

    def get_default(
        self, collection: str, db_name: Union[DBModel.guilds, DBModel.users, str]
    ):
        if collection == "global":
            return deepcopy(global_db_models.get(db_name, {}))
        return deepcopy(db_models.get(db_name, {}))

    async def get_data(
        self,
        id_: Union[int, str],
        *,
        db_name: Union[DBModel.guilds, DBModel.users, str],
        collection: str,
        default_model: dict = None,
    ) -> Dict[str, Any]:
        default_dict = (
            deepcopy(default_model.get(db_name, {}))
            if default_model and db_name in default_model
            else self.get_default(collection, db_name)
        )

        async with self.session_maker() as session:
            if collection == "global":
                if db_name == DBModel.guilds:
                    guild_id = int(id_)
                    stmt = select(GuildGlobalConfig).where(
                        GuildGlobalConfig.guild_id == guild_id
                    )
                    res = (await session.execute(stmt)).scalar_one_or_none()
                    if not res:
                        res = GuildGlobalConfig(guild_id=guild_id)
                        session.add(res)
                        await session.commit()
                    data = res.to_dict()

                elif db_name == DBModel.users:
                    user_id = int(id_)
                    stmt = select(UserGlobalConfig).where(
                        UserGlobalConfig.user_id == user_id
                    )
                    res = (await session.execute(stmt)).scalar_one_or_none()
                    if not res:
                        res = UserGlobalConfig(user_id=user_id)
                        session.add(res)
                        await session.commit()
                    data = res.to_dict()

                elif db_name == DBModel.default:
                    key = str(id_)
                    stmt = select(DefaultConfig).where(DefaultConfig.id == key)
                    res = (await session.execute(stmt)).scalar_one_or_none()
                    if not res:
                        res = DefaultConfig(id=key)
                        session.add(res)
                        await session.commit()
                    data = res.to_dict()
                else:
                    data = default_dict.copy()

            else:
                bot_id = str(collection)
                if db_name == DBModel.guilds:
                    guild_id = int(id_)
                    stmt = select(GuildConfig).where(
                        GuildConfig.bot_id == bot_id, GuildConfig.guild_id == guild_id
                    )
                    res = (await session.execute(stmt)).scalar_one_or_none()
                    if not res:
                        res = GuildConfig(
                            bot_id=bot_id,
                            guild_id=guild_id,
                            player_controller=default_player_controller(),
                        )
                        session.add(res)
                        await session.commit()
                    data = res.to_dict()

                elif db_name == DBModel.users:
                    user_id = int(id_)
                    stmt = select(UserConfig).where(
                        UserConfig.bot_id == bot_id, UserConfig.user_id == user_id
                    )
                    res = (await session.execute(stmt)).scalar_one_or_none()
                    if not res:
                        res = UserConfig(bot_id=bot_id, user_id=user_id)
                        session.add(res)
                        await session.commit()
                    data = res.to_dict()
                else:
                    data = default_dict.copy()

        # Cập nhật schema nếu version lệch hoặc điền các trường mặc định còn thiếu
        if default_dict and data.get("ver") != default_dict.get("ver"):
            data = update_values(deepcopy(default_dict), data)
            data["ver"] = default_dict["ver"]
            await self.update_data(
                id_,
                data,
                db_name=db_name,
                collection=collection,
                default_model=default_model,
            )
        elif default_dict:
            data = update_values(deepcopy(default_dict), data)

        data["_id"] = str(id_)
        return data

    async def update_data(
        self,
        id_: Union[int, str],
        data: dict,
        *,
        db_name: Union[DBModel.guilds, DBModel.users, str],
        collection: str,
        default_model: dict = None,
    ) -> Dict[str, Any]:
        data["_id"] = str(id_)

        async with self.session_maker() as session:
            if collection == "player_sessions":
                bot_id = str(db_name)
                guild_id = int(id_)
                session_payload = data.get("data", "")
                stmt = select(PlayerSession).where(
                    PlayerSession.bot_id == bot_id, PlayerSession.guild_id == guild_id
                )
                res = (await session.execute(stmt)).scalar_one_or_none()
                if res:
                    res.data = session_payload
                else:
                    res = PlayerSession(
                        bot_id=bot_id, guild_id=guild_id, data=session_payload
                    )
                    session.add(res)
                await session.commit()
                return data

            if collection == "global":
                if db_name == DBModel.guilds:
                    guild_id = int(id_)
                    stmt = select(GuildGlobalConfig).where(
                        GuildGlobalConfig.guild_id == guild_id
                    )
                    res = (await session.execute(stmt)).scalar_one_or_none()
                    if res:
                        res.update_from_dict(data)
                    else:
                        res = GuildGlobalConfig(guild_id=guild_id)
                        res.update_from_dict(data)
                        session.add(res)
                    await session.commit()
                    return res.to_dict()

                elif db_name == DBModel.users:
                    user_id = int(id_)
                    stmt = select(UserGlobalConfig).where(
                        UserGlobalConfig.user_id == user_id
                    )
                    res = (await session.execute(stmt)).scalar_one_or_none()
                    if res:
                        res.update_from_dict(data)
                    else:
                        res = UserGlobalConfig(user_id=user_id)
                        res.update_from_dict(data)
                        session.add(res)
                    await session.commit()
                    return res.to_dict()

                elif db_name == DBModel.default:
                    key = str(id_)
                    stmt = select(DefaultConfig).where(DefaultConfig.id == key)
                    res = (await session.execute(stmt)).scalar_one_or_none()
                    if res:
                        res.update_from_dict(data)
                    else:
                        res = DefaultConfig(id=key)
                        res.update_from_dict(data)
                        session.add(res)
                    await session.commit()
                    return res.to_dict()

            else:
                bot_id = str(collection)
                if db_name == DBModel.guilds:
                    guild_id = int(id_)
                    stmt = select(GuildConfig).where(
                        GuildConfig.bot_id == bot_id, GuildConfig.guild_id == guild_id
                    )
                    res = (await session.execute(stmt)).scalar_one_or_none()
                    if res:
                        res.update_from_dict(data)
                    else:
                        res = GuildConfig(bot_id=bot_id, guild_id=guild_id)
                        res.update_from_dict(data)
                        session.add(res)
                    await session.commit()
                    return res.to_dict()

                elif db_name == DBModel.users:
                    user_id = int(id_)
                    stmt = select(UserConfig).where(
                        UserConfig.bot_id == bot_id, UserConfig.user_id == user_id
                    )
                    res = (await session.execute(stmt)).scalar_one_or_none()
                    if res:
                        res.update_from_dict(data)
                    else:
                        res = UserConfig(bot_id=bot_id, user_id=user_id)
                        res.update_from_dict(data)
                        session.add(res)
                    await session.commit()
                    return res.to_dict()

        return data

    async def query_data(
        self, db_name: str, collection: str, filter: dict = None, limit=500
    ) -> List[Dict[str, Any]]:
        async with self.session_maker() as session:
            if collection == "player_sessions":
                bot_id = str(db_name)
                stmt = (
                    select(PlayerSession)
                    .where(PlayerSession.bot_id == bot_id)
                    .limit(limit)
                )
                records = (await session.execute(stmt)).scalars().all()
                return [{"_id": str(r.guild_id), "data": r.data} for r in records]

            if collection == "global":
                if db_name == DBModel.guilds:
                    stmt = select(GuildGlobalConfig).limit(limit)
                    records = (await session.execute(stmt)).scalars().all()
                    return [r.to_dict() for r in records]
                elif db_name == DBModel.users:
                    stmt = select(UserGlobalConfig).limit(limit)
                    records = (await session.execute(stmt)).scalars().all()
                    return [r.to_dict() for r in records]
                elif db_name == DBModel.default:
                    stmt = select(DefaultConfig).limit(limit)
                    records = (await session.execute(stmt)).scalars().all()
                    return [r.to_dict() for r in records]
            else:
                bot_id = str(collection)
                if db_name == DBModel.guilds:
                    stmt = (
                        select(GuildConfig)
                        .where(GuildConfig.bot_id == bot_id)
                        .limit(limit)
                    )
                    records = (await session.execute(stmt)).scalars().all()
                    return [r.to_dict() for r in records]
                elif db_name == DBModel.users:
                    stmt = (
                        select(UserConfig)
                        .where(UserConfig.bot_id == bot_id)
                        .limit(limit)
                    )
                    records = (await session.execute(stmt)).scalars().all()
                    return [r.to_dict() for r in records]

        return []

    async def delete_data(self, id_: Union[int, str], db_name: str, collection: str):
        async with self.session_maker() as session:
            if collection == "player_sessions":
                bot_id = str(db_name)
                guild_id = int(id_)
                stmt = delete(PlayerSession).where(
                    PlayerSession.bot_id == bot_id, PlayerSession.guild_id == guild_id
                )
                await session.execute(stmt)
                await session.commit()
                return

            if collection == "global":
                if db_name == DBModel.guilds:
                    stmt = delete(GuildGlobalConfig).where(
                        GuildGlobalConfig.guild_id == int(id_)
                    )
                elif db_name == DBModel.users:
                    stmt = delete(UserGlobalConfig).where(
                        UserGlobalConfig.user_id == int(id_)
                    )
                elif db_name == DBModel.default:
                    stmt = delete(DefaultConfig).where(DefaultConfig.id == str(id_))
                else:
                    return
            else:
                bot_id = str(collection)
                if db_name == DBModel.guilds:
                    stmt = delete(GuildConfig).where(
                        GuildConfig.bot_id == bot_id, GuildConfig.guild_id == int(id_)
                    )
                elif db_name == DBModel.users:
                    stmt = delete(UserConfig).where(
                        UserConfig.bot_id == bot_id, UserConfig.user_id == int(id_)
                    )
                else:
                    return

            await session.execute(stmt)
            await session.commit()

    async def get_secret_data(
        self, id_: int, db_name: Union[DBModel.users, DBModel.guilds, DBModel.default]
    ):
        return await self.get_data(
            id_=id_,
            db_name=db_name,
            collection="global",
            default_model=global_db_models,
        )

    # Helper cho module TTS
    async def get_tts_lang(self, guild_id: int) -> str:
        async with self.session_maker() as session:
            stmt = select(GuildTTSLang).where(GuildTTSLang.guild_id == int(guild_id))
            res = (await session.execute(stmt)).scalar_one_or_none()
            if not res or not res.language:
                return "Tiếng Việt"
            return res.language

    async def save_tts_lang(self, guild_id: int, language: str) -> None:
        async with self.session_maker() as session:
            stmt = select(GuildTTSLang).where(GuildTTSLang.guild_id == int(guild_id))
            res = (await session.execute(stmt)).scalar_one_or_none()
            if res:
                res.language = language
            else:
                res = GuildTTSLang(guild_id=int(guild_id), language=language)
                session.add(res)
            await session.commit()