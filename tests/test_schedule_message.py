#!/usr/bin/env python3
"""
Test scheduled message delivery.
Creates an event that should trigger a scheduled_message action.
"""

import pytest

# This file is an integration helper script that requires a live DB and a running scheduler.
# It is not intended to run as part of the unit test suite.
pytest.skip(
    "integration helper script (requires live DB/scheduler)", allow_module_level=True
)

import asyncio
import json
from datetime import datetime, timedelta
import sys

sys.path.insert(0, "/videodrome/videodrome-deployment/Synthetic_Heart")

from core.db import get_db_connection, release_db_connection
from core.logging_utils import log_info, log_error


async def test_schedule_message():
    """Create a test event and verify scheduling."""

    log_info("[test] Starting scheduled message test...")

    # Calculate time 1 minute from now
    now = datetime.utcnow()
    scheduled_time = now + timedelta(minutes=1)

    log_info(f"[test] Current time: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    log_info(
        f"[test] Scheduling event for: {scheduled_time.strftime('%Y-%m-%d %H:%M:%S')}"
    )

    # Create event in database
    conn = await get_db_connection()
    try:
        cursor = conn.cursor()

        query = """
        INSERT INTO scheduled_events 
        (type, payload, scheduled_at, created_at, delivered)
        VALUES (%s, %s, %s, %s, %s)
        """

        payload = json.dumps(
            {
                "text": "🎯 Test message: questo è il messaggio schedulato per test!",
                "send_in": "1 minute",
                "interface": "telegram_bot",
            }
        )

        cursor.execute(query, ("event_reminder", payload, scheduled_time, now, False))

        conn.commit()
        event_id = cursor.lastrowid

        log_info(f"[test] ✅ Event created with ID: {event_id}")
        log_info(f"[test] Payload: {payload}")

        # Verify the event was created
        cursor.execute(
            "SELECT id, type, scheduled_at, delivered FROM scheduled_events WHERE id = %s",
            (event_id,),
        )
        row = cursor.fetchone()
        if row:
            log_info(f"[test] ✅ Event verified in DB: {row}")
        else:
            log_error("[test] ❌ Event NOT found in DB after creation!")

        cursor.close()

    finally:
        release_db_connection(conn)

    log_info(
        "[test] 📌 Now the scheduler should pick this up and deliver it to the LLM"
    )
    log_info(
        "[test] 📌 Watch the logs: tail -f logs/dev/synth.log | grep -E 'schedule_message|event_reminder|action_parser'"
    )
    log_info("[test] ✅ Test event created successfully!")


if __name__ == "__main__":
    asyncio.run(test_schedule_message())
