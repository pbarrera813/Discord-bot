from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import re

import discord
from discord.ext import commands, tasks

from services.database import AI_INTERACTIONS_CONTEXT_CHANNEL_ID
from services.server_memory import ServerMemoryInput, ServerMemoryService
from services.voice_messages import ALLOWED_TTS_TAGS
from utils.i18n import normalize_language, tr
from utils.permissions import owner_or_has_permissions

HELP_SECTION_ALIASES: dict[str, str] = {
    "basic": "basic",
    "home": "basic",
    "start": "basic",
    "basico": "basic",
    "basica": "basic",
    "list": "sections",
    "sections": "sections",
    "index": "sections",
    "indice": "sections",
    "secciones": "sections",
    "general": "general",
    "birthday": "birthday",
    "birthdays": "birthday",
    "cumple": "birthday",
    "cumples": "birthday",
    "cumpleanos": "birthday",
    "cumpleaños": "birthday",
    "ai": "general",
    "ia": "general",
    "chat": "general",
    "voice": "voice",
    "voz": "voice",
    "audio": "voice",
    "coding": "coding",
    "code": "coding",
    "programming": "coding",
    "programacion": "coding",
    "sports": "sports",
    "sport": "sports",
    "deportes": "sports",
    "liga": "sports",
    "ligamx": "sports",
    "fun": "fun",
    "diversion": "fun",
    "utility": "utility",
    "utilities": "utility",
    "utilidad": "utility",
    "utilidades": "utility",
    "moderation": "moderation",
    "mod": "moderation",
    "moderacion": "moderation",
    "admin": "admin",
    "announcement": "announcements",
    "announcements": "announcements",
    "welcome": "announcements",
    "goodbye": "announcements",
    "anuncios": "announcements",
    "variables": "variables",
    "variable": "variables",
    "vars": "variables",
}

HELP_SECTION_LABELS_EN: dict[str, str] = {
    "basic": "basic",
    "sections": "sections",
    "general": "general",
    "voice": "voice",
    "birthday": "birthday",
    "coding": "coding",
    "sports": "sports",
    "fun": "fun",
    "utility": "utility",
    "moderation": "moderation",
    "admin": "admin",
    "announcements": "announcements",
    "variables": "variables",
}

HELP_SECTION_LABELS_ES: dict[str, str] = {
    "basic": "basico",
    "sections": "secciones",
    "general": "general",
    "voice": "voz",
    "birthday": "cumpleaños",
    "coding": "programacion",
    "sports": "deportes",
    "fun": "diversion",
    "utility": "utilidad",
    "moderation": "moderacion",
    "admin": "admin",
    "announcements": "anuncios",
    "variables": "variables",
}

@dataclass(frozen=True)
class HelpCommandSpec:
    path: str
    section: str
    usage_en: str
    usage_es: str
    description_en: str
    description_es: str
    access: str = "everyone"
    aliases: tuple[str, ...] = ()
    material_options: tuple[str, ...] = ()
    material_choices: tuple[str, ...] = ()
    show_to_all: bool = False


@dataclass(frozen=True)
class HelpCapabilitySpec:
    key: str
    section: str
    title_en: str
    title_es: str
    body_en: str
    body_es: str
    access: str = "everyone"


HELP_ACCESS_EN: dict[str, str] = {
    "everyone": "",
    "manage_messages": "Requires Manage Messages.",
    "moderator": "Requires moderator permissions.",
    "manage_guild": "Requires Manage Server or Administrator.",
    "administrator": "Requires Administrator.",
}

HELP_ACCESS_ES: dict[str, str] = {
    "everyone": "",
    "manage_messages": "Requiere Gestionar mensajes.",
    "moderator": "Requiere permisos de moderación.",
    "manage_guild": "Requiere Gestionar servidor o Administrador.",
    "administrator": "Requiere Administrador.",
}

HELP_INTENTIONAL_EXCLUSIONS: dict[str, str] = {}


