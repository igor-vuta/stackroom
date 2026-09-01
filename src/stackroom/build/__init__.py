"""Turning a :class:`~stackroom.model.Collection` into a directory of files.

Build modules render; they never parse a PDF. Everything they need has already
been learned by ``ingest`` and written into the dataclasses in ``model.py``.

The output layout is fixed by ``docs/ARCHITECTURE.md`` and the guarantees there
are not negotiable: one real HTML page per document page, the original file
downloadable from every page it produced, word order in the HTML identical to
``Page.words``, and nothing loaded from a third-party host.

This file deliberately imports nothing. Importing ``stackroom.build`` should
cost nothing and drag in no optional dependency, so a caller that only wants
``build.search`` does not pay for Jinja.
"""
