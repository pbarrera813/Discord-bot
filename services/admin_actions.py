from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord

from services.modlog import send_modlog_embed
from services.server_memory import ServerMemoryService
from utils.discord_helpers import WARNING_ROLE_NAMES, ensure_muted_role, ensure_warning_roles
from utils.i18n import tr


DISCORD_TIMEOUT_MAX_SECONDS = 28 * 24 * 60 * 60


@dataclass(frozen=True)
class AdminActionResult:
    success: bool
    message: str
    code: str
    mute_mode: str | None = None
    resource_handle: dict[str, object] | None = None
    data: dict[str, object] | None = None


class AdminActionService:
    def __init__(self, bot) -> None:
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
        *,
        allow_self: bool = False,
    ) -> AdminActionResult | None:
        # Moderating yourself is refused, but changing your own nickname is harmless, so that action passes allow_self.
        self_target = actor.id == target.id
        if self_target and not allow_self:
            return AdminActionResult(False, tr(lang, "You cannot moderate yourself.", "No puedes moderarte a ti mismo."), "self_target")
        if target.id == guild.owner_id:
            return AdminActionResult(False, tr(lang, "You cannot moderate the server owner.", "No puedes moderar al propietario del servidor."), "target_is_owner")
        if (
            not self_target
            and actor.id != guild.owner_id
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
        event_key: tuple[str, int] | None = None,
    ) -> None:
        color = discord.Color.blue()
        if event_key is not None:
            color = discord.Color.red() if event_key[0] == "channel_delete" else discord.Color.green()
        embed = discord.Embed(
            title=title,
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Moderator", value=moderator.mention, inline=True)
        if title in {"Channel Created", "Channel Deleted"}:
            embed.description = target
        else:
            embed.add_field(name="Target", value=target, inline=True)
        if reason:
            embed.add_field(name="Reason", value=reason[:1024], inline=False)
        if details:
            embed.add_field(name="Details", value=details[:1024], inline=False)
        if event_key is not None:
            embed.set_footer(text=f"Channel ID: {event_key[1]} | User ID: {moderator.id}")
        try:
            await send_modlog_embed(guild, self.db, embed, **(
                {"event_key": event_key, "event_enriched": True} if event_key is not None else {}
            ))
        except Exception:
            # The Discord mutation already happened; logging must not make a
            # successful effect appear retryable.
            logging.exception("Admin action log delivery failed after effect")

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

    async def create_role(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        role_name: str,
        *,
        color_hex: str | None = None,
        reason: str | None = None,
        lang: str = "en",
    ) -> AdminActionResult:
        name = " ".join(str(role_name or "").split())[:100]
        if not name:
            return AdminActionResult(False, tr(lang, "I need a role name to create it.", "Necesito un nombre de rol para crearlo."), "missing_role_name")
        if not self._actor_has(actor, "manage_roles"):
            return AdminActionResult(False, tr(lang, "You do not have permission to manage roles.", "No tienes permisos para gestionar roles."), "author_missing_manage_roles")
        me = self._bot_member(guild)
        if me is None:
            return AdminActionResult(False, tr(lang, "Bot member not found in this guild.", "No se encontro al bot como miembro en este servidor."), "bot_member_missing")
        if not self._has_perm(me, "manage_roles"):
            return AdminActionResult(False, tr(lang, "I need Manage Roles permission to do that.", "Necesito Gestionar roles para hacer eso."), "bot_missing_manage_roles")
        if any(str(getattr(role, "name", "")).casefold() == name.casefold() for role in getattr(guild, "roles", ())):
            return AdminActionResult(False, tr(lang, "A role with that name already exists.", "Ya existe un rol con ese nombre."), "role_already_exists")

        normalized_color: str | None = None
        if color_hex:
            match = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(color_hex).strip())
            if match is None:
                return AdminActionResult(False, tr(lang, "Role color must be a six-digit hex value.", "El color del rol debe ser un valor hexadecimal de seis digitos."), "invalid_role_color")
            normalized_color = f"#{match.group(1).lower()}"
        color = discord.Color(int(normalized_color[1:], 16)) if normalized_color else discord.Color.default()
        reason_text = reason or tr(lang, "No reason provided.", "Sin razon proporcionada.")
        try:
            role = await guild.create_role(name=name, color=color, reason=f"Role created by {actor} | {reason_text}")
        except discord.Forbidden:
            return AdminActionResult(False, tr(lang, "I do not have permission to create that role.", "No tengo permisos para crear ese rol."), "create_role_forbidden")
        except discord.HTTPException as exc:
            return AdminActionResult(False, tr(lang, f"Failed to create role: {exc}", f"No se pudo crear el rol: {exc}"), "create_role_http_error")
        await self._log_action(
            guild,
            title="Role Created",
            moderator=actor,
            target=f"{getattr(role, 'name', name)} ({getattr(role, 'id', 'unknown')})",
            reason=reason_text,
            details=f"Color: {normalized_color or 'default'}",
        )
        return AdminActionResult(
            True,
            tr(lang, f"Created role {getattr(role, 'mention', name)}.", f"Rol creado {getattr(role, 'mention', name)}."),
            "role_created",
            resource_handle={
                "resource_type": "role",
                "guild_id": getattr(guild, "id", None),
                "resource_id": getattr(role, "id", None),
            },
        )

    async def add_role(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        target: discord.Member,
        role: discord.Role,
        *,
        reason: str | None = None,
        lang: str = "en",
    ) -> AdminActionResult:
        role_error = self._can_manage_role(guild, actor, role, lang)
        if role_error is not None:
            return role_error
        target_error = self._can_act_on_target(guild, actor, target, lang)
        if target_error is not None:
            return target_error
        if role in getattr(target, "roles", ()):
            return AdminActionResult(False, tr(lang, f"{target.mention} already has {role.mention}.", f"{target.mention} ya tiene {role.mention}."), "role_already_assigned")
        reason_text = reason or tr(lang, "No reason provided.", "Sin razon proporcionada.")
        try:
            await target.add_roles(role, reason=f"Role added by {actor} | {reason_text}")
        except discord.Forbidden:
            return AdminActionResult(False, tr(lang, "I do not have permission to add that role.", "No tengo permisos para agregar ese rol."), "add_role_forbidden")
        except discord.HTTPException as exc:
            return AdminActionResult(False, tr(lang, f"Failed to add role: {exc}", f"No se pudo agregar el rol: {exc}"), "add_role_http_error")
        await self._log_action(guild, title="Role Added", moderator=actor, target=f"{target.mention} | {role.mention}", reason=reason_text)
        return AdminActionResult(True, tr(lang, f"Added {role.mention} to {target.mention}.", f"Se agrego {role.mention} a {target.mention}."), "role_added")

    async def remove_role(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        target: discord.Member,
        role: discord.Role,
        *,
        reason: str | None = None,
        lang: str = "en",
    ) -> AdminActionResult:
        role_error = self._can_manage_role(guild, actor, role, lang)
        if role_error is not None:
            return role_error
        target_error = self._can_act_on_target(guild, actor, target, lang)
        if target_error is not None:
            return target_error
        if role not in getattr(target, "roles", ()):
            return AdminActionResult(False, tr(lang, f"{target.mention} does not have {role.mention}.", f"{target.mention} no tiene {role.mention}."), "role_not_assigned")
        reason_text = reason or tr(lang, "No reason provided.", "Sin razon proporcionada.")
        try:
            await target.remove_roles(role, reason=f"Role removed by {actor} | {reason_text}")
        except discord.Forbidden:
            return AdminActionResult(False, tr(lang, "I do not have permission to remove that role.", "No tengo permisos para quitar ese rol."), "remove_role_forbidden")
        except discord.HTTPException as exc:
            return AdminActionResult(False, tr(lang, f"Failed to remove role: {exc}", f"No se pudo quitar el rol: {exc}"), "remove_role_http_error")
        await self._log_action(guild, title="Role Removed", moderator=actor, target=f"{target.mention} | {role.mention}", reason=reason_text)
        return AdminActionResult(True, tr(lang, f"Removed {role.mention} from {target.mention}.", f"Se quito {role.mention} de {target.mention}."), "role_removed")

    async def _remove_member(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        target: discord.Member,
        *,
        action: str,
        duration_seconds: int | None = None,
        reason: str | None = None,
        lang: str = "en",
    ) -> AdminActionResult:
        permission = "kick_members" if action == "kick" else "ban_members"
        if not self._actor_has(actor, permission):
            return AdminActionResult(False, tr(lang, "You do not have permission to do that.", "No tienes permisos para hacer eso."), f"author_missing_{permission}")
        target_error = self._can_act_on_target(guild, actor, target, lang)
        if target_error is not None:
            return target_error
        me = self._bot_member(guild)
        if me is None or not self._has_perm(me, permission):
            return AdminActionResult(False, tr(lang, "I do not have permission to do that.", "No tengo permisos para hacer eso."), f"bot_missing_{permission}")
        reason_text = reason or tr(lang, "No reason provided.", "Sin razon proporcionada.")
        try:
            if action == "kick":
                await target.kick(reason=f"Kicked by {actor} | {reason_text}")
            else:
                await target.ban(reason=f"{'Tempbanned' if action == 'tempban' else 'Banned'} by {actor} | {reason_text}")
        except discord.Forbidden:
            return AdminActionResult(False, tr(lang, "I do not have permission to do that to this user.", "No tengo permisos para hacer eso con este usuario."), f"{action}_forbidden")
        except discord.HTTPException as exc:
            return AdminActionResult(False, tr(lang, f"Failed to {action} user: {exc}", f"No se pudo ejecutar {action}: {exc}"), f"{action}_http_error")
        if action == "tempban" and duration_seconds:
            await self.db.upsert_temp_action(
                guild_id=guild.id,
                user_id=target.id,
                action="tempban",
                expires_at=int(datetime.now(timezone.utc).timestamp()) + duration_seconds,
                duration_input=f"{duration_seconds}s",
                reason=reason_text,
                moderator_id=actor.id,
            )
        title = {"kick": "User Kicked", "ban": "User Banned", "tempban": "User Temporarily Banned"}[action]
        details = f"Duration: {duration_seconds}s" if action == "tempban" and duration_seconds else None
        await self._log_action(guild, title=title, moderator=actor, target=target.mention, reason=reason_text, details=details)
        past = {"kick": "kicked", "ban": "banned", "tempban": "temporarily banned"}[action]
        return AdminActionResult(True, tr(lang, f"{target.mention} has been {past}.", f"{target.mention} ha sido procesado: {action}."), f"{action}_applied")

    async def kick_member(self, guild: discord.Guild, actor: discord.Member, target: discord.Member, *, reason: str | None = None, lang: str = "en") -> AdminActionResult:
        return await self._remove_member(guild, actor, target, action="kick", reason=reason, lang=lang)

    async def ban_member(self, guild: discord.Guild, actor: discord.Member, target: discord.Member, *, reason: str | None = None, lang: str = "en") -> AdminActionResult:
        return await self._remove_member(guild, actor, target, action="ban", reason=reason, lang=lang)

    async def tempban_member(self, guild: discord.Guild, actor: discord.Member, target: discord.Member, *, duration_seconds: int, reason: str | None = None, lang: str = "en") -> AdminActionResult:
        if duration_seconds < 1:
            return AdminActionResult(False, tr(lang, "A temporary ban needs a duration.", "Un ban temporal necesita una duracion."), "invalid_duration")
        return await self._remove_member(guild, actor, target, action="tempban", duration_seconds=duration_seconds, reason=reason, lang=lang)

    async def unban_user(self, guild: discord.Guild, actor: discord.Member, user_id: int, *, reason: str | None = None, lang: str = "en") -> AdminActionResult:
        if user_id < 1:
            return AdminActionResult(False, tr(lang, "Provide a valid user ID.", "Proporciona una ID de usuario valida."), "invalid_user_id")
        if not self._actor_has(actor, "ban_members"):
            return AdminActionResult(False, tr(lang, "You do not have permission to unban users.", "No tienes permisos para desbanear usuarios."), "author_missing_ban_members")
        me = self._bot_member(guild)
        if me is None or not self._has_perm(me, "ban_members"):
            return AdminActionResult(False, tr(lang, "I need Ban Members permission to do that.", "Necesito el permiso Banear miembros para hacer eso."), "bot_missing_ban_members")
        reason_text = reason or tr(lang, "No reason provided.", "Sin razon proporcionada.")
        try:
            await guild.unban(discord.Object(id=user_id), reason=f"Unbanned by {actor} | {reason_text}")
        except discord.NotFound:
            return AdminActionResult(False, tr(lang, "That user is not currently banned.", "Ese usuario no esta baneado actualmente."), "not_banned")
        except discord.Forbidden:
            return AdminActionResult(False, tr(lang, "I do not have permission to unban that user.", "No tengo permisos para desbanear a ese usuario."), "unban_forbidden")
        except discord.HTTPException as exc:
            return AdminActionResult(False, tr(lang, f"Failed to unban user: {exc}", f"No se pudo desbanear al usuario: {exc}"), "unban_http_error")
        await self._log_action(guild, title="User Unbanned", moderator=actor, target=str(user_id), reason=reason_text)
        return AdminActionResult(True, tr(lang, f"Unbanned user `{user_id}`.", f"Se desbaneo al usuario `{user_id}`."), "unbanned")

    async def create_text_channel(self, guild: discord.Guild, actor: discord.Member, channel_name: str, *, lang: str = "en") -> AdminActionResult:
        if not self._actor_has(actor, "manage_channels"):
            return AdminActionResult(False, tr(lang, "You do not have permission to manage channels.", "No tienes permisos para gestionar canales."), "author_missing_manage_channels")
        me = self._bot_member(guild)
        if me is None or not self._has_perm(me, "manage_channels"):
            return AdminActionResult(False, tr(lang, "I need Manage Channels permission to do that.", "Necesito Gestionar canales para hacer eso."), "bot_missing_manage_channels")
        name = re.sub(r"[^a-z0-9\-_]", "", re.sub(r"\s+", "-", str(channel_name).strip().lstrip("#").lower())).strip("-_")[:100]
        if not name:
            return AdminActionResult(False, tr(lang, "Invalid channel name.", "Nombre de canal invalido."), "invalid_channel_name")
        if any(str(getattr(channel, "name", "")).casefold() == name.casefold() for channel in getattr(guild, "text_channels", ())):
            return AdminActionResult(False, tr(lang, "A channel with that name already exists.", "Ya existe un canal con ese nombre."), "channel_already_exists")
        try:
            channel = await guild.create_text_channel(name, reason=f"Channel created by {actor}")
        except discord.Forbidden:
            return AdminActionResult(False, tr(lang, "I do not have permission to create channels.", "No tengo permisos para crear canales."), "create_channel_forbidden")
        except discord.HTTPException as exc:
            return AdminActionResult(False, tr(lang, f"Failed to create channel: {exc}", f"No se pudo crear el canal: {exc}"), "create_channel_http_error")
        await self._log_action(guild, title="Channel Created", moderator=actor, target=f"#{discord.utils.escape_markdown(getattr(channel, 'name', name))}", reason="", event_key=("channel_create", channel.id))
        return AdminActionResult(
            True,
            tr(lang, f"Created channel {getattr(channel, 'mention', '#' + name)}.", f"Canal creado {getattr(channel, 'mention', '#' + name)}."),
            "channel_created",
            resource_handle={
                "resource_type": "channel",
                "guild_id": getattr(guild, "id", None),
                "resource_id": getattr(channel, "id", None),
            },
        )

    async def delete_channel(self, guild: discord.Guild, actor: discord.Member, channel: discord.TextChannel, *, current_channel_id: int | None, reason: str | None = None, lang: str = "en") -> AdminActionResult:
        if current_channel_id is not None and getattr(channel, "id", None) == current_channel_id:
            return AdminActionResult(False, tr(lang, "You cannot delete the current channel.", "No puedes eliminar el canal actual."), "current_channel")
        if not self._actor_has(actor, "manage_channels"):
            return AdminActionResult(False, tr(lang, "You do not have permission to manage channels.", "No tienes permisos para gestionar canales."), "author_missing_manage_channels")
        me = self._bot_member(guild)
        if me is None or not self._has_channel_perm(me, channel, "manage_channels"):
            return AdminActionResult(False, tr(lang, "I need Manage Channels permission to do that.", "Necesito Gestionar canales para hacer eso."), "bot_missing_manage_channels")
        name, channel_id = getattr(channel, "name", "channel"), getattr(channel, "id", "unknown")
        reason_text = reason or tr(lang, "No reason provided.", "Sin razon proporcionada.")
        try:
            await channel.delete(reason=f"Channel deleted by {actor} | {reason_text}")
        except discord.Forbidden:
            return AdminActionResult(False, tr(lang, "I do not have permission to delete channels.", "No tengo permisos para eliminar canales."), "delete_channel_forbidden")
        except discord.HTTPException as exc:
            return AdminActionResult(False, tr(lang, f"Failed to delete channel: {exc}", f"No se pudo eliminar el canal: {exc}"), "delete_channel_http_error")
        await self._log_action(guild, title="Channel Deleted", moderator=actor, target=f"#{discord.utils.escape_markdown(name)}", reason=reason_text, event_key=("channel_delete", channel.id))
        return AdminActionResult(True, tr(lang, f"Deleted channel #{name}.", f"Canal eliminado #{name}."), "channel_deleted")

    async def _sync_warning_roles(self, guild: discord.Guild, member: discord.Member, count: int, *, reason: str) -> None:
        existing = [discord.utils.get(guild.roles, name=name) for name in WARNING_ROLE_NAMES.values()]
        assigned = [role for role in existing if role is not None and role in getattr(member, "roles", ())]
        try:
            if count <= 0:
                if assigned:
                    await member.remove_roles(*assigned, reason=reason)
                return
            roles = await ensure_warning_roles(guild, reason=reason)
            selected = roles[min(max(count, 1), 3)]
            remove = [role for level, role in roles.items() if level != min(max(count, 1), 3) and role in getattr(member, "roles", ())]
            if remove:
                await member.remove_roles(*remove, reason=reason)
            if selected not in getattr(member, "roles", ()):
                await member.add_roles(selected, reason=reason)
        except (discord.Forbidden, discord.HTTPException):
            return

    async def _apply_warning_punishment(self, guild: discord.Guild, actor: discord.Member, target: discord.Member, count: int, reason: str) -> str:
        if count in {1, 2, 3}:
            label, seconds = {1: ("1d", 86400), 2: ("3d", 259200), 3: ("7d", 604800)}[count]
            try:
                muted_role = await ensure_muted_role(guild, self.db, reason=f"Warning punishment requested by {actor}")
                await target.add_roles(muted_role, reason=f"Auto-tempmute from warning {count} by {actor} | {reason}")
                await self.db.upsert_temp_action(
                    guild_id=guild.id,
                    user_id=target.id,
                    action="tempmute",
                    expires_at=int(datetime.now(timezone.utc).timestamp()) + seconds,
                    duration_input=label,
                    reason=f"Warning {count}: {reason}",
                    moderator_id=actor.id,
                )
                return f"tempmute_{label}"
            except (discord.Forbidden, discord.HTTPException):
                return "punishment_unavailable"
        try:
            await target.ban(reason=f"Auto-ban after warning {count} by {actor} | {reason}")
            return "ban"
        except (discord.Forbidden, discord.HTTPException):
            return "punishment_unavailable"

    async def add_warning(self, guild: discord.Guild, actor: discord.Member, target: discord.Member, *, reason: str | None = None, lang: str = "en") -> AdminActionResult:
        if not self._actor_has(actor, "moderate_members"):
            return AdminActionResult(False, tr(lang, "You do not have permission to warn members.", "No tienes permisos para advertir a miembros."), "author_missing_moderate_members")
        target_error = self._can_act_on_target(guild, actor, target, lang)
        if target_error is not None:
            return target_error
        reason_text = reason or tr(lang, "No reason provided.", "Sin razon proporcionada.")
        await self.db.add_warning(guild_id=guild.id, user_id=target.id, moderator_id=actor.id, reason=reason_text)
        count = len(await self.db.get_warnings(guild.id, target.id))
        await self._sync_warning_roles(guild, target, count, reason=f"Warning role update requested by {actor}")
        punishment = await self._apply_warning_punishment(guild, actor, target, count, reason_text)
        await self._log_action(guild, title="User Warned", moderator=actor, target=target.mention, reason=reason_text, details=f"Warning count: {count} | {punishment}")
        return AdminActionResult(True, tr(lang, f"{target.mention} has been warned.", f"{target.mention} ha sido advertido."), "warning_added")

    async def list_warnings(self, guild: discord.Guild, actor: discord.Member, target: discord.Member, *, lang: str = "en") -> AdminActionResult:
        if not self._actor_has(actor, "moderate_members"):
            return AdminActionResult(False, tr(lang, "You do not have permission to view warnings.", "No tienes permisos para ver advertencias."), "author_missing_moderate_members")
        warnings = await self.db.get_warnings(guild.id, target.id)
        return AdminActionResult(True, str(len(warnings)), "warnings_listed")

    async def remove_warning(self, guild: discord.Guild, actor: discord.Member, target: discord.Member, warning_number: int, *, lang: str = "en") -> AdminActionResult:
        if not self._actor_has(actor, "moderate_members"):
            return AdminActionResult(False, tr(lang, "You do not have permission to remove warnings.", "No tienes permisos para eliminar advertencias."), "author_missing_moderate_members")
        if warning_number not in {1, 2, 3}:
            return AdminActionResult(False, tr(lang, "Warning number must be 1, 2, or 3.", "El numero de advertencia debe ser 1, 2 o 3."), "invalid_warning_number")
        target_error = self._can_act_on_target(guild, actor, target, lang)
        if target_error is not None:
            return target_error
        warnings = list(reversed(await self.db.get_warnings(guild.id, target.id)))
        if warning_number > len(warnings):
            return AdminActionResult(False, tr(lang, f"{target.mention} does not have warning #{warning_number}.", f"{target.mention} no tiene la advertencia #{warning_number}."), "warning_not_found")
        removed = await self.db.delete_warning_by_id(guild.id, int(warnings[warning_number - 1]["id"]))
        if removed is None:
            return AdminActionResult(False, tr(lang, "Warning not found.", "No se encontro la advertencia."), "warning_not_found")
        count = len(await self.db.get_warnings(guild.id, target.id))
        await self._sync_warning_roles(guild, target, count, reason=f"Warning removed by {actor}")
        await self._log_action(guild, title="Warning Removed", moderator=actor, target=target.mention, reason="Warning removed by moderator", details=f"Removed warning number: {warning_number}")
        return AdminActionResult(True, tr(lang, f"Warning #{warning_number} was removed from {target.mention}.", f"Se elimino la advertencia #{warning_number} de {target.mention}."), "warning_removed")

    async def clear_warnings(self, guild: discord.Guild, actor: discord.Member, target: discord.Member, *, reason: str | None = None, lang: str = "en") -> AdminActionResult:
        if not self._actor_has(actor, "moderate_members"):
            return AdminActionResult(False, tr(lang, "You do not have permission to clear warnings.", "No tienes permisos para limpiar advertencias."), "author_missing_moderate_members")
        target_error = self._can_act_on_target(guild, actor, target, lang)
        if target_error is not None:
            return target_error
        reason_text = reason or tr(lang, "No reason provided.", "Sin razon proporcionada.")
        count = await self.db.clear_warnings(guild.id, target.id)
        await self._sync_warning_roles(guild, target, 0, reason=f"Warnings cleared by {actor} | {reason_text}")
        await self._log_action(guild, title="Warnings Cleared", moderator=actor, target=target.mention, reason=reason_text, details=f"Cleared warnings: {count}")
        return AdminActionResult(True, tr(lang, f"Cleared {count} warnings for {target.mention}.", f"Se limpiaron {count} advertencias de {target.mention}."), "warnings_cleared")

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

    async def set_slowmode(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        channel: discord.TextChannel,
        seconds: int,
        *,
        reason: str | None = None,
        lang: str = "en",
    ) -> AdminActionResult:
        if seconds < 0 or seconds > 21600:
            return AdminActionResult(False, tr(lang, "Slowmode must be between 0 and 21600 seconds.", "El modo lento debe estar entre 0 y 21600 segundos."), "invalid_slowmode")
        if not self._actor_has(actor, "manage_channels"):
            return AdminActionResult(False, tr(lang, "You do not have permission to manage channels.", "No tienes permisos para gestionar canales."), "author_missing_manage_channels")
        me = self._bot_member(guild)
        if me is None:
            return AdminActionResult(False, tr(lang, "Bot member not found in this guild.", "No se encontro al bot como miembro en este servidor."), "bot_member_missing")
        if not self._has_channel_perm(me, channel, "manage_channels"):
            return AdminActionResult(False, tr(lang, "I do not have permission to change slowmode.", "No tengo permisos para cambiar el modo lento."), "bot_missing_manage_channels")
        try:
            await channel.edit(slowmode_delay=seconds, reason=f"Slowmode changed by {actor}")
        except discord.Forbidden:
            return AdminActionResult(False, tr(lang, "I do not have permission to change slowmode.", "No tengo permisos para cambiar el modo lento."), "slowmode_forbidden")
        except discord.HTTPException as exc:
            return AdminActionResult(False, tr(lang, f"Failed to change slowmode: {exc}", f"No se pudo cambiar el modo lento: {exc}"), "slowmode_http_error")
        await self._log_action(guild, title="Slowmode Updated", moderator=actor, target=channel.mention, reason=reason or "AI slowmode update", details=f"Slowmode: {seconds}s")
        if seconds == 0:
            return AdminActionResult(True, tr(lang, f"Slowmode disabled in {channel.mention}.", f"Modo lento desactivado en {channel.mention}."), "slowmode_disabled")
        return AdminActionResult(True, tr(lang, f"Slowmode set to `{seconds}s` in {channel.mention}.", f"Modo lento establecido en `{seconds}s` en {channel.mention}."), "slowmode_set")

    async def set_nickname(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        target: discord.Member,
        nickname: str,
        *,
        lang: str = "en",
    ) -> AdminActionResult:
        nick = str(nickname or "").strip()
        if not nick:
            return AdminActionResult(False, tr(lang, "Provide a nickname to set.", "Proporciona un apodo para establecer."), "missing_nickname")
        if len(nick) > 32:
            return AdminActionResult(False, tr(lang, "Nickname must be 32 characters or fewer.", "El apodo debe tener 32 caracteres o menos."), "nickname_too_long")
        if not self._actor_has(actor, "manage_nicknames"):
            return AdminActionResult(False, tr(lang, "You do not have permission to manage nicknames.", "No tienes permisos para gestionar apodos."), "author_missing_manage_nicknames")
        target_error = self._can_act_on_target(guild, actor, target, lang, allow_self=True)
        if target_error is not None:
            return target_error
        me = self._bot_member(guild)
        if me is None or not self._has_perm(me, "manage_nicknames"):
            return AdminActionResult(False, tr(lang, "I do not have permission to change that nickname.", "No tengo permisos para cambiar ese apodo."), "bot_missing_manage_nicknames")
        try:
            await target.edit(nick=nick, reason=f"Nickname changed by {actor}")
        except discord.Forbidden:
            return AdminActionResult(False, tr(lang, "I do not have permission to change that nickname.", "No tengo permisos para cambiar ese apodo."), "nickname_forbidden")
        except discord.HTTPException as exc:
            return AdminActionResult(False, tr(lang, f"Failed to change nickname: {exc}", f"No se pudo cambiar el apodo: {exc}"), "nickname_http_error")
        await self._log_action(guild, title="Nickname Updated", moderator=actor, target=target.mention, reason=f"New nickname: {nick}")
        return AdminActionResult(True, tr(lang, f"Nickname updated for {target.mention}: `{nick}`.", f"Apodo actualizado para {target.mention}: `{nick}`."), "nickname_set")

    async def user_info(self, guild: discord.Guild, actor: discord.Member, target: discord.Member, *, lang: str = "en") -> AdminActionResult:
        if not self._actor_has(actor, "moderate_members"):
            return AdminActionResult(False, tr(lang, "You do not have permission to view user information.", "No tienes permisos para ver informacion de usuarios."), "author_missing_moderate_members")
        data = {
            "user_id": target.id,
            "display_name": target.display_name,
            "booster": bool(target.premium_since),
            "roles": [
                {"id": role.id, "name": role.name}
                for role in sorted(target.roles, key=lambda role: role.position, reverse=True)
                if not role.is_default()
            ],
            "permissions": [name.replace("_", " ").title() for name, enabled in target.guild_permissions if enabled],
            "joined_at": int(target.joined_at.timestamp()) if target.joined_at is not None else None,
            "created_at": int(target.created_at.timestamp()),
        }
        await self._log_action(guild, title="User Info Lookup", moderator=actor, target=f"{target.mention} ({target.id})", reason="")
        return AdminActionResult(True, tr(lang, "User information", "Informacion del usuario"), "user_info", data=data)

    @staticmethod
    async def send_as_bot(channel: discord.abc.Messageable, text: str) -> bool:
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def say(self, guild: discord.Guild, actor: discord.Member, channel: discord.TextChannel, text: str, *, lang: str = "en") -> AdminActionResult:
        text = str(text or "").strip()
        if not text:
            return AdminActionResult(False, tr(lang, "Message cannot be empty.", "El mensaje no puede estar vacio."), "missing_say_text")
        if len(text) > 2000:
            return AdminActionResult(False, tr(lang, "Message must be 2000 characters or fewer.", "El mensaje debe tener 2000 caracteres o menos."), "say_text_too_long")
        if not self._actor_has(actor, "manage_messages"):
            return AdminActionResult(False, tr(lang, "You do not have permission to make me say messages.", "No tienes permisos para hacerme decir mensajes."), "author_missing_manage_messages")
        me = self._bot_member(guild)
        if me is None or not self._has_channel_perm(me, channel, "send_messages"):
            return AdminActionResult(False, tr(lang, "I need Send Messages permission here.", "Necesito Enviar mensajes aqui."), "bot_missing_send_messages")
        if not await self.send_as_bot(channel, text):
            return AdminActionResult(False, tr(lang, "I could not send that message in this channel.", "No pude enviar ese mensaje en este canal."), "say_send_failed")
        return AdminActionResult(True, tr(lang, "Message sent.", "Mensaje enviado."), "say_sent")

    async def clear_channel(self, guild: discord.Guild, actor: discord.Member, channel: discord.TextChannel, *, reason: str | None = None, lang: str = "en") -> AdminActionResult:
        if not self._actor_has(actor, "manage_channels"):
            return AdminActionResult(False, tr(lang, "You do not have permission to manage channels.", "No tienes permisos para gestionar canales."), "author_missing_manage_channels")
        me = self._bot_member(guild)
        if me is None:
            return AdminActionResult(False, tr(lang, "Bot member not found in this guild.", "No se encontro al bot como miembro en este servidor."), "bot_member_missing")
        if not self._has_channel_perm(me, channel, "manage_messages"):
            return AdminActionResult(False, tr(lang, "I do not have permission to clear that channel.", "No tengo permisos para limpiar ese canal."), "bot_missing_manage_messages")
        deleted_count = 0
        recent_cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        recent_batch: list[discord.Message] = []

        async def flush_recent_batch() -> None:
            nonlocal deleted_count
            if not recent_batch:
                return
            if len(recent_batch) == 1:
                await recent_batch[0].delete()
            else:
                await channel.delete_messages(recent_batch)
            deleted_count += len(recent_batch)
            recent_batch.clear()

        try:
            async for message in channel.history(limit=None, oldest_first=False):
                if message.created_at >= recent_cutoff:
                    recent_batch.append(message)
                    if len(recent_batch) >= 100:
                        await flush_recent_batch()
                    continue
                await flush_recent_batch()
                await message.delete()
                deleted_count += 1
            await flush_recent_batch()
        except discord.Forbidden:
            return AdminActionResult(False, tr(lang, "I do not have permission to clear that channel.", "No tengo permisos para limpiar ese canal."), "clear_channel_forbidden", data={"deleted_count": deleted_count})
        except discord.HTTPException as exc:
            return AdminActionResult(False, tr(lang, f"Failed to clear channel: {exc}", f"No se pudo limpiar el canal: {exc}"), "clear_channel_http_error", data={"deleted_count": deleted_count})
        await self._log_action(
            guild,
            title="Channel Cleared",
            moderator=actor,
            target=f"#{channel.name} ({channel.id})",
            reason=reason or "AI channel clear",
            details=f"Deleted messages: {deleted_count}",
        )
        return AdminActionResult(True, tr(lang, f"Cleared messages in {channel.mention}.", f"Mensajes limpiados en {channel.mention}."), "channel_cleared", data={"deleted_count": deleted_count})

    async def clone_channel(self, guild: discord.Guild, actor: discord.Member, channel: discord.TextChannel, *, reason: str | None = None, lang: str = "en") -> AdminActionResult:
        if not self._actor_has(actor, "manage_channels"):
            return AdminActionResult(False, tr(lang, "You do not have permission to manage channels.", "No tienes permisos para gestionar canales."), "author_missing_manage_channels")
        me = self._bot_member(guild)
        if me is None:
            return AdminActionResult(False, tr(lang, "Bot member not found in this guild.", "No se encontro al bot como miembro en este servidor."), "bot_member_missing")
        if not self._has_channel_perm(me, channel, "manage_channels"):
            return AdminActionResult(False, tr(lang, "I do not have permission to clone that channel.", "No tengo permisos para clonar ese canal."), "bot_missing_manage_channels")
        try:
            cloned = await channel.clone(reason=f"Channel cloned by {actor}")
        except discord.Forbidden:
            return AdminActionResult(False, tr(lang, "I do not have permission to clone that channel.", "No tengo permisos para clonar ese canal."), "clone_channel_forbidden")
        except discord.HTTPException as exc:
            return AdminActionResult(False, tr(lang, f"Failed to clone channel: {exc}", f"No se pudo clonar el canal: {exc}"), "clone_channel_http_error")
        # The clone exists from here on: a failed repositioning is reported as such, never as "nothing happened", and keeps the handle.
        handle = {"resource_type": "channel", "guild_id": getattr(guild, "id", None), "resource_id": getattr(cloned, "id", None)}
        positioned = True
        try:
            await cloned.edit(category=channel.category, position=min(channel.position + 1, max(0, len(guild.channels) - 1)))
        except (discord.Forbidden, discord.HTTPException):
            positioned = False
        await self._log_action(
            guild,
            title="Channel Cloned",
            moderator=actor,
            target=f"#{channel.name} ({channel.id})",
            reason=reason or "AI channel clone",
            details=f"Clone created: #{cloned.name} ({cloned.id})",
            event_key=("channel_create", cloned.id),
        )
        if not positioned:
            return AdminActionResult(
                False,
                tr(lang, f"Cloned {channel.mention} into {cloned.mention}, but I could not move the clone into position.", f"Canal clonado {channel.mention} en {cloned.mention}, pero no pude mover el clon a su posicion."),
                "clone_position_failed",
                resource_handle=handle,
            )
        return AdminActionResult(True, tr(lang, f"Cloned channel {channel.mention} into {cloned.mention}.", f"Canal clonado {channel.mention} en {cloned.mention}."), "channel_cloned", resource_handle=handle)

    async def purge_user_messages(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        channel: discord.TextChannel,
        target: discord.Member,
        amount: int,
        *,
        exclude_message_id: int | None = None,
        reason: str | None = None,
        lang: str = "en",
    ) -> AdminActionResult:
        if amount < 1 or amount > 500:
            return AdminActionResult(False, tr(lang, "Amount must be between 1 and 500.", "La cantidad debe estar entre 1 y 500."), "invalid_message_count")
        if not self._actor_has(actor, "manage_messages"):
            return AdminActionResult(False, tr(lang, "You do not have permission to delete messages.", "No tienes permisos para eliminar mensajes."), "author_missing_manage_messages")
        me = self._bot_member(guild)
        if me is None:
            return AdminActionResult(False, tr(lang, "Bot member not found in this guild.", "No se encontro al bot como miembro en este servidor."), "bot_member_missing")
        if not self._has_channel_perm(me, channel, "manage_messages"):
            return AdminActionResult(False, tr(lang, "No messages could be deleted due to permissions or age limits.", "No se pudo eliminar ningún mensaje por permisos o límites de antigüedad."), "bot_missing_manage_messages")
        history = getattr(channel, "history", None)
        if history is None:
            return AdminActionResult(False, tr(lang, "This action only works in text channels.", "Esta accion solo funciona en canales de texto."), "channel_unsupported")
        candidates: list[discord.Message] = []
        try:
            async for msg in history(limit=2000):
                if exclude_message_id is not None and msg.id == exclude_message_id:
                    continue
                if msg.author.id == target.id:
                    candidates.append(msg)
                    if len(candidates) >= amount:
                        break
        except discord.HTTPException as exc:
            return AdminActionResult(False, tr(lang, f"Failed to read message history: {exc}", f"No se pudo leer el historial de mensajes: {exc}"), "purge_history_http_error")
        if not candidates:
            return AdminActionResult(False, tr(lang, f"No recent messages found for {target.mention}.", f"No se encontraron mensajes recientes de {target.mention}."), "purge_no_messages")
        deleted_count = 0
        for msg in candidates:
            try:
                await msg.delete()
                deleted_count += 1
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue
        data = {"requested": amount, "found": len(candidates), "deleted": deleted_count}
        if deleted_count == 0:
            return AdminActionResult(False, tr(lang, "No messages could be deleted due to permissions or age limits.", "No se pudo eliminar ningún mensaje por permisos o límites de antigüedad."), "purge_nothing_deleted", data=data)
        await self._log_action(
            guild,
            title="User Messages Purged",
            moderator=actor,
            target=f"{target.mention} in #{channel.name}",
            reason=reason or "AI user message purge",
            details=f"Requested: {amount} | Deleted: {deleted_count}",
        )
        return AdminActionResult(True, tr(lang, "Messages deleted.", "Mensajes eliminados."), "user_messages_purged", data=data)

    # Server configuration shared by the explicit AdminCog commands and the typed ADMIN operations.  Database only: no bot permission is
    # needed and, exactly like the commands, no modlog entry is written.  The actor needs Manage Server (mirrors the commands' decorator).
    def _config_denied(self, actor: discord.Member, lang: str) -> AdminActionResult | None:
        if self._actor_has(actor, "manage_guild"):
            return None
        return AdminActionResult(False, tr(lang, "You do not have permission to manage the server.", "No tienes permisos para gestionar el servidor."), "author_missing_manage_guild")

    async def set_modlog_channel(self, guild: discord.Guild, actor: discord.Member, channel: discord.TextChannel | None, *, lang: str = "en") -> AdminActionResult:
        denied = self._config_denied(actor, lang)
        if denied is not None:
            return denied
        await self.db.get_or_create_guild_settings(guild.id)
        await self.db.set_modlog_channel(guild.id, channel.id if channel else None)
        if channel is None:
            return AdminActionResult(True, "Mod-log channel cleared.", "modlog_cleared")
        return AdminActionResult(True, f"Mod-log channel set to {channel.mention}.", "modlog_set", data={"channel_id": channel.id})

    async def set_prefix(self, guild: discord.Guild, actor: discord.Member, prefix: str, *, lang: str = "en") -> AdminActionResult:
        denied = self._config_denied(actor, lang)
        if denied is not None:
            return denied
        prefix = str(prefix or "").strip()
        if not prefix:
            return AdminActionResult(False, "Prefix cannot be empty.", "prefix_empty")
        if len(prefix) > 5:
            return AdminActionResult(False, "Prefix is too long. Use up to 5 characters.", "prefix_too_long")
        await self.db.get_or_create_guild_settings(guild.id)
        await self.db.set_prefix(guild.id, prefix)
        return AdminActionResult(True, f"Prefix updated to `{prefix}`.", "prefix_set", data={"prefix": prefix})

    async def set_language(self, guild: discord.Guild, actor: discord.Member, code: str, *, lang: str = "en") -> AdminActionResult:
        denied = self._config_denied(actor, lang)
        if denied is not None:
            return denied
        requested = str(code or "").strip().lower()
        if requested not in {"en", "es"}:
            return AdminActionResult(False, "Invalid language code. Use `en` or `es`.", "invalid_language")
        await self.db.get_or_create_guild_settings(guild.id)
        await self.db.set_language(guild.id, requested)
        return AdminActionResult(
            True,
            tr(requested, f"Bot language updated to `{requested}`.", f"Idioma del bot actualizado a `{requested}`."),
            "language_set",
            data={"language": requested},
        )

    async def set_filter(self, guild: discord.Guild, actor: discord.Member, kind: str, enabled: bool, *, lang: str = "en") -> AdminActionResult:
        """``kind`` is ``spam`` or ``link``."""

        denied = self._config_denied(actor, lang)
        if denied is not None:
            return denied
        setter, label = (self.db.set_anti_spam, "Anti-spam") if kind == "spam" else (self.db.set_anti_link, "Anti-link")
        await setter(guild.id, enabled)
        return AdminActionResult(True, f"{label} is now {'enabled' if enabled else 'disabled'}.", f"anti{kind}_set", data={"enabled": enabled})

    async def add_ai_channel(self, guild: discord.Guild, actor: discord.Member, channel: discord.TextChannel, *, lang: str = "en") -> AdminActionResult:
        denied = self._config_denied(actor, lang)
        if denied is not None:
            return denied
        await self.db.add_ai_channel(guild.id, channel.id)
        scope, channel_ids = await self.db.get_ai_channel_scope(guild.id)
        return AdminActionResult(True, f"AI channel added: {channel.mention}.", "ai_channel_added", data={"ai_scope": scope, "channel_ids": channel_ids})

    async def remove_ai_channel(self, guild: discord.Guild, actor: discord.Member, channel: discord.TextChannel, *, lang: str = "en") -> AdminActionResult:
        denied = self._config_denied(actor, lang)
        if denied is not None:
            return denied
        scope, allowed = await self.db.get_ai_channel_scope(guild.id)
        data = {"ai_scope": scope, "channel_ids": allowed}
        if scope == "all":
            return AdminActionResult(False, "AI is currently allowed in all channels. Use `/aichannel add` first to enable channel restrictions.", "ai_channel_unrestricted", data=data)
        if channel.id not in allowed:
            return AdminActionResult(False, f"{channel.mention} is not currently in the AI-allowed list.", "ai_channel_not_listed", data=data)
        await self.db.remove_ai_channel(guild.id, channel.id)
        scope_after, allowed_after = await self.db.get_ai_channel_scope(guild.id)
        data = {"ai_scope": scope_after, "channel_ids": allowed_after}
        if scope_after == "none":
            return AdminActionResult(
                True,
                f"AI channel removed: {channel.mention}. AI is now disabled in all channels until you add at least one channel with `/aichannel add`.",
                "ai_channel_removed",
                data=data,
            )
        return AdminActionResult(True, f"AI channel removed: {channel.mention}. Remaining AI-allowed channels: {len(allowed_after)}.", "ai_channel_removed", data=data)

    async def list_ai_channels(self, guild: discord.Guild, actor: discord.Member, *, lang: str = "en") -> AdminActionResult:
        denied = self._config_denied(actor, lang)
        if denied is not None:
            return denied
        scope, channel_ids = await self.db.get_ai_channel_scope(guild.id)
        return AdminActionResult(True, "AI Allowed Channels", "ai_channels_listed", data={"ai_scope": scope, "channel_ids": channel_ids})

    async def clear_ai_channels(self, guild: discord.Guild, actor: discord.Member, *, lang: str = "en") -> AdminActionResult:
        denied = self._config_denied(actor, lang)
        if denied is not None:
            return denied
        await self.db.clear_ai_channels(guild.id)
        return AdminActionResult(True, "AI channel restrictions cleared. AI is now allowed in all channels.", "ai_channels_cleared", data={"ai_scope": "all", "channel_ids": []})

    # Server context, shared by the AdminCog commands and the typed ADMIN operations.  Manage Server like the commands' decorator; the reset
    # scope is validated here so a caller can never clear more than it named.
    async def server_context_view(self, guild: discord.Guild, actor: discord.Member, *, lang: str = "en") -> AdminActionResult:
        denied = self._config_denied(actor, lang)
        if denied is not None:
            return denied
        settings = await self.db.get_guild_settings(guild.id)
        entries = await self.db.get_server_context_entries(guild.id)
        counts = await ServerMemoryService(self.db).counts(guild.id)
        data = {
            "context": str(settings.server_context or "").strip()[:1500],
            "sources": [
                {"channel_id": int(entry.get("channel_id", 0)), "channel_name": str(entry.get("channel_name", "")).strip()[:100], "updated_at": str(entry.get("updated_at", "")).strip()[:40]}
                for entry in entries[:10]
            ],
            "source_count": len(entries),
            "memory_counts": [
                {"memory_type": str(row.get("memory_type", "")), "status": str(row.get("status", "")), "count": int(row.get("count", 0))}
                for row in counts[:40]
            ],
        }
        return AdminActionResult(True, "AI server context", "server_context_viewed", data=data)

    async def reset_server_context(self, guild: discord.Guild, actor: discord.Member, scope: str, *, lang: str = "en") -> AdminActionResult:
        denied = self._config_denied(actor, lang)
        if denied is not None:
            return denied
        scope_key = str(scope or "").strip().casefold()
        if scope_key not in {"summaries", "memory", "ai_history", "all"}:
            return AdminActionResult(
                False,
                tr(lang, "Invalid scope. Use `summaries`, `memory`, `ai_history`, or `all`.", "Scope invalido. Usa `summaries`, `memory`, `ai_history` o `all`."),
                "invalid_context_scope",
            )
        if scope_key == "all":
            await self.db.reset_ai_server_context(guild.id)
            await ServerMemoryService(self.db).clear_memories(guild.id)
        elif scope_key == "summaries":
            await self.db.reset_server_context_summaries(guild.id)
        elif scope_key == "memory":
            await ServerMemoryService(self.db).clear_memories(guild.id)
        else:
            await self.db.reset_ai_conversation_turns(guild.id)
        if scope_key in {"all", "ai_history"}:
            ai_cog = self.bot.get_cog("AIChatCog")
            if ai_cog is not None and hasattr(ai_cog, "clear_guild_history"):
                ai_cog.clear_guild_history(guild.id)
        return AdminActionResult(
            True,
            tr(lang, f"AI server context reset complete for scope `{scope_key}`.", f"Reinicio de contexto IA completado para scope `{scope_key}`."),
            "server_context_reset",
            data={"context_scope": scope_key},
        )

    def server_context_learn_denied(self, actor: discord.Member, channel: discord.TextChannel, *, lang: str = "en") -> AdminActionResult | None:
        """Besides Manage Server, the author must be able to read the channel the bot is about to summarize."""

        denied = self._config_denied(actor, lang)
        if denied is not None:
            return denied
        if not all(self._has_channel_perm(actor, channel, name) for name in ("view_channel", "read_message_history")):
            return AdminActionResult(False, tr(lang, "You cannot read that channel.", "No puedes leer ese canal."), "author_missing_read_access")
        return None

    # Color roles, shared by the /color commands and the typed ADMIN operations (the work itself lives in ModerationCog, next to the panel code).
    # Administrator mirrors the commands' decorator (bot owner included).
    def color_denied(self, actor: discord.Member, lang: str) -> AdminActionResult | None:
        if self._actor_has(actor, "administrator"):
            return None
        return AdminActionResult(False, tr(lang, "You need Administrator permission to manage color roles.", "Necesitas el permiso de Administrador para gestionar los roles de color."), "author_missing_administrator")

    def color_panel_bot_denied(self, guild: discord.Guild, channel: discord.TextChannel | None, lang: str) -> AdminActionResult | None:
        """``channel`` None checks guild-wide."""

        me = self._bot_member(guild)
        if me is None:
            return AdminActionResult(False, tr(lang, "Bot member not found in this guild.", "No se encontro al bot como miembro en este servidor."), "bot_member_missing")
        if not all(self._has_channel_perm(me, channel, name) for name in ("send_messages", "add_reactions", "embed_links", "attach_files", "read_message_history")):
            return AdminActionResult(False, tr(lang, "I need permission to send messages, add reactions, embed links and attach files there.", "Necesito permiso para enviar mensajes, agregar reacciones, insertar enlaces y adjuntar archivos ahi."), "bot_missing_channel_permissions")
        return None
