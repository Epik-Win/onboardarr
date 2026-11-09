#!/usr/bin/env python3
"""OnBoardarr: sync Wizarr invite targets into musicai and onboard Discord members."""
from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import discord
from discord.ext import commands

# Lazy import for aiohttp will be done when starting the webhook server

DEFAULT_ENV_FILE = Path("/opt/onboardarr/.env")
DEFAULT_MAPPING_FILE = Path("/opt/wizarr/plex_invite_targets.json")
DEFAULT_PENDING_INVITES_FILE = Path("/opt/wizarr/pending_wizarr_invites.json")
DEFAULT_PLEX_URL = os.environ.get("PLEX_URL", "http://localhost:32400")
DEFAULT_PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
DEFAULT_COMPOSE_FILE = Path("/opt/music_server.docker-compose.yml")
DEFAULT_MUSIC_SERVER_ENV_FILE = Path("/opt/music_server.env")


def telegram_api_call(token: str, method: str, payload: Dict[str, Any]) -> Any:
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=urlencode(payload).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen(request, timeout=35) as response:
            result = json.loads(response.read().decode())
    except HTTPError as exc:
        raise RuntimeError(f"Telegram {method} request failed with HTTP {exc.code}") from None
    except URLError as exc:
        raise RuntimeError(f"Telegram {method} request failed: {exc.reason}") from None
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method} request failed: {result}")
    return result.get("result")


def parse_telegram_chat_ids(value: str) -> set[int]:
    chat_ids = set()
    for item in value.split(","):
        item = item.strip()
        if item:
            try:
                chat_ids.add(int(item))
            except ValueError as exc:
                raise ValueError(f"Invalid Telegram chat ID: {item}") from exc
    return chat_ids


def music_server_env_file() -> Path:
    return Path(os.getenv("MUSIC_SERVER_ENV_FILE", str(DEFAULT_MUSIC_SERVER_ENV_FILE)))


def music_server_compose_file() -> Path:
    return Path(os.getenv("MUSIC_SERVER_COMPOSE_FILE", str(DEFAULT_COMPOSE_FILE)))


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Always apply values from the env file so the file is authoritative
        os.environ[key] = value


