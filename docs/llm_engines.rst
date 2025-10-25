LLM Engines
===========

The Synthetic Heart supports multiple language model backends through a modular engine system. Engines are automatically discovered and can be switched at runtime using the ``/llm`` command. This design ensures that LLM implementations are completely decoupled from the core system.

Engine Architecture
-------------------

All LLM engines follow a consistent architecture:

- **Auto-Discovery**: Engines are automatically found in the ``llm_engines/`` directory
- **Standard Interface**: All engines extend ``AIPluginBase`` for consistent integration
- **Capability Reporting**: Engines declare their supported models and features
- **Dynamic Switching**: Active engine can be changed without restarting the system
- **Unified Limits**: Engines report their constraints (token limits, modalities, etc.)

Selenium Plugin Architecture
-----------------------------

The Selenium-based LLM engines follow a standardized architecture to minimize code duplication and ensure consistent behavior across different browser-based services like ChatGPT, Grok, and Gemini.

**Core Design**

The architecture is built around a shared base class ``SeleniumLLMBase`` that handles common functionality:

- **Driver Management**: Centralized browser driver lifecycle and configuration
- **Workflow Standardization**: Consistent prompt sending and response waiting logic
- **Response Extraction**: Standardized text extraction with multiple fallback selectors
- **Error Handling**: Robust error recovery and logging

**Standardized Methods**

All Selenium engines inherit these core methods from ``SeleniumLLMBase``:

+------------------+------------------------------------------------+-----------------+
| Method           | Purpose                                        | Override?       |
+==================+================================================+=================+
| ``_locate_prompt_area()`` | Find input textarea/contenteditable area   | ❌ No (subclass) |
+------------------+------------------------------------------------+-----------------+
| ``_get_response_selectors()`` | Return CSS selectors for responses       | ✅ **Yes**      |
+------------------+------------------------------------------------+-----------------+
| ``_extract_response_text()`` | Extract response using selectors          | ⚠️ Optional*    |
+------------------+------------------------------------------------+-----------------+
| ``_send_prompt_with_confirmation()`` | Send prompt and wait for confirmation | ❌ No (subclass) |
+------------------+------------------------------------------------+-----------------+
| ``_ensure_logged_in()`` | Check if user is logged in               | ❌ No (subclass) |
+------------------+------------------------------------------------+-----------------+

*Optional: Only override if service has special dialog handling

**Response Extraction Flow**

The base class implements a robust response extraction system:

1. **Selector Priority**: Try each selector from ``_get_response_selectors()`` in order
2. **Latest Response**: Return the last matching element (most recent response)
3. **Text Extraction**: Try ``.text`` first, fallback to ``textContent`` attribute
4. **Stabilization**: Wait for response text to stop changing before returning

**Adding New Selenium Engines**

To add support for a new browser-based LLM service:

1. **Create Plugin File**: Extend ``SeleniumLLMBase`` in ``llm_engines/``
2. **Implement Required Methods**: Provide service-specific selectors and logic
3. **Define Response Selectors**: Return CSS selectors for response extraction
4. **Test Integration**: Verify with real service and adjust selectors as needed

**Response Choice Handling**

Some LLM services (like ChatGPT) offer users multiple response versions. The Selenium architecture automatically handles this:

- **Automatic Detection**: Checks for choice buttons using service-specific selectors
- **First Choice Selection**: Automatically selects the first available option
- **Fallback Behavior**: Continues normally if no choices are available
- **Service-Specific**: Each engine provides its own ``_get_response_choice_selectors()`` method

**Example Implementation**

.. code-block:: python

   from core.selenium_llm_base import SeleniumLLMBase
   
   class SeleniumGrokPlugin(SeleniumLLMBase):
       display_name = "Selenium Grok"
       
       def __init__(self, notify_fn=None):
           grok_config = {
               "service_url": "https://grok.com",
               "model": "grok",
               "interface_name": "grok"
           }
           super().__init__(config=grok_config, notify_fn=notify_fn)
       
       def _get_response_selectors(self) -> list:
           """Get CSS selectors for Grok responses."""
           return [
               "div.grok-response",  # Primary selector
               "[data-testid='grok-message']",  # Fallback
               ".response-text",  # Generic fallback
           ]

Available Engines
-----------------

**Stable Engines:**

* ``selenium_chatgpt_legacy`` – Legacy version of the ChatGPT Selenium engine. This is the only fully functional LLM engine at present.

**Experimental/Development Engines:**

* ``manual`` – Forward prompts to a human trainer for manual responses (useful for debugging and development).
* ``selenium_chatgpt`` – Drive a browser-based ChatGPT session using the standardized Selenium architecture. Supports automatic response choice handling and large prompts.
* ``selenium_gemini`` – Browser-controlled Google Gemini using the standardized Selenium architecture. Experimental, supports multiple Gemini models.
* ``selenium_grok`` – Browser-controlled xAI Grok using the standardized Selenium architecture. Experimental, supports Grok models.

