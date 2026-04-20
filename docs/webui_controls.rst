WebUI configuration control types
=================================

The server's exposed variables declare a ``ui_type`` that the WebUI renders.
Supported types include:

- ``string`` — single-line text input
- ``password`` — masked input
- ``number`` — numeric input
- ``bool`` / ``boolean`` — toggle switch
- ``select`` — dropdown
- ``combobox`` — free-text input with suggestion list (``datalist``)
- ``textarea`` / ``json`` — multi-line editor (JSON preserved when ``value_type=='json'``)
- ``tags`` — tag-list editor (stored as JSON array)
- ``tag-combobox`` — tag-list with suggestion support
- ``file`` — file upload control

Notes
-----
The WebUI uses ``ui_type`` together with ``value_type`` and ``options`` from the
API to decide which control to render and how values are serialized.

See also
--------
- :pyfile:`core/variables_engine.py` (supported `ui_type` values)
- :pyfile:`core/webui.py` (exposed-variable serialization)