def load_musicai_env(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not os.getenv(key):
            os.environ[key] = value


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def fetch_json(url: str, headers: Dict[str, str], payload: Optional[object] = None, timeout: int = 15, method: str = "GET") -> object:
    try:
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError, URLError
    except ImportError as exc:
        raise RuntimeError("Could not import urllib.request") from exc

    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers = {**headers, "Content-Type": "application/json"}
        if method == "GET":
            method = "POST"

    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
    except (HTTPError, URLError, ValueError) as exc:
        raise RuntimeError(f"Failed to fetch JSON from {url}: {exc}") from exc


def ensure_wizarr_api_key(db_path: Path, created_by_id: int = 1) -> str:
    """Create a new API key in Wizarr's DB and return it."""
    if not db_path.exists():
        raise FileNotFoundError(f"Wizarr database not found at {db_path}")

    api_key = secrets.token_urlsafe(32)
    key_hash = hash_api_key(api_key)
    connection = sqlite3.connect(str(db_path), timeout=10)
    try:
        cursor = connection.cursor()
        cursor.execute(
            "INSERT INTO api_key (name, key_hash, created_at, created_by_id, is_active) VALUES (?, ?, datetime('now'), ?, 1)",
            ("onboardarr", key_hash, created_by_id),
        )
        connection.commit()
    finally:
        connection.close()
    return api_key


def resolve_wizarr_api_key(db_path: Path, env_api_key: Optional[str]) -> str:
    if env_api_key:
        return env_api_key
    if not db_path.exists():
        raise FileNotFoundError(f"Wizarr database not found at {db_path}")

    connection = sqlite3.connect(str(db_path), timeout=10)
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT id FROM admin_account ORDER BY id LIMIT 1")
        admin_row = cursor.fetchone()
        if not admin_row:
            raise RuntimeError("No admin account found in Wizarr database")
        admin_id = admin_row[0]
    finally:
        connection.close()

    return ensure_wizarr_api_key(db_path, created_by_id=admin_id)


def resolve_wizarr_server_id(db_path: Path, env_server_id: Optional[str]) -> int:
    if env_server_id:
        try:
            return int(env_server_id)
        except ValueError as exc:
            raise ValueError("WIZARR_SERVER_ID must be an integer") from exc

    if not db_path.exists():
        raise FileNotFoundError(f"Wizarr database not found at {db_path}")

    connection = sqlite3.connect(str(db_path), timeout=10)
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT id FROM media_server WHERE verified = 1 ORDER BY id LIMIT 1")
        server_row = cursor.fetchone()
        if not server_row:
            raise RuntimeError("No verified Wizarr media server found")
        return int(server_row[0])
    finally:
        connection.close()


def _find_wizarr_server_name(server_data: object, server_id: int) -> Optional[str]:
    if isinstance(server_data, dict):
        for key in ("data", "results", "media_servers", "servers"):
            if key in server_data and isinstance(server_data[key], list):
                name = _find_wizarr_server_name(server_data[key], server_id)
                if name:
                    return name

        if server_data.get("id") in (server_id, str(server_id)):
            return (
                server_data.get("name")
                or server_data.get("title")
                or server_data.get("display_name")
                or server_data.get("server_name")
            )
        return None

    if isinstance(server_data, list):
        for item in server_data:
            if not isinstance(item, dict):
                continue
            if item.get("id") in (server_id, str(server_id)):
                return (
                    item.get("name")
                    or item.get("title")
                    or item.get("display_name")
                    or item.get("server_name")
                )
        return None

    return None


def resolve_wizarr_server_name(api_base_url: str, api_key: str, server_id: int, env_server_name: Optional[str], db_path: Optional[Path]) -> str:
    if env_server_name:
        return env_server_name.strip()

    if api_key and api_base_url:
        for endpoint in (
            "/api/media_servers",
            "/api/servers",
            f"/api/media_servers/{server_id}",
            f"/api/servers/{server_id}",
        ):
            try:
                response = fetch_json(f"{api_base_url.rstrip('/')}{endpoint}", {"X-API-Key": api_key}, None, timeout=20)
                name = _find_wizarr_server_name(response, server_id)
                if name:
                    return name
            except Exception:
                continue

    if db_path and db_path.exists():
        connection = sqlite3.connect(str(db_path), timeout=10)
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT name FROM media_server WHERE verified = 1 AND id = ? LIMIT 1", (server_id,))
            row = cursor.fetchone()
            if row and row[0]:
                return str(row[0])
        finally:
            connection.close()

    return env_server_name or "Plex"


def format_wizarr_invite_url(base_url: str, invite_url: str) -> str:
    invite_url = invite_url.strip()
    if not invite_url:
        raise ValueError("Invalid invitation URL returned by Wizarr")
    if invite_url.startswith("http://") or invite_url.startswith("https://"):
        if base_url:
            parsed = urlparse(invite_url)
            path = parsed.path or ""
            query = f"?{parsed.query}" if parsed.query else ""
            fragment = f"#{parsed.fragment}" if parsed.fragment else ""
            return f"{base_url.rstrip('/')}{path}{query}{fragment}"
        return invite_url
    if not base_url:
        return invite_url
    return f"{base_url.rstrip('/')}/{invite_url.lstrip('/')}"


def list_wizarr_invitations(api_base_url: str, api_key: str) -> List[Dict[str, object]]:
    response = fetch_json(
        f"{api_base_url.rstrip('/')}/api/invitations",
        {"X-API-Key": api_key},
        timeout=20,
        method="GET",
    )

    invitations = []
    if isinstance(response, dict):
        for candidate in ("data", "invitations", "results", "items", "invitation"):
            value = response.get(candidate)
            if isinstance(value, list):
                invitations = value
                break
            if isinstance(value, dict) and candidate != "invitation":
                invitations = [value]
                break
    elif isinstance(response, list):
        invitations = response

    return [inv for inv in invitations if isinstance(inv, dict)]


def delete_wizarr_invitation(api_base_url: str, api_key: str, invitation_id: str) -> None:
    fetch_json(
        f"{api_base_url.rstrip('/')}/api/invitations/{invitation_id}",
        {"X-API-Key": api_key},
        timeout=20,
        method="DELETE",
    )


def cleanup_wizarr_invitations(api_base_url: str, api_key: str) -> None:
    try:
        invitations = list_wizarr_invitations(api_base_url, api_key)
    except Exception:
        return

    for invitation in invitations:
        invitation_id = None
        for key in ("id", "invitation_id", "invite_id"):
            if invitation.get(key):
                invitation_id = str(invitation[key])
                break

        if not invitation_id and isinstance(invitation.get("url"), str):
            parsed = urlparse(invitation["url"])
            if parsed.path:
                invitation_id = parsed.path.rstrip("/").split("/")[-1]

        if invitation_id:
            try:
                delete_wizarr_invitation(api_base_url, api_key, invitation_id)
            except Exception:
                pass


def create_wizarr_invite(api_base_url: str, api_key: str, server_id: int, duration_days: Optional[int] = None) -> Dict[str, object]:
    payload = {
        "server_ids": [server_id],
        "library_ids": [],
        "allow_downloads": False,
        "allow_live_tv": False,
        "allow_mobile_uploads": False,
        "use_global_setting": True,
    }
    if duration_days is None:
        payload.update({
            "duration": "unlimited",
            "unlimited": True,
        })
    else:
        payload.update({
            "expires_in_days": duration_days,
            "duration": "temporary",
            "unlimited": False,
        })

    response = fetch_json(
        f"{api_base_url.rstrip('/')}/api/invitations",
        {"X-API-Key": api_key},
        payload=payload,
        timeout=20,
    )
    invitation = response.get("invitation") if isinstance(response, dict) else None
    if not invitation or not isinstance(invitation, dict) or "url" not in invitation:
        raise RuntimeError("Wizarr invitation creation returned unexpected response")
    return invitation


def _extract_invitation_code(invite_url: str) -> str:
    parsed = urlparse(invite_url)
    code = parsed.path.rstrip("/").split("/")[-1]
    if not code:
        raise ValueError(f"Could not parse invitation code from {invite_url}")
    return code


def _parse_datetime(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        return value
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def load_wizarr_users(api_base_url: str, api_key: str) -> Dict[str, Dict[str, object]]:
    try:
        response = fetch_json(
            f"{api_base_url.rstrip('/')}/api/users?limit=1000",
            {"X-API-Key": api_key},
            timeout=20,
            method="GET",
        )
    except Exception:
        return {}

    users = []
    if isinstance(response, dict):
        users_value = response.get("users")
        if isinstance(users_value, list):
            users = users_value
    elif isinstance(response, list):
        users = response

    result: Dict[str, Dict[str, object]] = {}
    for user in users:
        if not isinstance(user, dict):
            continue
        username = str(user.get("username", "")).strip()
        if not username:
            continue
        result[username.lower()] = {
            "username": username,
            "id": user.get("id"),
            "email": user.get("email"),
            "created_at": user.get("created_at"),
        }
    return result


def build_discord_to_plex_mapping(plex_to_discord: Dict[str, str]) -> Dict[str, str]:
    return {
        str(discord_user).strip().lower(): str(plex_user).strip()
        for plex_user, discord_user in plex_to_discord.items()
        if str(plex_user).strip() and str(discord_user).strip()
    }


def load_existing_mapping(env_path: Path) -> Dict[str, str]:
    if not env_path.exists():
        return {}

    for line in env_path.read_text().splitlines():
        if line.startswith("DISCORD_TO_PLEX_MAPPING_JSON="):
            value = line.split("=", 1)[1].strip()
            if not value:
                return {}
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return {
                        str(k).strip().lower(): str(v).strip()
                        for k, v in parsed.items()
                        if str(k).strip() and str(v).strip()
                    }
            except json.JSONDecodeError:
                return {}
    return {}


def update_env_file(env_path: Path, mapping: Dict[str, str]) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(mapping, separators=(",", ":"), sort_keys=True)
    if env_path.exists():
        text = env_path.read_text()
    else:
        text = ""

    pattern = re.compile(r"^DISCORD_TO_PLEX_MAPPING_JSON=.*$", re.MULTILINE)
    replacement = f"DISCORD_TO_PLEX_MAPPING_JSON={payload}"
    if pattern.search(text):
        text = pattern.sub(replacement, text)
    else:
        text = f"{text}\n{replacement}\n" if text else f"{replacement}\n"

    env_path.write_text(text)


def load_invite_targets(mapping_path: Path) -> Dict[str, str]:
    if not mapping_path.exists():
        return {}

    try:
        payload = json.loads(mapping_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Could not parse {mapping_path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {mapping_path}")

    return {
        str(plex_user).strip().lower(): str(discord_user).strip()
        for plex_user, discord_user in payload.items()
        if str(plex_user).strip() and str(discord_user).strip()
    }


def save_invite_targets(mapping_path: Path, discord_to_plex: Dict[str, str]) -> None:
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    plex_to_discord = {
        str(plex_user).strip(): str(discord_user).strip()
        for discord_user, plex_user in discord_to_plex.items()
        if str(plex_user).strip() and str(discord_user).strip()
    }
    mapping_path.write_text(json.dumps(plex_to_discord, indent=2, sort_keys=True) + "\n")


def load_pending_wizarr_invites(path: Path) -> List[Dict[str, object]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def save_pending_wizarr_invites(path: Path, invites: List[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(invites, indent=2, sort_keys=True) + "\n")


def append_pending_wizarr_invite(path: Path, invite_record: Dict[str, object]) -> None:
    invites = load_pending_wizarr_invites(path)
    invites.append(invite_record)
    save_pending_wizarr_invites(path, invites)


def resolve_invited_plex_username(db_path: Path, invite_code: str) -> Optional[str]:
    if not db_path.exists():
        return None

    connection = sqlite3.connect(str(db_path), timeout=10)
    try:
        cursor = connection.cursor()
        cursor.execute(
            'SELECT u.username FROM invitation i '
            'JOIN invitation_user iu ON iu.invite_id = i.id '
            'JOIN "user" u ON u.id = iu.user_id '
            'WHERE i.code = ? '
            'LIMIT 1',
            (invite_code,),
        )
        row = cursor.fetchone()
        if row and row[0]:
            return str(row[0]).strip()
    except Exception:
        pass
    finally:
        connection.close()
    return None


def discover_new_users(plex_url: str, plex_token: str) -> Dict[str, str]:
    if not plex_url or not plex_token:
        return {}

    headers = {"X-Plex-Token": plex_token}
    try:
        data = fetch_json(f"{plex_url}/accounts", headers=headers, timeout=15)
    except Exception:
        return {}

    users = []
    if isinstance(data, dict):
        accounts = data.get("MediaContainer", {}).get("Account", [])
        if isinstance(accounts, dict):
            accounts = [accounts]
        users = [user.get("title") for user in accounts if isinstance(user, dict)]
    elif isinstance(data, list):
        users = [item.get("title") for item in data if isinstance(item, dict)]

    return {user.lower(): user for user in users if user}


def merge_mappings(existing: Dict[str, str], invite_targets: Dict[str, str], discovered_users: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    if invite_targets:
        merged = build_discord_to_plex_mapping({plex: discord_name for plex, discord_name in invite_targets.items()})
        if discovered_users:
            merged.update({discord_name: plex_name for discord_name, plex_name in existing.items() if discord_name not in merged})
        return merged

    if discovered_users:
        inferred = {
            user.lower(): user
            for user in discovered_users.values()
            if user and user.strip()
        }
        inferred.update(existing)
        return inferred

    return dict(existing)


def sync_mapping(env_path: Path, mapping_path: Path, plex_url: str, plex_token: str, restart_service: Optional[str] = None) -> Dict[str, str]:
    existing = load_existing_mapping(env_path)
    invite_targets = load_invite_targets(mapping_path)
    discovered = discover_new_users(plex_url, plex_token) if plex_url and plex_token else {}
    merged = merge_mappings(existing, invite_targets, discovered)

    if merged:
        save_invite_targets(mapping_path, merged)

    update_env_file(env_path, merged)

    if restart_service:
        docker_bin = shutil.which("docker")
        if docker_bin:
            subprocess.run(
                [docker_bin, "compose", "-f", str(music_server_compose_file()), "restart", restart_service],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

    return merged


def build_welcome_message(channel_reference: str = "#music_request", bug_channel_reference: str = "#general") -> tuple[str, str, str, str, str]:
    seerr_url = os.getenv("SEERR_URL", "https://seerr.example.com").rstrip("/")
    audiobook_url = os.getenv(
        "AUDIOBOOK_REQUEST_URL", "https://audiobooks.example.com",
    ).rstrip("/")
    server_intro_message = (
        "👋 **Welcome to Media it up!**\n\n"

        "This is a private streaming service for friends and family. You can watch movies and TV, "
        "listen to music and audiobooks, and request things to add to the library.\n\n"

        "The next messages walk you through the apps, making requests, adding request websites "
        "to your phone's home screen, watching together, and getting help. Read them in order. "
        "If anything is confusing, ask the server admin for help."
    )

    main_message = (
        "📱 **1. Install the apps you want to use**\n"
        "Tap the link for your phone to open its app store, then install the app. You do not need "
        "every app; choose the ones for the things you want to watch or listen to.\n\n"

        "🎬 **Plex** (Movies & TV Shows)\n"
        "   • iPhone: <https://apps.apple.com/app/plex-watch-live-tv-and-movies/id383457673>\n"
        "   • Android: <https://play.google.com/store/apps/details?id=com.plexapp.android>\n"
        "   Open Plex and sign in with the Plex account you used to accept the server invite.\n\n"

        "🎵 **Plexamp** (Music)\n"
        "   • iPhone: <https://apps.apple.com/app/plexamp/id1500797510>\n"
        "   • Android: <https://play.google.com/store/apps/details?id=tv.plex.labs.plexamp>\n"
        "   Sign in with the same Plex account.\n\n"

        "📚 **Audiobooks for Plex**\n\n"
        "   **iPhone - Prologue:**\n"
        "   • Prologue plays audiobooks from Plex. The free version streams and syncs progress; "
        "paid features include downloads, Apple Watch, CarPlay, bookmarks, and a sleep timer.\n"
        "   • Download: <https://apps.apple.com/app/prologue/id1459223267>\n\n"

        "   **Android - Chronicle Epilogue:**\n"
        "   • Chronicle plays audiobooks from Plex and supports downloads and progress sync.\n"
        "   • Download: <https://play.google.com/store/apps/details?id=local.oss.chronicle>\n\n"

        "   **Android Alternative - Symfonium:**\n"
        "   • Paid app for music and audiobooks: "
        "<https://play.google.com/store/apps/details?id=app.symfonik.music.player>\n\n"

        "📝 **2. Request movies and TV shows**\n"
        f"   1. Open <{seerr_url}>.\n"
        "   2. Search for the movie or show and select the right title.\n"
        "   3. Choose **Request** and follow any choices shown on screen.\n\n"

        "📚 **3. Request audiobooks and music**\n"
        "   **Audiobooks:**\n"
        f"   Open <{audiobook_url}>, search for the book, select it, and follow "
        "the on-screen request steps.\n\n"

        "   **Music:**\n"
        f"   Share a YouTube or Spotify song/playlist link in {channel_reference}. "
        "You can also share a Shazam link for a song (not a playlist)."
    )

    mobile_shortcut_message = (
        "📱 **4. Add the REQUEST websites to your phone's home screen**\n"
        "These shortcuts are for making requests: Seerr for movies and TV, and the audiobook "
        "request website. They do not replace the Plex, Plexamp, or audiobook player apps.\n\n"
        "First, open the request website in your phone's browser:\n"
        f"• Movies and TV: <{seerr_url}>\n"
        f"• Audiobooks: <{audiobook_url}>\n\n"

        "🤖 **Android (Chrome)**\n"
        "1. Open one of the request websites above in Chrome.\n"
        "2. Tap the three-dot menu in the top-right corner.\n"
        "3. Tap **Add to Home screen** (or **Install app**, if shown), then confirm.\n"
        "4. Repeat for the other request website if you want both shortcuts.\n\n"

        "🍏 **iPhone (Safari)**\n"
        "1. Open one of the request websites above in Safari.\n"
        "2. Tap **Share** (the square with an up arrow).\n"
        "3. Scroll down and tap **Add to Home Screen**, then tap **Add**.\n"
        "4. Repeat for the other request website if you want both shortcuts."
    )

    watch_party_message = (
        "🍿 **5. Watch together**\n\n"
        "**Plex Watch Together** keeps the video in sync for everyone:\n"
        "1. Open Plex and choose the movie or TV episode you want to watch.\n"
        "2. Open its menu and choose **Watch Together**.\n"
        "3. Invite your friends to the session and start watching.\n\n"
        "**Discord Watch Party** is the voice chat for watching together:\n"
        "Join the open **Watch Party** voice channel in this Discord server to talk while you watch. "
        "You can use it alongside Plex Watch Together."
    )

    bug_report_message = (
        "🐛 **6. Report a problem or ask for help**\n\n"
        "**For a general bug:**\n"
        f"1. Open {bug_channel_reference} in Discord.\n"
        "2. Type `/report-bug` and choose the command from the menu.\n"
        "3. Fill in the form and submit it. The report is shared in the channel so members can vote, "
        "and the admin is notified.\n\n"
        "**For a movie or TV playback problem:**\n"
        f"1. Open <{seerr_url}> and find the movie or show.\n"
        "2. Open its page and tap/click the ⚠️ issue-report button.\n"
        "3. Describe what went wrong and submit the report; the title details are sent with it.\n\n"
        "If you cannot find the command or a button, contact the admin in Discord."
    )

    return server_intro_message, main_message, mobile_shortcut_message, watch_party_message, bug_report_message



def build_discord_invite_url(client_id: str) -> str:
    client_id = client_id.strip()
    if not client_id:
        return ""
    return (
        f"https://discord.com/api/oauth2/authorize?client_id={client_id}"
        "&permissions=277025520704&scope=bot%20applications.commands"
    )


def attempt_promote_to_plex_home(plex_url: str, plex_token: str, plex_username: str) -> bool:
    """Best-effort attempt to promote a Plex account to Plex Home.

    Plex API for managing Home users varies by server version; this is a
    best-effort implementation that tries common endpoints and gracefully
    logs failures. Returns True if the promotion appears successful.
    """
    if not plex_url or not plex_token or not plex_username:
        return False

    from urllib.request import Request, urlopen
    from urllib.error import HTTPError, URLError
    import json

    headers_common = {
        "X-Plex-Token": plex_token,
        "X-Plex-Product": "OnBoardarr",
        "X-Plex-Client-Identifier": "onboardarr",
    }

    # Try several plausible endpoints and payload formats. Stop on first success.
    candidates = [
        ("/api/home/users", "json"),
        ("/api/home/users.xml", "form"),
        ("/api/v2/home/users", "json"),
        ("/api/v2/home/users.xml", "form"),
        ("/home/users", "json"),
        ("/api/users", "json"),
    ]

    for path, fmt in candidates:
        post_url = f"{plex_url.rstrip('/')}{path}"
        try:
            if fmt == "json":
                data = json.dumps({"username": plex_username}).encode("utf-8")
                hdrs = {**headers_common, "Content-Type": "application/json", "Accept": "application/json"}
            else:
                # form-encoded fallback (or xml endpoint expecting form data)
                from urllib.parse import urlencode

                data = urlencode({"username": plex_username}).encode("utf-8")
                hdrs = {**headers_common, "Content-Type": "application/x-www-form-urlencoded"}

            req = Request(post_url, data=data, headers=hdrs, method="POST")
            with urlopen(req, timeout=15) as resp:
                status = getattr(resp, "status", None) or getattr(resp, "getcode", lambda: None)()
                if status in (200, 201):
                    print(f"Plex Home promotion succeeded via {path}")
                    return True
                # If server returns 202 or 204, consider it accepted
                if status in (202, 204):
                    print(f"Plex Home promotion accepted (status {status}) via {path}")
                    return True
        except HTTPError as he:
            try:
                body = he.read().decode("utf-8", errors="ignore")
            except Exception:
                body = ""
            print(f"Plex Home promotion HTTPError {he.code} for {post_url}: {body}")
            # continue to next candidate
            continue
        except URLError as ue:
            print(f"Plex Home promotion URLError for {post_url}: {ue}")
            continue
        except Exception as exc:
            print(f"Plex Home promotion attempt error for {post_url}: {exc}")
            continue

    # As a last step try a GET to list home users; presence of the endpoint
    # may indicate promotion must be done elsewhere.
    try:
        req = Request(f"{plex_url.rstrip('/')}/api/home/users", headers={**headers_common, "Accept": "application/json"}, method="GET")
        with urlopen(req, timeout=10) as resp:
            raw = resp.read()
            if raw:
                print("Plex home users endpoint reachable; promotion likely requires a different API call or Plex.tv action")
                return False
    except Exception:
        pass

    return False


def _get_plex_account_from_env() -> Optional[object]:
    """Return a plexapi.myplex.MyPlexAccount instance if plexapi is available
    and credentials are provided via env vars `PLEX_ADMIN_USER` and
    `PLEX_ADMIN_PASS` (or `PLEX_TOKEN`). Returns None if not possible.
    """
    try:
        from plexapi.myplex import MyPlexAccount
    except Exception:
        print("_get_plex_account_from_env: plexapi not installed")
        return None

    admin_user = os.getenv("PLEX_ADMIN_USER", "").strip() or os.getenv("PLEX_ADMIN", "").strip()
    admin_pass = os.getenv("PLEX_ADMIN_PASS", "").strip() or os.getenv("PLEX_PASSWORD", "").strip()
    plex_token = os.getenv("PLEX_TOKEN", "").strip()

    print(f"_get_plex_account_from_env: PLEX_ADMIN_USER present={bool(os.getenv('PLEX_ADMIN_USER', '').strip())}, PLEX_ADMIN present={bool(os.getenv('PLEX_ADMIN', '').strip())}, PLEX_ADMIN={admin_user}, PLEX_PASSWORD present={bool(os.getenv('PLEX_PASSWORD', '').strip())}, PLEX_TOKEN present={bool(plex_token)}")

    try:
        if admin_user and admin_pass:
            print("_get_plex_account_from_env: authenticating with admin user/pass")
            return MyPlexAccount(admin_user, admin_pass)
        if plex_token:
            print("_get_plex_account_from_env: authenticating with token")
            return MyPlexAccount(token=plex_token)
    except Exception as exc:
        print(f"Could not create MyPlexAccount: {exc}")
        return None

    print("_get_plex_account_from_env: no valid Plex admin credentials available")
    return None


def plex_home_slots_available(account: object) -> Optional[int]:
    try:
        current = int(getattr(account, "homeSize", 0))
        maximum = int(getattr(account, "maxHomeSize", 15))
        return max(0, maximum - current)
    except Exception:
        return None


def plex_invite_plex_home_user(account: object, target_user: str, server_name: str) -> bool:
    try:
        resource = account.resource(server_name)
        server = resource.connect()
        sections = server.library.sections()
        # createExistingUser invites the member's real account into the Home.
        # createHomeUser would instead create a second, login-less Managed Account with the same name.
        if hasattr(account, "createExistingUser"):
            try:
                account.createExistingUser(user=target_user, server=server, sections=sections)
                print("plex_invite_plex_home_user: used createExistingUser for Plex Home promotion")
                return True
            except Exception as exc:
                print(f"plex_invite_plex_home_user createExistingUser failed: {exc}; falling back to inviteFriend")

        account.inviteFriend(user=target_user, server=server, sections=sections)
        print("plex_invite_plex_home_user: used inviteFriend fallback for Plex Home promotion")
        return True
    except Exception as exc:
        print(f"plex_invite_plex_home_user failed: {exc}")
        return False


def wait_for_plex_home_acceptance(account: object, before_set: set, timeout: int = 60) -> Optional[set]:
    try:
        import time
        account.refresh()
        start = time.time()
        while time.time() - start < timeout:
            account.refresh()
            current = {u.username for u in account.home().users}
            new = current - before_set
            if new:
                return new
            time.sleep(3)
    except Exception as exc:
        print(f"wait_for_plex_home_acceptance error: {exc}")
    return None



class PlexPromotionError(Exception):
    pass


def _seerr_config() -> Optional[tuple]:
    api_key = os.getenv("SEERR_API_KEY", "").strip()
    if not api_key:
        return None
    return os.getenv("SEERR_URL", "http://seerr:5055").rstrip("/"), {"X-Api-Key": api_key}


def _seerr_load_users(base_url: str, headers: Dict[str, str]) -> List[Dict[str, object]]:
    """Import any Plex users Seerr doesn't know yet, then return all Seerr users."""
    try:
        # No body on purpose: Seerr only imports missing users when req.body is absent.
        fetch_json(f"{base_url}/api/v1/user/import-from-plex", headers, timeout=30, method="POST")
    except RuntimeError as exc:
        print(f"Seerr Plex user import returned an error (continuing): {exc}")

    listing = fetch_json(f"{base_url}/api/v1/user?take=200", headers, timeout=30)
    return listing.get("results", []) if isinstance(listing, dict) else []


def _seerr_apply_watchlist_sync(base_url: str, headers: Dict[str, str], user: Dict[str, object]) -> Optional[bool]:
    """Turn on watchlist auto-requesting for one Seerr user.

    Returns True if this call changed it, False if already on, None on failure. The POST
    is a read-modify-write because Seerr's handler overwrites every field in the body.
    """
    label = user.get("plexUsername") or f"id {user.get('id')}"
    settings_url = f"{base_url}/api/v1/user/{user['id']}/settings/main"
    try:
        settings = fetch_json(settings_url, headers, timeout=30)
        if not isinstance(settings, dict):
            print(f"Seerr watchlist sync skipped: unexpected settings payload for {label}")
            return None
        if settings.get("watchlistSyncMovies") and settings.get("watchlistSyncTv"):
            return False

        settings["watchlistSyncMovies"] = True
        settings["watchlistSyncTv"] = True
        fetch_json(settings_url, headers, payload=settings, timeout=30, method="POST")
        print(f"Seerr watchlist sync enabled for {label} (user id {user['id']})")
        return True
    except Exception as exc:
        print(f"Seerr watchlist sync failed for {label}: {exc}")
        return None


def seerr_enable_watchlist_sync(plex_username: str) -> bool:
    """Enable Plex watchlist auto-requesting for one newly onboarded member."""
    config = _seerr_config()
    if not config:
        print("Seerr watchlist sync skipped: SEERR_API_KEY not set")
        return False
    base_url, headers = config

    try:
        target = next((u for u in _seerr_load_users(base_url, headers)
                       if (u.get("plexUsername") or "").lower() == plex_username.lower()), None)
    except Exception as exc:
        print(f"Seerr watchlist sync failed for {plex_username}: {exc}")
        return False

    if not target:
        # Usually means the Plex share hasn't propagated yet; the periodic sweep retries.
        print(f"Seerr watchlist sync deferred: no Seerr user matching {plex_username} yet")
        return False

    changed = _seerr_apply_watchlist_sync(base_url, headers, target)
    if changed is False:
        print(f"Seerr watchlist sync already enabled for {plex_username}")
    return changed is not None


def seerr_sweep_watchlist_sync() -> None:
    """Enable watchlist auto-requesting for every Seerr user still missing it.

    Catches members whose Seerr account only appears after their first sign-in, which
    can be well after onboarding ran.
    """
    config = _seerr_config()
    if not config:
        return
    base_url, headers = config

    try:
        users = _seerr_load_users(base_url, headers)
    except Exception as exc:
        print(f"Seerr watchlist sweep failed: {exc}")
        return

    enabled = sum(1 for u in users if _seerr_apply_watchlist_sync(base_url, headers, u) is True)
    if enabled:
        print(f"Seerr watchlist sweep enabled sync for {enabled} user(s)")


async def _attempt_plex_promotion(self_or_client, user: Optional[discord.User], plex_username: str) -> bool:
    """Async helper to attempt Plex Home invite/promotion.

    This function is safe to call from async code and will run blocking
    plexapi calls in a thread to avoid blocking the event loop.
    """
    try:
        print(f"_attempt_plex_promotion environment: PLEX_ADMIN={os.getenv('PLEX_ADMIN')}, PLEX_PASSWORD present={bool(os.getenv('PLEX_PASSWORD'))}, PLEX_TOKEN present={bool(os.getenv('PLEX_TOKEN'))}")
        account = _get_plex_account_from_env()
        if not account:
            print("Plex Home promotion skipped: no Plex admin account available from env")
            return False

        slots = await asyncio.to_thread(plex_home_slots_available, account)
        if slots is None:
            print("Plex Home promotion skipped: could not determine Plex Home slot availability")
            return False

        if slots <= 0:
            print("Plex Home promotion skipped: no Plex Home slots available")
            return False

        print(f"Plex Home promotion starting for {plex_username}: {slots} slot(s) available")

        server_name = os.getenv("PLEX_SERVER_NAME", "")
        if not server_name and hasattr(self_or_client, '_resolve_plex_server_name'):
            server_name = self_or_client._resolve_plex_server_name()

        invited = await asyncio.to_thread(
            plex_invite_plex_home_user,
            account,
            plex_username,
            server_name,
        )

        if not invited:
            print(f"Plex Home promotion failed for {plex_username}: invitation call returned false")
            return False

        print(f"Plex Home invitation sent for {plex_username}; not waiting for acceptance")
        return True
    except Exception as exc:
        print(f"Plex Home promotion error for {plex_username}: {exc}")
        return True


class OnBoardarrClient(commands.Bot):
    def __init__(self, *args, **kwargs) -> None:
        # Respect the intents passed in from the caller. Only ensure the
        # non-privileged `guilds` intent is enabled so the bot can operate.
        intents = kwargs.pop("intents", discord.Intents.default())
        try:
            intents.guilds = True
        except Exception:
            pass
        super().__init__(command_prefix="!", intents=intents, **kwargs)
        self._discord_client_id = os.getenv("DISCORD_CLIENT_ID", "").strip()
        self._telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self._telegram_admin_chat_ids = parse_telegram_chat_ids(
            os.getenv("TELEGRAM_ADMIN_CHAT_IDS", ""),
        )
        self._telegram_offset = 0
        self._telegram_invite_urls: Dict[int, str] = {}
        self._plex_invite_url = os.getenv("PLEX_INVITE_URL", "").strip()
        self._wizarr_api_base_url = os.getenv("WIZARR_API_BASE_URL", "http://wizarr:5690").strip()
        self._wizarr_public_base_url = os.getenv("WIZARR_PUBLIC_BASE_URL", os.getenv("PLEX_INVITE_URL", "")).strip()
        self._wizarr_db_file = Path(os.getenv("WIZARR_DB_FILE", "/data/database/database.db"))
        self._wizarr_api_key = os.getenv("WIZARR_API_KEY", "").strip()
        self._wizarr_server_id = os.getenv("WIZARR_SERVER_ID")
        self._plex_server_name_override = os.getenv("PLEX_SERVER_NAME", "Plex").strip() or "Plex"
        self._plex_server_name_cache: Optional[str] = None
        self._pending_wizarr_duration: Optional[int] = None
        self._music_server_env_path = music_server_env_file()
        self._wizarr_mapping_path = Path(os.getenv("WIZARR_MAPPING_FILE", str(DEFAULT_MAPPING_FILE)))
        self._pending_wizarr_invites_path = Path(os.getenv("PENDING_WIZARR_INVITES_FILE", str(DEFAULT_PENDING_INVITES_FILE)))

    def _is_server_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return False
        return interaction.user.id == interaction.guild.owner_id

    def _is_server_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return False
        return self._is_server_owner(interaction) or interaction.user.guild_permissions.administrator

    def _resolve_wizarr_api_key(self) -> str:
        return resolve_wizarr_api_key(self._wizarr_db_file, self._wizarr_api_key)

    def _resolve_wizarr_server_id(self) -> int:
        return resolve_wizarr_server_id(self._wizarr_db_file, self._wizarr_server_id)

    def _resolve_plex_server_name(self) -> str:
        if self._plex_server_name_cache:
            return self._plex_server_name_cache

        try:
            api_key = self._resolve_wizarr_api_key()
            server_id = self._resolve_wizarr_server_id()
            self._plex_server_name_cache = resolve_wizarr_server_name(
                self._wizarr_api_base_url,
                api_key,
                server_id,
                self._plex_server_name_override,
                self._wizarr_db_file,
            )
        except Exception:
            self._plex_server_name_cache = self._plex_server_name_override or "Plex"

        return self._plex_server_name_cache

    def _format_wizarr_invite_link(self, invite_path: str) -> str:
        return format_wizarr_invite_url(self._wizarr_public_base_url, invite_path)

    def _create_wizarr_invite(self, duration_days: Optional[int] = None) -> Dict[str, object]:
        api_key = self._resolve_wizarr_api_key()
        server_id = self._resolve_wizarr_server_id()
        invitation = create_wizarr_invite(self._wizarr_api_base_url, api_key, server_id, duration_days)
        return invitation

    def _consume_pending_wizarr_duration(self) -> Optional[int]:
        duration_days = self._pending_wizarr_duration
        self._pending_wizarr_duration = None
        return duration_days

    def _resolve_named_channel_reference(self, guild: Optional[discord.Guild], preferred_names: List[str], fallback: str) -> str:
        if guild is None:
            return fallback
        return self._resolve_named_channel_reference_for_viewer(guild, guild.me, preferred_names, fallback)

    def _resolve_named_channel_reference_for_viewer(self, guild: discord.Guild, viewer: Optional[discord.abc.Snowflake], preferred_names: List[str], fallback: str) -> str:
        preferred_names = [name for name in preferred_names if name]

        for ch_name in preferred_names:
            for ch in guild.text_channels:
                if (
                    str(ch.id) == ch_name
                    or ch.name == ch_name
                    or ch.name.replace('-', '_') == ch_name.replace('-', '_')
                    or ch.name.replace('_', '-') == ch_name.replace('_', '-')
                ):
                    if ch.permissions_for(viewer or guild.me).view_channel:
                        return f"<#{ch.id}>"
                    return f"#{ch.name}"
        return fallback

    def _resolve_channel_reference(self, guild: Optional[discord.Guild]) -> str:
        preferred_names = [
            os.getenv("DISCORD_MAIN_CHANNEL", "").strip(),
            os.getenv("DISCORD_MAIN_CHANNEL_NAME", "").strip(),
            "music_request",
            "music-requests",
            "music_requests",
        ]
        return self._resolve_named_channel_reference(guild, preferred_names, "#music_request")

    def _resolve_bug_channel_reference(self, guild: Optional[discord.Guild]) -> str:
        preferred_names = [
            os.getenv("DISCORD_BUG_CHANNEL", "").strip(),
            os.getenv("DISCORD_BUG_CHANNEL_ID", "").strip(),
            "bug-reports",
            "bug_reports",
        ]
        return self._resolve_named_channel_reference(guild, preferred_names, "#bug-reports")

    async def _handle_pending_invite_use(self, invite_record: Dict[str, object]) -> None:
        discord_id = invite_record.get("discord_id")
        discord_username = str(invite_record.get("discord_username", "")).strip()
        invite_code = str(invite_record.get("invite_code", "")).strip()

        print(f"_handle_pending_invite_use start: discord_id={discord_id}, invite_code={invite_code}, discord_username={discord_username}")

        if not invite_code or not discord_id:
            print("_handle_pending_invite_use skipped: missing invite_code or discord_id")
            return

        plex_username = resolve_invited_plex_username(self._wizarr_db_file, invite_code)
        if not plex_username:
            plex_username = discord_username or "Unknown"

        try:
            user = await self.fetch_user(int(discord_id))
        except Exception as exc:
            print(f"Could not fetch Discord user {discord_id} for pending invite {invite_code}: {exc}")
            user = None

        if not user:
            print(f"_handle_pending_invite_use skipped: no Discord user object for {discord_id}")

        channel_reference = self._resolve_channel_reference(self.guilds[0] if self.guilds else None)
        bug_channel_reference = self._resolve_bug_channel_reference(self.guilds[0] if self.guilds else None)

        try:
            mapping = load_existing_mapping(self._music_server_env_path)
            if discord_username and plex_username:
                mapping[discord_username.lower()] = plex_username
                save_invite_targets(self._wizarr_mapping_path, mapping)
                sync_mapping(
                    self._music_server_env_path,
                    self._wizarr_mapping_path,
                    os.getenv("PLEX_URL", DEFAULT_PLEX_URL),
                    os.getenv("PLEX_TOKEN", DEFAULT_PLEX_TOKEN),
                    restart_service=None,
                )
        except Exception as exc:
            print(f"Failed to record mapping for pending invite {invite_code}: {exc}")

        if user:
            print(f"Deleting previous bot DMs for user {plex_username} ({discord_id}) before sending promotion and final instructions")
            await self._delete_all_bot_dm_messages(user)

        # Step 2: Attempt Plex Home promotion and record whether the invite was issued.
        plex_home_invited = False
        if user:
            try:
                print(f"Attempting Plex Home promotion for {plex_username}")
                plex_home_invited = await _attempt_plex_promotion(self, user, plex_username)
                print(f"Finished Plex Home promotion attempt for {plex_username}: invited={plex_home_invited}")
            except PlexPromotionError as exc:
                print(f"Plex Home invite flow error: {exc}")
            except Exception as exc:
                print(f"Plex Home promo unexpected error: {exc}")

        # Step 2b: Turn on Seerr's Plex watchlist auto-requesting for the new member.
        await asyncio.to_thread(seerr_enable_watchlist_sync, plex_username)

        # Step 3: Send final onboarding instructions (last DM)
        if user:
            print(f"Sending final onboarding instructions DM to {plex_username}")
            await self._send_post_join_message(user, plex_username, channel_reference, bug_channel_reference, plex_home_invited)
            print(f"_handle_pending_invite_use complete for {plex_username}")

    async def _process_pending_wizarr_invites(self) -> None:
        pending_invites = load_pending_wizarr_invites(self._pending_wizarr_invites_path)
        if not pending_invites:
            return

        try:
            invitations = list_wizarr_invitations(self._wizarr_api_base_url, self._resolve_wizarr_api_key())
        except Exception as exc:
            print(f"Could not list Wizarr invitations while processing pending invites: {exc}")
            return

        invitations_by_code = {
            str(invitation.get("code", "")).strip(): invitation
            for invitation in invitations
            if invitation.get("code")
        }

        remaining: List[Dict[str, object]] = []
        for invite_record in pending_invites:
            invite_code = str(invite_record.get("invite_code", "")).strip()
            invitation = invitations_by_code.get(invite_code)
            if not invitation:
                remaining.append(invite_record)
                continue

            status = str(invitation.get("status", "")).lower()
            invite_has_joined = bool(resolve_invited_plex_username(self._wizarr_db_file, invite_code))
            if status == "used" or invitation.get("used_at") or invitation.get("used_by") or invite_has_joined:
                await self._handle_pending_invite_use(invite_record)
                continue

            if status == "expired":
                print(f"Pending invite {invite_code} expired without being used")
                continue

            remaining.append(invite_record)

        if len(remaining) != len(pending_invites):
            save_pending_wizarr_invites(self._pending_wizarr_invites_path, remaining)

    async def _periodic_pending_invite_processing(self, interval_seconds: int) -> None:
        while True:
            try:
                await self._process_pending_wizarr_invites()
            except Exception as exc:
                print(f"Pending Wizarr invite processing failed: {exc}")
            await asyncio.sleep(interval_seconds)

    async def _delayed_pending_invite_check(self, delay_seconds: int) -> None:
        await asyncio.sleep(delay_seconds)
        try:
            await self._process_pending_wizarr_invites()
        except Exception as exc:
            print(f"Delayed pending Wizarr invite processing failed: {exc}")

    async def _delete_previous_invite_messages(self, user: discord.User) -> None:
        # Remove prior bot DMs with previous invite responses.
        try:
            dm_channel = user.dm_channel or await user.create_dm()
            async for message in dm_channel.history(limit=200):
                if message.author == self.user:
                    try:
                        await message.delete()
                    except Exception:
                        pass
        except Exception:
            pass

    async def _send_post_join_message(self, user: discord.User, plex_username: str, channel_reference: str, bug_channel_reference: str, plex_home_invited: bool = False) -> None:
        try:
            dm_channel = user.dm_channel or await user.create_dm()
            header_message = ""
            if plex_home_invited:
                header_message = (
                    "🎉 **Important:** A Plex Home invite has been sent — check your Plex "
                    "notification center and accept it to unlock full features.\n\n"
                )
            intro_message = (
                f"{header_message}✅ You have successfully joined {self._resolve_plex_server_name()} and your Plex username is **{plex_username}**. "
                "I have removed any older onboarding messages and sent the current instructions below."
            )
            (
                server_intro_message,
                main_message,
                mobile_shortcut_message,
                watch_party_message,
                bug_report_message,
            ) = build_welcome_message(channel_reference, bug_channel_reference)

            # Keep each topic in its own DM and the server description first.
            for chunk in (
                server_intro_message,
                intro_message,
                main_message,
                mobile_shortcut_message,
                watch_party_message,
                bug_report_message,
            ):
                for i in range(0, len(chunk), 2000):
                    await dm_channel.send(chunk[i:i + 2000], suppress_embeds=True)
        except Exception as exc:
            print(f"Failed to send post-join instructions to {user.display_name}: {exc}")

    async def _delete_all_bot_dm_messages(self, user: discord.User) -> int:
        deleted = 0
        try:
            dm_channel = user.dm_channel or await user.create_dm()
            async for message in dm_channel.history(limit=None):
                if message.author == self.user:
                    try:
                        await message.delete()
                        deleted += 1
                    except Exception:
                        pass
        except Exception:
            pass
        return deleted

    async def _disable_previous_invite_links(self, user: discord.User) -> None:
        try:
            dm_channel = user.dm_channel or await user.create_dm()
            invite_urls: List[str] = []
            async for message in dm_channel.history(limit=100):
                if message.author == self.user and message.content:
                    invite_urls.extend(re.findall(r"https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/invite/[A-Za-z0-9_-]+", message.content))
        except Exception:
            return

        for invite_url in invite_urls:
            try:
                await self._revoke_discord_invite(invite_url)
            except Exception:
                pass

    async def _revoke_discord_invite(self, invite_url: str) -> None:
        try:
            invite = await self.fetch_invite(invite_url, with_counts=False)
            if invite and invite.code:
                await invite.delete(reason="Revoked by OnBoardarr because a new /plex-invite was issued")
        except Exception:
            pass

    async def _create_discord_invite(self, guild_id: Optional[int] = None) -> str:
        """Create a single-use Discord invite for the configured guild.

        Tries to create an invite on the first text channel where the bot
        can create invites. Returns the invite URL.
        """
        gid = guild_id or (int(os.getenv("DISCORD_GUILD_ID")) if os.getenv("DISCORD_GUILD_ID") else None)
        if not gid:
            raise RuntimeError("DISCORD_GUILD_ID is not configured")

        guild = self.get_guild(gid)
        if guild is None:
            try:
                guild = await self.fetch_guild(gid)
            except Exception as exc:
                raise RuntimeError(f"Could not fetch guild {gid}: {exc}") from exc

        for channel in guild.text_channels:
            try:
                # single-use invite that expires in 24 hours.
                invite = await channel.create_invite(
                    max_uses=1,
                    max_age=86400,
                    unique=True,
                    reason="OnBoardarr single-use invite"
                )
                return invite.url
            except Exception:
                # try next channel
                continue

        raise RuntimeError("Could not create an invite in any text channel - missing permissions or channels")

    async def _telegram_send(self, chat_id: int, text: str) -> None:
        if not self._telegram_token:
            return
        for start in range(0, len(text), 4000):
            await asyncio.to_thread(
                telegram_api_call,
                self._telegram_token,
                "sendMessage",
                {"chat_id": chat_id, "text": text[start:start + 4000]},
            )

    async def _telegram_notify_admins(self, text: str) -> None:
        if not self._telegram_token:
            return
        for chat_id in self._telegram_admin_chat_ids:
            try:
                await self._telegram_send(chat_id, text)
            except Exception as exc:
                print(f"Failed to send Telegram admin notification: {exc}")

    async def _telegram_poll_loop(self) -> None:
        if not self._telegram_token:
            return
        await self.wait_until_ready()

        print("Telegram bot polling enabled")
        observed_chat_ids = set()
        while True:
            try:
                updates = await asyncio.to_thread(
                    telegram_api_call,
                    self._telegram_token,
                    "getUpdates",
                    {"offset": self._telegram_offset, "timeout": 25},
                )
                for update in updates or []:
                    self._telegram_offset = max(
                        self._telegram_offset, int(update.get("update_id", 0)) + 1,
                    )
                    message = update.get("message") or update.get("edited_message")
                    if not message or not message.get("text"):
                        continue
                    chat_id = int(message.get("chat", {}).get("id", 0))
                    if chat_id not in self._telegram_admin_chat_ids:
                        if chat_id not in observed_chat_ids:
                            print(f"Telegram chat ID observed: {chat_id}; authorize it in TELEGRAM_ADMIN_CHAT_IDS")
                            observed_chat_ids.add(chat_id)
                        continue
                    try:
                        await self._handle_telegram_message(chat_id, message["text"])
                    except Exception as exc:
                        print(f"Telegram command failed: {exc}")
                        await self._telegram_send(chat_id, "The command failed. Check the Onboardarr logs.")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"Telegram polling failed: {exc}")
                await asyncio.sleep(10)

    async def _handle_telegram_message(self, chat_id: int, text: str) -> None:
        parts = text.strip().split(maxsplit=1)
        if not parts:
            return
        command = parts[0].split("@", 1)[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""

        if command in ("/start", "/help"):
            await self._telegram_send(chat_id, (
                "Onboardarr admin commands:\n"
                "/plex-invite [days] — create a single-use Discord invite; "
                "the next joiner receives a Wizarr/Plex invite\n"
                "/reset-welcome — resend the current welcome guide to Discord members\n"
                "/status — show service connectivity"
            ))
            return

        if command == "/status":
            await self._telegram_send(chat_id, (
                f"Onboardarr is online. Discord: {'connected' if self.is_ready() else 'connecting'}; "
                f"Wizarr: {self._wizarr_api_base_url}; Plex: {self._plex_server_name_override}."
            ))
            return

        if command == "/reset-welcome":
            guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
            guild = self.get_guild(int(guild_id)) if guild_id else None
            if guild is None:
                await self._telegram_send(chat_id, "The configured Discord server is unavailable.")
                return
            deleted, resent, failed = await self._reset_welcome_messages_for_guild(guild)
            await self._telegram_send(
                chat_id,
                f"Reset complete: deleted {deleted} bot DM messages, resent welcome messages "
                f"to {resent} members, and failed for {failed}.",
            )
            return

        if command not in ("/plex-invite", "/invite"):
            await self._telegram_send(chat_id, "Unknown command. Send /help for available commands.")
            return

        duration_days = None
        if argument:
            try:
                duration_days = int(argument)
            except ValueError:
                await self._telegram_send(chat_id, "Usage: /plex-invite [positive number of days]")
                return
            if duration_days < 1:
                await self._telegram_send(chat_id, "Invite duration must be at least one day.")
                return

        try:
            cleanup_wizarr_invitations(
                self._wizarr_api_base_url, self._resolve_wizarr_api_key(),
            )
        except Exception as exc:
            print(f"Telegram Wizarr invite cleanup failed: {exc}")

        previous_invite = self._telegram_invite_urls.get(chat_id)
        if previous_invite:
            await self._revoke_discord_invite(previous_invite)

        try:
            discord_invite_url = await self._create_discord_invite()
        except Exception as exc:
            await self._telegram_send(chat_id, f"Could not create a Discord invite: {exc}")
            return

        self._pending_wizarr_duration = duration_days
        self._telegram_invite_urls[chat_id] = discord_invite_url
        duration_note = (
            f"The next joiner will receive a Plex invite valid for {duration_days} days."
            if duration_days else
            "The next joiner will receive an unlimited Plex invite."
        )
        await self._telegram_send(
            chat_id,
            f"Share this single-use Discord invite (expires in 24 hours):\n{discord_invite_url}\n\n"
            f"{duration_note}",
        )

    async def _reset_welcome_messages_for_guild(
        self, guild: discord.Guild,
    ) -> tuple[int, int, int]:
        deleted_count = 0
        resent_count = 0
        failed_count = 0
        try:
            members = [member for member in guild.members if not member.bot]
            if not members:
                members = [member for member in await guild.fetch_members(limit=None) if not member.bot]
        except Exception:
            members = [member for member in guild.members if not member.bot]

        music_preferred_names = [
            os.getenv("DISCORD_MAIN_CHANNEL", "").strip(),
            os.getenv("DISCORD_MAIN_CHANNEL_NAME", "").strip(),
            "music_request",
            "music-requests",
            "music_requests",
        ]
        bug_preferred_names = [
            os.getenv("DISCORD_BUG_CHANNEL", "").strip(),
            os.getenv("DISCORD_BUG_CHANNEL_ID", "").strip(),
            "bug-reports",
            "bug_reports",
        ]

        for member in members:
            try:
                deleted_count += await self._delete_all_bot_dm_messages(member)
                channel_reference = self._resolve_named_channel_reference_for_viewer(
                    guild, member, music_preferred_names, "#music_request",
                )
                bug_channel_reference = self._resolve_named_channel_reference_for_viewer(
                    guild, member, bug_preferred_names, "#bug-reports",
                )
                (
                    server_intro_message,
                    main_message,
                    mobile_shortcut_message,
                    watch_party_message,
                    bug_report_message,
                ) = build_welcome_message(channel_reference, bug_channel_reference)
                server_intro_message = (
                    "🔄 Our welcome message has been updated — here's the latest version:\n\n"
                    f"{server_intro_message}"
                )
                try:
                    for chunk in (
                        server_intro_message,
                        main_message,
                        mobile_shortcut_message,
                        watch_party_message,
                        bug_report_message,
                    ):
                        for start in range(0, len(chunk), 2000):
                            await member.send(chunk[start:start + 2000], suppress_embeds=True)
                    resent_count += 1
                except Exception as exc:
                    print(f"reset-welcome: failed to DM {member} ({member.id}): {exc}")
                    failed_count += 1
            except Exception:
                failed_count += 1

        return deleted_count, resent_count, failed_count

    async def setup_hook(self) -> None:
        @self.tree.command(name="plex-invite", description="Get the Discord server invite link to share with others")
        @discord.app_commands.describe(days="Temporary Plex access duration in days. Leave empty for unlimited access.")
        async def plex_invite(interaction: discord.Interaction, days: Optional[int] = None) -> None:
            if not self._is_server_owner(interaction):
                await interaction.response.send_message("Only the Discord server owner can use this command.", ephemeral=True, suppress_embeds=True)
                return

            await interaction.response.defer(ephemeral=True)

            # Delete previous invite message DMs and revoke old Discord invites
            await self._disable_previous_invite_links(interaction.user)
            await self._delete_previous_invite_messages(interaction.user)

            # Clean up any existing Wizarr invitations before creating a new one.
            try:
                cleanup_wizarr_invitations(self._wizarr_api_base_url, self._resolve_wizarr_api_key())
            except Exception:
                pass

            # Store a pending temporary duration for the next joining user.
            if days is not None and days > 0:
                self._pending_wizarr_duration = days

            # Create a single-use Discord invite for the configured guild
            try:
                discord_invite_url = await self._create_discord_invite()
            except Exception as exc:
                await interaction.followup.send(
                    f"I could not create a server invite: {exc}",
                    ephemeral=True,
                    suppress_embeds=True,
                )
                return

            duration_note = (
                f"The next new joiner will receive a temporary Plex invite valid for {days} days.\n\n"
                if days is not None and days > 0
                else "The next new joiner will receive a Plex invite with indefinite access.\n\n"
            )
            discord_note = "This Discord server invite link expires in 24 hours.\n\n"

            message = (
                "Share this link with someone to invite them to join the Discord server:\n\n"
                f"{discord_invite_url}\n\n"
                f"{discord_note}"
                f"{duration_note}"
                f"Once they join, I will automatically send them a Plex invite to {self._resolve_plex_server_name()}."
            )

            try:
                await interaction.user.send(message)
            except Exception as exc:
                await interaction.followup.send(f"I couldn't DM you the invite link: {exc}", ephemeral=True, suppress_embeds=True)
                return

            await interaction.followup.send("I sent your Discord invite link to you privately.", ephemeral=True, suppress_embeds=True)

        @self.tree.command(name="reset-welcome", description="Delete all onboarding DMs and resend only the welcome instructions to all current users")
        async def reset_welcome_messages(interaction: discord.Interaction) -> None:
            if not self._is_server_admin(interaction):
                await interaction.response.send_message("Only the Discord server owner or an administrator can use this command.", ephemeral=True, suppress_embeds=True)
                return

            if interaction.guild is None:
                await interaction.response.send_message("This command must be used in a guild.", ephemeral=True, suppress_embeds=True)
                return

            await interaction.response.defer(ephemeral=True)
            deleted_count, resent_count, failed_count = (
                await self._reset_welcome_messages_for_guild(interaction.guild)
            )
            await interaction.followup.send(
                f"Reset complete: deleted {deleted_count} bot DM messages, resent welcome message to {resent_count} users, failed for {failed_count} users.",
                ephemeral=True,
                suppress_embeds=True,
            )

        try:
            print(f"setup_hook: application_id={self.application_id}, command_count={len(list(self.tree.walk_commands()))}")
            guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
            if guild_id:
                try:
                    guild = discord.Object(id=int(guild_id))
                    self.tree.copy_global_to(guild=guild)
                    synced = await self.tree.sync(guild=guild)
                    print(f"Synced {len(synced)} slash commands to guild {guild_id}: {[c.name for c in synced]}")
                except Exception as exc:
                    print(f"Failed to sync slash commands to guild {guild_id}: {exc}")
            else:
                try:
                    await self.tree.sync()
                    print("Synced global slash commands")
                except Exception as exc:
                    print(f"Failed to sync global slash commands: {exc}")
        except Exception as exc:
            print(f"Slash command sync failed: {exc}")

        # Start webhook listener for Wizarr events (new user joins)
        try:
            asyncio.create_task(self._start_wizarr_webhook())
        except Exception:
            pass
        if self._telegram_token:
            asyncio.create_task(self._telegram_poll_loop())

    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return

        invite_url = self._plex_invite_url
        pending_duration = self._pending_wizarr_duration
        duration_days = None
        invitation = None
        duration_days = None
        invite_url = None
        invite_code = None
        try:
            invitation = self._create_wizarr_invite(duration_days=pending_duration)
            duration_days = self._consume_pending_wizarr_duration()
            invite_url = self._format_wizarr_invite_link(str(invitation.get("url", "")))
            invite_code = str(invitation.get("code", "")).strip() or _extract_invitation_code(str(invitation.get("url", "")))
            if invite_code:
                append_pending_wizarr_invite(self._pending_wizarr_invites_path, {
                    "invite_code": invite_code,
                    "discord_id": str(member.id),
                    "discord_username": member.name,
                    "created_at": datetime.datetime.utcnow().isoformat() + "Z",
                })
        except Exception as exc:
            print(f"Could not generate Wizarr invite for {member.display_name}: {exc}")

        plex_name = self._resolve_plex_server_name()
        # Build welcome message and include channel mention if possible
        preferred_names = [
            os.getenv("DISCORD_MAIN_CHANNEL", "").strip(),
            os.getenv("DISCORD_MAIN_CHANNEL_NAME", "").strip(),
            "music_request",
            "music-requests",
            "music_requests",
        ]
        preferred_names = [name for name in preferred_names if name]
        channel_mention = None
        try:
            for ch_name in preferred_names:
                for ch in member.guild.text_channels:
                    if (
                        str(ch.id) == ch_name
                        or ch.name == ch_name
                        or ch.name.replace('-', '_') == ch_name.replace('-', '_')
                        or ch.name.replace('_', '-') == ch_name.replace('_', '-')
                    ):
                        if ch.permissions_for(member).view_channel:
                            channel_mention = f"<#{ch.id}>"
                        else:
                            channel_mention = f"#{ch.name}"
                        break
                if channel_mention:
                    break
        except Exception:
            channel_mention = None

        expires_text = (
            f"This Plex invite grants access for {duration_days} days." if duration_days is not None else "This Plex invite grants indefinite access."
        )
        channel_reference = channel_mention or "#music_request"

        try:
            dm_channel = member.dm_channel or await member.create_dm()

            if invite_url:
                invite_message = (
                    f"**Please join our {plex_name} Plex/Wizarr server first!**\n\n"
                    f"**To Join, Here is Your {plex_name} Plex/Wizarr Invite:**\n{invite_url}\n{expires_text}"
                )
                await dm_channel.send(invite_message, suppress_embeds=True)
            elif invitation is not None:
                invite_message = (
                    f"**Please join our {plex_name} Plex/Wizarr server first!**\n\n"
                    "I generated the invite, but could not format the invite URL. Please contact the admin."
                )
                await dm_channel.send(invite_message, suppress_embeds=True)

            print(f"Sent Plex/Wizarr invite DM to {member.display_name} ({member.id})")
        except Exception as exc:
            print(f"Failed to send Plex/Wizarr invite DM to {member.display_name}: {exc}")

        await self._telegram_notify_admins(
            f"New Discord member: {member.display_name} ({member.id}). "
            f"Wizarr/Plex invite {'sent' if invite_url else 'could not be generated'}."
        )

        try:
            asyncio.create_task(self._delayed_pending_invite_check(5))
        except Exception:
            pass

        try:
            env_path = music_server_env_file()
            mapping_path = Path(os.getenv("WIZARR_MAPPING_FILE", str(DEFAULT_MAPPING_FILE)))
            plex_url = os.getenv("PLEX_URL", DEFAULT_PLEX_URL)
            plex_token = os.getenv("PLEX_TOKEN", DEFAULT_PLEX_TOKEN)
            sync_mapping(env_path, mapping_path, plex_url, plex_token, restart_service=None)
        except Exception:
            pass

    async def on_message(self, message: discord.Message) -> None:
        """Listen for Wizarr agent notifications in the main channel and
        process matching pending invites immediately.
        """
        try:
            # Ignore messages from ourselves or DMs
            if message.author == self.user:
                return
            if message.guild is None:
                return

            # Only pay attention to the configured main channel (name or id)
            preferred = os.getenv("DISCORD_MAIN_CHANNEL", "").strip()
            channel_ok = True
            if preferred:
                if preferred.isdigit():
                    channel_ok = str(message.channel.id) == preferred
                else:
                    channel_ok = message.channel.name == preferred or message.channel.name.replace('-', '_') == preferred.replace('-', '_')
            if not channel_ok:
                return

            text = (message.content or "").strip()
            if not text:
                return

            # Try to extract a Plex username from common notification formats.
            m = re.search(r"(?i)(?:new user|created user|user created|joined plex user|created plex user)[:\s-]*([A-Za-z0-9_.-]+)", text)
            if not m:
                m = re.search(r"(?i)username[:\s]+([A-Za-z0-9_.-]+)", text)
            if not m:
                return

            plex_username = m.group(1).strip()
            if not plex_username:
                return

            pending = load_pending_wizarr_invites(self._pending_wizarr_invites_path)
            if not pending:
                return

            remaining: List[Dict[str, object]] = []
            handled = False
            for rec in pending:
                rec_code = str(rec.get("invite_code", "")).strip()
                if not rec_code:
                    remaining.append(rec)
                    continue
                try:
                    resolved = resolve_invited_plex_username(self._wizarr_db_file, rec_code)
                except Exception:
                    resolved = None

                if resolved and resolved.strip().lower() == plex_username.strip().lower():
                    try:
                        await self._handle_pending_invite_use(rec)
                        handled = True
                        continue
                    except Exception as exc:
                        print(f"Failed to process pending invite from message: {exc}")
                        remaining.append(rec)
                        continue

                remaining.append(rec)

            if handled:
                try:
                    save_pending_wizarr_invites(self._pending_wizarr_invites_path, remaining)
                except Exception:
                    pass

        except Exception as exc:
            print(f"on_message handler error: {exc}")

    async def _start_wizarr_webhook(self) -> None:
        """Start an aiohttp web server to receive Wizarr webhooks for user creation.

        Expects Wizarr to send the configured API key in the `X-API-Key` header.
        The handler will look for invitation codes or usernames and trigger the
        same post-join handling as the periodic processor.
        """
        try:
            from aiohttp import web
        except Exception:
            print("aiohttp not available; webhook listener not started")
            return

        webhook_path = os.getenv("WIZARR_WEBHOOK_PATH", "/wizarr/webhook").strip()
        port = int(os.getenv("WIZARR_WEBHOOK_PORT", "8081"))

        async def handle(request: web.Request) -> web.Response:
            # Simple API key validation
            try:
                req_key = request.headers.get("X-API-Key") or request.headers.get("X-Wizarr-Api-Key")
                try:
                    configured = self._resolve_wizarr_api_key()
                except Exception:
                    configured = None
                if configured and req_key != configured:
                    return web.Response(status=403, text="Forbidden")

                payload = await request.json()
            except Exception:
                return web.Response(status=400, text="Invalid JSON or missing API key")

            code = None
            plex_username = None
            if isinstance(payload, dict):
                # invitation.code or invite_code
                inv = payload.get("invitation") or payload.get("invite")
                if isinstance(inv, dict):
                    code = str(inv.get("code", "")).strip() or None
                    plex_username = inv.get("username") or inv.get("plex_username")

                for k in ("invite_code", "code", "invitation_code"):
                    if not code and k in payload:
                        code = str(payload.get(k, "")).strip() or None

                # possible user payloads
                for k in ("username", "user", "user_name", "plex_username"):
                    if plex_username:
                        break
                    if k in payload:
                        val = payload.get(k)
                        if isinstance(val, dict):
                            plex_username = val.get("username") or val.get("display_name")
                        else:
                            plex_username = str(val)

            # Attempt to match pending invites by code first, then by resolved username
            pending = load_pending_wizarr_invites(self._pending_wizarr_invites_path)
            handled = False
            remaining: List[Dict[str, object]] = []
            for rec in pending:
                rec_code = str(rec.get("invite_code", "")).strip()
                if code and rec_code and rec_code == code:
                    try:
                        await self._handle_pending_invite_use(rec)
                        handled = True
                        continue
                    except Exception as exc:
                        print(f"Webhook handler failed to process pending invite {rec_code}: {exc}")
                        remaining.append(rec)
                        continue

                # If no code provided, try to detect by username mapping in DB
                if plex_username and rec_code:
                    try:
                        resolved = resolve_invited_plex_username(self._wizarr_db_file, rec_code)
                        if resolved and resolved.strip().lower() == str(plex_username).strip().lower():
                            await self._handle_pending_invite_use(rec)
                            handled = True
                            continue
                    except Exception:
                        pass

                remaining.append(rec)

            if handled:
                try:
                    save_pending_wizarr_invites(self._pending_wizarr_invites_path, remaining)
                except Exception:
                    pass
                return web.json_response({"status": "processed"})

            return web.json_response({"status": "ignored"}, status=202)

        app = web.Application()
        app.router.add_post(webhook_path, handle)

        runner = web.AppRunner(app)

        async def _run():
            try:
                await runner.setup()
                site = web.TCPSite(runner, "0.0.0.0", port)
                await site.start()
                print(f"Wizarr webhook listener started on port {port}{webhook_path}")
            except Exception as exc:
                print(f"Failed to start Wizarr webhook listener: {exc}")

        asyncio.create_task(_run())


async def periodic_sync(interval_seconds: int, env_path: Path, mapping_path: Path, plex_url: str, plex_token: str) -> None:
    while True:
        try:
            sync_mapping(env_path, mapping_path, plex_url, plex_token, restart_service="musicai")
        except Exception as exc:
            print(f"OnBoardarr sync failed: {exc}")
        await asyncio.sleep(interval_seconds)


async def periodic_seerr_watchlist_sweep(interval_seconds: int) -> None:
    while True:
        try:
            await asyncio.to_thread(seerr_sweep_watchlist_sync)
        except Exception as exc:
            print(f"Seerr watchlist sweep failed: {exc}")
        await asyncio.sleep(interval_seconds)


async def run_bot() -> None:
    intents = discord.Intents.default()
    # Always enable guilds intent (not privileged)
    intents.guilds = True

    # Privileged intents (members, message_content) are optional and must be
    # explicitly enabled in the Developer Portal. Allow enabling via an env var
    # to avoid runtime crashes when the token's application does not have them.
    enable_privileged = os.getenv("DISCORD_ENABLE_PRIVILEGED_INTENTS", "false").lower() in ("1", "true", "yes")
    if enable_privileged:
        print("DISCORD_ENABLE_PRIVILEGED_INTENTS is set; requesting privileged intents from Discord.")
        intents.members = True
        intents.message_content = True
    else:
        print("Starting bot WITHOUT privileged intents (set DISCORD_ENABLE_PRIVILEGED_INTENTS=true to request them in Developer Portal)")
        intents.members = False
        intents.message_content = False

    discord_app_id = os.getenv("DISCORD_CLIENT_ID", "").strip()
    client_kwargs = {"intents": intents}
    if discord_app_id:
        try:
            client_kwargs["application_id"] = int(discord_app_id)
        except ValueError:
            pass
    client = OnBoardarrClient(**client_kwargs)

    # Start periodic sync task
    asyncio.create_task(periodic_sync(
        int(os.getenv("SYNC_INTERVAL_SECONDS", "60")),
        music_server_env_file(),
        Path(os.getenv("WIZARR_MAPPING_FILE", str(DEFAULT_MAPPING_FILE))),
        os.getenv("PLEX_URL", DEFAULT_PLEX_URL),
        os.getenv("PLEX_TOKEN", DEFAULT_PLEX_TOKEN),
    ))
    asyncio.create_task(client._periodic_pending_invite_processing(int(os.getenv("PENDING_INVITE_POLL_SECONDS", "10"))))
    asyncio.create_task(periodic_seerr_watchlist_sweep(int(os.getenv("SEERR_WATCHLIST_SWEEP_SECONDS", "300"))))

    try:
        await client.start(os.environ["DISCORD_BOT_TOKEN"])
    except Exception as exc:
        # If privileged intents were requested but not enabled in the
        # developer portal, discord.py raises PrivilegedIntentsRequired.
        # Fall back to starting without privileged intents so the bot
        # can still run (won't receive member events until intents are
        # enabled in the Developer Portal).
        try:
            from discord.errors import PrivilegedIntentsRequired
        except Exception:
            PrivilegedIntentsRequired = None

        if PrivilegedIntentsRequired and isinstance(exc, PrivilegedIntentsRequired):
            print("Privileged intents requested but not enabled in Developer Portal; falling back to start without privileged intents.")
            # Clear privileged intents and restart client
            intents.members = False
            intents.message_content = False
            client = OnBoardarrClient(intents=intents)
            # periodic_sync already started
            await client.start(os.environ["DISCORD_BOT_TOKEN"])
        else:
            raise


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OnBoardarr: sync Invite targets and run the onboarding Discord bot")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--dry-run", action="store_true", help="Run one sync and exit")
    return parser


def main() -> None:
    args = build_argument_parser().parse_args()
    load_env_file(args.env_file)
    print(f"Loaded env file: {args.env_file}")
    print(f"PLEX_ADMIN present: {bool(os.getenv('PLEX_ADMIN', '').strip())}")
    print(f"PLEX_PASSWORD present: {bool(os.getenv('PLEX_PASSWORD', '').strip())}")
    print(f"PLEX_TOKEN present: {bool(os.getenv('PLEX_TOKEN', '').strip())}")
    load_musicai_env(music_server_env_file())

    # Emit clearer startup information for debugging (avoid printing raw tokens)
    try:
        configured_client_id = os.getenv("DISCORD_CLIENT_ID", "").strip()
        token_value = os.getenv("DISCORD_BOT_TOKEN", "")
        token_present = bool(token_value)
        masked_token = None
        if token_present:
            # Mask the token in logs
            masked_token = f"{token_value[:6]}...{token_value[-6:]}"
        print(f"Starting OnBoardarr; env_file={args.env_file}")
        print(f"Configured DISCORD_CLIENT_ID: {configured_client_id or '<none>'}")
        print(f"DISCORD_BOT_TOKEN present: {token_present} {f'(masked: {masked_token})' if masked_token else ''}")
    except Exception:
        pass

    env_path = music_server_env_file()
    mapping_path = Path(os.getenv("WIZARR_MAPPING_FILE", str(DEFAULT_MAPPING_FILE)))
    plex_url = os.getenv("PLEX_URL", DEFAULT_PLEX_URL)
    plex_token = os.getenv("PLEX_TOKEN", DEFAULT_PLEX_TOKEN)

    if args.dry_run:
        mapping = sync_mapping(env_path, mapping_path, plex_url, plex_token, restart_service=None)
        print(json.dumps(mapping, sort_keys=True))
        return

    if not os.getenv("DISCORD_BOT_TOKEN"):
        raise SystemExit("DISCORD_BOT_TOKEN is required")

    asyncio.run(run_bot())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"OnBoardarr failed: {exc}", file=sys.stderr)
        raise
