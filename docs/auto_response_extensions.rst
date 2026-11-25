Auto-Response System Extensions
================================

This document describes the extension of the auto-response system to additional interfaces beyond the initial terminal plugin implementation.

Extended Interfaces
-------------------

Reddit Interface
~~~~~~~~~~~~~~~~

**File**: ``interface/reddit_interface.py``

**Changes Made**:

1. **Import Addition**:
   
   .. code-block:: python

       from core.auto_response import request_llm_delivery

2. **Updated ``_listen_inbox()`` Method**:
   
   .. code-block:: python

       async def _listen_inbox(self):
           if asyncpraw is None:
               return
           try:
               async for item in self.reddit.inbox.stream(skip_existing=True):
                   wrapper = self._wrap_item(item)
                   if wrapper:
                       # Use auto-response system for autonomous Reddit interactions
                       await request_llm_delivery(
                           message=wrapper,
                           interface=self,
                           context={},
                           reason="reddit_autonomous_response"
                       )
           except Exception as e:
               log_error(f"[reddit_interface] Inbox listener stopped: {e}")
               self._running = False

**Purpose**: Routes incoming Reddit messages and comments through the LLM for intelligent autonomous responses, replacing direct ``plugin_instance.handle_incoming_message()`` calls.

X (Twitter) Interface
~~~~~~~~~~~~~~~~~~~~~

**File**: ``interface/x_interface.py``

**Changes Made**:

1. **Import Addition**:
   
   .. code-block:: python

       from core.auto_response import request_llm_delivery

2. **Enhanced ``send_message()`` Method**:
   
   .. code-block:: python

       async def send_message(self, payload: Dict[str, Any], original_message: Any | None = None) -> None:
           # ... existing logic ...
           
           # Check if this is an autonomous posting (no original_message) that should use auto-response
           if original_message is None and payload.get("autonomous", False):
               # This would be for future autonomous X posting features
               log_debug("[x_interface] Autonomous posting detected, using auto-response system")
               # For now, just log. Future implementation could create a synthetic message
               # and route through request_llm_delivery for LLM-mediated posting decisions
           
           log_info(f"[x_interface] Message posted: {text}")

**Purpose**: Prepares X interface for future autonomous posting features where the LLM could make intelligent decisions about content, timing, and responses to mentions or timeline events.

Event Plugin
~~~~~~~~~~~~

**File**: ``plugins/event_plugin.py``

**Changes Made**:

1. **Import Addition**:
   
   .. code-block:: python

       from core.auto_response import request_llm_delivery

2. **Updated Event Delivery Method**:
   
   .. code-block:: python

       # Use auto-response system for autonomous event notifications
       await request_llm_delivery(
           message=None,  # No original message for autonomous events
           interface=bot,  # Use telegram bot interface
           context=event_prompt,
           reason=f"scheduled_event_{event['id']}"
       )

3. **Updated Scheduled Action Processing**:
   
   .. code-block:: python

       # Use auto-response system for autonomous scheduled event execution
       await request_llm_delivery(
           message=unified_message,
           interface=None,  # Let auto-response determine interface
           context=scheduled_prompt,
           reason=f"scheduled_action_{event_id}"
       )

**Purpose**: Routes scheduled events and autonomous event notifications through the LLM for better formatting, context-aware delivery, and intelligent response generation.

Benefits of Extension
---------------------

1. **Unified Response Flow**
   All autonomous interface interactions now flow through the LLM, ensuring consistent AI-enhanced responses across platforms.

2. **Context Preservation**
   The auto-response system maintains conversation context and original message coordinates for proper delivery.

3. **Enhanced User Experience**
   Users receive AI-formatted, context-aware responses from all interfaces, not just direct commands.

4. **Scalability**
   New interfaces can easily adopt the same pattern for autonomous interactions.

5. **Error Handling**
   Consistent error handling and logging across all autonomous interface operations.

Migration Notes
---------------

**Before**: Interfaces directly called ``plugin_instance.handle_incoming_message()``

**After**: Interfaces use ``request_llm_delivery()`` with appropriate context and reason codes

**Compatibility**: The changes are backward compatible - existing manual interactions still work through the normal action system.

**Testing**: All syntax has been verified. Full functional testing requires proper environment configuration.

Future Enhancements
-------------------

- **X Interface**: Full autonomous posting and response system
- **Reddit Interface**: Enhanced subreddit monitoring and autonomous participation
- **Event Plugin**: More sophisticated event-triggered actions and notifications
- **Additional Interfaces**: Discord, Matrix, or other platforms using the same pattern

The auto-response system provides a solid foundation for expanding synth's autonomous capabilities across multiple platforms while maintaining consistent AI-enhanced interactions.
