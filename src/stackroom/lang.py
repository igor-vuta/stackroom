"""Language resources: stopwords, scripts, and the garbage-token rules.

This module answers one question for the rest of the pipeline: *does this look
like language?* It has no dependencies beyond the standard library, downloads
nothing, and holds its word lists as plain literals so a reader can audit them.

Why stopwords
-------------
The load-bearing signal for OCR quality is the **stopword ratio**, not mean
confidence. Real prose in any language is roughly a third function words - the,
of, and, в, не, der, de, di - and OCR garbage is not, because a mangled glyph
sequence almost never lands on one of the two hundred shortest words in the
language. Measured on this project's fixtures: a clean typed page scores 0.38,
the same page rotated 90 degrees scores 0.00 (its OCR reads ``pue Jo ay} Se
YONS``), and a heavily blurred page scores 0.00. Mean confidence for those same
three pages was 96, 49 and 30 - informative, but only after the damage is bad
enough to stop Tesseract from emitting words at all, and by then confidence has
nothing to average over.

Why the lists are short
-----------------------
60-120 words per language is enough. The ratio is a statistic, not a lookup: if
a page is really French, the top hundred French function words will carry a
quarter of its tokens. Longer lists add content words, which start matching
garbage and blunt the signal.

Script awareness
----------------
The vowel/consonant heuristic that catches Latin garbage is a catastrophe
applied to Chinese: every Han token has zero vowels, so an unguarded rule
declares an entire Chinese corpus to be garbage. Every rule in
:func:`is_garbage_token` therefore names the scripts it applies to.

The same discipline applies to the word lists. Eleven languages is not every
language, and a page in Arabic, Hebrew, Devanagari, Thai, Japanese or Korean
scores zero here for a reason that has nothing to do with its quality.
:func:`stopwords_apply` is how a caller tells that zero apart from the zero
that means garbage; getting it wrong is an archive that declares a whole
alphabet unreadable.

Nobody has to tell this module what language a page is in
-------------------------------------------------------
It works that out, and that is the point: an operator declares
``ocr.languages`` so *Tesseract* knows what shapes to expect on a scan, and
that list is a filter with a real cost - every extra alphabet is more ways to
misread the same ink. Nothing here may treat it as a claim about the
documents. :func:`stopword_ratio` takes the maximum over every list it has
whatever it is told, so a declared list can only ever raise a page's score.
"""

# ruff: noqa: RUF001
# This file is a dictionary in nine alphabets. Ruff's ambiguous-character rule
# exists to catch a Cyrillic lookalike smuggled into an otherwise Latin string;
# here every Cyrillic, Greek and CJK character below is deliberate and
# load-bearing, and flagging each of the several hundred of them individually
# would bury the rule's real findings everywhere else in the project.

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from itertools import pairwise

__all__ = [
    "SCRIPTS_WITH_STOPWORDS",
    "STOPWORDS",
    "detect_language",
    "is_garbage_token",
    "language_names",
    "normalize_language_codes",
    "normalize_token",
    "script_of",
    "stopword_ratio",
    "stopwords_apply",
]


# --------------------------------------------------------------------------
# stopwords
# --------------------------------------------------------------------------

# Each list is the most frequent function words of the language: articles,
# prepositions, conjunctions, pronouns, auxiliaries and a handful of very
# common verbs. Sources are the public-domain frequency lists that ship with
# NLTK, snowball and stopwords-iso, hand-trimmed to the closed-class words that
# survive OCR damage. They are literals on purpose - a contributor can read the
# whole language resource of this project without leaving the file.


def _words(block: str) -> frozenset[str]:
    """Whitespace-separated word list, written as prose so it can be read."""
    return frozenset(block.split())


