"""The page cache: that it is fast, and that it changes nothing.

The second half is the whole point. A cache is a claim - *this answer is the
same answer you would have computed* - and a build that publishes evidence
cannot make that claim on the strength of an argument. So the test that matters
most here builds a collection twice, once with an empty cache and once with a
full one, and compares the two output trees byte for byte, every file, including
the images.

The rest of the file is about the ways a cache goes wrong, which are all the
same way: it hands back something that is no longer true. Each one gets a test.

  key sensitivity      change one field of the job, get a different answer
  key stability        change nothing, get a hit - across processes, too
  round-tripping       every model type, every enum, every box, exactly
  hidden text          a planted secret is nowhere on disk afterwards
  eviction             a bounded cache stays bounded
  corruption           truncated, scribbled on, half-deleted: do the work
  concurrency          two builds, one cache, no lock, no wrong answers
  read-only, full      degrade to doing the work, never to a crash

These tests run against ``stackroom.pipeline`` whether or not the cache has been
wired into it yet: see :func:`run_build`.
"""

from __future__ import annotations

import errno
import gzip
import inspect
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

import synth
from stackroom import cache as cache_mod
from stackroom.build.site import attach_about, build_site
from stackroom.cache import PageCache, key_for, key_inputs
from stackroom.config import Config
from stackroom.model import (
    Box,
    ImageVariant,
    OcrQuality,
    Page,
    PageVerdict,
    Redaction,
    RedactionKind,
    TextSource,
    Word,
)
from stackroom.pipeline import PageJob, PageOutcome, build_collection

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

needs_poppler = pytest.mark.skipif(
    shutil.which("pdftoppm") is None, reason="poppler is not installed"
)
FIXED_STAMP = "2020-01-01T00:00:00+00:00"
"""A pinned build stamp for the byte-comparison.

``BuildInfo.built_at`` reads the clock, and it is written into
``manifest.json`` and printed in every page footer. It is the one thing in a
Stackroom site that is not a function of the input bytes, it is decided once for
the whole collection rather than per page, and it has nothing to do with the
cache - so it is pinned here rather than allowed to fail a comparison that is
about something else. (It is also the reason guarantee 6 is not quite true
today; see the report.)"""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _never_the_real_cache(tmp_path, monkeypatch):
    """No test may touch the machine's own cache directory.

    Every test here passes an explicit root, so this is belt and braces - but
    the failure it guards against is a test suite that fills a contributor's
    ``~/.cache`` with page images of their documents, which is exactly the thing
    this module has to be trusted not to do.
    """
    monkeypatch.setenv("STACKROOM_CACHE_DIR", str(tmp_path / "never-used"))
    monkeypatch.delenv("STACKROOM_CACHE_SALT", raising=False)
    monkeypatch.delenv("STACKROOM_CACHE", raising=False)


def fast_config(**overrides) -> Config:
    """The shipped defaults, made cheap: one small WebP, no search."""
    cfg = Config()
    cfg.render.dpi = 100
    cfg.render.widths = [600]
    cfg.render.thumb_width = 120
    cfg.render.formats = ["webp"]
    cfg.search.enabled = False
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def a_job(**overrides) -> PageJob:
    base = {
        "pdf": "/collections/march/release.pdf",
        "doc_id": "release",
        "number": 3,
        "media_dir": "/tmp/site/media/release",
        "media_prefix": "media/release",
    }
    base.update(overrides)
    return PageJob(**base)


DIGEST = "d" * 64


def run_build(root: Path, out: Path, cache: PageCache | None, cfg: Config | None = None):
    """Run the pipeline with a cache, wired or not.

    ``pipeline.build_collection`` grows a ``cache=`` argument when the wiring in
    the report is applied. Until it does, this does exactly what that wiring
    does - look before working, store after - from outside, so that these tests
    describe the behaviour rather than the patch, and keep passing across it.
    """
    cfg = cfg or fast_config()
    if cache is None:
        return build_collection(root, cfg, out, workers=1)
    if "cache" in inspect.signature(build_collection).parameters:
        return build_collection(root, cfg, out, workers=1, cache=cache)

    import stackroom.pipeline as pipeline_mod

    real = pipeline_mod.process_page
    cache.reset()

    def cached(job: PageJob) -> PageOutcome:
        restored = cache.get(job)
        if restored is not None:
            return restored
        outcome = real(job)
        cache.put(job, outcome)
        return outcome

    pipeline_mod.process_page = cached
    try:
        return build_collection(root, cfg, out, workers=1)
    finally:
        pipeline_mod.process_page = real


def build_site_from(root: Path, out: Path, cache: PageCache | None, cfg: Config | None = None):
    """A whole site on disk, with the clock pinned."""
    cfg = cfg or fast_config()
    collection, outcomes = run_build(root, out, cache, cfg)
    collection.build.built_at = FIXED_STAMP
    collection.build.duration_seconds = 0.0
    attach_about(collection, cfg)
    build_site(collection, cfg, out)
    return collection, outcomes


def blobs(cache: PageCache) -> list[Path]:
    """Every stored image file. ``rglob`` also yields the shard directories."""
    return sorted(p for p in (cache.root / "blobs").rglob("*") if p.is_file())


def tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def fake_outcome(
    job: PageJob, *, payload: bytes = b"not really a webp", words: int = 3
) -> PageOutcome:
    """An outcome and the image file that goes with it, without a PDF.

    The store does not care that these are not real pages; it cares about paths,
    digests, sizes and eviction order, and this makes those testable in
    milliseconds instead of seconds.
    """
    media = Path(job.media_dir)
    media.mkdir(parents=True, exist_ok=True)
    name = f"p{job.number:04d}@600.webp"
    (media / name).write_bytes(payload)
    page = Page(
        number=job.number,
        words=[Word(f"word{i}", Box(0.1 * i, 0.2, 0.05, 0.01), conf=90, line=0) for i in range(words)],
        lines=["word0 word1 word2"],
        images=[ImageVariant(f"{job.media_prefix}/{name}", "webp", 600, 800, len(payload))],
    )
    return PageOutcome(doc_id=job.doc_id, number=job.number, page=page, seconds=1.25)


# ==========================================================================
# 1. the key
# ==========================================================================


def test_the_same_job_and_the_same_bytes_give_the_same_key():
    assert key_for(a_job(), DIGEST) == key_for(a_job(), DIGEST)


def test_the_key_is_the_same_in_another_process():
    """Nothing in the key may depend on hash randomisation or on set order.

    A key that changes between processes is a cache that never hits, and it
    would not show up in a single-process test: the pool is processes, and so is
    the next build.
    """
    code = (
        "from stackroom.cache import key_for;"
        "from stackroom.pipeline import PageJob;"
        "print(key_for(PageJob(pdf='/collections/march/release.pdf', doc_id='release',"
        " number=3, media_dir='/tmp/site/media/release', media_prefix='media/release'),"
        f" {DIGEST!r}))"
    )
    keys = set()
    for seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
        )
        keys.add(out.stdout.strip())
    assert len(keys) == 1
    assert keys == {key_for(a_job(), DIGEST)}


def test_different_source_bytes_give_a_different_key():
    """The file's content, not its name, its size or its date."""
    assert key_for(a_job(), DIGEST) != key_for(a_job(), "e" * 64)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("doc_id", "somewhere-else"),
        ("number", 4),
        ("media_prefix", "media/somewhere-else"),
        ("dpi", 300),
        ("widths", (900,)),
        ("thumb_width", 200),
        ("formats", ("webp",)),
        ("max_megapixels", 20.0),
        ("ocr_mode", "always"),
        ("ocr_languages", ("eng", "fra")),
        ("psm", 6),
        ("auto_rotate", False),
        ("is_image", True),
    ],
)
def test_changing_a_field_that_changes_the_output_changes_the_key(field, value):
    assert key_for(a_job(), DIGEST) != key_for(a_job(**{field: value}), DIGEST)


