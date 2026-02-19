LLM Engines
===========

The Synthetic Heart supports multiple language model backends through a modular engine system. Engines are automatically discovered and can be switched at runtime using the ``/cortex`` command (``/llm`` remains as a deprecated alias). This design ensures that LLM implementations are completely decoupled from the core system.

Engine Architecture
-------------------

All LLM engines follow a consistent architecture:

- **Auto-Discovery**: Engines are automatically found in the ``llm_engines/`` directory
- **Standard Interface**: All engines extend ``AIPluginBase`` for consistent integration
- **Capability Reporting**: Engines declare their supported models and features
- **Dynamic Switching**: Active engine can be changed without restarting the system
- **Unified Limits**: Engines report their constraints (token limits, modalities, etc.)

Agent Hooks (optional)
~~~~~~~~~~~~~~~~~~~~~~~

Engines may optionally implement a small set of agent hooks to provide richer,
engine-specific integrations for the Agent plugin. These hooks are optional and
engines that do not implement them will degrade gracefully — the Agent will
fall back to calling ``plugin_instance.handle_incoming_message()`` and the
plugin-level handlers.

Recommended methods (all optional):

- ``supports_agent() -> bool`` — return True if the engine provides agentic features
- ``attach_agent(agent_plugin)`` / ``detach_agent(agent_plugin)`` — lifecycle hooks
- ``agent_prepare_prompt(context) -> dict`` — provide additional structured context
- ``agent_execute(action_dict, context) -> dict`` — optional engine-level action executor

Note: These hooks are intended to be lightweight extensions, not required
capabilities. The Agent integration remains fully functional with engines that
do nothing more than implement the standard ``AIPluginBase`` interface.

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

Authentication and Guest Mode
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Selenium-based LLM plugins can function in guest mode (without user authentication), but this comes with limitations:

- **Character Limits**: Guest mode typically has significantly reduced character limits compared to authenticated sessions, making SyntH appear less intelligent due to truncated prompts and responses
- **Recommended Setup**: Use a logged-in account through the webui (``http://<host>:5006``) for full functionality and higher character limits
- **Account Separation**: Create a dedicated account for SyntH separate from your personal account, as SyntH tends to flood chat history with frequent interactions

Available Engines
-----------------

**API Engines:**

* ``gemini_api`` – Direct REST API integration with Google Gemini. Supports multimodal input (images, audio, video, documents), correction/retry loops, and agentic hooks. See :doc:`gemini_api_engine` for full documentation.

**Stable Engines:**

* ``selenium_chatgpt_legacy`` – Legacy version of the ChatGPT Selenium engine. For backward compatibility only; consider migrating to the standardized ``selenium_chatgpt`` engine.

**Standardized Selenium Engines:**

All Selenium-based LLM engines now follow a consistent architecture for reliability and maintainability:

* ``selenium_chatgpt`` – Drive a browser-based ChatGPT session. Supports GPT-4, GPT-3.5-Turbo, and other OpenAI models with automatic response choice handling and large prompt support (up to 100k characters).
* ``selenium_grok`` – Browser-controlled xAI Grok with 128k token context window. Supports Grok and Grok Vision models for advanced reasoning and vision capabilities.
* ``selenium_gemini`` – Browser-controlled Google Gemini with multiple model support (Gemini 2.5 Flash, 1.5 Pro). Supports up to 500k characters for Pro models with multimodal capabilities.

**Other Engines:**

* ``manual`` – Forward prompts to a human trainer for manual responses (useful for debugging and development).

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

- **Standardized Architecture**: Built on ``SeleniumLLMBase`` for consistent behavior across all Selenium engines
- **Full Browser Control**: Uses Selenium to interact with ChatGPT web interface
- **Response Choice Handling**: Automatically selects first response when ChatGPT offers multiple options
- **Enhanced Prompt Limits**: Supports prompts up to 128,000 characters
- **Captcha Handling**: Manual intervention required for initial setup and captchas
- **Visual Desktop**: Optional web interface at ``http://<host>:5006`` for monitoring
- **Model Selection**: Supports different ChatGPT models via ``CHATGPT_MODEL`` environment variable
    - Preview models like ``gpt-5.1-codex-max`` are available (add to ``MODEL_LIMITS_MAP``).
        To enable the preview model for all clients, set the environment variable or component configuration to that model name, e.g.:
        ``CHATGPT_MODEL=gpt-5.1-codex-max``

**Key Features:**

- **Robust Response Extraction**: Multiple CSS selectors with fallback logic
- **Automatic Choice Selection**: Handles ChatGPT's multiple response options
- **Large Prompt Support**: Complete JSON prompts up to 128,000 characters
- **Error Recovery**: Graceful handling of network issues and browser problems

Setup Steps:

1. Start the system with ``docker compose up``
2. Access ``http://<host>:5006`` in your browser
3. Complete ChatGPT login and captcha verification
4. synth can then interact with ChatGPT in real-time

**Configuration:**

.. code-block:: bash

   CHATGPT_MODEL=gpt-4o  # Optional, defaults to gpt-4o
    # To enable GPT-5.1-Codex-Max (Preview) for all clients, you can set:
    # CHATGPT_MODEL=gpt-5.1-codex-max

**Response Selectors:**

The engine uses these CSS selectors for response extraction (tried in order):

- ``div.markdown.prose`` (primary)
- ``[data-message-author-role='assistant']`` (fallback)
- ``div.markdown`` (generic fallback)

**Troubleshooting:**

- **Prompt Truncation**: If prompts appear truncated, check the character limits for your model
- **Response Selection**: Verify CSS selectors are current if responses aren't extracted properly
- **Choice Handling**: Check logs for "Checking for response choice buttons" messages
- **Login Issues**: Ensure the browser window at ``http://<host>:5006`` has completed login

Selenium Gemini Engine
----------------------

The ``selenium_gemini`` engine controls a Google Gemini browser session using the standardized Selenium architecture:

- **Standardized Architecture**: Built on ``SeleniumLLMBase`` for consistent behavior
- **Model Support**: Gemini 2.5 Flash, 2.0 Flash, 1.5 Flash, 1.5 Pro with automatic limit detection
- **Multimodal**: Supports image inputs and analysis
- **Character Limits**: Up to 500k characters for Pro models
- **Browser Control**: Uses Selenium for web interface interaction
- **Response Extraction**: Robust selector-based text extraction

**Configuration:**

.. code-block:: bash

   GEMINI_MODEL=2.5-flash  # Optional, defaults to 2.5-flash

**Setup:**

1. Access ``http://<host>:5006`` to sign in to your Google account
2. Complete any authentication challenges
3. Switch to this engine with ``/cortex selenium_gemini`` (deprecated alias: ``/llm selenium_gemini``)

Selenium Grok Engine
--------------------

The ``selenium_grok`` engine controls an xAI Grok browser session using the standardized Selenium architecture:

- **Standardized Architecture**: Built on ``SeleniumLLMBase`` for consistent behavior
- **Advanced Reasoning**: Access to Grok's reasoning capabilities
- **Vision Support**: Grok Vision Beta for image analysis
- **Large Context**: Up to 128k tokens context window
- **Browser-Based**: Selenium-driven interaction with web interface
- **Response Extraction**: Robust selector-based text extraction

**Configuration:**

.. code-block:: bash

   GROK_MODEL=grok-beta  # Optional, defaults to grok-beta

**Setup:**

1. Access ``http://<host>:5006`` to log in to X/Grok
2. Complete login and any authentication challenges
3. Switch to this engine with ``/cortex selenium_grok`` (deprecated alias: ``/llm selenium_grok``)

Engine Registration and Discovery
---------------------------------

LLM engines are automatically discovered through the core initializer:

1. **Directory Scanning**: Core scans ``llm_engines/`` for Python files
2. **Class Inspection**: Files are checked for ``PLUGIN_CLASS`` attribute
3. **Registry Registration**: Engines register with the LLM registry
4. **Capability Indexing**: Engine capabilities are indexed for runtime selection
5. **Dynamic Loading**: Engines can be loaded/unloaded without system restart

Labeling engines for the WebUI
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Engines may provide a short, human-readable label used by the WebUI to help operators
choose the right engine for a task. Provide a label by either:

- exporting ``ENGINE_LABEL = "short description"`` in the engine module, or
- defining ``engine_label = "short description"`` on ``PLUGIN_CLASS``
- or passing ``label="short description"`` to ``CortexRegistry.register_engine_module``

These labels are surfaced in the Components page under "Cortex Engines" and are intended
to be brief and informative (one sentence).

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

Agentic Hooks (optional)
------------------------

LLM engines can optionally implement *agentic* hooks so the core Agent plugin
can attach and cooperate with engine-level features. These hooks are
non-mandatory and engines should degrade gracefully if the Agent plugin is
absent or disabled.

Suggested hooks for engines:

- ``supports_agent() -> bool`` — Return True if the engine provides agentic extensions.
- ``attach_agent(agent_plugin)`` / ``detach_agent(agent_plugin)`` — Called when the
  core Agent plugin attaches or detaches; engines can use this to cache the
  plugin reference or perform initialization.
- ``agent_prepare_prompt(context) -> dict`` — Return additional engine-specific
  prompt material.
- ``agent_execute(action_dict, context) -> dict`` — Optional execution helper;
  return a dict with execution result, or ``{"status": "unsupported"}``.

Engines must not raise exceptions if the Agent plugin is absent; calls should be
protected and degrade safely.

Engine Integration (Cortex)
----------------------------

Engines are now organized under the new **Cortex** abstraction. A cortex has a *kind* (e.g., ``llm``, ``live``, ``agent``) and one or more registered engines. Engines should register themselves with the Cortex registry.

.. code-block:: python

   from core.cortex_registry import get_cortex_registry
   cortex = get_cortex_registry()
   # Register an LLM engine (cortex='llm') or a live engine (cortex='live')
   cortex.register_engine_module("my_engine", "cortex.llm_engine.my_engine", cortex='llm')

Switch to a cortex engine at runtime using the Components tab in the WebUI
or via the CLI/commands (the UI now asks for the *cortex kind* first, then an engine):

.. code-block:: text

   # Select cortex kind (e.g., llm)
   /components set_active_cortex llm

   # Select specific engine for the cortex
   /components set_active_cortex_engine my_engine

Notes
-----
- Use the Cortex registry helpers to discover engines and their capabilities: ``get_cortex_registry().get_available_engines(cortex)``.
- Engines may optionally declare capability flags (vision, audio, actions, bidi, low_latency) when registered; these are used to choose the best engine for a given task.
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

Tutorial: Creating a New Selenium LLM Plugin
==============================================

This tutorial walks through creating a new Selenium-based LLM plugin from scratch, using the standardized ``SeleniumLLMBase`` architecture. We'll create a plugin for a hypothetical service called "MyLLM".

Step 1: Create the Plugin File
-------------------------------

Create a new file ``llm_engines/selenium_myllm.py``:

.. code-block:: python

   from core.selenium_llm_base import SeleniumLLMBase
   
   # Configuration constants
   SERVICE_URL = "https://myllm.example.com"
   MODEL_CONFIG_VAR = "MYLLM_MODEL"
   DEFAULT_MODEL = "myllm-standard"
   
   # Model limits mapping (character limits)
   MODEL_LIMITS_MAP = {
       "myllm-standard": 50000,
       "myllm-premium": 200000,
       "myllm-enterprise": 1000000,
       "default": 50000
   }
   
   class SeleniumMyLLMPlugin(SeleniumLLMBase):
       display_name = "Selenium MyLLM"
       
       def __init__(self, notify_fn=None):
           """Initialize the MyLLM plugin."""
           super().__init__(
               config={
                   "service_url": SERVICE_URL,
                   "interface_name": "myllm"
               },
               notify_fn=notify_fn
           )
           
           # Model configuration
           self.model_limits_map = MODEL_LIMITS_MAP
           self.model_config_var = MODEL_CONFIG_VAR
           self.default_model = DEFAULT_MODEL
           
           # Update limits
           self._update_interface_limits()
           
           # Define selectors for MyLLM service
           self.selectors["prompt_area"] = [
               "textarea#message-input",
               "div[contenteditable='true'].input-area",
               "textarea[placeholder*='Type your message']",
               "div.input-field[role='textbox']",
           ]
           
           self.selectors["send_button"] = [
               "button#send-message",
               "button[data-action='send']",
               "button[type='submit']",
               "button[aria-label*='Send']",
           ]
           
           self.selectors["response_text"] = [
               "div.message-response",
               ".chat-message.assistant",
               "[data-role='assistant-message']",
               ".response-content",
           ]
           
           # Optional: Modal dismissal selectors
           self.selectors["modal_dismissal"] = [
               "button.modal-close",
               ".modal button.close",
               "[data-dismiss='modal']",
           ]
           
           # Login detection selectors
           self.login_detection_selectors = [
               (By.CSS_SELECTOR, "button#login-btn"),
               (By.CSS_SELECTOR, "a[href*='login']"),
               (By.XPATH, "//button[contains(text(), 'Sign In')]"),
           ]
   
       def _ensure_logged_in(self, driver) -> bool:
           """Check if user is logged in to MyLLM."""
           try:
               current_url = driver.current_url
           except Exception:
               current_url = ""
           
           # Navigate to service if not there
           if not current_url.startswith("https://myllm.example.com"):
               driver.get(SERVICE_URL)
               time.sleep(2)
               current_url = driver.current_url
           
           # Check for login indicators
           if "login" in current_url or "auth" in current_url:
               if self._notify_fn:
                   self._notify_fn("🔐 Login required for MyLLM. Open UI to log in.")
               return False
           
           return True
   
       def get_supported_models(self) -> list:
           """Return list of supported models."""
           return list(MODEL_LIMITS_MAP.keys())
   
       def get_current_model(self) -> str:
           """Get current model, considering login status."""
           if not self.is_user_logged_in():
               return "default"
           return self._get_current_model_name()
   
       def get_interface_limits(self) -> dict:
           """Get interface limits."""
           self._update_interface_limits()
           return self.interface_limits
   
       # Optional: Override if service has response choices
       def _get_response_choice_selectors(self) -> list:
           """Selectors for response choice buttons."""
           return [
               "button.response-option",
               ".choice-buttons button:first-child",
           ]
   
   # Required: Export the plugin class
   PLUGIN_CLASS = SeleniumMyLLMPlugin

Step 2: Implement Service-Specific Logic
-----------------------------------------

Override methods as needed for your service:

**Login Detection (_ensure_logged_in):**

.. code-block:: python

   def _ensure_logged_in(self, driver) -> bool:
       """Service-specific login checking logic."""
       # Check current URL
       current_url = driver.current_url
       
       # Navigate if not on service
       if not current_url.startswith(self.service_url):
           driver.get(self.service_url)
           time.sleep(2)
       
       # Check for login page indicators
       login_indicators = driver.find_elements(By.CSS_SELECTOR, ".login-form, .auth-required")
       if login_indicators:
           self._notify_fn("🔐 Please log in to MyLLM service")
           return False
       
       return True

**Response Choice Handling (if applicable):**

.. code-block:: python

   def _get_response_choice_selectors(self) -> list:
       """Return selectors for multiple response choice buttons."""
       return [
           "button.choice-option",  # Primary
           ".response-choices button",  # Fallback
       ]

Web UI: Login flow endpoint
---------------------------

The Web UI provides an API to initiate an interactive login flow for Selenium-based
LLM engines. This is intended to start a browser session (Selkies/Chromium) so a
user can authenticate via the service's web interface.

Endpoint:

``POST /api/components/cortex/login``

Request JSON:

``{ "name": "selenium_chatgpt" }``

Typical success response (acknowledgement, non-blocking):

``{ "status": "ok", "name": "selenium_chatgpt", "action": "started", "logged_in": false }``

Notes:

- The login flow is started asynchronously and the endpoint returns immediately.
- Selkies availability is checked as a best-effort; absence does not prevent
    the flow from proceeding where possible, but a helpful error will be returned
    if the engine is not Selenium-based or not loaded.
- The client (Web UI) may poll ``GET /api/components`` to detect updates to
    the engine's ``login_state`` and ``logged_in`` fields.

Step 3: Define Robust Selectors
---------------------------------

**Prompt Area Selectors:**

Test multiple selectors in order of preference:

.. code-block:: python

   self.selectors["prompt_area"] = [
       "textarea#specific-id",           # Most specific
       "div[contenteditable='true']",    # Contenteditable divs
       "textarea[placeholder*='message']", # Placeholder matching
       "div.input-area",                 # Generic class
       "textarea",                       # Generic fallback
   ]

**Response Text Selectors:**

Provide multiple fallbacks for response extraction:

.. code-block:: python

   self.selectors["response_text"] = [
       "div.assistant-message",          # Service-specific
       "[data-author='assistant']",      # Data attributes
       ".chat-message.ai",               # Class-based
       ".response-content",              # Generic content
       "article p",                      # Generic article text
   ]

Step 4: Test the Plugin
-----------------------

1. **Start the system:**

   .. code-block:: bash

      docker compose -f docker-compose-dev.yml up -d

2. **Switch to your engine:**

   .. code-block:: text

      /llm selenium_myllm

3. **Monitor logs for selector attempts:**

   .. code-block:: bash

      docker compose -f docker-compose-dev.yml logs -f synth

4. **Test with a simple prompt:**

   Send a test message and check if the plugin:

   - Navigates to the correct URL
   - Finds the input area
   - Sends the prompt
   - Extracts the response

Step 5: Debug and Refine
-------------------------

**Common Issues:**

- **Selector not found**: Add more fallback selectors
- **Response not extracted**: Check response selectors in browser dev tools
- **Login not detected**: Verify login detection selectors
- **Modal blocking**: Add modal dismissal selectors

**Debug Logging:**

The base class logs selector attempts. Check logs for:

.. code-block:: text

   [selenium_base] Trying prompt selector: textarea#specific-id
   [selenium_base] Found prompt area with selector: textarea#specific-id
   [selenium_base] Trying response selector: div.assistant-message

**Browser Inspection:**

Run in non-headless mode to inspect elements:

.. code-block:: bash

   # In docker-compose-dev.yml, set headless: false temporarily
   environment:
     - SELENIUM_HEADLESS=false

Step 6: Add Configuration Variables
------------------------------------

Add configuration variables in ``core/config.py`` or use the web UI:

.. code-block:: python

   # In core/config.py
   MYLLM_MODEL = config_registry.get_var(
       "MYLLM_MODEL",
       "myllm-standard",
       label="MyLLM Model",
       description="Default model for MyLLM service",
       group="llm",
       component="selenium_myllm"
   )

Step 7: Document the Plugin
----------------------------

Update this documentation file to include your new plugin in the "Available Engines" section.

**Example Documentation Addition:**

.. code-block:: rst

   Selenium MyLLM Engine
   ---------------------

   The ``selenium_myllm`` engine controls a MyLLM browser session:

   - **Standardized Architecture**: Built on ``SeleniumLLMBase``
   - **Model Support**: Standard, Premium, and Enterprise models
   - **Character Limits**: Up to 1M characters for Enterprise
   - **Browser Control**: Selenium-driven web interface interaction

   Configuration:

   .. code-block:: bash

      MYLLM_MODEL=myllm-premium  # Optional, defaults to myllm-standard

Best Practices for New Plugins
-------------------------------

**Selector Strategy:**

1. **Specific first**: Use IDs, data attributes, unique classes
2. **Semantic second**: Use ARIA roles, content patterns
3. **Generic last**: Use tag names, common classes as fallbacks

**Error Handling:**

- Always provide fallbacks for critical selectors
- Log failures with context for debugging
- Use timeouts appropriate for your service

**Testing:**

- Test with different models/configurations
- Verify login detection works
- Check response extraction with various response types
- Test modal dismissal if applicable

**Maintenance:**

- Monitor service UI changes that might break selectors
- Update selectors when the service updates its interface
- Keep model limits current with service changes

**Performance:**

- Minimize selector complexity for faster element finding
- Use appropriate wait times for your service's responsiveness
- Consider caching frequently used elements when possible

By following this architecture, new LLM plugins can be created quickly and maintain consistency with existing engines while being easy to maintain and extend.