STOPWORDS: dict[str, frozenset[str]] = {
    "en": _words(
        """
        the of and to a in that is was it for on with as be at by this had not
        are but from or have an they which one you were her all she there would
        their we him been has when who will more no if out so said what up its
        about into than them can only other some could time these two may then
        do first any my now such like our over me even most made after also did
        many before must through back where much your way well down should
        because each just those how too little very make still own see between
        both being under never while last might us against since here off
        """
    ),
    "ru": _words(
        """
        и в не на я быть он с что а по это она этот к но они мы как из у
        который то за свой весь год от так для о же вы все тот мочь человек
        такой его сказать только или ещё еще бы себя один уже до время если сам
        когда другой вот наш мой знать стать при чтобы дело жизнь кто первый
        очень два день её ее там под можно после их где тут во них надо всё
        между чем потом нет теперь ни да ты был была было были есть нас ним ей
        ему меня тебя ли бы над без через при том тем эти
        """
    ),
    "uk": _words(
        """
        і й в у не на я бути він з із що а по це вона цей до але вони ми як
        який то за свій весь рік від так для про же ви все той могти людина
        такий його сказати тільки або ще б себе один вже час якщо сам коли
        інший ось наш мій знати стати при щоб справа життя хто перший дуже два
        день її там під можна після їх де тут них треба між ніж потім немає
        тепер ні ти був була було були є нас їй йому мене тебе ж чи куди тому
        ще над без через цього цим
        """
    ),
    "de": _words(
        """
        der die und in den von zu das mit sich des auf für ist im dem nicht ein
        eine als auch es an werden aus er hat dass sie nach wird bei einer um am
        sind noch wie einem über einen so zum war haben nur oder aber vor zur
        bis mehr durch man sein wurde sei ich doch ihre unter kann gegen wir
        wenn was seine ihr dann diese dieser alle wieder meine zeit gibt schon
        wo sehr ihm immer viel ohne keine denn ja damit weil ihn muss beim
        """
    ),
    "fr": _words(
        """
        le de un à être et en avoir que pour dans ce il qui ne sur se pas plus
        pouvoir par je avec tout faire son mettre autre on mais nous comme aussi
        leur y dire elle devoir avant deux même prendre la les des au aux une ou
        où si quand très bien sans sous entre encore dont cette ces cet ses mon
        ma mes notre votre vous ils elles lui moi était été sont ont fait peut
        doit ainsi alors après depuis donc chez contre vers toujours jamais non
        du d l qu n s
        """
    ),
    "es": _words(
        """
        de la que el en y a los se del las un por con no una su para es al lo
        como más o pero sus le ha me si sin sobre este ya entre cuando todo esta
        ser son dos también fue había era muy años hasta desde está mi porque
        qué sólo solo han yo hay vez puede todos así nos ni parte tiene él uno
        donde bien tiempo mismo ese ahora cada e otro después te otros aunque
        esa eso hace otra tan durante siempre día tanto ella tres sí dijo sido
        """
    ),
    "pt": _words(
        """
        de a o que e do da em um para é com não uma os no se na por mais as dos
        como mas foi ao ele das tem à seu sua ou ser quando muito há nos já está
        eu também só pelo pela até isso ela entre era depois sem mesmo aos ter
        seus quem nas me esse eles estão você tinha foram essa num nem suas meu
        às minha têm numa pelos elas havia seja qual será nós tenho lhe deles
        este dele tu te vocês lhes
        """
    ),
    "it": _words(
        """
        di a da in con su per tra fra il lo la i gli le un uno una e che non è
        si sono come ma anche più ci al del della dei delle nel nella alla dal o
        ha ho hanno era essere questo questa quando se tutto tutti molto dopo
        senza ancora così dove fa già loro lui lei noi voi mi ti vi ne sua suo
        mio mia nostro vostro perché quale quali cui ogni alcuni prima poi
        sempre mai oggi ora anni stato stata fatto può deve cosa due tre
        """
    ),
    "pl": _words(
        """
        w i z na do nie się to że o a jest dla po ale jak od przez przy za tym
        tego jego jej ich być ma są był była było były ten ta te tej który która
        które gdy gdzie kiedy tak już tylko bardzo może można jeszcze także oraz
        lub czy co kto ja ty on ona my wy oni sobie siebie nam wam im mnie
        ciebie jako aby żeby więc bez pod nad mimo między będzie ze we sam sama
        wszystko wszystkie jeden jedna dwa roku lat nic nikt
        """
    ),
    "nl": _words(
        """
        de het een en van in is dat op te zijn met voor niet aan er die ook als
        dan maar om door over ze hij uit bij nog kan was wordt worden naar heeft
        hebben of wij we je jij u ik mij me hem haar hun zich deze dit dus waar
        wat wie hoe toen tot tegen onder tussen zonder na al geen wel veel meer
        meest andere zelf alle twee jaar tijd moet mag zou zal heb had werd
        werden iets niets altijd nooit omdat terwijl hier daar nu ons onze
        """
    ),
    # Chinese is a bonus beyond the ten languages the spec requires, and it is
    # *character*-level on purpose: ARCHITECTURE.md requires CJK to be emitted
    # one character per token so Pagefind's word indices stay aligned, so these
    # are the function characters, not words. Without this list a page of real
    # Chinese scores a stopword ratio of zero and looks exactly like garbage.
    "zh": _words(
        """
        的 了 是 在 和 有 我 不 人 一 他 这 中 大 来 上 国 个 到 说 们 为 子 与
        地 也 你 时 要 就 出 会 可 以 对 生 能 而 那 都 得 下 之 年 过 于 及 其
        或 但 因 所 从 被 把 向 又 并 使 让 已 还 很 更 最 只 等 什 么 没 后
        """
    ),
}

