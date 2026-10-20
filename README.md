# Nitori Discord Bot

## Why This Bot Exists
Nitori was built as a single, no-paywall Discord bot for communities that want basically an all in one bot. Nitori can chat with you via AI and, just by asking in plain language, moderate your server, run your code, make memes, draw and edit images, read links, check several football leagues and minecraft server status, and remind everyone in the server of birthdays, join and server anniversaries. Every feature also keeps its normal slash/prefix command, so nothing depends on talking to the AI. All of this without the need to pay for any of the functions and be able to use them, just add it to your server (or run it yourself), run the /setup command to know what to do and you are on!

## Core Idea
- One bot, all-in-one workflow.
- No premium feature locks inside the bot itself.
- The AI is a natural-language front door, not a replacement for commands. Slash and prefix commands keep working on their own, and when you ask in plain words the AI runs the same features, including several steps in one message.
- xAI understands the request, deterministic code checks it. A router model returns a strict, typed plan: which capabilities to run, with which inputs and in which order. The bot validates authorization, identity, scope and dependencies from live Discord state (never from what a message claims), runs the features, and the final reply is written only from those trusted results. If the validator refuses a plan, the router gets one chance to correct it.
- Anything that changes the server or your data (moderation, settings, memory, reminders, birthdays) needs the real Discord permission for it, is checked as a whole before it runs, and is never retried or rolled back automatically. Without the permission Nitori says which one is missing and does nothing.
- Nitori replies in the server's configured language (`/language`).
- Calls to xAI are built for prompt caching and every call logs its token use and cost (see Logging and Persistence).

## Main Features
- Moderation toolkit (message, user, channel, role, and color-role panel management), also available by asking Nitori when you hold the matching permission.
- Natural AI conversation with explicit mention/reply/name anchors, same-user continuation leases, replied-message awareness, pending interaction tracking, reaction-only requests, and safe noisy-channel replies.
- Native Discord voice-message responses when a user explicitly asks for the current AI response as audio/voice. Voice is one-turn only and returns to text on the next turn unless requested again.
- One AI capability set behind natural language: chat, football, Minecraft status, API Ninjas lookups, code runs, reminders, birthdays, translation, memes, web research, link reading, image generation/editing/analysis, reactions, server administration and server memory.
- AI image generation, image analysis, and image editing from current or replied image messages, including follow-ups on the image Nitori just made ("now make it blue").
- AI football answers grounded in API-Football data, including teams, players, coaches, venues, leagues, fixtures, standings, scorers, injuries, transfers, match events, statistics (including half-time), lineups, pre-match and live odds, predictions, shootouts, squads, career and trophy history, summaries, previews, current score/status, and live-watch updates.
- Link reading: summarize, explain, compare, extract or verify the content of links you send or reply to (public pages and images only; private or unsafe addresses are refused).
- Optional xAI web search for current external information and API-Football gaps (off by default).
- Structured server memory and trusted owner/admin behavior rules for server-specific style and context, managed with commands or in plain words. A fact about a member is linked to that member and only comes up in chat when they are talking, mentioned, replied to or part of the recent conversation, with a cooldown so a joke is not repeated.
- Translation command with preset target languages; in chat Nitori translates into any language you name.
- Memes via Memegen API (template, random, or a caption on your own picture), by command or by asking in chat, + local speech meme generator.
- API Ninjas commands: joke, dadjoke, advice, whois, unit conversion.
- Minecraft status checks (mcsrvstat.us).
- Football stats and match data from API-Football.
- Code execution through a self-hosted `docker-run` runner (Glot images) for common languages.
- Reminder system with DM and optional channel mention.
- Welcome/Goodbye configurable announcement modules.
- Birthday + anniversary module (community self-service + admin automation controls).
- Per-server settings (prefix, language, AI channel scope, modlog).