def test_the_order_of_the_ocr_languages_is_part_of_the_key():
    """``-l eng+fra`` is not ``-l fra+eng``: Tesseract weights the first.

    A key over a *set* of languages would serve one for the other, and the
    difference is invisible until somebody reads a page of French.
    """
    a = key_for(a_job(ocr_languages=("eng", "fra")), DIGEST)
    b = key_for(a_job(ocr_languages=("fra", "eng")), DIGEST)
    assert a != b


def test_a_list_and_a_tuple_of_the_same_widths_are_the_same_key():
    """Nothing about a Python type should decide whether a page is re-encoded."""
    assert key_for(a_job(widths=(1600, 900)), DIGEST) == key_for(
        a_job(widths=[1600, 900]), DIGEST
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pdf", "/somewhere/else/entirely/release.pdf"),
        ("media_dir", "/var/tmp/another/site/media/release"),
        ("ocr_timeout", 600.0),
    ],
)
def test_things_that_cannot_change_the_output_do_not_change_the_key(field, value):
    """Moving a collection, building to a new folder, or raising a timeout must
    not cost hours. Each of these is argued for in ``docs/CACHING.md``."""
    assert key_for(a_job(), DIGEST) == key_for(a_job(**{field: value}), DIGEST)


def test_the_tool_versions_are_in_the_key():
    """A Tesseract upgrade changes the text on every scanned page."""
    env = cache_mod.probe_environment()
    other = replace(env, tesseract="9.9.9")
    assert key_for(a_job(), DIGEST, env) != key_for(a_job(), DIGEST, other)
    for field, value in (
        ("poppler", "pdftoppm version 0.0.1"),
        ("stackroom", "99.0.0"),
        ("source", "src:0000"),
        ("formats", ("webp",)),
        ("fonts", "fc:0:none"),
        ("omp_threads", "8"),
    ):
        assert key_for(a_job(), DIGEST, env) != key_for(
            a_job(), DIGEST, replace(env, **{field: value})
        ), field


def test_the_pillow_and_codec_versions_are_in_the_key():
    """The image libraries decide the published bytes, not Pillow alone."""
    env = cache_mod.probe_environment()
    other = replace(env, pillow={**env.pillow, "module:webp": "0.0.0"})
    assert key_for(a_job(), DIGEST, env) != key_for(a_job(), DIGEST, other)


def test_a_salt_lets_an_operator_invalidate_everything():
    assert key_for(a_job(), DIGEST, salt="") != key_for(a_job(), DIGEST, salt="rebuild-please")


def test_the_key_inputs_are_readable_and_complete():
    """``key_inputs`` is the documentation's executable half."""
    inputs = key_inputs(a_job(), DIGEST)
    assert inputs["source"]["sha256"] == DIGEST
    assert set(inputs["job"]) == set(cache_mod.KEYED_JOB_FIELDS)
    assert inputs["format"] == cache_mod.FORMAT
    assert inputs["schema"] and inputs["env"]["stackroom"]


def test_every_field_of_pagejob_is_accounted_for():
    """The tripwire. A field added to PageJob that nobody classified could
    change the output while sitting outside the key, and that is the one bug a
    cache must not have."""
    assert cache_mod._check_job_fields() == ""


def test_an_unclassified_job_field_disables_the_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_mod, "KEYED_JOB_FIELDS", frozenset({"number"}))
    cache = PageCache(tmp_path / "c")
    assert not cache.enabled
    assert "never heard of" in cache.disabled_reason
    assert cache.get(a_job()) is None


def test_the_codec_covers_every_field_of_every_model_type():
    assert cache_mod._check_codec() == ""


def test_the_schema_digest_is_part_of_the_key():
    """A field added to Page must make yesterday's entries unreachable rather
    than decode into a Page that is quietly missing it."""
    before = key_for(a_job(), DIGEST)
    cache_mod._model_schema.cache_clear()
    monkeyed = "different"
    original = Page.__dataclass_fields__
    try:
        Page.__dataclass_fields__ = {**original, monkeyed: original["number"]}
        cache_mod._model_schema.cache_clear()
        after = key_for(a_job(), DIGEST)
    finally:
        Page.__dataclass_fields__ = original
        cache_mod._model_schema.cache_clear()
    assert before != after


# ==========================================================================
# 2. serialisation
# ==========================================================================


def a_page() -> Page:
    """One page carrying every type the cache has to survive."""
    return Page(
        number=7,
        width_pt=612.0,
        height_pt=792.5,
        source=TextSource.OCR_OVERRIDE,
        words=[
            Word("Commission", Box(0.1, 0.2, 0.3, 0.04), conf=96, line=0),
            Word("秘", Box(1 / 3, 0.1 + 0.2, 1e-9, 0.0), conf=-1, line=1, hidden=True),
            Word("الوثيقة", Box(-0.0, 0.99999999, 0.5, 0.5), conf=0, line=1),
            Word("naïve—résumé", Box(0.12345678901234, 0.5, 0.2, 0.3), conf=100, line=2),
        ],
        lines=["Commission 秘", "الوثيقة naïve—résumé"],
        redactions=[
            Redaction(Box(0.1, 0.1, 0.2, 0.02), RedactionKind.VECTOR, codes=["b(6)", "b(7)(C)"]),
            Redaction(Box(0.4, 0.4, 0.1, 0.1), RedactionKind.RASTER),
        ],
        exemptions=[],
        bates=None,
        quality=OcrQuality(
            verdict=PageVerdict.SUSPECT,
            word_count=4,
            median_conf=71.5,
            low_conf_fraction=0.3333333333333333,
            stopword_ratio=0.019999999999999997,
            garbage_ratio=0.0,
            mean_word_length=6.25,
            ink_coverage=0.1234567890123,
            reasons=["stopword ratio 0.02", "median confidence 71"],
        ),
        redaction_ratio=0.08123456789,
        images=[ImageVariant("media/release/p0007@600.webp", "webp", 600, 776, 41234)],
        thumbs=[ImageVariant("media/release/p0007@thumb.webp", "webp", 120, 155, 2201)],
        placeholder="data:image/webp;base64,UklGRg==",
        language="en",
    )


def test_every_model_type_round_trips_exactly():
    page = a_page()
    back = cache_mod.decode_page(json.loads(json.dumps(cache_mod.encode_page(page))))
    assert back == page
    assert isinstance(back.source, TextSource)
    assert isinstance(back.quality.verdict, PageVerdict)
    assert [r.kind for r in back.redactions] == [RedactionKind.VECTOR, RedactionKind.RASTER]


def test_the_boxes_come_back_bit_for_bit():
    """Not nearly. ``model.to_jsonable`` rounds a box to 1/10,000 of the page,
    which is right for the JSON a browser reads and wrong for a cache: the
    negative draws redactions to three decimal places of a percent, and a warm
    build that rounded would publish different SVG from a cold one."""
    page = a_page()
    back = cache_mod.decode_page(json.loads(json.dumps(cache_mod.encode_page(page))))
    for before, after in zip(page.words, back.words, strict=True):
        assert repr(after.box) == repr(before.box)
    assert repr(back.redaction_ratio) == repr(page.redaction_ratio)
    assert repr(back.quality.ink_coverage) == repr(page.quality.ink_coverage)


def test_two_boxes_that_model_scale_would_collide_stay_apart():
    """The concrete version of the test above: 1e-5 and 2e-5 of the page are
    both zero at ``model.SCALE``, and are not the same box."""
    page = Page(
        number=1,
        words=[
            Word("a", Box(0.00001, 0.0, 0.1, 0.1)),
            Word("b", Box(0.00002, 0.0, 0.1, 0.1)),
        ],
    )
    back = cache_mod.decode_page(json.loads(json.dumps(cache_mod.encode_page(page))))
    assert back.words[0].box.x != back.words[1].box.x
    assert back == page