SCRIPTS_WITH_STOPWORDS: frozenset[str] = frozenset({"latin", "cyrillic", "han"})
"""The names :func:`script_of` returns for which :data:`STOPWORDS` has a list.

A zero stopword ratio only means "not language" when we *have* a list for the
script in front of us. For Greek, Arabic and everything this module cannot name
we do not, so callers must treat a zero there as "no opinion" rather than as
evidence of garbage - the alternative is an archive that declares every Arabic
page unreadable.

This set is the vocabulary answer, and it is too coarse to be the *test*: ask
:func:`stopwords_apply` instead. :func:`script_of` folds Hiragana, Katakana and
Hangul into "han" because the one thing it is asked is whether the vowel rule
applies, and for all four the answer is no - but the "zh" list is Han
ideographs, so a page of kana is in this set and has no word list behind it.
It is also too coarse in the other direction: a page written wholly in a script
this module cannot name comes back "mixed", which is *not* in this set, and
"mixed" is also what a genuinely bilingual Latin/Cyrillic page returns, which
is.
"""

_LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "ru": "Russian",
    "uk": "Ukrainian",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "pt": "Portuguese",
    "it": "Italian",
    "pl": "Polish",
    "nl": "Dutch",
    "zh": "Chinese",
}


def language_names() -> dict[str, str]:
    """Map of code to display name, for the CLI and the about page."""
    return dict(_LANGUAGE_NAMES)


# --------------------------------------------------------------------------
# scripts
# --------------------------------------------------------------------------

# Ranges are the primary Unicode blocks for each script, enough to classify a
# token. We deliberately fold Hiragana, Katakana and Hangul into "han": the
# only decision this module makes with the answer is "does the vowel rule
# apply", and for all four the answer is no.
_SCRIPT_RANGES: tuple[tuple[int, int, str], ...] = (
    (0x0041, 0x024F, "latin"),  # basic latin letters through latin extended-B
    (0x1E00, 0x1EFF, "latin"),  # latin extended additional (Vietnamese)
    (0x0370, 0x03FF, "greek"),
    (0x1F00, 0x1FFF, "greek"),  # greek extended (polytonic)
    (0x0400, 0x052F, "cyrillic"),
    # Hebrew (0x0590-0x05FF) is grouped with Arabic on purpose. The vocabulary
    # this module must return has no name for it, and the single decision taken
    # from the answer - whether the vowel rule applies - is the same for both,
    # because both are abjads that do not write their short vowels. If Hebrew
    # documents ever need distinguishing, the vocabulary is what has to grow.
    (0x0590, 0x06FF, "arabic"),
    (0x0750, 0x077F, "arabic"),
    (0x3040, 0x30FF, "han"),  # hiragana + katakana
    (0x3400, 0x4DBF, "han"),  # CJK extension A
    (0x4E00, 0x9FFF, "han"),  # CJK unified ideographs
    (0xAC00, 0xD7AF, "han"),  # hangul syllables
    (0xF900, 0xFAFF, "han"),  # CJK compatibility ideographs
)

_DOMINANT_SCRIPT_SHARE = 0.85
"""Share of letters one script needs to own before we name the text after it.

Judgement, not measurement: 0.85 tolerates the stray Latin acronym or digit
group inside a Cyrillic page (extremely common in scanned officialese) without
calling a genuinely bilingual page monolingual.
"""