HELP_COMMANDS: tuple[HelpCommandSpec, ...] = (
    HelpCommandSpec("help", "general", "/help [section]", "/help [sección]", "Open this paginated guide.", "Abre esta guía por páginas."),
    HelpCommandSpec("setup", "general", "/setup", "/setup", "Show setup and capability summary.", "Muestra resumen de configuración y capacidades."),
    HelpCommandSpec("translate", "general", "/translate <language> [text]", "/translate <idioma> [texto]", "Translate provided or replied text.", "Traduce texto escrito o respondido."),
    HelpCommandSpec("roast", "fun", "/roast <user|id|name>", "/roast <usuario|id|nombre>", "Playful AI roast using server vibe.", "Roast juguetón con la vibra del servidor."),
    HelpCommandSpec("roastme", "fun", "/roastme", "/roastme", "Roast yourself.", "Roast para ti."),
    HelpCommandSpec("say", "voice", "/say mensaje:<text> modo:<text|voice>", "/say mensaje:<texto> modo:<text|voice>", "Make Nitori send text or a native voice message.", "Haz que Nitori mande texto o mensaje de voz nativo.", access="manage_messages", material_options=("mensaje", "modo"), material_choices=("modo=text|voice",), show_to_all=True),
    HelpCommandSpec("remindme", "fun", "/remindme <time> <message>", "/remindme <tiempo> <mensaje>", "Create a reminder. Units: m, h, d, w, mo, y.", "Crea un recordatorio. Unidades: m, h, d, w, mo, y."),
    HelpCommandSpec("unremindme", "fun", "/unremindme <reminder>", "/unremindme <recordatorio>", "Remove one of your reminders.", "Elimina uno de tus recordatorios."),
    HelpCommandSpec("srvstatus", "utility", "/srvstatus <ip-or-domain>", "/srvstatus <ip-o-dominio>", "Check Minecraft server status.", "Revisa el estado de un servidor de Minecraft."),
    HelpCommandSpec("joke", "fun", "/joke", "/joke", "Random joke.", "Chiste aleatorio."),
    HelpCommandSpec("dadjoke", "fun", "/dadjoke", "/dadjoke", "Random dad joke.", "Chiste de papá aleatorio."),
    HelpCommandSpec("advice", "fun", "/advice", "/advice", "Random advice.", "Consejo aleatorio."),
    HelpCommandSpec("whois", "utility", "/whois <domain>", "/whois <dominio>", "Domain WHOIS info.", "Información WHOIS de dominio."),
    HelpCommandSpec("convert", "utility", "/convert <amount> <from> <to>", "/convert <cantidad> <desde> <hacia>", "Unit conversion.", "Conversión de unidades.", material_options=("from", "to")),
    HelpCommandSpec("code", "coding", "/code code:<code> language:<language> [file]", "/code code:<código> language:<lenguaje> [file]", "Compile/run c, c#, cpp, python, java, javascript, or rust.", "Compila/ejecuta c, c#, cpp, python, java, javascript o rust.", material_options=("code", "language", "file"), material_choices=("c", "c#", "cpp", "python", "java", "javascript", "rust")),
    HelpCommandSpec("codelangs", "coding", "/codelangs", "/codelangs", "List supported /code languages.", "Lista lenguajes soportados por /code.", aliases=("runlangs",)),
    HelpCommandSpec("meme", "fun", "/meme", "/meme", "Show meme command prompt.", "Muestra ayuda rápida de memes."),
    HelpCommandSpec("meme templates", "fun", "/meme templates [query]", "/meme templates [búsqueda]", "List meme templates.", "Lista plantillas de meme."),
    HelpCommandSpec("meme help", "fun", "/meme help", "/meme help", "Detailed meme guide.", "Guía detallada de memes."),
    HelpCommandSpec("meme create", "fun", "/meme create <template> <top> [bottom]", "/meme create <plantilla> <arriba> [abajo]", "Create a template meme.", "Crea un meme con plantilla."),
    HelpCommandSpec("meme random", "fun", "/meme random <top> [bottom]", "/meme random <arriba> [abajo]", "Create a random-template meme.", "Crea un meme con plantilla aleatoria."),
    HelpCommandSpec("meme custom", "fun", "/meme custom <image_url|attachment> <top> [bottom]", "/meme custom <url|adjunto> <arriba> [abajo]", "Create a meme from URL or attachment.", "Crea un meme desde URL o adjunto."),
    HelpCommandSpec("meme fonts", "fun", "/meme fonts [query]", "/meme fonts [búsqueda]", "List meme fonts.", "Lista fuentes de meme."),
    HelpCommandSpec("speech", "fun", "/speech <user>", "/speech <usuario>", "Speech bubble avatar meme.", "Meme de burbuja de diálogo."),
    HelpCommandSpec("football", "sports", "/football <league>", "/football <liga>", "Live football fallback for a selected league.", "Atajo de partidos en vivo para una liga."),
    HelpCommandSpec("football live", "sports", "/football live <league>", "/football live <liga>", "Live matches now.", "Partidos en vivo ahora."),
    HelpCommandSpec("football today", "sports", "/football today <league>", "/football today <liga>", "Today's fixtures.", "Partidos de hoy."),
    HelpCommandSpec("football next", "sports", "/football next <league> [count|team]", "/football next <liga> [cantidad|equipo]", "Next fixtures or next team match.", "Próximos partidos o próximo partido de equipo."),
    HelpCommandSpec("football last", "sports", "/football last <league> <team>", "/football last <liga> <equipo>", "Last played team match.", "Último partido jugado de un equipo."),
    HelpCommandSpec("football table", "sports", "/football table <league>", "/football table <liga>", "League standings.", "Tabla de posiciones."),
    HelpCommandSpec("football team", "sports", "/football team <league> <team>", "/football team <liga> <equipo>", "Team snapshot and form.", "Datos del equipo y forma reciente."),
    HelpCommandSpec("football scorers", "sports", "/football scorers <league>", "/football scorers <liga>", "Top scorers.", "Tabla de goleadores."),
    HelpCommandSpec("football match", "sports", "/football match <fixture_id|team>", "/football match <fixture_id|equipo>", "Match center by fixture ID or team.", "Centro de partido por ID o equipo."),
    HelpCommandSpec("football schedule", "sports", "/football schedule <team|league> [next|last|season]", "/football schedule <equipo|liga> [next|last|season]", "Fixture schedule.", "Calendario de partidos."),
    HelpCommandSpec("football player", "sports", "/football player <player>", "/football player <jugador>", "Player profile and season stats.", "Perfil y estadísticas de jugador."),
    HelpCommandSpec("football lineup", "sports", "/football lineup <fixture_id>", "/football lineup <fixture_id>", "Confirmed fixture lineups.", "Alineaciones confirmadas."),
    HelpCommandSpec("football stats", "sports", "/football stats <fixture_id>", "/football stats <fixture_id>", "Fixture statistics.", "Estadísticas del partido."),
    HelpCommandSpec("football injuries", "sports", "/football injuries <team>", "/football injuries <equipo>", "Injuries/unavailable players.", "Lesiones/jugadores no disponibles."),
    HelpCommandSpec("football transfers", "sports", "/football transfers <team>", "/football transfers <equipo>", "Recent transfers.", "Transferencias recientes."),
    HelpCommandSpec("football h2h", "sports", "/football h2h <team_a> <team_b>", "/football h2h <equipo_a> <equipo_b>", "Head-to-head matches.", "Historial entre equipos."),
    HelpCommandSpec("football top", "sports", "/football top <scorers|assists|yellowcards|redcards>", "/football top <scorers|assists|yellowcards|redcards>", "Top scorers, assists, yellow cards, or red cards.", "Líderes de goles, asistencias, amarillas o rojas.", material_choices=("scorers", "assists", "yellowcards", "redcards")),
    HelpCommandSpec("football preview", "sports", "/football preview <fixture_id>", "/football preview <fixture_id>", "Data-only match preview.", "Previa del partido con datos."),
    HelpCommandSpec("football summary", "sports", "/football summary <fixture_id>", "/football summary <fixture_id>", "Data-only match summary.", "Resumen del partido con datos."),
    HelpCommandSpec("birthday", "birthday", "/birthday", "/birthday", "Birthday command prompt.", "Ayuda rápida de cumpleaños."),
    HelpCommandSpec("birthday set", "birthday", "/birthday set <MM-DD|DD/MM> [year]", "/birthday set <MM-DD|DD/MM> [año]", "Save your birthday.", "Guarda tu cumpleaños."),
    HelpCommandSpec("birthday remove", "birthday", "/birthday remove", "/birthday remove", "Remove your birthday data.", "Elimina tus datos de cumpleaños."),
    HelpCommandSpec("birthday cleardata", "birthday", "/birthday cleardata", "/birthday cleardata", "Alias of /birthday remove.", "Alias de /birthday remove."),
    HelpCommandSpec("birthday view", "birthday", "/birthday view [user]", "/birthday view [usuario]", "View birthday info.", "Consulta cumpleaños."),
    HelpCommandSpec("birthday next", "birthday", "/birthday next [count]", "/birthday next [cantidad]", "Upcoming birthdays.", "Próximos cumpleaños."),
    HelpCommandSpec("birthday setup", "birthday", "/birthday setup [channel] [role]", "/birthday setup [canal] [rol]", "Quick server birthday setup.", "Configuración rápida de cumpleaños.", access="administrator"),
    HelpCommandSpec("birthday channel", "birthday", "/birthday channel [#channel]", "/birthday channel [#canal]", "Set/clear birthday channel.", "Define/limpia canal de cumpleaños.", access="administrator"),
    HelpCommandSpec("birthday role", "birthday", "/birthday role [@role]", "/birthday role [@rol]", "Set/clear birthday role.", "Define/limpia rol de cumpleaños.", access="administrator"),
    HelpCommandSpec("birthday timezone", "birthday", "/birthday timezone <iana_tz>", "/birthday timezone <zona_iana>", "Set server birthday timezone.", "Define zona horaria de cumpleaños.", access="administrator"),
    HelpCommandSpec("birthday mode", "birthday", "/birthday mode <user|server>", "/birthday mode <user|server>", "Choose birthday timezone mode.", "Elige modo de zona horaria.", access="administrator", material_choices=("user", "server")),
    HelpCommandSpec("birthday ages", "birthday", "/birthday ages <true|false>", "/birthday ages <true|false>", "Show or hide ages.", "Muestra u oculta edades.", access="administrator"),
    HelpCommandSpec("birthday event", "birthday", "/birthday event <default|year|join|server|disable> [color] [image] [message]", "/birthday event <default|year|join|server|disable> [color] [image] [message]", "Configure birthday/anniversary events.", "Configura eventos de cumpleaños/aniversario.", access="administrator"),
    HelpCommandSpec("birthday preview", "birthday", "/birthday preview <default|year|server|user>", "/birthday preview <default|year|server|user>", "Preview birthday event output.", "Vista previa del evento.", access="administrator"),
    HelpCommandSpec("birthday templateadd", "birthday", "/birthday templateadd <type> <template>", "/birthday templateadd <tipo> <plantilla>", "Add custom event template.", "Agrega plantilla personalizada.", access="administrator"),
    HelpCommandSpec("birthday templatelist", "birthday", "/birthday templatelist <type>", "/birthday templatelist <tipo>", "List custom templates.", "Lista plantillas personalizadas.", access="administrator"),
    HelpCommandSpec("birthday templateremove", "birthday", "/birthday templateremove <type> <id>", "/birthday templateremove <tipo> <id>", "Remove custom template.", "Elimina plantilla personalizada.", access="administrator"),
    HelpCommandSpec("birthday blacklistuser", "birthday", "/birthday blacklistuser <user> <true|false>", "/birthday blacklistuser <usuario> <true|false>", "Exclude/include a user.", "Excluye/incluye usuario.", access="administrator"),
    HelpCommandSpec("birthday blacklistrole", "birthday", "/birthday blacklistrole <role> <true|false>", "/birthday blacklistrole <rol> <true|false>", "Exclude/include a role.", "Excluye/incluye rol.", access="administrator"),
    HelpCommandSpec("birthday trusted", "birthday", "/birthday trusted [role] [blockmessage] [blockrole] [blocklist]", "/birthday trusted [rol] [blockmessage] [blockrole] [blocklist]", "Trusted-role restrictions.", "Restricciones por rol confiable.", access="administrator"),
    HelpCommandSpec("message", "moderation", "/message", "/message", "Message moderation prompt.", "Ayuda rápida de moderación de mensajes.", access="moderator"),
    HelpCommandSpec("message delete", "moderation", "/message delete <amount>", "/message delete <cantidad>", "Delete recent messages.", "Borra mensajes recientes.", access="manage_messages"),
    HelpCommandSpec("message clear", "moderation", "/message clear [#channel]", "/message clear [#canal]", "Clear messages in a channel.", "Limpia mensajes de un canal.", access="moderator"),
    HelpCommandSpec("message purgeuser", "moderation", "/message purgeuser <user> <amount>", "/message purgeuser <usuario> <cantidad>", "Delete messages from one user.", "Borra mensajes de un usuario.", access="manage_messages"),
    HelpCommandSpec("channel", "moderation", "/channel", "/channel", "Channel management prompt.", "Ayuda rápida de canales.", access="moderator"),
    HelpCommandSpec("channel add", "moderation", "/channel add <name>", "/channel add <nombre>", "Create a text channel.", "Crea un canal de texto.", access="moderator"),
    HelpCommandSpec("channel delete", "moderation", "/channel delete <#channel>", "/channel delete <#canal>", "Delete a text channel.", "Elimina un canal.", access="moderator"),
    HelpCommandSpec("channel clear", "moderation", "/channel clear [#channel]", "/channel clear [#canal]", "Clear messages in a channel.", "Limpia mensajes de un canal.", access="moderator"),
    HelpCommandSpec("channel clone", "moderation", "/channel clone [#channel]", "/channel clone [#canal]", "Clone channel settings.", "Clona configuración del canal.", access="moderator"),
    HelpCommandSpec("channel lock", "moderation", "/channel lock [#channel]", "/channel lock [#canal]", "Lock posting permissions.", "Bloquea envío de mensajes.", access="moderator"),
    HelpCommandSpec("channel unlock", "moderation", "/channel unlock [#channel]", "/channel unlock [#canal]", "Restore posting permissions.", "Restaura envío de mensajes.", access="moderator"),
    HelpCommandSpec("channel slowmode", "moderation", "/channel slowmode <#channel> <seconds|disable>", "/channel slowmode <#canal> <segundos|disable>", "Set or disable slowmode.", "Configura o desactiva slowmode.", access="moderator"),
    HelpCommandSpec("user", "moderation", "/user", "/user", "Member moderation prompt.", "Ayuda rápida de miembros.", access="moderator"),
    HelpCommandSpec("user info", "moderation", "/user info <user>", "/user info <usuario>", "Show server member info.", "Muestra info del miembro.", access="moderator"),
    HelpCommandSpec("user setnick", "moderation", "/user setnick <user> <nickname>", "/user setnick <usuario> <apodo>", "Change nickname.", "Cambia apodo.", access="moderator"),
    HelpCommandSpec("user mute", "moderation", "/user mute <user> [reason]", "/user mute <usuario> [razón]", "Mute a member.", "Silencia a un miembro.", access="moderator"),
    HelpCommandSpec("user unmute", "moderation", "/user unmute <user> [reason]", "/user unmute <usuario> [razón]", "Remove mute role.", "Quita silencio.", access="moderator"),
    HelpCommandSpec("user kick", "moderation", "/user kick <user> [reason]", "/user kick <usuario> [razón]", "Kick a member.", "Expulsa a un miembro.", access="moderator"),
    HelpCommandSpec("user ban", "moderation", "/user ban <user> [reason]", "/user ban <usuario> [razón]", "Ban a member.", "Banea a un miembro.", access="moderator"),
    HelpCommandSpec("user unban", "moderation", "/user unban <user_id> [reason]", "/user unban <id_usuario> [razón]", "Unban by ID.", "Desbanea por ID.", access="moderator"),
    HelpCommandSpec("user tempmute", "moderation", "/user tempmute <user> <time> [reason]", "/user tempmute <usuario> <tiempo> [razón]", "Temporary mute.", "Silencio temporal.", access="moderator"),
    HelpCommandSpec("user tempban", "moderation", "/user tempban <user> <time> [reason]", "/user tempban <usuario> <tiempo> [razón]", "Temporary ban.", "Ban temporal.", access="moderator"),
    HelpCommandSpec("user warn", "moderation", "/user warn <user> [reason]", "/user warn <usuario> [razón]", "Add a warning.", "Agrega advertencia.", access="moderator"),
    HelpCommandSpec("user unwarn", "moderation", "/user unwarn <user> <1|2|3>", "/user unwarn <usuario> <1|2|3>", "Remove a warning slot.", "Quita advertencia.", access="moderator"),
    HelpCommandSpec("user warnings", "moderation", "/user warnings <user>", "/user warnings <usuario>", "List warnings.", "Lista advertencias.", access="moderator"),
    HelpCommandSpec("user clearwarnings", "moderation", "/user clearwarnings <user> [reason]", "/user clearwarnings <usuario> [razón]", "Clear warnings.", "Limpia advertencias.", access="moderator"),
    HelpCommandSpec("role", "moderation", "/role", "/role", "Role moderation prompt.", "Ayuda rápida de roles.", access="moderator"),
    HelpCommandSpec("role add", "moderation", "/role add <user> <role>", "/role add <usuario> <rol>", "Add a role.", "Agrega un rol.", access="moderator"),
    HelpCommandSpec("role remove", "moderation", "/role remove <user> <role>", "/role remove <usuario> <rol>", "Remove a role.", "Quita un rol.", access="moderator"),
    HelpCommandSpec("role create", "moderation", "/role create <name> [color]", "/role create <nombre> [color]", "Create a role.", "Crea un rol.", access="moderator"),
    HelpCommandSpec("color", "admin", "/color", "/color", "Color role prompt.", "Ayuda rápida de roles de color.", access="administrator"),
    HelpCommandSpec("color setup", "admin", "/color setup", "/color setup", "Create default color roles.", "Crea roles de color por defecto.", access="administrator"),
    HelpCommandSpec("color list", "admin", "/color list", "/color list", "Show public color panel.", "Muestra panel público de colores.", access="administrator"),
    HelpCommandSpec("color channel", "admin", "/color channel <#channel>", "/color channel <#canal>", "Set color panel channel.", "Define canal del panel.", access="administrator"),
    HelpCommandSpec("color reload", "admin", "/color reload", "/color reload", "Repost/update color panel.", "Republica/actualiza panel.", access="administrator"),
    HelpCommandSpec("color add", "admin", "/color add <color> [name]", "/color add <color> [nombre]", "Add selectable color role.", "Agrega rol de color.", access="administrator"),
    HelpCommandSpec("color remove", "admin", "/color remove <name>", "/color remove <nombre>", "Remove selectable color role.", "Elimina rol de color.", access="administrator"),
    HelpCommandSpec("setmodlog", "admin", "/setmodlog [#channel]", "/setmodlog [#canal]", "Set moderation log channel.", "Define canal de logs.", access="manage_guild"),
    HelpCommandSpec("setprefix", "admin", "/setprefix <new_prefix>", "/setprefix <nuevo_prefijo>", "Change server prefix.", "Cambia prefijo del servidor.", access="manage_guild"),
    HelpCommandSpec("language", "admin", "/language <en|es>", "/language <en|es>", "Set command response language.", "Define idioma de comandos.", access="manage_guild", material_choices=("en", "es")),
    HelpCommandSpec("setservercontext", "admin", "/setservercontext <#channel>", "/setservercontext <#canal>", "Analyze a channel and refresh AI server context.", "Analiza un canal y actualiza contexto IA.", access="manage_guild"),
    HelpCommandSpec("resetservercontext", "admin", "/resetservercontext [summaries|memory|ai_history|all]", "/resetservercontext [summaries|memory|ai_history|all]", "Reset AI summaries, memory, history, or all.", "Limpia resúmenes, memoria, historial IA o todo.", access="manage_guild"),
    HelpCommandSpec("viewservercontext", "admin", "/viewservercontext", "/viewservercontext", "View AI context and memory counts.", "Muestra contexto IA y conteos.", access="manage_guild"),
    HelpCommandSpec("servercontext", "admin", "/servercontext", "/servercontext", "View structured server memory.", "Muestra memoria estructurada.", access="manage_guild"),
    HelpCommandSpec("servercontext view", "admin", "/servercontext view", "/servercontext view", "View structured server memory.", "Muestra memoria estructurada.", access="manage_guild"),
    HelpCommandSpec("servercontext remember", "admin", "/servercontext remember <type> <value> [user] [channel] [key]", "/servercontext remember <tipo> <valor> [usuario] [canal] [key]", "Store structured server memory.", "Guarda memoria estructurada.", access="manage_guild"),
    HelpCommandSpec("servercontext forget", "admin", "/servercontext forget <memory_id>", "/servercontext forget <memory_id>", "Archive a memory.", "Archiva una memoria.", access="manage_guild"),
    HelpCommandSpec("servercontext list", "admin", "/servercontext list [type] [status]", "/servercontext list [tipo] [estado]", "List memories.", "Lista memorias.", access="manage_guild"),
    HelpCommandSpec("servercontext user", "admin", "/servercontext user <user>", "/servercontext user <usuario>", "List user memories.", "Lista memorias de usuario.", access="manage_guild"),
    HelpCommandSpec("servercontext approve", "admin", "/servercontext approve <memory_id>", "/servercontext approve <memory_id>", "Approve pending memory.", "Aprueba memoria pendiente.", access="manage_guild"),
    HelpCommandSpec("servercontext reject", "admin", "/servercontext reject <memory_id>", "/servercontext reject <memory_id>", "Reject pending memory.", "Rechaza memoria pendiente.", access="manage_guild"),
    HelpCommandSpec("servercontext reset", "admin", "/servercontext reset <summaries|memory|ai_history|all>", "/servercontext reset <summaries|memory|ai_history|all>", "Reset one context scope.", "Reinicia un scope de contexto.", access="manage_guild"),
    HelpCommandSpec("antispam", "admin", "/antispam <true|false>", "/antispam <true|false>", "Toggle anti-spam filter.", "Activa/desactiva anti-spam.", access="manage_guild"),
    HelpCommandSpec("antilink", "admin", "/antilink <true|false>", "/antilink <true|false>", "Toggle anti-link filter.", "Activa/desactiva anti-links.", access="manage_guild"),
    HelpCommandSpec("aichannel", "admin", "/aichannel", "/aichannel", "Show AI channel restrictions.", "Muestra restricciones de canales IA.", access="manage_guild"),
    HelpCommandSpec("aichannel add", "admin", "/aichannel add <#channel>", "/aichannel add <#canal>", "Allow AI in one channel.", "Permite IA en un canal.", access="manage_guild"),
    HelpCommandSpec("aichannel remove", "admin", "/aichannel remove <#channel>", "/aichannel remove <#canal>", "Remove an allowed AI channel.", "Quita canal permitido de IA.", access="manage_guild"),
    HelpCommandSpec("aichannel list", "admin", "/aichannel list", "/aichannel list", "List AI-allowed channels.", "Lista canales permitidos para IA.", access="manage_guild"),
    HelpCommandSpec("aichannel clear", "admin", "/aichannel clear", "/aichannel clear", "Allow AI in all channels.", "Permite IA en todos los canales.", access="manage_guild"),
    HelpCommandSpec("welcome", "announcements", "/welcome show", "/welcome show", "Show welcome settings.", "Muestra configuración de bienvenida.", access="manage_guild"),
    HelpCommandSpec("welcome show", "announcements", "/welcome show", "/welcome show", "Show welcome settings.", "Muestra configuración de bienvenida.", access="manage_guild"),
    HelpCommandSpec("welcome set", "announcements", "/welcome set [channel] [mode] [message] [image] [color]", "/welcome set [canal] [modo] [mensaje] [imagen] [color]", "Set welcome output.", "Configura bienvenida.", access="manage_guild", material_choices=("mode=text|embed|both",)),
    HelpCommandSpec("welcome edit", "announcements", "/welcome edit [message] [color] [mode] [image] [channel]", "/welcome edit [mensaje] [color] [modo] [imagen] [canal]", "Edit welcome options.", "Edita opciones de bienvenida.", access="manage_guild", material_choices=("mode=text|embed|both",)),
    HelpCommandSpec("welcome test", "announcements", "/welcome test", "/welcome test", "Preview welcome output.", "Vista previa de bienvenida.", access="manage_guild"),
    HelpCommandSpec("welcome preview", "announcements", "/welcome preview", "/welcome preview", "Alias of /welcome test.", "Alias de /welcome test.", access="manage_guild"),
    HelpCommandSpec("goodbye", "announcements", "/goodbye show", "/goodbye show", "Show goodbye settings.", "Muestra configuración de despedida.", access="manage_guild"),
    HelpCommandSpec("goodbye show", "announcements", "/goodbye show", "/goodbye show", "Show goodbye settings.", "Muestra configuración de despedida.", access="manage_guild"),
    HelpCommandSpec("goodbye set", "announcements", "/goodbye set [channel] [mode] [message] [image] [color]", "/goodbye set [canal] [modo] [mensaje] [imagen] [color]", "Set goodbye output.", "Configura despedida.", access="manage_guild", material_choices=("mode=text|embed|both",)),
    HelpCommandSpec("goodbye edit", "announcements", "/goodbye edit [message] [color] [mode] [image] [channel]", "/goodbye edit [mensaje] [color] [modo] [imagen] [canal]", "Edit goodbye options.", "Edita opciones de despedida.", access="manage_guild", material_choices=("mode=text|embed|both",)),
    HelpCommandSpec("goodbye test", "announcements", "/goodbye test", "/goodbye test", "Preview goodbye output.", "Vista previa de despedida.", access="manage_guild"),
    HelpCommandSpec("goodbye preview", "announcements", "/goodbye preview", "/goodbye preview", "Alias of /goodbye test.", "Alias de /goodbye test.", access="manage_guild"),
)


