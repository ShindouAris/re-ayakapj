# SPDX-License-Identifier: MIT

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING, List, Literal, Optional, Tuple, Type, Union

from .asset import Asset, AssetMixin
from .enums import StickerFormatType, StickerType, try_enum
from .errors import InvalidData
from .mixins import Hashable
from .utils import MISSING, _get_as_snowflake, cached_slot_property, find, get, snowflake_time

__all__ = (
    "StickerPack",
    "StickerItem",
    "Sticker",
    "StandardSticker",
    "GuildSticker",
)

if TYPE_CHECKING:
    import datetime

    from .guild import Guild
    from .state import ConnectionState
    from .types.sticker import (
        EditGuildSticker,
        GuildSticker as GuildStickerPayload,
        ListStickerPacks as ListStickerPacksPayload,
        StandardSticker as StandardStickerPayload,
        Sticker as StickerPayload,
        StickerItem as StickerItemPayload,
        StickerPack as StickerPackPayload,
    )
    from .user import User


class StickerPack(Hashable):
    """Represents a sticker pack.

    .. versionadded:: 2.0

    .. versionchanged:: 2.8
        :attr:`cover_sticker_id`, :attr:`cover_sticker` and :attr:`banner` are now optional.

    .. collapse:: operations

        .. describe:: str(x)

            Returns the name of the sticker pack.

        .. describe:: x == y

           Checks if the sticker pack is equal to another sticker pack.

        .. describe:: x != y

           Checks if the sticker pack is not equal to another sticker pack.

    Attributes
    ----------
    name: :class:`str`
        The name of the sticker pack.
    description: :class:`str`
        The description of the sticker pack.
    id: :class:`int`
        The id of the sticker pack.
    stickers: List[:class:`StandardSticker`]
        The stickers of this sticker pack.
    sku_id: :class:`int`
        The SKU ID of the sticker pack.
    cover_sticker_id: Optional[:class:`int`]
         The ID of the sticker used for the cover of the sticker pack, if any.
    cover_sticker: Optional[:class:`StandardSticker`]
        The sticker used for the cover of the sticker pack, if any.
    """

    __slots__ = (
        "_state",
        "id",
        "stickers",
        "name",
        "sku_id",
        "cover_sticker_id",
        "cover_sticker",
        "description",
        "_banner",
    )

    def __init__(self, *, state: ConnectionState, data: StickerPackPayload) -> None:
        self._state: ConnectionState = state
        self._from_data(data)

    def _from_data(self, data: StickerPackPayload) -> None:
        self.id: int = int(data["id"])
        stickers = data["stickers"]
        self.stickers: List[StandardSticker] = [
            StandardSticker(state=self._state, data=sticker) for sticker in stickers
        ]
        self.name: str = data["name"]
        self.sku_id: int = int(data["sku_id"])
        self.cover_sticker_id: Optional[int] = _get_as_snowflake(data, "cover_sticker_id")
        self.cover_sticker: Optional[StandardSticker] = get(self.stickers, id=self.cover_sticker_id)
        self.description: str = data["description"]
        self._banner: Optional[int] = _get_as_snowflake(data, "banner_asset_id")

    @property
    def banner(self) -> Optional[Asset]:
        """Optional[:class:`Asset`]: The banner asset of the sticker pack, if any."""
        if not self._banner:
            return None
        return Asset._from_sticker_banner(self._state, self._banner)

    def __repr__(self) -> str:
        return f"<StickerPack id={self.id} name={self.name!r} description={self.description!r}>"

    def __str__(self) -> str:
        return self.name


class _StickerTag(Hashable, AssetMixin):
    __slots__ = ()

    id: int
    format: StickerFormatType

    @property
    def url(self) -> str:
        """:class:`str`: The url for the sticker's image."""
        # https://github.com/discord/discord-api-docs/issues/6675#issuecomment-1954755672
        base = Asset.BASE_MEDIA if self.format is StickerFormatType.gif else Asset.BASE
        return f"{base}/stickers/{self.id}.{self.format.file_extension}"

    async def read(self) -> bytes:
        """|coro|

        Retrieves the content of this sticker as a :class:`bytes` object.

        .. note::

            Stickers that use the :attr:`StickerFormatType.lottie` format cannot be read.

        Raises
        ------
        HTTPException
            Downloading the asset failed.
        NotFound
            The asset was deleted.
        TypeError
            The sticker is a lottie type.

        Returns
        -------
        :class:`bytes`
            The content of the asset.
        """
        if self.format is StickerFormatType.lottie:
            raise TypeError('Cannot read stickers of format "lottie".')
        return await super().read()


