from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord

from services.modlog import send_modlog_embed
from utils.discord_helpers import ensure_muted_role
from utils.i18n import tr


DISCORD_TIMEOUT_MAX_SECONDS = 28 * 24 * 60 * 60


@dataclass(frozen=True)
class AdminActionResult:
    success: bool
    message: str
    code: str
    mute_mode: str | None = None


class AdminActionService:
    def __init__(self, bot) -> None:  # noqa: ANN001
        self.bot = bot
        self.db = getattr(bot, "db", None)

    @staticmethod
    def _has_perm(member: discord.Member, name: str) -> bool:
        perms = getattr(member, "guild_permissions", None)
        return bool(getattr(perms, "administrator", False) or getattr(perms, name, False))

    def _actor_has(self, actor: discord.Member, permission: str) -> bool:
        if bool(getattr(self.bot, "is_owner_user", lambda _user: False)(actor)):
            return True
        return self._has_perm(actor, permission)

    @staticmethod
    def _bot_member(guild: discord.Guild) -> discord.Member | None:
        return getattr(guild, "me", None)

    @classmethod
    def _has_channel_perm(cls, member: discord.Member, channel: discord.abc.GuildChannel, name: str) -> bool:
        permissions_for = getattr(channel, "permissions_for", None)
        if permissions_for is not None:
            try:
                perms = permissions_for(member)
                return bool(getattr(perms, "administrator", False) or getattr(perms, name, False))
            except Exception:
                logging.exception("Failed to read channel permissions for admin action")
        return cls._has_perm(member, name)

    def _can_act_on_target(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        target: discord.Member,
        lang: str,
    ) -> AdminActionResult | None:
        if actor.id == target.id:
            return AdminActionResult(False, tr(lang, "You cannot moderate yourself.", "No puedes moderarte a ti mismo."), "self_target")
        if target.id == guild.owner_id:
            return AdminActionResult(False, tr(lang, "You cannot moderate the server owner.", "No puedes moderar al propietario del servidor."), "target_is_owner")
        if (
            actor.id != guild.owner_id
            and not bool(getattr(self.bot, "is_owner_user", lambda _user: False)(actor))
            and actor.top_role <= target.top_role
        ):
            return AdminActionResult(False, tr(lang, "Your highest role must be above the target's highest role.", "Tu rol mas alto debe estar por encima del rol mas alto del objetivo."), "actor_hierarchy")
        me = self._bot_member(guild)
        if me is None:
            return AdminActionResult(False, tr(lang, "Bot member not found in this guild.", "No se encontro al bot como miembro en este servidor."), "bot_member_missing")
        if me.top_role <= target.top_role:
            return AdminActionResult(False, tr(lang, "My highest role must be above the target's highest role.", "Mi rol mas alto debe estar por encima del rol mas alto del objetivo."), "bot_hierarchy")
        return None

    def _can_manage_role(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        role: discord.Role,
        lang: str,
    ) -> AdminActionResult | None:
        is_default = getattr(role, "is_default", None)
        if callable(is_default) and is_default():
            return AdminActionResult(False, tr(lang, "You cannot manage the @everyone role.", "No puedes gestionar el rol @everyone."), "role_is_default")
        if bool(getattr(role, "managed", False)):
            return AdminActionResult(False, tr(lang, "That role is managed by an integration and cannot be edited manually.", "Ese rol esta gestionado por una integracion y no se puede editar manualmente."), "role_is_managed")
        if not self._actor_has(actor, "manage_roles"):
            return AdminActionResult(False, tr(lang, "You do not have permission to manage roles.", "No tienes permisos para gestionar roles."), "author_missing_manage_roles")
        me = self._bot_member(guild)
        if me is None:
            return AdminActionResult(False, tr(lang, "Bot member not found in this guild.", "No se encontro al bot como miembro en este servidor."), "bot_member_missing")
        if not self._has_perm(me, "manage_roles"):
            return AdminActionResult(False, tr(lang, "I need Manage Roles permission to do that.", "Necesito Gestionar roles para hacer eso."), "bot_missing_manage_roles")
        if role >= me.top_role:
            return AdminActionResult(False, tr(lang, "I cannot manage that role due to role hierarchy.", "No puedo gestionar ese rol por la jerarquia de roles."), "bot_role_hierarchy")
        if (
            actor.id != guild.owner_id
            and not bool(getattr(self.bot, "is_owner_user", lambda _user: False)(actor))
            and role >= actor.top_role
        ):
            return AdminActionResult(False, tr(lang, "You cannot manage a role equal to or higher than your top role.", "No puedes gestionar un rol igual o superior a tu rol mas alto."), "actor_role_hierarchy")
        return None

    async def _log_action(
        self,
        guild: discord.Guild,
        *,
        title: str,
        moderator: discord.abc.User,
        target: str,
        reason: str,
        details: str | None = None,
    ) -> None:
        embed = discord.Embed(
            title=title,
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Moderator", value=moderator.mention, inline=True)
        embed.add_field(name="Target", value=target, inline=True)
        embed.add_field(name="Reason", value=reason[:1024], inline=False)
        if details:
            embed.add_field(name="Details", value=details[:1024], inline=False)
        await send_modlog_embed(guild, self.db, embed)

    async def delete_recent_messages(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        channel: discord.TextChannel,
        trigger_message: discord.Message,
        count: int,
        *,
        lang: str = "en",
    ) -> AdminActionResult:
        if count < 1 or count > 500:
            return AdminActionResult(False, tr(lang, "Amount must be between 1 and 500.", "La cantidad debe estar entre 1 y 500."), "invalid_message_count")
        if not self._actor_has(actor, "manage_messages"):
            return AdminActionResult(False, tr(lang, "You do not have permission to delete messages.", "No tienes permisos para eliminar mensajes."), "author_missing_manage_messages")
        me = self._bot_member(guild)
        if me is None:
            return AdminActionResult(False, tr(lang, "Bot member not found in this guild.", "No se encontro al bot como miembro en este servidor."), "bot_member_missing")
        if not self._has_channel_perm(me, channel, "manage_messages"):
            return AdminActionResult(False, tr(lang, "I need Manage Messages permission to delete messages here.", "Necesito Gestionar mensajes para eliminar mensajes aqui."), "bot_missing_manage_messages")
        purge = getattr(channel, "purge", None)
        if purge is None:
            return AdminActionResult(False, tr(lang, "This action only works in text channels.", "Esta accion solo funciona en canales de texto."), "channel_unsupported")
        try:
            deleted = await purge(limit=count, before=trigger_message)
        except discord.Forbidden:
            return AdminActionResult(False, tr(lang, "I do not have permission to delete messages here.", "No tengo permisos para eliminar mensajes aqui."), "delete_messages_forbidden")
        except discord.HTTPException as exc:
            return AdminActionResult(False, tr(lang, f"Failed to delete messages: {exc}", f"No se pudieron eliminar los mensajes: {exc}"), "delete_messages_http_error")
        deleted_count = len(deleted or [])
        await self._log_action(
            guild,
            title="Messages Deleted",
            moderator=actor,
            target=f"#{getattr(channel, 'name', 'channel')}",
            reason="AI bulk delete requested",
            details=f"Requested: {count} | Deleted: {deleted_count}",
        )
        return AdminActionResult(True, tr(lang, "Messages deleted.", "Mensajes eliminados."), "messages_deleted")

    async def delete_role(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        role: discord.Role,
        *,
        reason: str | None = None,
        lang: str = "en",
    ) -> AdminActionResult:
        role_error = self._can_manage_role(guild, actor, role, lang)
        if role_error is not None:
            return role_error
        reason_text = reason or tr(lang, "No reason provided.", "Sin razon proporcionada.")
        role_label = getattr(role, "mention", None) or getattr(role, "name", "role")
        try:
            await role.delete(reason=f"Role deleted by {actor} | {reason_text}")
        except discord.Forbidden:
            return AdminActionResult(False, tr(lang, "I do not have permission to delete that role.", "No tengo permisos para eliminar ese rol."), "delete_role_forbidden")
        except discord.HTTPException as exc:
            return AdminActionResult(False, tr(lang, f"Failed to delete role: {exc}", f"No se pudo eliminar el rol: {exc}"), "delete_role_http_error")
        await self._log_action(
            guild,
            title="Role Deleted",
            moderator=actor,
            target=f"{getattr(role, 'name', role_label)} ({getattr(role, 'id', 'unknown')})",
            reason=reason_text,
        )
        return AdminActionResult(True, tr(lang, f"Deleted role {role_label}.", f"Rol eliminado {role_label}."), "role_deleted")

    async def mute_member(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        target: discord.Member,
        *,
        duration_seconds: int | None = None,
        duration_label: str | None = None,
        reason: str | None = None,
        mute_mode: str = "auto",
        lang: str = "en",
    ) -> AdminActionResult:
        reason_text = reason or tr(lang, "No reason provided.", "Sin razon proporcionada.")
        mode = mute_mode if mute_mode in {"auto", "timeout", "role_mute"} else "auto"
        hierarchy_error = self._can_act_on_target(guild, actor, target, lang)
        if hierarchy_error is not None:
            return hierarchy_error
        if target.guild_permissions.administrator:
            return AdminActionResult(False, tr(lang, "Cannot mute an administrator due to Discord permission rules.", "No se puede silenciar a un administrador por las reglas de permisos de Discord."), "target_is_admin")
        if not self._actor_has(actor, "moderate_members"):
            return AdminActionResult(False, tr(lang, "You do not have permission to mute members.", "No tienes permisos para silenciar miembros."), "author_missing_moderate_members")

        if duration_seconds is not None and 1 <= duration_seconds <= DISCORD_TIMEOUT_MAX_SECONDS and mode in {"auto", "timeout"}:
            me = self._bot_member(guild)
            if me is None or not self._has_perm(me, "moderate_members"):
                if mode == "timeout":
                    return AdminActionResult(False, tr(lang, "I need Moderate Members permission to use timeout.", "Necesito el permiso Moderar miembros para usar timeout."), "bot_missing_moderate_members")
            else:
                until = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)
                try:
                    await target.timeout(until, reason=f"Timeout by {actor} | {reason_text}")
                except discord.Forbidden:
                    return AdminActionResult(False, tr(lang, "I do not have permission to timeout that user.", "No tengo permisos para poner en timeout a ese usuario."), "bot_timeout_forbidden")
                except discord.HTTPException as exc:
                    return AdminActionResult(False, tr(lang, f"Failed to timeout user: {exc}", f"No se pudo poner en timeout al usuario: {exc}"), "timeout_http_error")
                label = duration_label or f"{duration_seconds}s"
                await self._log_action(
                    guild,
                    title="User Timed Out",
                    moderator=actor,
                    target=target.mention,
                    reason=reason_text,
                    details=f"Duration: {label} | mute_mode=timeout",
                )
                return AdminActionResult(
                    True,
                    tr(
                        lang,
                        f"Done, put {target.mention} in timeout for {label}. Reason: {reason_text}",
                        f"Listo, puse a {target.mention} en timeout por {label}. Motivo: {reason_text}",
                    ),
                    "timeout_applied",
                    mute_mode="timeout",
                )

        return await self._role_mute_member(
            guild,
            actor,
            target,
            duration_seconds=duration_seconds,
            duration_label=duration_label,
            reason_text=reason_text,
            lang=lang,
        )

    async def _role_mute_member(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        target: discord.Member,
        *,
        duration_seconds: int | None,
        duration_label: str | None,
        reason_text: str,
        lang: str,
    ) -> AdminActionResult:
        me = self._bot_member(guild)
        if me is None or not self._has_perm(me, "manage_roles"):
            return AdminActionResult(False, tr(lang, "I need Manage Roles permission to use the Muted role fallback.", "Necesito Gestionar roles para usar el rol Muted."), "bot_missing_manage_roles", mute_mode="role_mute")
        muted_role = await ensure_muted_role(guild, self.db, reason=f"Requested by {actor}")
        if me.top_role <= muted_role:
            return AdminActionResult(False, tr(lang, "My highest role must be above the Muted role.", "Mi rol mas alto debe estar por encima del rol Muted."), "bot_below_muted_role", mute_mode="role_mute")
        if muted_role in target.roles:
            return AdminActionResult(False, tr(lang, f"{target.mention} is already muted.", f"{target.mention} ya esta silenciado."), "already_muted", mute_mode="role_mute")
        try:
            await target.add_roles(muted_role, reason=f"Muted by {actor} | {reason_text}")
        except discord.Forbidden:
            return AdminActionResult(False, tr(lang, "I do not have permission to add the Muted role.", "No tengo permisos para agregar el rol Muted."), "role_mute_forbidden", mute_mode="role_mute")
        except discord.HTTPException as exc:
            return AdminActionResult(False, tr(lang, f"Failed to mute user: {exc}", f"No se pudo silenciar al usuario: {exc}"), "role_mute_http_error", mute_mode="role_mute")
        label = duration_label or (f"{duration_seconds}s" if duration_seconds else None)
        if duration_seconds:
            await self.db.upsert_temp_action(
                guild_id=guild.id,
                user_id=target.id,
                action="tempmute",
                expires_at=int(datetime.now(timezone.utc).timestamp()) + duration_seconds,
                duration_input=label or f"{duration_seconds}s",
                reason=reason_text,
                moderator_id=actor.id,
            )
        details = f"mute_mode=role_mute" + (f" | Duration: {label}" if label else "")
        await self._log_action(
            guild,
            title="User Muted",
            moderator=actor,
            target=target.mention,
            reason=reason_text,
            details=details,
        )
        logging.info("admin_action mute_mode=role_mute guild=%s target=%s duration_seconds=%s", guild.id, target.id, duration_seconds)
        message = (
            tr(
                lang,
                f"Done, applied Muted role to {target.mention} for {label}. Reason: {reason_text}",
                f"Listo, le aplique el rol Muted a {target.mention} por {label}. Motivo: {reason_text}",
            )
            if label
            else tr(
                lang,
                f"Done, applied Muted role to {target.mention}. Reason: {reason_text}",
                f"Listo, le aplique el rol Muted a {target.mention}. Motivo: {reason_text}",
            )
        )
        return AdminActionResult(True, message, "role_mute_applied", mute_mode="role_mute")

    async def unmute_member(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        target: discord.Member,
        *,
        reason: str | None = None,
        lang: str = "en",
    ) -> AdminActionResult:
        reason_text = reason or tr(lang, "No reason provided.", "Sin razon proporcionada.")
        if not self._actor_has(actor, "moderate_members"):
            return AdminActionResult(False, tr(lang, "You do not have permission to unmute members.", "No tienes permisos para quitar silencios."), "author_missing_moderate_members")
        hierarchy_error = self._can_act_on_target(guild, actor, target, lang)
        if hierarchy_error is not None:
            return hierarchy_error
        changed = False
        try:
            if getattr(target, "timed_out_until", None) is not None:
                await target.timeout(None, reason=f"Timeout removed by {actor} | {reason_text}")
                changed = True
        except discord.Forbidden:
            return AdminActionResult(False, tr(lang, "I do not have permission to remove timeout from that user.", "No tengo permisos para quitarle el timeout a ese usuario."), "bot_timeout_forbidden")
        settings = await self.db.get_guild_settings(guild.id)
        muted_role = guild.get_role(settings.muted_role_id or 0) or discord.utils.get(guild.roles, name="Muted")
        if muted_role is not None and muted_role in target.roles:
            try:
                await target.remove_roles(muted_role, reason=f"Unmuted by {actor} | {reason_text}")
                changed = True
            except discord.Forbidden:
                return AdminActionResult(False, tr(lang, "I do not have permission to remove the Muted role.", "No tengo permisos para quitar el rol Muted."), "role_unmute_forbidden")
        if not changed:
            return AdminActionResult(False, tr(lang, f"{target.mention} is not muted.", f"{target.mention} no esta silenciado."), "not_muted")
        await self._log_action(guild, title="User Unmuted", moderator=actor, target=target.mention, reason=reason_text)
        return AdminActionResult(True, tr(lang, f"{target.mention} has been unmuted. Reason: {reason_text}", f"Se ha quitado el silencio a {target.mention}. Razon: {reason_text}"), "unmuted")

    async def set_channel_lock(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        channel: discord.TextChannel,
        *,
        locked: bool,
        lang: str = "en",
    ) -> AdminActionResult:
        if not self._actor_has(actor, "manage_channels"):
            return AdminActionResult(False, tr(lang, "You do not have permission to manage channels.", "No tienes permisos para gestionar canales."), "author_missing_manage_channels")
        me = self._bot_member(guild)
        if me is None or not self._has_perm(me, "manage_channels"):
            return AdminActionResult(False, tr(lang, "I need Manage Channels permission to do that.", "Necesito Gestionar canales para hacer eso."), "bot_missing_manage_channels")
        overwrite = channel.overwrites_for(guild.default_role)
        if locked and overwrite.send_messages is False:
            return AdminActionResult(False, tr(lang, f"{channel.mention} is already locked.", f"{channel.mention} ya esta bloqueado."), "already_locked")
        if not locked and overwrite.send_messages is None:
            return AdminActionResult(False, tr(lang, f"{channel.mention} is already unlocked.", f"{channel.mention} ya esta desbloqueado."), "already_unlocked")
        overwrite.send_messages = False if locked else None
        try:
            await channel.set_permissions(
                guild.default_role,
                overwrite=overwrite,
                reason=f"Channel {'locked' if locked else 'unlocked'} by {actor}",
            )
        except discord.Forbidden:
            return AdminActionResult(False, tr(lang, "I do not have permission to change this channel.", "No tengo permisos para cambiar este canal."), "channel_forbidden")
        except discord.HTTPException as exc:
            return AdminActionResult(False, tr(lang, f"Failed to update channel: {exc}", f"No se pudo actualizar el canal: {exc}"), "channel_http_error")
        title = "Channel Locked" if locked else "Channel Unlocked"
        await self._log_action(guild, title=title, moderator=actor, target=channel.mention, reason="AI/admin channel lock" if locked else "AI/admin channel unlock")
        return AdminActionResult(
            True,
            tr(lang, f"{'Locked' if locked else 'Unlocked'} {channel.mention}.", f"Canal {'bloqueado' if locked else 'desbloqueado'} {channel.mention}."),
            "locked" if locked else "unlocked",
        )
