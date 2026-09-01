# Translating Stackroom

Stackroom's interface is translated at **build time**. You put

```toml
language = "ru"
```

in `stackroom.toml`, and the site that comes out has a Russian masthead, a
Russian redaction ledger, Russian plurals, Russian thousands separators and
`lang="ru"` on every page. No script runs in the reader's browser to do it, and
there is no language switcher: one build, one language, one folder of files.

This document is for the person adding a language. It should take an afternoon.

---

## The short version

```bash
python -m stackroom.i18n list            # what already ships, and how many messages
python -m stackroom.i18n new de          # writes src/stackroom/locales/de.json
$EDITOR src/stackroom/locales/de.json    # translate every message in it
python -m stackroom.i18n check de        # until it prints nothing but the count
```

Four catalogues ship — English, Polish, Russian and Ukrainian — and `list`
prints the message count as well as the languages, which is the number to trust:
the catalogue grows whenever a page gains a sentence, and a figure written into
this file would be wrong by the time you read it.

Then build a real archive with `language = "pl"` and read it. The checker can
tell you that a sentence is complete; only you can tell that it is *good*.

---

## What a catalogue looks like

`src/stackroom/locales/<code>.json`, shipped inside the package:

```json
{
  "locale": "ru",
  "name": "Русский",
  "english_name": "Russian",
  "direction": "ltr",
  "plural": "ru",
  "number": { "group": " ", "decimal": ",", "minimum_grouping_digits": 1,
              "percent": "{n} %" },
  "date": { "short": "{d}.{m}.{y}" },
  "messages": {
    "nav.about": "Об архиве",
    "page.redactions": {
      "one":  "{count} изъятие",
      "few":  "{count} изъятия",
      "many": "{count} изъятий",
      "other": "{count} изъятия"
    }
  }
}
```

| Field | What it decides |
|---|---|
| `name` | The language's name **in itself** — Русский, Українська, العربية. |
| `direction` | `ltr` or `rtl`. Goes straight onto `<html dir>`. |
| `plural` | Which plural rule to use. Usually your own code. A language may borrow another's — Belarusian says `"plural": "ru"`. |
| `number.group` | Thousands separator. A **non-breaking** space (` `) if your language uses a space, or `16 000` breaks across a line as `16` and `000`. |
| `number.minimum_grouping_digits` | How many digits must come before the first separator. `1` groups from four digits (`1,000`); `2` groups only from five, which is what Spanish wants (`1000`, but `10.000`). |
| `number.percent` | Where the sign goes and whether a space precedes it. English `{n}%`; Russian, Ukrainian, French and Spanish all `{n} %`. |
| `date.short` | `{y}`, `{m}`, `{d}` are the four- and two-digit parts; `{d0}` and `{m0}` drop a leading zero; `{month}` uses `month.1`…`month.12` if you add them. |

**English is the source.** `en.json` also carries a `notes` block: a sentence or
two per message saying what it means, what its placeholders are, and where a
term has a specific legal or archival sense. You do not copy the notes into your
file — read them where they are:

```bash
python -m stackroom.i18n show withheld.heading
```

```
withheld.heading
  What was withheld
  placeholders: none
  note: The page that counts and explains what an agency removed. See the
        note on nav.withheld for the term of art.
  pl: Co wyłączono
  ru: Что было изъято
  uk: Що було вилучено
```

---

## The five rules

### 1. A message is a whole sentence

Every message is one complete, translatable unit with **named** placeholders.
Nothing is assembled by gluing fragments together, because a sentence built out
of pieces can only ever come out in English word order.

The colophon is the example. It used to be:

```jinja
{{ stats.documents }} document{{ '' if stats.documents == 1 else 's' }},
{{ '{:,}'.format(stats.pages) }} page{{ ... }}. Built {{ date }} with Stackroom {{ v }}.
```

Two counts, two `+ 's'` suffixes and a hard-coded US number format. It is now
three messages:

```json
"count.documents": { "one": "1 document", "other": "{count} documents" },
"count.pages":     { "one": "1 page",     "other": "{count} pages" },
"footer.built": "{documents}, {pages}. Built {date} with Stackroom {version}."
```

`{documents}` and `{pages}` arrive already pluralised and already written with
your separators, and the frame sentence is yours to reorder however you like.

Where a count phrase would have to be inflected differently inside the frame —
which it does in Russian, where a numeral governs the case of what follows it —
the whole sentence gets plural forms instead. `withheld.nothing` is one of
those.

### 2. Plural forms are CLDR categories, and your language decides which

The categories are `zero`, `one`, `two`, `few`, `many`, `other`. `other` is
required in every language. Which of the rest you must supply is decided by
your `plural` rule, and `check` will tell you:

| Rule | Forms you must write |
|---|---|
| `en`, `de`, `nl`, `it`, `es`, `fr`, `pt` | `one`, `other` |
| `ru`, `uk`, `pl` | `one`, `few`, `many`, `other` |
| `he` | `one`, `two`, `other` |
| `ar` | all six |
| `zh`, `ja`, `ko`, `vi`, `th` | `other` only |

### 3. If your `one` form covers more than the number one, it has to print the number

This is the mistake everybody makes, and it is invisible until somebody counts.

English's `one` category matches exactly the number 1, so `"1 page"` is a
perfectly good English `one` form. Russian's `one` category matches **1, 21, 31,
101, 1001**. A Russian catalogue that copies the English shape publishes

> **1 страница**

on a document that has twenty-one of them.

```
$ python -m stackroom.i18n check ru
ru: 412/412 messages (100%)
  hardcoded number   count.pages: form(s) one print no {count}, but this
                     language uses that form for more than one number
```

That check found twenty-four of these in the Russian catalogue in this
repository while it was being written — the transcript above is from then, which
is why its message count is not today's. It is not a hypothetical.

The same trap catches French, whose `one` covers 0 as well as 1. Arabic's
`zero`, `one` and `two` each match exactly one value, so those three may write
the number out as a word.

And the other half of the same rule: the `other` form in Russian and Ukrainian
is **not** the "5 and up" form — that is `many`. `other` is the form for
fractions (`1,5 страницы`), which this site never produces. Put the genitive
singular there and move on.

### 4. Markup lives only in keys that end in `_html`

```json
"doc.numbered_html": "Numbered <span class=\"mono\">{prefix}…</span>"
```

A message whose key ends in `_html` may contain markup, and its parameters are
escaped for you. Every other message is plain text and is escaped on the way
out, so a `<span>` in one of those is published to the reader as visible angle
brackets. `check` refuses both mistakes.

Keep every tag your English source has — same tags, same order. You may move
them, and you often should.

### 5. Placeholders are a contract

Your translation carries exactly the placeholders its English source carries.
`{cout}` is not a typo the build catches; it is the literal text `{cout}` on a
published page, in a language the person who built the site probably cannot
read. `check` compares them for you.

---

## The interface language is not the index language

`language` is the language of the **interface**. It decides the masthead, the
notices, the ledger, the plural forms, the separator in `16 000`, and
`<html lang>`. It says nothing about what the documents are written in.

The search index is a different question. Pagefind is given one language with
`--force-language`, and that choice picks the **stemmer and the stop-word
list** it indexes with. A Russian-language archive of English documents wants a
Russian interface and an *English* stemmer: index English prose with the
Russian one and "filed" and "filing" stop being the same word, while the reader
downloads the wrong stemmer's WebAssembly to search with.

So the two settings are separate:

```toml
language = "ru"          # the interface

[search]
# language = "en"        # the stemmer. Unset: the language the documents were read as.
```

Left unset — which is the normal case — the index language is **the language
most of the pages were actually read as**, from `Page.language`, which the
ingest detects per page and `CollectionStats.languages` sorts by frequency. A
collection with no readable text at all falls back to `language`, and then to
English. `SiteBuilder.index_language()` is the whole rule, in four lines.

Set `search.language` when detection is wrong or when you know better — a
production that is nine-tenths boilerplate in one language and evidence in
another, say. It is passed through `i18n.normalize_locale` rather than
`lang.normalize_language_codes`, because pagefind knows more languages than
this project keeps stop-word lists for and an operator who names one should be
believed.

One thing this does *not* break: Pagefind's client picks its index from the
page's `<html lang>`, which is now the interface language and will not match an
index built for the documents. It falls back to the largest index present, and
`--force-language` guarantees there is exactly one. Verified on a `ru` build of
the demo: `<html lang="ru">`, `pagefind-entry.json` reporting a single `en`
index of 14 pages, `wasm.en.pagefind` on the wire, and an English query
returning all 14.

---

## Terms of art

Stackroom is a freedom-of-information tool, and several of its words are terms
in that practice rather than ordinary vocabulary. Where your language's
transparency community has settled on a word, **use theirs, not a literal
translation of the English**. The notes in `en.json` flag every one of these;
the load-bearing ones are:

| English | What it means | Shipped choices |
|---|---|---|
| **withheld** | Material an agency removed before releasing the document | pl `wyłączenia` · ru `изъятия` · uk `вилучення` |
| **redaction** | One blacked-out area, counted as an object | pl `zaczernienie` · ru `изъятие` · uk `вилучення` |
| **exemption** | The statutory ground cited for withholding | pl `podstawa` · ru `основание` · uk `підстава` |
| **control number** | The stamp on each page of a production (a Bates number) | pl `numer kontrolny` · ru `контрольный номер` · uk `контрольний номер` |
| **the negative** | The page that draws only what was removed — a photographic negative | pl `Negatyw` · ru/uk `Негатив` |
| **release** / **production** | The batch of documents an agency handed over | pl `udostępnienie` · ru `выдача` · uk `добірка` |

