# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime
from typing import Optional, Dict, Any, List

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    func,
    JSON,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonType = JSON().with_variant(JSONB, "postgresql")


class Base(DeclarativeBase):
    pass


def default_player_controller() -> Dict[str, Any]:
    return {
        "channel": None,
        "message_id": None,
        "skin": None,
        "static_skin": None,
        "fav_links": {},
        "purge_mode": "on_message",
    }


class GuildConfig(Base):
    __tablename__ = "guild_configs"

    bot_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    ver: Mapped[float] = mapped_column(Float, default=1.10)
    autoplay: Mapped[bool] = mapped_column(Boolean, default=False)
    check_other_bots_in_vc: Mapped[bool] = mapped_column(Boolean, default=False)
    enable_restrict_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    default_player_volume: Mapped[int] = mapped_column(Integer, default=100)
    enable_prefixed_commands: Mapped[bool] = mapped_column(Boolean, default=True)
    djroles: Mapped[List[Any]] = mapped_column(JsonType, default=list)
    player_controller: Mapped[Dict[str, Any]] = mapped_column(
        JsonType, default=default_player_controller
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def to_dict(self) -> Dict[str, Any]:
        ctrl = default_player_controller()
        if self.player_controller and isinstance(self.player_controller, dict):
            ctrl.update(self.player_controller)
            if not isinstance(ctrl.get("fav_links"), dict):
                ctrl["fav_links"] = {}
        return {
            "_id": str(self.guild_id),
            "ver": self.ver,
            "player_controller": ctrl,
            "autoplay": self.autoplay,
            "check_other_bots_in_vc": self.check_other_bots_in_vc,
            "enable_restrict_mode": self.enable_restrict_mode,
            "default_player_volume": self.default_player_volume,
            "enable_prefixed_commands": self.enable_prefixed_commands,
            "djroles": self.djroles if self.djroles is not None else [],
        }

    def update_from_dict(self, data: Dict[str, Any]) -> None:
        if "ver" in data:
            self.ver = float(data["ver"])
        if "player_controller" in data:
            self.player_controller = data["player_controller"]
        if "autoplay" in data:
            self.autoplay = bool(data["autoplay"])
        if "check_other_bots_in_vc" in data:
            self.check_other_bots_in_vc = bool(data["check_other_bots_in_vc"])
        if "enable_restrict_mode" in data:
            self.enable_restrict_mode = bool(data["enable_restrict_mode"])
        if "default_player_volume" in data:
            self.default_player_volume = int(data["default_player_volume"])
        if "enable_prefixed_commands" in data:
            self.enable_prefixed_commands = bool(data["enable_prefixed_commands"])
        if "djroles" in data:
            self.djroles = data["djroles"]


class GuildGlobalConfig(Base):
    __tablename__ = "guild_global_configs"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    ver: Mapped[float] = mapped_column(Float, default=1.4)
    prefix: Mapped[str] = mapped_column(String(20), default="")
    global_skin: Mapped[bool] = mapped_column(Boolean, default=False)
    player_skin: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    player_skin_static: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    voice_channel_status: Mapped[str] = mapped_column(String(255), default="")
    custom_skins: Mapped[Dict[str, Any]] = mapped_column(JsonType, default=dict)
    custom_skins_static: Mapped[Dict[str, Any]] = mapped_column(JsonType, default=dict)
    listen_along_invites: Mapped[Dict[str, Any]] = mapped_column(JsonType, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "_id": str(self.guild_id),
            "ver": self.ver,
            "prefix": self.prefix or "",
            "global_skin": self.global_skin,
            "player_skin": self.player_skin,
            "player_skin_static": self.player_skin_static,
            "voice_channel_status": self.voice_channel_status or "",
            "custom_skins": self.custom_skins if self.custom_skins is not None else {},
            "custom_skins_static": (
                self.custom_skins_static if self.custom_skins_static is not None else {}
            ),
            "listen_along_invites": (
                self.listen_along_invites if self.listen_along_invites is not None else {}
            ),
        }

    def update_from_dict(self, data: Dict[str, Any]) -> None:
        if "ver" in data:
            self.ver = float(data["ver"])
        if "prefix" in data:
            self.prefix = str(data["prefix"]) if data["prefix"] is not None else ""
        if "global_skin" in data:
            self.global_skin = bool(data["global_skin"])
        if "player_skin" in data:
            self.player_skin = data["player_skin"]
        if "player_skin_static" in data:
            self.player_skin_static = data["player_skin_static"]
        if "voice_channel_status" in data:
            self.voice_channel_status = (
                str(data["voice_channel_status"])
                if data["voice_channel_status"] is not None
                else ""
            )
        if "custom_skins" in data:
            self.custom_skins = data["custom_skins"]
        if "custom_skins_static" in data:
            self.custom_skins_static = data["custom_skins_static"]
        if "listen_along_invites" in data:
            self.listen_along_invites = data["listen_along_invites"]


class UserConfig(Base):
    __tablename__ = "user_configs"

    bot_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    ver: Mapped[float] = mapped_column(Float, default=1.0)
    fav_links: Mapped[Dict[str, Any]] = mapped_column(JsonType, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "_id": str(self.user_id),
            "ver": self.ver,
            "fav_links": self.fav_links if self.fav_links is not None else {},
        }

    def update_from_dict(self, data: Dict[str, Any]) -> None:
        if "ver" in data:
            self.ver = float(data["ver"])
        if "fav_links" in data:
            self.fav_links = data["fav_links"]


class UserGlobalConfig(Base):
    __tablename__ = "user_global_configs"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    ver: Mapped[float] = mapped_column(Float, default=1.4)
    token: Mapped[str] = mapped_column(String(255), default="")
    custom_prefix: Mapped[str] = mapped_column(String(20), default="")
    fav_links: Mapped[Dict[str, Any]] = mapped_column(JsonType, default=dict)
    integration_links: Mapped[Dict[str, Any]] = mapped_column(JsonType, default=dict)
    last_tracks: Mapped[List[Any]] = mapped_column(JsonType, default=list)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "_id": str(self.user_id),
            "ver": self.ver,
            "token": self.token or "",
            "custom_prefix": self.custom_prefix or "",
            "fav_links": self.fav_links if self.fav_links is not None else {},
            "integration_links": (
                self.integration_links if self.integration_links is not None else {}
            ),
            "last_tracks": self.last_tracks if self.last_tracks is not None else [],
        }

    def update_from_dict(self, data: Dict[str, Any]) -> None:
        if "ver" in data:
            self.ver = float(data["ver"])
        if "token" in data:
            self.token = str(data["token"]) if data["token"] is not None else ""
        if "custom_prefix" in data:
            self.custom_prefix = (
                str(data["custom_prefix"]) if data["custom_prefix"] is not None else ""
            )
        if "fav_links" in data:
            self.fav_links = data["fav_links"]
        if "integration_links" in data:
            self.integration_links = data["integration_links"]
        if "last_tracks" in data:
            self.last_tracks = data["last_tracks"]


class DefaultConfig(Base):
    __tablename__ = "default_configs"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    ver: Mapped[float] = mapped_column(Float, default=1.0)
    extra_tokens: Mapped[Dict[str, Any]] = mapped_column(JsonType, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "_id": self.id,
            "ver": self.ver,
            "extra_tokens": self.extra_tokens if self.extra_tokens is not None else {},
        }

    def update_from_dict(self, data: Dict[str, Any]) -> None:
        if "ver" in data:
            self.ver = float(data["ver"])
        if "extra_tokens" in data:
            self.extra_tokens = data["extra_tokens"]


class PlayerSession(Base):
    __tablename__ = "player_sessions"

    bot_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    data: Mapped[str] = mapped_column(Text, nullable=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GuildTTSLang(Base):
    __tablename__ = "guild_tts_langs"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    language: Mapped[str] = mapped_column(String(50), default="Tiếng Việt")