def _script_of_char(ch: str) -> str | None:
    """Script name for one character, or ``None`` if it is not a letter."""
    if not ch.isalpha():
        return None
    cp = ord(ch)
    for lo, hi, name in _SCRIPT_RANGES:
        if lo <= cp <= hi:
            return name
    return "other"


def script_of(text: str) -> str:
    """Dominant script of *text*: latin, cyrillic, greek, arabic, han or mixed.

    Only letters vote. Digits, punctuation and whitespace are script-neutral,
    so ``"(b)(7)(C)"`` is Latin rather than mixed. Text with no letters at all
    is reported as Latin, which is the neutral default the garbage rules
    already assume - the rules that matter for a digit string (length, repeats,
    punctuation count) are script-independent anyway.

    A script this vocabulary cannot name - Devanagari, Thai, Georgian - comes
    back as "mixed", and "mixed" is treated everywhere as "do not apply
    Latin-shaped reasoning to this". That is the safe direction: the cost is a
    weaker check on those pages, not a corpus wrongly declared unreadable.
    """
    counts: dict[str, int] = {}
    total = 0
    for ch in text:
        name = _script_of_char(ch)
        if name is None:
            continue
        counts[name] = counts.get(name, 0) + 1
        total += 1
    if total == 0:
        return "latin"
    best, best_n = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    if best == "other":
        return "mixed"
    return best if best_n / total >= _DOMINANT_SCRIPT_SHARE else "mixed"


_KANA_AND_HANGUL: tuple[tuple[int, int], ...] = ((0x3040, 0x30FF), (0xAC00, 0xD7AF))
"""The two blocks inside "han" that :data:`STOPWORDS` does not cover.

Kana and Hangul share a script name with the Han ideographs because the vowel
rule treats all three the same way. The word list does not: "zh" is a hundred
Han *characters*, and a page of Japanese or Korean matches none of them.
"""

_MIN_STOPWORD_LETTER_SHARE = 0.5
"""How much of a page must be in a covered script before the ratio means anything.

Judgement. Half is the point at which the covered part of the page can still
carry the signal: a page 60% English and 40% Hindi can reach a stopword ratio
of about 0.6 x 0.35 = 0.21, comfortably above the 0.10 that
``ingest/quality.py`` treats as "not language", while a page 40% English and
60% Hindi tops out near 0.14 and is not worth an opinion. Below the share we
say so rather than guessing, because the two answers a zero can mean - "this is
garbage" and "we have no words for this" - are not interchangeable and only one
of them is a reason to re-OCR a page.
"""


def stopwords_apply(text: str) -> bool:
    """Can a stopword ratio mean anything for *text*?

    True when at least :data:`_MIN_STOPWORD_LETTER_SHARE` of the letters in
    *text* are written in a script one of the :data:`STOPWORDS` lists is
    written in. Digits and punctuation do not vote, exactly as in
    :func:`script_of`; text with no letters at all gets no opinion.

    This is the question a caller actually has, and it is not
    ``script_of(text) in SCRIPTS_WITH_STOPWORDS`` - that test is wrong in both
    directions. A page of Japanese kana is "han" and has no word list. A page
    of Devanagari, Thai or Georgian is "mixed", because those scripts have no
    name in this module's vocabulary, and "mixed" is the same answer a
    bilingual Latin/Cyrillic page gives - one of those pages we can judge and
    the other we cannot.

    What it deliberately does **not** catch is a language we have no list for
    written in a script we do: Vietnamese is Latin, scores zero against all
    eleven lists, and comes back True here. Nothing about the script can tell
    us otherwise, so the protection for that page has to be - and is -
    ``ingest/quality.py``'s rule that no single signal condemns a page.
    """
    covered = total = 0
    for ch in text:
        if not ch.isalpha():
            continue
        total += 1
        if _script_of_char(ch) not in SCRIPTS_WITH_STOPWORDS:
            continue
        cp = ord(ch)
        if any(lo <= cp <= hi for lo, hi in _KANA_AND_HANGUL):
            continue
        covered += 1
    if total == 0:
        return False
    return covered / total >= _MIN_STOPWORD_LETTER_SHARE


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------

# Categories stripped from the ends of a token. We keep P* and S* off both
# ends but never from the middle, because the middle is where the meaning is:
# "l'homme", "well-known" and "U.S.C." all have to survive.
_STRIP_CATEGORIES = ("P", "S")

