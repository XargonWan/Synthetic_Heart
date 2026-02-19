Synthetic Heart Documentation
===================================

.. raw:: html

   <div align="center">
      <div style="background: #222; border-radius: 12px; padding: 12px; width: 700px; height: 300px; display: flex; align-items: center; justify-content: center; margin: 0 auto;">
         <img src="res/synth_logo.png" alt="Synthetic Heart Logo" style="max-width: 100%; max-height: 100%; object-fit: contain;" />
      </div>
   </div>


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
   plugins
   vrm_animations
   webui_controls
   gemini/synth-live-voice-integration
   faq

.. toctree::
   :maxdepth: 2
   :caption: Developer guide:

   component_development_guide
   dev_components
   component_pattern
   two_phase_init_implementation
   config_management
   contributing
   faq

Building the Documentation
--------------------------

Install the documentation requirements from the repository root and run:

.. code-block:: bash

   sphinx-build -b html docs docs/_build/html

The generated HTML files will be available under ``docs/_build/html``.
