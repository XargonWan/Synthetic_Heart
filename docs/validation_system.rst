Dynamic Component Validation System
====================================

Overview
--------

The dynamic component validation system replaces hardcoded validation rules in the corrector with a flexible system that allows components to register their own validation rules.

Key Features
------------

Centralized Validation
~~~~~~~~~~~~~~~~~~~~~~
- Validation rules are centrally registered in the ``ValidationRegistry``
- Removes hardcoding from the corrector
- Modular and extensible system

Automatic Registration
~~~~~~~~~~~~~~~~~~~~~
- Existing components are automatically registered at startup
- Compatible with the legacy system
- Gradual migration without breaking changes

Dynamic Management
~~~~~~~~~~~~~~~~~
- Components can be registered/removed at runtime
- Validation rules are automatically updated
- No residue when a component is removed

Architecture
------------

Core Components
~~~~~~~~~~~~~~~

**ValidationRegistry** (``core/validation_registry.py``)
    Manages validation rules for all components. Validates action payloads against registered rules. Thread-safe and performance-oriented.

**ComponentRegistry** (``core/component_registry.py``)
    Manages component registration. Facilitates registration from JSON configurations. Maintains component metadata.

**ComponentAutoRegistration** (``core/component_auto_registration.py``)
    Automatic registration from existing plugins/interfaces. Backward compatibility with legacy system. Auto-discovery of rules from ``get_supported_actions()`` methods.

Integration with Action Parser
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The system is integrated into the existing action_parser:

.. code-block:: python

    def _validate_payload(action_type: str, payload: dict, errors: List[str]) -> None:
        # First: new centralized system
        validation_registry = get_validation_registry()
        registry_errors = validation_registry.validate_action_payload(action_type, payload)
        errors.extend(registry_errors)
        
        # Then: legacy system (for compatibility)
        # ... existing plugin/interface validation code ...

Usage for Developers
--------------------

Method 1: Using Existing System (Recommended)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Components can simply define ``required_fields`` in their ``get_supported_actions()`` methods:

.. code-block:: python

    class MyPlugin:
        def get_supported_actions(self):
            return {
                "send_message": {
                    "description": "Send a message",
                    "required_fields": ["text", "chat_id"],  # Automatic validation
                    "optional_fields": ["parse_mode"]
                }
            }

Method 2: Manual Registration
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

For more granular control:

.. code-block:: python

    from core.component_registry import register_component_validation

    class AdvancedPlugin:
        def __init__(self):
            register_component_validation("my_plugin", "plugin", {
                "actions": {
                    "complex_action": {
                        "required_fields": ["param1", "param2"]
                    }
                }
            })

Method 3: Custom Validation
~~~~~~~~~~~~~~~~~~~~~~~~~~~

For complex validation logic:

.. code-block:: python

    def custom_validator(payload):
        errors = []
        if payload.get("start_date") > payload.get("end_date"):
            errors.append("start_date must be before end_date")
        return errors

    rule = ValidationRule(
        action_type="date_range_action",
        required_fields=["start_date", "end_date"],
        custom_validator=custom_validator
    )

Corrector Functionality
-----------------------

Updated Behavior
~~~~~~~~~~~~~~~

1. **Error for non-existent actions** (as before)
   - If the action_type is not supported by any component
   - Message: ``"Unsupported type 'action_name' - no plugin or interface found to handle it"``

2. **Error for missing fields** (new)
   - Automatic validation of ``required_fields``
   - Message: ``"Missing required field 'field_name' for action 'action_type'"``

3. **Error for empty fields** (new)
   - Required fields cannot be empty or null
   - Message: ``"Field 'field_name' cannot be empty for action 'action_type'"``