# OCR and PDF text layers produce three different apostrophes and four
# different hyphens for the same key on the typewriter. Fold them, or the
# French stopword "d'" matches in one document and not the next.
_PUNCT_FOLD = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‛": "'",
        "ʼ": "'",
        "´": "'",
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "―": "-",
        "−": "-",
    }
)


def normalize_token(tok: str) -> str:
    """Casefold, NFKC-normalise and strip edge punctuation from one token.

    NFKC first: it turns ligatures (ﬁ), fullwidth forms and compatibility
    digits into their plain equivalents, which is exactly the noise a PDF text
    layer emits. Casefold rather than lower() so that German ``STRASSE`` and
    ``Straße`` both reach ``strasse``.
    """
    if not tok:
        return ""
    out = unicodedata.normalize("NFKC", tok).translate(_PUNCT_FOLD).casefold()
    start, end = 0, len(out)
    while start < end and unicodedata.category(out[start])[0] in _STRIP_CATEGORIES:
        start += 1
    while end > start and unicodedata.category(out[end - 1])[0] in _STRIP_CATEGORIES:
        end -= 1
    return out[start:end]


def _alphabetic(tokens: Sequence[str]) -> list[str]:
    """Normalised tokens that contain at least one letter.

    Numbers are dropped from the denominator of every ratio in this module. A
    page of a budget table is 90% digits and has no stopwords at all; counting
    those digits would drag its stopword ratio to zero and get a perfectly
    readable page flagged as garbage.
    """
    out = []
    for tok in tokens:
        norm = normalize_token(tok)
        if norm and any(ch.isalpha() for ch in norm):
            out.append(norm)
    return out


# --------------------------------------------------------------------------
# the stopword signal
# --------------------------------------------------------------------------


_TESSERACT_CODES = {
    "eng": "en", "rus": "ru", "ukr": "uk", "deu": "de", "ger": "de",
    "fra": "fr", "fre": "fr", "spa": "es", "por": "pt", "ita": "it",
    "pol": "pl", "nld": "nl", "dut": "nl", "chi_sim": "zh", "chi_tra": "zh",
    "chi_sim_vert": "zh", "chi_tra_vert": "zh",
}
"""Tesseract names its languages in ISO 639-2; we speak 639-1.

This map exists because the two vocabularies meet at exactly one place - the
operator writes ``ocr.languages = ["rus"]`` in their config because that is
what Tesseract's package is called, and every module downstream wants ``ru``.
Getting this wrong is silent and total: an unrecognised code matches no
stopwords, every page scores zero, and a perfectly good collection is declared
unreadable end to end.
"""


def normalize_language_codes(codes: Sequence[str] | None) -> list[str]:
    """Turn whatever the caller has into codes this module knows.

    Accepts ISO 639-1 (``ru``), ISO 639-2 / Tesseract (``rus``), locale forms
    (``ru_RU``, ``en-GB``) and Tesseract's script suffixes (``chi_sim``).
    Anything still unrecognised is dropped rather than passed through, so a
    caller can tell "no languages I know" from "these languages".
    """
    if not codes:
        return []
    out: list[str] = []
    for raw in codes:
        code = raw.strip().lower()
        if not code:
            continue
        mapped = _TESSERACT_CODES.get(code)
        if mapped is None and code in STOPWORDS:
            mapped = code
        if mapped is None:
            base = code.replace("-", "_").split("_")[0]
            mapped = _TESSERACT_CODES.get(base, base if base in STOPWORDS else None)
        if mapped and mapped not in out:
            out.append(mapped)
    return out


