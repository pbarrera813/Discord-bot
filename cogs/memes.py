from __future__ import annotations

import random
from datetime import datetime, timezone
from io import BytesIO
from typing import Any
from urllib.parse import urlparse

import discord
from discord.ext import commands

from services.modlog import send_modlog_embed
from utils.i18n import tr

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - optional runtime dependency
    Image = None
    ImageDraw = None
    ImageFont = None


class MemeHelpPaginatorView(discord.ui.View):
    def __init__(
        self,
        *,
        pages: list[discord.Embed],
        author_id: int,
        lang: str,
    ) -> None:
        super().__init__(timeout=180)
        self.pages = pages
        self.author_id = author_id
        self.lang = lang
        self.current_page = 0
        self.message: discord.Message | None = None

        self.prev_button = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label=tr(lang, "Previous", "Anterior"),
        )
        self.next_button = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label=tr(lang, "Next", "Siguiente"),
        )
        self.prev_button.callback = self._on_prev
        self.next_button.callback = self._on_next
        self.add_item(self.prev_button)
        self.add_item(self.next_button)
        self._sync_controls()

    def _sync_controls(self) -> None:
        single_page = len(self.pages) <= 1
        self.prev_button.disabled = single_page
        self.next_button.disabled = single_page

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            tr(
                self.lang,
                "Only the user who opened this panel can use these buttons.",
                "Solo el usuario que abrio este panel puede usar estos botones.",
            ),
            ephemeral=True,
        )
        return False

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        if not self.pages:
            return
        self.current_page = (self.current_page - 1) % len(self.pages)
        await interaction.response.edit_message(
            embed=self.pages[self.current_page], view=self
        )

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if not self.pages:
            return
        self.current_page = (self.current_page + 1) % len(self.pages)
        await interaction.response.edit_message(
            embed=self.pages[self.current_page], view=self
        )

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return


class MemesCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _lang(self, guild: discord.Guild | None) -> str:
        if guild is None:
            return "en"
        settings = await self.bot.db.get_guild_settings(guild.id)
        return settings.language_code

    async def _client_or_message(self, ctx: commands.Context, lang: str) -> Any | None:
        client = getattr(self.bot, "memegen_client", None)
        if client is not None:
            return client
        await ctx.send(
            tr(
                lang,
                "Memegen client is not available.",
                "El cliente de Memegen no está disponible.",
            )
        )
        return None

    async def _defer_if_needed(self, ctx: commands.Context) -> None:
        if ctx.interaction and not ctx.interaction.response.is_done():
            try:
                await ctx.defer()
            except (discord.NotFound, discord.HTTPException):
                return

    @staticmethod
    def _template_name(item: dict[str, Any]) -> str:
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return "Unknown"

    @staticmethod
    def _template_id(item: dict[str, Any]) -> str:
        template_id = item.get("id")
        if isinstance(template_id, str) and template_id.strip():
            return template_id.strip()
        return ""

    @staticmethod
    def _pick_template(
        templates: list[dict[str, Any]],
        query: str,
    ) -> dict[str, Any] | None:
        normalized = query.strip().casefold()
        if not normalized:
            return None

        exact: list[dict[str, Any]] = []
        partial: list[dict[str, Any]] = []
        for item in templates:
            template_id = MemesCog._template_id(item)
            template_name = MemesCog._template_name(item)
            keys = [template_id.casefold(), template_name.casefold()]
            if normalized in keys:
                exact.append(item)
                continue
            if normalized in keys[0] or normalized in keys[1]:
                partial.append(item)
        if exact:
            return exact[0]
        if partial:
            return partial[0]
        return None

    def _memehelp_pages(self, lang: str) -> list[discord.Embed]:
        page1 = discord.Embed(
            title=tr(lang, "Meme Command Guide", "Guia de comandos de memes"),
            description=tr(
                lang,
                "Create memes with templates, random picks, or custom images.",
                "Crea memes con plantillas, opciones aleatorias o imagenes personalizadas.",
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        page1.add_field(
            name=tr(lang, "Commands", "Comandos"),
            value=tr(
                lang,
                (
                    "`/meme create <template> <top_text> [bottom_text]`\n"
                    "`/meme random <top_text> [bottom_text]`\n"
                    "`/meme custom <image_url> <top_text> [bottom_text]`\n"
                    "`/meme custom <top_text> [bottom_text]` + attach image\n"
                    "`/meme templates [query]`\n"
                    "`/meme fonts [query]`\n"
                    "`/speech <user>`"
                ),
                (
                    "`/meme create <plantilla> <texto_arriba> [texto_abajo]`\n"
                    "`/meme random <texto_arriba> [texto_abajo]`\n"
                    "`/meme custom <url_imagen> <texto_arriba> [texto_abajo]`\n"
                    "`/meme custom <texto_arriba> [texto_abajo]` + adjunta imagen\n"
                    "`/meme templates [busqueda]`\n"
                    "`/meme fonts [busqueda]`\n"
                    "`/speech <usuario>`"
                ),
            ),
            inline=False,
        )
        page1.add_field(
            name=tr(lang, "Examples", "Ejemplos"),
            value=tr(
                lang,
                (
                    "`/meme create drake \"Use slash commands\" \"Use prefix commands\"`\n"
                    "`/meme random \"Monday mood\" \"Need more coffee\"`\n"
                    "`/meme custom https://example.com/cat.jpg \"Me coding\" \"Production at 5 PM\"`\n"
                    "`/meme custom \"Top text\" \"Bottom text\"` (with image attached)\n"
                    "`/meme templates cat`\n"
                    "`/meme fonts impact`\n"
                    "`/speech @Nitori`"
                ),
                (
                    "`/meme create drake \"Usar slash\" \"Usar prefijo\"`\n"
                    "`/meme random \"Humor del lunes\" \"Falta cafe\"`\n"
                    "`/meme custom https://example.com/gato.jpg \"Yo programando\" \"Produccion a las 5\"`\n"
                    "`/meme custom \"Texto arriba\" \"Texto abajo\"` (con imagen adjunta)\n"
                    "`/meme templates gato`\n"
                    "`/meme fonts impact`\n"
                    "`/speech @Nitori`"
                ),
            ),
            inline=False,
        )
        page1.add_field(
            name=tr(lang, "Tips", "Tips"),
            value=tr(
                lang,
                (
                    "- Search templates with `/meme templates [query]` before creating.\n"
                    "- If text has spaces, keep it inside quotes.\n"
                    "- For custom memes, use a direct public image URL.\n"
                    "- Official docs: https://memegen.link"
                ),
                (
                    "- Busca plantillas con `/meme templates [busqueda]` antes de crear.\n"
                    "- Si el texto tiene espacios, ponlo entre comillas.\n"
                    "- Para memes personalizados, usa una URL publica directa de imagen.\n"
                    "- Documentacion oficial: https://memegen.link"
                ),
            ),
            inline=False,
        )

        page2 = discord.Embed(
            title=tr(
                lang,
                "Meme Advanced Guide",
                "Guia avanzada de memes",
            ),
            description=tr(
                lang,
                "Page 2 covers advanced text formatting and Memegen escape behavior.",
                "La pagina 2 cubre formato avanzado de texto y escapes de Memegen.",
            ),
            color=discord.Color.teal(),
            timestamp=datetime.now(timezone.utc),
        )
        page2.add_field(
            name=tr(lang, "Special Character Rules", "Reglas de caracteres especiales"),
            value=tr(
                lang,
                (
                    "` ` -> `_`\n"
                    "`_` -> `__`\n"
                    "`-` -> `--`\n"
                    "`?` -> `~q`\n"
                    "`#` -> `~h`\n"
                    "`/` -> `~s`\n"
                    "`%` -> `~p`\n"
                    "`\"` -> `''`\n"
                    "new line -> `~n`"
                ),
                (
                    "` ` -> `_`\n"
                    "`_` -> `__`\n"
                    "`-` -> `--`\n"
                    "`?` -> `~q`\n"
                    "`#` -> `~h`\n"
                    "`/` -> `~s`\n"
                    "`%` -> `~p`\n"
                    "`\"` -> `''`\n"
                    "salto de linea -> `~n`"
                ),
            ),
            inline=False,
        )
        page2.add_field(
            name=tr(lang, "Advanced Examples", "Ejemplos avanzados"),
            value=tr(
                lang,
                (
                    "`/meme create drake \"Deploy now?\" \"Maybe after QA\"`\n"
                    "`/meme create xzibit \"I said #general\" \"Not #genral\"`\n"
                    "`/meme create doge \"Path /api/v1\" \"100% working\"`\n"
                    "`/meme custom \"\" \"Bottom only\"` (with image attached)\n"
                    "`/meme fonts noto`"
                ),
                (
                    "`/meme create drake \"Desplegar ya?\" \"Tal vez despues de QA\"`\n"
                    "`/meme create xzibit \"Dije #general\" \"No #genral\"`\n"
                    "`/meme create doge \"Ruta /api/v1\" \"100% funcionando\"`\n"
                    "`/meme custom \"\" \"Solo abajo\"` (con imagen adjunta)\n"
                    "`/meme fonts noto`"
                ),
            ),
            inline=False,
        )
        page2.add_field(
            name=tr(lang, "Note", "Nota"),
            value=tr(
                lang,
                "The bot applies escaping automatically to meme text. Use \"\" to intentionally leave top or bottom text empty.",
                "El bot aplica escapes automaticamente al texto del meme. Usa \"\" para dejar vacio el texto superior o inferior.",
            ),
            inline=False,
        )

        pages = [page1, page2]
        total = len(pages)
        for index, embed in enumerate(pages, start=1):
            embed.set_footer(
                text=tr(
                    lang,
                    f"Use the buttons to navigate. Page {index}/{total}",
                    f"Usa los botones para navegar. Pagina {index}/{total}",
                )
            )
        return pages

    @staticmethod
    def _looks_like_url(value: str) -> bool:
        text = value.strip()
        if not text:
            return False
        try:
            parsed = urlparse(text)
        except ValueError:
            return False
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    @staticmethod
    def _is_supported_image_payload(
        content_type: str | None,
        filename: str | None,
    ) -> bool:
        content_type_normalized = (content_type or "").lower()
        if content_type_normalized.startswith("image/"):
            return True
        normalized_filename = (filename or "").lower()
        return normalized_filename.endswith(
            (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".svg", ".avif")
        )

    @classmethod
    def _is_supported_image_attachment(cls, attachment: discord.Attachment) -> bool:
        return cls._is_supported_image_payload(attachment.content_type, attachment.filename)

    @classmethod
    def _interaction_image_url(cls, interaction: discord.Interaction | None) -> str | None:
        if interaction is None:
            return None

        data = interaction.data if isinstance(interaction.data, dict) else None
        resolved = data.get("resolved") if isinstance(data, dict) else None
        attachments = resolved.get("attachments") if isinstance(resolved, dict) else None
        if isinstance(attachments, dict):
            for payload in attachments.values():
                if not isinstance(payload, dict):
                    continue
                if not cls._is_supported_image_payload(
                    payload.get("content_type"),
                    payload.get("filename"),
                ):
                    continue
                for key in ("url", "proxy_url"):
                    candidate = payload.get(key)
                    if isinstance(candidate, str) and candidate.strip():
                        return candidate.strip()

        namespace = getattr(interaction, "namespace", None)
        if namespace is None:
            return None
        for value in vars(namespace).values():
            if isinstance(value, discord.Attachment) and cls._is_supported_image_attachment(value):
                return value.url
        return None

    @staticmethod
    def _normalize_meme_text(value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if text in {'""', "''"}:
            return ""
        return text

    async def _log_created_meme(
        self,
        *,
        guild: discord.Guild | None,
        lang: str,
        actor: discord.abc.User | discord.Member,
        meme_type: str,
        image_url: str | None,
        details: list[tuple[str, str]] | None = None,
        jump_url: str | None = None,
    ) -> None:
        if guild is None:
            return
        embed = discord.Embed(
            title=tr(lang, "Meme created", "Meme creado"),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name=tr(lang, "User", "Usuario"), value=actor.mention, inline=True)
        embed.add_field(name=tr(lang, "Type", "Tipo"), value=meme_type, inline=True)
        if details:
            for name, value in details[:6]:
                embed.add_field(name=name[:256], value=value[:1024], inline=False)
        if image_url:
            embed.set_image(url=image_url)

        view = None
        if jump_url:
            view = discord.ui.View()
            view.add_item(
                discord.ui.Button(
                    label=tr(lang, "Go to message", "Ir al mensaje"),
                    style=discord.ButtonStyle.link,
                    url=jump_url,
                )
            )
        await send_modlog_embed(guild, self.bot.db, embed, view=view)

    async def _build_speech_meme_file(
        self,
        *,
        user: discord.Member,
    ) -> discord.File | None:
        if Image is None or ImageDraw is None or ImageFont is None:
            return None

        avatar_asset = user.display_avatar.replace(size=512, format="png")
        try:
            avatar_bytes = await avatar_asset.read()
        except (discord.HTTPException, discord.NotFound, discord.Forbidden):
            return None

        try:
            avatar = Image.open(BytesIO(avatar_bytes)).convert("RGBA")
        except Exception:
            return None

        # Match the reference composition: wide canvas, high bubble area, avatar panel below.
        width = 760
        top_section_height = 323

        background_top = (236, 236, 236, 255)
        bubble_fill = (236, 236, 236, 255)
        bubble_outline = (16, 16, 16, 255)

        src_w, src_h = avatar.size
        if src_w <= 0 or src_h <= 0:
            return None

        panel_width = width
        # No margins and no crop: lower panel follows avatar ratio.
        panel_height = max(1, int(round(panel_width * (src_h / src_w))))
        height = top_section_height + panel_height

        image = Image.new("RGBA", (width, height), background_top)
        draw = ImageDraw.Draw(image)

        avatar_scaled = avatar.resize((panel_width, panel_height), Image.Resampling.LANCZOS)
        avatar_panel = Image.new("RGBA", (panel_width, panel_height), (0, 0, 0, 0))
        avatar_panel.paste(avatar_scaled, (0, 0), avatar_scaled)

        image.paste(avatar_panel, (0, top_section_height), avatar_panel)

        # Draw bubble after avatar: a clipped large oval plus a short seam tail.
        bubble_bottom_y = top_section_height - 70
        bubble_rect = (-120, -250, width + 120, bubble_bottom_y)
        draw.rounded_rectangle(
            bubble_rect,
            radius=260,
            fill=bubble_fill,
            outline=bubble_outline,
            width=4,
        )
        tail = [
            (int(width * 0.58), bubble_bottom_y),
            (int(width * 0.72), bubble_bottom_y),
            (int(width * 0.64), top_section_height - 4),
        ]
        draw.polygon(tail, fill=bubble_fill)
        draw.line([tail[0], tail[2]], fill=bubble_outline, width=4)
        draw.line([tail[2], tail[1]], fill=bubble_outline, width=4)
        # Clean edge artifact on the top-right corner.
        draw.rectangle((width - 2, 0, width - 1, top_section_height), fill=background_top)

        output = BytesIO()
        try:
            image.convert("RGB").save(output, format="PNG")
            output.seek(0)
            return discord.File(output, filename="speech_meme.png")
        except Exception:
            return None

    @commands.hybrid_group(
        name="meme",
        description="Meme commands.",
        invoke_without_command=True,
    )
    async def meme_group(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        lang = await self._lang(ctx.guild)
        await ctx.send(
            tr(
                lang,
                "Use `/meme help` to see meme subcommands.",
                "Usa `/meme help` para ver los subcomandos de memes.",
            )
        )

    @meme_group.command(
        name="templates",
        description="List available meme templates (optional query).",
    )
    async def memetemplates(self, ctx: commands.Context, *, query: str | None = None) -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        await self._defer_if_needed(ctx)

        try:
            templates = await client.list_templates()
        except Exception as exc:
            await ctx.send(
                tr(
                    lang,
                    f"Failed to load templates: {exc}",
                    f"No se pudieron cargar las plantillas: {exc}",
                )
            )
            return

        query_text = (query or "").strip().casefold()
        filtered = templates
        if query_text:
            filtered = []
            for item in templates:
                template_id = self._template_id(item).casefold()
                template_name = self._template_name(item).casefold()
                if query_text in template_id or query_text in template_name:
                    filtered.append(item)

        if not filtered:
            await ctx.send(
                tr(
                    lang,
                    "No templates matched your search.",
                    "No se encontraron plantillas para tu búsqueda.",
                )
            )
            return

        items_per_page = 20
        pages: list[discord.Embed] = []
        total_items = len(filtered)

        for start in range(0, total_items, items_per_page):
            chunk = filtered[start : start + items_per_page]
            lines = []
            for item in chunk:
                template_id = self._template_id(item)
                template_name = self._template_name(item)
                lines.append(f"`{template_id}` - {template_name}")

            end = start + len(chunk)
            embed = discord.Embed(
                title=tr(lang, "Meme Templates", "Plantillas de memes"),
                description="\n".join(lines)[:4000],
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc),
            )
            if query_text:
                embed.add_field(
                    name=tr(lang, "Search", "Busqueda"),
                    value=f"`{query_text}`",
                    inline=False,
                )
            pages.append(embed)

            page_index = len(pages)
            total_pages = (total_items + items_per_page - 1) // items_per_page
            embed.set_footer(
                text=tr(
                    lang,
                    f"Showing {start + 1}-{end} of {total_items} templates | Page {page_index}/{total_pages}",
                    f"Mostrando {start + 1}-{end} de {total_items} plantillas | Pagina {page_index}/{total_pages}",
                )
            )

        if len(pages) == 1:
            await ctx.send(embed=pages[0])
            return

        view = MemeHelpPaginatorView(
            pages=pages,
            author_id=ctx.author.id,
            lang=lang,
        )
        sent = await ctx.send(embed=pages[0], view=view)
        if isinstance(sent, discord.Message):
            view.message = sent

    @meme_group.command(
        name="help",
        description="Show meme command usage, structure, and examples.",
    )
    async def memehelp(self, ctx: commands.Context) -> None:
        lang = await self._lang(ctx.guild)
        await self._defer_if_needed(ctx)
        pages = self._memehelp_pages(lang)
        view = MemeHelpPaginatorView(
            pages=pages,
            author_id=ctx.author.id,
            lang=lang,
        )
        sent = await ctx.send(embed=pages[0], view=view)
        if isinstance(sent, discord.Message):
            view.message = sent

    @meme_group.command(
        name="create",
        description="Generate a meme with a template id.",
    )
    async def meme_create(
        self,
        ctx: commands.Context,
        template: str,
        top_text: str,
        *,
        bottom_text: str | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return

        template_input = template.strip()
        if not template_input:
            await ctx.send(tr(lang, "Template is required.", "La plantilla es obligatoria."))
            return

        await self._defer_if_needed(ctx)
        try:
            templates = await client.list_templates()
            selected = self._pick_template(templates, template_input)
            if selected is None:
                await ctx.send(
                    tr(
                        lang,
                        "Template not found. Use `/meme templates` to search.",
                        "Plantilla no encontrada. Usa `/meme templates` para buscar.",
                    )
                )
                return
            template_id = self._template_id(selected)
            image_url = client.build_template_url(
                template_id=template_id,
                top_text=top_text,
                bottom_text=bottom_text,
            )
        except Exception as exc:
            await ctx.send(
                tr(
                    lang,
                    f"Failed to build meme: {exc}",
                    f"No se pudo generar el meme: {exc}",
                )
            )
            return

        embed = discord.Embed(
            title=tr(lang, "Generated Meme", "Meme generado"),
            description=f"Template: `{template_id}`",
            color=discord.Color.random(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_image(url=image_url)
        sent = await ctx.send(embed=embed)
        await self._log_created_meme(
            guild=ctx.guild,
            lang=lang,
            actor=ctx.author,
            meme_type="meme",
            image_url=image_url,
            details=[
                (tr(lang, "Template", "Plantilla"), f"`{template_id}`"),
                (tr(lang, "Top text", "Texto arriba"), (top_text or "")[:1024] or tr(lang, "[empty]", "[vacio]")),
                (
                    tr(lang, "Bottom text", "Texto abajo"),
                    ((bottom_text or "").strip()[:1024] if bottom_text else tr(lang, "[empty]", "[vacio]")),
                ),
            ],
            jump_url=sent.jump_url if isinstance(sent, discord.Message) else None,
        )

    @meme_group.command(
        name="random",
        description="Generate a meme using a random template.",
    )
    async def memerandom(
        self,
        ctx: commands.Context,
        top_text: str,
        *,
        bottom_text: str | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return

        await self._defer_if_needed(ctx)
        try:
            templates = await client.list_templates()
            if not templates:
                raise RuntimeError("No templates available.")
            selected = random.choice(templates)
            template_id = self._template_id(selected)
            image_url = client.build_template_url(
                template_id=template_id,
                top_text=top_text,
                bottom_text=bottom_text,
            )
        except Exception as exc:
            await ctx.send(
                tr(
                    lang,
                    f"Failed to build random meme: {exc}",
                    f"No se pudo generar un meme aleatorio: {exc}",
                )
            )
            return

        embed = discord.Embed(
            title=tr(lang, "Random Meme", "Meme aleatorio"),
            description=f"Template: `{template_id}`",
            color=discord.Color.random(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_image(url=image_url)
        sent = await ctx.send(embed=embed)
        await self._log_created_meme(
            guild=ctx.guild,
            lang=lang,
            actor=ctx.author,
            meme_type="memerandom",
            image_url=image_url,
            details=[
                (tr(lang, "Template", "Plantilla"), f"`{template_id}`"),
                (tr(lang, "Top text", "Texto arriba"), (top_text or "")[:1024] or tr(lang, "[empty]", "[vacio]")),
                (
                    tr(lang, "Bottom text", "Texto abajo"),
                    ((bottom_text or "").strip()[:1024] if bottom_text else tr(lang, "[empty]", "[vacio]")),
                ),
            ],
            jump_url=sent.jump_url if isinstance(sent, discord.Message) else None,
        )

    @meme_group.command(
        name="custom",
        description="Generate a meme with a custom image URL or attachment.",
    )
    @discord.app_commands.describe(
        source="Image URL, or top text when attaching an image.",
        top_text="Top text in URL mode. With attachment mode, this is optional bottom text.",
        bottom_text="Optional extra bottom text.",
        image="Optional attached image for custom meme background.",
    )
    async def memecustom(
        self,
        ctx: commands.Context,
        source: str | None = None,
        top_text: str | None = None,
        *,
        bottom_text: str | None = None,
        image: discord.Attachment | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return

        source_input = (source or "").strip()
        source_text = self._normalize_meme_text(source_input) or ""
        source_is_url = bool(source_text) and self._looks_like_url(source_text)

        attachment_url: str | None = None
        if image is not None:
            if not self._is_supported_image_attachment(image):
                await ctx.send(
                    tr(
                        lang,
                        "Attachment must be an image file.",
                        "El archivo adjunto debe ser una imagen.",
                    )
                )
                return
            attachment_url = image.url

        if attachment_url is None:
            attachment_url = self._interaction_image_url(ctx.interaction)

        if attachment_url is None and ctx.message and ctx.message.attachments:
            for file in ctx.message.attachments:
                if self._is_supported_image_attachment(file):
                    attachment_url = file.url
                    break

        if source_is_url:
            background_url = source_text
            if top_text is None:
                await ctx.send(
                    tr(
                        lang,
                        "Top text is required when using an image URL.",
                        "El texto superior es obligatorio cuando usas una URL de imagen.",
                    )
                )
                return
            normalized_top = self._normalize_meme_text(top_text)
            normalized_bottom = self._normalize_meme_text(bottom_text)
        else:
            if attachment_url is None:
                await ctx.send(
                    tr(
                        lang,
                        "No image detected. Attach an image or provide a valid image URL.",
                        "No se detecto una imagen. Adjunta una imagen o proporciona una URL valida.",
                    )
                )
                return
            background_url = attachment_url
            source_was_explicit = source is not None
            if source_text:
                normalized_top = source_text
                if top_text and bottom_text:
                    normalized_bottom = self._normalize_meme_text(
                        f"{top_text.strip()} {bottom_text.strip()}".strip()
                    )
                else:
                    normalized_bottom = self._normalize_meme_text(top_text or bottom_text)
            elif source_was_explicit:
                normalized_top = ""
                if top_text and bottom_text:
                    normalized_bottom = self._normalize_meme_text(
                        f"{top_text.strip()} {bottom_text.strip()}".strip()
                    )
                else:
                    normalized_bottom = self._normalize_meme_text(top_text or bottom_text)
            else:
                normalized_top = self._normalize_meme_text(top_text)
                normalized_bottom = self._normalize_meme_text(bottom_text)

        await self._defer_if_needed(ctx)
        try:
            image_url = client.build_custom_url(
                background_url=background_url,
                top_text=normalized_top,
                bottom_text=normalized_bottom,
            )
        except Exception as exc:
            await ctx.send(
                tr(
                    lang,
                    f"Failed to build custom meme: {exc}",
                    f"No se pudo generar el meme personalizado: {exc}",
                )
            )
            return

        embed = discord.Embed(
            title=tr(lang, "Custom Meme", "Meme personalizado"),
            color=discord.Color.random(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_image(url=image_url)
        sent = await ctx.send(embed=embed)

        empty_label = tr(lang, "[empty]", "[vacio]")
        top_preview = empty_label if normalized_top == "" else (normalized_top or empty_label)
        details: list[tuple[str, str]] = [
            (
                tr(lang, "Source", "Origen"),
                tr(
                    lang,
                    "Image URL" if source_is_url else "Attached image",
                    "URL de imagen" if source_is_url else "Imagen adjunta",
                ),
            ),
            (tr(lang, "Top text", "Texto arriba"), top_preview[:1024]),
        ]
        if normalized_bottom is not None:
            bottom_preview = empty_label if normalized_bottom == "" else normalized_bottom
            details.append((tr(lang, "Bottom text", "Texto abajo"), bottom_preview[:1024]))

        await self._log_created_meme(
            guild=ctx.guild,
            lang=lang,
            actor=ctx.author,
            meme_type="memecustom",
            image_url=image_url,
            details=details,
            jump_url=sent.jump_url if isinstance(sent, discord.Message) else None,
        )

    @commands.hybrid_command(
        name="speech",
        description="Create a speech reaction meme from a user's avatar.",
    )
    @discord.app_commands.describe(
        user="User to use as the speech meme target.",
    )
    async def speech(
        self,
        ctx: commands.Context,
        user: discord.Member,
    ) -> None:
        lang = await self._lang(ctx.guild)
        await self._defer_if_needed(ctx)

        if Image is None or ImageDraw is None or ImageFont is None:
            await ctx.send(
                tr(
                    lang,
                    "Speech meme rendering is unavailable. Install `pillow` and restart the bot.",
                    "La generacion del meme speech no esta disponible. Instala `pillow` y reinicia el bot.",
                )
            )
            return

        speech_file = await self._build_speech_meme_file(user=user)
        if speech_file is None:
            await ctx.send(
                tr(
                    lang,
                    "I could not build the speech meme image right now.",
                    "No pude generar la imagen del meme speech en este momento.",
                )
            )
            return

        embed = discord.Embed(
            title=tr(lang, "Speech Meme", "Meme Speech"),
            description=tr(
                lang,
                f"Target: {user.mention}",
                f"Objetivo: {user.mention}",
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_image(url="attachment://speech_meme.png")
        sent = await ctx.send(
            embed=embed,
            file=speech_file,
            allowed_mentions=discord.AllowedMentions(users=True),
        )
        attachment_url: str | None = None
        if isinstance(sent, discord.Message) and sent.attachments:
            attachment_url = sent.attachments[0].url
        await self._log_created_meme(
            guild=ctx.guild,
            lang=lang,
            actor=ctx.author,
            meme_type="speech",
            image_url=attachment_url,
            details=[(tr(lang, "Target", "Objetivo"), user.mention)],
            jump_url=sent.jump_url if isinstance(sent, discord.Message) else None,
        )

    @meme_group.command(
        name="fonts",
        description="List available meme fonts.",
    )
    async def memefonts(self, ctx: commands.Context, *, query: str | None = None) -> None:
        lang = await self._lang(ctx.guild)
        client = await self._client_or_message(ctx, lang)
        if client is None:
            return
        await self._defer_if_needed(ctx)

        try:
            fonts = await client.list_fonts()
        except Exception as exc:
            await ctx.send(
                tr(
                    lang,
                    f"Failed to load fonts: {exc}",
                    f"No se pudieron cargar las fuentes: {exc}",
                )
            )
            return

        q = (query or "").strip().casefold()
        if q:
            fonts = [font for font in fonts if q in font.casefold()]

        if not fonts:
            await ctx.send(
                tr(
                    lang,
                    "No fonts matched your search.",
                    "No se encontraron fuentes para tu búsqueda.",
                )
            )
            return

        shown = fonts[:40]
        embed = discord.Embed(
            title=tr(lang, "Meme Fonts", "Fuentes para memes"),
            description=", ".join(f"`{font}`" for font in shown)[:4000],
            color=discord.Color.teal(),
            timestamp=datetime.now(timezone.utc),
        )
        if len(fonts) > len(shown):
            embed.set_footer(
                text=tr(
                    lang,
                    f"Showing {len(shown)} of {len(fonts)} fonts.",
                    f"Mostrando {len(shown)} de {len(fonts)} fuentes.",
                )
            )
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MemesCog(bot))
