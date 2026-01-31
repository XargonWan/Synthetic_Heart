"""
Simple helper to scan the repository for common platform-specific constructs
and generate a short report. Intended for maintainers to run locally or in CI.

Usage:
    python tools/scan_platform_issues.py

It checks for:
 - Shell scripts with shebangs (bash/sh)
 - Commands that manipulate unix permissions (chown/chmod/id)
 - Audio/X server/pulse utilities (pactl, pulseaudio)
 - s6/supervisord container helpers
 - Presence of docker-compose/dockerfile references
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    'shebang_bash': re.compile(r'^#!.*\b(bash|sh)\b'),
    'chown_chmod': re.compile(r'\b(chown|chmod|id -u|id -g)\b'),
    'pulse_audio': re.compile(r'\b(pulseaudio|pactl)\b'),
    's6': re.compile(r'\bs6-'),
    'unix_socket': re.compile(r'unix:'),
    'docker_compose': re.compile(r'docker[- ]?compose|Dockerfile|docker-compose.yml', re.IGNORECASE),
}

REPORT = []

for p in ROOT.rglob('*'):
    if p.is_file() and p.suffix in ('.sh', '.py', ''):
        try:
            text = p.read_text(encoding='utf-8', errors='ignore')
        except Exception:
            continue
        for name, pat in PATTERNS.items():
            if pat.search(text):
                REPORT.append((name, str(p)))

if not REPORT:
    print('No platform-specific patterns found.')
else:
    print('Platform-specific scan results:')
    by_type = {}
    for name, path in REPORT:
        by_type.setdefault(name, []).append(path)
    for name, paths in by_type.items():
        print(f'\n[{name}]')
        for p in paths[:30]:
            print(' -', p)
        if len(paths) > 30:
            print('  ... and more')

print('\nScan complete.')