def stopword_ratio(tokens: Sequence[str], languages: Sequence[str] | None = None) -> float:
    """Share of *tokens* that are function words, in ``[0, 1]``.

    The score is the best a page can do: the **maximum** over each language's
    list taken separately and, when *languages* names more than one, the union
    of those lists as well - whichever is higher.

    *The maximum, not the union of everything.* The union of eleven languages
    matches so much short garbage - "de", "a", "i", "o", "no", "se" are
    stopwords somewhere - that the signal would be diluted by roughly a third.
    Scoring each language separately and keeping the winner asks the question
    this module is for, "is this any language?", without paying for that.

    *The declared languages are a prior, never a filter.* Naming languages
    asserts what the collection is written in, and a genuinely bilingual page
    should be able to score for both halves at once, which only the union can
    do. So the union is offered *against* the language-agnostic maximum rather
    than in place of it, and naming a language can only ever raise a page's
    score.

    It used to replace it, and that was a bug with consequences: a Russian page
    in a collection declared ``["eng"]`` scored exactly zero, was told by
    ``ingest/quality.py`` that its text layer "does not read as language", and
    was re-OCR'd or published as unreadable. The two lists people reach for
    here are different facts - what the recogniser should *try*, which has to
    be a filter because every extra alphabet costs accuracy on the others, and
    what the text is *expected* to be, which must not be, because being wrong
    about it should cost nothing. Only the first belongs in ``ocr.languages``,
    and it is deliberately the only language list this pipeline asks an
    operator for.

    A consequence worth stating rather than discovering: with the maximum
    always taken, a declared list changes the answer only for a page that is
    genuinely two languages at once. That is the whole of what a second
    "expected languages" setting would buy, and it is why there is not one.

    Measured on this project's fixtures: clean typed page 0.38, the same page
    rotated 90 degrees 0.00, heavy blur 0.00.
    """
    words = _alphabetic(tokens)
    if not words:
        return 0.0
    best = 0.0
    for stops in STOPWORDS.values():
        hits = sum(1 for w in words if w in stops)
        best = max(best, hits / len(words))
    # One declared language is already in the maximum above; only a union of
    # two or more can beat it. An unrecognised code drops out in
    # `normalize_language_codes`, so a collection declared in a language we
    # have no list for simply scores as the maximum, which is the answer that
    # under-reports rather than the one that condemns.
    known = normalize_language_codes(languages)
    if len(known) > 1:
        wanted: set[str] = set()
        for code in known:
            wanted |= STOPWORDS.get(code, frozenset())
        best = max(best, sum(1 for w in words if w in wanted) / len(words))
    return best


_CONFIDENT_RATIO = 0.20
"""Stopword ratio at which we call a language identification certain.

Measured: clean English prose scores 0.38 here, and the shortest real fixture
paragraph still clears 0.25. Anything at or above 0.20 is prose; the confidence
scale below is linear up to this point and saturates at it.
"""


def detect_language(tokens: Sequence[str]) -> tuple[str, float]:
    """Best-matching language code and a confidence in ``[0, 1]``.

    Confidence is deliberately low when *nothing* scores well, and that is a
    signal in its own right: text that matches no language's function words is
    usually not text. The score is the winner's stopword ratio, scaled against
    :data:`_CONFIDENT_RATIO` and then discounted when the runner-up is close,
    because "it is one of Spanish or Portuguese" is a real answer and should not
    be reported as certainty about either.
    """
    words = _alphabetic(tokens)
    if not words:
        return ("und", 0.0)
    scores = sorted(
        ((sum(1 for w in words if w in stops) / len(words), code) for code, stops in STOPWORDS.items()),
        key=lambda sc: (-sc[0], sc[1]),
    )
    top_ratio, top_code = scores[0]
    if top_ratio <= 0.0:
        return ("und", 0.0)
    runner_up = scores[1][0] if len(scores) > 1 else 0.0
    strength = min(1.0, top_ratio / _CONFIDENT_RATIO)
    # Margin: 1.0 when the winner is unchallenged, 0 when it ties.
    margin = (top_ratio - runner_up) / top_ratio
    # A short page cannot support a confident answer whatever it matches; 40
    # tokens is roughly three lines of prose (judgement).
    sample = min(1.0, len(words) / 40.0)
    return (top_code, round(strength * (0.35 + 0.65 * margin) * sample, 4))


# --------------------------------------------------------------------------
# garbage tokens
# --------------------------------------------------------------------------

# Taghva et al., "Automatic Removal of Garbage Strings in OCR Text" (2001) and
# Cuper, "Examining a Multi Layered Approach for Classification of OCR Quality
# without Ground Truth" (2022). Reference corpora sit at a garbage rate of 0.08
# or below.
_MAX_TOKEN_LEN = 20
"""Taghva rule L: a token longer than 20 characters is almost never a word."""

_MAX_REPEAT_RUN = 4
"""Taghva rule R: four or more identical characters in a row (``lllll``)."""