HELP_CAPABILITIES: tuple[HelpCapabilitySpec, ...] = (
    HelpCapabilitySpec("ai_conversation", "general", "AI Conversation", "Conversación IA", "Mention Nitori, address it by name, reply to one of its AI messages, or continue a fresh same-user thread. Slash-command result embeds are ignored unless you explicitly mention Nitori in the reply.", "Menciona a Nitori, háblale por nombre, responde a uno de sus mensajes IA o continúa una conversación reciente. Las respuestas a embeds de comandos se ignoran salvo que menciones a Nitori explícitamente."),
    HelpCapabilitySpec("image_context", "general", "Images", "Imágenes", "Nitori can analyze supported current/replied images and can generate or edit images when directly asked.", "Nitori puede analizar imágenes actuales/respondidas y generar o editar imágenes cuando se lo pides directamente."),
    HelpCapabilitySpec("web_lookup", "utility", "Current Web Lookup", "Búsqueda web actual", "When web search is enabled, direct current/freshness questions can use web lookup. Normal chat does not browse automatically.", "Cuando la búsqueda web está activada, preguntas directas sobre información actual pueden usar web. El chat normal no navega automáticamente."),
    HelpCapabilitySpec("football_ai", "sports", "Natural Football Questions", "Preguntas naturales de fútbol", "You can ask football questions in normal language. Nitori resolves teams, players, leagues, fixtures, dates, stats, standings, events, lineups, transfers, injuries, and match-center details through API-Football.", "Puedes hacer preguntas de fútbol en lenguaje natural. Nitori resuelve equipos, jugadores, ligas, partidos, fechas, estadísticas, tablas, eventos, alineaciones, transferencias, lesiones y centros de partido con API-Football."),
    HelpCapabilitySpec("football_watch", "sports", "Live Watch", "Seguimiento en vivo", "Ask Nitori to follow a live match for compact updates. One AI-started watch can run per channel and stops at final time or timeout.", "Pídele a Nitori seguir un partido en vivo para actualizaciones compactas. Puede haber un seguimiento IA por canal y se detiene al final o por timeout."),
    HelpCapabilitySpec("voice_conversation", "voice", "Conversational Voice", "Voz conversacional", "Any normal user can explicitly ask for the current AI response as a native Discord voice message. Voice applies to that one response only; the next turn returns to text unless voice is requested again.", "Cualquier usuario normal puede pedir explícitamente que la respuesta IA actual se entregue como mensaje de voz nativo de Discord. La voz aplica solo a esa respuesta; el siguiente turno vuelve a texto salvo que se pida voz otra vez."),
    HelpCapabilitySpec("voice_tts", "voice", "TTS Details", "Detalles TTS", "Voice uses the configured xAI TTS voice, currently Iris with es-MX. It sends native Discord voice messages, not Discord tts=true and not voice-channel playback.", "La voz usa la voz xAI TTS configurada, actualmente Iris con es-MX. Envía mensajes de voz nativos de Discord, no Discord tts=true ni reproducción en canal de voz."),
    HelpCapabilitySpec("voice_tags", "voice", "Expressive Tags", "Etiquetas expresivas", "In voice delivery, supported xAI expressive tags may be used when semantically appropriate: " + ", ".join(f"`[{tag}]`" for tag in sorted(ALLOWED_TTS_TAGS)), "En respuestas de voz, se pueden usar etiquetas expresivas de xAI cuando tengan sentido: " + ", ".join(f"`[{tag}]`" for tag in sorted(ALLOWED_TTS_TAGS))),
)