4. **Unknown action handling & corrector** (new)  
   - If the LLM emits an unknown or misnamed action (for example ``"message_send"`` or a plugin-specific action that doesn't exist), the system will NOT perform silent conversions. Instead:
     - The action is validated against the current registry of supported action types (from the Validation Registry and registered interfaces).
     - If validation fails (unsupported type or invalid parameters), the **corrector** will be invoked to request a selective correction from the LLM. This ensures the LLM is explicitly asked to fix names or payloads and prevents implicit guessing by the core.
     - Component authors or integrators can register explicit alias resolvers via ``get_validation_registry().register_action_alias(alias_name, resolver)`` when an environment requires it, but this is opt-in and not applied automatically.

No Hardcoding
~~~~~~~~~~~~~

The corrector no longer contains hardcoded rules for specific components. All rules are dynamically discovered and managed through the registration system.

Practical Examples
------------------

Telegram Plugin
~~~~~~~~~~~~~~

.. code-block:: python

    def get_supported_actions(self):
        return {
            "send_message": {
                "required_fields": ["text", "chat_id"],
                "optional_fields": ["parse_mode", "reply_markup"]
            },
            "send_photo": {
                "required_fields": ["photo", "chat_id"], 
                "optional_fields": ["caption"]
            }
        }

Weather Plugin
~~~~~~~~~~~~~

.. code-block:: python

    def get_supported_actions(self):
        return {
            "weather_request": {
                "required_fields": ["location"],
                "optional_fields": ["units", "forecast_days"]
            }
        }

Migration
---------

Existing Components
~~~~~~~~~~~~~~~~~~

No modifications are required for existing components that already implement ``get_supported_actions()`` with ``required_fields``. The system registers them automatically.

New Components
~~~~~~~~~~~~~

Use the recommended pattern above to define validation rules.

Component Removal
~~~~~~~~~~~~~~~~

When a component is removed from the code, its validation rules are automatically removed from the registry, eliminating any trace from the core.

Benefits
--------

1. **Modularity**: Each component manages its own rules
2. **Maintainability**: No central hardcoding to maintain
3. **Flexibility**: Dynamic and customizable rules
4. **Performance**: Efficient validation with caching
5. **Backward Compatibility**: Gradual migration support
6. **Clean Architecture**: Separation of concerns

Testing
-------

The system includes automatic tests to verify:

- Required field validation
- Empty field handling
- Component registration/removal
- Non-existent action detection

To run the tests:

.. code-block:: bash

    python3 test_validation_system.py

Implementation Notes
-------------------

- The system is thread-safe and can be used in concurrent environments
- Rules are cached for optimal performance
- Graceful degradation if the validation system fails
- Complete logging for debugging and monitoring
- Seamless integration with the existing system

API Reference
-------------

ValidationRule Class
~~~~~~~~~~~~~~~~~~~

.. code-block:: python

    class ValidationRule:
        def __init__(self, action_type: str, required_fields: List[str] = None, 
                     custom_validator: callable = None, component_name: str = None)

ValidationRegistry Class
~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

    class ValidationRegistry:
        def register_component_rules(self, component_name: str, rules: List[ValidationRule])
        def unregister_component(self, component_name: str)
        def validate_action_payload(self, action_type: str, payload: Dict[str, Any]) -> List[str]
        def get_supported_action_types(self) -> Set[str]

Helper Functions
~~~~~~~~~~~~~~~

.. code-block:: python

    def register_component_validation(component_name: str, component_type: str, 
                                    json_config: Dict[str, Any]) -> ComponentDescriptor

    def unregister_component_validation(component_name: str)

    def auto_register_all_components()

Payload Normalization
~~~~~~~~~~~~~~~~~~~~~

Before validation, the action parser automatically normalizes payloads to make the system more flexible.

**Normalization Rules** (``_normalize_payload`` in ``core/action_parser.py``):

- String numbers are converted to integers for numeric ID fields
- Supported fields: ``thread_id``, ``chat_id``, ``user_id``, ``message_id``, ``animation_state``, and any field ending with ``_id``
- Applies to both top-level fields and nested dictionaries
- Non-numeric strings are left unchanged
- Already-integer values remain unchanged

**Example:**

.. code-block:: python

    # Input (from LLM)
    {
        "type": "message_telegram_bot",
        "payload": {
            "text": "Hello",
            "target": "-1003098886330",
            "thread_id": "2"        # String!
        }
    }
    
    # After normalization
    {
        "type": "message_telegram_bot",
        "payload": {
            "text": "Hello",
            "target": "-1003098886330",
            "thread_id": 2          # Converted to int
        }
    }

This eliminates trivial validation errors (like ``payload.thread_id must be an int``) without requiring LLM correction, keeping the system flexible and user-friendly.