def test_an_outcome_round_trips_and_loses_only_the_clock():
    job = a_job()
    outcome = fake_outcome(job)
    outcome.warnings = ["the page was scanned 90 degrees out of upright"]
    back = cache_mod.decode_outcome(json.loads(json.dumps(cache_mod.encode_outcome(outcome))))
    assert back.doc_id == outcome.doc_id
    assert back.number == outcome.number
    assert back.page == outcome.page
    assert back.warnings == outcome.warnings
    assert back.error is None and back.analysis_failed is False


def test_a_page_carrying_recovered_text_is_refused_by_the_codec():
    """Nothing puts recovered text on ``Page.hidden`` today. If anything ever
    does, this is what stops it reaching a file."""
    from stackroom.model import HiddenText

    page = a_page()
    page.hidden = [HiddenText(Box(0.1, 0.1, 0.2, 0.02), "Gregory Aldana")]
    with pytest.raises(cache_mod.CodecError):
        cache_mod.encode_page(page)


def test_nonsense_does_not_decode_into_a_page():
    for junk in ({}, [], "page", {"number": 1}, {**cache_mod.encode_page(a_page()), "words": [[1]]}):
        with pytest.raises(cache_mod.CodecError):
            cache_mod.decode_page(junk)


# ==========================================================================
# 3. the store: hits, misses, and the images
# ==========================================================================


def test_a_stored_page_comes_back_with_its_images(tmp_path):
    cache = PageCache(tmp_path / "cache")
    job = a_job(media_dir=str(tmp_path / "site-a" / "media" / "release"))
    outcome = fake_outcome(job, payload=b"pretend this is a webp" * 40)
    assert cache.put(job, outcome, DIGEST)

    elsewhere = a_job(media_dir=str(tmp_path / "site-b" / "media" / "release"))
    back = cache.get(elsewhere, DIGEST)
    assert back is not None
    assert back.page == outcome.page
    written = Path(elsewhere.media_dir) / "p0003@600.webp"
    assert written.read_bytes() == (Path(job.media_dir) / "p0003@600.webp").read_bytes()


def test_the_restored_image_is_hard_linked_rather_than_re_encoded(tmp_path):
    """61% of a build is encoding images. Not doing it again is most of the
    saving, and a link is the cheapest way not to."""
    cache = PageCache(tmp_path / "cache")
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    cache.put(job, fake_outcome(job), DIGEST)
    shutil.rmtree(tmp_path / "site")

    assert cache.get(job, DIGEST) is not None
    restored = Path(job.media_dir) / "p0003@600.webp"
    blob = blobs(cache)[0]
    assert restored.stat().st_ino == blob.stat().st_ino


def test_a_miss_leaves_nothing_behind(tmp_path):
    cache = PageCache(tmp_path / "cache")
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    assert cache.get(job, DIGEST) is None
    assert cache.misses == 1
    assert not (tmp_path / "site").exists() or not list((tmp_path / "site").rglob("*.webp"))


def test_identical_images_from_two_documents_are_stored_once(tmp_path):
    """Content addressing, for free: the same blank page in two productions,
    the same exhibit attached to two memos."""
    cache = PageCache(tmp_path / "cache")
    for doc in ("one", "two"):
        job = a_job(
            doc_id=doc,
            media_prefix=f"media/{doc}",
            media_dir=str(tmp_path / "site" / "media" / doc),
        )
        cache.put(job, fake_outcome(job, payload=b"identical bytes"), DIGEST)
    assert len(blobs(cache)) == 1


def test_the_counters_start_again_for_each_build(tmp_path):
    """A watch session keeps one cache open for hours."""
    cache = PageCache(tmp_path / "cache")
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    cache.put(job, fake_outcome(job), DIGEST)
    cache.get(job, DIGEST)
    assert cache.hits == 1
    cache.reset()
    assert (cache.hits, cache.misses, cache.saved_seconds) == (0, 0, 0.0)
    assert cache.get(job, DIGEST) is not None


# --------------------------------------------------------------------------
# why a build missed
# --------------------------------------------------------------------------


def _stamp_path(cache: PageCache) -> Path:
    return cache.root / cache_mod.ENV_STAMP


def test_opening_a_cache_does_not_stamp_it(tmp_path):
    """``stackroom cache show`` opens one, and must not erase the record.

    If the stamp were written on open, the first command an operator ran to
    look at a cold cache would overwrite the one thing that could explain it.
    """
    cache = PageCache(tmp_path / "cache")
    assert not _stamp_path(cache).exists()
    cache.stats()
    assert not _stamp_path(cache).exists()


def test_storing_a_page_records_what_it_was_built_with(tmp_path):
    cache = PageCache(tmp_path / "cache")
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    assert cache.put(job, fake_outcome(job), DIGEST)
    written = json.loads(_stamp_path(cache).read_text(encoding="utf-8"))
    assert written["environment"] == cache.env.as_dict()
    assert isinstance(written["written"], int)


def test_a_build_is_not_handed_back_its_own_stamp(tmp_path):
    """The stamp is read when the cache opens, not when the question is asked.

    ``_explain_a_cold_cache`` runs at the *end* of a build, by which point that
    build has stored four thousand pages and rewritten the stamp. Reading it
    then would compare this environment with itself and report that nothing
    moved, on exactly the builds that most need an explanation.
    """
    env = cache_mod.probe_environment()
    first = PageCache(tmp_path / "cache", env=replace(env, tesseract="5.3.3"))
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    assert first.put(job, fake_outcome(job), DIGEST)

    second = PageCache(tmp_path / "cache", env=replace(env, tesseract="5.3.4"))
    assert second.get(job, DIGEST) is None
    assert second.put(a_job(number=4), fake_outcome(a_job(number=4)), DIGEST)
    assert second.miss_reason() == "tesseract moved from 5.3.3 to 5.3.4", (
        "the build's own stamp answered the question"
    )


def test_the_stamp_is_written_once_per_process(tmp_path):
    """A syscall per page, on a 20,000-page build, for one unchanging fact."""
    cache = PageCache(tmp_path / "cache")
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    assert cache.put(job, fake_outcome(job), DIGEST)
    _stamp_path(cache).write_text("{}", encoding="utf-8")
    assert cache.put(a_job(number=4), fake_outcome(a_job(number=4)), DIGEST)
    assert _stamp_path(cache).read_text(encoding="utf-8") == "{}"


def test_a_cold_cache_can_name_the_component_that_moved(tmp_path):
    """The whole point: "tesseract moved from 5.3.3 to 5.3.4"."""
    cache = PageCache(tmp_path / "cache", env=replace(cache_mod.probe_environment(), tesseract="5.3.3"))
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    assert cache.put(job, fake_outcome(job), DIGEST)

    later = PageCache(tmp_path / "cache", env=replace(cache_mod.probe_environment(), tesseract="5.3.4"))
    assert later.get(job, DIGEST) is None, "a moved version has to miss"
    assert later.miss_reason() == "tesseract moved from 5.3.3 to 5.3.4"


def test_a_moved_source_digest_is_described_rather_than_printed(tmp_path):
    """Sixty-four hex characters at an operator explains nothing."""
    env = cache_mod.probe_environment()
    cache = PageCache(tmp_path / "cache", env=replace(env, source="src:" + "a" * 32))
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    assert cache.put(job, fake_outcome(job), DIGEST)
    later = PageCache(tmp_path / "cache", env=replace(env, source="src:" + "b" * 32))
    assert "source" in later.miss_reason()
    assert "a" * 32 not in later.miss_reason()


