Plugin Guides
=============

Each plugin ships its own guide, which is the single source of truth:
the WebUI renders it at runtime in the plugin detail pane, and the docs
build collects the same files here automatically. Two layouts are
supported — folder-plugins use ``plugins/<name>/guide.md`` while
single-file plugins use ``plugins/<name>.guide.md`` (the guide sits next
to the module).

.. toctree::
   :maxdepth: 1
   :glob:

   generated/*
