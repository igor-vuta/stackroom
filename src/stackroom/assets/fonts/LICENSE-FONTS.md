# Fonts shipped with Stackroom

Every font in this directory is licensed under the **SIL Open Font License,
Version 1.1**. This file is the notice that licence requires to travel with
them, and it is a condition of redistribution - not a courtesy. If you fork,
mirror or re-host a Stackroom site, this file has to come along.

Stackroom self-hosts its fonts because an archive must not load anything from
a third party (`docs/ARCHITECTURE.md`, guarantee 5). Nothing here is fetched
from Google Fonts or any CDN at build time or at read time.

---

## A note on the names: these are NOT the original fonts

The files here are **subsets**. Glyphs outside the scripts this project needs
have been removed, the variable sans has had its weight axis trimmed, and
everything has been recompressed to WOFF2. Under the OFL's definitions that
makes each one a **Modified Version**:

> "Modified Version" refers to any derivative made by adding to, deleting, or
> substituting -- in whole or in part -- any of the components of the Original
> Version, by changing formats or by porting the Font Software to a new
> environment.

All three upstream families are released **with Reserved Font Names** -
`Source` for the two Adobe families, `Plex` for IBM's - and OFL clause 3 says:

> No Modified Version of the Font Software may use the Reserved Font Name(s)
> unless explicit written permission is granted by the corresponding Copyright
> Holder.

So the shipped faces are renamed. They are called **Stackroom Sans**,
**Stackroom Serif** and **Stackroom Mono** in `fonts.css` and in their own name
tables. This is a licence obligation, not a branding decision, and renaming
them back would breach the licence. The original copyright, licence, licence
URL and designer strings are preserved inside every file; only the
name-identifying fields were changed.

Do not describe these files as Source Sans, Source Serif or IBM Plex Mono. They
are derived from them, and the correct phrasing is exactly that.

---

## Attribution

### Stackroom Sans - from Source Sans 3

