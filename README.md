# OnBoardarr

OnBoardarr automates Discord member onboarding for Plex and Wizarr. It can
create invitations, reconcile accepted invites, maintain Discord-to-Plex
account mappings, and send configurable setup guidance to new members.

## Requirements

- Python 3.12
- `discord.py`; install `aiohttp` to enable the Wizarr webhook listener
- A Discord application/bot and a configured Wizarr instance
- Plex credentials and writable persistent storage for invite state/mappings

Install the basic dependency with:

```sh
python -m pip install discord.py aiohttp
```

## Configuration

Provide settings in the dotenv-style file passed with `--env-file` (default:
`/opt/onboardarr/.env`) or through the process environment. At minimum,
configure `DISCORD_BOT_TOKEN`, `WIZARR_API_BASE_URL`, `WIZARR_API_KEY`,
`WIZARR_DB_FILE`, `PLEX_URL`, and `PLEX_TOKEN`.

Useful optional settings include:

- `PLEX_SERVER_NAME`, `PLEX_INVITE_URL`, and `WIZARR_PUBLIC_BASE_URL`
- `WIZARR_MAPPING_FILE` and `PENDING_WIZARR_INVITES_FILE`
- `MUSIC_SERVER_ENV_FILE` and `MUSIC_SERVER_COMPOSE_FILE` for integrations
  that update and restart a separate music service
- `SEERR_URL`, `AUDIOBOOK_REQUEST_URL`, `DISCORD_MAIN_CHANNEL`,
  `DISCORD_BUG_CHANNEL`, and `DISCORD_BUG_CHANNEL_ID` for welcome messages
- `WIZARR_WEBHOOK_PORT` and `WIZARR_WEBHOOK_PATH`

Mount secrets and persistent data separately from the source tree. Do not
commit `.env`, invite records, account mappings, or service credentials.

## Helpers and tests

`sync_plex_invite_mapping.py` reconciles invite targets with the Discord-to-Plex
mapping. `watch_wizarr_invite.py` is a retrying wrapper for that helper.

Run the mapping tests with:

```sh
python -m pytest -q
```

## License

MIT. See [LICENSE](LICENSE).