def test_nothing_moved_means_nothing_is_claimed(tmp_path):
    """A miss with an unchanged environment was the documents or the job."""
    cache = PageCache(tmp_path / "cache")
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    assert cache.put(job, fake_outcome(job), DIGEST)
    assert PageCache(tmp_path / "cache").miss_reason() == ""


def test_an_unreadable_stamp_is_not_an_error(tmp_path):
    cache = PageCache(tmp_path / "cache")
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    assert cache.put(job, fake_outcome(job), DIGEST)
    _stamp_path(cache).write_bytes(b"\x00 not json at all")
    later = PageCache(tmp_path / "cache")
    assert later.last_env() == {}
    assert later.miss_reason() == ""


def test_the_stamp_can_never_become_part_of_the_key(tmp_path):
    """Structural, not a promise.

    ``key_for`` is a pure function of ``(job, source_sha256, env, salt)`` and
    opens no files, so nothing written into the cache directory can reach it.
    This asserts that directly: the key is the same with the stamp absent,
    present, and holding somebody else's environment. If a future change makes
    the key consult the directory, two of these three go red.
    """
    cache = PageCache(tmp_path / "cache")
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    expected = cache.key(job, DIGEST)

    assert cache.put(job, fake_outcome(job), DIGEST)
    assert cache.key(job, DIGEST) == expected

    _stamp_path(cache).write_text(
        json.dumps({"environment": {"tesseract": "0.0.0"}}), encoding="utf-8"
    )
    assert PageCache(tmp_path / "cache").key(job, DIGEST) == expected

    _stamp_path(cache).unlink()
    assert PageCache(tmp_path / "cache").key(job, DIGEST) == expected


def test_clearing_the_cache_takes_the_stamp_with_it(tmp_path):
    cache = PageCache(tmp_path / "cache")
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    assert cache.put(job, fake_outcome(job), DIGEST)
    cache.clear()
    assert not _stamp_path(cache).exists()
    assert PageCache(tmp_path / "cache").last_env() == {}


def test_the_stamp_is_not_counted_as_a_cached_page(tmp_path):
    cache = PageCache(tmp_path / "cache")
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    assert cache.put(job, fake_outcome(job), DIGEST)
    assert cache.stats().entries == 1


def test_a_disabled_cache_writes_nothing_at_all(tmp_path):
    cache = cache_mod.open_cache(directory=tmp_path / "cache", enabled=False)
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    assert cache.get(job, DIGEST) is None
    assert cache.put(job, fake_outcome(job), DIGEST) is False
    assert not (tmp_path / "cache").exists()


def test_the_environment_variable_turns_it_off(tmp_path, monkeypatch):
    monkeypatch.setenv("STACKROOM_CACHE", "0")
    assert not cache_mod.open_cache(directory=tmp_path / "cache").enabled


# ==========================================================================
# 4. what must never be stored
# ==========================================================================


def test_a_page_with_text_under_a_black_box_is_never_stored(tmp_path):
    from stackroom.model import HiddenText

    cache = PageCache(tmp_path / "cache")
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    outcome = fake_outcome(job)
    outcome.hidden = [HiddenText(Box(0.1, 0.1, 0.2, 0.02), "Gregory Aldana")]
    assert cache.put(job, outcome, DIGEST) is False
    assert cache.get(job, DIGEST) is None


def test_a_page_the_check_could_not_run_on_is_never_stored(tmp_path):
    """"We could not look" is not "there was nothing there", and a cache that
    writes the first down and serves it back turns it into the second."""
    cache = PageCache(tmp_path / "cache")
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    outcome = fake_outcome(job)
    outcome.analysis_failed = True
    assert cache.put(job, outcome, DIGEST) is False


def test_a_page_that_errored_is_never_stored(tmp_path):
    cache = PageCache(tmp_path / "cache")
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    outcome = fake_outcome(job)
    outcome.error = "could not render: pdftoppm timed out after 300s"
    assert cache.put(job, outcome, DIGEST) is False


@pytest.mark.parametrize(
    ("note", "stored"),
    [
        ("recognition failed: tesseract was killed", False),
        ("could not read the text layer: xref broken", False),
        ("pdftoppm timed out after 300s", False),
        ("the page was scanned 90 degrees out of upright", True),
        ("the PDF's own text layer was unusable; read the page from the image instead", True),
    ],
)
def test_a_note_that_might_be_bad_luck_keeps_a_page_out_of_the_cache(tmp_path, note, stored):
    """Tesseract killed by the OOM reaper is not a fact about the document.
    Writing it down would poison that page until somebody cleared the cache."""
    cache = PageCache(tmp_path / "cache")
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    outcome = fake_outcome(job)
    outcome.warnings = [note]
    assert cache.put(job, outcome, DIGEST) is stored


def test_a_page_that_has_already_been_annotated_is_refused(tmp_path):
    """Exemption codes and control numbers are decided across the whole
    document and written back into its pages. A page stored after that carries
    an answer its own job does not determine."""
    cache = PageCache(tmp_path / "cache")
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    outcome = fake_outcome(job)
    outcome.page.exemptions = ["b(6)"]
    assert cache.put(job, outcome, DIGEST) is False

    outcome = fake_outcome(job)
    outcome.page.bates = "ABC000123"
    assert cache.put(job, outcome, DIGEST) is False

    outcome = fake_outcome(job)
    outcome.page.redactions = [Redaction(Box(0, 0, 0.1, 0.1), RedactionKind.VECTOR, ["b(5)"])]
    assert cache.put(job, outcome, DIGEST) is False


def test_a_page_whose_numbers_are_not_numbers_is_refused(tmp_path):
    """A NaN would round-trip through JSON as a value nothing else accepts."""
    cache = PageCache(tmp_path / "cache")
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    outcome = fake_outcome(job)
    outcome.page.redaction_ratio = float("nan")
    assert cache.put(job, outcome, DIGEST) is False


# ==========================================================================
# 5. bounded, and prunable
# ==========================================================================


def _fill(cache: PageCache, tmp_path: Path, count: int, size: int = 4096) -> list[PageJob]:
    jobs = []
    for n in range(count):
        job = a_job(number=n + 1, media_dir=str(tmp_path / "site" / "media" / "release"))
        assert cache.put(job, fake_outcome(job, payload=bytes([n % 251]) * size), DIGEST)
        jobs.append(job)
    return jobs


def test_the_cache_stays_inside_its_limit(tmp_path):
    cache = PageCache(tmp_path / "cache", max_bytes=20_000)
    _fill(cache, tmp_path, 10, size=4096)
    assert cache.stats().bytes > 20_000, "the point of the test is that it went over"
    cache.prune()
    assert cache.stats().bytes <= 20_000


def _entry_for(cache: PageCache, job: PageJob) -> Path:
    return cache._entry_path(cache.key(job, DIGEST))


def test_reading_a_page_marks_it_as_recently_used(tmp_path):
    """Least recently *used*, not least recently written: a page rebuilt every
    hour and a page nobody has touched since March are not the same page."""
    cache = PageCache(tmp_path / "cache")
    job = _stored(tmp_path, cache)
    entry = _entry_for(cache, job)
    long_ago = time.time() - 90 * 86400
    os.utime(entry, (long_ago, long_ago))

    assert cache.get(job, DIGEST) is not None
    assert entry.stat().st_mtime > long_ago + 86400


