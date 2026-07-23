# Nitori Discord Bot

## Why This Bot Exists
Nitori was built as a single, no-paywall Discord bot for communities that want basically an all in one bot. Nitori can chat with you via AI, moderate your server, run your code, check several football leagues, minecraft server status and remind everyone in the server of birthdays, join and server anniversaries. All of this without the need to pay for any of the functions and be able to use them, just add it to your server (or run it yourself), run the /setup command to know what to do and you are on!

## Core Idea
- One bot, all-in-one workflow.
- No premium feature locks inside the bot itself.
- AI only where needed: chat, translation, image understanding/generation/editing, natural football answers, web lookup, and server context.
- Moderation/API features run as native command logic unless a natural-language AI request explicitly needs routing.

## Main Features
- Moderation toolkit (message, user, channel, role, and color-role panel management).
- Natural AI conversation with explicit mention/reply/name anchors, same-user continuation leases, replied-message awareness, pending interaction changes, reaction-only requests, and safe noisy-channel replies.
- AI image generation, image analysis, and image editing from current or replied image messages.
- AI football answers grounded in API-Football data, including teams, players, leagues, fixtures, standings, scorers, injuries, transfers, match events, stats, lineups, summaries, previews, and live-watch updates.
- Optional xAI web search for current external information and API-Football gaps.
- Structured server memory and trusted owner/admin behavior rules for server-specific style and context.
- Translation command with multiple target languages.
- Memes via Memegen API + local speech meme generator.
- API Ninjas commands: joke, dadjoke, advice, whois, unit conversion.
- Minecraft status checks (mcsrvstat.us).
- Football stats and match data from API-Football.
- Code execution (Glot API) for common languages.
- Reminder system with DM and optional channel mention.
- Welcome/Goodbye configurable announcement modules.
- Birthday + anniversary module (community self-service + admin automation controls).
- Per-server settings (prefix, language, AI channel scope, modlog).

## Requirements
- Python 3.11+
- Discord bot token
- xAI API key (chat/translate/context/image analysis/image generation/image editing/optional web search)
- API Ninjas key (joke/dadjoke/advice/whois/convert)
- API-Football key (supported football leagues)
- Glot API token (/code)

## Environment Variables
Use `.env.example` as base.

Required for full feature set:
- `DISCORD_TOKEN`
- `XAI_API_KEY`
- `API_NINJAS_KEY`
- `API_FOOTBALL_KEY`
- `GLOT_API_TOKEN`

Common optional settings:
- `XAI_MODEL` (default model)
- `XAI_VISION_MODEL` (optional image-capable model for AI chat attachments; defaults to `XAI_MODEL`)
- `XAI_IMAGE_MODEL` (optional image generation/editing model; defaults to `grok-imagine-image-quality`)
- `XAI_WEB_SEARCH_ENABLED` (default `false`; enable only when deployed bot should use xAI web search)
- `XAI_X_SEARCH_ENABLED` (default `false`; reserved for explicit X/social search support)
- `XAI_WEB_SEARCH_MAX_SOURCES` (default `3`)
- `XAI_WEB_SEARCH_COOLDOWN_SECONDS` (default `30`)
- `DB_PATH`
- `DEFAULT_PREFIX`
- `GLOT_BASE_URL`
- `GLOT_API_MODE`
- `API_FOOTBALL_BASE_URL`

## Local Run
```bash
python -m venv .venv
# Windows
.\.venv\Scripts\activate
# Linux
# source .venv/bin/activate

pip install -r requirements.txt
python bot.py
```

## Command Reference
`<prefix>` means your configured text prefix (default from env/db). Most commands support both slash and prefix, except where noted.

### General / AI
- `/help [section]`
- `/setup`
- `/translate <language> [text]`
- Mention the bot, reply to one of its AI messages, or continue a same-user conversation while the lease is fresh. Nitori avoids channel-wide passive listening, command traffic, slash-command output replies, and side conversations.
- Nitori can read supported image attachments (`jpg`, `jpeg`, `png`) from the current message or replied-to message, understand replied text/embed context when directly addressed, and react with a relevant emoji when requested.
- Ask Nitori directly to draw/create/generate an image, edit an attached/replied image, or analyze an image. Generated and edited images are uploaded directly in Discord.
- Ask Nitori natural football questions. It plans the request, validates teams/leagues/players/fixtures through API-Football, and answers with grounded data instead of raw model guesses.
- Ask Nitori to follow a live match with minute-by-minute updates. One AI-started watch can run per channel; updates are compact and stop at full time or timeout.
- Ask current web/freshness questions only when web search is enabled. Normal chat never uses web tools automatically.
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
- `/say <message>` (mod/admin permission required)

### Reminders
- `/remindme <time> <message>`
- `/unremindme <reminder>`

Reminder units:
- `m`, `h`, `d`, `w`, `mo`, `y`

### Birthdays / Anniversaries
Community commands:
- `/birthday set <MM-DD|DD/MM> [birth_year]`
- `/birthday remove`
- `/birthday next [count]`

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

### Fun APIs (API Ninjas)
- `/joke`
- `/dadjoke`
- `/advice`
- `/whois <domain>`
- `/convert <amount> <from_unit> <to_unit>`

### Memes
- `/meme help`
- `/meme templates [query]`
- `/meme fonts [query]`
- `/meme create <template> <top_text> [bottom_text]`
- `/meme random <top_text> [bottom_text]`
- `/meme custom <image_url> <top_text> [bottom_text]`
- `/meme custom <top_text> [bottom_text]` + attach image
- `/speech <user>`

### Code Runner
- `/code code:<code> language:<language> [file]`
- `<prefix>code <language> <code>`
- `/codelangs`
- Supports Discord fenced blocks (example: ```python ...```) with language auto-detection.

Supported code languages:
- `c`, `c#`, `cpp`, `python`, `java`, `javascript`, `rust`

Supported source files:
- `.c`, `.cpp`, `.cs`, `.java`, `.js`, `.py`, `.rs`

## Logging and Persistence
- Uses SQLite (`DB_PATH`) for server settings, reminders, warns, temp actions, and context.
- AI responses, structured server memories, trusted behavior rules, and football/image context metadata are persisted where needed for replies and future context.
- Live football watches are in-memory only; bot restart clears active watches.
- Modlog channel receives moderation and selected API/command events.
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