Polish is worth reading beside the other two: it chose `zaczernienie`
("blackening") for a single redaction where Russian and Ukrainian reused the
word for withholding, because Polish practice names the mark rather than the
act. That is the kind of decision this table exists to record.

If the metaphor in "the negative" does not carry into your language, choose one
that does. Translating the word rather than the idea is the wrong answer.

**Exemption glosses are not in the catalogue and should not be.** The
plain-language explanations of `(b)(5)`, `s.31`, `art. 4(1)(a)` and so on live
in `src/stackroom/ingest/exemptions.py`, one vocabulary per jurisdiction, and
they describe a specific foreign statute. They stay in English deliberately: a
Russian-language archive of US FOIA documents is usually better served by the
English gloss beside the English code than by a translation that quietly
becomes legal advice about a statute the translator has not read. If your
project wants them translated, that is an editorial decision for that archive,
and the right shape for it is a per-jurisdiction gloss catalogue rather than a
line in this file.

---

## The checklist

- [ ] `python -m stackroom.i18n new <code>` and fill in `name`, `english_name`,
      `direction`, `plural`.
- [ ] Set `number.group`, `number.decimal`, `minimum_grouping_digits` and
      `percent`. Check a four-digit number and a five-digit one.
- [ ] Set `date.short`.
- [ ] Translate every message. `check --missing` prints the English and the
      note for each one still to do, and `check --untranslated` lists the ones
      you have left byte-identical to the English — which is sometimes right
      and is always worth reading once.
- [ ] Every plural message has every form your rule requires.
- [ ] Every form your language reuses across several numbers prints `{count}`.
- [ ] Every `_html` message keeps its tags; no other message has a `<` in it.
- [ ] Every message carries its source's placeholders.
- [ ] `python -m stackroom.i18n check <code>` exits 0.
- [ ] `python -m pytest tests/test_i18n.py` passes — the shipped catalogues are
      tested as data, so yours is covered the moment it exists.
- [ ] Build a real archive with `language = "<code>"` and **read it**. Look
      especially at: the front page's strip caption, one page with redactions,
      the withheld ledger, the negative, and the colophon at the foot of every
      page.
- [ ] Say in your pull request where you are unsure. A reviewer who can see
      your uncertainty will fix it; one who cannot will not know to look.

---

## Where you are still going to see English

Being honest about this is the point of the section. Verified against the tree
on 2026-09-01, by reading `templates/`, `build/site.py` and `ingest/` rather
than by trusting the previous version of this list — two of its five entries
had been fixed and nobody had moved them.

- **Statutory exemption glosses.** Deliberate; see above. `exemptions.legend()`
  takes no translator and returns the vocabulary's English text, and every
  caller in `build/site.py` passes it straight through.
- **One `aria-label` inside the transcription.** The bar that stands in for a
  redacted passage carries `aria-label="withheld"` — and, where the box has a
  code, the English gloss for it as well, from the same vocabulary as the
  bullet above. That element is inside the block the search index reads, which
  is frozen byte-for-byte because the index's word positions are the boxes
  drawn on the scan. It is the only piece of interface text in there, and
  moving it is not a translation change.
- **The command line.** `stackroom build` talks to the operator, not to a
  reader. Its messages are a separate job with a different audience, and there
  is no plan to change that. The per-page notes the ingest prints are part of
  that — including the sentences in `OcrQuality.reasons`. What the *reader*
  sees about a doubtful page is not one of those strings: `_quality_note()`
  turns the verdict into `quality.suspect_body` and friends, which are in every
  catalogue.

### What used to be on this list and no longer is

Left here because a reader who saw an older copy of this file should be able to
tell what moved.

- **`build/negative.py`.** The three arrangement names, their captions and the
  "what this picture cannot show" entries were composed in Python and untouched
  by the catalogue. They are wired now — thirty-six `t("negative.…")` calls in
  that module, and seventy `negative.*` keys.
- **Everything JavaScript writes.** The search status line, the citation panel,
  the offline controls, the command palette and the full-size viewer were an
  English island of roughly 120 strings. They are translated at build time like
  everything else: the build writes `assets/i18n.js` from the catalogue's `js.*`
  keys, `prefs.js` reads it once in the head and republishes `t()`, `n()` and
  `pct()` on `window.stackroomReader`, and every deferred script talks to that.
  Nothing is translated in the browser and nothing is fetched at load. A handful
  of keys the scripts need are shared with the templates rather than duplicated;
  `i18n.JS_SHARED` is that list and it is short on purpose.
