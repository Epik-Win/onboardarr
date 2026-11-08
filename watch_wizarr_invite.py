#!/usr/bin/env python3
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / 'sync_plex_invite_mapping.py'
ENV_FILE = Path(os.getenv('MUSIC_SERVER_ENV_FILE', '/opt/music_server.env'))
MAPPING_FILE = Path(os.getenv(
    'WIZARR_MAPPING_FILE', '/opt/wizarr/plex_invite_targets.json',
))
PLEX_URL = os.environ.get('PLEX_URL', 'http://localhost:32400')
PLEX_TOKEN = os.environ.get('PLEX_TOKEN', '')


def main() -> int:
    if not SCRIPT.exists():
        print('sync script missing', file=sys.stderr)
        return 1

    for attempt in range(1, 6):
        command = [
            'python3', str(SCRIPT),
            '--env-file', str(ENV_FILE),
            '--mapping-file', str(MAPPING_FILE),
            '--plex-url', PLEX_URL,
            '--plex-token', PLEX_TOKEN,
            '--restart-service', os.getenv('MUSIC_SERVICE_NAME', 'musicai'),
        ]
        result = subprocess.run(command, text=True, capture_output=True)
        print(result.stdout.strip())
        if result.returncode == 0:
            return 0
        print(result.stderr.strip(), file=sys.stderr)
        if attempt < 5:
            time.sleep(2)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