Manual Engine
-------------

The ``manual`` engine forwards all prompts to a human trainer instead of an AI model:

- **Debugging Tool**: Useful for testing interfaces and workflows without API costs
- **Development Aid**: Allows manual inspection of prompts and responses
- **No Configuration**: Works immediately without API keys or external dependencies
- **Trainer Feedback**: Responses are sent back through the normal message flow

Selenium ChatGPT Engine
-----------------------

The ``selenium_chatgpt`` engine controls a real ChatGPT browser session using the standardized Selenium architecture:

- **Standardized Architecture**: Built on ``SeleniumLLMBase`` for consistent behavior
- **Full Browser Control**: Uses Selenium to interact with ChatGPT web interface
- **Response Choice Handling**: Automatically selects first response when ChatGPT offers multiple options
- **Enhanced Prompt Limits**: Supports prompts up to 100,000 characters (previously limited to 10,000)
- **Captcha Handling**: Manual intervention required for initial setup and captchas
- **Visual Desktop**: Optional web interface at ``http://<host>:5006`` for monitoring
- **Model Selection**: Supports different ChatGPT models via ``CHATGPT_MODEL``

**Key Features:**

- **Robust Response Extraction**: Multiple CSS selectors with fallback logic
- **Automatic Choice Selection**: Handles ChatGPT's multiple response options
- **Large Prompt Support**: Complete JSON prompts up to 100,000 characters
- **Error Recovery**: Graceful handling of network issues and browser problems

Setup Steps:

1. Start the system with ``docker compose up``
2. Access ``http://<host>:5006`` in your browser
3. Complete ChatGPT login and captcha verification
4. synth can then interact with ChatGPT in real-time

**Configuration:**

.. code-block:: bash

   CHATGPT_MODEL=gpt-4  # Optional, defaults to gpt-4

**Response Selectors:**

The engine uses these CSS selectors for response extraction (tried in order):

- ``div.markdown.prose`` (primary)
- ``[data-message-author-role='assistant']`` (fallback)
- ``div.markdown`` (generic fallback)

**Troubleshooting:**

- **Prompt Truncation**: If prompts appear truncated, check that the limit is set to 100,000 characters
- **Response Selection**: Verify CSS selectors are current if responses aren't extracted properly
- **Choice Handling**: Check logs for "Checking for response choice buttons" messages

Selenium ChatGPT Legacy Engine
------------------------------

The ``selenium_chatgpt_legacy`` engine is a legacy version of the ChatGPT Selenium engine for backward compatibility with older setups. It does not use the standardized ``SeleniumLLMBase`` architecture and may have limitations with prompt length and response handling. Consider migrating to ``selenium_chatgpt`` for improved functionality.
The legacy engine will be removed as soon as the new engine is fully stable.

Selenium Gemini Engine
----------------------

The ``selenium_gemini`` engine controls a Google Gemini browser session using the standardized Selenium architecture:

- **Standardized Architecture**: Built on ``SeleniumLLMBase`` for consistent behavior
- **Model Support**: Gemini 2.5 Flash, 2.0 Flash, 1.5 Flash, 1.5 Pro with automatic limit detection
- **Multimodal**: Supports image inputs and analysis
- **Character Limits**: Up to 500k characters for Pro models
- **Browser Control**: Uses Selenium for web interface interaction
- **Response Extraction**: Robust selector-based text extraction

Configuration:

.. code-block:: bash

   GEMINI_MODEL=2.5-flash  # Optional, defaults to 2.5-flash

Selenium Grok Engine
--------------------

The ``selenium_grok`` engine controls an xAI Grok browser session using the standardized Selenium architecture:

- **Standardized Architecture**: Built on ``SeleniumLLMBase`` for consistent behavior
- **Advanced Reasoning**: Access to Grok's reasoning capabilities
- **Vision Support**: Grok Vision Beta for image analysis
- **Large Context**: Up to 128k tokens context window
- **Browser-Based**: Selenium-driven interaction with web interface
- **Response Extraction**: Robust selector-based text extraction

Configuration:

.. code-block:: bash

   GROK_MODEL=grok-beta  # Optional, defaults to grok-beta

Engine Registration and Discovery
---------------------------------

LLM engines are automatically discovered through the core initializer:

1. **Directory Scanning**: Core scans ``llm_engines/`` for Python files
2. **Class Inspection**: Files are checked for ``PLUGIN_CLASS`` attribute
3. **Registry Registration**: Engines register with the LLM registry
4. **Capability Indexing**: Engine capabilities are indexed for runtime selection
5. **Dynamic Loading**: Engines can be loaded/unloaded without system restart

Developing LLM Engines
----------------------

Creating a new LLM engine requires extending ``AIPluginBase`` and implementing the core methods:

.. code-block:: python

   from core.ai_plugin_base import AIPluginBase
   from core.transport_layer import llm_to_interface

   class MyEngine(AIPluginBase):
       def __init__(self, notify_fn=None):
           self.notify_fn = notify_fn

       async def handle_incoming_message(self, bot, message, prompt):
           """Process a message and generate response."""
           # Generate response using your LLM
           reply = await self.generate_response(prompt)
           
           # Send response back through the interface
           await llm_to_interface(bot.send_message, chat_id=message.chat_id, text=reply)
           return reply

       async def generate_response(self, messages):
           """Core LLM interaction method."""
           # Implement your model API calls here
           # messages is a list of message objects with role/content
           response = await call_my_llm_api(messages)
           return response

       def get_supported_models(self) -> list[str]:
           """Return available model names."""
           return ["my-model-v1", "my-model-v2"]

       def get_rate_limit(self):
           """Return (requests_per_hour, time_window_seconds, burst_limit)."""
           return (100, 3600, 10)  # 100 requests/hour with 10 burst

   # Required: Export the engine class
   PLUGIN_CLASS = MyEngine

Developing Selenium Engines
~~~~~~~~~~~~~~~~~~~~~~~~~~~

For browser-based LLM services, extend ``SeleniumLLMBase`` instead of ``AIPluginBase``:

.. code-block:: python

   from core.selenium_llm_base import SeleniumLLMBase
   
   class SeleniumMyService(SeleniumLLMBase):
       display_name = "Selenium MyService"
       
       def __init__(self, notify_fn=None):
           config = {
               "service_url": "https://my-service.com",
               "model": "my-model",
               "interface_name": "my-service"
           }
           super().__init__(config=config, notify_fn=notify_fn)
       
       def _get_response_selectors(self) -> list:
           """Return CSS selectors for response extraction."""
           return [
               "div.response-container",  # Primary selector
               ".message-content",        # Fallback
               "article p",               # Generic fallback
           ]
       
       def _locate_prompt_area(self, driver, timeout: int = 10):
           """Find the input area for your service."""
           # Implement service-specific logic to locate input field
           pass
       
       def _ensure_logged_in(self, driver) -> bool:
           """Check if user is logged in to your service."""
           # Implement login detection logic
           pass

**Key Methods to Implement:**

- ``_get_response_selectors()``: Return prioritized list of CSS selectors for response text
- ``_locate_prompt_area()``: Find and return the input textarea/contenteditable element
- ``_ensure_logged_in()``: Verify user authentication status
- ``_send_prompt_with_confirmation()``: Send prompt (usually inherited, override only if needed)

**Response Choice Handling:**

If your service offers multiple response options, override ``_get_response_choice_selectors()``:

.. code-block:: python

   def _get_response_choice_selectors(self) -> list:
       """Return selectors for response choice buttons."""
       return [
           "button.response-choice",  # Primary
           "div.options button:first-child",  # Fallback
       ]

**Testing Selenium Engines:**

1. **Manual Testing**: Run container in non-headless mode to observe browser interaction
2. **Selector Testing**: Check logs for "Trying response selector" messages
3. **Integration Testing**: Send test messages and verify response extraction
4. **Choice Testing**: Test with services that offer multiple response options

Engine Integration
------------------

Once created, register your engine with the LLM registry:

.. code-block:: python

   from core.llm_registry import get_llm_registry
   get_llm_registry().register_engine_module("my_engine", "llm_engines.my_engine")

Switch to your engine at runtime:

.. code-block:: text

   /llm my_engine

Engine Capabilities
-------------------

Engines report their capabilities to the system:

- **Model List**: Available models and their identifiers
- **Token Limits**: Maximum prompt and response lengths
- **Modalities**: Support for text, images, audio, etc.
- **Rate Limits**: API constraints and throttling requirements
- **Features**: Function calling, streaming, fine-tuning support

These capabilities are used by the prompt engine to construct appropriate prompts and by the interface layer to handle different content types.

Best Practices
--------------

**Error Handling**
    Implement robust error handling with user-friendly messages.

**Rate Limiting**
    Respect API limits and implement backoff strategies.

**Token Management**
    Track token usage and handle context window limitations.

**Async Operations**
    Use async methods for all I/O operations to maintain responsiveness.

**Security**
    Never log API keys or sensitive authentication data.

**Selenium-Specific Best Practices:**

**Selector Robustness**
    Provide multiple CSS selectors with fallbacks for response extraction.

**DOM Stability**
    Wait for elements to stabilize before interaction to handle dynamic content.

**Browser Resource Management**
    Monitor browser memory usage and implement cleanup for long-running sessions.

**Network Resilience**
    Handle network timeouts and implement retry logic for browser operations.

**Login State Management**
    Regularly verify authentication status and handle re-authentication gracefully.

For complete examples, examine ``llm_engines/selenium_chatgpt.py`` (standardized architecture), ``llm_engines/selenium_gemini.py``, or ``llm_engines/selenium_grok.py`` in the repository.
