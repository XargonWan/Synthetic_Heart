"""
Grillo Response Recorder

Utility functions to properly record Grillo beat responses in the activity log.
"""

from typing import Any, Dict, List, Optional

from core.logging_utils import log_debug, log_error, log_info


def build_grillo_response_append_expression(db_type: str) -> str:
    """Return a backend-safe SQL expression for appending response text."""

    if str(db_type).strip().lower() == "postgres":
        return (
            "CASE "
            "WHEN response_text IS NULL OR response_text = '' THEN %s::text "
            "ELSE response_text || E'\\n\\n' || %s::text "
            "END"
        )

    return (
        "CASE "
        "WHEN response_text IS NULL OR response_text = '' THEN %s "
        "ELSE CONCAT(response_text, '\n\n', %s) "
        "END"
    )


async def update_grillo_activity_response(
    activity_log_id: Optional[int],
    response_text: str,
    diary_entry_id: Optional[int] = None,
) -> bool:
    """
    Update the grillo_activity_log with response text and optional diary entry link.

    Args:
        activity_log_id: The activity log ID to update
        response_text: The response text to record
        diary_entry_id: Optional diary entry ID to link

    Returns:
        True if update succeeded, False otherwise
    """
    if not activity_log_id:
        log_debug("[grillo_response] No activity_log_id provided, skipping update")
        return False

    try:
        from core.db import get_conn_ctx

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                if diary_entry_id:
                    await cur.execute(
                        """
                        UPDATE grillo_activity_log 
                        SET response_text = %s, diary_entry_id = %s, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (
                            response_text[:2000] if response_text else None,
                            diary_entry_id,
                            activity_log_id,
                        ),
                    )
                else:
                    await cur.execute(
                        """
                        UPDATE grillo_activity_log 
                        SET response_text = %s, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (
                            response_text[:2000] if response_text else None,
                            activity_log_id,
                        ),
                    )
                await conn.commit()
                log_info(
                    f"[grillo_response] Updated activity {activity_log_id} with response"
                )
                return True
    except Exception as e:
        log_error(f"[grillo_response] Failed to update activity {activity_log_id}: {e}")
        return False


async def extract_response_text_from_cortex_response(response: Any) -> str:
    """
    Extract meaningful response text from various LLM response formats.

    Args:
        response: The raw LLM response (could be dict, string, or other)

    Returns:
        Extracted response text
    """
    if response is None:
        return ""

    if isinstance(response, str):
        return response[:2000]

    if isinstance(response, dict):
        # Try common response keys
        for key in ["message", "content", "text", "response"]:
            if key in response and response[key]:
                val = response[key]
                if isinstance(val, str):
                    return val[:2000]
                elif isinstance(val, dict):
                    # Nested content
                    for subkey in ["content", "text"]:
                        if subkey in val:
                            return str(val[subkey])[:2000]

        # Try to extract from actions
        actions: List[Dict[str, Any]] = response.get("actions", [])
        if actions and isinstance(actions, list):
            texts: List[str] = []
            for action in actions:
                if isinstance(action, dict):
                    payload = action.get("payload", {})
                    if isinstance(payload, dict):
                        # Get text from payload
                        for key in ["content", "text", "message"]:
                            if key in payload:
                                texts.append(str(payload[key]))
            if texts:
                return " | ".join(texts)[:2000]

        # Fallback to string representation
        return str(response)[:500]

    return str(response)[:500]