SERVER_CONTEXT_TOPIC_TERMS: dict[str, tuple[str, ...]] = {
    "football": (
        "football",
        "futbol",
        "fútbol",
        "soccer",
        "partido",
        "liga",
        "goles",
        "gol",
        "fixture",
        "api-football",
    ),
    "gaming": ("game", "gaming", "juego", "videojuego", "minecraft", "steam", "xbox", "playstation"),
    "technology": ("code", "codigo", "código", "programacion", "programación", "deploy", "api", "bot", "server"),
}

SERVER_CONTEXT_STYLE_TERMS: dict[str, tuple[str, ...]] = {
    "informal and conversational": ("wey", "we", "bro", "jaja", "lol", "no manches", "cabron", "cabrón"),
    "polite and direct": ("please", "por favor", "gracias", "thanks"),
    "concise replies": ("resume", "resumen", "short", "corto", "breve", "rapido", "rápido"),
}


class HelpPaginatorView(discord.ui.View):
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
            row=0,
        )
        self.next_button = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label=tr(lang, "Next", "Siguiente"),
            row=0,
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
                "Only the user who opened this help panel can control these buttons.",
                "Solo el usuario que abrió este panel de ayuda puede usar estos botones.",
            ),
            ephemeral=True,
        )
        return False

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        if not self.pages:
            return
        self.current_page = (self.current_page - 1) % len(self.pages)
        self._sync_controls()
        await interaction.response.edit_message(
            embed=self.pages[self.current_page], view=self
        )

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if not self.pages:
            return
        self.current_page = (self.current_page + 1) % len(self.pages)
        self._sync_controls()
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


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.server_context_refresh_worker.start()

    def cog_unload(self) -> None:
        self.server_context_refresh_worker.cancel()

    async def _lang(self, guild: discord.Guild | None) -> str:
        if guild is None:
            return "en"
        settings = await self.bot.db.get_guild_settings(guild.id)
        return settings.language_code

    async def _update_prefix(self, ctx: commands.Context, new_prefix: str) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        new_prefix = new_prefix.strip()
        if not new_prefix:
            await ctx.send("Prefix cannot be empty.")
            return
        if len(new_prefix) > 5:
            await ctx.send("Prefix is too long. Use up to 5 characters.")
            return

        await self.bot.db.get_or_create_guild_settings(guild.id)
        await self.bot.db.set_prefix(guild.id, new_prefix)
        await ctx.send(f"Prefix updated to `{new_prefix}`.")

    async def _collect_channel_messages(
        self, channel: discord.TextChannel, lang: str
    ) -> list[str]:
        since = datetime.now(timezone.utc) - timedelta(days=7)
        lines: list[str] = []

        async for msg in channel.history(limit=1000, after=since, oldest_first=True):
            if msg.author.bot:
                continue

            content = (msg.content or "").strip()
            if not content:
                if msg.attachments:
                    content = tr(lang, "[attachment]", "[archivo adjunto]")
                else:
                    continue

            content = " ".join(content.split())
            if self._is_context_line_suspicious(content):
                continue
            if len(content) > 260:
                content = f"{content[:257]}..."

            lines.append(f"{msg.author.display_name}: {content}")
            if len(lines) >= 400:
                break

        return lines

    async def _summarize_channel_context(
        self,
        *,
        channel: discord.TextChannel,
        lang: str,
    ) -> str | None:
        lines = await self._collect_channel_messages(channel, lang)
        if not lines:
            return None

        pseudo_rows = [
            {
                "role": "user",
                "speaker": line.split(":", 1)[0].strip() if ":" in line else "User",
                "content": line.split(":", 1)[1].strip() if ":" in line else line,
                "message_id": str(index),
                "author_user_id": line.split(":", 1)[0].strip() if ":" in line else "User",
                "parent_message_id": str(index // 4),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            for index, line in enumerate(lines)
        ]
        if not self._has_enough_interaction_context(pseudo_rows):
            return None

        transcript = self._interaction_rows_to_transcript(pseudo_rows)
        if not transcript:
            return None
        if len(transcript) > 22000:
            transcript = transcript[:22000]

        return await self.bot.llm_client.summarize_server_context(
            channel_name=channel.name,
            messages_transcript=transcript,
            language=lang,
        )

    @staticmethod
    def _is_context_entry_stale(entry: dict[str, object]) -> bool:
        raw = str(entry.get("updated_at", "")).strip()
        if not raw:
            return True
        try:
            updated_at = datetime.fromisoformat(raw)
        except ValueError:
            return True
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - updated_at >= timedelta(days=7)

    @staticmethod
    def _has_enough_interaction_context(rows: list[dict[str, object]]) -> bool:
        eligible = AdminCog._eligible_server_context_rows(rows)
        if len(eligible) < 12:
            return False
        branches = AdminCog._distinct_context_branches(eligible)
        users = AdminCog._distinct_context_users(eligible)
        days = AdminCog._distinct_context_days(eligible)
        if len(branches) < 3:
            return False
        if len(users) < 2 and len(days) < 2:
            return False
        total_chars = sum(len(str(row.get("content", "")).strip()) for row in eligible)
        return total_chars >= 240

    @staticmethod
    def _interaction_rows_to_transcript(rows: list[dict[str, object]]) -> str:
        eligible = AdminCog._eligible_server_context_rows(rows)
        stable = AdminCog._stable_server_context_patterns(eligible)
        if not any(stable.values()):
            return ""
        lines = [
            "Eligible stable server patterns only. Summarize only these pre-qualified patterns.",
            "Do not add names, matches, clubs, players, files, images, events, commands, or one-off details.",
        ]
        for label, key in (
            ("Tone", "tone"),
            ("Inside jokes/memes", "inside_jokes"),
            ("Common topics", "common_topics"),
            ("Personality style", "personality_style"),
            ("How the bot should reply", "reply_style"),
        ):
            values = stable.get(key, [])
            if values:
                lines.append(f"{label}: {' | '.join(values[:6])}")
        return "\n".join(lines)

    @staticmethod
    def _eligible_server_context_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
        eligible: list[dict[str, object]] = []
        for row in rows:
            if str(row.get("role", "")).strip().lower() != "user":
                continue
            content = " ".join(str(row.get("content", "")).strip().split())
            if len(content) < 8:
                continue
            if AdminCog._is_context_line_suspicious(content):
                continue
            copy = dict(row)
            copy["content"] = content
            eligible.append(copy)
        return eligible

    @staticmethod
    def _distinct_context_users(rows: list[dict[str, object]]) -> set[str]:
        return {
            str(row.get("author_user_id") or row.get("speaker") or "").strip().casefold()
            for row in rows
            if str(row.get("author_user_id") or row.get("speaker") or "").strip()
        }

    @staticmethod
    def _context_branch_key(row: dict[str, object], index: int) -> str:
        parent = str(row.get("parent_message_id") or "").strip()
        if parent:
            return parent
        message_id = str(row.get("message_id") or "").strip()
        channel_id = str(row.get("channel_id") or "").strip()
        return f"{channel_id}:{message_id or index}"

    @staticmethod
    def _distinct_context_branches(rows: list[dict[str, object]]) -> set[str]:
        return {AdminCog._context_branch_key(row, index) for index, row in enumerate(rows)}

    @staticmethod
    def _distinct_context_days(rows: list[dict[str, object]]) -> set[str]:
        days: set[str] = set()
        for row in rows:
            raw = str(row.get("created_at") or "").strip()
            if len(raw) >= 10:
                days.add(raw[:10])
        return days

    @staticmethod
    def _candidate_passes_context_threshold(rows: list[dict[str, object]]) -> bool:
        if len(rows) < 3:
            return False
        if len(AdminCog._distinct_context_branches(rows)) < 2:
            return False
        if len(AdminCog._distinct_context_users(rows)) < 2 and len(AdminCog._distinct_context_days(rows)) < 2:
            return False
        return True

    @staticmethod
    def _stable_server_context_patterns(rows: list[dict[str, object]]) -> dict[str, list[str]]:
        buckets: dict[str, dict[str, list[dict[str, object]]]] = {
            "tone": defaultdict(list),
            "inside_jokes": defaultdict(list),
            "common_topics": defaultdict(list),
            "personality_style": defaultdict(list),
            "reply_style": defaultdict(list),
        }
        for row in rows:
            text = str(row.get("content") or "")
            normalized = text.casefold()
            for topic, terms in SERVER_CONTEXT_TOPIC_TERMS.items():
                if any(term in normalized for term in terms):
                    buckets["common_topics"][topic].append(row)
            for style, terms in SERVER_CONTEXT_STYLE_TERMS.items():
                if any(term in normalized for term in terms):
                    if style == "concise replies":
                        buckets["reply_style"][style].append(row)
                    elif style == "informal and conversational":
                        buckets["tone"][style].append(row)
                        buckets["personality_style"]["playful conversational energy"].append(row)
                    else:
                        buckets["tone"][style].append(row)
            joke = AdminCog._inside_joke_candidate(text)
            if joke:
                buckets["inside_jokes"][joke].append(row)

        stable: dict[str, list[str]] = {key: [] for key in buckets}
        for field, candidates in buckets.items():
            for label, evidence in candidates.items():
                if AdminCog._candidate_passes_context_threshold(evidence):
                    stable[field].append(label)
        return stable

    @staticmethod
    def _inside_joke_candidate(text: str) -> str | None:
        normalized = " ".join(text.strip().split())
        if not normalized:
            return None
        lowered = normalized.casefold()
        if any(term in lowered for terms in SERVER_CONTEXT_TOPIC_TERMS.values() for term in terms):
            return None
        if len(normalized) > 80:
            return None
        if re.search(r"\b[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñ]+)+", normalized):
            return None
        if any(marker in lowered for marker in ("jaja", "lol", "meme", "chiste")):
            return normalized
        return None

    @tasks.loop(hours=24)
    async def server_context_refresh_worker(self) -> None:
        for guild in list(self.bot.guilds):
            try:
                await self._refresh_guild_server_context(guild)
            except Exception:
                logging.exception("AI server context refresh failed in guild=%s", guild.id)

    @server_context_refresh_worker.before_loop
    async def before_server_context_refresh_worker(self) -> None:
        await self.bot.wait_until_ready()

    async def _refresh_guild_server_context(self, guild: discord.Guild) -> None:
        entries = await self.bot.db.get_server_context_entries(guild.id)
        real_entries = [
            entry for entry in entries if int(entry.get("channel_id", 0)) > 0
        ]
        lang = await self._lang(guild)

        if real_entries:
            for entry in real_entries:
                if not self._is_context_entry_stale(entry):
                    continue
                channel_id = int(entry["channel_id"])
                channel = guild.get_channel(channel_id)
                if not isinstance(channel, discord.TextChannel):
                    continue
                try:
                    summary = await self._summarize_channel_context(
                        channel=channel,
                        lang=lang,
                    )
                except (discord.Forbidden, discord.HTTPException):
                    logging.exception(
                        "Failed to refresh AI channel context guild=%s channel=%s",
                        guild.id,
                        channel_id,
                    )
                    continue
                if not summary:
                    continue
                await self.bot.db.upsert_server_context_entry(
                    guild_id=guild.id,
                    channel_id=channel.id,
                    channel_name=channel.name,
                    summary=summary,
                    max_entries=2,
                )
            return

        sentinel_entry = next(
            (
                entry
                for entry in entries
                if int(entry.get("channel_id", 0)) == AI_INTERACTIONS_CONTEXT_CHANNEL_ID
            ),
            None,
        )
        if sentinel_entry is not None and not self._is_context_entry_stale(sentinel_entry):
            return

        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        rows = await self.bot.db.get_recent_ai_conversation_turns(guild.id, since)
        if not self._has_enough_interaction_context(rows):
            return

        transcript = self._interaction_rows_to_transcript(rows)
        if not transcript:
            return
        summary = await self.bot.llm_client.summarize_server_context(
            channel_name="ai-interactions",
            messages_transcript=transcript,
            language=lang,
        )
        await self.bot.db.upsert_server_context_entry(
            guild_id=guild.id,
            channel_id=AI_INTERACTIONS_CONTEXT_CHANNEL_ID,
            channel_name="ai-interactions",
            summary=summary,
            max_entries=2,
        )

    @staticmethod
    def _is_context_line_suspicious(content: str) -> bool:
        lowered = content.casefold()
        markers = (
            "ignore previous instructions",
            "ignore all instructions",
            "disregard previous instructions",
            "you are now system",
            "act as system",
            "developer prompt",
            "system prompt",
            "reveal prompt",
            "jailbreak",
            "prompt injection",
        )
        return any(marker in lowered for marker in markers)

    @commands.hybrid_command(name="setmodlog", description="Set the moderation log channel.")
    @owner_or_has_permissions(manage_guild=True)
    async def set_modlog(
        self, ctx: commands.Context, channel: discord.TextChannel | None = None
    ) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        await self.bot.db.get_or_create_guild_settings(guild.id)
        await self.bot.db.set_modlog_channel(guild.id, channel.id if channel else None)
        if channel is None:
            await ctx.send("Mod-log channel cleared.")
            return
        await ctx.send(f"Mod-log channel set to {channel.mention}.")

    @commands.hybrid_command(
        name="setprefix", description="Set the text command prefix for this server."
    )
    @owner_or_has_permissions(manage_guild=True)
    async def set_prefix(self, ctx: commands.Context, prefix: str) -> None:
        await self._update_prefix(ctx, prefix)

    @commands.hybrid_command(
        name="language",
        description="Set bot language for command responses. Allowed: en, es.",
    )
    @owner_or_has_permissions(manage_guild=True)
    @discord.app_commands.rename(language_code="language")
    async def language(self, ctx: commands.Context, language_code: str) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        requested = language_code.strip().lower()
        if requested not in {"en", "es"}:
            await ctx.send("Invalid language code. Use `en` or `es`.")
            return

        lang = normalize_language(requested)
        await self.bot.db.get_or_create_guild_settings(guild.id)
        await self.bot.db.set_language(guild.id, lang)
        await ctx.send(
            tr(
                lang,
                f"Bot language updated to `{lang}`.",
                f"Idioma del bot actualizado a `{lang}`.",
            )
        )

    @commands.hybrid_command(
        name="setservercontext",
        description="Analyze one text channel (last 7 days) and update AI server context.",
    )
    @owner_or_has_permissions(manage_guild=True)
    async def set_server_context(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        lang = await self._lang(guild)

        if ctx.interaction:
            await ctx.defer()

        try:
            summary = await self._summarize_channel_context(
                channel=channel,
                lang=lang,
            )
        except discord.Forbidden:
            await ctx.send(
                tr(
                    lang,
                    "I do not have permission to read message history in that channel.",
                    "No tengo permisos para leer el historial de mensajes en ese canal.",
                )
            )
            return
        except discord.HTTPException:
            logging.exception("Failed to read channel history for context in guild=%s", guild.id)
            await ctx.send(
                tr(
                    lang,
                    "Failed to read channel history.",
                    "No se pudo leer el historial del canal.",
                )
            )
            return
        except Exception:
            logging.exception("Failed to summarize server context in guild=%s", guild.id)
            await ctx.send(
                tr(
                    lang,
                    "Failed to analyze channel context right now. Try again in a moment.",
                    "No se pudo analizar el contexto del canal en este momento. Intenta de nuevo en un momento.",
                )
            )
            return

        if not summary:
            await ctx.send(
                tr(
                    lang,
                    "I could not find enough user messages from the last 7 days in that channel. "
                    "If the channel is old/inactive, choose a more active one.",
                    "No encontré suficientes mensajes de usuarios de los últimos 7 días en ese canal. "
                    "Si el canal es antiguo o inactivo, elige uno más activo.",
                )
            )
            return

        await self.bot.db.upsert_server_context_entry(
            guild_id=guild.id,
            channel_id=channel.id,
            channel_name=channel.name,
            summary=summary,
            max_entries=2,
        )

        await ctx.send(
            tr(
                lang,
                "I now understand how the people in the server behave, I will talk like you from now!",
                "Ahora entiendo cómo se comporta la gente en el servidor, ¡hablaré como ustedes de ahora en adelante!",
            )
        )

    @commands.hybrid_command(
        name="resetservercontext",
        description="Reset AI server context and stored AI conversation memory.",
    )
    @discord.app_commands.describe(scope="What to reset: summaries, memory, ai_history, or all.")
    @owner_or_has_permissions(manage_guild=True)
    async def reset_server_context(self, ctx: commands.Context, scope: str = "all") -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        lang = await self._lang(guild)
        scope_key = scope.strip().casefold()
        if scope_key not in {"summaries", "memory", "ai_history", "all"}:
            await ctx.send(
                tr(
                    lang,
                    "Invalid scope. Use `summaries`, `memory`, `ai_history`, or `all`.",
                    "Scope invalido. Usa `summaries`, `memory`, `ai_history` o `all`.",
                )
            )
            return
        await self._reset_server_context_scope(guild.id, scope_key)
        await ctx.send(
            tr(
                lang,
                f"AI server context reset complete for scope `{scope_key}`.",
                f"Reinicio de contexto IA completado para scope `{scope_key}`.",
            )
        )

    async def _reset_server_context_scope(self, guild_id: int, scope: str) -> None:
        if scope == "all":
            await self.bot.db.reset_ai_server_context(guild_id)
            await ServerMemoryService(self.bot.db).clear_memories(guild_id)
        elif scope == "summaries":
            await self.bot.db.reset_server_context_summaries(guild_id)
        elif scope == "memory":
            await ServerMemoryService(self.bot.db).clear_memories(guild_id)
        elif scope == "ai_history":
            await self.bot.db.reset_ai_conversation_turns(guild_id)
        if scope in {"all", "ai_history"}:
            ai_cog = self.bot.get_cog("AIChatCog")
            if ai_cog is not None and hasattr(ai_cog, "clear_guild_history"):
                ai_cog.clear_guild_history(guild_id)

    @commands.hybrid_command(
        name="viewservercontext",
        description="View the stored AI server context for this server.",
    )
    @owner_or_has_permissions(manage_guild=True)
    async def view_server_context(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        settings = await self.bot.db.get_guild_settings(guild.id)
        entries = await self.bot.db.get_server_context_entries(guild.id)
        counts = await ServerMemoryService(self.bot.db).counts(guild.id)
        output = self._format_server_context_view(settings.server_context, entries, counts)
        await self._send_server_context_view(ctx, output)

    @commands.hybrid_group(
        name="servercontext",
        description="Manage structured AI server memory.",
        fallback="view",
        invoke_without_command=True,
    )
    @owner_or_has_permissions(manage_guild=True)
    async def server_context_group(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        settings = await self.bot.db.get_guild_settings(guild.id)
        entries = await self.bot.db.get_server_context_entries(guild.id)
        counts = await ServerMemoryService(self.bot.db).counts(guild.id)
        output = self._format_server_context_view(settings.server_context, entries, counts)
        await self._send_server_context_view(ctx, output)

    @server_context_group.command(name="remember", description="Store a structured server memory.")
    @discord.app_commands.describe(
        memory_type="Type, for example USER_NICKNAME, SERVER_RULE, CHANNEL_CONTEXT.",
        value="Value to remember.",
        user="Optional target user.",
        channel="Optional target channel.",
        key="Optional memory key.",
    )
    @owner_or_has_permissions(manage_guild=True)
    async def server_context_remember(
        self,
        ctx: commands.Context,
        memory_type: str,
        value: str,
        user: discord.Member | None = None,
        channel: discord.TextChannel | None = None,
        key: str = "",
    ) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        lang = await self._lang(guild)
        try:
            normalized_type = ServerMemoryService.normalize_memory_type(memory_type)
            memory_key = key or ("preferred_nickname" if normalized_type == "USER_NICKNAME" else normalized_type.casefold())
            row = await ServerMemoryService(self.bot.db).create_memory(
                ServerMemoryInput(
                    guild_id=guild.id,
                    memory_type=normalized_type,
                    subject_user_id=user.id if user else None,
                    subject_channel_id=channel.id if channel else None,
                    key=memory_key,
                    value=value,
                    created_by_user_id=ctx.author.id,
                    source_type="command",
                    approved_by_user_id=ctx.author.id,
                )
            )
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        await ctx.send(tr(lang, f"Saved structured memory `{row.get('id')}`.", f"Memoria estructurada guardada `{row.get('id')}`."))

    @server_context_group.command(name="forget", description="Archive a structured server memory.")
    @discord.app_commands.describe(memory_id="Memory ID from /servercontext list or view.")
    @owner_or_has_permissions(manage_guild=True)
    async def server_context_forget(self, ctx: commands.Context, memory_id: int) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        lang = await self._lang(guild)
        ok = await ServerMemoryService(self.bot.db).archive_memory(guild.id, memory_id)
        await ctx.send(tr(lang, "Memory archived." if ok else "Memory not found.", "Memoria archivada." if ok else "Memoria no encontrada."))

    @server_context_group.command(name="list", description="List structured server memories.")
    @discord.app_commands.describe(memory_type="Optional memory type.", status="active, pending, rejected, archived, or expired.")
    @owner_or_has_permissions(manage_guild=True)
    async def server_context_list(self, ctx: commands.Context, memory_type: str = "", status: str = "active") -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        try:
            rows = await ServerMemoryService(self.bot.db).list_memories(
                guild.id,
                memory_type=memory_type or None,
                status=status or None,
                limit=25,
            )
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        await self._send_server_context_view(ctx, self._format_server_memory_rows(rows))

    @server_context_group.command(name="user", description="List structured memories about a user.")
    @owner_or_has_permissions(manage_guild=True)
    async def server_context_user(self, ctx: commands.Context, user: discord.Member) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        rows = await ServerMemoryService(self.bot.db).list_user_memories(guild.id, user.id)
        await self._send_server_context_view(ctx, self._format_server_memory_rows(rows))

    @server_context_group.command(name="approve", description="Approve a pending structured memory.")
    @owner_or_has_permissions(manage_guild=True)
    async def server_context_approve(self, ctx: commands.Context, memory_id: int) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        ok = await ServerMemoryService(self.bot.db).approve_memory(guild.id, memory_id, ctx.author.id)
        await ctx.send("Memory approved." if ok else "Memory not found.")

    @server_context_group.command(name="reject", description="Reject a pending structured memory.")
    @owner_or_has_permissions(manage_guild=True)
    async def server_context_reject(self, ctx: commands.Context, memory_id: int) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        ok = await ServerMemoryService(self.bot.db).reject_memory(guild.id, memory_id, ctx.author.id)
        await ctx.send("Memory rejected." if ok else "Memory not found.")

    @server_context_group.command(name="reset", description="Reset one server context scope.")
    @discord.app_commands.describe(scope="summaries, memory, ai_history, or all.")
    @owner_or_has_permissions(manage_guild=True)
    async def server_context_reset(self, ctx: commands.Context, scope: str = "all") -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        lang = await self._lang(guild)
        scope_key = scope.strip().casefold()
        if scope_key not in {"summaries", "memory", "ai_history", "all"}:
            await ctx.send(
                tr(
                    lang,
                    "Invalid scope. Use `summaries`, `memory`, `ai_history`, or `all`.",
                    "Scope invalido. Usa `summaries`, `memory`, `ai_history` o `all`.",
                )
            )
            return
        await self._reset_server_context_scope(guild.id, scope_key)
        await ctx.send(
            tr(
                lang,
                f"AI server context reset complete for scope `{scope_key}`.",
                f"Reinicio de contexto IA completado para scope `{scope_key}`.",
            )
        )

    @staticmethod
    def _format_server_context_view(
        server_context: str | None,
        entries: list[dict[str, object]],
        memory_counts: list[dict[str, object]] | None = None,
    ) -> str:
        context = (server_context or "").strip()
        if not context and not entries and not memory_counts:
            return "No AI server context is currently stored."

        lines = ["AI server context currently stored:"]
        if memory_counts:
            lines.append("")
            lines.append("Structured memory:")
            for item in memory_counts:
                lines.append(
                    f"- {item.get('memory_type', 'UNKNOWN')} / {item.get('status', 'unknown')}: {item.get('count', 0)}"
                )
        if entries:
            lines.append("")
            lines.append("Sources:")
            for entry in entries:
                channel_id = int(entry.get("channel_id", 0))
                channel_name = str(entry.get("channel_name", "")).strip() or "unknown"
                updated_at = str(entry.get("updated_at", "")).strip() or "unknown time"
                if channel_id == AI_INTERACTIONS_CONTEXT_CHANNEL_ID:
                    label = f"AI interactions ({channel_id})"
                else:
                    label = f"#{channel_name} ({channel_id})"
                lines.append(f"- {label}, updated {updated_at}")

        if context:
            lines.append("")
            lines.append("Context:")
            lines.append(context)
        else:
            lines.append("")
            lines.append("No combined context text is currently stored.")
        return "\n".join(lines)

    async def _send_server_context_view(self, ctx: commands.Context, output: str) -> None:
        chunks = self._split_context_view_output(output)
        ephemeral = bool(ctx.interaction)
        for chunk in chunks:
            if ephemeral:
                await ctx.send(chunk, ephemeral=True)
            else:
                await ctx.send(chunk)

    @staticmethod
    def _format_server_memory_rows(rows: list[dict[str, object]]) -> str:
        if not rows:
            return "No structured server memories found."
        lines = ["Structured server memories:"]
        for row in rows:
            target = ""
            if row.get("subject_user_id"):
                target = f" user={row.get('subject_user_id')}"
            elif row.get("subject_channel_id"):
                target = f" channel={row.get('subject_channel_id')}"
            lines.append(
                f"- `{row.get('id')}` {row.get('memory_type')} {row.get('status')}{target} "
                f"{row.get('key')}: {row.get('value')}"
            )
        return "\n".join(lines)

    @staticmethod
    def _split_context_view_output(output: str, limit: int = 1900) -> list[str]:
        text = output.strip() or "No AI server context is currently stored."
        chunks: list[str] = []
        remaining = text
        while len(remaining) > limit:
            split_at = remaining.rfind("\n", 0, limit)
            if split_at <= 0:
                split_at = limit
            chunks.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].strip()
        if remaining:
            chunks.append(remaining)
        return chunks or ["No AI server context is currently stored."]

    @commands.hybrid_command(name="setup", description="Show bot setup and capabilities.")
    async def setup_help(self, ctx: commands.Context) -> None:
        lang = await self._lang(ctx.guild)
        await ctx.send(
            tr(
                lang,
                "Hello! I'm Nitori. I can help you moderate your server, chat with users, "
                "check Minecraft server status, follow Liga MX matches, and generate memes too!\n"
                "If you want me to learn your server vibe, run `/setservercontext` and choose a text channel from the server list. "
                "I will analyze the last 7 days of that channel.",
                "Hola! Soy Nitori. Puedo ayudarte a moderar tu servidor, chatear con los usuarios "
                "revisar el estado de servidores de Minecraft, seguir Liga MX y generar memes también!\n"
                "Si quieres que aprenda mejor la vibra del servidor, usa `/setservercontext` y elige un canal de texto de la lista del servidor. "
                "Analizaré los últimos 7 días de ese canal.",
            )
        )

    @commands.hybrid_command(
        name="antispam", description="Enable or disable the basic anti-spam filter."
    )
    @owner_or_has_permissions(manage_guild=True)
    async def antispam(self, ctx: commands.Context, enabled: bool) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        await self.bot.db.set_anti_spam(ctx.guild.id, enabled)
        await ctx.send(f"Anti-spam is now {'enabled' if enabled else 'disabled'}.")

    @commands.hybrid_command(
        name="antilink", description="Enable or disable the basic anti-link filter."
    )
    @owner_or_has_permissions(manage_guild=True)
    async def antilink(self, ctx: commands.Context, enabled: bool) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        await self.bot.db.set_anti_link(ctx.guild.id, enabled)
        await ctx.send(f"Anti-link is now {'enabled' if enabled else 'disabled'}.")

    @commands.hybrid_group(
        name="aichannel",
        description="Manage AI-allowed channels.",
        invoke_without_command=True,
    )
    @owner_or_has_permissions(manage_guild=True)
    async def aichannel(self, ctx: commands.Context) -> None:
        await self.aichannellist(ctx)

    @aichannel.command(
        name="add",
        description="Allow AI chat/translation in a specific channel.",
    )
    @owner_or_has_permissions(manage_guild=True)
    async def aichanneladd(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        await self.bot.db.add_ai_channel(ctx.guild.id, channel.id)
        await ctx.send(f"AI channel added: {channel.mention}.")

    @aichannel.command(
        name="remove",
        description="Remove a channel from the AI-allowed channel list.",
    )
    @owner_or_has_permissions(manage_guild=True)
    async def aichannelremove(
        self, ctx: commands.Context, channel: discord.TextChannel
    ) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        scope, allowed_channels = await self.bot.db.get_ai_channel_scope(ctx.guild.id)
        if scope == "all":
            await ctx.send(
                "AI is currently allowed in all channels. Use `/aichannel add` first to enable channel restrictions."
            )
            return
        if channel.id not in allowed_channels:
            await ctx.send(f"{channel.mention} is not currently in the AI-allowed list.")
            return

        await self.bot.db.remove_ai_channel(ctx.guild.id, channel.id)
        scope_after, allowed_after = await self.bot.db.get_ai_channel_scope(ctx.guild.id)
        if scope_after == "none":
            await ctx.send(
                f"AI channel removed: {channel.mention}. AI is now disabled in all channels "
                "until you add at least one channel with `/aichannel add`."
            )
            return
        await ctx.send(
            f"AI channel removed: {channel.mention}. Remaining AI-allowed channels: {len(allowed_after)}."
        )

    @aichannel.command(
        name="list",
        description="List channels where AI chat/translation is currently allowed.",
    )
    @owner_or_has_permissions(manage_guild=True)
    async def aichannellist(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        scope, channel_ids = await self.bot.db.get_ai_channel_scope(ctx.guild.id)
        embed = discord.Embed(title="AI Allowed Channels", color=discord.Color.blue())
        if scope == "all":
            embed.description = "AI is allowed in all channels (no channel restrictions set)."
            await ctx.send(embed=embed)
            return
        if scope == "none":
            embed.description = (
                "AI is currently disabled in all channels. "
                "Use `/aichannel add <#channel>` to allow specific channels."
            )
            await ctx.send(embed=embed)
            return

        mentions = []
        for channel_id in channel_ids:
            channel = ctx.guild.get_channel(channel_id)
            if channel:
                mentions.append(channel.mention)
            else:
                mentions.append(f"`{channel_id}`")
        embed.description = "\n".join(mentions)
        await ctx.send(embed=embed)

    @aichannel.command(
        name="clear",
        description="Clear AI channel restrictions (AI allowed in all channels).",
    )
    @owner_or_has_permissions(manage_guild=True)
    async def aichannelclear(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        await self.bot.db.clear_ai_channels(ctx.guild.id)
        await ctx.send("AI channel restrictions cleared. AI is now allowed in all channels.")

    @commands.hybrid_command(
        name="help",
        description="Show the full help guide with pages.",
    )
    @discord.app_commands.describe(
        section="Open a specific help section (for example: fun, sports, moderation, admin).",
    )
    async def help_cmd(
        self,
        ctx: commands.Context,
        *,
        section: str | None = None,
    ) -> None:
        lang = await self._lang(ctx.guild)
        member = ctx.author if isinstance(ctx.author, discord.Member) else None
        pages = self._build_help_pages(lang, member=member)
        section_keys = self._help_section_keys(member)

        page_index = 0
        if section:
            resolved = self._resolve_help_section(section, section_keys)
            if resolved is None:
                labels = HELP_SECTION_LABELS_EN if lang == "en" else HELP_SECTION_LABELS_ES
                available = ", ".join(f"`{labels.get(key, key)}`" for key in section_keys)
                if ctx.interaction is not None:
                    await ctx.send(
                        tr(
                            lang,
                            f"Unknown help section. Try one of: {available}",
                            f"Seccion de ayuda no valida. Prueba una de estas: {available}",
                        ),
                        ephemeral=True,
                    )
                else:
                    await ctx.send(
                        tr(
                            lang,
                            f"Unknown help section. Try one of: {available}",
                            f"Seccion de ayuda no valida. Prueba una de estas: {available}",
                        ),
                    )
                return
            page_index = resolved

        view = HelpPaginatorView(
            pages=pages,
            author_id=ctx.author.id,
            lang=lang,
        )
        view.current_page = page_index
        if ctx.interaction is not None:
            sent = await ctx.send(embed=pages[page_index], view=view, ephemeral=True)
        else:
            sent = await ctx.send(embed=pages[page_index], view=view)
        if isinstance(sent, discord.Message):
            view.message = sent

    @help_cmd.autocomplete("section")
    async def help_section_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[discord.app_commands.Choice[str]]:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        keys = self._help_section_keys(member)
        lang = await self._lang(interaction.guild)
        labels = HELP_SECTION_LABELS_EN if lang == "en" else HELP_SECTION_LABELS_ES
        current_norm = self._normalize_help_section(current)
        choices: list[discord.app_commands.Choice[str]] = []
        for key in keys:
            label = labels.get(key, key)
            if current_norm and current_norm not in label:
                continue
            choices.append(discord.app_commands.Choice(name=label, value=label))
        return choices[:25]

    def _help_section_keys(self, member: discord.Member | None) -> list[str]:
        is_admin = self._is_admin_member(member)
        is_mod = self._is_mod_member(member)
        keys = ["basic", "sections", "general", "voice", "sports", "fun", "utility", "coding", "birthday"]
        if is_mod:
            keys.append("moderation")
        if is_admin:
            keys.extend(["admin", "announcements", "variables"])
        return keys

    @staticmethod
    def _normalize_help_section(value: str) -> str:
        normalized = value.strip().casefold().replace("_", " ").replace("-", " ")
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized

    def _resolve_help_section(
        self,
        raw_value: str,
        allowed_keys: list[str],
    ) -> int | None:
        normalized = self._normalize_help_section(raw_value)
        if not normalized:
            return 0
        if normalized.isdigit():
            page_number = int(normalized)
            if 1 <= page_number <= len(allowed_keys):
                return page_number - 1
            return None
        mapped = HELP_SECTION_ALIASES.get(normalized)
        if mapped is None or mapped not in allowed_keys:
            return None
        return allowed_keys.index(mapped)

    @staticmethod
    def _is_admin_member(member: discord.Member | None) -> bool:
        if member is None:
            return False
        perms = member.guild_permissions
        return bool(perms.administrator or perms.manage_guild)

    @staticmethod
    def _is_mod_member(member: discord.Member | None) -> bool:
        if member is None:
            return False
        perms = member.guild_permissions
        return bool(
            perms.administrator
            or perms.manage_guild
            or perms.manage_messages
            or perms.manage_channels
            or perms.manage_roles
            or perms.moderate_members
            or perms.kick_members
            or perms.ban_members
            or perms.manage_nicknames
        )

    @staticmethod
    def documented_help_command_paths() -> set[str]:
        return {item.path for item in HELP_COMMANDS}

    @staticmethod
    def documented_help_aliases() -> set[str]:
        aliases: set[str] = set()
        for item in HELP_COMMANDS:
            aliases.update(item.aliases)
        return aliases

    @staticmethod
    def documented_non_command_capabilities() -> set[str]:
        return {item.key for item in HELP_CAPABILITIES}

    @staticmethod
    def intentional_help_exclusions() -> dict[str, str]:
        return dict(HELP_INTENTIONAL_EXCLUSIONS)

    @staticmethod
    def _help_access_allowed(spec: HelpCommandSpec | HelpCapabilitySpec, *, is_admin: bool, is_mod: bool) -> bool:
        if getattr(spec, "show_to_all", False):
            return True
        if spec.access == "everyone":
            return True
        if spec.access == "administrator":
            return is_admin
        if spec.access in {"manage_guild"}:
            return is_admin
        return is_mod

    @staticmethod
    def _help_access_label(lang: str, access: str) -> str:
        labels = HELP_ACCESS_EN if lang == "en" else HELP_ACCESS_ES
        return labels.get(access, "")

    def _help_command_line(self, spec: HelpCommandSpec, lang: str) -> str:
        usage = spec.usage_en if lang == "en" else spec.usage_es
        description = spec.description_en if lang == "en" else spec.description_es
        line = f"`{usage}` - {description}"
        access = self._help_access_label(lang, spec.access)
        if access:
            line += f" {access}"
        if spec.aliases:
            alias_label = tr(lang, "Aliases", "Aliases")
            rendered = ", ".join(f"`/{alias}`" for alias in spec.aliases)
            line += f" {alias_label}: {rendered}."
        return line

    @staticmethod
    def _help_chunks(lines: list[str], *, limit: int = 1000) -> list[str]:
        chunks: list[str] = []
        current = ""
        for line in lines:
            candidate = line if not current else f"{current}\n{line}"
            if len(candidate) <= limit:
                current = candidate
                continue
            if current:
                chunks.append(current)
            current = line
        if current:
            chunks.append(current)
        return chunks or ["-"]

    def _add_help_lines(self, embed: discord.Embed, *, name: str, lines: list[str]) -> None:
        chunks = self._help_chunks(lines)
        for index, chunk in enumerate(chunks, start=1):
            field_name = name if index == 1 else f"{name} ({index})"
            embed.add_field(name=field_name, value=chunk, inline=False)

    def _command_lines_for_section(self, section: str, lang: str, *, is_admin: bool, is_mod: bool) -> list[str]:
        return [
            self._help_command_line(spec, lang)
            for spec in HELP_COMMANDS
            if spec.section == section and self._help_access_allowed(spec, is_admin=is_admin, is_mod=is_mod)
        ]

    def _capability_lines_for_section(self, section: str, lang: str, *, is_admin: bool, is_mod: bool) -> list[str]:
        lines: list[str] = []
        for spec in HELP_CAPABILITIES:
            if spec.section != section:
                continue
            if not self._help_access_allowed(spec, is_admin=is_admin, is_mod=is_mod):
                continue
            title = spec.title_en if lang == "en" else spec.title_es
            body = spec.body_en if lang == "en" else spec.body_es
            lines.append(f"**{title}** - {body}")
        return lines

    def _add_help_section(
        self,
        pages: list[discord.Embed],
        *,
        section: str,
        lang: str,
        title_en: str,
        title_es: str,
        color: discord.Color,
        is_admin: bool,
        is_mod: bool,
    ) -> None:
        embed = discord.Embed(title=tr(lang, title_en, title_es), color=color)
        capability_lines = self._capability_lines_for_section(section, lang, is_admin=is_admin, is_mod=is_mod)
        command_lines = self._command_lines_for_section(section, lang, is_admin=is_admin, is_mod=is_mod)
        if capability_lines:
            self._add_help_lines(embed, name=tr(lang, "Capabilities", "Capacidades"), lines=capability_lines)
        if command_lines:
            self._add_help_lines(embed, name=tr(lang, "Commands", "Comandos"), lines=command_lines)
        pages.append(embed)

    def _build_registered_help_pages(
        self,
        lang: str,
        *,
        member: discord.Member | None = None,
    ) -> list[discord.Embed]:
        is_admin = self._is_admin_member(member)
        is_mod = self._is_mod_member(member)

        page1 = discord.Embed(
            title=tr(lang, "Nitori Help", "Ayuda de Nitori"),
            description=tr(
                lang,
                "Welcome. Use the buttons below to navigate Nitori's current command and capability guide.",
                "Bienvenido. Usa los botones para navegar la guía actual de comandos y capacidades de Nitori.",
            ),
            color=discord.Color.blurple(),
        )
        quick_lines = [
            "`/help`",
            "`/setup`",
            "`@Nitori <message>`",
            "`/football live ligamx`",
            "`/say mensaje:<text> modo:<text|voice>`",
            "`/meme create <template> <top> [bottom]`",
        ]
        self._add_help_lines(page1, name=tr(lang, "Quick Start", "Inicio rápido"), lines=quick_lines)
        page1.add_field(
            name=tr(lang, "Notes", "Notas"),
            value=tr(
                lang,
                "Most commands support prefix usage too. Permission-sensitive commands are labeled or shown only when relevant.",
                "La mayoría de comandos también soportan prefijo. Los comandos con permisos se etiquetan o se muestran solo cuando corresponde.",
            ),
            inline=False,
        )

        section_items = [
            tr(lang, "1. Basic help", "1. Ayuda básica"),
            tr(lang, "2. Sections index", "2. Índice de secciones"),
            tr(lang, "3. AI / Conversation", "3. IA / Conversación"),
            tr(lang, "4. Voice", "4. Voz"),
            tr(lang, "5. Football / Live Watch", "5. Fútbol / En vivo"),
            tr(lang, "6. Fun", "6. Diversión"),
            tr(lang, "7. Utility", "7. Utilidad"),
            tr(lang, "8. Code", "8. Código"),
            tr(lang, "9. Birthday", "9. Cumpleaños"),
        ]
        if is_mod:
            section_items.append(tr(lang, "10. Moderation", "10. Moderación"))
        if is_admin:
            section_items.extend(
                [
                    tr(lang, "11. Admin / Server Context", "11. Admin / Contexto"),
                    tr(lang, "12. Welcome / Goodbye", "12. Welcome / Goodbye"),
                    tr(lang, "13. Variables", "13. Variables"),
                ]
            )
        page2 = discord.Embed(title=tr(lang, "Help Sections", "Secciones de ayuda"), color=discord.Color.blurple())
        self._add_help_lines(page2, name=tr(lang, "This Guide Contains", "Esta guía contiene"), lines=section_items)
        page2.add_field(
            name=tr(lang, "Target Formats", "Formatos de objetivo"),
            value=tr(
                lang,
                "Moderation commands accept @mention, user ID, username, or display name where Discord allows it.",
                "Los comandos de moderación aceptan @mención, ID, usuario o nombre visible cuando Discord lo permite.",
            ),
            inline=False,
        )

        pages = [page1, page2]
        self._add_help_section(pages, section="general", lang=lang, title_en="AI / Conversation", title_es="IA / Conversación", color=discord.Color.green(), is_admin=is_admin, is_mod=is_mod)
        self._add_help_section(pages, section="voice", lang=lang, title_en="Voice", title_es="Voz", color=discord.Color.blue(), is_admin=is_admin, is_mod=is_mod)
        self._add_help_section(pages, section="sports", lang=lang, title_en="Football / Live Watch", title_es="Fútbol / En vivo", color=discord.Color.gold(), is_admin=is_admin, is_mod=is_mod)
        self._add_help_section(pages, section="fun", lang=lang, title_en="Fun", title_es="Diversión", color=discord.Color.teal(), is_admin=is_admin, is_mod=is_mod)
        self._add_help_section(pages, section="utility", lang=lang, title_en="Utility", title_es="Utilidad", color=discord.Color.dark_teal(), is_admin=is_admin, is_mod=is_mod)
        self._add_help_section(pages, section="coding", lang=lang, title_en="Code Runner", title_es="Ejecución de código", color=discord.Color.dark_teal(), is_admin=is_admin, is_mod=is_mod)
        self._add_help_section(pages, section="birthday", lang=lang, title_en="Birthday", title_es="Cumpleaños", color=discord.Color.purple(), is_admin=is_admin, is_mod=is_mod)
        if is_mod:
            self._add_help_section(pages, section="moderation", lang=lang, title_en="Moderation", title_es="Moderación", color=discord.Color.orange(), is_admin=is_admin, is_mod=is_mod)
        if is_admin:
            self._add_help_section(pages, section="admin", lang=lang, title_en="Admin / Server Context", title_es="Admin / Contexto", color=discord.Color.red(), is_admin=is_admin, is_mod=is_mod)
            self._add_help_section(pages, section="announcements", lang=lang, title_en="Welcome / Goodbye", title_es="Welcome / Goodbye", color=discord.Color.dark_gold(), is_admin=is_admin, is_mod=is_mod)
            variables = [
                "`{user}` -> @John",
                "`{username}` -> John",
                "`{avatar}` -> " + tr(lang, "user avatar", "avatar del usuario"),
                "`{server}` -> " + tr(lang, "server name", "nombre del servidor"),
                "`{channel}` / `{channel:rules}` / `{123456789012345678}`",
                "`{role}` / `{role:member}` / `{RoleName}`",
                "`{age}` / `{year}` -> " + tr(lang, "birthday and anniversary templates", "plantillas de cumpleaños y aniversarios"),
            ]
            page_vars = discord.Embed(title=tr(lang, "Template Variables", "Variables de plantilla"), color=discord.Color.dark_blue())
            self._add_help_lines(page_vars, name=tr(lang, "Variables", "Variables"), lines=variables)
            pages.append(page_vars)
        self._set_page_footers(pages, lang)
        return pages

    def _build_help_pages(
        self,
        lang: str,
        *,
        member: discord.Member | None = None,
    ) -> list[discord.Embed]:
        return self._build_registered_help_pages(lang, member=member)

    def _set_page_footers(self, pages: list[discord.Embed], lang: str) -> None:
        total = len(pages)
        for index, embed in enumerate(pages, start=1):
            prefix = tr(lang, "Page", "P\u00e1gina")
            footer = tr(
                lang,
                "Use /help and the buttons to navigate.",
                "Usa /help y los botones para navegar.",
            )
            embed.set_footer(text=f"{footer} | {prefix} {index}/{total}")

    @set_modlog.error
    @set_prefix.error
    @language.error
    @set_server_context.error
    @reset_server_context.error
    @view_server_context.error
    @setup_help.error
    @antispam.error
    @antilink.error
    @aichannel.error
    @aichanneladd.error
    @aichannelremove.error
    @aichannellist.error
    @aichannelclear.error
    async def admin_error_handler(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You do not have permission to use this command.")
            return
        logging.exception("Admin command failed", exc_info=error)
        await ctx.send("Command failed due to an internal error. Please try again.")

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
