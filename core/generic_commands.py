# core/generic_commands.py

"""
Generic commands that work with any interface through AbstractContext.
"""

from core.abstract_context import AbstractContext
from core.context import context_command
from core.recent_chats import last_chats_command_generic
from core.logging_utils import log_debug, log_warning
from typing import Optional, Callable

async def generic_context_command(abstract_context: AbstractContext, reply_fn: Optional[Callable] = None):
    """Generic context command that works with any interface."""
    await context_command(abstract_context, reply_fn)

async def generic_last_chats_command(abstract_context: AbstractContext, reply_fn: Optional[Callable] = None, get_chat_info_fn: Optional[Callable] = None):
    """Generic last_chats command that works with any interface."""
    await last_chats_command_generic(abstract_context, reply_fn, get_chat_info_fn)

async def generic_help_command(abstract_context: AbstractContext, reply_fn: Optional[Callable] = None):
    """Generic help command that works with any interface."""
    if not abstract_context.is_trainer():
        return
    
    from core.command_registry import get_help_text
    help_text = get_help_text()
    
    if reply_fn:
        await reply_fn(help_text)

async def generic_diary_command(abstract_context: AbstractContext, reply_fn: Optional[Callable] = None, args: str = ""):
    """Generic diary command that shows recent AI diary entries."""
    if not abstract_context.is_trainer():
        return
    
    try:
        from plugins.ai_diary import get_recent_entries, format_diary_for_injection, is_plugin_enabled
        
        if not is_plugin_enabled():
            if reply_fn:
                await reply_fn("📔 Diary plugin is currently disabled or unavailable.")
            return
        
        # Parse arguments for days filter
        days = 7  # default
        if args.strip():
            try:
                days = int(args.strip())
                if days <= 0:
                    days = 7
            except ValueError:
                pass
        
        # Get recent entries (no char limit for manual viewing)
        try:
            days_arg = int(days)
        except Exception:
            days_arg = 2
        entries = get_recent_entries(days=days_arg, max_chars=None)
        
        if not entries:
            response = f"📔 No diary entries found in the last {days} days."
        else:
            response = f"📔 **synth's Diary - Last {days} days ({len(entries)} entries)**\n\n"
            response += format_diary_for_injection(entries)
            response += f"\n\n_Use `/diary <days>` to view a different time range._"
        
        if reply_fn:
            await reply_fn(response)
    
    except ImportError:
        if reply_fn:
            await reply_fn("📔 Diary plugin is not installed.")
    except Exception as e:
        log_warning(f"[generic_commands] Error in diary command: {e}")
        if reply_fn:
            await reply_fn("❌ Error retrieving diary entries.")

# Function to create interface-specific command wrappers
def create_command_wrapper(interface_name: str, adapter_fn: Callable, original_params_fn: Callable):
    """Create a wrapper that converts interface-specific parameters to AbstractContext."""
    def command_wrapper(generic_command_fn):
        async def wrapper(*args, **kwargs):
            try:
                # Convert interface-specific parameters to AbstractContext
                abstract_context = adapter_fn(*args, **kwargs)
                
                # Create a reply function from original parameters
                reply_fn = original_params_fn(*args, **kwargs)
                
                # Call the generic command
                await generic_command_fn(abstract_context, reply_fn)
            except Exception as e:
                log_warning(f"[generic_commands] Error in {interface_name} command wrapper: {e}")
        
        return wrapper
    return command_wrapper
