#!/usr/bin/env python3
"""
Script to cleanup (delete) chat archive entries with zero messages.

Usage:
    python scripts/cleanup_empty_archives.py --dry-run  # list archives to be deleted
    python scripts/cleanup_empty_archives.py --delete   # actually delete them

It uses the existing DB-backed module core.chat_archives_db to list and delete archives.
"""

import argparse
import asyncio
import sys


async def run(dry_run=True, delete_empty_content=False):
    try:
        # Import locally - assumes this script is run with project root set as cwd
        from core.chat_archives_db import list_archives, load_archive, delete_archive
    except Exception as e:
        print("Failed to import core.chat_archives_db: ", e)
        sys.exit(1)

    print("Fetching archives...")
    archives = await list_archives()
    print(f"Found {len(archives)} archives")
    to_delete = []
    for arch in archives:
        aid = arch.get("id")
        print(
            f"Inspecting archive {aid}: name={arch.get('name')} session={arch.get('session_id')} created={arch.get('created_at')} message_count_approx={arch.get('message_count')}"
        )
        try:
            meta = await load_archive(aid)
            messages = meta.get("messages") or []
            if isinstance(messages, list) and len(messages) == 0:
                to_delete.append(aid)
            else:
                # messages exist, optionally check if none contains text
                if delete_empty_content:
                    all_empty = True
                    for msg in messages:
                        if isinstance(msg, dict) and (
                            (msg.get("text") and str(msg.get("text")).strip())
                            or (
                                msg.get("message_text")
                                and str(msg.get("message_text")).strip()
                            )
                        ):
                            all_empty = False
                            break
                    if all_empty:
                        to_delete.append(aid)
                # if the messages is not a list or we decided not to delete, skip
                pass
        except Exception as e:
            print(f"  - Failed to inspect archive {aid}: {e}")

    if not to_delete:
        print("No empty archives to delete.")
        return 0

    print("\nArchives with zero messages (will be deleted):")
    for aid in to_delete:
        print("  -", aid)

    if dry_run:
        print("\nDry-run mode: no changes performed.")
        return 0

    print("\nDeleting archives...")
    deleted = []
    failed = []
    for aid in to_delete:
        try:
            await delete_archive(aid)
            deleted.append(aid)
            print(f"  - Deleted {aid}")
        except Exception as e:
            failed.append((aid, str(e)))
            print(f"  - Failed to delete {aid}: {e}")

    print("\nSummary:")
    print("  Deleted:", len(deleted))
    print("  Failed:", len(failed))
    if failed:
        print("  Failures:")
        for aid, err in failed:
            print("   -", aid, err)

    return 0


def _main():
    parser = argparse.ArgumentParser(
        description="Delete chat archives with zero messages"
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Actually delete archives. Default: dry-run",
    )
    parser.add_argument(
        "--delete-empty-content",
        action="store_true",
        help="Also delete archives where messages exist but none contain text (or message_text)",
    )
    args = parser.parse_args()
    # Use asyncio.run for modern event loop management
    try:
        rc = asyncio.run(
            run(dry_run=not args.delete, delete_empty_content=args.delete_empty_content)
        )
        sys.exit(rc)
    except KeyboardInterrupt:
        print("\nCancelled by user")
        sys.exit(1)


if __name__ == "__main__":
    _main()
