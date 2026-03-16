from __future__ import annotations

import io
import os
import re
import shlex
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from utils.i18n import tr


LANGUAGE_ALIASES: dict[str, tuple[str, str]] = {
    "c": ("c", "main.c"),
    "c#": ("csharp", "Program.cs"),
    "csharp": ("csharp", "Program.cs"),
    "cs": ("csharp", "Program.cs"),
    "cpp": ("cpp", "main.cpp"),
    "c++": ("cpp", "main.cpp"),
    "cplusplus": ("cpp", "main.cpp"),
    "cxx": ("cpp", "main.cpp"),
    "cpp23": ("cpp", "main.cpp"),
    "c++23": ("cpp", "main.cpp"),
    "cplusplus23": ("cpp", "main.cpp"),
    "cxx23": ("cpp", "main.cpp"),
    "python": ("python", "main.py"),
    "py": ("python", "main.py"),
    "python3": ("python", "main.py"),
    "py3": ("python", "main.py"),
    "java": ("java", "Main.java"),
    "javascript": ("javascript", "main.js"),
    "js": ("javascript", "main.js"),
    "node": ("javascript", "main.js"),
    "rust": ("rust", "main.rs"),
    "rs": ("rust", "main.rs"),
}

LANGUAGE_CHOICES = [
    "c",
    "c#",
    "cpp",
    "python",
    "java",
    "javascript",
    "rust",
]

FILE_EXTENSION_MAP: dict[str, tuple[str, str]] = {
    ".c": ("c", "main.c"),
    ".cs": ("csharp", "Program.cs"),
    ".cpp": ("cpp", "main.cpp"),
    ".java": ("java", "Main.java"),
    ".js": ("javascript", "main.js"),
    ".py": ("python", "main.py"),
    ".rs": ("rust", "main.rs"),
}

LANGUAGE_LOGOS: dict[str, str] = {
    "c": "https://raw.githubusercontent.com/github/explore/main/topics/c/c.png",
    "csharp": "https://raw.githubusercontent.com/github/explore/main/topics/csharp/csharp.png",
    "cpp": "https://raw.githubusercontent.com/github/explore/main/topics/cpp/cpp.png",
    "python": "https://raw.githubusercontent.com/github/explore/main/topics/python/python.png",
    "java": "https://raw.githubusercontent.com/github/explore/main/topics/java/java.png",
    "javascript": "https://raw.githubusercontent.com/github/explore/main/topics/javascript/javascript.png",
    "rust": "https://raw.githubusercontent.com/github/explore/main/topics/rust/rust.png",
}

LANGUAGE_DISPLAY: dict[str, str] = {
    "c": "C",
    "csharp": "C#",
    "cpp": "C++",
    "python": "Python",
    "java": "Java",
    "javascript": "JavaScript",
    "rust": "Rust",
}


class CodeRunnerCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _lang(self, guild: discord.Guild | None) -> str:
        if guild is None:
            return "en"
        settings = await self.bot.db.get_guild_settings(guild.id)
        return settings.language_code

    async def _client_or_message(self, ctx: commands.Context) -> Any | None:
        lang = await self._lang(ctx.guild)
        client = getattr(self.bot, "glot_client", None)
        if client is not None:
            return client
        await ctx.send(
            tr(
                lang,
                "Glot is not configured. Add `GLOT_API_TOKEN` in `.env`.",
                "Glot no esta configurado. Agrega `GLOT_API_TOKEN` en `.env`.",
            )
        )
        return None

    @staticmethod
    def _resolve_language(raw: str) -> tuple[str, str] | None:
        key = raw.strip().lower()
        if not key:
            return None
        resolved = LANGUAGE_ALIASES.get(key)
        if resolved is not None:
            return resolved
        compact_key = re.sub(r"[\s_\-]+", "", key)
        return LANGUAGE_ALIASES.get(compact_key)

    @staticmethod
    def _preferred_run_command(language_slug: str, filename: str) -> str | None:
        # Try modern standard first for C++; fallback is handled in command execution.
        if language_slug != "cpp":
            return None
        safe_source = shlex.quote(filename or "main.cpp")
        return f"g++ -std=c++23 {safe_source} -O2 -pipe -o main && ./main"

    @staticmethod
    def _needs_cpp_runtime_fallback(result: dict[str, Any]) -> bool:
        stderr = str(result.get("stderr", "") or "").lower()
        error = str(result.get("error", "") or "").lower()
        combined = f"{stderr}\n{error}"
        return (
            "g++: command not found" in combined
            or "exit code: 127" in combined
            or "exit status 127" in combined
        )

    @staticmethod
    def _strip_code_fence(raw_code: str) -> str:
        code, _ = CodeRunnerCog._parse_code_input(raw_code)
        return code

    @staticmethod
    def _parse_code_input(raw_code: str) -> tuple[str, str | None]:
        code = raw_code.strip()
        if not code.startswith("```"):
            return code, None
        inner = code[3:]
        detected_language: str | None = None
        if inner.endswith("```"):
            inner = inner[:-3]
        if "\n" in inner:
            first_line, rest = inner.split("\n", 1)
            maybe_lang = first_line.strip().lower()
            if re.fullmatch(r"[a-z0-9_+#-]{1,20}", maybe_lang):
                detected_language = maybe_lang
                code = rest
            else:
                code = f"{first_line}\n{rest}"
        else:
            single_line = inner.strip()
            parts = single_line.split(maxsplit=1)
            if len(parts) == 2 and re.fullmatch(r"[a-z0-9_+#-]{1,20}", parts[0].strip().lower()):
                detected_language = parts[0].strip().lower()
                code = parts[1]
            else:
                code = single_line
        return code.strip("\n"), detected_language

    @staticmethod
    def _truncate(value: str, limit: int = 1400) -> str:
        if len(value) <= limit:
            return value
        return f"{value[:limit]}..."

    @staticmethod
    def _resolve_from_extension(filename: str) -> tuple[str, str] | None:
        _, ext = os.path.splitext((filename or "").strip().lower())
        resolved = FILE_EXTENSION_MAP.get(ext)
        if resolved is None:
            return None
        language_slug, default_filename = resolved
        original_name = (filename or "").strip()
        safe_name = original_name if original_name else default_filename
        return language_slug, safe_name

    async def _resolve_attachment_input(
        self,
        *,
        attachment: discord.Attachment,
        lang: str,
    ) -> tuple[str, str, str]:
        by_ext = self._resolve_from_extension(attachment.filename or "")
        if by_ext is None:
            raise RuntimeError(self._unsupported_extension_message(lang, attachment.filename or ""))

        language_slug, filename = by_ext
        if attachment.size > 180_000:
            raise RuntimeError(
                tr(
                    lang,
                    "Source file is too large (max 180KB).",
                    "El archivo fuente es demasiado grande (max 180KB).",
                )
            )

        raw = await attachment.read()
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            raise RuntimeError(
                tr(
                    lang,
                    "The attached source file is empty.",
                    "El archivo fuente adjunto esta vacio.",
                )
            )
        return language_slug, filename, text

    @staticmethod
    def _unsupported_extension_message(lang: str, filename: str = "") -> str:
        detail = f" `{filename}`." if filename else "."
        return tr(
            lang,
            "Unsupported source file extension" + detail
            + " Allowed: `.c`, `.cpp`, `.cs`, `.java`, `.js`, `.py`, `.rs`.",
            "Extension de archivo fuente no soportada" + detail
            + " Permitidas: `.c`, `.cpp`, `.cs`, `.java`, `.js`, `.py`, `.rs`.",
        )

    async def _defer_if_needed(self, ctx: commands.Context) -> None:
        if ctx.interaction is None:
            return
        if ctx.interaction.response.is_done():
            return
        try:
            await ctx.defer()
        except (discord.NotFound, discord.HTTPException):
            return

    def _normalize_prefix_inputs(
        self,
        *,
        ctx: commands.Context,
        code: str | None,
        language: str | None,
        has_attachment: bool,
    ) -> tuple[str | None, str | None]:
        if ctx.interaction is not None or ctx.message is None:
            return code, language

        content = (ctx.message.content or "").strip()
        if not content:
            return code, language

        prefix = str(getattr(ctx, "prefix", "") or "")
        invoked = str(getattr(ctx, "invoked_with", "code") or "code")
        head = f"{prefix}{invoked}".strip()
        remainder = content
        if head and content.lower().startswith(head.lower()):
            remainder = content[len(head) :].strip()
        else:
            parts = content.split(maxsplit=1)
            remainder = parts[1].strip() if len(parts) > 1 else ""

        if not remainder:
            return code, language

        # For prefix commands, always prefer the raw remainder when it's a fenced block.
        # Discord argument parsing can split multiline fenced text into broken args.
        if not has_attachment and remainder.startswith("```"):
            fenced_code, fenced_language = self._parse_code_input(remainder)
            return fenced_code, (fenced_language or language)

        parts = remainder.split(maxsplit=1)
        candidate_language = parts[0].strip()
        if not self._resolve_language(candidate_language):
            return code, language

        if has_attachment:
            return code, candidate_language

        candidate_code = parts[1].strip() if len(parts) > 1 else ""
        if not candidate_code:
            return code, candidate_language
        return candidate_code, candidate_language

    @commands.hybrid_command(
        name="code",
        description="Compile/run code snippet: c, c#, cpp, python, java, javascript, rust.",
    )
    @app_commands.describe(
        code="Source code (plain text or ```fenced```)",
        language="Language: c, c#, cpp, python, java, javascript, rust",
        source_file="Optional source file (.c, .cpp, .cs, .java, .js, .py, .rs)",
    )
    @app_commands.choices(
        language=[
            app_commands.Choice(name="c", value="c"),
            app_commands.Choice(name="c#", value="c#"),
            app_commands.Choice(name="cpp", value="cpp"),
            app_commands.Choice(name="python", value="python"),
            app_commands.Choice(name="java", value="java"),
            app_commands.Choice(name="javascript", value="javascript"),
            app_commands.Choice(name="rust", value="rust"),
        ]
    )
    async def code_cmd(
        self,
        ctx: commands.Context,
        code: str | None = None,
        language: str | None = None,
        source_file: discord.Attachment | None = None,
    ) -> None:
        client = await self._client_or_message(ctx)
        if client is None:
            return
        lang = await self._lang(ctx.guild)

        attachment = source_file
        if attachment is None and ctx.message and ctx.message.attachments:
            attachment = ctx.message.attachments[0]
        code, language = self._normalize_prefix_inputs(
            ctx=ctx,
            code=code,
            language=language,
            has_attachment=attachment is not None,
        )

        cleaned_code = ""
        language_slug = ""
        filename = ""

        if attachment is not None:
            try:
                resolved_attachment = await self._resolve_attachment_input(
                    attachment=attachment,
                    lang=lang,
                )
            except Exception as exc:
                await ctx.send(str(exc))
                return
            language_slug, filename, cleaned_code = resolved_attachment

            if language:
                resolved_lang = self._resolve_language(language)
                if resolved_lang is None:
                    await ctx.send(
                        tr(
                            lang,
                            "Invalid language. Allowed: c, c#, cpp, python, java, javascript, rust.",
                            "Lenguaje invalido. Permitidos: c, c#, cpp, python, java, javascript, rust.",
                        )
                    )
                    return
                language_slug = resolved_lang[0]
        else:
            parsed_code, fenced_language = self._parse_code_input(code or "")
            resolved = self._resolve_language(language or "")
            if resolved is None and fenced_language:
                resolved = self._resolve_language(fenced_language)
            if resolved is None:
                await ctx.send(
                    tr(
                        lang,
                        "Select language first, upload a valid source file, or use a fenced block like ```python ...```.",
                        "Selecciona primero el lenguaje, sube un archivo fuente valido, o usa bloque con lenguaje como ```python ...```.",
                    )
                )
                return
            language_slug, filename = resolved
            cleaned_code = parsed_code.strip()
            if not cleaned_code:
                await ctx.send(
                    tr(
                        lang,
                        "Please provide code to run.",
                        "Por favor proporciona codigo para ejecutar.",
                    )
                )
                return

        if len(cleaned_code) > 12000:
            await ctx.send(
                tr(
                    lang,
                    "Code is too long (max 12,000 chars).",
                    "El codigo es demasiado largo (maximo 12,000 caracteres).",
                )
            )
            return

        await self._defer_if_needed(ctx)
        preferred_command = self._preferred_run_command(language_slug, filename)
        used_cpp_fallback = False
        try:
            result = await client.run_code(
                language=language_slug,
                code=cleaned_code,
                filename=filename,
                command=preferred_command,
            )
        except Exception as exc:
            if language_slug == "cpp" and preferred_command:
                try:
                    result = await client.run_code(
                        language=language_slug,
                        code=cleaned_code,
                        filename=filename,
                    )
                    used_cpp_fallback = True
                except Exception:
                    await ctx.send(
                        tr(
                            lang,
                            f"Run failed: {exc}",
                            f"La ejecucion fallo: {exc}",
                        )
                    )
                    return
            else:
                await ctx.send(
                    tr(
                        lang,
                        f"Run failed: {exc}",
                        f"La ejecucion fallo: {exc}",
                    )
                )
                return

        if language_slug == "cpp" and preferred_command and self._needs_cpp_runtime_fallback(result):
            try:
                result = await client.run_code(
                    language=language_slug,
                    code=cleaned_code,
                    filename=filename,
                )
                used_cpp_fallback = True
            except Exception:
                # Keep original result so user sees the underlying runtime error.
                pass

        stdout = str(result.get("stdout", "") or "")
        stderr = str(result.get("stderr", "") or "")
        error = str(result.get("error", "") or "")

        if error.strip():
            embed_color = discord.Color.red()
        elif stderr.strip():
            embed_color = discord.Color.yellow()
        else:
            embed_color = discord.Color.green()

        embed = discord.Embed(
            title=tr(lang, "Code", "Código"),
            color=embed_color,
        )
        logo_url = LANGUAGE_LOGOS.get(language_slug)
        if logo_url:
            embed.set_thumbnail(url=logo_url)
        language_display = LANGUAGE_DISPLAY.get(language_slug, language_slug)
        embed.add_field(name=tr(lang, "Language", "Lenguaje"), value=f"`{language_display}`", inline=True)
        if used_cpp_fallback:
            embed.add_field(
                name=tr(lang, "Runtime", "Runtime"),
                value=tr(lang, "Default C++ runtime (fallback)", "Runtime C++ por defecto (fallback)"),
                inline=True,
            )

        no_output = not stdout.strip() and not stderr.strip() and not error.strip()
        if no_output:
            embed.description = tr(
                lang,
                "Program finished with no output.",
                "El programa termino sin salida.",
            )
            await ctx.send(embed=embed)
            return

        content_parts: list[str] = []
        if stdout.strip():
            content_parts.append(f"STDOUT\n{stdout}")
            embed.add_field(
                name=tr(lang, "Result", "Resultado"),
                value=f"```txt\n{self._truncate(stdout)}\n```",
                inline=False,
            )
        if stderr.strip():
            content_parts.append(f"STDERR\n{stderr}")
            embed.add_field(
                name=tr(lang, "Error location", "Ubicación de error"),
                value=f"```txt\n{self._truncate(stderr)}\n```",
                inline=False,
            )
        if error.strip():
            content_parts.append(f"ERROR\n{error}")
            embed.add_field(
                name="ERROR",
                value=f"```txt\n{self._truncate(error)}\n```",
                inline=False,
            )

        full_output = "\n\n".join(content_parts)
        if len(full_output) > 3500:
            output_file = discord.File(
                io.BytesIO(full_output.encode("utf-8")),
                filename="run-output.txt",
            )
            await ctx.send(
                tr(
                    lang,
                    "Output was too long, sending as file.",
                    "La salida fue muy larga, la envio como archivo.",
                ),
                embed=embed,
                file=output_file,
            )
            return

        await ctx.send(embed=embed)

    @code_cmd.autocomplete("language")
    async def run_language_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        needle = (current or "").strip().lower()
        options = LANGUAGE_CHOICES
        if needle:
            options = [item for item in LANGUAGE_CHOICES if needle in item.lower()]
        return [app_commands.Choice(name=item, value=item) for item in options[:25]]

    @commands.hybrid_command(
        name="codelangs",
        aliases=["runlangs"],
        description="List supported languages for /code.",
    )
    async def code_languages(self, ctx: commands.Context) -> None:
        lang = await self._lang(ctx.guild)
        await ctx.send(
            tr(
                lang,
                "Supported for /code: " + ", ".join(f"`{name}`" for name in LANGUAGE_CHOICES)
                + " | Files: `.c`, `.cpp`, `.cs`, `.java`, `.js`, `.py`, `.rs`"
                + " | `cpp` tries C++23 first.",
                "Soportados para /code: " + ", ".join(f"`{name}`" for name in LANGUAGE_CHOICES)
                + " | Archivos: `.c`, `.cpp`, `.cs`, `.java`, `.js`, `.py`, `.rs`"
                + " | `cpp` intenta C++23 primero.",
            )
        )

    @code_cmd.error
    @code_languages.error
    async def code_runner_error_handler(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        lang = await self._lang(ctx.guild)
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                tr(
                    lang,
                    "Usage: `/code code:<code> language:<language> [source_file]` or `<prefix>code <language> <code>`",
                    "Uso: `/code code:<codigo> language:<lenguaje> [archivo_fuente]` o `<prefijo>code <lenguaje> <codigo>`",
                )
            )
            return
        await ctx.send(
            tr(
                lang,
                f"Command failed: {error}",
                f"El comando fallo: {error}",
            )
        )

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CodeRunnerCog(bot))
