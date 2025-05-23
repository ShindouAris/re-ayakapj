# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import datetime
import io
import re
from base64 import b64decode, b64encode
from os import PathLike
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ClassVar,
    Dict,
    List,
    Optional,
    Tuple,
    Union,
    cast,
    overload,
)

from . import utils
from .channel import PartialMessageable
from .components import (
    MessageTopLevelComponent,
    _message_component_factory,
)
from .embeds import Embed
from .emoji import Emoji
from .enums import (
    ChannelType,
    InteractionType,
    MessageReferenceType,
    MessageType,
    try_enum,
    try_enum_to_int,
)
from .errors import HTTPException
from .file import File
from .flags import AttachmentFlags, MessageFlags
from .guild import Guild
from .member import Member
from .mixins import Hashable
from .partial_emoji import PartialEmoji
from .poll import Poll
from .reaction import Reaction
from .sticker import StickerItem
from .threads import Thread
from .ui.action_row import components_to_dict
from .user import User
from .utils import MISSING, _get_as_snowflake, assert_never, deprecated, escape_mentions

if TYPE_CHECKING:
    from typing_extensions import Self

    from .abc import GuildChannel, MessageableChannel, Snowflake
    from .channel import DMChannel, GroupChannel
    from .guild import GuildMessageable
    from .mentions import AllowedMentions
    from .role import Role
    from .state import ConnectionState
    from .threads import AnyThreadArchiveDuration
    from .types.components import (
        MessageTopLevelComponent as MessageTopLevelComponentPayload,
    )
    from .types.embed import Embed as EmbedPayload
    from .types.gateway import (
        MessageReactionAddEvent,
        MessageReactionRemoveEvent,
        MessageUpdateEvent,
    )
    from .types.interactions import (
        AuthorizingIntegrationOwners as AuthorizingIntegrationOwnersPayload,
        InteractionMessageReference as InteractionMessageReferencePayload,
        InteractionMetadata as InteractionMetadataPayload,
    )
    from .types.member import Member as MemberPayload, UserWithMember as UserWithMemberPayload
    from .types.message import (
        Attachment as AttachmentPayload,
        ForwardedMessage as ForwardedMessagePayload,
        Message as MessagePayload,
        MessageActivity as MessageActivityPayload,
        MessageApplication as MessageApplicationPayload,
        MessageReference as MessageReferencePayload,
        Reaction as ReactionPayload,
        RoleSubscriptionData as RoleSubscriptionDataPayload,
    )
    from .types.threads import ThreadArchiveDurationLiteral
    from .types.user import User as UserPayload
    from .ui._types import MessageComponentInput
    from .ui.view import View

    EmojiInputType = Union[Emoji, PartialEmoji, str]

__all__ = (
    "Attachment",
    "Message",
    "PartialMessage",
    "DeletedReferencedMessage",
    "MessageReference",
    "InteractionReference",
    "InteractionMetadata",
    "AuthorizingIntegrationOwners",
    "RoleSubscriptionData",
    "ForwardedMessage",
)


def convert_emoji_reaction(emoji: Union[EmojiInputType, Reaction]) -> str:
    if isinstance(emoji, Reaction):
        emoji = emoji.emoji

    if isinstance(emoji, Emoji):
        return f"{emoji.name}:{emoji.id}"
    if isinstance(emoji, PartialEmoji):
        return emoji._as_reaction()
    if isinstance(emoji, str):
        # Reactions must be in name:id format, not <:name:id> and/or with the `a:` prefix.
        # No existing emojis start/end with `<>` or `:`, so this should be okay.

        s = emoji.strip("<>:")
        # `str.removeprefix` is py 3.9 only
        if s.startswith("a:"):
            s = s[2:]
        return s

    assert_never(emoji)
    raise TypeError(
        f"emoji argument must be str, Emoji, PartialEmoji, or Reaction, not {emoji.__class__.__name__}."
    )


async def _edit_handler(
    msg: Union[Message, PartialMessage],
    *,
    default_flags: int,
    previous_allowed_mentions: Optional[AllowedMentions],
    delete_after: Optional[float],
    # these are the actual edit kwargs,
    # all of which can be set to `MISSING`
    content: Optional[str],
    embed: Optional[Embed],
    embeds: List[Embed],
    file: File,
    files: List[File],
    attachments: Optional[List[Attachment]],
    suppress: bool,  # deprecated
    suppress_embeds: bool,
    flags: MessageFlags,
    allowed_mentions: Optional[AllowedMentions],
    view: Optional[View],
    components: Optional[MessageComponentInput],
) -> Message:
    if embed is not MISSING and embeds is not MISSING:
        raise TypeError("Cannot mix embed and embeds keyword arguments.")
    if file is not MISSING and files is not MISSING:
        raise TypeError("Cannot mix file and files keyword arguments.")
    if view is not MISSING and components is not MISSING:
        raise TypeError("Cannot mix view and components keyword arguments.")
    if suppress is not MISSING:
        suppress_deprecated_msg = "'suppress' is deprecated in favour of 'suppress_embeds'."
        if suppress_embeds is not MISSING:
            raise TypeError(
                "Cannot mix suppress and suppress_embeds keyword arguments.\n"
                + suppress_deprecated_msg
            )
        utils.warn_deprecated(suppress_deprecated_msg, stacklevel=3)
        suppress_embeds = suppress

    payload: Dict[str, Any] = {}
    if content is not MISSING:
        if content is not None:
            payload["content"] = str(content)
        else:
            payload["content"] = None

    if file is not MISSING:
        files = [file]

    if embed is not MISSING:
        embeds = [embed] if embed else []
    if embeds is not MISSING:
        payload["embeds"] = [e.to_dict() for e in embeds]
        for embed in embeds:
            if embed._files:
                files = files or []
                files.extend(embed._files.values())

    if suppress_embeds is not MISSING:
        flags = MessageFlags._from_value(default_flags if flags is MISSING else flags.value)
        flags.suppress_embeds = suppress_embeds
    if flags is not MISSING:
        payload["flags"] = flags.value

    if allowed_mentions is MISSING:
        if previous_allowed_mentions:
            payload["allowed_mentions"] = previous_allowed_mentions.to_dict()
    else:
        if allowed_mentions:
            if msg._state.allowed_mentions is not None:
                payload["allowed_mentions"] = msg._state.allowed_mentions.merge(
                    allowed_mentions
                ).to_dict()
            else:
                payload["allowed_mentions"] = allowed_mentions.to_dict()

    if attachments is not MISSING:
        payload["attachments"] = [] if attachments is None else [a.to_dict() for a in attachments]

    if view is not MISSING:
        msg._state.prevent_view_updates_for(msg.id)
        if view:
            payload["components"] = view.to_components()
        else:
            payload["components"] = []

    if components is not MISSING:
        payload["components"] = [] if components is None else components_to_dict(components)

    try:
        data = await msg._state.http.edit_message(msg.channel.id, msg.id, **payload, files=files)
    finally:
        if files:
            for f in files:
                f.close()
    message = Message(state=msg._state, channel=msg.channel, data=data)

    if view and not view.is_finished():
        msg._state.store_view(view, msg.id)

    if delete_after is not None:
        await msg.delete(delay=delete_after)

    return message


class Attachment(Hashable):
    """Represents an attachment from Discord.

    .. collapse:: operations

        .. describe:: str(x)

            Returns the URL of the attachment.

        .. describe:: x == y

            Checks if the attachment is equal to another attachment.

        .. describe:: x != y

            Checks if the attachment is not equal to another attachment.

        .. describe:: hash(x)

            Returns the hash of the attachment.

    .. versionchanged:: 1.7
        Attachment can now be casted to :class:`str` and is hashable.

    Attributes
    ----------
    id: :class:`int`
        The attachment's ID.
    size: :class:`int`
        The attachment's size in bytes.
    height: Optional[:class:`int`]
        The attachment's height, in pixels. Only applicable to images and videos.
    width: Optional[:class:`int`]
        The attachment's width, in pixels. Only applicable to images and videos.
    filename: :class:`str`
        The attachment's filename.
    title: Optional[:class:`str`]
        The attachment title. If the filename contained special characters,
        this will be set to the original filename, without filename extension.

        .. versionadded:: 2.10

    url: :class:`str`
        The attachment URL. If the message this attachment was attached
        to is deleted, then this will 404.
    proxy_url: :class:`str`
        The proxy URL. This is a cached version of the :attr:`~Attachment.url` in the
        case of images. When the message is deleted, this URL might be valid for a few
        minutes or not valid at all.
    content_type: Optional[:class:`str`]
        The attachment's `media type <https://en.wikipedia.org/wiki/Media_type>`_.

        .. versionadded:: 1.7

    ephemeral: :class:`bool`
        Whether the attachment is ephemeral.

        .. versionadded:: 2.1

    description: :class:`str`
        The attachment's description.

        .. versionadded:: 2.3

    duration: Optional[:class:`float`]
        The duration of the audio attachment in seconds, if this is attached to a voice message
        (see :attr:`MessageFlags.is_voice_message`).

        .. versionadded:: 2.9

    waveform: Optional[:class:`bytes`]
        The byte array representing a sampled waveform, if this is attached to a voice message
        (see :attr:`MessageFlags.is_voice_message`).

        .. versionadded:: 2.9
    """

    __slots__ = (
        "id",
        "size",
        "height",
        "width",
        "filename",
        "title",
        "url",
        "proxy_url",
        "_http",
        "content_type",
        "ephemeral",
        "description",
        "duration",
        "waveform",
        "_flags",
    )

    def __init__(self, *, data: AttachmentPayload, state: ConnectionState) -> None:
        self.id: int = int(data["id"])
        self.size: int = data["size"]
        self.height: Optional[int] = data.get("height")
        self.width: Optional[int] = data.get("width")
        self.filename: str = data["filename"]
        self.title: Optional[str] = data.get("title")
        self.url: str = data["url"]
        self.proxy_url: str = data["proxy_url"]
        self._http = state.http
        self.content_type: Optional[str] = data.get("content_type")
        self.ephemeral: bool = data.get("ephemeral", False)
        self.description: Optional[str] = data.get("description")
        self.duration: Optional[float] = data.get("duration_secs")
        self.waveform: Optional[bytes] = (
            b64decode(waveform_data) if (waveform_data := data.get("waveform")) else None
        )
        self._flags: int = data.get("flags", 0)

    def is_spoiler(self) -> bool:
        """Whether this attachment contains a spoiler.

        :return type: :class:`bool`
        """
        return self.filename.startswith("SPOILER_")

    def __repr__(self) -> str:
        return f"<Attachment id={self.id} filename={self.filename!r} url={self.url!r} ephemeral={self.ephemeral!r}>"

    def __str__(self) -> str:
        return self.url or ""

    @property
    def flags(self) -> AttachmentFlags:
        """:class:`AttachmentFlags`: Returns the attachment's flags.

        .. versionadded:: 2.10
        """
        return AttachmentFlags._from_value(self._flags)

    async def save(
        self,
        fp: Union[io.BufferedIOBase, PathLike],
        *,
        seek_begin: bool = True,
        use_cached: bool = False,
    ) -> int:
        """|coro|

        Saves this attachment into a file-like object.

        Parameters
        ----------
        fp: Union[:class:`io.BufferedIOBase`, :class:`os.PathLike`]
            The file-like object to save this attachment to or the filename
            to use. If a filename is passed then a file is created with that
            filename and used instead.
        seek_begin: :class:`bool`
            Whether to seek to the beginning of the file after saving is
            successfully done.
        use_cached: :class:`bool`
            Whether to use :attr:`proxy_url` rather than :attr:`url` when downloading
            the attachment. This will allow attachments to be saved after deletion
            more often, compared to the regular URL which is generally deleted right
            after the message is deleted. Note that this can still fail to download
            deleted attachments if too much time has passed and it does not work
            on some types of attachments.

        Raises
        ------
        HTTPException
            Saving the attachment failed.
        NotFound
            The attachment was deleted.

        Returns
        -------
        :class:`int`
            The number of bytes written.
        """
        data = await self.read(use_cached=use_cached)
        if isinstance(fp, io.BufferedIOBase):
            written = fp.write(data)
            if seek_begin:
                fp.seek(0)
            return written
        else:
            with open(fp, "wb") as f:
                return f.write(data)

    async def read(self, *, use_cached: bool = False) -> bytes:
        """|coro|

        Retrieves the content of this attachment as a :class:`bytes` object.

        .. versionadded:: 1.1

        Parameters
        ----------
        use_cached: :class:`bool`
            Whether to use :attr:`proxy_url` rather than :attr:`url` when downloading
            the attachment. This will allow attachments to be saved after deletion
            more often, compared to the regular URL which is generally deleted right
            after the message is deleted. Note that this can still fail to download
            deleted attachments if too much time has passed and it does not work
            on some types of attachments.

        Raises
        ------
        HTTPException
            Downloading the attachment failed.
        Forbidden
            You do not have permissions to access this attachment
        NotFound
            The attachment was deleted.

        Returns
        -------
        :class:`bytes`
            The contents of the attachment.
        """
        url = self.proxy_url if use_cached else self.url
        data = await self._http.get_from_cdn(url)
        return data

    async def to_file(
        self,
        *,
        use_cached: bool = False,
        spoiler: bool = False,
        description: Optional[str] = MISSING,
    ) -> File:
        """|coro|

        Converts the attachment into a :class:`File` suitable for sending via
        :meth:`abc.Messageable.send`.

        .. versionadded:: 1.3

        Parameters
        ----------
        use_cached: :class:`bool`
            Whether to use :attr:`proxy_url` rather than :attr:`url` when downloading
            the attachment. This will allow attachments to be saved after deletion
            more often, compared to the regular URL which is generally deleted right
            after the message is deleted. Note that this can still fail to download
            deleted attachments if too much time has passed and it does not work
            on some types of attachments.

            .. versionadded:: 1.4

        spoiler: :class:`bool`
            Whether the file is a spoiler.

            .. versionadded:: 1.4

        description: Optional[:class:`str`]
            The file's description. Copies this attachment's description by default,
            set to ``None`` to remove.

            .. versionadded:: 2.3

        Raises
        ------
        HTTPException
            Downloading the attachment failed.
        Forbidden
            You do not have permissions to access this attachment
        NotFound
            The attachment was deleted.

        Returns
        -------
        :class:`File`
            The attachment as a file suitable for sending.
        """
        if description is MISSING:
            description = self.description
        data = await self.read(use_cached=use_cached)
        return File(
            io.BytesIO(data), filename=self.filename, spoiler=spoiler, description=description
        )

    def to_dict(self) -> AttachmentPayload:
        result: AttachmentPayload = {
            "filename": self.filename,
            "id": self.id,
            "proxy_url": self.proxy_url,
            "size": self.size,
            "url": self.url,
            "ephemeral": self.ephemeral,
        }
        if self.height:
            result["height"] = self.height
        if self.width:
            result["width"] = self.width
        if self.content_type:
            result["content_type"] = self.content_type
        if self.description:
            result["description"] = self.description
        if self.duration is not None:
            result["duration_secs"] = self.duration
        if self.waveform is not None:
            result["waveform"] = b64encode(self.waveform).decode("ascii")
        if self._flags:
            result["flags"] = self._flags
        if self.title:
            result["title"] = self.title
        return result


