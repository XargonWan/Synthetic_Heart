Synthetic Heart Documentation
===================================

.. image:: res/synth_banner.png
   :alt: Synthetic Heart banner
   :align: center
   :width: 100%


.. container:: hero-box

   The Documentation for **Synthetic Heart** is below; use the quickstart to get
   running and the User / Developer sections to explore features and internals.


Welcome to the **Synthetic Heart** documentation. These pages are built
with Sphinx and hosted on **Read the Docs**. Every push to the repository
triggers a new build of this wiki.

The following sections provide an overview of the project and instructions for
getting started.

.. toctree::
   :maxdepth: 2
   :caption: User guide:

   quickstart
   usage
   features
   cortex
   interfaces
   peer_synths
   plugins
   self_growth
   external_endpoints
   auris_vox
   radio_azuracast
   vrm_animations
   animation_system
   webui_controls
   synth_stage_frontend
   gemini/synth-live-voice-integration
   faq

.. toctree::
   :maxdepth: 2
   :caption: Developer guide:

   architecture
   prompt_pipeline
   auto_response
   chat_instructions
   compose_env_vars
   COMPONENT_DEVELOPMENT_GUIDE
   dev_components
   api_endpoints
   component_pattern
   two_phase_init_implementation
   config_management
   prompt_engine_json_prompt
   memory_search_and_management
   CHANGELOG
   contributing

Building the Documentation
--------------------------

Install the documentation requirements from the repository root and run:

.. code-block:: bash

   sphinx-build -b html docs docs/_build/html

The generated HTML files will be available under ``docs/_build/html``.