_MAX_PUNCT = 2
"""Taghva rule P: more than two punctuation or symbol characters in a token.

Counted by Unicode category (P*, S* and the C* controls), *not* by
``str.isalnum()``. Combining marks are category M and are part of the letter
they sit on: counting them as punctuation makes "दुनिया" three marks of
punctuation and condemns Devanagari, Thai, Hebrew with niqqud and any
decomposed Vietnamese along with it. Found by testing the rule against four
writing systems it was never designed for.

Known cost, accepted: heavily punctuated citations fail it. "U.S.C." has three
full stops and is called garbage. That is tolerable only because the caller
needs a *rate* above 0.08 before it counts as evidence, and a page carries far
more prose than citations - but it is the reason the garbage rate is never
allowed to condemn a page on its own.
"""

_PUNCT_CATEGORIES = ("P", "S", "C")

_VOWEL_MIN, _VOWEL_MAX = 0.10, 0.90
"""Taghva rule V, as a vowel *fraction* of the token's letters.

The literature states the bound as a "vowel-consonant ratio" of 0.1 to 0.9. Read
as a literal v/c quotient the upper bound rejects ordinary words - "idea" has
three vowels to one consonant, a quotient of 3.0 - so we read it as the vowel
fraction, which puts "strengths" at 0.11 and "aeiou" at 1.00. That is the
reading that reproduces the published garbage rates.
"""

_MIN_LEN_FOR_VOWEL_RULE = 4
"""Below this the vowel rule flags real abbreviations, so we do not apply it.

Measured against the obvious short forms: "st", "mr", "dr", "vs", "pp", "cf"
and every two-letter statutory subpart would otherwise be garbage.
"""

_VOWELS: dict[str, frozenset[str]] = {
    # 'y' counts: without it "rhythms" and "myths" are zero-vowel garbage.
    "latin": frozenset("aeiouyàáâãäåæèéêëìíîïòóôõöøùúûüýÿœ"),
    "cyrillic": frozenset("аеёиоуыэюяіїєўӑәөүұ"),
    "greek": frozenset("αεηιουωάέήίόύώϊϋΐΰ"),
}

_SCRIPTS_WITHOUT_VOWEL_RULE = frozenset({"han", "arabic", "mixed", "other"})
"""Scripts the vowel rule must never touch.

Han/Kana/Hangul have no vowel letters at all, so the rule would classify 100%
of Chinese, Japanese and Korean text as garbage. Arabic and Hebrew are abjads:
short vowels are simply not written. "Mixed" is excluded because a
script-blind vowel count over two alphabets is meaningless - the other three
rules still apply and they are what catch mixed-script OCR spray.
"""


def is_garbage_token(tok: str, script: str = "latin") -> bool:
    """Is *tok* a string OCR invented rather than read?

    The rules are Taghva's, made script-aware. Length, repeated characters and
    punctuation count apply to every script; the vowel-fraction rule applies
    only where vowels are written as letters. Getting that wrong is not a
    tuning question - a Han-blind vowel rule declares every Chinese page in the
    archive to be garbage, which is worse than having no quality check at all.
    """
    raw = unicodedata.normalize("NFKC", tok).translate(_PUNCT_FOLD).strip()
    if not raw:
        return True
    if len(raw) > _MAX_TOKEN_LEN:
        return True

    # Four identical characters in a row. Applies to every script: no writing
    # system repeats a glyph four times inside one token.
    run = 1
    for prev, cur in pairwise(raw):
        run = run + 1 if cur == prev else 1
        if run >= _MAX_REPEAT_RUN:
            return True

    letters = [ch for ch in raw if ch.isalpha()]
    punctuation = sum(1 for ch in raw if unicodedata.category(ch)[0] in _PUNCT_CATEGORIES)
    if punctuation > _MAX_PUNCT:
        return True
    if not letters:
        # Pure digits and dates are not garbage; a bare punctuation smear is,
        # and it has already been caught by the rule above.
        return False

    if script in _SCRIPTS_WITHOUT_VOWEL_RULE:
        return False
    vowels = _VOWELS.get(script)
    if vowels is None:
        return False
    if len(letters) < _MIN_LEN_FOR_VOWEL_RULE:
        return False
    frac = sum(1 for ch in letters if ch.casefold() in vowels) / len(letters)
    return not (_VOWEL_MIN <= frac <= _VOWEL_MAX)
