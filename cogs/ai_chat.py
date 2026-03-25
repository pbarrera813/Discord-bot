from __future__ import annotations

from collections import defaultdict, deque
import difflib
import logging
import re
import time
import unicodedata
from typing import Final

import discord
from discord.ext import commands

from utils.discord_helpers import parse_user_id_from_text
from utils.i18n import tr


SUPPORTED_TRANSLATE_LANGUAGES: Final[tuple[str, ...]] = (
    "english",
    "spanish",
    "german",
    "japanese",
    "russian",
    "french",
    "italian",
    "portuguese",
)

TRANSLATE_LANGUAGE_ALIASES: Final[dict[str, str]] = {
    "english": "english",
    "en": "english",
    "ingles": "english",
    "spanish": "spanish",
    "es": "spanish",
    "espanol": "spanish",
    "german": "german",
    "de": "german",
    "aleman": "german",
    "japanese": "japanese",
    "ja": "japanese",
    "japones": "japanese",
    "russian": "russian",
    "ru": "russian",
    "ruso": "russian",
    "french": "french",
    "fr": "french",
    "frances": "french",
    "italian": "italian",
    "it": "italian",
    "italiano": "italian",
    "portuguese": "portuguese",
    "pt": "portuguese",
    "portugues": "portuguese",
}

COMMAND_HINTS: Final[dict[str, str]] = {
    "help": "help",
    "setup": "setup",
    "setservercontext": "setservercontext",
    "setprefix": "setprefix",
    "prefix": "setprefix",
    "language": "language",
    "code": "code",
    "codelangs": "codelangs",
    "runlangs": "codelangs",
    "srvstatus": "srvstatus",
    "delete": "message delete",
    "mute": "user mute",
    "unmute": "user unmute",
    "kick": "user kick",
    "ban": "user ban",
    "unban": "user unban",
    "tempmute": "user tempmute",
    "tempban": "user tempban",
    "warn": "user warn",
    "unwarn": "user unwarn",
    "warnings": "user warnings",
    "clearwarnings": "user clearwarnings",
    "userinfo": "user info",
    "setnick": "user setnick",
    "channeladd": "channel add",
    "channeldel": "channel delete",
    "clonechannel": "channel clone",
    "clearchannel": "channel clear",
    "channel": "channel add",
    "lock": "channel lock",
    "unlock": "channel unlock",
    "slowmode": "channel slowmode",
    "purgeuser": "message purgeuser",
    "roleadd": "role add",
    "roleremove": "role remove",
    "createrole": "role create",
    "colorsetup": "color setup",
    "colors": "color list",
    "colorchannel": "color channel",
    "colorreload": "color reload",
    "coloradd": "color add",
    "colorremove": "color remove",
    "translate": "translate",
    "roast": "roast",
    "roastme": "roastme",
    "joke": "joke",
    "dadjoke": "dadjoke",
    "advice": "advice",
    "whois": "whois",
    "convert": "convert",
    "remindme": "remindme",
    "unremindme": "unremindme",
    "meme": "meme create",
    "memerandom": "meme random",
    "memecustom": "meme custom",
    "memetemplates": "meme templates",
    "memefonts": "meme fonts",
    "memehelp": "meme help",
    "speech": "speech",
    "football": "football live ligamx",
    "footballlive": "football live ligamx",
    "footballtoday": "football today ligamx",
    "footballnext": "football next ligamx",
    "footballlast": "football last ligamx",
    "footballtable": "football table ligamx",
    "footballteam": "football team ligamx",
    "footballscorers": "football scorers ligamx",
    "ligamx": "football live ligamx",
    "premier": "football live premier",
    "laliga": "football live laliga",
    "concacaf": "football live concacaf",
    "ligamxlive": "football live ligamx",
    "ligamxtoday": "football today ligamx",
    "ligamxnext": "football next ligamx",
    "ligamxlast": "football last ligamx",
    "ligamxtable": "football table ligamx",
    "ligamxteam": "football team ligamx",
    "ligamxscorers": "football scorers ligamx",
}


class AIChatCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._cooldowns: dict[tuple[int, int], float] = {}
        self._cooldown_seconds = 4.0
        self._conversation_history: dict[tuple[int, int], deque[dict[str, str]]] = defaultdict(
            lambda: deque(maxlen=120)
        )
        self._history_entry_max_chars = 900
        self._chat_response_id_limit = 600
        self._chat_response_ids: deque[int] = deque(maxlen=self._chat_response_id_limit)
        self._chat_response_id_set: set[int] = set()

    async def _is_ai_allowed_in_channel(
        self,
        guild: discord.Guild,
        channel: discord.abc.GuildChannel | discord.Thread,
    ) -> bool:
        if await self.bot.db.is_ai_channel_allowed(guild.id, channel.id):
            return True
        if isinstance(channel, discord.Thread) and channel.parent_id is not None:
            return await self.bot.db.is_ai_channel_allowed(guild.id, channel.parent_id)
        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        settings = await self.bot.db.get_or_create_guild_settings(message.guild.id)
        prefix = settings.prefix
        lang = settings.language_code

        if await self._handle_mention_translate(message, lang):
            return

        channel_allowed = await self._is_ai_allowed_in_channel(
            message.guild,
            message.channel,
        )
        if not channel_allowed:
            return

        if message.content.startswith(prefix):
            return

        replied_message = await self._get_replied_message(message)

        if not await self._is_chat_trigger(message, replied_message):
            return
        if not self._allowed_by_cooldown(message):
            return

        user_prompt = self._extract_chat_prompt(message)
        if not user_prompt:
            return
        if self.bot.user in message.mentions:
            hinted_command = self._detect_command_hint(user_prompt)
            if hinted_command:
                await message.reply(
                    tr(
                        lang,
                        f"Hey, there is a command for this! Use: `/{hinted_command}` or `{prefix}{hinted_command}`",
                        f"Oye, hay un comando para esto. Usa: `/{hinted_command}` o `{prefix}{hinted_command}`",
                    ),
                    mention_author=True,
                )
                return

        convo_key = self._conversation_key(message.guild.id, message.channel.id)
        await self._ensure_history_loaded(convo_key, message.guild.id, message.channel.id)
        if (
            replied_message is not None
            and self.bot.user is not None
            and replied_message.author.id == self.bot.user.id
            and replied_message.content.strip()
        ):
            prefixed = self._append_conversation_turn(
                convo_key,
                role="assistant",
                speaker=self.bot.user.display_name,
                content=replied_message.content.strip(),
            )
            if prefixed is not None:
                await self._persist_conversation_turn(
                    guild_id=message.guild.id,
                    channel_id=message.channel.id,
                    role="assistant",
                    speaker=self.bot.user.display_name,
                    content=prefixed,
                )

        relay_context = await self._resolve_relay_target_from_prompt(
            guild=message.guild,
            author=message.author,
            prompt=user_prompt,
            lang=lang,
        )

        await message.channel.typing()
        try:
            reply = await self.bot.llm_client.chat(
                server_context=settings.server_context,
                user_message=user_prompt,
                author_name=message.author.display_name,
                channel_name=getattr(message.channel, "name", "unknown"),
                channel_reference=self._channel_reference(message.channel),
                available_channels=self._serialize_text_channels(message.guild),
                available_emojis=self._serialize_custom_emojis(message.guild),
                conversation_history=self._build_conversation_history(convo_key),
                mention_hints=self._build_mention_hints(relay_context),
                relay_instruction=self._build_relay_instruction(relay_context, lang),
            )
        except Exception as exc:
            logging.exception("AI chat failure in guild=%s channel=%s", message.guild.id, message.channel.id)
            if self._is_ai_limit_error(exc):
                await message.reply(
                    tr(
                        lang,
                        "Wow, I think that's a lot of talking from me right now, try later!",
                        "Wow, creo que he hablado demasiado por ahora, intenta más tarde!",
                    ),
                    mention_author=True,
                )
            elif self._is_empty_completion_error(exc):
                await message.reply(
                    tr(
                        lang,
                        "Oops, my brain lagged for a second. Try again!",
                        "Ups, se me fue la onda por un segundo. Intenta otra vez!",
                    ),
                    mention_author=True,
                )
            else:
                await message.reply(
                    tr(
                        lang,
                        "I hit an internal AI error. Please try again in a bit.",
                        "Tuve un error interno de IA. Intenta de nuevo en un momento.",
                    ),
                    mention_author=True,
                )
            return

        reply = await self._normalize_discord_references(reply, message.guild)
        reply = self._apply_relay_postprocess(reply, relay_context)
        reply = self._strip_bot_speaker_prefix(reply)
        prefixed_user = self._append_conversation_turn(
            convo_key,
            role="user",
            speaker=message.author.display_name,
            content=user_prompt,
        )
        if prefixed_user is not None:
            await self._persist_conversation_turn(
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                role="user",
                speaker=message.author.display_name,
                content=prefixed_user,
            )
        bot_speaker = self.bot.user.display_name if self.bot.user else "Nitori"
        prefixed_bot = self._append_conversation_turn(
            convo_key,
            role="assistant",
            speaker=bot_speaker,
            content=reply,
        )
        if prefixed_bot is not None:
            await self._persist_conversation_turn(
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                role="assistant",
                speaker=bot_speaker,
                content=prefixed_bot,
            )
        await self._send_long_reply(message, reply, mention_author=True)

    @commands.hybrid_command(
        name="roast",
        description="Generate a playful roast using the server vibe context.",
    )
    async def roast(
        self,
        ctx: commands.Context,
        target: discord.Member | None = None,
    ) -> None:
        guild = ctx.guild
        lang = await self._lang(guild)
        if target is None:
            await ctx.send(
                tr(
                    lang,
                    "Provide a target user for `/roast`, or use `/roastme`.",
                    "Proporciona un usuario objetivo para `/roast`, o usa `/roastme`.",
                )
            )
            return

        if guild is None:
            await ctx.send(
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                )
            )
            return

        await self._run_roast(
            ctx,
            target_member=target,
        )

    @commands.hybrid_command(
        name="roastme",
        description="Roast yourself using the server vibe context.",
    )
    async def roastme(self, ctx: commands.Context) -> None:
        await self._run_roast(
            ctx,
            target_member=ctx.author,  # type: ignore[arg-type]
        )

    async def _run_roast(
        self,
        ctx: commands.Context,
        *,
        target_member: discord.Member,
    ) -> None:
        guild = ctx.guild
        lang = await self._lang(guild)
        if guild is None:
            await ctx.send(
                tr(
                    lang,
                    "This command can only be used in a server.",
                    "Este comando solo se puede usar en un servidor.",
                )
            )
            return

        channel = ctx.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await ctx.send(
                tr(
                    lang,
                    "This command can only be used in text channels or threads.",
                    "Este comando solo se puede usar en canales de texto o hilos.",
                )
            )
            return

        channel_allowed = await self._is_ai_allowed_in_channel(guild, channel)
        if not channel_allowed:
            await ctx.send(
                tr(
                    lang,
                    "AI commands are not enabled in this channel.",
                    "Los comandos de IA no estan habilitados en este canal.",
                )
            )
            return

        settings = await self.bot.db.get_or_create_guild_settings(guild.id)
        target = target_member.display_name
        mention = target_member.mention
        target_profile = await self._build_roast_target_profile(
            guild=guild,
            channel=channel,
            target=target_member,
            lang=lang,
        )
        prompt = tr(
            lang,
            (
                f"Roast target (and only this target): {target} [{mention}]. "
                "Write a playful roast in 5-7 sentences with strong humor and server-style references when possible. "
                "Do not roast the command author unless they are the target. "
                "Output only the roast content; no headers, no speaker labels, no 'Nitori:' prefixes. "
                f"Start by mentioning the target exactly once as {mention}. "
                "Use the profile details below to personalize the roast with concrete references.\n"
                f"{target_profile}"
            ),
            (
                f"Objetivo del roast (y solo ese objetivo): {target} [{mention}]. "
                "Escribe un roast jugueton en 5-7 frases con humor fuerte y referencias al estilo del servidor cuando se pueda. "
                "No roastees al autor del comando a menos que sea el objetivo. "
                "Devuelve solo el roast, sin encabezados ni etiquetas de hablante ni prefijos tipo 'Nitori:'. "
                f"Empieza mencionando al objetivo exactamente una vez como {mention}. "
                "Usa los detalles del perfil para personalizar el roast con referencias concretas.\n"
                f"{target_profile}"
            ),
        )

        convo_key = self._conversation_key(guild.id, channel.id)
        await self._ensure_history_loaded(convo_key, guild.id, channel.id)

        if ctx.interaction and not ctx.interaction.response.is_done():
            try:
                await ctx.defer()
            except (discord.NotFound, discord.HTTPException):
                pass

        try:
            reply = await self.bot.llm_client.chat(
                server_context=settings.server_context,
                user_message=prompt,
                author_name=ctx.author.display_name,
                channel_name=getattr(channel, "name", "unknown"),
                channel_reference=self._channel_reference(channel),
                available_channels=self._serialize_text_channels(guild),
                available_emojis=self._serialize_custom_emojis(guild),
                conversation_history=self._build_conversation_history(convo_key),
            )
        except Exception as exc:
            if guild is not None:
                logging.exception("AI roast failure in guild=%s channel=%s", guild.id, channel.id)
            if self._is_ai_limit_error(exc):
                await ctx.send(
                    tr(
                        lang,
                        "Wow, I think that's a lot of talking from me right now, try later!",
                        "Wow, creo que he hablado demasiado por ahora, intenta mas tarde!",
                    )
                )
            elif self._is_empty_completion_error(exc):
                await ctx.send(
                    tr(
                        lang,
                        "Oops, my brain lagged for a second. Try again!",
                        "Ups, se me fue la onda por un segundo. Intenta otra vez!",
                    )
                )
            else:
                await ctx.send(
                    tr(
                        lang,
                        "I hit an internal AI error. Please try again in a bit.",
                        "Tuve un error interno de IA. Intenta de nuevo en un momento.",
                    )
                )
            return

        reply = await self._normalize_discord_references(reply, guild)
        reply = self._format_roast_reply(reply, mention)
        prefixed_user = self._append_conversation_turn(
            convo_key,
            role="user",
            speaker=ctx.author.display_name,
            content=prompt,
        )
        if prefixed_user is not None:
            await self._persist_conversation_turn(
                guild_id=guild.id,
                channel_id=channel.id,
                role="user",
                speaker=ctx.author.display_name,
                content=prefixed_user,
            )
        bot_speaker = self.bot.user.display_name if self.bot.user else "Nitori"
        prefixed_bot = self._append_conversation_turn(
            convo_key,
            role="assistant",
            speaker=bot_speaker,
            content=reply,
        )
        if prefixed_bot is not None:
            await self._persist_conversation_turn(
                guild_id=guild.id,
                channel_id=channel.id,
                role="assistant",
                speaker=bot_speaker,
                content=prefixed_bot,
            )
        await ctx.send(
            reply[:1900],
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )

    async def _build_roast_target_profile(
        self,
        *,
        guild: discord.Guild,
        channel: discord.TextChannel | discord.Thread,
        target: discord.Member,
        lang: str,
    ) -> str:
        warnings = await self.bot.db.get_warnings(guild.id, target.id)
        warning_reasons: list[str] = []
        for item in warnings[:3]:
            reason = str(item.get("reason", "")).strip()
            if reason:
                warning_reasons.append(reason[:120])

        roles = [role.name for role in target.roles if not role.is_default()]
        role_preview = ", ".join(roles[-8:]) if roles else tr(lang, "none", "ninguno")

        recent_count, recent_lines = await self._collect_recent_target_messages(
            channel=channel,
            member_id=target.id,
            sample_limit=6,
            scan_limit=280,
        )
        recent_preview = (
            " | ".join(recent_lines)
            if recent_lines
            else tr(lang, "No recent text found in this channel.", "No se encontro texto reciente en este canal.")
        )

        warning_preview = (
            " | ".join(warning_reasons)
            if warning_reasons
            else tr(lang, "No warning reasons recorded.", "No hay razones de advertencia registradas.")
        )

        joined_ts = (
            f"<t:{int(target.joined_at.timestamp())}:R>"
            if target.joined_at is not None
            else tr(lang, "Unknown", "Desconocido")
        )
        created_ts = f"<t:{int(target.created_at.timestamp())}:R>"
        boost_text = tr(
            lang,
            "yes" if target.premium_since else "no",
            "si" if target.premium_since else "no",
        )

        return tr(
            lang,
            (
                f"Target mention: {target.mention}\n"
                f"Target ID: {target.id}\n"
                f"Display name: {target.display_name}\n"
                f"Username: {target.name}\n"
                f"Joined server: {joined_ts}\n"
                f"Joined Discord: {created_ts}\n"
                f"Server booster: {boost_text}\n"
                f"Roles: {role_preview}\n"
                f"Warnings: {len(warnings)}\n"
                f"Warning reasons sample: {warning_preview}\n"
                f"Recent text messages in this channel (count {recent_count}): {recent_preview}"
            ),
            (
                f"Mencion del objetivo: {target.mention}\n"
                f"ID del objetivo: {target.id}\n"
                f"Nombre visible: {target.display_name}\n"
                f"Usuario: {target.name}\n"
                f"Ingreso al servidor: {joined_ts}\n"
                f"Ingreso a Discord: {created_ts}\n"
                f"Booster del servidor: {boost_text}\n"
                f"Roles: {role_preview}\n"
                f"Advertencias: {len(warnings)}\n"
                f"Muestra de razones de advertencia: {warning_preview}\n"
                f"Mensajes recientes en este canal (cantidad {recent_count}): {recent_preview}"
            ),
        )

    async def _collect_recent_target_messages(
        self,
        *,
        channel: discord.TextChannel | discord.Thread,
        member_id: int,
        sample_limit: int,
        scan_limit: int,
    ) -> tuple[int, list[str]]:
        total = 0
        samples: list[str] = []
        try:
            async for msg in channel.history(limit=scan_limit):
                if msg.author.id != member_id:
                    continue
                content = (msg.content or "").strip()
                if not content and msg.attachments:
                    content = "[attachment]"
                if not content:
                    continue
                normalized = " ".join(content.split())
                total += 1
                if len(samples) < sample_limit:
                    samples.append(normalized[:160])
                if total >= max(sample_limit * 4, 20):
                    break
        except (discord.Forbidden, discord.HTTPException):
            return 0, []
        return total, samples

    @staticmethod
    def _format_roast_reply(text: str, target_mention: str) -> str:
        cleaned = text.strip().strip("`")
        # Strip common speaker prefixes the model may prepend.
        prefix_re = re.compile(
            r"^(?:\*\*)?(?:nitori(?:-buchona)?|bot)\s*[:\-]\s*",
            flags=re.IGNORECASE,
        )
        while True:
            updated = prefix_re.sub("", cleaned, count=1).lstrip()
            if updated == cleaned:
                break
            cleaned = updated

        if not cleaned:
            return target_mention

        if cleaned.startswith(target_mention):
            # If the model duplicated the mention at start, reduce to one.
            doubled = f"{target_mention} {target_mention}"
            if cleaned.startswith(doubled):
                return f"{target_mention} {cleaned[len(doubled):].lstrip()}"
            return cleaned
        return f"{target_mention} {cleaned}"

    @staticmethod
    def _member_name_variants(member: discord.Member) -> list[str]:
        variants: list[str] = []
        if member.name:
            variants.append(member.name)
        if member.display_name:
            variants.append(member.display_name)
        global_name = getattr(member, "global_name", None)
        if isinstance(global_name, str) and global_name:
            variants.append(global_name)
        discriminator = getattr(member, "discriminator", None)
        if isinstance(discriminator, str) and discriminator not in {"", "0"}:
            variants.append(f"{member.name}#{discriminator}")
        return variants

    async def _resolve_relay_target_from_prompt(
        self,
        *,
        guild: discord.Guild,
        author: discord.Member,
        prompt: str,
        lang: str,
    ) -> dict[str, object] | None:
        lowered = prompt.casefold()
        relay_markers = (
            "tell ",
            "let ",
            "notify ",
            "remind ",
            "ask ",
            "ping ",
            "message ",
            "dile ",
            "avisale",
            "avísale",
            "notifica",
            "recuerdale",
            "recuérdale",
            "preguntale",
            "pregúntale",
            "avisa ",
        )
        if not any(marker in lowered for marker in relay_markers):
            return None

        if self.bot.user is not None:
            for raw_id in re.findall(r"<@!?(\d{15,22})>", prompt):
                try:
                    user_id = int(raw_id)
                except ValueError:
                    continue
                if user_id in {author.id, self.bot.user.id}:
                    continue
                member = guild.get_member(user_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(user_id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        continue
                if member is not None:
                    return self._build_relay_context(author, member, raw_query=member.display_name)

        for query in self._extract_relay_name_candidates(prompt):
            member, _ = await self._resolve_target_member(guild, query, lang)
            if member is None:
                continue
            if member.id == author.id:
                continue
            if self.bot.user is not None and member.id == self.bot.user.id:
                continue
            return self._build_relay_context(author, member, raw_query=query)
        return None

    @staticmethod
    def _extract_relay_name_candidates(prompt: str) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()

        def push(value: str) -> None:
            cleaned = " ".join(value.strip().split())
            if not cleaned:
                return
            key = cleaned.casefold()
            if key in seen:
                return
            seen.add(key)
            candidates.append(cleaned)

        patterns = (
            r"\b(?:tell|let|notify|remind|ask|ping|message)\s+([@A-Za-z0-9_.\-]{2,32})(?:\s+(?:know|that|to)\b|[^\w]|$)",
            r"\b(?:dile|avisale|avísale|notifica(?:le)?|recuerdale|recuérdale|preguntale|pregúntale)\s+a?\s*([@A-Za-z0-9_.\-]{2,32})(?:\s+que\b|[^\w]|$)",
            r"\b(?:avisa|avisar)\s+a?\s*([@A-Za-z0-9_.\-]{2,32})(?:\s+que\b|[^\w]|$)",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, prompt, flags=re.IGNORECASE):
                push(match.group(1).lstrip("@"))

        for at_name in re.findall(r"(?<!<)@([A-Za-z0-9_.\-]{2,64})", prompt):
            push(at_name)

        # Also consider quoted names.
        for quoted in re.findall(r"[\"'“”‘’]([A-Za-z0-9_.\-]{2,64})[\"'“”‘’]", prompt):
            push(quoted)

        return candidates[:10]

    def _build_relay_context(
        self,
        requester: discord.Member,
        target: discord.Member,
        *,
        raw_query: str,
    ) -> dict[str, object]:
        aliases = [target.display_name, target.name]
        global_name = getattr(target, "global_name", None)
        if isinstance(global_name, str) and global_name:
            aliases.append(global_name)
        aliases.append(raw_query)
        deduped: list[str] = []
        seen: set[str] = set()
        for alias in aliases:
            clean = alias.strip()
            if not clean:
                continue
            key = clean.casefold()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(clean)
        return {
            "target_id": target.id,
            "target_mention": target.mention,
            "target_names": deduped,
            "requester_id": requester.id,
            "requester_mention": requester.mention,
            "requester_name": requester.display_name,
            "raw_query": raw_query.strip(),
        }

    @staticmethod
    def _build_mention_hints(relay_context: dict[str, object] | None) -> list[str] | None:
        if not relay_context:
            return None
        target_mention = str(relay_context.get("target_mention", "")).strip()
        raw_query = str(relay_context.get("raw_query", "")).strip()
        requester_mention = str(relay_context.get("requester_mention", "")).strip()
        requester_name = str(relay_context.get("requester_name", "")).strip()
        hints: list[str] = []
        if raw_query and target_mention:
            hints.append(f"{raw_query} -> {target_mention}")
        names = relay_context.get("target_names")
        if isinstance(names, list):
            for name in names[:3]:
                if isinstance(name, str) and name.strip() and target_mention:
                    hints.append(f"{name.strip()} -> {target_mention}")
        if requester_mention and requester_name:
            hints.append(f"requester {requester_name} -> {requester_mention}")
        return hints or None

    @staticmethod
    def _build_relay_instruction(
        relay_context: dict[str, object] | None,
        lang: str,
    ) -> str | None:
        if not relay_context:
            return None
        target_mention = str(relay_context.get("target_mention", "")).strip()
        requester_mention = str(relay_context.get("requester_mention", "")).strip()
        if not target_mention or not requester_mention:
            return None
        return tr(
            lang,
            (
                f"Resolved relay target: {target_mention}. "
                f"Requester: {requester_mention}. "
                "Deliver the message to the target. Mention target once. "
                "Do not ping requester unless explicitly asked."
            ),
            (
                f"Objetivo de relevo resuelto: {target_mention}. "
                f"Solicitante: {requester_mention}. "
                "Entrega el mensaje al objetivo. Menciona al objetivo una sola vez. "
                "No etiquetes al solicitante a menos que te lo pidan explícitamente."
            ),
        )

    def _apply_relay_postprocess(
        self,
        text: str,
        relay_context: dict[str, object] | None,
    ) -> str:
        if not relay_context:
            return text

        target_mention = str(relay_context.get("target_mention", "")).strip()
        requester_mention = str(relay_context.get("requester_mention", "")).strip()
        requester_name = str(relay_context.get("requester_name", "")).strip()
        target_names = relay_context.get("target_names")
        if not target_mention:
            return text

        updated = text

        # Fix malformed mentions containing the target alias.
        if isinstance(target_names, list):
            for alias in target_names:
                if not isinstance(alias, str):
                    continue
                alias_clean = alias.strip()
                if len(alias_clean) < 3:
                    continue
                malformed = re.compile(
                    rf"<@!?[^>]*{re.escape(alias_clean)}[^>]*>",
                    flags=re.IGNORECASE,
                )
                updated = malformed.sub(target_mention, updated)

        # Avoid pinging requester in relay replies unless explicitly requested.
        if requester_mention and requester_name and requester_mention in updated:
            updated = updated.replace(requester_mention, requester_name)

        # Ensure target is explicitly mentioned at least once.
        if target_mention not in updated and isinstance(target_names, list):
            for alias in target_names:
                if not isinstance(alias, str):
                    continue
                alias_clean = alias.strip()
                if len(alias_clean) < 3:
                    continue
                plain_alias = alias_clean.lstrip("@")
                name_re = re.compile(
                    rf"(?<![\w<@])@?{re.escape(plain_alias)}(?![\w>])",
                    flags=re.IGNORECASE,
                )
                updated, count = name_re.subn(target_mention, updated, count=1)
                if count:
                    break
        if target_mention not in updated:
            updated = f"{target_mention} {updated}".strip()

        updated = self._dedupe_repeated_mentions(updated)
        return updated

    async def _resolve_target_member(
        self,
        guild: discord.Guild,
        user_query: str,
        lang: str,
    ) -> tuple[discord.Member | None, str | None]:
        query = user_query.strip()
        if not query:
            return None, tr(
                lang,
                "Provide a user mention, ID, username, or display name.",
                "Proporciona una mencion, ID, nombre de usuario o nombre visible.",
            )

        user_id = parse_user_id_from_text(query)
        if user_id is not None:
            member = guild.get_member(user_id)
            if member is not None:
                return member, None
            try:
                member = await guild.fetch_member(user_id)
                return member, None
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None, tr(
                    lang,
                    "I couldn't find a server member with that ID.",
                    "No pude encontrar un miembro del servidor con esa ID.",
                )

        query_cf = query.casefold()

        def matches_exact(member: discord.Member) -> bool:
            return any(name.casefold() == query_cf for name in self._member_name_variants(member))

        def matches_prefix(member: discord.Member) -> bool:
            return any(name.casefold().startswith(query_cf) for name in self._member_name_variants(member))

        exact_cached = [m for m in guild.members if matches_exact(m)]
        if len(exact_cached) == 1:
            return exact_cached[0], None
        if len(exact_cached) > 1:
            return None, tr(
                lang,
                "More than one member matches that name. Use @mention or user ID.",
                "Mas de un miembro coincide con ese nombre. Usa @mencion o ID de usuario.",
            )

        prefix_cached = [m for m in guild.members if matches_prefix(m)]
        if len(prefix_cached) == 1:
            return prefix_cached[0], None
        if len(prefix_cached) > 1:
            return None, tr(
                lang,
                "More than one member matches that name. Use @mention or user ID.",
                "Mas de un miembro coincide con ese nombre. Usa @mencion o ID de usuario.",
            )

        try:
            queried = await guild.query_members(query=query, limit=25)
        except (discord.Forbidden, discord.HTTPException):
            queried = []

        exact_queried = [m for m in queried if matches_exact(m)]
        if len(exact_queried) == 1:
            return exact_queried[0], None
        if len(exact_queried) > 1:
            return None, tr(
                lang,
                "More than one member matches that name. Use @mention or user ID.",
                "Mas de un miembro coincide con ese nombre. Usa @mencion o ID de usuario.",
            )

        prefix_queried = [m for m in queried if matches_prefix(m)]
        if len(prefix_queried) == 1:
            return prefix_queried[0], None
        if len(prefix_queried) > 1:
            return None, tr(
                lang,
                "More than one member matches that name. Use @mention or user ID.",
                "Mas de un miembro coincide con ese nombre. Usa @mencion o ID de usuario.",
            )

        return None, tr(
            lang,
            "User not found in this server. Use @mention, user ID, username, or display name.",
            "No encontre al usuario en este servidor. Usa @mencion, ID, nombre de usuario o nombre visible.",
        )

    @commands.hybrid_command(
        name="translate",
        description="Translate replied text or provided text.",
    )
    async def translate_cmd(
        self,
        ctx: commands.Context,
        language: str,
        *,
        text: str | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        target = self._normalize_target_language(language)
        if not target:
            await ctx.send(self._translate_usage_message(lang))
            return

        source_text = (text or "").strip()
        if not source_text and ctx.message is not None:
            replied = await self._get_replied_message(ctx.message)
            if replied is not None:
                source_text = replied.content.strip()

        if not source_text:
            await ctx.send(
                tr(
                    lang,
                    "Reply to a message and use `translate <language>`, or provide text after the language.",
                    "Responde a un mensaje y usa `translate <idioma>`, o agrega texto después del idioma.",
                )
            )
            return

        if self._is_same_language(source_text, target):
            await ctx.send(
                tr(
                    lang,
                    "The original text it's already on that language!",
                    "El mensaje original ya estaba en ese idioma!",
                )
            )
            return

        try:
            translated = await self.bot.llm_client.translate(
                text=source_text,
                target_language=target,
            )
        except Exception as exc:
            logging.exception("AI translate command failure in guild=%s", ctx.guild.id if ctx.guild else None)
            if self._is_ai_limit_error(exc):
                await ctx.send(
                    tr(
                        lang,
                        "Wow, I think that's a lot of talking from me right now, try later!",
                        "Wow, creo que he hablado demasiado por ahora, intenta más tarde!",
                    )
                )
            elif self._is_empty_completion_error(exc):
                await ctx.send(
                    tr(
                        lang,
                        "Oops, my brain lagged for a second. Try again!",
                        "Ups, se me fue la onda por un segundo. Intenta otra vez!",
                    )
                )
            else:
                await ctx.send(
                    tr(
                        lang,
                        "I hit an internal AI error while translating. Please try again.",
                        "Tuve un error interno de IA al traducir. Intenta de nuevo.",
                    )
                )
            return

        translated = await self._normalize_discord_references(translated, ctx.guild)
        await ctx.send(translated[:1900], allowed_mentions=discord.AllowedMentions.none())

    async def _handle_mention_translate(self, message: discord.Message, lang: str) -> bool:
        if self.bot.user is None:
            return False
        if self.bot.user not in message.mentions:
            return False

        original = await self._get_replied_message(message)
        if original is None:
            return False
        if self.bot.user and original.author.id == self.bot.user.id:
            return False

        content_wo_mention = self._remove_bot_mentions(message.content, self.bot.user.id)
        if not content_wo_mention:
            return False

        raw = content_wo_mention.strip()
        lowered = raw.lower()

        language_input: str | None = None
        explicit_translate = lowered.startswith("translate")
        if explicit_translate:
            language_input = raw[9:].strip()
            if language_input.startswith(":"):
                language_input = language_input[1:].strip()
            if not language_input:
                await message.reply(
                    self._translate_usage_message(lang),
                    mention_author=True,
                )
                return True
        else:
            # Shortcut mode only accepts a single token language name.
            if " " in raw:
                return False
            language_input = raw.strip(".,!?;:()[]{}\"'`")
            if not language_input:
                return False

        if not language_input:
            return False

        target = self._normalize_target_language(language_input)
        if not target:
            if explicit_translate:
                await message.reply(
                    self._translate_usage_message(lang),
                    mention_author=True,
                )
                return True
            return False

        await self._run_translation(message, original, target, lang)
        return True

    async def _run_translation(
        self,
        trigger_message: discord.Message,
        original: discord.Message,
        language: str,
        lang: str,
    ) -> None:
        if not self._allowed_by_cooldown(trigger_message):
            return
        if not original.content.strip():
            await trigger_message.reply(
                "The replied message has no text to translate.",
                mention_author=True,
            )
            return
        if self._is_same_language(original.content, language):
            await trigger_message.reply(
                tr(
                    lang,
                    "The original text it's already on that language!",
                    "El mensaje original ya estaba en ese idioma!",
                ),
                mention_author=True,
            )
            return

        await trigger_message.channel.typing()
        try:
            translated = await self.bot.llm_client.translate(
                text=original.content,
                target_language=language,
            )
        except Exception as exc:
            logging.exception(
                "AI mention-translate failure in guild=%s channel=%s",
                trigger_message.guild.id if trigger_message.guild else None,
                trigger_message.channel.id,
            )
            if self._is_ai_limit_error(exc):
                await trigger_message.reply(
                    tr(
                        lang,
                        "Wow, I think that's a lot of talking from me right now, try later!",
                        "Wow, creo que he hablado demasiado por ahora, intenta más tarde!",
                    ),
                    mention_author=True,
                )
            elif self._is_empty_completion_error(exc):
                await trigger_message.reply(
                    tr(
                        lang,
                        "Oops, my brain lagged for a second. Try again!",
                        "Ups, se me fue la onda por un segundo. Intenta otra vez!",
                    ),
                    mention_author=True,
                )
            else:
                await trigger_message.reply(
                    tr(
                        lang,
                        "I hit an internal AI error while translating. Please try again.",
                        "Tuve un error interno de IA al traducir. Intenta de nuevo.",
                    ),
                    mention_author=True,
                )
            return

        translated = await self._normalize_discord_references(translated, trigger_message.guild)
        await trigger_message.reply(translated[:1900], mention_author=True)

    async def _is_chat_trigger(
        self,
        message: discord.Message,
        replied_message: discord.Message | None = None,
    ) -> bool:
        if self.bot.user is None:
            return False

        if self.bot.user in message.mentions:
            stripped = self._remove_bot_mentions(message.content, self.bot.user.id)
            return bool(stripped)

        ref_msg = replied_message
        if ref_msg is None:
            ref_msg = await self._get_replied_message(message)
        return bool(
            ref_msg
            and self.bot.user
            and ref_msg.author.id == self.bot.user.id
            and self._is_chat_response_message(ref_msg.id)
        )

    def _extract_chat_prompt(self, message: discord.Message) -> str:
        if self.bot.user and self.bot.user in message.mentions:
            return self._remove_bot_mentions(message.content, self.bot.user.id)
        return message.content.strip()

    @staticmethod
    def _remove_bot_mentions(text: str, bot_id: int) -> str:
        text = text.replace(f"<@{bot_id}>", "").replace(f"<@!{bot_id}>", "")
        return text.strip()

    @staticmethod
    def _detect_command_hint(prompt: str) -> str | None:
        lowered = prompt.strip().casefold()
        if not lowered:
            return None
        lowered = lowered.lstrip("/.!")
        parts = re.findall(r"[a-z0-9]+", lowered)
        if not parts:
            return None

        first = parts[0]
        second = parts[1] if len(parts) > 1 else ""
        if first == "channel":
            if second in {"delete", "del", "remove"}:
                return "channel delete"
            if second == "add":
                return "channel add"
            if second in {"clear", "clone", "lock", "unlock", "slowmode"}:
                return f"channel {second}"
        if first == "message" and second in {"delete", "clear", "purgeuser"}:
            return f"message {second}"
        if first in {"member", "user"} and second in {
            "info",
            "setnick",
            "mute",
            "unmute",
            "kick",
            "ban",
            "unban",
            "tempmute",
            "tempban",
            "warn",
            "unwarn",
            "warnings",
            "clearwarnings",
        }:
            return f"user {second}"
        if first == "role" and second in {"add", "remove", "create"}:
            return f"role {second}"
        if first == "color" and second in {"setup", "list", "channel", "reload", "add", "remove"}:
            return f"color {second}"
        compact = f"{first}{second}" if second else first
        for key in (compact, first):
            hinted = COMMAND_HINTS.get(key)
            if hinted:
                return hinted
        return None

    @staticmethod
    def _conversation_key(guild_id: int, channel_id: int) -> tuple[int, int]:
        return (guild_id, channel_id)

    def _build_conversation_history(
        self, key: tuple[int, int]
    ) -> list[dict[str, str]]:
        history = self._conversation_history.get(key)
        if not history:
            return []
        return list(history)

    async def _ensure_history_loaded(
        self,
        key: tuple[int, int],
        guild_id: int,
        channel_id: int,
    ) -> None:
        history = self._conversation_history.get(key)
        if history and len(history) > 0:
            return
        rows = await self.bot.db.get_ai_conversation_history(
            guild_id=guild_id,
            channel_id=channel_id,
            limit=120,
        )
        loaded = deque(maxlen=120)
        for row in rows:
            role = str(row.get("role", "")).strip().lower()
            content = str(row.get("content", "")).strip()
            if role not in {"user", "assistant"} or not content:
                continue
            loaded.append({"role": role, "content": content})
        self._conversation_history[key] = loaded

    def _append_conversation_turn(
        self,
        key: tuple[int, int],
        *,
        role: str,
        speaker: str,
        content: str,
    ) -> str | None:
        cleaned = " ".join(content.strip().split())
        if not cleaned:
            return None
        if len(cleaned) > self._history_entry_max_chars:
            cleaned = cleaned[: self._history_entry_max_chars - 3].rstrip() + "..."
        prefixed = f"{speaker}: {cleaned}"
        history = self._conversation_history[key]
        if history:
            last = history[-1]
            if last.get("role") == role and last.get("content") == prefixed:
                return None
        history.append({"role": role, "content": prefixed})
        return prefixed

    async def _persist_conversation_turn(
        self,
        *,
        guild_id: int,
        channel_id: int,
        role: str,
        speaker: str,
        content: str,
    ) -> None:
        await self.bot.db.add_ai_conversation_turn(
            guild_id=guild_id,
            channel_id=channel_id,
            role=role,
            speaker=speaker,
            content=content,
        )

    @staticmethod
    def _channel_reference(channel: discord.abc.GuildChannel | discord.Thread) -> str:
        return f"<#{channel.id}>"

    async def _send_long_reply(
        self,
        trigger_message: discord.Message,
        text: str,
        *,
        mention_author: bool,
    ) -> None:
        parts = self._split_for_discord(text, limit=1900)
        if not parts:
            return
        first = await trigger_message.reply(parts[0], mention_author=mention_author)
        self._remember_chat_response_message(first.id)
        for part in parts[1:]:
            extra = await trigger_message.channel.send(part)
            self._remember_chat_response_message(extra.id)

    def _is_chat_response_message(self, message_id: int) -> bool:
        return message_id in self._chat_response_id_set

    def _remember_chat_response_message(self, message_id: int) -> None:
        if message_id in self._chat_response_id_set:
            return
        if len(self._chat_response_ids) >= self._chat_response_id_limit:
            oldest = self._chat_response_ids.popleft()
            self._chat_response_id_set.discard(oldest)
        self._chat_response_ids.append(message_id)
        self._chat_response_id_set.add(message_id)

    def _strip_bot_speaker_prefix(self, text: str) -> str:
        cleaned = text.strip().strip("`")
        names = {
            "nitori",
            "nitori-buchona",
            "nitori kawashiro",
            "bot",
        }
        if self.bot.user is not None:
            if self.bot.user.name:
                names.add(self.bot.user.name)
            if self.bot.user.display_name:
                names.add(self.bot.user.display_name)

        pattern_names = [name.strip() for name in names if name and name.strip()]
        if not pattern_names:
            return cleaned
        pattern_names.sort(key=len, reverse=True)
        joined = "|".join(re.escape(name) for name in pattern_names)
        prefix_re = re.compile(
            rf"^(?:\*\*)?(?:{joined})(?:\*\*)?\s*[:\-]\s*",
            flags=re.IGNORECASE,
        )
        while True:
            updated = prefix_re.sub("", cleaned, count=1).lstrip()
            if updated == cleaned:
                break
            cleaned = updated

        # Remove in-line speaker labels like "Nitori:" that models may inject mid-message.
        inline_label_re = re.compile(
            rf"(?:(?<=\s)|^)(?:\*\*)?(?:{joined})(?:\*\*)?\s*[:\-]\s*",
            flags=re.IGNORECASE,
        )
        cleaned = inline_label_re.sub(" ", cleaned)

        if self.bot.user is not None:
            mention_label_re = re.compile(
                rf"<@!?{self.bot.user.id}>\s*[:\-]\s*",
                flags=re.IGNORECASE,
            )
            cleaned = mention_label_re.sub("", cleaned)

            for bot_name in (self.bot.user.name, self.bot.user.display_name):
                if not bot_name:
                    continue
                plain_at_label_re = re.compile(
                    rf"@{re.escape(bot_name)}\s*[:\-]\s*",
                    flags=re.IGNORECASE,
                )
                cleaned = plain_at_label_re.sub("", cleaned)

        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned

    @staticmethod
    def _split_for_discord(text: str, *, limit: int) -> list[str]:
        cleaned = text.strip()
        if not cleaned:
            return []
        chunks: list[str] = []
        remaining = cleaned
        while len(remaining) > limit:
            cut = remaining.rfind("\n", 0, limit)
            if cut < int(limit * 0.5):
                cut = remaining.rfind(" ", 0, limit)
            if cut < int(limit * 0.5):
                cut = limit
            piece = remaining[:cut].rstrip()
            if piece:
                chunks.append(piece)
            remaining = remaining[cut:].lstrip()
        if remaining:
            chunks.append(remaining)
        return chunks

    @staticmethod
    async def _get_replied_message(message: discord.Message) -> discord.Message | None:
        ref = message.reference
        if ref is None or ref.message_id is None:
            return None
        if isinstance(ref.resolved, discord.Message):
            return ref.resolved

        try:
            return await message.channel.fetch_message(ref.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    def _allowed_by_cooldown(self, message: discord.Message) -> bool:
        key = (message.guild.id, message.author.id)  # type: ignore[union-attr]
        now = time.monotonic()
        last = self._cooldowns.get(key, 0.0)
        if now - last < self._cooldown_seconds:
            return False
        self._cooldowns[key] = now
        return True

    async def _lang(self, guild: discord.Guild | None) -> str:
        if guild is None:
            return "en"
        settings = await self.bot.db.get_guild_settings(guild.id)
        return settings.language_code

    @staticmethod
    def _is_ai_limit_error(error: Exception) -> bool:
        msg = str(error).lower()
        markers = (
            "429",
            "rate limit",
            "too many requests",
            "tokens per minute",
            "request too large",
            "context length",
            "maximum context length",
            "token limit",
            "quota",
            "insufficient_quota",
        )
        return any(marker in msg for marker in markers)

    @staticmethod
    def _is_empty_completion_error(error: Exception) -> bool:
        return "empty completion" in str(error).lower()

    async def _normalize_discord_references(
        self, text: str, guild: discord.Guild | None
    ) -> str:
        if guild is None:
            return text

        user_prefixed = re.compile(r"(?<!<)@(\d{15,22})(?!>)")
        channel_prefixed = re.compile(r"(?<!<)#(\d{15,22})(?!>)")
        user_name_prefixed = re.compile(r"(?<![<\w])@([^\s@#<>]{1,64})")
        channel_name_prefixed = re.compile(r"(?<![<\w])#([^\s@#<>]{1,100})")
        malformed_user_angle = re.compile(r"<@!?([^\s@#<>]{1,64})>")
        malformed_user_angle_loose = re.compile(r"<@!?([^<>]{1,80})>")
        malformed_channel_angle = re.compile(r"<#([^\s@#<>]{1,100})>")
        bare_id = re.compile(r"(?<![@#<])\b(\d{15,22})\b(?!>)")

        text = user_prefixed.sub(lambda m: f"<@{m.group(1)}>", text)
        text = channel_prefixed.sub(lambda m: f"<#{m.group(1)}>", text)

        member_map: dict[str, int] = {}
        member_compact_candidates: list[tuple[str, int]] = []
        seen_compact_pairs: set[tuple[str, int]] = set()

        def register_member_candidate(member_id: int, candidate: str | None) -> None:
            if not isinstance(candidate, str):
                return
            direct_key = self._normalize_lookup_key(candidate)
            if direct_key and direct_key not in member_map:
                member_map[direct_key] = member_id
            compact_key = self._compact_lookup_key(candidate)
            if compact_key:
                if compact_key not in member_map:
                    member_map[compact_key] = member_id
                pair = (compact_key, member_id)
                if pair not in seen_compact_pairs:
                    seen_compact_pairs.add(pair)
                    member_compact_candidates.append(pair)

        for member in guild.members:
            candidates = [member.name, member.display_name]
            global_name = getattr(member, "global_name", None)
            if isinstance(global_name, str):
                candidates.append(global_name)
            for candidate in candidates:
                register_member_candidate(member.id, candidate)

        channel_map: dict[str, int] = {}
        for channel in guild.text_channels:
            direct_key = self._normalize_lookup_key(channel.name)
            if direct_key and direct_key not in channel_map:
                channel_map[direct_key] = channel.id
            compact_key = self._compact_lookup_key(channel.name)
            if compact_key and compact_key not in channel_map:
                channel_map[compact_key] = channel.id

        def fuzzy_member_id(compact_query: str) -> int | None:
            if len(compact_query) < 3:
                return None
            by_member_id: dict[int, float] = {}
            for compact_key, member_id in member_compact_candidates:
                base = difflib.SequenceMatcher(None, compact_query, compact_key).ratio()
                bonus = 0.0
                if compact_query == compact_key:
                    bonus += 0.45
                if compact_query in compact_key:
                    bonus += 0.25
                elif compact_key in compact_query:
                    bonus += 0.10
                if compact_key.startswith(compact_query) or compact_query.startswith(compact_key):
                    bonus += 0.08
                score = base + bonus
                if score > by_member_id.get(member_id, 0.0):
                    by_member_id[member_id] = score

            if not by_member_id:
                return None
            ranked = sorted(by_member_id.items(), key=lambda kv: kv[1], reverse=True)
            best_id, best_score = ranked[0]
            second_score = ranked[1][1] if len(ranked) > 1 else 0.0
            if best_score >= 0.78 and (best_score - second_score >= 0.07 or best_score >= 1.05):
                return best_id
            return None

        def resolve_member_id_from_token(token_raw: str) -> int | None:
            for candidate in self._candidate_user_queries(token_raw):
                direct_key = self._normalize_lookup_key(candidate)
                compact_key = self._compact_lookup_key(candidate)
                resolved = member_map.get(direct_key) or member_map.get(compact_key)
                if resolved is not None:
                    return resolved
                if compact_key:
                    fuzzy = fuzzy_member_id(compact_key)
                    if fuzzy is not None:
                        if direct_key:
                            member_map[direct_key] = fuzzy
                        if compact_key:
                            member_map[compact_key] = fuzzy
                        return fuzzy
            return None

        unresolved_user_tokens: set[str] = set()
        for match in user_name_prefixed.finditer(text):
            token_core, _ = self._split_token(match.group(1))
            if not token_core:
                continue
            if token_core.lower() in {"everyone", "here"}:
                continue
            direct_key = self._normalize_lookup_key(token_core)
            compact_key = self._compact_lookup_key(token_core)
            if direct_key and direct_key in member_map:
                continue
            if compact_key and compact_key in member_map:
                continue
            unresolved_user_tokens.add(token_core)
        for match in malformed_user_angle.finditer(text):
            token = match.group(1)
            if token.isdigit():
                continue
            token_core, _ = self._split_token(token)
            if not token_core:
                continue
            if token_core.lower() in {"everyone", "here"}:
                continue
            direct_key = self._normalize_lookup_key(token_core)
            compact_key = self._compact_lookup_key(token_core)
            if direct_key and direct_key in member_map:
                continue
            if compact_key and compact_key in member_map:
                continue
            unresolved_user_tokens.add(token_core)
        for match in malformed_user_angle_loose.finditer(text):
            token = match.group(1).strip()
            if not token or token.isdigit():
                continue
            unresolved_user_tokens.add(token)

        for token in list(unresolved_user_tokens)[:6]:
            resolved_id = resolve_member_id_from_token(token)
            if resolved_id is None:
                resolved_id = await self._query_member_id(guild, token)
            if resolved_id is None:
                continue
            for candidate in self._candidate_user_queries(token):
                register_member_candidate(resolved_id, candidate)

        def resolve_user_name(match: re.Match[str]) -> str:
            token = match.group(1)
            token_core, suffix = self._split_token(token)
            if not token_core:
                return match.group(0)
            if token_core.lower() in {"everyone", "here"}:
                return match.group(0)
            member_id = resolve_member_id_from_token(token_core)
            if member_id is None:
                return match.group(0)
            return f"<@{member_id}>{suffix}"

        def resolve_channel_name(match: re.Match[str]) -> str:
            token = match.group(1)
            token_core, suffix = self._split_token(token)
            if not token_core:
                return match.group(0)
            direct_key = self._normalize_lookup_key(token_core)
            compact_key = self._compact_lookup_key(token_core)
            channel_id = channel_map.get(direct_key) or channel_map.get(compact_key)
            if channel_id is None:
                return match.group(0)
            return f"<#{channel_id}>{suffix}"

        def resolve_malformed_user(match: re.Match[str]) -> str:
            token = match.group(1)
            if token.isdigit():
                return f"<@{token}>"
            token_core, suffix = self._split_token(token)
            if not token_core:
                return match.group(0)
            if token_core.lower() in {"everyone", "here"}:
                return match.group(0)
            member_id = resolve_member_id_from_token(token_core)
            if member_id is None:
                return match.group(0)
            return f"<@{member_id}>{suffix}"

        def resolve_malformed_user_loose(match: re.Match[str]) -> str:
            token = match.group(1).strip()
            if not token:
                return match.group(0)
            if token.isdigit():
                return f"<@{token}>"
            member_id = resolve_member_id_from_token(token)
            if member_id is None:
                return match.group(0)
            return f"<@{member_id}>"

        def resolve_malformed_channel(match: re.Match[str]) -> str:
            token = match.group(1)
            if token.isdigit():
                return f"<#{token}>"
            token_core, suffix = self._split_token(token)
            if not token_core:
                return match.group(0)
            direct_key = self._normalize_lookup_key(token_core)
            compact_key = self._compact_lookup_key(token_core)
            channel_id = channel_map.get(direct_key) or channel_map.get(compact_key)
            if channel_id is None:
                return match.group(0)
            return f"<#{channel_id}>{suffix}"

        text = user_name_prefixed.sub(resolve_user_name, text)
        text = channel_name_prefixed.sub(resolve_channel_name, text)
        text = malformed_user_angle.sub(resolve_malformed_user, text)
        text = malformed_user_angle_loose.sub(resolve_malformed_user_loose, text)
        text = malformed_channel_angle.sub(resolve_malformed_channel, text)

        def resolve_bare(match: re.Match[str]) -> str:
            raw = match.group(1)
            try:
                snowflake = int(raw)
            except ValueError:
                return raw

            channel = guild.get_channel(snowflake)
            if channel is None and hasattr(guild, "get_thread"):
                channel = guild.get_thread(snowflake)  # type: ignore[assignment]
            if channel is not None:
                return f"<#{snowflake}>"

            member = guild.get_member(snowflake)
            if member is not None:
                return f"<@{snowflake}>"
            return raw

        text = bare_id.sub(resolve_bare, text)
        text = self._normalize_custom_emoji_mentions(text, guild)
        return self._dedupe_repeated_mentions(text)

    @staticmethod
    def _normalize_custom_emoji_mentions(text: str, guild: discord.Guild) -> str:
        emojis: list[discord.Emoji] = [
            emoji for emoji in guild.emojis if emoji.available and emoji.is_usable()
        ]
        if not emojis:
            return text

        emoji_map: dict[str, discord.Emoji] = {}
        emoji_by_id: dict[int, discord.Emoji] = {}
        for emoji in emojis:
            key = AIChatCog._normalize_lookup_key(emoji.name)
            if key and key not in emoji_map:
                emoji_map[key] = emoji
            emoji_by_id[emoji.id] = emoji

        if not emoji_map and not emoji_by_id:
            return text

        full_emoji = re.compile(r"<a?:([A-Za-z0-9_]{2,32}):(\d{15,22})>")
        malformed_angle = re.compile(r"<(a?):([A-Za-z0-9_]{2,32})>")
        shortcode = re.compile(r"(?<!<):([A-Za-z0-9_]{2,32}):(?!\d)")

        def to_emoji_token(emoji: discord.Emoji) -> str:
            prefix = "a" if emoji.animated else ""
            return f"<{prefix}:{emoji.name}:{emoji.id}>"

        def resolve_full_emoji(match: re.Match[str]) -> str:
            name = match.group(1)
            raw_id = match.group(2)
            try:
                emoji_id = int(raw_id)
            except ValueError:
                return match.group(0)

            emoji = emoji_by_id.get(emoji_id)
            if emoji is None:
                key = AIChatCog._normalize_lookup_key(name)
                emoji = emoji_map.get(key)
            if emoji is None:
                return match.group(0)
            return to_emoji_token(emoji)

        def resolve_malformed_angle(match: re.Match[str]) -> str:
            name = match.group(2)
            key = AIChatCog._normalize_lookup_key(name)
            emoji = emoji_map.get(key)
            if emoji is None:
                return match.group(0)
            return to_emoji_token(emoji)

        def resolve_shortcode(match: re.Match[str]) -> str:
            name = match.group(1)
            key = AIChatCog._normalize_lookup_key(name)
            emoji = emoji_map.get(key)
            if emoji is None:
                return match.group(0)
            return to_emoji_token(emoji)

        text = full_emoji.sub(resolve_full_emoji, text)
        text = malformed_angle.sub(resolve_malformed_angle, text)
        text = shortcode.sub(resolve_shortcode, text)
        return text

    @staticmethod
    async def _query_member_id(guild: discord.Guild, token: str) -> int | None:
        queries = AIChatCog._candidate_user_queries(token)
        if not queries:
            return None

        for query in queries:
            cached = guild.get_member_named(query)
            if cached is not None:
                return cached.id

            try:
                matches = await guild.query_members(query=query, limit=8)
            except (discord.Forbidden, discord.HTTPException):
                continue

            if not matches:
                continue

            direct_key = AIChatCog._normalize_lookup_key(query)
            compact_key = AIChatCog._compact_lookup_key(query)
            for member in matches:
                for candidate in (
                    member.name,
                    member.display_name,
                    getattr(member, "global_name", None),
                ):
                    if not isinstance(candidate, str):
                        continue
                    if AIChatCog._normalize_lookup_key(candidate) == direct_key:
                        return member.id
                    if AIChatCog._compact_lookup_key(candidate) == compact_key:
                        return member.id
            return matches[0].id
        return None

    @staticmethod
    def _normalize_lookup_key(value: str) -> str:
        collapsed = " ".join(value.strip().casefold().split())
        if not collapsed:
            return ""
        normalized = unicodedata.normalize("NFKD", collapsed)
        return "".join(ch for ch in normalized if not unicodedata.combining(ch))

    @staticmethod
    def _compact_lookup_key(value: str) -> str:
        base = AIChatCog._normalize_lookup_key(value)
        if not base:
            return ""
        return re.sub(r"[^a-z0-9]+", "", base)

    @staticmethod
    def _split_token(token: str) -> tuple[str, str]:
        match = re.match(r"^([\w.-]{1,64})(.*)$", token, flags=re.UNICODE)
        if match:
            return match.group(1), match.group(2)
        trimmed = token.rstrip(".,!?;:)]}'\"")
        if not trimmed:
            return token, ""
        return trimmed, token[len(trimmed) :]

    @staticmethod
    def _candidate_user_queries(raw: str) -> list[str]:
        cleaned = " ".join(raw.strip().strip("@").split())
        if not cleaned:
            return []

        stopwords = {
            "whoever",
            "someone",
            "user",
            "usuario",
            "member",
            "miembro",
            "named",
            "name",
            "nombre",
            "called",
            "llamado",
            "llamada",
            "the",
            "el",
            "la",
            "al",
            "to",
            "a",
        }

        candidates: list[str] = []
        seen: set[str] = set()

        def push(value: str) -> None:
            item = " ".join(value.strip().split())
            if not item:
                return
            key = item.casefold()
            if key in seen:
                return
            seen.add(key)
            candidates.append(item)

        push(cleaned)
        tokens = re.findall(r"[A-Za-z0-9_.\-']{2,}", cleaned)
        filtered = [tok for tok in tokens if tok.casefold() not in stopwords]
        if filtered:
            push(" ".join(filtered))
            push(filtered[-1])
            push(filtered[0])
            for tok in filtered:
                push(tok)
        return candidates[:8]

    @staticmethod
    def _dedupe_repeated_mentions(text: str) -> str:
        user_dup = re.compile(r"(<@!?(\d{15,22})>)([\s\.,;:!?-]{0,10})<@!?\2>")
        channel_dup = re.compile(r"(<#(\d{15,22})>)([\s\.,;:!?-]{0,10})<#\2>")
        while True:
            updated, user_count = user_dup.subn(r"\1\3", text)
            updated, channel_count = channel_dup.subn(r"\1\3", updated)
            text = updated
            if user_count + channel_count == 0:
                break
        return text

    @staticmethod
    def _serialize_text_channels(guild: discord.Guild) -> list[str]:
        channels = sorted(guild.text_channels, key=lambda c: c.position)
        serialized: list[str] = []
        for channel in channels[:60]:
            serialized.append(f"#{channel.name} (<#{channel.id}>)")
        return serialized

    @staticmethod
    def _serialize_custom_emojis(guild: discord.Guild) -> list[str]:
        serialized: list[str] = []
        for emoji in guild.emojis:
            if not emoji.available or not emoji.is_usable():
                continue
            prefix = "a" if emoji.animated else ""
            serialized.append(f"{emoji.name}: <{prefix}:{emoji.name}:{emoji.id}>")
            if len(serialized) >= 80:
                break
        return serialized

    @staticmethod
    def _normalize_target_language(raw_language: str) -> str | None:
        normalized = raw_language.strip().lower()
        normalized = normalized.replace("_", " ").replace("-", " ")
        normalized = " ".join(normalized.split())
        return TRANSLATE_LANGUAGE_ALIASES.get(normalized)

    @staticmethod
    def _translate_usage_message(lang: str) -> str:
        choices = ", ".join(SUPPORTED_TRANSLATE_LANGUAGES)
        return tr(
            lang,
            (
                "**Supported translation languages**\n"
                f"`{choices}`\n"
                "Use:\n"
                "- `@Bot english` (replying to a message)\n"
                "- `/translate <language> [text]`"
            ),
            (
                "**Idiomas soportados para traduccion**\n"
                f"`{choices}`\n"
                "Usa:\n"
                "- `@Bot english` (respondiendo a un mensaje)\n"
                "- `/translate <idioma> [texto]`"
            ),
        )

    def _is_same_language(self, text: str, target_language: str) -> bool:
        detected = self._detect_text_language(text)
        return detected == target_language

    @staticmethod
    def _detect_text_language(text: str) -> str | None:
        raw = text.strip()
        if not raw:
            return None

        if re.search(r"[\u3040-\u30ff\u31f0-\u31ff\u4e00-\u9fff]", raw):
            return "japanese"
        if re.search(r"[\u0400-\u04FF]", raw):
            return "russian"

        lowered = raw.lower()
        tokens = re.findall(r"[a-zA-Z']+", lowered)
        if len(tokens) < 2:
            return None

        stopwords: dict[str, set[str]] = {
            "english": {"the", "and", "you", "are", "is", "this", "that", "with", "for", "what", "how"},
            "spanish": {"el", "la", "los", "las", "que", "como", "con", "para", "por", "estoy", "estas"},
            "german": {"der", "die", "das", "und", "ist", "ich", "nicht", "mit", "du", "wie", "was"},
            "french": {"le", "la", "les", "et", "est", "que", "avec", "pour", "pas", "comment", "quoi"},
            "italian": {"il", "lo", "la", "gli", "le", "e", "che", "con", "per", "come", "sono"},
            "portuguese": {"o", "a", "os", "as", "e", "que", "com", "para", "como", "voce"},
        }

        scores = {lang: 0.0 for lang in stopwords.keys()}
        for token in tokens:
            for lang, words in stopwords.items():
                if token in words:
                    scores[lang] += 1.0

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        if not ranked:
            return None
        best_lang, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        if best_score < 2.0:
            return None
        if best_score - second_score < 1.0:
            return None
        return best_lang

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AIChatCog(bot))
