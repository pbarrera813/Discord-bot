# Nitori Discord Bot

## Why This Bot Exists
Nitori was built as a single, no-paywall Discord bot for communities that want basically an all in one bot. Nitori can chat with you via AI, moderate your server, run your code, check several football leagues, minecraft server status and remind everyone in the server of birthdays, join and server anniversaries. All of this without the need to pay for any of the functions and be able to use them, just add it to your server (or run it yourself), run the /setup command to know what to do and you are on!

## Core Idea
- One bot, all-in-one workflow.
- No premium feature locks inside the bot itself.
- AI only where needed (chat and translation).
- Moderation/API features run as native command logic (no LLM required).

## Main Features
- Moderation toolkit (message, user, channel, role, and color-role panel management).
- AI conversation with mention/reply triggers, context memory, and server context summaries.
- Translation command with multiple target languages.
- Memes via Memegen API + local speech meme generator.
- API Ninjas commands: joke, dadjoke, advice, whois, unit conversion.
- Minecraft status checks (mcsrvstat.us).
- Liga BBVA MX stats (API-Football).
- Code execution (Glot API) for common languages.
- Reminder system with DM and optional channel mention.
- Welcome/Goodbye configurable announcement modules.
- Birthday + anniversary module (community self-service + admin automation controls).
- Per-server settings (prefix, language, AI channel scope, modlog).

## Requirements
- Python 3.11+
- Discord bot token
- xAI API key (chat/translate/context)
- API Ninjas key (joke/dadjoke/advice/whois/convert)
- API-Football key (Liga MX)
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
- `DB_PATH`
- `DEFAULT_PREFIX`
- `GLOT_BASE_URL`
- `GLOT_API_MODE`
- `API_FOOTBALL_BASE_URL`
- `LIGAMX_LEAGUE_ID`

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
- Mention bot + message, or reply to bot message, to chat
- `/setservercontext <#channel>` (admin/manage guild)
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
- `{channel}`
- `{#channel_name}`
- `{role_name}`

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
- Birthday/member/server anniversary announcement events are posted at `12:00 AM` bot local time.

Admin commands (slash-visible for admins only):
- `/birthday setup [channel] [role]`
- `/birthday channel [#channel]`
- `/birthday role [@role]`
- `/birthday timezone <iana_tz>`
- `/birthday mode <user|server>`
- `/birthday ages <true|false>`
- `/birthday event <default|year|join|server|disable> [color] [image] [message]`
- `/birthday templateadd <event_type> <template_text>`
- `/birthday templatelist <event_type>`
- `/birthday templateremove <event_type> <template_id>`
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

Supported leagues:
- `ligamx`
- `premier`
- `laliga`
- `concacaf`

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
- `/code code:<code> language:<language> [source_file]`
- `<prefix>code <language> <code>`
- `/codelangs`

Supported code languages:
- `c`, `c#`, `cpp`, `python`, `java`, `javascript`, `rust`

Supported source files:
- `.c`, `.cpp`, `.cs`, `.java`, `.js`, `.py`, `.rs`

## Logging and Persistence
- Uses SQLite (`DB_PATH`) for server settings, reminders, warns, temp actions, and context.
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