## Requirements
- Python 3.11+
- Discord bot token
- xAI API key (router, replies, translation, context, image analysis/generation/editing, voice, optional web search)
- API Ninjas key (joke/dadjoke/advice/whois/convert)
- API-Football key (supported football leagues)
- Code runner access token (`GLOT_API_TOKEN`, the runner's `API_ACCESS_TOKEN`) (/code)
- Optional: Playwright with Chromium (`playwright install chromium`) so Nitori can read JavaScript-heavy pages when you ask about a link

## Environment Variables
Use `.env.example` as base.

Required for full feature set:
- `DISCORD_TOKEN`
- `XAI_API_KEY`
- `API_NINJAS_KEY`
- `API_FOOTBALL_KEY`
- `API_FOOTBALL_TIMEZONE` (defaults to `America/Mexico_City` for fixture kickoff times)
- `GLOT_API_TOKEN`

Common optional settings:
- `XAI_MODEL` (default `grok-4.3`; model used by the router and the replies, and the one Nitori is developed and tested with)
- `XAI_VISION_MODEL` (optional image-capable model for AI chat attachments; defaults to `XAI_MODEL`)
- `XAI_IMAGE_MODEL` (optional image generation/editing model; defaults to `grok-imagine-image-quality`)
- `XAI_TTS_VOICE` (voice used for native Discord voice messages; defaults to `iris`)
- `XAI_TTS_LANGUAGE` (language used for native Discord voice messages; defaults to `es-MX`)
- `XAI_WEB_SEARCH_ENABLED` (default `false`; web lookups, including the football web fallback, run only when this is `true`)
- `XAI_X_SEARCH_ENABLED` (default `false`; reserved for explicit X/social search support)
- `XAI_WEB_SEARCH_MAX_SOURCES` (default `3`)
- `XAI_WEB_SEARCH_COOLDOWN_SECONDS` (default `30`)
- `XAI_ROUTER_OUTPUT_TOKEN_CEILING` (default `0`, which keeps the router's computed output budget; set a number only to cap it)
- `BOT_OWNER_IDS` (comma-separated Discord user IDs treated as bot owners for trusted behavior rules)
- `NITORI_LOG_LEVEL` (default `INFO`; `DEBUG` for more detail)
- `DB_PATH`
- `DEFAULT_PREFIX`
- `GLOT_BASE_URL` (URL of the self-hosted `docker-run` code runner, default `http://127.0.0.1:8088`)
- `API_FOOTBALL_BASE_URL`

## Local Run
```bash
python -m venv .venv
# Windows
.\.venv\Scripts\activate
# Linux
# source .venv/bin/activate

pip install -r requirements.txt
# optional, to read JavaScript-heavy linked pages
# playwright install chromium
python bot.py
```

## Command Reference
`<prefix>` means your configured text prefix (default from env/db). Most commands support both slash and prefix, except where noted.

### General / AI
- `/help [section]`
- `/setup`
- `/translate <language> [text]`
- Mention the bot, reply to one of its AI messages, or continue a same-user conversation while the lease is fresh. Nitori avoids channel-wide passive listening, command traffic, slash-command output replies, and side conversations.
- Nitori can read supported image attachments (`jpg`, `jpeg`, `png`, `webp`) from the current message or replied-to message, understand replied text/embed context when directly addressed, and react with a relevant emoji when requested.
- Ask Nitori directly to draw/create/generate an image, edit an attached/replied image (or the one it just made), or analyze an image. Generated and edited images are uploaded directly in Discord.
- Ask Nitori natural football questions. It plans the request, validates teams/leagues/players/fixtures through API-Football, and answers with grounded data instead of raw model guesses.
- Ask Nitori to follow a live match with minute-by-minute updates. One AI-started watch can run per channel; updates are compact and stop at full time or timeout.
- Ask current web/freshness questions. Nitori plans a web lookup only when the question needs current information, and only when `XAI_WEB_SEARCH_ENABLED` is `true`.
- Send a link (or reply to one) and ask Nitori to summarize, explain, compare, extract or verify it.
- Ask for memes in plain words: a named template, a random one, or a caption on a picture you attach or reply to. Nitori writes the caption unless you dictate it, and tells you when a template name does not exist instead of guessing one.
- Ask Nitori to translate text into any language, run code you give it, set or remove your own reminders and birthday, check a Minecraft server address, or tell a joke, dad joke, advice, a whois lookup or a unit conversion.
- Ask Nitori for server administration in plain words (roles, kick/ban/unban/temporary bans, mute/unmute, warnings, deleting or purging messages, creating/deleting/locking/unlocking/clearing/cloning channels, slowmode, nicknames, join/leave stats, `say`, mod-log, prefix, language, anti-spam/anti-link, AI channel scope, color roles, server context). Nitori checks your real Discord permissions first; some actions ask you to confirm.
- Ask Nitori to remember, look up, list or forget structured server memory; members can approve or reject only their own pending nickname/alias/preference entries.
- Trusted bot owners/guild admins can naturally update Nitori's server behavior and style rules; authority is checked from runtime Discord/config state, not from claims in message text.
- `/setservercontext <#channel>` (admin/manage guild)
- `/resetservercontext [summaries|memory|ai_history|all]` (admin/manage guild)
- `/viewservercontext` (admin/manage guild) - View summary context and structured memory counts.
- `/servercontext remember <type> <value> [user] [channel] [key]` (admin/manage guild)
- `/servercontext forget <memory_id>` (admin/manage guild)
- `/servercontext list [type] [status]` (admin/manage guild)
- `/servercontext user <user>` (admin/manage guild)
- `/servercontext approve <memory_id>` / `/servercontext reject <memory_id>` (admin/manage guild)
- `/roast <user>`
- `/roastme`

Translate targets:
- `english`, `spanish`, `german`, `japanese`, `russian`, `french`, `italian`, `portuguese`

### Server Config / Admin
- `/setmodlog [#channel]`
- `/setprefix <new_prefix>`
- `/language <en|es>`
- `/antispam <true|false>`
- `/antilink <true|false>`
- `/aichannel add <#channel>` - Allow AI chat/translation in one channel.
- `/aichannel remove <#channel>` - Remove one allowed AI channel.
- `/aichannel list` - Show current AI-allowed channels/scope.
- `/aichannel clear` - Remove channel restrictions (AI allowed in all channels).

### Announcements (Slash-only)
- `/welcome show`
- `/welcome set [channel] [mode] [message] [image] [color]`
- `/welcome edit [message] [color] [mode] [image] [channel]`
- `/welcome test`
- `/welcome preview`

- `/goodbye show`
- `/goodbye set [channel] [mode] [message] [image] [color]`
- `/goodbye edit [message] [color] [mode] [image] [channel]`
- `/goodbye test`
- `/goodbye preview`

Supported template variables for welcome/goodbye messages:
- `{user}`
- `{username}`
- `{avatar}`
- `{server}`
- `{channel}` (configured announcement channel)
- `{channel:rules}` (or channel ID)
- `{role:Member}` (or role ID)
- `{rules}` / `{Member}` / `{123456789012345678}` (auto-detection fallback)

### Moderation: Message Group
- `/message delete <amount>`
- `/message clear [#channel]`
- `/message purgeuser <user|user-id> <amount>`

### Moderation: Channel Group
- `/channel add <channel-name>`
- `/channel delete <#channel>`
- `/channel clear [#channel]`
- `/channel clone [#channel]`
- `/channel lock [#channel]`
- `/channel unlock [#channel]`
- `/channel slowmode <#channel> <seconds|disable>`

### Moderation: User Group
- `/user info <user|user-id>`
- `/user setnick <user> <nickname>`
- `/user mute <user> [reason]`
- `/user unmute <user> [reason]`
- `/user kick <user> [reason]`
- `/user ban <user> [reason]`
- `/user unban <user_id> [reason]`
- `/user tempmute <user> <time> [reason]`
- `/user tempban <user> <time> [reason]`
- `/user warn <user> [reason]`
- `/user unwarn <user> <1|2|3>`
- `/user warnings <user>`
- `/user clearwarnings <user> [reason]`

Temp time format:
- `120s`, `2m`, `3h`, `1d`

### Moderation: Role Group
- `/role add <user> <role|role-id>`
- `/role remove <user> <role|role-id>`
- `/role create <role-name> [hex-color]`

### Color Role Panel (Admin)
- `/color setup`
- `/color list`
- `/color channel <#channel>`
- `/color reload`
- `/color add <hex-color> [name]`
- `/color remove <name>`

### Utility
- `/say <message>` (mod/admin permission required; slash form supports `modo=text|voice`)

### Voice
- Any normal user can explicitly ask Nitori to deliver the current AI response as a native Discord voice message.
- Voice delivery is per-turn only. The next conversation turn returns to text unless voice/audio is requested again in that current message.
- Conversational voice uses the normal AI capability pipeline first, then converts the final response through xAI TTS.
- `/say mensaje:<text> modo:<text|voice>` is still restricted by its existing Manage Messages permission; `modo=voice` sends a native Discord voice message.
- Native voice means a Discord voice-message upload, not Discord `tts=true` and not voice-channel playback.
- Default TTS configuration is Iris with `es-MX`, configurable through `XAI_TTS_VOICE` and `XAI_TTS_LANGUAGE`.
- Supported expressive xAI TTS tags in voice output: `[pause]`, `[long-pause]`, `[hum-tune]`, `[laugh]`, `[chuckle]`, `[giggle]`, `[cry]`, `[tsk]`, `[tongue-click]`, `[lip-smack]`, `[breath]`, `[inhale]`, `[exhale]`, `[sigh]`.

### Reminders
- `/remindme <time> <message>`
- `/unremindme <reminder>`

Reminder units:
- `m`, `h`, `d`, `w`, `mo`, `y`

In chat: ask Nitori to remind you, list your reminders or remove one. Reminders are always your own.

### Birthdays / Anniversaries
Community commands:
- `/birthday set <MM-DD|DD/MM> [birth_year]`
- `/birthday remove`
- `/birthday next [count]`

In chat: "set my birthday to March 3", "remove my birthday", "when are the next birthdays". Setting or removing a birthday only ever changes your own; the settings below are admin-only.

Schedule note:
- Birthday/member/server anniversary events are targeted for `12:00 AM` in the configured server timezone.
- If midnight is missed (restart/outage), the bot retries later the same day until the event is successfully dispatched once.

Admin commands (slash-visible for admins only):
- `/birthday setup [channel] [role]`
- `/birthday channel [#channel]`
- `/birthday role [@role]`
- `/birthday timezone <iana_tz>`
- `/birthday mode <user|server>`
- `/birthday ages <true|false>`
- `/birthday event <default|year|join|server|disable> [color] [image] [message]`
- `/birthday preview <default|year|server|user>`
- `/birthday templateadd <type> <template>`
- `/birthday templatelist <type>`
- `/birthday templateremove <type> <id>`
- `/birthday blacklistuser <user> <true|false>`
- `/birthday blacklistrole <role> <true|false>`
- `/birthday trusted [role] [prevent_message] [prevent_role] [prevent_list]`

### Minecraft
- `/srvstatus <ip-or-domain>`
- In chat, give Nitori the address; it never guesses one.

### Sports (Football)
- `/football live <league>`
- `/football today <league>`
- `/football next <league> [count|team]`
- `/football last <league> <team>`
- `/football table <league>`
- `/football team <league> <team>`
- `/football scorers <league>`
- `/football match <fixture|team>`
- `/football schedule <team|league> [next|last|season]`
- `/football player <player>`
- `/football lineup <fixture_id>`
- `/football stats <fixture_id>`
- `/football injuries <team>`
- `/football transfers <team>`
- `/football h2h <team_a> <team_b>`
- `/football top <scorers|assists|yellowcards|redcards>`
- `/football preview <fixture_id>`
- `/football summary <fixture_id>`

Supported leagues:
- `ligamx`
- `premier`
- `laliga`
- `concacaf`
- `worldcup`

Natural AI football requests are not limited to these short keys. When addressed directly, Nitori can resolve natural names such as Liga MX, Liga de Expansion MX, Premier League, LaLiga, World Cup, clubs, national teams, and player aliases through the shared football resolver before calling API-Football.

The AI football pipeline also supports typed follow-ups from validated football context, fixture result/date questions, match-center details, events, statistics, lineups, fixture-player data, injuries, transfers, top scorers/assists/cards, H2H, and live-watch selection without treating model guesses as facts.

### Fun APIs (API Ninjas)
- `/joke`
- `/dadjoke`
- `/advice`
- `/whois <domain>`
- `/convert <amount> <from_unit> <to_unit>`

All five also work by asking Nitori in plain words.

### Memes
- `/meme help`
- `/meme templates [query]`
- `/meme fonts [query]`
- `/meme create <template> <top_text> [bottom_text]`
- `/meme random <top_text> [bottom_text]`
- `/meme custom <image_url> <top_text> [bottom_text]`
- `/meme custom <top_text> [bottom_text]` + attach image
- `/speech <user>`

In chat, Nitori attaches the finished meme as an image. It can also combine steps, for example translating a phrase and putting the result on a template.

### Code Runner
- `/code code:<code> language:<language> [file]`
- `<prefix>code <language> <code>`
- `/codelangs`
- Supports Discord fenced blocks (example: ```python ...```) with language auto-detection.
- In chat, ask Nitori to run the code you paste; it runs exactly what you supplied.

Supported code languages:
- `c`, `c#`, `cpp`, `python`, `java`, `javascript`, `rust`

Supported source files:
- `.c`, `.cpp`, `.cs`, `.java`, `.js`, `.py`, `.rs`

## Logging and Persistence
- Uses SQLite (`DB_PATH`) for server settings, reminders, warns, temp actions, and context.
- AI responses, structured server memories, trusted behavior rules, and football/image context metadata are persisted where needed for replies and future context.
- Live football watches are in-memory only; bot restart clears active watches.
- Modlog channel receives moderation and selected API/command events.
- Every xAI call writes one journal line such as `AI xAI usage role=nitori-router input=15356 cached=13952 output=1077 reasoning=300 cost_usd=0.0072`, so token use, prompt-cache hits and cost per call are visible. Requests keep their stable parts first and send a per-role cache key so repeated prompt prefixes are billed at the cached rate.
- Data is persistent across bot restarts.

## Deployment (systemd example)
```ini
[Unit]
Description=Nitori Discord Bot
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/opt/nitori-discord-bot
ExecStart=/opt/nitori-discord-bot/.venv/bin/python /opt/nitori-discord-bot/bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## Git Safety (in case you decide to host it yourself)
- Do not commit `.env` (scan bots can steal exposed API keys quickly).
- Keep tokens and secrets out of repository history (even if you later rotate keys).
