# Onboardarr

Onboardarr automates Discord member onboarding for Plex and Wizarr. It creates
and reconciles invitations, maintains Discord-to-Plex account mappings, and
sends configurable setup guidance to new members.

## Requirements

- Python 3.12
- A Discord application and bot added to the target server
- A reachable Plex server and Wizarr instance
- Access to the Wizarr database file and writable storage for mapping files

Install dependencies:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install discord.py plexapi aiohttp
```

`aiohttp` is needed for the optional Wizarr webhook listener.

## Configure and run

Create a private dotenv-style file, for example `.env`, and set the service
credentials and endpoints:

```dotenv
DISCORD_BOT_TOKEN=replace-with-your-bot-token
DISCORD_GUILD_ID=replace-with-your-server-id
DISCORD_CLIENT_ID=replace-with-your-application-id

WIZARR_API_BASE_URL=http://wizarr:5690
WIZARR_API_KEY=replace-with-your-wizarr-api-key
WIZARR_DB_FILE=/path/to/wizarr/database.db

PLEX_URL=http://plex:32400
PLEX_TOKEN=replace-with-your-plex-token
PLEX_SERVER_NAME=Plex

# Optional Telegram admin bot
TELEGRAM_BOT_TOKEN=replace-with-a-dedicated-telegram-bot-token
TELEGRAM_ADMIN_CHAT_IDS=replace-with-your-telegram-chat-id
```

The database path must be readable by the process. The default mapping paths
are `/opt/wizarr/plex_invite_targets.json` and
`/opt/wizarr/pending_wizarr_invites.json`; configure
`WIZARR_MAPPING_FILE` and `PENDING_WIZARR_INVITES_FILE` if those locations are
not persistent or writable in your deployment.

Start the bot:

```sh
python onboardarr.py --env-file .env
```

The default environment file is `/opt/onboardarr/.env`. The bot provides the
`/plex-invite` command to the server owner and `/reset-welcome` to the server
owner or an administrator. `/plex-invite` offers English, Spanish, French, and
German for the Plex/Wizarr invite message sent to the next person who joins;
English is the default. The longer onboarding guide remains in English. The
bot also sends setup instructions to new members and processes pending Wizarr
invitations in the background.

If existing-member reconciliation requires Discord member/message access,
enable `DISCORD_ENABLE_PRIVILEGED_INTENTS=true` and enable the corresponding
privileged intents for the bot in the Discord Developer Portal. Keep the
setting off if those intents are not needed.

## Telegram admin bot

Telegram support is optional. Configure a bot token and numeric
`TELEGRAM_ADMIN_CHAT_IDS` (comma-separated for multiple admin chats). Telegram
admins can use `/plex-invite [days] [language]` to create the same single-use
Discord invite as the Discord command. For example, `/plex-invite 14 es`
creates a 14-day invite and sends the next joiner a Spanish Plex/Wizarr invite
message. Supported language codes are `en`, `es`, `fr`, and `de`; omitting the
language uses English. `/status` and `/help` show basic bot information.
`/reset-welcome` resends the current onboarding instructions to
all Discord members; use it carefully because it deletes and replaces the
bot's onboarding DMs. Onboardarr sends Telegram admins a notice when a Discord
member joins and whether the Wizarr invite was generated.

To find a chat ID, start the bot and message it once; an unauthorized chat's
numeric ID is printed to the Onboardarr log. Add the ID to the allow-list and
restart. Telegram commands are restricted to this list. Remove any webhook
configured for the bot because Onboardarr uses long polling. Use a different
bot token from Debugarr and Checkrr if those integrations are enabled; they
also poll Telegram updates.

## Mapping helper

`sync_plex_invite_mapping.py` reconciles invite targets with the
Discord-to-Plex mapping. `watch_wizarr_invite.py` retries that helper when
needed. To run a one-time mapping reconciliation without starting the bot:

```sh
python onboardarr.py --env-file .env --dry-run
```

Despite the option name, this command writes the reconciled mapping outputs;
it is not a read-only preview. Back up existing mapping files before using it
if you need to preserve their current contents.

## Optional settings

- `PLEX_INVITE_URL` and `WIZARR_PUBLIC_BASE_URL` for public invite links.
- `WIZARR_SERVER_ID` when the server ID should be specified explicitly.
- `WIZARR_WEBHOOK_PORT` and `WIZARR_WEBHOOK_PATH` for the optional webhook
  listener (defaults: `8081` and `/wizarr/webhook`).
- `DISCORD_MAIN_CHANNEL`, `DISCORD_BUG_CHANNEL`, and
  `DISCORD_BUG_CHANNEL_ID` for welcome-message destinations.
- `SEERR_URL` and `AUDIOBOOK_REQUEST_URL` for links in welcome messages.
- `MUSIC_SERVER_ENV_FILE` and `MUSIC_SERVER_COMPOSE_FILE` when using the
  optional music-service mapping integration.

Restrict webhook access to trusted services. Keep credentials, invite records,
and account mappings out of version control.

## Docker image

Each push to `main` publishes a multi-architecture image to GitHub Container
Registry. Pull and run the image with a mounted environment file and persistent
Wizarr storage:

```sh
docker pull ghcr.io/epik-win/onboardarr:latest
docker run -d --name onboardarr \
  --restart unless-stopped \
  -v "$PWD/.env:/run/secrets/onboardarr.env:ro" \
  -v /path/to/wizarr:/opt/wizarr \
  -v /path/to/music-server.env:/opt/music_server.env:ro \
  ghcr.io/epik-win/onboardarr:latest
```

The image is also tagged with the source commit SHA for immutable deployments.
Keep credentials and writable application data outside the image.

## Tests

```sh
python -m pytest -q
```

## License

MIT. See [LICENSE](LICENSE).