def test_eviction_keeps_the_pages_that_were_used_most_recently(tmp_path):
    cache = PageCache(tmp_path / "cache", max_bytes=1_000_000)
    jobs = _fill(cache, tmp_path, 6, size=4096)

    # A week of history, written where eviction reads it. (Real accesses are
    # days apart; six of them inside one test are not, and a filesystem's mtime
    # is quantised to a timer tick.)
    now = time.time()
    for days, job in zip((7, 6, 5, 4, 3, 2), jobs, strict=True):
        stamp = now - days * 86400
        os.utime(_entry_for(cache, job), (stamp, stamp))

    # ...and this morning the operator rebuilt the two oldest.
    for job in (jobs[0], jobs[1]):
        assert cache.get(job, DIGEST) is not None

    # Room for three and a half of them: measured rather than guessed, because
    # a budget that lands within a few bytes of a whole number of entries makes
    # this test a coin toss rather than a check.
    per_entry = cache.stats().bytes / len(jobs)
    cache.prune(max_bytes=int(per_entry * 3.5))

    survivors = {j.number for j in jobs if cache.get(j, DIGEST) is not None}
    assert survivors == {1, 2, 6}, "the three most recently used, and only those"


def test_pruning_removes_the_images_of_evicted_pages_too(tmp_path):
    cache = PageCache(tmp_path / "cache", max_bytes=1_000_000)
    _fill(cache, tmp_path, 5, size=8192)
    before = len(blobs(cache))
    # Blobs are only swept once they are old enough that no build could be
    # about to reference them; pretend an hour has passed.
    for blob in blobs(cache):
        old = blob.stat().st_mtime - cache_mod.BLOB_GRACE_SECONDS - 60
        os.utime(blob, (old, old))
    cache.prune(max_bytes=10_000)
    after = len(blobs(cache))
    assert after < before


def test_a_blob_written_moments_ago_survives_a_sweep(tmp_path):
    """Another build may be between writing its blobs and writing the entry
    that claims them. Sweeping those would only cost that build a miss, but
    there is no reason to."""
    cache = PageCache(tmp_path / "cache", max_bytes=1_000_000)
    _fill(cache, tmp_path, 3)
    orphan = cache.root / "blobs" / "ff" / ("f" * 64)
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"just written by somebody else")
    cache.prune()
    assert orphan.exists()


def test_clear_removes_everything_including_layouts_we_do_not_know(tmp_path):
    cache = PageCache(tmp_path / "cache")
    _fill(cache, tmp_path, 3)
    stranger = cache.root.parent / "v99" / "entries" / "ab" / "whatever.json.gz"
    stranger.parent.mkdir(parents=True)
    stranger.write_bytes(b"from a future version")

    report = cache.clear()
    assert report.entries_removed == 3
    assert not stranger.exists()
    assert cache.stats().entries == 0
    assert cache.root.exists(), "still usable straight afterwards"
    assert cache.put(a_job(media_dir=str(tmp_path / "site" / "media" / "release")),
                     fake_outcome(a_job(media_dir=str(tmp_path / "site" / "media" / "release"))),
                     DIGEST)


def test_cache_path_prints_the_directory_cache_dir_takes(tmp_path):
    """The two commands that print a path must print the same one.

    ``stackroom cache path`` is documented "for scripts", and a script does one
    thing with it: hands it back as ``--cache-dir``. It used to print the entry
    directory ``<base>/pages/<layout>``, which is the one path here that cannot
    be handed back - doing so opens a second cache nested inside the first,
    silently, and every page misses. ``--entries`` still reaches that path, by
    name.
    """
    from typer.testing import CliRunner

    from stackroom import cli as cli_mod

    base = tmp_path / "chosen"
    runner = CliRunner()
    printed = runner.invoke(cli_mod.app, ["cache", "path", "--cache-dir", str(base)])
    assert printed.exit_code == 0, printed.output
    assert printed.output.strip() == str(base)

    entries = runner.invoke(
        cli_mod.app, ["cache", "path", "--entries", "--cache-dir", str(base)]
    )
    assert entries.exit_code == 0, entries.output
    assert entries.output.strip() == str(cache_mod.cache_root(base))
    assert entries.output.strip() != printed.output.strip()

    # And it round-trips: opening a cache at what it printed is the same cache.
    assert PageCache(printed.output.strip()).root == cache_mod.cache_root(base)

    # `cache show` names both, labelled, so which is which is not a guess.
    # Whitespace-stripped because rich wraps a long path across two lines.
    shown = runner.invoke(cli_mod.app, ["cache", "show", "--cache-dir", str(base)])
    assert shown.exit_code == 0, shown.output
    flat = "".join(shown.output.split())
    assert f"Where{base}" in flat
    assert f"Entriesin{cache_mod.cache_root(base)}" in flat


def test_a_zero_size_limit_makes_the_cache_a_no_op(tmp_path):
    cache = PageCache(tmp_path / "cache", max_bytes=0)
    assert not cache.enabled
    assert cache.get(a_job()) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2GB", 2_000_000_000),
        ("1.5GiB", int(1.5 * 1024**3)),
        ("500 MB", 500_000_000),
        ("512M", 512_000_000),
        ("4096", 4096),
        ("0", 0),
        ("", None),
        (None, None),
    ],
)
def test_sizes_are_read_the_way_people_write_them(text, expected):
    assert cache_mod.parse_size(text) == expected


def test_an_unreadable_size_says_so():
    with pytest.raises(ValueError, match="2GB"):
        cache_mod.parse_size("two gigabytes")