class DeletedReferencedMessage:
    """A special sentinel type that denotes whether the
    resolved message referenced message had since been deleted.

    The purpose of this class is to separate referenced messages that could not be
    fetched and those that were previously fetched but have since been deleted.

    .. versionadded:: 1.6
    """

    __slots__ = ("_parent",)

    def __init__(self, parent: MessageReference) -> None:
        self._parent: MessageReference = parent

    def __repr__(self) -> str:
        return f"<DeletedReferencedMessage id={self.id} channel_id={self.channel_id} guild_id={self.guild_id!r}>"

    @property
    def id(self) -> int:
        """:class:`int`: The message ID of the deleted referenced message."""
        # the parent's message id won't be None here
        return self._parent.message_id  # type: ignore

    @property
    def channel_id(self) -> int:
        """:class:`int`: The channel ID of the deleted referenced message."""
        return self._parent.channel_id

    @property
    def guild_id(self) -> Optional[int]:
        """Optional[:class:`int`]: The guild ID of the deleted referenced message."""
        return self._parent.guild_id


class MessageReference:
    """Represents a reference to a :class:`~disnake.Message`.

    .. versionadded:: 1.5

    .. versionchanged:: 1.6
        This class can now be constructed by users.

    Attributes
    ----------
    type: :class:`MessageReferenceType`
        The type of the message reference.

        .. versionadded:: 2.10

    message_id: Optional[:class:`int`]
        The ID of the message referenced/forwarded.
    channel_id: :class:`int`
        The channel ID of the message referenced/forwarded.
    guild_id: Optional[:class:`int`]
        The guild ID of the message referenced/forwarded.
    fail_if_not_exists: :class:`bool`
        Whether replying to the referenced message should raise :class:`HTTPException`
        if the message no longer exists or Discord could not fetch the message.

        .. versionadded:: 1.7

    resolved: Optional[Union[:class:`Message`, :class:`DeletedReferencedMessage`]]
        The message that this reference resolved to. If this is ``None``
        then the original message was not fetched either due to the Discord API
        not attempting to resolve it or it not being available at the time of creation.
        If the message was resolved at a prior point but has since been deleted then
        this will be of type :class:`DeletedReferencedMessage`.

        Currently, this is mainly the replied to message when a user replies to a message.

        .. versionadded:: 1.6
    """

    __slots__ = (
        "type",
        "message_id",
        "channel_id",
        "guild_id",
        "fail_if_not_exists",
        "resolved",
        "_state",
    )

    def __init__(
        self,
        *,
        type: MessageReferenceType = MessageReferenceType.default,
        message_id: int,
        channel_id: int,
        guild_id: Optional[int] = None,
        fail_if_not_exists: bool = True,
    ) -> None:
        self._state: Optional[ConnectionState] = None
        self.resolved: Optional[Union[Message, DeletedReferencedMessage]] = None
        self.type: MessageReferenceType = type
        self.message_id: Optional[int] = message_id
        self.channel_id: int = channel_id
        self.guild_id: Optional[int] = guild_id
        self.fail_if_not_exists: bool = fail_if_not_exists

    @classmethod
    def with_state(cls, state: ConnectionState, data: MessageReferencePayload) -> Self:
        self = cls.__new__(cls)
        # if the type is not present in the message reference object returned by the API
        # we assume automatically that it's a DEFAULT (aka message reply) message reference
        self.type = try_enum(MessageReferenceType, data.get("type", 0))
        self.message_id = utils._get_as_snowflake(data, "message_id")
        self.channel_id = int(data["channel_id"])
        self.guild_id = utils._get_as_snowflake(data, "guild_id")
        self.fail_if_not_exists = data.get("fail_if_not_exists", True)
        self._state = state
        self.resolved = None
        return self

    @classmethod
    def from_message(
        cls,
        message: Message,
        *,
        type: MessageReferenceType = MessageReferenceType.default,
        fail_if_not_exists: bool = True,
    ) -> Self:
        """Creates a :class:`MessageReference` from an existing :class:`~disnake.Message`.

        .. versionadded:: 1.6

        Parameters
        ----------
        message: :class:`~disnake.Message`
            The message to be converted into a reference.
        type: :class:`MessageReferenceType`
            The type of the message reference. This is used to control whether to reply to
            or forward a message. Defaults to replying.

            .. versionadded:: 2.10

        fail_if_not_exists: :class:`bool`
            Whether replying to the referenced message should raise :class:`HTTPException`
            if the message no longer exists or Discord could not fetch the message.

            .. versionadded:: 1.7

        Returns
        -------
        :class:`MessageReference`
            A reference to the message.
        """
        self = cls(
            type=type,
            message_id=message.id,
            channel_id=message.channel.id,
            guild_id=getattr(message.guild, "id", None),
            fail_if_not_exists=fail_if_not_exists,
        )
        self._state = message._state
        return self

    @property
    def cached_message(self) -> Optional[Message]:
        """Optional[:class:`~disnake.Message`]: The cached message, if found in the internal message cache."""
        return self._state and self._state._get_message(self.message_id)

    @property
    def jump_url(self) -> str:
        """:class:`str`: Returns a URL that allows the client to jump to the referenced message.

        .. versionadded:: 1.7
        """
        guild_id = self.guild_id if self.guild_id is not None else "@me"
        return f"https://discord.com/channels/{guild_id}/{self.channel_id}/{self.message_id}"

    def __repr__(self) -> str:
        return f"<MessageReference type={self.type!r} message_id={self.message_id!r} channel_id={self.channel_id!r} guild_id={self.guild_id!r}>"

    def to_dict(self) -> MessageReferencePayload:
        result: MessageReferencePayload = {
            "type": self.type.value,
            "channel_id": self.channel_id,
            "fail_if_not_exists": self.fail_if_not_exists,
        }
        if self.message_id is not None:
            result["message_id"] = self.message_id
        if self.guild_id is not None:
            result["guild_id"] = self.guild_id
        return result

    to_message_reference_dict = to_dict


class InteractionReference:
    """Represents an interaction being referenced in a message.

    This means responses to message components do not include this property,
    instead including a message reference object as components always exist on preexisting messages.

    .. versionadded:: 2.1

    .. deprecated:: 2.10
        Use :attr:`Message.interaction_metadata` instead.

    Attributes
    ----------
    id: :class:`int`
        The ID of the interaction.
    type: :class:`InteractionType`
        The type of interaction.
    name: :class:`str`
        The name of the application command, including group and subcommand name if applicable
        (separated by spaces).

        .. note::

            For interaction references created before July 18th, 2022, this will not include group or subcommand names.

    user: Union[:class:`User`, :class:`Member`]
        The user or member that triggered the referenced interaction.

        .. versionchanged:: 2.10
            This is now a :class:`Member` when in a guild, if the message was received via a
            gateway event or the member is cached.
    """

    __slots__ = ("id", "type", "name", "user")

    def __init__(
        self,
        *,
        state: ConnectionState,
        guild: Optional[Guild],
        data: InteractionMessageReferencePayload,
    ) -> None:
        self.id: int = int(data["id"])
        self.type: InteractionType = try_enum(InteractionType, int(data["type"]))
        self.name: str = data["name"]

        user: Optional[Union[User, Member]] = None
        if guild:
            if isinstance(guild, Guild):  # this can be a placeholder object in interactions
                user = guild.get_member(int(data["user"]["id"]))

            # If not cached, try data from event.
            # This is only available via gateway (message_create/_edit), not HTTP
            if not user and (member := data.get("member")):
                user = Member(data=member, user_data=data["user"], guild=guild, state=state)

        # If still none, deserialize user
        if not user:
            user = state.store_user(data["user"])

        self.user: Union[User, Member] = user

    def __repr__(self) -> str:
        return f"<InteractionReference id={self.id!r} type={self.type!r} name={self.name!r} user={self.user!r}>"

    @property
    def author(self) -> Union[User, Member]:
        return self.user


