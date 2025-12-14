import os
import re
from pathlib import Path

FORBIDDEN_PATTERNS = [
    r"message_queue\.\_queue\.put",
    r"\_queue\.put\(",
    r"\_queue\.\_queue\.put",
]


def scan_forbidden(root_path: str = None) -> list:
    matches = []
    repo_root = Path(__file__).resolve().parent.parent
    for base in ["plugins", "plugins_dev", "interface", "interface_dev"]:
        root = repo_root / base
        if not root.exists():
            continue
        for dirpath, _, filenames in os.walk(str(root)):
            for filename in filenames:
                if not filename.endswith('.py'):
                    continue
                file_path = os.path.join(dirpath, filename)
                try:
                    with open(file_path, 'r', encoding='utf-8') as fh:
                        content = fh.read()
                except Exception:
                    continue
                for pat in FORBIDDEN_PATTERNS:
                    if re.search(pat, content):
                        matches.append((file_path, pat))
    return matches


def test_no_direct_queue_writes():
    # Run scanner for the repo
    matches = scan_forbidden(os.path.dirname(__file__))
    if matches:
        files = '\n'.join([f"{m[0]} matches {m[1]}" for m in matches])
        raise AssertionError(f"Found direct queue writes in plugin files:\n{files}")