def test_the_cache_directory_follows_the_platform_conventions(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert cache_mod.default_cache_dir() == tmp_path / "xdg" / "stackroom"
    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.delenv("STACKROOM_CACHE_DIR", raising=False)
    assert cache_mod.default_cache_dir().name == "stackroom" or os.name == "nt"
    monkeypatch.setenv("STACKROOM_CACHE_DIR", str(tmp_path / "chosen"))
    assert cache_mod.cache_root().parent.parent == tmp_path / "chosen"
    assert cache_mod.cache_root(tmp_path / "explicit").parent.parent == tmp_path / "explicit"


def test_the_cache_directory_is_tagged_so_backups_skip_it(tmp_path):
    PageCache(tmp_path / "cache")
    tag = (tmp_path / "cache" / "CACHEDIR.TAG").read_bytes()
    assert tag.startswith(b"Signature: 8a477f597d28d172789f06886806bc55")
    readme = (tmp_path / "cache" / "README.txt").read_text()
    assert "delete" in readme.lower() and "black box" in readme


# ==========================================================================
# 6. every way it can go wrong
# ==========================================================================


def _one_entry(cache: PageCache) -> Path:
    return next((cache.root / "entries").rglob("*.json.gz"))


def _stored(tmp_path: Path, cache: PageCache) -> PageJob:
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    assert cache.put(job, fake_outcome(job), DIGEST)
    return job


def test_a_truncated_entry_is_a_miss_and_is_thrown_away(tmp_path):
    cache = PageCache(tmp_path / "cache")
    job = _stored(tmp_path, cache)
    entry = _one_entry(cache)
    entry.write_bytes(entry.read_bytes()[:20])
    assert cache.get(job, DIGEST) is None
    assert not entry.exists(), "a corrupt entry is removed, not left to fail again"


def test_an_entry_full_of_noise_is_a_miss(tmp_path):
    cache = PageCache(tmp_path / "cache")
    job = _stored(tmp_path, cache)
    _one_entry(cache).write_bytes(os.urandom(4096))
    assert cache.get(job, DIGEST) is None


def test_an_entry_that_is_not_the_entry_it_claims_to_be_is_a_miss(tmp_path):
    """Belt and braces against a half-written file that happens to gunzip: the
    key is inside the entry as well as being its name."""
    cache = PageCache(tmp_path / "cache")
    job = _stored(tmp_path, cache)
    entry = _one_entry(cache)
    body = json.loads(gzip.decompress(entry.read_bytes()))
    body["key"] = "0" * 64
    entry.write_bytes(gzip.compress(json.dumps(body).encode()))
    assert cache.get(job, DIGEST) is None


def test_an_entry_from_a_future_format_is_a_miss(tmp_path):
    cache = PageCache(tmp_path / "cache")
    job = _stored(tmp_path, cache)
    entry = _one_entry(cache)
    body = json.loads(gzip.decompress(entry.read_bytes()))
    body["format"] = cache_mod.FORMAT + 1
    entry.write_bytes(gzip.compress(json.dumps(body).encode()))
    assert cache.get(job, DIGEST) is None


def test_a_missing_image_is_a_miss_and_leaves_no_half_written_page(tmp_path):
    cache = PageCache(tmp_path / "cache")
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    outcome = fake_outcome(job)
    outcome.page.images.append(ImageVariant(f"{job.media_prefix}/p0003@thumb.webp", "webp", 1, 1, 3))
    (Path(job.media_dir) / "p0003@thumb.webp").write_bytes(b"abc")
    assert cache.put(job, outcome, DIGEST)

    shutil.rmtree(tmp_path / "site")
    blobs(cache)[0].unlink()
    assert cache.get(job, DIGEST) is None
    left = list(Path(job.media_dir).glob("*")) if Path(job.media_dir).exists() else []
    assert left == [], "a partial restore is cleaned up rather than published"


def test_an_image_whose_bytes_have_changed_is_caught_and_thrown_away(tmp_path):
    """The blob is named by its own digest, so this is checkable on every hit,
    and it is what makes "byte-identical, cached or not" a checked claim."""
    cache = PageCache(tmp_path / "cache")
    job = _stored(tmp_path, cache)
    blob = blobs(cache)[0]
    same_length = bytes(len(blob.read_bytes()))
    os.chmod(blob, 0o600)
    blob.write_bytes(same_length)
    assert cache.get(job, DIGEST) is None
    assert not blob.exists()


def test_an_entry_that_names_an_image_outside_the_media_folder_is_refused(tmp_path):
    """Nothing writes an entry like this. It is here because the restore step
    writes files with names taken out of a file, and that is worth a check."""
    cache = PageCache(tmp_path / "cache")
    job = _stored(tmp_path, cache)
    entry = _one_entry(cache)
    body = json.loads(gzip.decompress(entry.read_bytes()))
    body["blobs"][0][0] = "../../../../etc/stackroom-was-here"
    entry.write_bytes(gzip.compress(json.dumps(body).encode()))
    assert cache.get(job, DIGEST) is None
    assert not Path("/etc/stackroom-was-here").exists()


def test_a_read_only_cache_directory_still_serves_what_is_in_it(tmp_path, monkeypatch):
    """The realistic shape: a shared read-only mount, or a cache somebody else
    owns. Reads work, writes stop, the build carries on."""
    cache = PageCache(tmp_path / "cache")
    job = _stored(tmp_path, cache)
    shutil.rmtree(tmp_path / "site")

    def refuse(path, payload):
        raise PermissionError(errno.EACCES, "Permission denied", str(path))

    monkeypatch.setattr(cache_mod, "_atomic_write", refuse)
    other = a_job(number=9, media_dir=str(tmp_path / "site" / "media" / "release"))
    assert cache.put(other, fake_outcome(other), DIGEST) is False
    assert any("not writable" in w for w in cache.warnings)
    assert cache.get(job, DIGEST) is not None, "reads are unaffected"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_genuinely_read_only_directory_is_not_a_crash(tmp_path):
    cache = PageCache(tmp_path / "cache")
    job = _stored(tmp_path, cache)
    for directory in sorted((tmp_path / "cache").rglob("*"), reverse=True):
        if directory.is_dir():
            os.chmod(directory, 0o555)
    os.chmod(tmp_path / "cache", 0o555)
    try:
        other = a_job(number=9, media_dir=str(tmp_path / "site" / "media" / "release"))
        assert cache.put(other, fake_outcome(other), DIGEST) is False
        assert cache.get(job, DIGEST) is not None
    finally:
        for directory in sorted((tmp_path / "cache").rglob("*")):
            if directory.is_dir():
                os.chmod(directory, 0o755)
        os.chmod(tmp_path / "cache", 0o755)


def test_a_full_disk_stops_writing_and_nothing_else(tmp_path, monkeypatch):
    cache = PageCache(tmp_path / "cache")
    job = _stored(tmp_path, cache)

    def no_space(path, payload):
        raise OSError(errno.ENOSPC, "No space left on device", str(path))

    monkeypatch.setattr(cache_mod, "_write_bytes", no_space)
    monkeypatch.setattr(cache_mod, "_atomic_write", no_space)
    other = a_job(number=9, media_dir=str(tmp_path / "site" / "media" / "release"))
    assert cache.put(other, fake_outcome(other), DIGEST) is False
    assert any("full" in w for w in cache.warnings)
    assert cache.get(job, DIGEST) is not None


def test_a_cache_directory_that_cannot_be_made_disables_the_cache(tmp_path):
    blocker = tmp_path / "in-the-way"
    blocker.write_text("not a directory")
    cache = cache_mod.open_cache(directory=blocker)
    assert not cache.enabled
    assert cache.disabled_reason
    assert cache.get(a_job()) is None


def test_a_source_file_that_cannot_be_hashed_is_simply_a_miss(tmp_path):
    cache = PageCache(tmp_path / "cache")
    assert cache.get(a_job(pdf=str(tmp_path / "gone.pdf"))) is None


# --------------------------------------------------------------------------
# concurrency
# --------------------------------------------------------------------------


def test_two_builds_sharing_one_cache_do_not_corrupt_it(tmp_path):
    """No lock. Every write is a rename over a complete file, and every failure
    to read one is a miss, so the worst a race can do is make somebody redo a
    page they were going to redo anyway."""
    root = tmp_path / "cache"
    problems: list[BaseException] = []
    barrier = threading.Barrier(4)

    def worker(n: int) -> None:
        try:
            cache = PageCache(root)  # a separate handle, as a separate process has
            barrier.wait(timeout=30)
            for round_ in range(6):
                shared = a_job(number=1, media_dir=str(tmp_path / f"site{n}" / "media" / "release"))
                mine = a_job(
                    number=100 + n, media_dir=str(tmp_path / f"site{n}" / "media" / "release")
                )
                cache.put(shared, fake_outcome(shared, payload=b"the same bytes"), DIGEST)
                cache.put(mine, fake_outcome(mine, payload=f"mine {n}".encode()), DIGEST)
                got = cache.get(mine, DIGEST)
                assert got is not None, "a page this thread just wrote"
                assert got.number == 100 + n
                if round_ == 3:
                    cache.prune()
        except BaseException as exc:  # reported on the main thread, where it fails the test
            problems.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not problems, problems

    # And whatever survived is readable and correct.
    cache = PageCache(root)
    for n in range(4):
        job = a_job(number=100 + n, media_dir=str(tmp_path / "check" / "media" / "release"))
        got = cache.get(job, DIGEST)
        if got is not None:
            assert got.number == 100 + n
            assert (Path(job.media_dir) / f"p{100 + n:04d}@600.webp").read_bytes() == f"mine {n}".encode()


def test_a_second_process_writing_the_same_key_is_harmless(tmp_path):
    """The real thing, in a real second process."""
    root = tmp_path / "cache"
    job = a_job(media_dir=str(tmp_path / "site" / "media" / "release"))
    cache = PageCache(root)
    cache.put(job, fake_outcome(job), DIGEST)

    code = f"""
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from test_cache import a_job, fake_outcome, DIGEST
from stackroom.cache import PageCache
cache = PageCache({str(root)!r})
job = a_job(media_dir={str(tmp_path / 'site2' / 'media' / 'release')!r})
for _ in range(10):
    cache.put(job, fake_outcome(job), DIGEST)
print("ok")
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    assert cache.get(job, DIGEST) is not None


# ==========================================================================
# 7. watch mode
# ==========================================================================


def test_the_watcher_sees_a_new_file_a_changed_one_and_a_missing_one(tmp_path):
    root = tmp_path / "papers"
    root.mkdir()
    (root / "one.pdf").write_bytes(b"a")
    watcher = cache_mod.Watcher(root, interval=0.01, settle=0.0)

    (root / "two.pdf").write_bytes(b"b")
    assert watcher.poll().added == (str(root / "two.pdf"),)

    (root / "one.pdf").write_bytes(b"aaaaaaaa")
    assert watcher.poll().modified == (str(root / "one.pdf"),)

    (root / "two.pdf").unlink()
    assert watcher.poll().removed == (str(root / "two.pdf"),)

    assert not watcher.poll()


def test_the_watcher_ignores_the_noise_it_should(tmp_path):
    root = tmp_path / "papers"
    (root / "sub").mkdir(parents=True)
    (root / "keep.pdf").write_bytes(b"a")
    watcher = cache_mod.Watcher(root, ignore=[root / "site"], interval=0.01, settle=0.0)

    (root / ".hidden.pdf").write_bytes(b"x")
    (root / "draft.pdf.part").write_bytes(b"x")
    (root / "notes.pdf~").write_bytes(b"x")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.pyc").write_bytes(b"x")
    (root / "site").mkdir()
    (root / "site" / "index.html").write_bytes(b"<html>")
    assert not watcher.poll(), "none of that is a document changing"

    (root / "sub" / "real.pdf").write_bytes(b"a document")
    assert watcher.poll().added == (str(root / "sub" / "real.pdf"),)


def test_the_watcher_also_watches_the_files_it_is_told_to(tmp_path):
    """``stackroom.toml`` and ``about.md`` decide what the site says; they are
    usually beside the documents and sometimes are not."""
    root = tmp_path / "papers"
    root.mkdir()
    config = tmp_path / "elsewhere" / "stackroom.toml"
    config.parent.mkdir()
    config.write_text('title = "One"')
    watcher = cache_mod.Watcher(root, extra=[config], interval=0.01, settle=0.0)
    config.write_text('title = "Two"')
    assert watcher.poll().modified == (str(config),)


def test_a_file_still_being_written_does_not_trigger_a_build_until_it_stops(tmp_path):
    """A 300 MB PDF appears the moment its first byte lands. Building then reads
    half a file: poppler reports a page count that is about to be wrong and the
    last page renders as a stripe."""
    root = tmp_path / "papers"
    root.mkdir()
    watcher = cache_mod.Watcher(root, interval=1.0, settle=2.0)

    target = root / "big.pdf"
    script = [
        lambda: target.write_bytes(b"x" * 100),
        lambda: target.write_bytes(b"x" * 5_000),
        lambda: target.write_bytes(b"x" * 40_000),
        lambda: None,
        lambda: None,
        lambda: None,
    ]
    ticks: list[int] = []

    def fake_sleep(_seconds: float) -> None:
        step = len(ticks)
        ticks.append(step)
        if step < len(script):
            script[step]()

    change = watcher.wait(sleep=fake_sleep)
    assert change is not None
    assert change.added == (str(target),)
    assert target.stat().st_size == 40_000, "the build sees the finished file"
    assert len(ticks) >= 5, "it waited for the writing to stop"


def test_the_watcher_can_be_told_to_stop(tmp_path):
    root = tmp_path / "papers"
    root.mkdir()
    watcher = cache_mod.Watcher(root, interval=0.01, settle=0.0)
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 3

    assert watcher.wait(stop=stop) is None


def test_watch_builds_once_and_then_once_per_change(tmp_path):
    root = tmp_path / "papers"
    root.mkdir()
    (root / "one.pdf").write_bytes(b"a")
    seen: list[object] = []
    lines: list[str] = []

    def build(change):
        seen.append(change)
        if len(seen) == 1:
            (root / "two.pdf").write_bytes(b"b")  # the change the next cycle sees
        return f"build {len(seen)}"

    runs = cache_mod.watch(
        root, build, interval=0.01, settle=0.0, emit=lines.append, cycles=2
    )
    assert runs == 2
    assert seen[0] is None, "the first build is not triggered by a change"
    assert seen[1].added == (str(root / "two.pdf"),)
    assert any("build 2" in line for line in lines)


def test_a_build_that_fails_does_not_end_the_watch(tmp_path):
    """The next thing the operator does is fix the file that broke it. Making
    them restart the watcher as well would be a small unkindness at the exact
    moment they are least in the mood for one."""
    root = tmp_path / "papers"
    root.mkdir()
    lines: list[str] = []
    attempts = {"n": 0}

    def build(_change):
        attempts["n"] += 1
        if attempts["n"] == 1:
            (root / "two.pdf").write_bytes(b"b")
            raise RuntimeError("this collection would publish a failed redaction")
        return "recovered"

    runs = cache_mod.watch(root, build, interval=0.01, settle=0.0, emit=lines.append, cycles=2)
    assert runs == 2
    assert any("failed redaction" in line for line in lines)
    assert any("recovered" in line for line in lines)


def test_a_change_that_undoes_itself_is_not_a_change(tmp_path):
    root = tmp_path / "papers"
    root.mkdir()
    target = root / "one.pdf"
    target.write_bytes(b"aaaa")
    stamp = target.stat()
    watcher = cache_mod.Watcher(root, interval=0.5, settle=1.0)

    steps = [
        lambda: target.write_bytes(b"bbbbbbbb"),
        lambda: (target.write_bytes(b"aaaa"), os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))),
        lambda: target.write_bytes(b"cccc"),
        lambda: None,
        lambda: None,
        lambda: None,
    ]
    ticks: list[int] = []

    def fake_sleep(_seconds: float) -> None:
        step = len(ticks)
        ticks.append(step)
        if step < len(steps):
            steps[step]()

    change = watcher.wait(sleep=fake_sleep)
    assert change is not None and change.modified == (str(target),)


def test_a_change_describes_itself_for_a_person():
    change = cache_mod.Change(
        added=("/a/one.pdf",), modified=("/a/two.pdf", "/a/three.pdf"), removed=("/a/four.pdf",)
    )
    text = change.describe(limit=1)
    assert "one.pdf new" in text
    assert "two.pdf and 1 more changed" in text
    assert "four.pdf gone" in text
    assert change.count == 4


# ==========================================================================
# 8. end to end: the claim the whole thing rests on
# ==========================================================================

CLEAR_Y = 200.0
"""A band of the page that ``synth.born_digital_pdf`` leaves empty, so a box
put here covers nothing it was not asked to cover."""


@pytest.fixture(scope="module")
def papers(tmp_path_factory) -> Path:
    """A small collection with one of everything the cache has to survive.

    Two documents: a born-digital memo with a properly redacted box and a
    control number, and a page image with no text layer, which is the one that
    has to go through Tesseract.
    """
    root = tmp_path_factory.mktemp("cache-e2e") / "papers"
    root.mkdir()
    synth.born_digital_pdf(
        root / "memo.pdf",
        pages=2,
        bates_prefix="ACME",
        redactions={2: [synth.RedactionSpec(90, CLEAR_Y, 200, 14, code="(b)(6)")]},
    )
    synth.image_only_pdf(root / "scan.pdf", [synth.typed_page(lines=10)])
    (root / "about.md").write_text(
        "# About\n\nReleased in March under a request made the previous year.\n",
        encoding="utf-8",
    )
    return root


@needs_poppler
def test_a_warm_build_is_byte_identical_to_a_cold_one(papers, tmp_path):
    """The claim, checked: same input bytes, same output bytes, cached or not.

    Everything - the HTML, the JSON word boxes, the manifest, the service
    worker's inventory and every encoded image - compared file by file. The
    build stamp is pinned because it reads the clock and has nothing to do with
    the cache; it is the only thing in a Stackroom site that is not a function
    of the input.
    """
    cache = PageCache(tmp_path / "cache")

    cold_dir = tmp_path / "cold"
    _, cold = build_site_from(papers, cold_dir, cache)
    assert cache.hits == 0
    assert cache.stores == len(cold) >= 3

    cache.reset()
    warm_dir = tmp_path / "warm"
    build_site_from(papers, warm_dir, cache)
    assert cache.hits == len(cold), "every page came from the cache"
    assert cache.misses == 0

    cold_tree, warm_tree = tree(cold_dir), tree(warm_dir)
    assert sorted(cold_tree) == sorted(warm_tree)
    differing = [name for name in cold_tree if cold_tree[name] != warm_tree[name]]
    assert differing == []
    assert len([n for n in cold_tree if n.endswith(".webp")]) >= 4, "images were compared"


@needs_poppler
def test_the_pages_themselves_come_back_identical(papers, tmp_path):
    """The tree comparison covers this, but a failure there is easier to read
    when this has already localised it to the model."""
    cache = PageCache(tmp_path / "cache")
    cold_collection, _ = run_build(papers, tmp_path / "cold", cache)
    cache.reset()
    warm_collection, _ = run_build(papers, tmp_path / "warm", cache)
    assert cache.hits and not cache.misses

    for cold_doc, warm_doc in zip(
        cold_collection.documents, warm_collection.documents, strict=True
    ):
        assert cold_doc.pages == warm_doc.pages
        assert cold_doc.bates_prefix == warm_doc.bates_prefix
        assert cold_doc.bates_gaps == warm_doc.bates_gaps
    assert cold_collection.stats == warm_collection.stats


@needs_poppler
def test_the_document_level_passes_still_run_over_restored_pages(papers, tmp_path):
    """Exemption codes and control numbers are not cached: they are decided by
    looking at the whole document, and they are cheap. What is cached is the
    page as ``process_page`` left it, before any of that."""
    cache = PageCache(tmp_path / "cache")
    run_build(papers, tmp_path / "cold", cache)
    cache.reset()
    warm, _ = run_build(papers, tmp_path / "warm", cache)

    memo = warm.document("memo")
    assert memo is not None
    assert memo.bates_prefix == "ACME"
    assert any(page.bates for page in memo.pages)
    assert any(page.exemptions for page in memo.pages)

    entry = json.loads(gzip.decompress(_one_entry(cache).read_bytes()))
    stored = entry["outcome"]["page"]
    assert stored["exemptions"] == [] and stored["bates"] is None


@needs_poppler
def test_only_the_document_that_changed_is_read_again(papers, tmp_path):
    """The whole point, in one assertion: fixing one document must not cost the
    others. It is the *file's* digest, so every page of the file that changed is
    re-read - see ``docs/CACHING.md`` on why a per-page digest was rejected."""
    root = tmp_path / "papers"
    shutil.copytree(papers, root)
    cache = PageCache(tmp_path / "cache")
    _, first = run_build(root, tmp_path / "one", cache)
    assert cache.stores == len(first)

    synth.born_digital_pdf(root / "memo.pdf", pages=2, bates_prefix="ACME", title="Corrected")
    cache.reset()
    _, second = run_build(root, tmp_path / "two", cache)

    assert cache.misses == 2, "the two pages of the document that changed"
    assert cache.hits == len(second) - 2, "everything else"


@needs_poppler
def test_touching_a_file_without_changing_it_costs_nothing(papers, tmp_path):
    """mtime is not the key. A collection copied off a stick, restored from a
    backup, or checked out again has new timestamps and the same bytes."""
    root = tmp_path / "papers"
    shutil.copytree(papers, root)
    cache = PageCache(tmp_path / "cache")
    _, first = run_build(root, tmp_path / "one", cache)

    for pdf in root.glob("*.pdf"):
        os.utime(pdf, (0, 0))
    cache.reset()
    run_build(root, tmp_path / "two", cache)
    assert cache.hits == len(first)
    assert cache.misses == 0


# --------------------------------------------------------------------------
# the secret
# --------------------------------------------------------------------------

SECRET = "MERCURYSEVENTEENBLUEBIRD"


@pytest.fixture(scope="module")
def leaking(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("cache-leak") / "papers"
    root.mkdir()
    synth.born_digital_pdf(
        root / "release.pdf",
        pages=2,
        redactions={
            1: [synth.RedactionSpec(90, CLEAR_Y, 200, 14, hidden_text=SECRET, code="(b)(6)")]
        },
    )
    return root


@needs_poppler
def test_a_planted_secret_is_nowhere_in_the_cache_directory(leaking, tmp_path):
    """Guarantee 4 says text a redaction failed to remove is never published.
    ``model.HiddenText`` says it is never written to any file on disk. A cache
    is a file on disk, so this greps the whole of it - compressed entries
    decompressed first - for the text, for its shape, and for every substring of
    it long enough to be recognisable."""
    cache = PageCache(tmp_path / "cache")
    _, outcomes = run_build(leaking, tmp_path / "out", cache)

    found = [o for o in outcomes if o.hidden]
    assert found, "the fixture is supposed to leak; the cache is not why it does not"
    assert found[0].hidden[0].text == SECRET
    shape = found[0].hidden[0].redacted_repr()

    haystack = b""
    for path in sorted(cache.root.rglob("*")):
        if not path.is_file():
            continue
        raw = path.read_bytes()
        haystack += raw
        if path.name.endswith(".json.gz"):
            haystack += gzip.decompress(raw)
    for needle in (SECRET, SECRET[:10], SECRET.lower(), shape, "#" * 8):
        assert needle.encode() not in haystack, needle
    assert haystack, "the other pages of that document are cached; only this one is not"


@needs_poppler
def test_a_page_that_leaked_is_read_from_the_file_on_every_build(leaking, tmp_path):
    """Not cached, and deliberately so: the report the operator sees on the
    second build has to be the report they saw on the first, and it cannot be
    if the text that fills it was never written down."""
    cache = PageCache(tmp_path / "cache")
    _, first = run_build(leaking, tmp_path / "one", cache)
    cache.reset()
    _, second = run_build(leaking, tmp_path / "two", cache)

    leak_first = sorted((o.doc_id, o.number, o.hidden[0].text) for o in first if o.hidden)
    leak_second = sorted((o.doc_id, o.number, o.hidden[0].text) for o in second if o.hidden)
    assert leak_first == leak_second == [("release", 1, SECRET)]
    assert cache.misses == 1, "the leaking page, and only it, was read again"
    assert cache.hits == len(second) - 1


@needs_poppler
def test_the_cache_reports_what_it_did(papers, tmp_path):
    cache = PageCache(tmp_path / "cache")
    run_build(papers, tmp_path / "cold", cache)
    cache.reset()
    run_build(papers, tmp_path / "warm", cache)
    line = cache.summary()
    assert "came from the cache" in line
    assert cache.saved_seconds > 0
    stats = cache.stats()
    assert stats.entries >= 3 and stats.blobs >= 3 and stats.bytes > 0
    assert stats.root == cache.root


def test_a_file_that_changed_while_it_was_being_read_is_not_remembered(tmp_path):
    """The pipeline hashes a file in ``discover`` and reads it again in the
    workers. A file replaced in between makes that build wrong on its own
    account; what the cache can do is refuse to keep the mistake."""
    source = tmp_path / "release.pdf"
    source.write_bytes(b"the original bytes")
    cache = PageCache(tmp_path / "cache")
    cache.note_digests({str(source): DIGEST})

    job = a_job(pdf=str(source), media_dir=str(tmp_path / "site" / "media" / "release"))
    source.write_bytes(b"different bytes entirely, mid-build")
    assert cache.put(job, fake_outcome(job)) is False
    assert any("changed while it was being read" in w for w in cache.warnings)
    assert cache.get(job, DIGEST) is None
