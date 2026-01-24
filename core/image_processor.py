# core/image_processor.py
"""Image processing system for forwarding images to LLM with access control."""

import os
from typing import Optional, Dict, Any, Tuple, Union
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.config import get_trainer_id
from core.interfaces_registry import get_interface_registry
from core.abstract_context import AbstractContext, AbstractUser, AbstractMessage
from core.config_manager import config_registry

# Register access control configuration
RESTRICT_ACTIONS = config_registry.get_var(
    "RESTRICT_ACTIONS",
    "off",
    label="Restrict Sensitive Content Actions",
    description="Controls who can send images, audio, video, and other sensitive content to the LLM: 'off' (everyone), 'trainer_only' (only trainer), 'deny_all' (nobody including trainer)",
    group="core",
    component="core",
    constraints={"choices": ["off", "trainer_only", "deny_all"]},
)


class ImageProcessor:
    """Core image processing system with access control."""
    
    def __init__(self):
        log_info(f"[image_processor] Initialized with RESTRICT_ACTIONS={str(RESTRICT_ACTIONS).lower()}")
    
    def _get_restrict_mode(self) -> str:
        """Get current restrict mode from ConfigVar."""
        return str(RESTRICT_ACTIONS).lower()
    
    def _check_access_permissions(self, user_id: Union[int, str], interface_name: str, has_trigger: bool) -> Tuple[bool, str]:
        """
        Check if user has permission to send images to LLM.
        
        Args:
            user_id: User ID
            interface_name: Interface name (e.g., 'telegram_bot', 'discord_bot')
            has_trigger: Whether the message contains trigger words/mentions
            
        Returns:
            Tuple of (allowed, reason)
        """
        restrict_mode = self._get_restrict_mode()
        
        # Check for deprecated "deny_all" mode (same as "on")
        if restrict_mode == "deny_all":
            return False, "Image processing is completely disabled (RESTRICT_ACTIONS=deny_all)"
        
        # "on" mode: nobody can send images, not even the trainer
        if restrict_mode == "on":
            return False, "Image processing is completely disabled (RESTRICT_ACTIONS=on)"
        
        if not has_trigger:
            return False, "Message does not contain bot trigger"
        
        # "off" mode: everyone can send images
        if restrict_mode == "off":
            return True, "Image processing is open to all users (RESTRICT_ACTIONS=off)"
        
        # "trainer_only" mode: only trainers can send images
        if restrict_mode == "trainer_only":
            # Check if user is trainer for this interface
            registry = get_interface_registry()
            is_trainer = registry.is_trainer(interface_name, user_id)
            
            if not is_trainer:
                return False, f"Image processing restricted to trainers only (user {user_id} is not trainer for {interface_name})"
            
            return True, f"Access granted to trainer {user_id} for interface {interface_name}"
        
        return False, f"Unknown RESTRICT_ACTIONS mode: {restrict_mode}"
    
    async def should_process_image(self, context: AbstractContext, has_trigger: bool = False) -> Tuple[bool, str]:
        """
        Determine if an image should be processed and sent to LLM.
        
        Args:
            context: Abstract context containing user/interface info
            has_trigger: Whether the message contains trigger words/mentions
            
        Returns:
            Tuple of (should_process, reason)
        """
        
        if not context.user:
            return False, "No user information available"
        
        if not context.interface_name:
            return False, "No interface information available"
        
        user_id = context.user.id
        interface_name = context.interface_name
        
        allowed, reason = self._check_access_permissions(user_id, interface_name, has_trigger)
        
        log_debug(f"[image_processor] Access check for user {user_id} on {interface_name}: {allowed} - {reason}")
        
        return allowed, reason
    
    async def prepare_image_for_llm(self, image_data: Dict[str, Any], context: AbstractContext) -> Optional[Dict[str, Any]]:
        """
        Prepare image data for sending to LLM.
        
        Args:
            image_data: Image data from interface (file_id, url, path, etc.)
            context: Abstract context
            
        Returns:
            Processed image data ready for LLM or None if processing failed
        """
        
        try:
            # Basic validation
            if not image_data:
                log_warning("[image_processor] No image data provided")
                return None
            
            # Prepare standard format for LLM
            llm_image_data = {
                "type": "image",
                "source": {
                    "interface": context.interface_name,
                    "user_id": context.user.id if context.user else None,
                    "chat_id": context.get_chat_id(),
                    "message_id": context.message.id if context.message else None
                },
                "image_data": image_data,
                "metadata": {
                    "timestamp": getattr(context.message, 'timestamp', None) if context.message else None,
                    "caption": image_data.get("caption", ""),
                    "mime_type": image_data.get("mime_type", ""),
                    "file_size": image_data.get("file_size", 0)
                }
            }
            
            log_info(f"[image_processor] Prepared image for LLM: {llm_image_data['source']}")
            return llm_image_data
            
        except Exception as e:
            log_error(f"[image_processor] Error preparing image for LLM: {e}")
            return None
    
    async def forward_to_llm(self, processed_image_data: Dict[str, Any]) -> bool:
        """
        Forward processed image data to LLM system.
        
        Args:
            processed_image_data: Image data prepared for LLM
            
        Returns:
            True if successfully forwarded, False otherwise
        """
        try:
            # Import here to avoid circular imports
            from core.auto_response import request_llm_delivery
            
            # Create context for LLM request
            source_info = processed_image_data.get("source", {})
            
            llm_context = {
                "chat_id": source_info.get("chat_id"),
                "message_id": source_info.get("message_id"),
                "interface_name": source_info.get("interface"),
                "user_id": source_info.get("user_id"),
                "content_type": "image",
                "image_data": processed_image_data
            }
            
            # Create message content for LLM
            caption = processed_image_data.get("metadata", {}).get("caption", "")
            if caption:
                message_content = f"[Image received with caption: {caption}]"
            else:
                message_content = "[Image received without caption]"
            
            log_info(f"[image_processor] Forwarding image to LLM from user {source_info.get('user_id')}")
            
            # Send to LLM delivery system
            await request_llm_delivery(
                action_outputs=[message_content],
                original_context=llm_context,
                action_type="image_analysis"
            )
            
            return True
            
        except Exception as e:
            log_error(f"[image_processor] Error forwarding image to LLM: {e}")
            return False
    
    async def process_image_message(self, image_data: Dict[str, Any], context: AbstractContext, 
                                   has_trigger: bool = False, forward_to_llm: bool = True) -> Optional[Dict[str, Any]]:
        """
        Main entry point for processing image messages.
        
        Args:
            image_data: Image data from interface
            context: Abstract context
            has_trigger: Whether message contains trigger words/mentions
            forward_to_llm: Whether to automatically forward to LLM
            
        Returns:
            Processed image data for LLM or None if not allowed/failed
        """
        
        # Check access permissions
        should_process, reason = await self.should_process_image(context, has_trigger)
        
        if not should_process:
            log_debug(f"[image_processor] Image not processed: {reason}")
            return None
        
        log_info(f"[image_processor] Processing image: {reason}")
        
        # Prepare image for LLM
        processed_data = await self.prepare_image_for_llm(image_data, context)
        
        if processed_data and forward_to_llm:
            # Forward to LLM automatically
            success = await self.forward_to_llm(processed_data)
            if success:
                log_info("[image_processor] Image successfully forwarded to LLM")
            else:
                log_warning("[image_processor] Failed to forward image to LLM")
        
        return processed_data


# Global instance
_image_processor = None

def get_image_processor() -> ImageProcessor:
    """Get the global image processor instance."""
    global _image_processor
    if _image_processor is None:
        _image_processor = ImageProcessor()
    return _image_processor


async def process_image_message(image_data: Dict[str, Any], context: AbstractContext, 
                               has_trigger: bool = False, forward_to_llm: bool = True) -> Optional[Dict[str, Any]]:
    """
    Convenience function for processing image messages.
    
    Args:
        image_data: Image data from interface
        context: Abstract context
        has_trigger: Whether message contains trigger words/mentions
        forward_to_llm: Whether to automatically forward to LLM
        
    Returns:
        Processed image data for LLM or None if not allowed/failed
    """
    processor = get_image_processor()
    return await processor.process_image_message(image_data, context, has_trigger, forward_to_llm)


__all__ = [
    "ImageProcessor",
    "get_image_processor", 
    "process_image_message"
]
