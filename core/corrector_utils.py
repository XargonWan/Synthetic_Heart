# core/corrector_utils.py
"""Utilities for the corrector to provide detailed action information."""

from typing import Dict, Any
from core.logging_utils import log_debug, log_warning
from core.action_schema_converter import extract_for_corrector, normalize_action_schema


def get_action_description_for_corrector(action_name: str, action_def: Dict[str, Any]) -> Dict[str, Any]:
    """Get complete action description for the corrector.
    
    When the LLM makes mistakes, we send the corrector more detailed information
    including examples and detailed descriptions.
    
    Parameters
    ----------
    action_name : str
        Name of the action
    action_def : Dict[str, Any]
        Action definition (old or new format)
        
    Returns
    -------
    Dict[str, Any]
        Complete action information with schema, brief, and examples
    """
    normalized = normalize_action_schema(action_name, action_def)
    return extract_for_corrector(action_name, normalized)


def build_corrector_actions_context(available_actions: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Build complete action context for the corrector.
    
    This includes all details: schema, brief description, and full examples.
    Used when the LLM makes mistakes and needs detailed guidance.
    
    Parameters
    ----------
    available_actions : Dict[str, Dict[str, Any]]
        Dictionary of all available actions
        
    Returns
    -------
    Dict[str, Dict[str, Any]]
        Complete action context for corrector
    """
    corrector_context = {}
    for action_name, action_def in available_actions.items():
        try:
            corrector_context[action_name] = get_action_description_for_corrector(action_name, action_def)
        except Exception as e:
            log_warning(f"[corrector_utils] Error building context for action '{action_name}': {e}")
            # Add minimal info to prevent breaking
            corrector_context[action_name] = {
                "schema": {},
                "brief": "Action (error building description)",
                "source": "unknown"
            }
    
    return corrector_context