- **Copyright:** © 2023 Adobe (http://www.adobe.com/), with Reserved Font Name ‘Source’
- **Reserved Font Name:** `Source`
- **Designer:** Paul D. Hunt (Adobe)
- **Upstream version:** Version 3.052;hotconv 1.1.0;makeotfexe 2.6.0
- **Trademark notice:** Source is a trademark of Adobe in the United States and/or other countries.
- **Source:** <https://github.com/adobe-fonts/source-sans>, release `3.052R`
  (commit `ed1808970eb3c7301c9a523bee26473ba0bb62fa`). The variable font is not
  on the repository's `release` branch; it is published only as the release
  asset `VF-source-sans-3.052R.zip`, from which `VF/SourceSans3VF-Upright.otf`
  is taken.
- **Modifications:** weight axis limited to 400-600 with
  `fontTools.varLib.instancer`; subset to the Latin, Cyrillic and Greek ranges
  listed in `fonts.css`; converted to WOFF2; renamed per OFL clause 3.

### Stackroom Serif - from Source Serif 4

- **Copyright:** © 2014 - 2023 Adobe (http://www.adobe.com/), with Reserved Font Name ‘Source’.
- **Reserved Font Name:** `Source`
- **Designer:** Frank Grießhammer (Adobe)
- **Upstream version:** Version 4.005;hotconv 1.1.0;makeotfexe 2.6.0
- **Trademark notice:** Source is a trademark of Adobe in the United States and/or other countries.
- **Source:** <https://github.com/adobe-fonts/source-serif>, release `4.005R`
  (commit `2823e993c53fca27c5c8749f529b56a5a7c77b6b`), files
  `WOFF2/OTF/SourceSerif4-Regular.otf.woff2`,
  `WOFF2/OTF/SourceSerif4-Semibold.otf.woff2` and
  `WOFF2/OTF/SourceSerif4-It.otf.woff2`.
- **Modifications:** subset to the Latin, Cyrillic and Greek ranges listed in
  `fonts.css`; recompressed to WOFF2; renamed per OFL clause 3.

### Stackroom Mono - from IBM Plex Mono

- **Copyright:** Copyright 2017 IBM Corp. All rights reserved.
- **Reserved Font Name:** `Plex`
- **Designers:** Mike Abbink, Paul van der Laan, Pieter van Rosmalen (Bold Monday)
- **Upstream version:** Version 2.005 (npm package `@ibm/plex-mono` 2.5.0)
- **Trademark notice:** IBM Plex® is a trademark of IBM Corp, registered in many jurisdictions worldwide.
- **Source:** <https://github.com/IBM/plex>, tag `@ibm/plex-mono@2.5.0`
  (commit `2f9ba1b25957d958db71a849e85d72e3ecfb845a`), file
  `packages/plex-mono/fonts/complete/woff2/IBMPlexMono-Regular.woff2`.
- **Modifications:** subset to the Latin and Cyrillic ranges listed in
  `fonts.css`; recompressed to WOFF2; renamed per OFL clause 3.
- **Note:** IBM Plex Mono has no Greek. It carries a single codepoint in the
  Greek blocks (U+03C0, the mathematical pi), so `fonts.css` deliberately omits
  Greek from this family's `unicode-range` and lets Greek fall through to the
  reader's own monospace font.

The licence text below applies to all three, and is reproduced verbatim from
the `LICENSE.md` / `LICENSE.txt` shipped in each upstream repository at the
commits above. All three shipped identical licence bodies.

---

## Files in this directory

| File | Bytes | Derived from |
|---|---:|---|
| `stackroom-mono-400-core.woff2` | 11,928 | IBM Plex Mono |
| `stackroom-mono-400-cyrillic.woff2` | 9,828 | IBM Plex Mono |
| `stackroom-mono-400-ext-rare.woff2` | 2,896 | IBM Plex Mono |
| `stackroom-mono-400-ext.woff2` | 7,776 | IBM Plex Mono |
| `stackroom-sans-var-core.woff2` | 29,496 | Source Sans 3 |
| `stackroom-sans-var-cyrillic.woff2` | 17,280 | Source Sans 3 |
| `stackroom-sans-var-ext-rare.woff2` | 20,804 | Source Sans 3 |
| `stackroom-sans-var-ext.woff2` | 31,016 | Source Sans 3 |
| `stackroom-sans-var-greek.woff2` | 16,988 | Source Sans 3 |
| `stackroom-serif-400-core.woff2` | 30,156 | Source Serif 4 |
| `stackroom-serif-400-cyrillic.woff2` | 16,460 | Source Serif 4 |
| `stackroom-serif-400-ext-rare.woff2` | 5,396 | Source Serif 4 |
| `stackroom-serif-400-ext.woff2` | 15,716 | Source Serif 4 |
| `stackroom-serif-400-greek.woff2` | 7,572 | Source Serif 4 |
| `stackroom-serif-400i-core.woff2` | 28,768 | Source Serif 4 |
| `stackroom-serif-400i-cyrillic.woff2` | 15,868 | Source Serif 4 |
| `stackroom-serif-400i-ext-rare.woff2` | 5,732 | Source Serif 4 |
| `stackroom-serif-400i-ext.woff2` | 14,800 | Source Serif 4 |
| `stackroom-serif-400i-greek.woff2` | 7,684 | Source Serif 4 |
| `stackroom-serif-600-core.woff2` | 32,108 | Source Serif 4 |
| `stackroom-serif-600-cyrillic.woff2` | 17,300 | Source Serif 4 |
| `stackroom-serif-600-ext-rare.woff2` | 5,568 | Source Serif 4 |
| `stackroom-serif-600-ext.woff2` | 16,276 | Source Serif 4 |
| `stackroom-serif-600-greek.woff2` | 8,060 | Source Serif 4 |

`fonts.css` and this file are Stackroom's own and are covered by the project's
MIT licence; the `.woff2` files are covered by the OFL below. Regenerate the
fonts with `tools/build-fonts.sh`.

---

## Copyright notices, as required by OFL clause 1

```
Copyright 2014 - 2023 Adobe (http://www.adobe.com/), with Reserved Font Name 'Source'.
All Rights Reserved. Source is a trademark of Adobe in the United States and/or
other countries.

Copyright 2010-2022 Adobe (http://www.adobe.com/), with Reserved Font Name 'Source'.
All Rights Reserved. Source is a trademark of Adobe in the United States and/or
other countries.

Copyright (c) 2017 IBM Corp. with Reserved Font Name "Plex"
```

---

-----------------------------------------------------------
SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007
-----------------------------------------------------------

PREAMBLE
The goals of the Open Font License (OFL) are to stimulate worldwide
development of collaborative font projects, to support the font creation
efforts of academic and linguistic communities, and to provide a free and
open framework in which fonts may be shared and improved in partnership
with others.

The OFL allows the licensed fonts to be used, studied, modified and
redistributed freely as long as they are not sold by themselves. The
fonts, including any derivative works, can be bundled, embedded, 
redistributed and/or sold with any software provided that any reserved
names are not used by derivative works. The fonts and derivatives,
however, cannot be released under any other type of license. The
requirement for fonts to remain under this license does not apply
to any document created using the fonts or their derivatives.

DEFINITIONS
"Font Software" refers to the set of files released by the Copyright
Holder(s) under this license and clearly marked as such. This may
include source files, build scripts and documentation.

"Reserved Font Name" refers to any names specified as such after the
copyright statement(s).

"Original Version" refers to the collection of Font Software components as
distributed by the Copyright Holder(s).

"Modified Version" refers to any derivative made by adding to, deleting,
or substituting -- in part or in whole -- any of the components of the
Original Version, by changing formats or by porting the Font Software to a
new environment.

"Author" refers to any designer, engineer, programmer, technical
writer or other person who contributed to the Font Software.

PERMISSION & CONDITIONS
Permission is hereby granted, free of charge, to any person obtaining
a copy of the Font Software, to use, study, copy, merge, embed, modify,
redistribute, and sell modified and unmodified copies of the Font
Software, subject to the following conditions:

1) Neither the Font Software nor any of its individual components,
in Original or Modified Versions, may be sold by itself.

2) Original or Modified Versions of the Font Software may be bundled,
redistributed and/or sold with any software, provided that each copy
contains the above copyright notice and this license. These can be
included either as stand-alone text files, human-readable headers or
in the appropriate machine-readable metadata fields within text or
binary files as long as those fields can be easily viewed by the user.

3) No Modified Version of the Font Software may use the Reserved Font
Name(s) unless explicit written permission is granted by the corresponding
Copyright Holder. This restriction only applies to the primary font name as
presented to the users.

4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font
Software shall not be used to promote, endorse or advertise any
Modified Version, except to acknowledge the contribution(s) of the
Copyright Holder(s) and the Author(s) or with their explicit written
permission.

5) The Font Software, modified or unmodified, in part or in whole,
must be distributed entirely under this license, and must not be
distributed under any other license. The requirement for fonts to
remain under this license does not apply to any document created
using the Font Software.

TERMINATION
This license becomes null and void if any of the above conditions are
not met.

DISCLAIMER
THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT
OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE
COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL
DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM
OTHER DEALINGS IN THE FONT SOFTWARE.
