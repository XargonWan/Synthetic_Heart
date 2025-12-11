# plugins/example_advanced_plugin.py
"""
Example plugin demonstrating the new Dynamic Component Validation System.

This plugin shows how to:
1. Use the standard get_supported_actions() format
2. Register custom validation rules  
3. Handle complex validation logic
4. Integrate seamlessly with the new validation system
"""

from core.core_initializer import register_plugin
from core.logging_utils import log_debug, log_info, log_warning


class ExampleAdvancedPlugin:
    """Example plugin demonstrating advanced validation features."""
    
    def __init__(self):
        register_plugin("example_advanced", self)
        
        # Register custom validation rules with the new system
        self._register_custom_validation()
        
        log_info("[example_advanced_plugin] Registered ExampleAdvancedPlugin")
    
    def _register_custom_validation(self):
        """Register custom validation rules with the new validation system."""
        try:
            from core.validation_registry import ValidationRule, get_validation_registry
            
            # Custom validator for file operations
            def validate_file_operation(payload):
                """Custom validation for file operations."""
                errors = []
                
                file_path = payload.get("file_path", "")
                operation = payload.get("operation", "")
                
                # Check file path safety
                dangerous_paths = ["/..", "/etc/", "/root/", "/usr/bin/"]
                if any(danger in file_path for danger in dangerous_paths):
                    errors.append("File path contains dangerous directory")
                
                # Check operation type
                allowed_operations = ["read", "write", "append", "delete"]
                if operation not in allowed_operations:
                    errors.append(f"Operation must be one of: {', '.join(allowed_operations)}")
                
                # Check permissions based on operation
                if operation == "delete" and not payload.get("confirm_delete"):
                    errors.append("Delete operations require confirm_delete=true")
                
                return errors
            
            # Custom validator for API calls
            def validate_api_call(payload):
                """Custom validation for API calls."""
                errors = []
                
                url = payload.get("url", "")
                method = payload.get("method", "").upper()
                
                # Check URL format
                if not url.startswith(("http://", "https://")):
                    errors.append("URL must start with http:// or https://")
                
                # Check HTTP method
                allowed_methods = ["GET", "POST", "PUT", "DELETE", "PATCH"]
                if method not in allowed_methods:
                    errors.append(f"HTTP method must be one of: {', '.join(allowed_methods)}")
                
                # Require authentication for certain methods
                if method in ["POST", "PUT", "DELETE"] and not payload.get("auth_token"):
                    errors.append("POST, PUT, and DELETE requests require auth_token")
                
                return errors
            
            # Create validation rules
            rules = [
                ValidationRule(
                    action_type="file_operation",
                    required_fields=["file_path", "operation"],
                    custom_validator=validate_file_operation,
                    component_name="example_advanced"
                ),
                ValidationRule(
                    action_type="api_call",
                    required_fields=["url", "method"],
                    custom_validator=validate_api_call,
                    component_name="example_advanced"
                ),
                ValidationRule(
                    action_type="simple_action",
                    required_fields=["message", "priority"],
                    component_name="example_advanced"
                )
            ]
            
            # Register with validation registry
            registry = get_validation_registry()
            registry.register_component_rules("example_advanced", rules)
            
            log_debug("[example_advanced_plugin] Registered custom validation rules")
            
        except Exception as e:
            log_warning(f"[example_advanced_plugin] Failed to register custom validation: {e}")
    
    def get_supported_actions(self):
        """Standard method for declaring supported actions - automatically discovered."""
        return {
            "file_operation": {
                "description": "Perform file system operations with safety checks",
                "required_fields": ["file_path", "operation"],
                "optional_fields": ["content", "confirm_delete", "backup"],
            },
            "api_call": {
                "description": "Make HTTP API calls with authentication",
                "required_fields": ["url", "method"],
                "optional_fields": ["headers", "auth_token", "timeout"],
            },
            "simple_action": {
                "description": "Simple action with basic validation",
                "required_fields": ["message", "priority"],
                "optional_fields": ["tags", "scheduled_time"],
            },
            "notification": {
                "description": "Send notifications to users",
                "required_fields": ["recipient", "message"],
                "optional_fields": ["type", "urgency"],
            }
        }
    
    def get_supported_action_types(self):
        """Alternative method to declare action types."""
        return ["file_operation", "api_call", "simple_action", "notification"]
    
    async def run_action(self, action_type: str, payload: dict, context: dict = None):
        """Execute the actions (implementation would go here)."""
        log_info(f"[example_advanced_plugin] Executing {action_type} with payload: {payload}")
        
        # Implementation would go here based on action_type
        if action_type == "file_operation":
            return await self._handle_file_operation(payload)
        elif action_type == "api_call":
            return await self._handle_api_call(payload)
        elif action_type == "simple_action":
            return await self._handle_simple_action(payload)
        elif action_type == "notification":
            return await self._handle_notification(payload)
        else:
            raise ValueError(f"Unsupported action type: {action_type}")
    
    async def _handle_file_operation(self, payload: dict):
        """Handle file operations."""
        # Implementation would go here
        log_debug(f"[example_advanced_plugin] File operation: {payload}")
        return {"status": "success", "message": "File operation completed"}
    
    async def _handle_api_call(self, payload: dict):
        """Handle API calls."""
        # Implementation would go here
        log_debug(f"[example_advanced_plugin] API call: {payload}")
        return {"status": "success", "message": "API call completed"}
    
    async def _handle_simple_action(self, payload: dict):
        """Handle simple actions."""
        # Implementation would go here
        log_debug(f"[example_advanced_plugin] Simple action: {payload}")
        return {"status": "success", "message": "Simple action completed"}
    
    async def _handle_notification(self, payload: dict):
        """Handle notifications."""
        # Implementation would go here
        log_debug(f"[example_advanced_plugin] Notification: {payload}")
        return {"status": "success", "message": "Notification sent"}


# Uncomment the following line to actually instantiate the plugin
# example_plugin = ExampleAdvancedPlugin()
