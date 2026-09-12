"""initial postgres schema

Revision ID: 0001_initial
Revises: 
Create Date: 2026-09-12 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0001_initial'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

    op.create_table(
        'guild_configs',
        sa.Column('bot_id', sa.String(length=32), nullable=False),
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('ver', sa.Float(), server_default='1.10', nullable=False),
        sa.Column('autoplay', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('check_other_bots_in_vc', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('enable_restrict_mode', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('default_player_volume', sa.Integer(), server_default='100', nullable=False),
        sa.Column('enable_prefixed_commands', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('djroles', json_type, server_default='[]', nullable=False),
        sa.Column('player_controller', json_type, server_default='{}', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('bot_id', 'guild_id')
    )

    op.create_table(
        'guild_global_configs',
        sa.Column('guild_id', sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column('ver', sa.Float(), server_default='1.4', nullable=False),
        sa.Column('prefix', sa.String(length=20), server_default='', nullable=False),
        sa.Column('global_skin', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('player_skin', sa.String(length=100), nullable=True),
        sa.Column('player_skin_static', sa.String(length=100), nullable=True),
        sa.Column('voice_channel_status', sa.String(length=255), server_default='', nullable=False),
        sa.Column('custom_skins', json_type, server_default='{}', nullable=False),
        sa.Column('custom_skins_static', json_type, server_default='{}', nullable=False),
        sa.Column('listen_along_invites', json_type, server_default='{}', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('guild_id')
    )

    op.create_table(
        'user_configs',
        sa.Column('bot_id', sa.String(length=32), nullable=False),
        sa.Column('user_id', sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column('ver', sa.Float(), server_default='1.0', nullable=False),
        sa.Column('fav_links', json_type, server_default='{}', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('bot_id', 'user_id')
    )

    op.create_table(
        'user_global_configs',
        sa.Column('user_id', sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column('ver', sa.Float(), server_default='1.4', nullable=False),
        sa.Column('token', sa.String(length=255), server_default='', nullable=False),
        sa.Column('custom_prefix', sa.String(length=20), server_default='', nullable=False),
        sa.Column('fav_links', json_type, server_default='{}', nullable=False),
        sa.Column('integration_links', json_type, server_default='{}', nullable=False),
        sa.Column('last_tracks', json_type, server_default='[]', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('user_id')
    )

    op.create_table(
        'default_configs',
        sa.Column('id', sa.String(length=50), nullable=False),
        sa.Column('ver', sa.Float(), server_default='1.0', nullable=False),
        sa.Column('extra_tokens', json_type, server_default='{}', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_table(
        'player_sessions',
        sa.Column('bot_id', sa.String(length=32), nullable=False),
        sa.Column('guild_id', sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column('data', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('bot_id', 'guild_id')
    )

    op.create_table(
        'guild_tts_langs',
        sa.Column('guild_id', sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column('language', sa.String(length=50), server_default='Tiếng Việt', nullable=False),
        sa.PrimaryKeyConstraint('guild_id')
    )


def downgrade() -> None:
    op.drop_table('guild_tts_langs')
    op.drop_table('player_sessions')
    op.drop_table('default_configs')
    op.drop_table('user_global_configs')
    op.drop_table('user_configs')
    op.drop_table('guild_global_configs')
    op.drop_table('guild_configs')
