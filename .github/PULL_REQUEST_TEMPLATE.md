## What this changes

<!-- One or two sentences. What it does, and what made it worth doing. -->

## Why

<!-- The document, the release, or the case that prompted it. If there is an
     issue, link it: "Fixes #123". If a real (public) document is what exposed
     the problem, a link to it is worth more than a paragraph. -->

## How it was tested

<!-- Which tests, and what you ran locally:

       pytest
       ruff check .

     If you changed a threshold in ingest/, say which page you measured it on.
     Every number in there is a measurement, and one changed without evidence
     is a regression waiting to be found by somebody's archive. -->

## Checks

- [ ] `pytest` passes.
- [ ] `ruff check .` is clean.
- [ ] There are tests for the behaviour I changed, and at least as many for
      what must *not* happen as for what must.
- [ ] `CHANGELOG.md` has a line under `Unreleased`.
- [ ] No document that is not already public appears in this branch — not as a
      fixture, not in a test, not in a screenshot, not in the commit history.

## Guarantees

`docs/ARCHITECTURE.md` lists six guarantees the output must keep. Tick any this
change touches, and say in a sentence how it still holds:

- [ ] 1. Every page is a real HTML file containing that page's text.
- [ ] 2. The original file is downloadable from every page that came from it.
- [ ] 3. `Page.words` order is identical to token order in the page HTML.
- [ ] 4. Text a redaction failed to remove is never published.
- [ ] 5. Nothing loads from a third-party host.
- [ ] 6. The build is deterministic: same input bytes, same output bytes.
- [ ] None of them.

<!-- Guarantee 3 in particular: the search index returns match positions as
     indices into Page.words, and the viewer draws boxes from them. If you
     changed one side, this PR should change the other. -->

## Anything else

<!-- Trade-offs you made, things you were unsure about, a question for the
     reviewer. A draft PR with an open question in it is a perfectly good way
     to ask. -->