class StickerItem(_StickerTag):
    """Represents a sticker item.

    .. versionadded:: 2.0

    .. collapse:: operations

        .. describe:: str(x)

            Returns the name of the sticker item.

        .. describe:: x == y

           Checks if the sticker item is equal to another sticker item.

        .. describe:: x != y

           Checks if the sticker item is not equal to another sticker item.

    Attributes
    ----------
    name: :class:`str`
        The sticker's name.
    id: :class:`int`
        The ID of the sticker.
    format: :class:`StickerFormatType`
        The format for the sticker's image.
    """

    __slots__ = ("name", "id", "format")

    def __init__(self, *, state: ConnectionState, data: StickerItemPayload) -> None:
        self._state: ConnectionState = state
        self.name: str = data["name"]
        self.id: int = int(data["id"])
        self.format: StickerFormatType = try_enum(StickerFormatType, data["format_type"])

    def __repr__(self) -> str:
        return f"<StickerItem id={self.id} name={self.name!r} format={self.format}>"

    def __str__(self) -> str:
        return self.name

    async def fetch(self) -> Union[Sticker, StandardSticker, GuildSticker]:
        """|coro|

        Attempts to retrieve the full sticker data of the sticker item.

        Raises
        ------
        HTTPException
            Retrieving the sticker failed.

        Returns
        -------
        Union[:class:`StandardSticker`, :class:`GuildSticker`]
            The retrieved sticker.
        """
        data: StickerPayload = await self._state.http.get_sticker(self.id)
        cls, _ = _sticker_factory(data["type"])  # type: ignore
        return cls(state=self._state, data=data)


class Sticker(_StickerTag):
    """Represents a sticker.

    .. versionadded:: 1.6

    .. collapse:: operations

        .. describe:: str(x)

            Returns the name of the sticker.

        .. describe:: x == y

           Checks if the sticker is equal to another sticker.

        .. describe:: x != y

           Checks if the sticker is not equal to another sticker.

    Attributes
    ----------
    name: :class:`str`
        The sticker's name.
    id: :class:`int`
        The ID of the sticker.
    description: :class:`str`
        The description of the sticker.
    pack_id: :class:`int`
        The ID of the sticker's pack.
    format: :class:`StickerFormatType`
        The format for the sticker's image.
    """

    __slots__ = ("id", "name", "description", "format")

    def __init__(self, *, state: ConnectionState, data: StickerPayload) -> None:
        self._state: ConnectionState = state
        self._from_data(data)

    def _from_data(self, data: StickerPayload) -> None:
        self.id: int = int(data["id"])
        self.name: str = data["name"]
        self.description: str = data.get("description") or ""
        self.format: StickerFormatType = try_enum(StickerFormatType, data["format_type"])

    def __repr__(self) -> str:
        return f"<Sticker id={self.id} name={self.name!r}>"

    def __str__(self) -> str:
        return self.name

    @property
    def created_at(self) -> datetime.datetime:
        """:class:`datetime.datetime`: Returns the sticker's creation time in UTC."""
        return snowflake_time(self.id)


class StandardSticker(Sticker):
    """Represents a sticker that is found in a standard sticker pack.

    .. versionadded:: 2.0

    .. collapse:: operations

        .. describe:: str(x)

            Returns the name of the sticker.

        .. describe:: x == y

           Checks if the sticker is equal to another sticker.

        .. describe:: x != y

           Checks if the sticker is not equal to another sticker.

    Attributes
    ----------
    name: :class:`str`
        The sticker's name.
    id: :class:`int`
        The ID of the sticker.
    description: :class:`str`
        The description of the sticker.
    pack_id: :class:`int`
        The ID of the sticker's pack.
    format: :class:`StickerFormatType`
        The format for the sticker's image.
    tags: List[:class:`str`]
        A list of tags for the sticker.
    sort_value: :class:`int`
        The sticker's sort order within its pack.
    """

    __slots__ = ("sort_value", "pack_id", "type", "tags")

    def _from_data(self, data: StandardStickerPayload) -> None:
        super()._from_data(data)
        self.sort_value: int = data["sort_value"]
        self.pack_id: int = int(data["pack_id"])
        self.type: StickerType = StickerType.standard

        try:
            self.tags: List[str] = [tag.strip() for tag in data["tags"].split(",")]
        except KeyError:
            self.tags = []

    def __repr__(self) -> str:
        return f"<StandardSticker id={self.id} name={self.name!r} pack_id={self.pack_id}>"

    async def pack(self) -> StickerPack:
        """|coro|

        Retrieves the sticker pack that this sticker belongs to.

        Raises
        ------
        InvalidData
            The corresponding sticker pack was not found.
        HTTPException
            Retrieving the sticker pack failed.

        Returns
        -------
        :class:`StickerPack`
            The retrieved sticker pack.
        """
        data: ListStickerPacksPayload = await self._state.http.list_sticker_packs()
        packs = data["sticker_packs"]
        pack = find(lambda d: int(d["id"]) == self.pack_id, packs)

        if pack:
            return StickerPack(state=self._state, data=pack)
        raise InvalidData(f"Could not find corresponding sticker pack for {self!r}")