class InteractionMetadata:
    """Represents metadata about the interaction that caused a particular message.

    .. versionadded:: 2.10

    Attributes
    ----------
    id: :class:`int`
        The ID of the interaction.
    type: :class:`InteractionType`
        The type of the interaction.
    user: :class:`User`
        The user that triggered the interaction.
    authorizing_integration_owners: :class:`AuthorizingIntegrationOwners`
        Details about the authorizing user/guild for the application installation
        related to the interaction.
    original_response_message_id: Optional[:class:`int`]
        The ID of the original response message.
        Only present on :attr:`~Interaction.followup` messages.

    target_user: Optional[:class:`User`]
        The ID of the message the command was run on.
        Only present on interactions of :attr:`ApplicationCommandType.message` commands.
    target_message_id: Optional[:class:`int`]
        The user the command was run on.
        Only present on interactions of :attr:`ApplicationCommandType.user` commands.

    interacted_message_id: Optional[:class:`int`]
        The ID of the message containing the component.
        Only present on :attr:`InteractionType.component` interactions.

    triggering_interaction_metadata: Optional[:class:`InteractionMetadata`]
        The metadata of the original interaction that triggered the modal.
        Only present on :attr:`InteractionType.modal_submit` interactions.
    """

    __slots__ = (
        "id",
        "type",
        "user",
        "authorizing_integration_owners",
        "original_response_message_id",
        "target_user",
        "target_message_id",
        "interacted_message_id",
        "triggering_interaction_metadata",
    )

    def __init__(self, *, state: ConnectionState, data: InteractionMetadataPayload) -> None:
        self.id: int = int(data["id"])
        self.type: InteractionType = try_enum(InteractionType, int(data["type"]))
        self.user: User = state.create_user(data["user"])
        self.authorizing_integration_owners: AuthorizingIntegrationOwners = (
            AuthorizingIntegrationOwners(data.get("authorizing_integration_owners") or {})
        )

        # followup only
        self.original_response_message_id: Optional[int] = _get_as_snowflake(
            data, "original_response_message_id"
        )

        # application command/type 2 only
        self.target_user: Optional[User] = (
            state.create_user(target_user) if (target_user := data.get("target_user")) else None
        )
        self.target_message_id: Optional[int] = _get_as_snowflake(data, "target_message_id")

        # component/type 3 only
        self.interacted_message_id: Optional[int] = _get_as_snowflake(data, "interacted_message_id")

        # modal_submit/type 5 only
        self.triggering_interaction_metadata: Optional[InteractionMetadata] = (
            InteractionMetadata(state=state, data=metadata)
            if (metadata := data.get("triggering_interaction_metadata"))
            else None
        )


class AuthorizingIntegrationOwners:
    """Represents details about the authorizing guild/user for the application installation
    related to an interaction.

    See the :ddocs:`official docs <interactions/receiving-and-responding#interaction-object-authorizing-integration-owners-object>`
    for more information.

    .. versionadded:: 2.10

    Attributes
    ----------
    guild_id: Optional[:class:`int`]
        The ID of the authorizing guild, if the application (and command, if applicable)
        was installed to the guild. In DMs with the bot, this will be ``0``.
    user_id: Optional[:class:`int`]
        The ID of the authorizing user, if the application (and command, if applicable)
        was installed to the user.
    """

    __slots__ = ("guild_id", "user_id")

    def __init__(self, data: AuthorizingIntegrationOwnersPayload) -> None:
        # keys are stringified ApplicationInstallTypes
        self.guild_id: Optional[int] = _get_as_snowflake(data, "0")
        self.user_id: Optional[int] = _get_as_snowflake(data, "1")

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} guild_id={self.guild_id!r} user_id={self.user_id!r}>"


class RoleSubscriptionData:
    """Represents metadata of the role subscription purchase/renewal in a message
    of type :attr:`MessageType.role_subscription_purchase`.

    .. versionadded:: 2.9

    Attributes
    ----------
    role_subscription_listing_id: :class:`int`
        The ID of the subscription listing the user subscribed to.

        See also :attr:`RoleTags.subscription_listing_id`.
    tier_name: :class:`str`
        The name of the tier the user subscribed to.
    total_months_subscribed: :class:`int`
        The cumulative number of months the user has been subscribed for.
    is_renewal: :class:`bool`
        Whether this message is for a subscription renewal instead of a new subscription.
    """

    __slots__ = (
        "role_subscription_listing_id",
        "tier_name",
        "total_months_subscribed",
        "is_renewal",
    )

    def __init__(self, data: RoleSubscriptionDataPayload) -> None:
        self.role_subscription_listing_id: int = int(data["role_subscription_listing_id"])
        self.tier_name: str = data["tier_name"]
        self.total_months_subscribed: int = data["total_months_subscribed"]
        self.is_renewal: bool = data["is_renewal"]


def flatten_handlers(cls):
    prefix = len("_handle_")
    handlers = [
        (key[prefix:], value)
        for key, value in cls.__dict__.items()
        if key.startswith("_handle_") and key != "_handle_member"
    ]

    # store _handle_member last
    handlers.append(("member", cls._handle_member))
    cls._HANDLERS = handlers
    cls._CACHED_SLOTS = [attr for attr in cls.__slots__ if attr.startswith("_cs_")]
    return cls


