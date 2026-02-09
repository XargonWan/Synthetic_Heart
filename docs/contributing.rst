Contributing
============

.. image:: res/contributing.png
   :width: 600px
   :alt: Contributing Guide


We welcome pull requests for bug fixes and new features. Ensure your code passes ``python -m py_compile`` and ``python -m mypy . --strict`` (current strict scope is defined in ``mypy.ini`` for incremental coverage), and use the following commit message format:

- ``fix(element): description`` – bug fixes
- ``feat(element): description`` – new features
- ``minor(element): description`` – small improvements
- ``chore(element): description`` – small improvements
- ``doc(element): description`` – documentation changes
- ``patch(element): description`` – incremental updates

Example::

   fix(api): resolved an authentication issue

For larger changes, open an issue first to discuss your ideas. Documentation contributions are also appreciated. Update the ``docs/`` sources and verify that ``sphinx-build`` completes without errors.
