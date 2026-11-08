import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[0]
SCRIPT_PATH = ROOT / "sync_plex_invite_mapping.py"

spec = importlib.util.spec_from_file_location("sync_plex_invite_mapping", SCRIPT_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def test_build_discord_to_plex_mapping_inverts_plex_to_discord_entries():
    plex_to_discord = {
        "plexuser1": "discorduser1",
        "plexuser2": "discorduser2",
    }

    mapping = module.build_discord_to_plex_mapping(plex_to_discord)

    assert mapping == {
        "discorduser1": "plexuser1",
        "discorduser2": "plexuser2",
    }


def test_merge_mappings_keeps_existing_entries_and_adds_invite_targets():
    existing = {"existingdiscord": "existingplex"}
    invite_targets = {"newplexuser": "newdiscorduser"}
    discovered_users = {"newplexuser": "newplexuser"}

    merged = module.merge_mappings(existing, invite_targets, discovered_users)

    assert merged == {
        "existingdiscord": "existingplex",
        "newdiscorduser": "newplexuser",
    }


def test_merge_mappings_replaces_stale_entries_with_current_invite_targets():
    existing = {
        "olddiscord": "oldplex",
        "discord_user_2": "plex_user_2",
    }
    invite_targets = {"plex_user_3": "discord_user_3"}
    discovered_users = {}

    merged = module.merge_mappings(existing, invite_targets, discovered_users)

    assert merged == {"discord_user_3": "plex_user_3"}
