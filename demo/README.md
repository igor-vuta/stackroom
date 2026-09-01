# The demo collection

**Everything in here is invented.** There is no Free City of Meridian Hollow,
no Bureau of Sunlight, no Carrow Ridge, no Halcyon Mirrorworks and nobody named
in any of these pages. It is a worked example, written so that a tool for
handling other people's disclosures can be demonstrated on a disclosure that
hurts nobody — and so that the screenshots in the top-level `README.md` can be
of a real build rather than of a mock-up.

## Building it

```
python demo/build-demo.py            # the PDFs, both sites, and a check
python demo/build-demo.py --pdfs     # just the documents
stackroom serve demo/site
```

`--stamp {margin,in-box,auto}` chooses where the exemption codes are printed.
Both placements are real, and Stackroom reads them by different rules — a code
inside its own rectangle is right beside it, while a margin stamp is reachable
only by being on the same *line* — so the flag is how you measure that rule
rather than a matter of taste. Build each way and count the boxes that come
back carrying a code:

```
python demo/build-demo.py --stamp margin     # every code out in the left margin
python demo/build-demo.py --stamp in-box     # every code reversed out of its box
python demo/build-demo.py --stamp auto       # in the box where it fits, the margin
                                             # where it does not: the default, and
                                             # what a real release is a mixture of
```

It takes about two minutes from cold and needs `reportlab` (a dev dependency),
poppler, Tesseract with **English and Russian** data — `tesseract-ocr-rus`, for
the one page that is in Cyrillic — and `pagefind` for the search index.

The documents are not committed. They are several megabytes of scanned annexe
and they regenerate byte for byte, so committing them would charge every clone
for them for ever. The two things that *are* committed are the two things a
person wrote: `about.md` and `stackroom.toml` in each folder, and the script.

The script ends by checking that the built site still contains every feature
the screenshots claim, and prints what it found:

```
  ok  a control-number gap                         [['BOS-000011', 'BOS-000013']]
  ok  a page in a non-Latin script                 en, ru
  ...
```

## What is in it, and why

`release-2024/` is the collection the screenshots are of: 21 pages in four
documents, answering the invented request BOS-2018-0117. It is built to be
awkward on purpose, because the awkward cases are the ones that break archives:

| | |
|---|---|
| **born-digital pages and scans** | 16 with a real text layer, 5 read by OCR |
| **redactions of every size** | from a six-character figure to a page blacked out end to end |
| **a page produced and withheld whole** | page 4 of the award memorandum, stamped (b)(5) reversed out of the black box |
| **both ways a reviewer stamps a code** | reversed out of the box, and out in the left margin level with the passage — `--stamp` builds it either way, and 41 of the 43 boxes carry a code under both |
| **three pages never produced at all** | a gap in the control numbers, BOS-000011 to BOS-000013 |
| **two statutes** | US FOIA (b) codes and the Privacy Act code (k)(2) |
| **a page nothing legible came back from** | annexe page 4, and the site says so |
| **a blank page** | annexe page 3, which is not the same thing |
| **a page in Cyrillic** | the letter from Zerkalsk, read from its text layer |
| **an honest false positive** | a dark band left by an open scanner lid, counted as a redaction because it cannot be told from one |

`release-2019/` is the same file one review decision earlier: one passage of the
correspondence still under a black box, one passage of the evaluation record not
yet under one, and no annexes at all. `build-demo.py` runs

```
stackroom compare demo/release-2019 demo/release-2024 -o demo/site
```

so the built site carries a `compare/` section with something true to say.

`ridgeway-2024/` is a second, four-page collection, and it exists for one
reason: Stackroom reads **one jurisdiction's exemption vocabulary per
collection**, chosen by `jurisdiction` in `stackroom.toml`, so showing a second
one means publishing a second archive. It is stamped with sections of the UK
Freedom of Information Act 2000 and builds to `demo/site-ridgeway`.

## The planted leak

Screenshot 4 in the top-level README is `stackroom check` refusing to publish a
failed redaction. That needs a document with a failed redaction in it, so the
script will write you one:

```
python demo/build-demo.py --leak /tmp/leaky-demo-copy --leak-codes b7c,k2
stackroom check /tmp/leaky-demo-copy
```

The copy has the words drawn *under* their black boxes instead of removed. It
is written outside the repository, it is never built into a site, and the
`.gitignore` here would stop it being committed anyway. The text it hides is as
invented as the rest of the collection — but the habit is the point, so treat it
as you would a real one and delete it when you are done.

## Editing it

The documents are written as data at the bottom of `build-demo.py`, in prose
with the redactions marked inline:

```
"The second-ranked bidder withdrew four days after the site visit, giving "
"as its reason [[b4|that the Bureau's own survey of the ridge understated "
"the number of units needing a new drive by at least six]], which the panel "
"did not test."
```

`[[b4|…]]` means *the agency took these words out and cited (b)(4)*: they are
never written into the PDF, and the black box is exactly as wide as they would
have been. `[[b4@2019|…]]` means only the 2019 release took them out, which is
what the comparison finds. Write the withheld text as if it were real — a
redaction is only interesting when it sits in the middle of a sentence somebody
wants to finish.

If you change the documents, retake the screenshots: `docs/images/` is full of
numbers that have to stay true of the collection they are pictures of.
