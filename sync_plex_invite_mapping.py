#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

DEFAULT_ENV_FILE = Path(os.getenv("MUSIC_SERVER_ENV_FILE", "/opt/music_server.env"))
DEFAULT_MAPPING_FILE = Path(
    os.getenv("WIZARR_MAPPING_FILE", "/opt/wizarr/plex_invite_targets.json"),
)
DEFAULT_PLEX_URL = os.environ.get("PLEX_URL", "http://localhost:32400")
DEFAULT_PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
DEFAULT_COMPOSE_FILE = Path(os.getenv(
    "MUSIC_SERVER_COMPOSE_FILE", "/opt/music_server.docker-compose.yml",
))


def fetch_json(url: str, headers: Dict[str, str], timeout: int = 15) -> object:
    try:
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError, URLError
    except ImportError as exc:
        raise RuntimeError("Could not import urllib.request") from exc

    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8"))
    except (HTTPError, URLError, ValueError) as exc:
        raise RuntimeError(f"Failed to fetch JSON from {url}: {exc}") from exc


def build_discord_to_plex_mapping(plex_to_discord: Dict[str, str]) -> Dict[str, str]:
    """Invert a mapping of plex username -> discord username."""
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


def save_invite_targets(mapping_path: Path, discord_to_plex: Dict[str, str]) -> None:
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    plex_to_discord = {
        str(plex_user).strip(): str(discord_user).strip()
        for discord_user, plex_user in discord_to_plex.items()
        if str(plex_user).strip() and str(discord_user).strip()
    }
    mapping_path.write_text(json.dumps(plex_to_discord, indent=2, sort_keys=True) + "\n")


def merge_mappings(existing: Dict[str, str], invite_targets: Dict[str, str], discovered_users: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    if invite_targets:
        merged = build_discord_to_plex_mapping({plex: discord for plex, discord in invite_targets.items()})
        if discovered_users:
            merged.update({discord_name: plex_name for discord_name, plex_name in existing.items() if discord_name not in merged})
        return merged

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
                [
                    docker_bin,
                    "compose",
                    "-f",
                    os.getenv("MUSIC_SERVER_COMPOSE_FILE", str(DEFAULT_COMPOSE_FILE)),
                    "restart",
                    restart_service,
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )

    return merged


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync Plex invite joins into the music bot mapping")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--mapping-file", type=Path, default=DEFAULT_MAPPING_FILE)
    parser.add_argument("--plex-url", default=DEFAULT_PLEX_URL)
    parser.add_argument("--plex-token", default=DEFAULT_PLEX_TOKEN)
    parser.add_argument("--restart-service", default=None)
    return parser


if __name__ == "__main__":
    args = build_argument_parser().parse_args()
    try:
        mapping = sync_mapping(
            env_path=args.env_file,
            mapping_path=args.mapping_file,
            plex_url=args.plex_url,
            plex_token=args.plex_token,
            restart_service=args.restart_service,
        )
    except Exception as exc:  # pragma: no cover - CLI safety net
        print(f"sync failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(mapping, sort_keys=True))