class GuildSticker(Sticker):
    """Represents a sticker that belongs to a guild.

    .. versionadded:: 2.0

    .. collapse:: operations

        .. describe:: str(x)

            Returns the name of the sticker.

        .. describe:: x == y

           Checks if the sticker is equal to another sticker.

        .. describe:: x != y

           Checks if the sticker is not equal to another sticker.

    Attributes
    ----------
    name: :class:`str`
        The sticker's name.
    id: :class:`int`
        The ID of the sticker.
    description: :class:`str`
        The description of the sticker.
    format: :class:`StickerFormatType`
        The format for the sticker's image.
    available: :class:`bool`
        Whether this sticker is available for use.
    guild_id: :class:`int`
        The ID of the guild that this sticker is from.
    user: Optional[:class:`User`]
        The user that created this sticker. This can only be retrieved using
        :meth:`Guild.fetch_sticker`/:meth:`Guild.fetch_stickers` while
        having the :attr:`~Permissions.manage_guild_expressions` permission.
    emoji: :class:`str`
        The name of a unicode emoji that represents this sticker.
    """

    __slots__ = ("available", "guild_id", "user", "emoji", "type", "_cs_guild")

    def _from_data(self, data: GuildStickerPayload) -> None:
        super()._from_data(data)
        self.available: bool = data.get("available", True)
        self.guild_id: int = int(data["guild_id"])
        user = data.get("user")
        self.user: Optional[User] = self._state.store_user(user) if user else None
        self.emoji: str = data["tags"]
        self.type: StickerType = StickerType.guild

    def __repr__(self) -> str:
        return f"<GuildSticker name={self.name!r} id={self.id} guild_id={self.guild_id} user={self.user!r}>"

    @cached_slot_property("_cs_guild")
    def guild(self) -> Optional[Guild]:
        """Optional[:class:`Guild`]: The guild that this sticker is from.
        Could be ``None`` if the bot is not in the guild.

        .. versionadded:: 2.0
        """
        return self._state._get_guild(self.guild_id)

    async def edit(
        self,
        *,
        name: str = MISSING,
        description: Optional[str] = MISSING,
        emoji: str = MISSING,
        reason: Optional[str] = None,
    ) -> GuildSticker:
        """|coro|

        Edits a :class:`GuildSticker` for the guild.

        You must have :attr:`~Permissions.manage_guild_expressions` permission to
        do this.

        Parameters
        ----------
        name: :class:`str`
            The sticker's new name. Must be at least 2 characters.
        description: Optional[:class:`str`]
            The sticker's new description. Can be ``None``.
        emoji: :class:`str`
            The name of a unicode emoji that represents the sticker's expression.
        reason: Optional[:class:`str`]
            The reason for editing this sticker. Shows up on the audit log.

        Raises
        ------
        Forbidden
            You are not allowed to edit this sticker.
        HTTPException
            An error occurred editing the sticker.

        Returns
        -------
        :class:`GuildSticker`
            The newly modified sticker.
        """
        payload: EditGuildSticker = {}

        if name is not MISSING:
            payload["name"] = name

        if description is not MISSING:
            payload["description"] = description

        if emoji is not MISSING:
            try:
                emoji = unicodedata.name(emoji)
            except TypeError:
                pass
            else:
                emoji = emoji.replace(" ", "_")

            payload["tags"] = emoji

        data: GuildStickerPayload = await self._state.http.modify_guild_sticker(
            self.guild_id, self.id, payload, reason=reason
        )
        return GuildSticker(state=self._state, data=data)

    async def delete(self, *, reason: Optional[str] = None) -> None:
        """|coro|

        Deletes the custom :class:`Sticker` from the guild.

        You must have :attr:`~Permissions.manage_guild_expressions` permission to
        do this.

        Parameters
        ----------
        reason: Optional[:class:`str`]
            The reason for deleting this sticker. Shows up on the audit log.

        Raises
        ------
        Forbidden
            You are not allowed to delete this sticker.
        HTTPException
            An error occurred deleting the sticker.
        """
        await self._state.http.delete_guild_sticker(self.guild_id, self.id, reason=reason)


def _sticker_factory(
    sticker_type: Literal[1, 2],
) -> Tuple[Type[Union[StandardSticker, GuildSticker, Sticker]], StickerType]:
    value = try_enum(StickerType, sticker_type)
    if value == StickerType.standard:
        return StandardSticker, value
    elif value == StickerType.guild:
        return GuildSticker, value
    else:
        return Sticker, value