@flatten_handlers
class Message(Hashable):
    """Represents a message from Discord.

    .. collapse:: operations

        .. describe:: x == y

            Checks if two messages are equal.

        .. describe:: x != y

            Checks if two messages are not equal.

        .. describe:: hash(x)

            Returns the message's hash.

    Attributes
    ----------
    tts: :class:`bool`
        Specifies if the message was done with text-to-speech.
        This can only be accurately received in :func:`on_message` due to
        a Discord limitation.
    type: :class:`MessageType`
        The type of message. In most cases this should not be checked, but it is helpful
        in cases where it might be a system message for :attr:`system_content`.
    author: Union[:class:`Member`, :class:`abc.User`]
        A :class:`Member` that sent the message. If :attr:`channel` is a
        private channel or the user has the left the guild, then it is a :class:`User` instead.
    content: :class:`str`
        The actual contents of the message.
    nonce: Optional[Union[:class:`str`, :class:`int`]]
        The value used by the Discord guild and the client to verify that the message is successfully sent.
        This is not stored long term within Discord's servers and is only used ephemerally.
    embeds: List[:class:`Embed`]
        A list of embeds the message has.
    channel: Union[:class:`TextChannel`, :class:`VoiceChannel`, :class:`StageChannel`, :class:`Thread`, :class:`DMChannel`, :class:`GroupChannel`, :class:`PartialMessageable`]
        The channel that the message was sent from.
        Could be a :class:`DMChannel` or :class:`GroupChannel` if it's a private message.
    position: Optional[:class:`int`]
        A number that indicates the approximate position of a message in a :class:`Thread`.
        This is a number that starts at 0. e.g. the first message is position 0.
        This is `None` if the message was not sent in a :class:`Thread`, or if it was sent before July 1, 2022.

        .. versionadded:: 2.6

    reference: Optional[:class:`~disnake.MessageReference`]
        The message that this message references. This is only applicable to messages of
        type :attr:`MessageType.pins_add`, crossposted messages created by a
        followed channel integration, message replies, or application command responses.

        .. versionadded:: 1.5

    interaction_metadata: Optional[:class:`InteractionMetadata`]
        The metadata about the interaction that caused this message, if any.

        .. versionadded:: 2.10

    mention_everyone: :class:`bool`
        Specifies if the message mentions everyone.

        .. note::

            This does not check if the ``@everyone`` or the ``@here`` text is in the message itself.
            Rather this boolean indicates if either the ``@everyone`` or the ``@here`` text is in the message
            **and** it did end up mentioning.
    mentions: List[:class:`abc.User`]
        A list of :class:`Member` that were mentioned. If the message is in a private message
        then the list will be of :class:`User` instead. For messages that are not of type
        :attr:`MessageType.default`\\, this array can be used to aid in system messages.
        For more information, see :attr:`system_content`.

        .. warning::

            The order of the mentions list is not in any particular order so you should
            not rely on it. This is a Discord limitation, not one with the library.
    role_mentions: List[:class:`Role`]
        A list of :class:`Role` that were mentioned. If the message is in a private message
        then the list is always empty.
    id: :class:`int`
        The message ID.
    application_id: Optional[:class:`int`]
        If this message was sent from an interaction, or is an application owned webhook,
        then this is the ID of the application.

        .. versionadded:: 2.5

    webhook_id: Optional[:class:`int`]
        If this message was sent by a webhook, then this is the webhook ID's that sent this
        message.
    attachments: List[:class:`Attachment`]
        A list of attachments given to a message.
    pinned: :class:`bool`
        Specifies if the message is currently pinned.
    flags: :class:`MessageFlags`
        Extra features of the message.

        .. versionadded:: 1.3

    reactions : List[:class:`Reaction`]
        Reactions to a message. Reactions can be either custom emoji or standard unicode emoji.
    activity: Optional[:class:`dict`]
        The activity associated with this message. Sent with Rich-Presence related messages that for
        example, request joining, spectating, or listening to or with another member.

        It is a dictionary with the following optional keys:

        - ``type``: An integer denoting the type of message activity being requested.
        - ``party_id``: The party ID associated with the party.
    application: Optional[:class:`dict`]
        The rich presence enabled application associated with this message.

        It is a dictionary with the following keys:

        - ``id``: A string representing the application's ID.
        - ``name``: A string representing the application's name.
        - ``description``: A string representing the application's description.
        - ``icon``: A string representing the icon ID of the application.
        - ``cover_image``: A string representing the embed's image asset ID.
    stickers: List[:class:`StickerItem`]
        A list of sticker items given to the message.

        .. versionadded:: 1.6

    components: List[:class:`Component`]
        A list of components in the message.

        .. versionadded:: 2.0

    message_snapshots: list[:class:`ForwardedMessage`]
        A list of forwarded messages.

        .. versionadded:: 2.10

    guild: Optional[:class:`Guild`]
        The guild that the message belongs to, if applicable.

    poll: Optional[:class:`Poll`]
        The poll contained in this message.

        .. versionadded:: 2.10
    """

    __slots__ = (
        "_state",
        "_cs_channel_mentions",
        "_cs_raw_mentions",
        "_cs_clean_content",
        "_cs_raw_channel_mentions",
        "_cs_raw_role_mentions",
        "_cs_system_content",
        "tts",
        "content",
        "channel",
        "position",
        "application_id",
        "webhook_id",
        "mention_everyone",
        "embeds",
        "id",
        "mentions",
        "author",
        "attachments",
        "nonce",
        "pinned",
        "role_mentions",
        "type",
        "flags",
        "reactions",
        "reference",
        "_interaction",
        "interaction_metadata",
        "message_snapshots",
        "application",
        "activity",
        "stickers",
        "components",
        "guild",
        "poll",
        "_edited_timestamp",
        "_role_subscription_data",
    )

    if TYPE_CHECKING:
        _HANDLERS: ClassVar[List[Tuple[str, Callable[..., None]]]]
        _CACHED_SLOTS: ClassVar[List[str]]
        guild: Optional[Guild]
        reference: Optional[MessageReference]
        mentions: List[Union[User, Member]]
        author: Union[User, Member]
        role_mentions: List[Role]

    def __init__(
        self,
        *,
        state: ConnectionState,
        channel: MessageableChannel,
        data: MessagePayload,
    ) -> None:
        self._state: ConnectionState = state
        self.id: int = int(data["id"])
        self.application_id: Optional[int] = utils._get_as_snowflake(data, "application_id")
        self.webhook_id: Optional[int] = utils._get_as_snowflake(data, "webhook_id")
        self.reactions: List[Reaction] = [
            Reaction(message=self, data=d) for d in data.get("reactions", [])
        ]
        self.attachments: List[Attachment] = [
            Attachment(data=a, state=self._state) for a in data["attachments"]
        ]
        self.embeds: List[Embed] = [Embed.from_dict(a) for a in data["embeds"]]
        self.application: Optional[MessageApplicationPayload] = data.get("application")
        self.activity: Optional[MessageActivityPayload] = data.get("activity")
        # for user experience, on_message has no business getting partials
        # TODO: Subscripted message to include the channel
        self.channel: Union[GuildMessageable, DMChannel, GroupChannel] = channel  # type: ignore
        self.position: Optional[int] = data.get("position", None)
        self._edited_timestamp: Optional[datetime.datetime] = utils.parse_time(
            data["edited_timestamp"]
        )
        self.type: MessageType = try_enum(MessageType, data["type"])
        self.pinned: bool = data["pinned"]
        self.flags: MessageFlags = MessageFlags._from_value(data.get("flags", 0))
        self.mention_everyone: bool = data["mention_everyone"]
        self.tts: bool = data["tts"]
        self.content: str = data["content"]
        self.nonce: Optional[Union[int, str]] = data.get("nonce")
        self.stickers: List[StickerItem] = [
            StickerItem(data=d, state=state) for d in data.get("sticker_items", [])
        ]
        self.components: List[MessageTopLevelComponent] = [
            _message_component_factory(d) for d in data.get("components", [])
        ]

        self.poll: Optional[Poll] = None
        if poll_data := data.get("poll"):
            self.poll = Poll.from_dict(message=self, data=poll_data)

        try:
            # if the channel doesn't have a guild attribute, we handle that
            self.guild = channel.guild  # type: ignore
        except AttributeError:
            self.guild = state._get_guild(utils._get_as_snowflake(data, "guild_id"))

        self._interaction: Optional[InteractionReference] = (
            InteractionReference(state=state, guild=self.guild, data=interaction)
            if (interaction := data.get("interaction"))
            else None
        )
        self.interaction_metadata: Optional[InteractionMetadata] = (
            InteractionMetadata(state=state, data=interaction)
            if (interaction := data.get("interaction_metadata")) is not None
            else None
        )

        if thread_data := data.get("thread"):
            if not self.thread and isinstance(self.guild, Guild):
                self.guild._store_thread(thread_data)

        self._role_subscription_data: Optional[RoleSubscriptionDataPayload] = data.get(
            "role_subscription_data"
        )

        try:
            ref = data["message_reference"]
        except KeyError:
            self.reference = None
        else:
            self.reference = ref = MessageReference.with_state(state, ref)
            try:
                resolved = data["referenced_message"]
            except KeyError:
                pass
            else:
                if resolved is None:
                    ref.resolved = DeletedReferencedMessage(ref)
                else:
                    # Right now the channel IDs match but maybe in the future they won't.
                    if ref.channel_id == channel.id:
                        chan = channel
                    else:
                        chan, _ = state._get_guild_channel(resolved)

                    # the channel will be the correct type here
                    ref.resolved = self.__class__(channel=chan, data=resolved, state=state)  # type: ignore

        _ref = data.get("message_reference", {})
        self.message_snapshots: List[ForwardedMessage] = [
            ForwardedMessage(
                state=self._state,
                channel_id=utils._get_as_snowflake(_ref, "channel_id"),
                guild_id=utils._get_as_snowflake(_ref, "guild_id"),
                data=a["message"],
            )
            for a in data.get("message_snapshots", [])
        ]

        for handler in ("author", "member", "mentions", "mention_roles"):
            try:
                getattr(self, f"_handle_{handler}")(data[handler])
            except KeyError:
                continue

    def __repr__(self) -> str:
        name = self.__class__.__name__
        return f"<{name} id={self.id} channel={self.channel!r} type={self.type!r} author={self.author!r} flags={self.flags!r}>"

    def _try_patch(self, data, key, transform=None) -> None:
        try:
            value = data[key]
        except KeyError:
            pass
        else:
            if transform is None:
                setattr(self, key, value)
            else:
                setattr(self, key, transform(value))

    def _add_reaction(
        self, data: MessageReactionAddEvent, emoji: EmojiInputType, user_id: int
    ) -> Reaction:
        reaction = utils.find(lambda r: r.emoji == emoji, self.reactions)
        is_me = user_id == self._state.self_id

        if reaction is None:
            reaction_data: ReactionPayload = {
                "count": 1,
                "me": is_me,
                "emoji": data["emoji"],
            }
            reaction = Reaction(message=self, data=reaction_data, emoji=emoji)
            self.reactions.append(reaction)
        else:
            reaction.count += 1
            if is_me:
                reaction.me = is_me

        return reaction

    def _remove_reaction(
        self, data: MessageReactionRemoveEvent, emoji: EmojiInputType, user_id: int
    ) -> Reaction:
        reaction = utils.find(lambda r: r.emoji == emoji, self.reactions)

        if reaction is None:
            # already removed?
            raise ValueError("Emoji already removed?")

        # if reaction isn't in the list, we crash. This means Discord
        # sent bad data, or we stored improperly
        reaction.count -= 1

        if user_id == self._state.self_id:
            reaction.me = False
        if reaction.count == 0:
            # this raises ValueError if something went wrong as well.
            self.reactions.remove(reaction)

        return reaction

    def _clear_emoji(self, emoji) -> Optional[Reaction]:
        to_check = str(emoji)
        for index, reaction in enumerate(self.reactions):  # noqa: B007
            if str(reaction.emoji) == to_check:
                break
        else:
            # didn't find anything so just return
            return

        del self.reactions[index]
        return reaction

    def _update(self, data: MessageUpdateEvent) -> None:
        # In an update scheme, 'author' key has to be handled before 'member'
        # otherwise they overwrite each other which is undesirable.
        # Since there's no good way to do this we have to iterate over every
        # handler rather than iterating over the keys which is a little slower
        for key, handler in self._HANDLERS:
            try:
                value = data[key]
            except KeyError:
                continue
            else:
                handler(self, value)

        # clear the cached properties
        for attr in self._CACHED_SLOTS:
            try:
                delattr(self, attr)
            except AttributeError:
                pass

    def _handle_edited_timestamp(self, value: str) -> None:
        self._edited_timestamp = utils.parse_time(value)

    def _handle_pinned(self, value: bool) -> None:
        self.pinned = value

    def _handle_flags(self, value: int) -> None:
        self.flags = MessageFlags._from_value(value)

    def _handle_application(self, value: MessageApplicationPayload) -> None:
        self.application = value

    def _handle_activity(self, value: MessageActivityPayload) -> None:
        self.activity = value

    def _handle_mention_everyone(self, value: bool) -> None:
        self.mention_everyone = value

    def _handle_tts(self, value: bool) -> None:
        self.tts = value

    def _handle_type(self, value: int) -> None:
        self.type = try_enum(MessageType, value)

    def _handle_content(self, value: str) -> None:
        self.content = value

    def _handle_attachments(self, value: List[AttachmentPayload]) -> None:
        self.attachments = [Attachment(data=a, state=self._state) for a in value]

    def _handle_embeds(self, value: List[EmbedPayload]) -> None:
        self.embeds = [Embed.from_dict(data) for data in value]

    def _handle_nonce(self, value: Union[str, int]) -> None:
        self.nonce = value

    def _handle_author(self, author: UserPayload) -> None:
        self.author = self._state.store_user(author)
        if isinstance(self.guild, Guild):
            found = self.guild.get_member(self.author.id)
            if found is not None:
                self.author = found

    def _handle_member(self, member: MemberPayload) -> None:
        # The gateway now gives us full Member objects sometimes with the following keys
        # deaf, mute, joined_at, roles
        # For the sake of performance I'm going to assume that the only
        # field that needs *updating* would be the joined_at field.
        # If there is no Member object (for some strange reason), then we can upgrade
        # ourselves to a more "partial" member object.
        author = self.author
        try:
            # Update member reference
            author._update_from_message(member)  # type: ignore
        except AttributeError:
            # It's a user here
            # TODO: consider adding to cache here
            self.author = Member._from_message(message=self, data=member)

    def _handle_mentions(
        self, mentions: Union[List[UserPayload], List[UserWithMemberPayload]]
    ) -> None:
        self.mentions = r = []
        guild = self.guild
        state = self._state
        if not isinstance(guild, Guild):
            self.mentions = [state.store_user(m) for m in mentions]
            return

        for mention in filter(None, mentions):
            id_search = int(mention["id"])
            member = guild.get_member(id_search)
            if member is not None:
                r.append(member)
            else:
                r.append(Member._try_upgrade(data=mention, guild=guild, state=state))

    def _handle_mention_roles(self, role_mentions: List[int]) -> None:
        self.role_mentions = []
        if isinstance(self.guild, Guild):
            for role_id in map(int, role_mentions):
                role = self.guild.get_role(role_id)
                if role is not None:
                    self.role_mentions.append(role)

    def _handle_components(self, components: List[MessageTopLevelComponentPayload]) -> None:
        self.components = [_message_component_factory(d) for d in components]

    def _rebind_cached_references(self, new_guild: Guild, new_channel: GuildMessageable) -> None:
        self.guild = new_guild
        self.channel = new_channel

        # rebind the members' guilds; the members themselves will potentially be
        # updated later in _update_member_references, after re-chunking
        if isinstance(self.author, Member):
            self.author.guild = new_guild
        if self._interaction and isinstance(self._interaction.user, Member):
            self._interaction.user.guild = new_guild

    @utils.cached_slot_property("_cs_raw_mentions")
    def raw_mentions(self) -> List[int]:
        """List[:class:`int`]: A property that returns an array of user IDs matched with
        the syntax of ``<@user_id>`` in the message content.

        This allows you to receive the user IDs of mentioned users
        even in a private message context.
        """
        return [int(x) for x in re.findall(r"<@!?([0-9]{17,19})>", self.content)]

    @utils.cached_slot_property("_cs_raw_channel_mentions")
    def raw_channel_mentions(self) -> List[int]:
        """List[:class:`int`]: A property that returns an array of channel IDs matched with
        the syntax of ``<#channel_id>`` in the message content.
        """
        return [int(x) for x in re.findall(r"<#([0-9]{17,19})>", self.content)]

    @utils.cached_slot_property("_cs_raw_role_mentions")
    def raw_role_mentions(self) -> List[int]:
        """List[:class:`int`]: A property that returns an array of role IDs matched with
        the syntax of ``<@&role_id>`` in the message content.
        """
        return [int(x) for x in re.findall(r"<@&([0-9]{17,19})>", self.content)]

    @utils.cached_slot_property("_cs_channel_mentions")
    def channel_mentions(self) -> List[GuildChannel]:
        """List[:class:`abc.GuildChannel`]: A list of :class:`abc.GuildChannel` that were mentioned. If the message is in a private message
        then the list is always empty.
        """
        if self.guild is None:
            return []
        it = filter(None, map(self.guild.get_channel, self.raw_channel_mentions))
        return utils._unique(it)

    @utils.cached_slot_property("_cs_clean_content")
    def clean_content(self) -> str:
        """:class:`str`: A property that returns the content in a "cleaned up"
        manner. This basically means that mentions are transformed
        into the way the client shows it. e.g. ``<#id>`` will transform
        into ``#name``.

        This will also transform @everyone and @here mentions into
        non-mentions.

        .. note::

            This *does not* affect markdown. If you want to escape
            or remove markdown then use :func:`utils.escape_markdown` or :func:`utils.remove_markdown`
            respectively, along with this function.
        """
        transformations = {
            re.escape(f"<#{channel.id}>"): f"#{channel.name}" for channel in self.channel_mentions
        }

        mention_transforms = {
            re.escape(f"<@{member.id}>"): f"@{member.display_name}" for member in self.mentions
        }

        # add the <@!user_id> cases as well..
        second_mention_transforms = {
            re.escape(f"<@!{member.id}>"): f"@{member.display_name}" for member in self.mentions
        }

        transformations.update(mention_transforms)
        transformations.update(second_mention_transforms)

        if self.guild is not None:
            role_transforms = {
                re.escape(f"<@&{role.id}>"): f"@{role.name}" for role in self.role_mentions
            }
            transformations.update(role_transforms)

        def repl(obj):
            return transformations.get(re.escape(obj.group(0)), "")

        pattern = re.compile("|".join(transformations.keys()))
        result = pattern.sub(repl, self.content)
        return escape_mentions(result)

    @property
    def created_at(self) -> datetime.datetime:
        """:class:`datetime.datetime`: The message's creation time in UTC."""
        return utils.snowflake_time(self.id)

    @property
    def edited_at(self) -> Optional[datetime.datetime]:
        """Optional[:class:`datetime.datetime`]: An aware UTC datetime object containing the edited time of the message."""
        return self._edited_timestamp

    @property
    def jump_url(self) -> str:
        """:class:`str`: Returns a URL that allows the client to jump to this message."""
        guild_id = getattr(self.guild, "id", "@me")
        return f"https://discord.com/channels/{guild_id}/{self.channel.id}/{self.id}"

    @property
    def thread(self) -> Optional[Thread]:
        """Optional[:class:`Thread`]: The thread started from this message. ``None`` if no thread has been started.

        .. versionadded:: 2.4
        """
        if not isinstance(self.guild, Guild):
            return None

        return self.guild.get_thread(self.id)

    @property
    def role_subscription_data(self) -> Optional[RoleSubscriptionData]:
        """Optional[:class:`RoleSubscriptionData`]: The metadata of the role
        subscription purchase/renewal, if this message is a :attr:`MessageType.role_subscription_purchase`.

        .. versionadded:: 2.9
        """
        if not self._role_subscription_data:
            return None
        return RoleSubscriptionData(self._role_subscription_data)

    def is_system(self) -> bool:
        """Whether the message is a system message.

        A system message is a message that is constructed entirely by the Discord API
        in response to something.

        .. versionadded:: 1.3

        :return type: :class:`bool`
        """
        return self.type not in (
            MessageType.default,
            MessageType.reply,
            MessageType.application_command,
            MessageType.thread_starter_message,
            MessageType.context_menu_command,
        )

    @utils.cached_slot_property("_cs_system_content")
    def system_content(self) -> Optional[str]:
        """Optional[:class:`str`]: A property that returns the content that is rendered
        regardless of the :attr:`Message.type`.

        In the case of :attr:`MessageType.default` and :attr:`MessageType.reply`\\,
        this just returns the regular :attr:`Message.content`. Otherwise this
        returns an English message denoting the contents of the system message.

        If the message type is unrecognised this method will return ``None``.
        """
        if self.type in (MessageType.default, MessageType.reply):
            return self.content

        if self.type is MessageType.recipient_add:
            if self.channel.type is ChannelType.group:
                return f"{self.author.name} added {self.mentions[0].name} to the group."
            else:
                return f"{self.author.name} added {self.mentions[0].name} to the thread."

        if self.type is MessageType.recipient_remove:
            if self.channel.type is ChannelType.group:
                return f"{self.author.name} removed {self.mentions[0].name} from the group."
            else:
                return f"{self.author.name} removed {self.mentions[0].name} from the thread."

        # MessageType.call cannot be read by bots.

        if self.type is MessageType.channel_name_change:
            if (
                self.channel.type is ChannelType.public_thread
                and (parent := getattr(self.channel, "parent", None))
                and parent.type in (ChannelType.forum, ChannelType.media)
            ):
                return f"{self.author.name} changed the post title: **{self.content}**"
            return f"{self.author.name} changed the channel name: **{self.content}**"

        if self.type is MessageType.channel_icon_change:
            return f"{self.author.name} changed the channel icon."

        if self.type is MessageType.pins_add:
            return f"{self.author.name} pinned a message to this channel."

        if self.type is MessageType.new_member:
            formats = [
                "{0} joined the party.",
                "{0} is here.",
                "Welcome, {0}. We hope you brought pizza.",
                "A wild {0} appeared.",
                "{0} just landed.",
                "{0} just slid into the server.",
                "{0} just showed up!",
                "Welcome {0}. Say hi!",
                "{0} hopped into the server.",
                "Everyone welcome {0}!",
                "Glad you're here, {0}.",
                "Good to see you, {0}.",
                "Yay you made it, {0}!",
            ]

            created_at_ms = int(self.created_at.timestamp() * 1000)
            return formats[created_at_ms % len(formats)].format(self.author.name)

        if self.type is MessageType.premium_guild_subscription:
            if not self.content:
                return f"{self.author.name} just boosted the server!"
            else:
                return f"{self.author.name} just boosted the server **{self.content}** times!"

        if self.type is MessageType.premium_guild_tier_1:
            if not self.content:
                return f"{self.author.name} just boosted the server! {self.guild} has achieved **Level 1!**"
            else:
                return f"{self.author.name} just boosted the server **{self.content}** times! {self.guild} has achieved **Level 1!**"

        if self.type is MessageType.premium_guild_tier_2:
            if not self.content:
                return f"{self.author.name} just boosted the server! {self.guild} has achieved **Level 2!**"
            else:
                return f"{self.author.name} just boosted the server **{self.content}** times! {self.guild} has achieved **Level 2!**"

        if self.type is MessageType.premium_guild_tier_3:
            if not self.content:
                return f"{self.author.name} just boosted the server! {self.guild} has achieved **Level 3!**"
            else:
                return f"{self.author.name} just boosted the server **{self.content}** times! {self.guild} has achieved **Level 3!**"

        if self.type is MessageType.channel_follow_add:
            return f"{self.author.name} has added {self.content} to this channel. Its most important updates will show up here."

        if self.type is MessageType.guild_stream:
            # the author will be a Member
            return f"{self.author.name} is live! Now streaming {self.author.activity.name}."  # type: ignore

        if self.type is MessageType.guild_discovery_disqualified:
            return "This server has been removed from Server Discovery because it no longer passes all the requirements. Check Server Settings for more details."

        if self.type is MessageType.guild_discovery_requalified:
            return "This server is eligible for Server Discovery again and has been automatically relisted!"

        if self.type is MessageType.guild_discovery_grace_period_initial_warning:
            return "This server has failed Discovery activity requirements for 1 week. If this server fails for 4 weeks in a row, it will be automatically removed from Discovery."

        if self.type is MessageType.guild_discovery_grace_period_final_warning:
            return "This server has failed Discovery activity requirements for 3 weeks in a row. If this server fails for 1 more week, it will be removed from Discovery."

        if self.type is MessageType.thread_created:
            return f"{self.author.name} started a thread: **{self.content}**. See all threads."

        # note: MessageType.reply is implemented at the top of this method, with MessageType.default

        if self.type is MessageType.application_command:
            return self.content

        if self.type is MessageType.thread_starter_message:
            if self.reference is None or self.reference.resolved is None:
                return "Sorry, we couldn't load the first message in this thread"

            # the resolved message for the reference will be a Message
            return self.reference.resolved.content  # type: ignore

        if self.type is MessageType.guild_invite_reminder:
            # todo: determine if this should be the owner content or the user content
            return "Wondering who to invite?\nStart by inviting anyone who can help you build the server!"

        if self.type is MessageType.context_menu_command:
            return self.content

        if self.type is MessageType.auto_moderation_action:
            return self.content

        if self.type is MessageType.role_subscription_purchase:
            if not (data := self.role_subscription_data):
                return

            guild_name = f"**{self.guild.name}**" if self.guild else None
            if data.total_months_subscribed > 0:
                action = "renewed" if data.is_renewal else "joined"
                return (
                    f"{self.author.name} {action} **{data.tier_name}** and has been a subscriber "
                    f"of {guild_name} for {data.total_months_subscribed} "
                    f"{'month' if data.total_months_subscribed == 1 else 'months'}!"
                )
            elif data.is_renewal:
                return f"{self.author.name} renewed **{data.tier_name}** in their {guild_name} membership!"
            else:
                return f"{self.author.name} joined **{data.tier_name}** as a subscriber of {guild_name}!"

        if self.type is MessageType.interaction_premium_upsell:
            return self.content

        if self.type is MessageType.stage_start:
            return f"{self.author.name} started {self.content}"

        if self.type is MessageType.stage_end:
            return f"{self.author.name} ended {self.content}"

        if self.type is MessageType.stage_speaker:
            return f"{self.author.name} is now a speaker."

        if self.type is MessageType.stage_topic:
            return f"{self.author.name} changed the Stage topic: {self.content}"

        if self.type is MessageType.guild_application_premium_subscription:
            application_name = (
                self.application["name"]
                if self.application and "name" in self.application
                else "a deleted application"
            )
            return f"{self.author.name} upgraded {application_name} to premium for this server! 🎉"

        if self.type is MessageType.guild_incident_alert_mode_enabled:
            enabled_until = utils.parse_time(self.content)
            return f"{self.author.name} enabled security actions until {enabled_until.strftime('%d/%m/%Y, %H:%M')}."

        if self.type is MessageType.guild_incident_alert_mode_disabled:
            return f"{self.author.name} disabled security actions."

        if self.type is MessageType.guild_incident_report_raid:
            guild_name = self.guild.name if self.guild else None
            return f"{self.author.name} reported a raid in {guild_name}."

        if self.type is MessageType.guild_incident_report_false_alarm:
            return f"{self.author.name} resolved an Activity Alert."

        if self.type is MessageType.poll_result:
            if not self.embeds:
                return

            poll_result_embed = self.embeds[0]
            poll_embed_fields: Dict[str, str] = {}
            if not poll_result_embed._fields:
                return

            for field in poll_result_embed._fields:
                poll_embed_fields[field["name"]] = field["value"]

            # should never be none
            question = poll_embed_fields["poll_question_text"]
            # should never be none
            total_votes = poll_embed_fields["total_votes"]
            winning_answer = poll_embed_fields.get("victor_answer_text")
            winning_answer_votes = poll_embed_fields.get("victor_answer_votes")
            msg = f"{self.author.display_name}'s poll {question} has closed."

            if winning_answer and winning_answer_votes:
                msg += (
                    f"\n\n{winning_answer}"
                    f"\nWinning answer • {(100 * int(winning_answer_votes)) // int(total_votes)}%"
                )
            else:
                msg += "\n\nThere was no winner."
            return msg

        # in the event of an unknown or unsupported message type, we return nothing
        return None

    @property
    @deprecated("interaction_metadata")
    def interaction(self) -> Optional[InteractionReference]:
        """Optional[:class:`~disnake.InteractionReference`]: The interaction that this message references.
        This exists only when the message is a response to an interaction without an existing message.

        .. versionadded:: 2.1

        .. deprecated:: 2.10
            Use :attr:`interaction_metadata` instead.
        """
        return self._interaction

    async def delete(self, *, delay: Optional[float] = None) -> None:
        """|coro|

        Deletes the message.

        Your own messages could be deleted without any proper permissions. However to
        delete other people's messages, you need the :attr:`~Permissions.manage_messages`
        permission.

        .. versionchanged:: 1.1
            Added the new ``delay`` keyword-only parameter.

        Parameters
        ----------
        delay: Optional[:class:`float`]
            If provided, the number of seconds to wait in the background
            before deleting the message. If the deletion fails then it is silently ignored.

        Raises
        ------
        Forbidden
            You do not have proper permissions to delete the message.
        NotFound
            The message was deleted already
        HTTPException
            Deleting the message failed.
        """
        if delay is not None:

            async def delete(delay: float) -> None:
                await asyncio.sleep(delay)
                try:
                    await self._state.http.delete_message(self.channel.id, self.id)
                except HTTPException:
                    pass

            asyncio.create_task(delete(delay))
        else:
            await self._state.http.delete_message(self.channel.id, self.id)

    @overload
    async def edit(
        self,
        content: Optional[str] = ...,
        *,
        embed: Optional[Embed] = ...,
        file: File = ...,
        attachments: Optional[List[Attachment]] = ...,
        suppress_embeds: bool = ...,
        flags: MessageFlags = ...,
        allowed_mentions: Optional[AllowedMentions] = ...,
        view: Optional[View] = ...,
        components: Optional[MessageComponentInput] = ...,
        delete_after: Optional[float] = ...,
    ) -> Message: ...

    @overload
    async def edit(
        self,
        content: Optional[str] = ...,
        *,
        embed: Optional[Embed] = ...,
        files: List[File] = ...,
        attachments: Optional[List[Attachment]] = ...,
        suppress_embeds: bool = ...,
        flags: MessageFlags = ...,
        allowed_mentions: Optional[AllowedMentions] = ...,
        view: Optional[View] = ...,
        components: Optional[MessageComponentInput] = ...,
        delete_after: Optional[float] = ...,
    ) -> Message: ...

    @overload
    async def edit(
        self,
        content: Optional[str] = ...,
        *,
        embeds: List[Embed] = ...,
        file: File = ...,
        attachments: Optional[List[Attachment]] = ...,
        suppress_embeds: bool = ...,
        flags: MessageFlags = ...,
        allowed_mentions: Optional[AllowedMentions] = ...,
        view: Optional[View] = ...,
        components: Optional[MessageComponentInput] = ...,
        delete_after: Optional[float] = ...,
    ) -> Message: ...

    @overload
    async def edit(
        self,
        content: Optional[str] = ...,
        *,
        embeds: List[Embed] = ...,
        files: List[File] = ...,
        attachments: Optional[List[Attachment]] = ...,
        suppress_embeds: bool = ...,
        flags: MessageFlags = ...,
        allowed_mentions: Optional[AllowedMentions] = ...,
        view: Optional[View] = ...,
        components: Optional[MessageComponentInput] = ...,
        delete_after: Optional[float] = ...,
    ) -> Message: ...

    async def edit(
        self,
        content: Optional[str] = MISSING,
        *,
        embed: Optional[Embed] = MISSING,
        embeds: List[Embed] = MISSING,
        file: File = MISSING,
        files: List[File] = MISSING,
        attachments: Optional[List[Attachment]] = MISSING,
        suppress: bool = MISSING,  # deprecated
        suppress_embeds: bool = MISSING,
        flags: MessageFlags = MISSING,
        allowed_mentions: Optional[AllowedMentions] = MISSING,
        view: Optional[View] = MISSING,
        components: Optional[MessageComponentInput] = MISSING,
        delete_after: Optional[float] = None,
    ) -> Message:
        """|coro|

        Edits the message.

        The content must be able to be transformed into a string via ``str(content)``.

        .. note::
            If the original message has embeds with images that were created from local files
            (using the ``file`` parameter with :meth:`Embed.set_image` or :meth:`Embed.set_thumbnail`),
            those images will be removed if the message's attachments are edited in any way
            (i.e. by setting ``file``/``files``/``attachments``, or adding an embed with local files).

        .. note::

            This method cannot be used on messages authored by others, with one exception.
            The ``suppress_embeds`` parameter can be used to change the state of embeds on
            other users' messages, requiring the :attr:`~.Permissions.manage_messages` permission.

        .. versionchanged:: 1.3
            The ``suppress`` keyword-only parameter was added.

        .. versionchanged:: 2.5
            The ``suppress`` keyword-only parameter was deprecated
            in favor of ``suppress_embeds``.

        .. versionchanged:: 2.6
            Raises :exc:`TypeError` instead of ``InvalidArgument``.

        Parameters
        ----------
        content: Optional[:class:`str`]
            The new content to replace the message with.
            Could be ``None`` to remove the content.
        embed: Optional[:class:`Embed`]
            The new embed to replace the original with. This cannot be mixed with the
            ``embeds`` parameter.
            Could be ``None`` to remove the embed.
        embeds: List[:class:`Embed`]
            The new embeds to replace the original with. Must be a maximum of 10.
            This cannot be mixed with the ``embed`` parameter.
            To remove all embeds ``[]`` should be passed.

            .. versionadded:: 2.0

        file: :class:`File`
            The file to upload. This cannot be mixed with the ``files`` parameter.
            Files will be appended to the message, see the ``attachments`` parameter
            to remove/replace existing files.

            .. versionadded:: 2.1

        files: List[:class:`File`]
            A list of files to upload. This cannot be mixed with the ``file`` parameter.
            Files will be appended to the message, see the ``attachments`` parameter
            to remove/replace existing files.

            .. versionadded:: 2.1

        attachments: Optional[List[:class:`Attachment`]]
            A list of attachments to keep in the message.
            If ``[]`` or ``None`` is passed then all existing attachments are removed.
            Keeps existing attachments if not provided.

            .. versionchanged:: 2.5
                Supports passing ``None`` to clear attachments.

        suppress_embeds: :class:`bool`
            Whether to suppress embeds for the message. This hides
            all the embeds from the UI if set to ``True``. If set
            to ``False``, this brings the embeds back if they were
            suppressed.
        flags: :class:`MessageFlags`
            The new flags to set for this message. Overrides existing flags.
            Only :attr:`~MessageFlags.suppress_embeds` is supported.

            If parameter ``suppress_embeds`` is provided,
            that will override the setting of :attr:`.MessageFlags.suppress_embeds`.

            .. versionadded:: 2.9

        delete_after: Optional[:class:`float`]
            If provided, the number of seconds to wait in the background
            before deleting the message we just edited. If the deletion fails,
            then it is silently ignored.
        allowed_mentions: Optional[:class:`~disnake.AllowedMentions`]
            Controls the mentions being processed in this message. If this is
            passed, then the object is merged with :attr:`Client.allowed_mentions`.
            The merging behaviour only overrides attributes that have been explicitly passed
            to the object, otherwise it uses the attributes set in :attr:`Client.allowed_mentions`.
            If no object is passed at all then the defaults given by :attr:`Client.allowed_mentions`
            are used instead.

            .. versionadded:: 1.4

        view: Optional[:class:`~disnake.ui.View`]
            The updated view to update this message with. This cannot be mixed with ``components``.
            If ``None`` is passed then the view is removed.

            .. versionadded:: 2.0

        components: |components_type|
            The updated components to update this message with. This cannot be mixed with ``view``.
            If ``None`` is passed then the components are removed.

            .. versionadded:: 2.4

        Raises
        ------
        HTTPException
            Editing the message failed.
        Forbidden
            Tried to suppress embeds on a message without permissions or
            edited a message's content or embed that isn't yours.
        TypeError
            You specified both ``embed`` and ``embeds``, or ``file`` and ``files``, or ``view`` and ``components``.

        Returns
        -------
        :class:`Message`
            The message that was edited.
        """
        # allowed_mentions can only be changed on the bot's own messages
        if self._state.allowed_mentions is not None and self.author.id == self._state.self_id:
            previous_allowed_mentions = self._state.allowed_mentions
        else:
            previous_allowed_mentions = None

        # if no attachment list was provided but we're uploading new files,
        # use current attachments as the base
        if attachments is MISSING and (file or files):
            attachments = self.attachments

        return await _edit_handler(
            self,
            default_flags=self.flags.value,
            previous_allowed_mentions=previous_allowed_mentions,
            content=content,
            embed=embed,
            embeds=embeds,
            file=file,
            files=files,
            attachments=attachments,
            suppress=suppress,
            suppress_embeds=suppress_embeds,
            flags=flags,
            allowed_mentions=allowed_mentions,
            view=view,
            components=components,
            delete_after=delete_after,
        )

    async def publish(self) -> None:
        """|coro|

        Publishes this message to your announcement channel.

        You must have the :attr:`~Permissions.send_messages` permission to do this.

        If the message is not your own then the :attr:`~Permissions.manage_messages`
        permission is also needed.

        Raises
        ------
        Forbidden
            You do not have the proper permissions to publish this message.
        HTTPException
            Publishing the message failed.
        """
        await self._state.http.publish_message(self.channel.id, self.id)

    async def pin(self, *, reason: Optional[str] = None) -> None:
        """|coro|

        Pins the message.

        You must have the :attr:`~Permissions.manage_messages` permission to do
        this in a non-private channel context.

        This does not work with messages sent in a :class:`VoiceChannel` or :class:`StageChannel`.

        Parameters
        ----------
        reason: Optional[:class:`str`]
            The reason for pinning the message. Shows up on the audit log.

            .. versionadded:: 1.4

        Raises
        ------
        Forbidden
            You do not have permissions to pin the message.
        NotFound
            The message or channel was not found or deleted.
        HTTPException
            Pinning the message failed, probably due to the channel
            having more than 50 pinned messages or the channel not supporting pins.
        """
        await self._state.http.pin_message(self.channel.id, self.id, reason=reason)
        self.pinned = True

    async def unpin(self, *, reason: Optional[str] = None) -> None:
        """|coro|

        Unpins the message.

        You must have the :attr:`~Permissions.manage_messages` permission to do
        this in a non-private channel context.

        Parameters
        ----------
        reason: Optional[:class:`str`]
            The reason for unpinning the message. Shows up on the audit log.

            .. versionadded:: 1.4

        Raises
        ------
        Forbidden
            You do not have permissions to unpin the message.
        NotFound
            The message or channel was not found or deleted.
        HTTPException
            Unpinning the message failed.
        """
        await self._state.http.unpin_message(self.channel.id, self.id, reason=reason)
        self.pinned = False

    async def add_reaction(self, emoji: EmojiInputType) -> None:
        """|coro|

        Adds a reaction to the message.

        The emoji may be a unicode emoji or a custom guild :class:`Emoji`.

        You must have the :attr:`~Permissions.read_message_history` permission
        to use this. If nobody else has reacted to the message using this
        emoji, the :attr:`~Permissions.add_reactions` permission is required.

        .. versionchanged:: 2.6
            Raises :exc:`TypeError` instead of ``InvalidArgument``.

        Parameters
        ----------
        emoji: Union[:class:`Emoji`, :class:`Reaction`, :class:`PartialEmoji`, :class:`str`]
            The emoji to react with.

        Raises
        ------
        HTTPException
            Adding the reaction failed.
        Forbidden
            You do not have the proper permissions to react to the message.
        NotFound
            The emoji you specified was not found.
        TypeError
            The emoji parameter is invalid.
        """
        emoji = convert_emoji_reaction(emoji)
        await self._state.http.add_reaction(self.channel.id, self.id, emoji)

    async def remove_reaction(
        self, emoji: Union[EmojiInputType, Reaction], member: Snowflake
    ) -> None:
        """|coro|

        Removes a reaction by the member from the message.

        The emoji may be a unicode emoji or a custom guild :class:`Emoji`.

        If the reaction is not your own (i.e. ``member`` parameter is not you) then
        the :attr:`~Permissions.manage_messages` permission is needed.

        The ``member`` parameter must represent a member and meet
        the :class:`abc.Snowflake` abc.

        .. versionchanged:: 2.6
            Raises :exc:`TypeError` instead of ``InvalidArgument``.

        Parameters
        ----------
        emoji: Union[:class:`Emoji`, :class:`Reaction`, :class:`PartialEmoji`, :class:`str`]
            The emoji to remove.
        member: :class:`abc.Snowflake`
            The member for which to remove the reaction.

        Raises
        ------
        HTTPException
            Removing the reaction failed.
        Forbidden
            You do not have the proper permissions to remove the reaction.
        NotFound
            The member or emoji you specified was not found.
        TypeError
            The emoji parameter is invalid.
        """
        emoji = convert_emoji_reaction(emoji)

        if member.id == self._state.self_id:
            await self._state.http.remove_own_reaction(self.channel.id, self.id, emoji)
        else:
            await self._state.http.remove_reaction(self.channel.id, self.id, emoji, member.id)

    async def clear_reaction(self, emoji: Union[EmojiInputType, Reaction]) -> None:
        """|coro|

        Clears a specific reaction from the message.

        The emoji may be a unicode emoji or a custom guild :class:`Emoji`.

        You need the :attr:`~Permissions.manage_messages` permission to use this.

        .. versionadded:: 1.3

        .. versionchanged:: 2.6
            Raises :exc:`TypeError` instead of ``InvalidArgument``.

        Parameters
        ----------
        emoji: Union[:class:`Emoji`, :class:`Reaction`, :class:`PartialEmoji`, :class:`str`]
            The emoji to clear.

        Raises
        ------
        HTTPException
            Clearing the reaction failed.
        Forbidden
            You do not have the proper permissions to clear the reaction.
        NotFound
            The emoji you specified was not found.
        TypeError
            The emoji parameter is invalid.
        """
        emoji = convert_emoji_reaction(emoji)
        await self._state.http.clear_single_reaction(self.channel.id, self.id, emoji)

    async def clear_reactions(self) -> None:
        """|coro|

        Removes all the reactions from the message.

        You need the :attr:`~Permissions.manage_messages` permission to use this.

        Raises
        ------
        HTTPException
            Removing the reactions failed.
        Forbidden
            You do not have the proper permissions to remove all the reactions.
        """
        await self._state.http.clear_reactions(self.channel.id, self.id)

    async def create_thread(
        self,
        *,
        name: str,
        auto_archive_duration: Optional[AnyThreadArchiveDuration] = None,
        slowmode_delay: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> Thread:
        """|coro|

        Creates a public thread from this message.

        You must have :attr:`~disnake.Permissions.create_public_threads` in order to
        create a public thread from a message.

        The channel this message belongs in must be a :class:`TextChannel`.

        .. versionadded:: 2.0

        .. versionchanged:: 2.6
            Raises :exc:`TypeError` instead of ``InvalidArgument``.

        Parameters
        ----------
        name: :class:`str`
            The name of the thread.
        auto_archive_duration: Union[:class:`int`, :class:`ThreadArchiveDuration`]
            The duration in minutes before a thread is automatically archived for inactivity.
            If not provided, the channel's default auto archive duration is used.
            Must be one of ``60``, ``1440``, ``4320``, or ``10080``.
        slowmode_delay: Optional[:class:`int`]
            Specifies the slowmode rate limit for users in this thread, in seconds.
            A value of ``0`` disables slowmode. The maximum value possible is ``21600``.
            If set to ``None`` or not provided, slowmode is inherited from the parent's
            :attr:`~TextChannel.default_thread_slowmode_delay`.

            .. versionadded:: 2.3

        reason: Optional[:class:`str`]
            The reason for creating the thread. Shows up on the audit log.

            .. versionadded:: 2.5

        Raises
        ------
        Forbidden
            You do not have permissions to create a thread.
        HTTPException
            Creating the thread failed.
        TypeError
            This message does not have guild info attached.

        Returns
        -------
        :class:`.Thread`
            The created thread.
        """
        if self.guild is None:
            raise TypeError("This message does not have guild info attached.")

        if auto_archive_duration is not None:
            auto_archive_duration = cast(
                "ThreadArchiveDurationLiteral", try_enum_to_int(auto_archive_duration)
            )

        default_auto_archive_duration: ThreadArchiveDurationLiteral = getattr(
            self.channel, "default_auto_archive_duration", 1440
        )
        data = await self._state.http.start_thread_with_message(
            self.channel.id,
            self.id,
            name=name,
            auto_archive_duration=auto_archive_duration or default_auto_archive_duration,
            rate_limit_per_user=slowmode_delay,
            reason=reason,
        )
        return Thread(guild=self.guild, state=self._state, data=data)

    async def reply(
        self, content: Optional[str] = None, *, fail_if_not_exists: bool = True, **kwargs: Any
    ) -> Message:
        """|coro|

        A shortcut method to :meth:`.abc.Messageable.send` to reply to the
        :class:`.Message`.

        .. versionadded:: 1.6

        .. versionchanged:: 2.3
            Added ``fail_if_not_exists`` keyword argument. Defaults to ``True``.

        .. versionchanged:: 2.6
            Raises :exc:`TypeError` or :exc:`ValueError` instead of ``InvalidArgument``.

        Parameters
        ----------
        fail_if_not_exists: :class:`bool`
            Whether replying using the message reference should raise :exc:`~disnake.HTTPException`
            if the message no longer exists or Discord could not fetch the message.

            .. versionadded:: 2.3

        Raises
        ------
        HTTPException
            Sending the message failed.
        Forbidden
            You do not have the proper permissions to send the message.
        TypeError
            You specified both ``embed`` and ``embeds``, or ``file`` and ``files``, or ``view`` and ``components``.
        ValueError
            The ``files`` or ``embeds`` list is too large.

        Returns
        -------
        :class:`.Message`
            The message that was sent.
        """
        if not fail_if_not_exists:
            reference = MessageReference.from_message(self, fail_if_not_exists=False)
        else:
            reference = self
        return await self.channel.send(content, reference=reference, **kwargs)

    async def forward(
        self,
        channel: MessageableChannel,
    ) -> Message:
        """|coro|

        A shortcut method to :meth:`.abc.Messageable.send` to forward a
        :class:`.Message`.

        .. versionadded:: 2.10

        Parameters
        ----------
        channel: Union[:class:`TextChannel`, :class:`VoiceChannel`, :class:`StageChannel`, :class:`Thread`, :class:`DMChannel`, :class:`GroupChannel`, :class:`PartialMessageable`]
            The channel where the message should be forwarded to.

        Raises
        ------
        HTTPException
            Sending the message failed.
        Forbidden
            You do not have the proper permissions to send the message.

        Returns
        -------
        :class:`.Message`
            The message that was sent.
        """
        reference = self.to_reference(
            type=MessageReferenceType.forward,
            fail_if_not_exists=False,
        )
        return await channel.send(reference=reference)

    def to_reference(
        self,
        *,
        type: MessageReferenceType = MessageReferenceType.default,
        fail_if_not_exists: bool = True,
    ) -> MessageReference:
        """Creates a :class:`~disnake.MessageReference` from the current message.

        .. versionadded:: 1.6

        Parameters
        ----------
        type: :class:`MessageReferenceType`
            The type of the message reference. This is used to control whether to reply to
            or forward a message. Defaults to replying.

            .. versionadded:: 2.10

        fail_if_not_exists: :class:`bool`
            Whether replying using the message reference should raise :class:`HTTPException`
            if the message no longer exists or Discord could not fetch the message.

            .. versionadded:: 1.7

        Returns
        -------
        :class:`~disnake.MessageReference`
            The reference to this message.
        """
        return MessageReference.from_message(
            self,
            type=type,
            fail_if_not_exists=fail_if_not_exists,
        )

    def to_message_reference_dict(self) -> MessageReferencePayload:
        data: MessageReferencePayload = {
            # defaulting to REPLY when implicitly transforming a Message or
            # PartialMessage object to a MessageReference
            "type": 0,
            "message_id": self.id,
            "channel_id": self.channel.id,
        }

        if self.guild is not None:
            data["guild_id"] = self.guild.id

        return data


class PartialMessage(Hashable):
    """Represents a partial message to aid with working messages when only
    a message and channel ID are present.

    There are two ways to construct this class. The first one is through
    the constructor itself, and the second is via the following:

    - :meth:`TextChannel.get_partial_message`
    - :meth:`VoiceChannel.get_partial_message`
    - :meth:`StageChannel.get_partial_message`
    - :meth:`Thread.get_partial_message`
    - :meth:`DMChannel.get_partial_message`
    - :meth:`GroupChannel.get_partial_message`
    - :meth:`PartialMessageable.get_partial_message`

    Note that this class is trimmed down and has no rich attributes.

    .. versionadded:: 1.6

    .. collapse:: operations

        .. describe:: x == y

            Checks if two partial messages are equal.

        .. describe:: x != y

            Checks if two partial messages are not equal.

        .. describe:: hash(x)

            Returns the partial message's hash.

    Attributes
    ----------
    channel: Union[:class:`TextChannel`, :class:`VoiceChannel`, :class:`StageChannel`, :class:`Thread`, :class:`DMChannel`, :class:`GroupChannel`, :class:`PartialMessageable`]
        The channel associated with this partial message.
    id: :class:`int`
        The message ID.
    """

    __slots__ = ("channel", "id", "_cs_guild", "_state")

    jump_url: str = Message.jump_url  # type: ignore
    delete = Message.delete
    publish = Message.publish
    pin = Message.pin
    unpin = Message.unpin
    add_reaction = Message.add_reaction
    remove_reaction = Message.remove_reaction
    clear_reaction = Message.clear_reaction
    clear_reactions = Message.clear_reactions
    reply = Message.reply
    to_reference = Message.to_reference
    to_message_reference_dict = Message.to_message_reference_dict
    forward = Message.forward

    def __init__(self, *, channel: MessageableChannel, id: int) -> None:
        if channel.type not in (
            ChannelType.text,
            ChannelType.news,
            ChannelType.private,
            ChannelType.group,
            ChannelType.news_thread,
            ChannelType.public_thread,
            ChannelType.private_thread,
            ChannelType.voice,
            ChannelType.stage_voice,
        ):
            raise TypeError(
                f"Expected TextChannel, VoiceChannel, StageChannel, Thread, DMChannel, GroupChannel, or PartialMessageable "
                f"with a valid type, not {type(channel)!r} (type: {channel.type!r})"
            )

        self.channel: MessageableChannel = channel
        self._state: ConnectionState = channel._state
        self.id: int = id

    def _update(self, data) -> None:
        # This is used for duck typing purposes.
        # Just do nothing with the data.
        pass

    # Also needed for duck typing purposes
    # n.b. not exposed
    pinned = property(None, lambda x, y: None)

    def __repr__(self) -> str:
        return f"<PartialMessage id={self.id} channel={self.channel!r}>"

    @property
    def created_at(self) -> datetime.datetime:
        """:class:`datetime.datetime`: The partial message's creation time in UTC."""
        return utils.snowflake_time(self.id)

    @utils.cached_slot_property("_cs_guild")
    def guild(self) -> Optional[Guild]:
        """Optional[:class:`Guild`]: The guild that the partial message belongs to, if applicable."""
        return getattr(self.channel, "guild", None)

    async def fetch(self) -> Message:
        """|coro|

        Fetches the partial message to a full :class:`Message`.

        Raises
        ------
        NotFound
            The message was not found.
        Forbidden
            You do not have the permissions required to get a message.
        HTTPException
            Retrieving the message failed.

        Returns
        -------
        :class:`Message`
            The full message.
        """
        data = await self._state.http.get_message(self.channel.id, self.id)
        return self._state.create_message(channel=self.channel, data=data)

    @overload
    async def edit(
        self,
        content: Optional[str] = ...,
        *,
        embed: Optional[Embed] = ...,
        file: File = ...,
        attachments: Optional[List[Attachment]] = ...,
        suppress_embeds: bool = ...,
        flags: MessageFlags = ...,
        allowed_mentions: Optional[AllowedMentions] = ...,
        view: Optional[View] = ...,
        components: Optional[MessageComponentInput] = ...,
        delete_after: Optional[float] = ...,
    ) -> Message: ...

    @overload
    async def edit(
        self,
        content: Optional[str] = ...,
        *,
        embed: Optional[Embed] = ...,
        files: List[File] = ...,
        attachments: Optional[List[Attachment]] = ...,
        suppress_embeds: bool = ...,
        flags: MessageFlags = ...,
        allowed_mentions: Optional[AllowedMentions] = ...,
        view: Optional[View] = ...,
        components: Optional[MessageComponentInput] = ...,
        delete_after: Optional[float] = ...,
    ) -> Message: ...

    @overload
    async def edit(
        self,
        content: Optional[str] = ...,
        *,
        embeds: List[Embed] = ...,
        file: File = ...,
        attachments: Optional[List[Attachment]] = ...,
        suppress_embeds: bool = ...,
        flags: MessageFlags = ...,
        allowed_mentions: Optional[AllowedMentions] = ...,
        view: Optional[View] = ...,
        components: Optional[MessageComponentInput] = ...,
        delete_after: Optional[float] = ...,
    ) -> Message: ...

    @overload
    async def edit(
        self,
        content: Optional[str] = ...,
        *,
        embeds: List[Embed] = ...,
        files: List[File] = ...,
        attachments: Optional[List[Attachment]] = ...,
        suppress_embeds: bool = ...,
        flags: MessageFlags = ...,
        allowed_mentions: Optional[AllowedMentions] = ...,
        view: Optional[View] = ...,
        components: Optional[MessageComponentInput] = ...,
        delete_after: Optional[float] = ...,
    ) -> Message: ...

    async def edit(
        self,
        content: Optional[str] = MISSING,
        *,
        embed: Optional[Embed] = MISSING,
        embeds: List[Embed] = MISSING,
        file: File = MISSING,
        files: List[File] = MISSING,
        attachments: Optional[List[Attachment]] = MISSING,
        suppress: bool = MISSING,  # deprecated
        suppress_embeds: bool = MISSING,
        flags: MessageFlags = MISSING,
        allowed_mentions: Optional[AllowedMentions] = MISSING,
        view: Optional[View] = MISSING,
        components: Optional[MessageComponentInput] = MISSING,
        delete_after: Optional[float] = None,
    ) -> Message:
        """|coro|

        Edits the message.

        The content must be able to be transformed into a string via ``str(content)``.

        .. note::
            If the original message has embeds with images that were created from local files
            (using the ``file`` parameter with :meth:`Embed.set_image` or :meth:`Embed.set_thumbnail`),
            those images will be removed if the message's attachments are edited in any way
            (i.e. by setting ``file``/``files``/``attachments``, or adding an embed with local files).

        .. note::

            This method cannot be used on messages authored by others, with one exception.
            The ``suppress_embeds`` parameter can be used to change the state of embeds on
            other users' messages, requiring the :attr:`~.Permissions.manage_messages` permission.

        .. versionchanged:: 2.1
            :class:`disnake.Message` is always returned.

        .. versionchanged:: 2.5
            The ``suppress`` keyword-only parameter was deprecated
            in favor of ``suppress_embeds``.

        .. versionchanged:: 2.6
            Raises :exc:`TypeError` instead of ``InvalidArgument``.

        Parameters
        ----------
        content: Optional[:class:`str`]
            The new content to replace the message with.
            Could be ``None`` to remove the content.
        embed: Optional[:class:`Embed`]
            The new embed to replace the original with. This cannot be mixed with the
            ``embeds`` parameter.
            Could be ``None`` to remove the embed.
        embeds: List[:class:`Embed`]
            The new embeds to replace the original with. Must be a maximum of 10.
            This cannot be mixed with the ``embed`` parameter.
            To remove all embeds ``[]`` should be passed.

            .. versionadded:: 2.1

        file: :class:`File`
            The file to upload. This cannot be mixed with the ``files`` parameter.
            Files will be appended to the message, see the ``attachments`` parameter
            to remove/replace existing files.

            .. versionadded:: 2.1

        files: List[:class:`File`]
            A list of files to upload. This cannot be mixed with the ``file`` parameter.
            Files will be appended to the message, see the ``attachments`` parameter
            to remove/replace existing files.

            .. versionadded:: 2.1

        attachments: Optional[List[:class:`Attachment`]]
            A list of attachments to keep in the message.
            If ``[]`` or ``None`` is passed then all existing attachments are removed.
            Keeps existing attachments if not provided.

            .. versionadded:: 2.1

            .. versionchanged:: 2.5
                Supports passing ``None`` to clear attachments.

        suppress_embeds: :class:`bool`
            Whether to suppress embeds for the message. This hides
            all the embeds from the UI if set to ``True``. If set
            to ``False``, this brings the embeds back if they were
            suppressed.
        flags: :class:`MessageFlags`
            The new flags to set for this message. Overrides existing flags.
            Only :attr:`~MessageFlags.suppress_embeds` is supported.

            If parameter ``suppress_embeds`` is provided,
            that will override the setting of :attr:`.MessageFlags.suppress_embeds`.

            .. versionadded:: 2.9

        delete_after: Optional[:class:`float`]
            If provided, the number of seconds to wait in the background
            before deleting the message we just edited. If the deletion fails,
            then it is silently ignored.
        allowed_mentions: Optional[:class:`~disnake.AllowedMentions`]
            Controls the mentions being processed in this message. If this is
            passed, then the object is merged with :attr:`Client.allowed_mentions`.
            The merging behaviour only overrides attributes that have been explicitly passed
            to the object, otherwise it uses the attributes set in :attr:`Client.allowed_mentions`.

            .. note::
                Unlike :meth:`Message.edit`, this does not default to
                :attr:`Client.allowed_mentions` if no object is passed.
        view: Optional[:class:`~disnake.ui.View`]
            The updated view to update this message with. This cannot be mixed with ``components``.
            If ``None`` is passed then the view is removed.

            .. versionadded:: 2.0

        components: |components_type|
            The updated components to update this message with. This cannot be mixed with ``view``.
            If ``None`` is passed then the components are removed.

            .. versionadded:: 2.4

        Raises
        ------
        NotFound
            The message was not found.
        HTTPException
            Editing the message failed.
        Forbidden
            Tried to suppress embeds on a message without permissions or
            edited a message's content or embed that isn't yours.
        TypeError
            You specified both ``embed`` and ``embeds``, or ``file`` and ``files``, or ``view`` and ``components``.

        Returns
        -------
        :class:`Message`
            The message that was edited.
        """
        # if no attachment list was provided but we're uploading new files,
        # use current attachments as the base
        if attachments is MISSING and (file or files):
            attachments = (await self.fetch()).attachments

        return await _edit_handler(
            self,
            default_flags=0,
            previous_allowed_mentions=None,
            content=content,
            embed=embed,
            embeds=embeds,
            file=file,
            files=files,
            attachments=attachments,
            suppress=suppress,
            suppress_embeds=suppress_embeds,
            flags=flags,
            allowed_mentions=allowed_mentions,
            view=view,
            components=components,
            delete_after=delete_after,
        )


class ForwardedMessage:
    """Represents a forwarded :class:`Message`.

    .. versionadded:: 2.10

    Attributes
    ----------
    type: :class:`MessageType`
        The type of message.
    content: :class:`str`
        The actual contents of the message.
    embeds: List[:class:`Embed`]
        A list of embeds the message has.
    channel_id: :class:`int`
        The ID of the channel where the message was forwarded from.
    attachments: List[:class:`Attachment`]
        A list of attachments given to a message.
    flags: :class:`MessageFlags`
        Extra features of the message.
    mentions: List[:class:`abc.User`]
        A list of :class:`Member` that were mentioned. If the message is in a private message
        then the list will be of :class:`User` instead. For messages that are not of type
        :attr:`MessageType.default`\\, this array can be used to aid in system messages.
        For more information, see :attr:`Message.system_content`.

        .. warning::

            The order of the mentions list is not in any particular order so you should
            not rely on it. This is a Discord limitation, not one with the library.
    role_mentions: List[:class:`Role`]
        A list of :class:`Role` that were mentioned. If the message is in a private message
        then the list is always empty.
    stickers: List[:class:`StickerItem`]
        A list of sticker items given to the message.
    components: List[:class:`Component`]
        A list of components in the message.
    guild_id: Optional[:class:`int`]
        The guild ID where the message was forwarded from, if applicable.
    """

    __slots__ = (
        "_state",
        "type",
        "content",
        "embeds",
        "channel_id",
        "attachments",
        "_timestamp",
        "_edited_timestamp",
        "flags",
        "mentions",
        "role_mentions",
        "stickers",
        "components",
        "guild_id",
    )

    def __init__(
        self,
        *,
        state: ConnectionState,
        channel_id: Optional[int],
        guild_id: Optional[int],
        data: ForwardedMessagePayload,
    ) -> None:
        self._state = state
        self.type: MessageType = try_enum(MessageType, data["type"])
        self.content: str = data["content"]
        self.embeds: List[Embed] = [Embed.from_dict(a) for a in data["embeds"]]
        # should never be None in message_reference(s) that are forwarding
        self.channel_id: int = channel_id  # type: ignore
        self.attachments: List[Attachment] = [
            Attachment(data=a, state=state) for a in data["attachments"]
        ]
        self._timestamp: datetime.datetime = utils.parse_time(data["timestamp"])
        self._edited_timestamp: Optional[datetime.datetime] = utils.parse_time(
            data["edited_timestamp"]
        )
        self.flags: MessageFlags = MessageFlags._from_value(data.get("flags", 0))
        self.stickers: List[StickerItem] = [
            StickerItem(data=d, state=state) for d in data.get("sticker_items", [])
        ]
        self.components: List[MessageTopLevelComponent] = [
            _message_component_factory(d) for d in data.get("components", [])
        ]
        self.guild_id = guild_id

        self.mentions: List[Union[User, Member]] = []
        if self.guild is None:
            self.mentions = [state.store_user(m) for m in data["mentions"]]
        else:
            for mention in filter(None, data["mentions"]):
                id_search = int(mention["id"])
                member = self.guild.get_member(id_search)
                if member is not None:
                    self.mentions.append(member)
                else:
                    self.mentions.append(
                        Member._try_upgrade(data=mention, guild=self.guild, state=state)
                    )

        self.role_mentions: List[Role] = []
        if self.guild is not None:
            for role_id in map(int, data.get("mention_roles", [])):
                role = self.guild.get_role(role_id)
                if role is not None:
                    self.role_mentions.append(role)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}>"

    @property
    def guild(self) -> Optional[Guild]:
        """Optional[:class:`disnake.Guild`]: The guild where the message was forwarded from, if applicable.
        This could be ``None`` if the guild is not cached.
        """
        return self._state._get_guild(self.guild_id)

    @property
    def channel(self) -> Optional[Union[GuildChannel, Thread, PartialMessageable]]:
        """Optional[Union[:class:`TextChannel`, :class:`VoiceChannel`, :class:`StageChannel`, :class:`Thread`, :class:`PartialMessageable`]]:
        The channel that the message was forwarded from. This could be ``None`` if the channel is not cached or a
        :class:`disnake.PartialMessageable` if the ``guild`` is not cached or if the message forwarded is not coming from a guild (e.g DMs).
        """
        if self.guild:
            channel = self.guild.get_channel_or_thread(self.channel_id)
        else:
            channel = PartialMessageable(state=self._state, id=self.channel_id)
        return channel

    @property
    def created_at(self) -> datetime.datetime:
        """:class:`datetime.datetime`: The message's creation time in UTC."""
        return self._timestamp

    @property
    def edited_at(self) -> Optional[datetime.datetime]:
        """Optional[:class:`datetime.datetime`]: An aware UTC datetime object containing the edited time of the message."""
        return self._edited_timestamp