- **`compare/`.** The two comparison templates had no catalogue entries and were
  about 1,200 words of English. They now carry a `t()` call for every string, and
  `compare.*` is the largest single group in `en.json`.
- **The masthead's link to `compare/`.** It was published as the English word
  `Compared`, marked `lang="en"`, for as long as it took the one line of
  `templates/base.html.jinja` to catch up with the pages behind it. It is
  `{{ t('nav.compare') }}` now, and `nav.compare` is filled in in all four
  catalogues.
- **The second digest on the about page.** With `safety.strip_metadata` on, the
  paragraph explaining why a document has both a `sha256` and a
  `published_sha256` was English. It is two keys now — `about.two_digests`,
  which agrees with a count and so carries plural forms, and
  `about.two_digests_which_html`, which is markup around three literal names no
  language translates. Split there on purpose: it keeps the plural forms off
  the half with tags in it. The two field names themselves stay English inside
  `<small lang="en" dir="ltr">`, because they are what the reader will find
  beside those numbers in `manifest.json`.

## Right-to-left

`direction: "rtl"` in the catalogue puts `dir="rtl"` on `<html>`, and the
runtime already does the rest of the *markup* correctly:

- The document's own language and direction are marked on the transcription
  block, not on the page, so an Arabic-interface archive of English documents
  gets `<html lang="ar" dir="rtl">` with `<div class="page-text" lang="en"
  dir="ltr">` inside it — both declared, in the right places.
- The pager's arrows are inside the messages (`"← Page {number}"`), so a
  right-to-left catalogue writes them pointing the other way. Same for the two
  "→" links on the front page and the withheld ledger.

The **stylesheet** is not finished for RTL, and shipping an Arabic catalogue
without doing this work would produce a page that reads right-to-left with its
furniture on the wrong side. The stylesheet is still overwhelmingly physical —
the grep below counts 38 direction-sensitive physical declarations across
`assets/stackroom.css` and `assets/parts/*.css`, against seven logical ones.
Run it again before you start, because the number grows:

```sh
grep -rnoE 'margin-(left|right)|padding-(left|right)|border-(left|right)|text-align: *(left|right)' \
  src/stackroom/assets/stackroom.css src/stackroom/assets/parts/*.css | wc -l
```

What needs changing:

| Where | Now | Should be |
|---|---|---|
| `.masthead__nav` | `margin-left: auto` | `margin-inline-start: auto` — otherwise the whole navigation sticks to the wrong edge |
| `.notice`, `.notice--warn`, `.resume`, `.pal__row` | `border-left` / `border-left-color` | `border-inline-start` — the accent bar is on the wrong side of every notice |
| `.prose blockquote` | `padding-left` + `border-left` | `padding-inline-start` + `border-inline-start` |
| `.text-layer .ln` | `padding-left: 1.25em; text-indent: -1.25em` | logical equivalents — this one is needed for an Arabic *document* even under an English interface |
| `th, td` | `text-align: left` | `text-align: start` |
| `td.n, th.n`, `.negative__figure`, `.shortcuts dt` | `text-align: right; padding-right: 0` | `text-align: end; padding-inline-end: 0` |
| `.pref` (`parts/prefs.css`) | `right: 0` | `inset-inline-end: 0` — the preferences panel opens off the wrong edge |
| `.lens__close` | `margin-left: auto` | `margin-inline-start: auto` |
| `parts/scan.css` page-turn keyframes | `translateX(±6%)` | direction-aware, or the page turn slides the wrong way |
| The strip of ticks (`ribbon()`) | page 1 is drawn at x=0 | Under RTL a timeline should run right to left. `[dir="rtl"] .ribbon { transform: scaleX(-1) }` does it, but `scan.js` maps a pointer's x-position back to a page number and would need the same flip. |
| `parts/compare.css` | eleven physical declarations | the comparison section arrived after this list was first written and has had no RTL pass at all |

**Three things must stay physical, and converting them would be a serious bug:**

1. The redaction boxes drawn over the scan. `page.html.jinja` writes
   `style="left:…%;top:…%"` from coordinates measured in the *image*, and the
   image is not mirrored by `dir`. Under `inset-inline-start` every redaction
   box in every RTL archive would land mirrored on the page — pointing at the
   wrong words, in an archive whose whole purpose is to point at the right
   ones.
2. `.lens__canvas`, `.scrub` and `.passage` in `parts/scan.css`, for the same
   reason: `top: 0; left: 0; transform-origin: 0 0` is image geometry.
3. The inline `width:` on a withheld bar in the transcription is a share of the
   page and is direction-neutral already.

The wrapper — `.wrap { margin-inline: auto; padding-inline: var(--gutter) }` —
is already logical, which is the single most important one and the reason the
page does not simply fall apart.
