"""Deterministic HTML evidence-reference extraction."""

from dataclasses import FrozenInstanceError

import pytest
from heatseeker_source_registry.references import (
    REFERENCE_EXTRACTOR_VERSION,
    ReferenceCandidate,
    extract_references,
)

BASE = "https://source.example/research/index.html"


def test_anchor_classification_normalisation_context_and_repeated_occurrences():
    raw = b"""
    <html><head><base href="/catalog/"></head><body>
      <h2>Capability evidence</h2>
      <a href="../about/?z=2&amp;a=1#team" title="Company page">About us</a>
      <a href="docs/capability.PDF#page=2" type="application/pdf">
        Capability statement
      </a>
      <a href="/download?id=42" download>Download dataset</a>
      <a href="/same.pdf">First occurrence</a>
      <a href="/same.pdf">Second occurrence</a>
    </body></html>
    """

    references = extract_references(BASE, raw)
    assert REFERENCE_EXTRACTOR_VERSION == "references/0.1"
    assert [reference.kind for reference in references] == [
        "navigation",
        "document",
        "document",
        "document",
        "document",
    ]

    navigation = references[0]
    assert navigation.url == "https://source.example/about/?z=2&a=1#team"
    assert navigation.normalised_url == "https://source.example/about?a=1&z=2"
    assert navigation.expected_content == "html"
    assert navigation.rule == "anchor-navigation"
    assert navigation.nearby_heading == "Capability evidence"
    assert navigation.title_text == "Company page"

    typed_pdf = references[1]
    assert typed_pdf.url == "https://source.example/catalog/docs/capability.PDF#page=2"
    assert typed_pdf.normalised_url == "https://source.example/catalog/docs/capability.PDF"
    assert typed_pdf.declared_type == "application/pdf"
    assert typed_pdf.anchor_text == "Capability statement"

    assert references[2].normalised_url == "https://source.example/download?id=42"
    repeated = references[3:]
    assert [reference.normalised_url for reference in repeated] == [
        "https://source.example/same.pdf",
        "https://source.example/same.pdf",
    ]
    assert [reference.anchor_text for reference in repeated] == [
        "First occurrence",
        "Second occurrence",
    ]
    assert repeated[0].ordinal != repeated[1].ordinal

    with pytest.raises(FrozenInstanceError):
        navigation.kind = "document"  # type: ignore[misc]


def test_images_choose_best_picture_srcset_and_keep_figure_context():
    raw = b"""
    <html><body>
      <h1>Rail bridge project</h1>
      <figure>
        <picture>
          <source type="image/webp"
                  srcset="/images/bridge-800.webp 800w, /images/bridge-1600.webp 1600w">
          <img src="/images/fallback.jpg" data-src="/images/lazy.jpg"
               srcset="/images/bridge-640.jpg 640w, /images/bridge-1200.jpg 1200w"
               alt="Scaffold around the bridge" title="Project photograph">
        </picture>
        <figcaption>Temporary access during the eastern span repair.</figcaption>
      </figure>
      <img src="/images/placeholder.gif" data-src="/images/yard.jpg" alt="Company yard">
    </body></html>
    """

    images = [reference for reference in extract_references(BASE, raw) if reference.kind == "image"]
    assert len(images) == 2

    project = images[0]
    assert project.normalised_url == "https://source.example/images/bridge-1600.webp"
    assert project.rule == "picture-srcset"
    assert project.source_attribute == "srcset"
    assert project.srcset_descriptor == "1600w"
    assert project.declared_type == "image/webp"
    assert project.alt_text == "Scaffold around the bridge"
    assert project.title_text == "Project photograph"
    assert project.caption == "Temporary access during the eastern span repair."
    assert project.nearby_heading == "Rail bridge project"

    lazy = images[1]
    assert lazy.normalised_url == "https://source.example/images/yard.jpg"
    assert lazy.rule == "img-data-src"
    assert lazy.raw_url == "/images/yard.jpg"


def test_embedded_documents_and_social_images_are_extracted_but_html_iframes_are_not():
    raw = b"""
    <html><head>
      <meta property="og:image" content="https://cdn.example/project.jpg">
      <meta property="og:image:alt" content="Project hero image">
      <meta name="twitter:image" content="/social/card.png">
      <meta name="twitter:image:alt" content="Social card">
    </head><body>
      <h3>Downloads</h3>
      <object data="/download?id=capability.pdf" type="application/pdf" title="Capability"></object>
      <embed src="/sheets/register.xlsx">
      <iframe src="/viewer?file=tender.pdf" title="Tender"></iframe>
      <iframe src="/ordinary/page.html"></iframe>
      <embed src="/media/movie.mp4" type="video/mp4">
    </body></html>
    """

    references = extract_references(BASE, raw)
    assert [(reference.kind, reference.rule) for reference in references] == [
        ("image", "meta-og-image"),
        ("image", "meta-twitter-image"),
        ("document", "object-data"),
        ("document", "embed-src"),
        ("document", "iframe-document"),
    ]
    assert references[0].normalised_url == "https://cdn.example/project.jpg"
    assert references[0].alt_text == "Project hero image"
    assert references[1].alt_text == "Social card"
    assert references[2].declared_type == "application/pdf"
    assert references[2].nearby_heading == "Downloads"
    assert references[4].normalised_url == "https://source.example/viewer?file=tender.pdf"


def test_unsafe_urls_are_rejected_external_urls_returned_and_valid_base_href_applies():
    raw = b"""
    <html><head><base href="https://assets.example/company/"></head><body>
      <a href="docs/profile.pdf">External document</a>
      <a href="javascript:alert(1)">JS</a>
      <a href="data:text/plain,secret">Data</a>
      <a href="blob:https://source.example/id">Blob</a>
      <a href="mailto:test@example.com">Mail</a>
      <a href="ftp://files.example/report.pdf">FTP</a>
      <a href="https://user:password@private.example/report.pdf">Credentials</a>
      <a href="#local-fragment">Fragment</a>
      <img src="javascript:alert(1)">
      <img srcset="data:image/png;base64,AAAA 3x, safe-1.jpg 1x, safe-2.jpg 2x">
    </body></html>
    """

    references = extract_references(BASE, raw)
    assert [(reference.kind, reference.normalised_url) for reference in references] == [
        ("document", "https://assets.example/company/docs/profile.pdf"),
        ("image", "https://assets.example/company/safe-2.jpg"),
    ]
    assert references[1].srcset_descriptor == "2x"

    with pytest.raises(ValueError, match="credential-free"):
        extract_references("https://user:secret@source.example/", b"<a href='/'>x</a>")


def test_image_budget_is_per_occurrence_and_does_not_remove_documents_or_navigation():
    images = "".join(f'<img src="/images/{index}.jpg" alt="image {index}">' for index in range(6))
    raw = (
        f"<html><body>{images}"
        '<a href="/after">After gallery</a>'
        '<a href="/report.pdf">Report</a>'
        '<img src="/images/0.jpg" alt="repeated occurrence">'
        "</body></html>"
    ).encode()

    references = extract_references(BASE, raw, max_images_per_page=3)
    selected_images = [reference for reference in references if reference.kind == "image"]
    assert [reference.normalised_url for reference in selected_images] == [
        "https://source.example/images/0.jpg",
        "https://source.example/images/1.jpg",
        "https://source.example/images/2.jpg",
    ]
    assert [(reference.kind, reference.normalised_url) for reference in references[-2:]] == [
        ("navigation", "https://source.example/after"),
        ("document", "https://source.example/report.pdf"),
    ]

    without_images = extract_references(BASE, raw, max_images_per_page=0)
    assert [reference.kind for reference in without_images] == ["navigation", "document"]


def test_srcset_width_density_query_commas_and_repeated_occurrences():
    raw = b"""
    <html><body>
      <img src="/fallback-a.jpg"
           srcset="/small.jpg 480w, /large.jpg?crop=1,2 1440w">
      <img src="/fallback-b.jpg" srcset="/one.jpg 1x, /two.jpg 2x">
      <img src="/same.jpg" alt="first">
      <img src="/same.jpg" alt="second">
    </body></html>
    """

    images = extract_references(BASE, raw)
    assert [image.normalised_url for image in images] == [
        "https://source.example/large.jpg?crop=1%2C2",
        "https://source.example/two.jpg",
        "https://source.example/same.jpg",
        "https://source.example/same.jpg",
    ]
    assert [image.srcset_descriptor for image in images[:2]] == ["1440w", "2x"]
    assert [image.alt_text for image in images[-2:]] == ["first", "second"]
    assert len({image.ordinal for image in images}) == 4


def test_image_budget_validation():
    with pytest.raises(ValueError, match="non-negative"):
        extract_references(BASE, b"", max_images_per_page=-1)
    with pytest.raises(TypeError, match="integer"):
        extract_references(BASE, b"", max_images_per_page=1.5)  # type: ignore[arg-type]


def test_reference_candidate_public_shape_is_stable():
    fields = tuple(ReferenceCandidate.__dataclass_fields__)
    assert fields == (
        "url",
        "normalised_url",
        "raw_url",
        "kind",
        "expected_content",
        "rule",
        "ordinal",
        "source_attribute",
        "anchor_text",
        "alt_text",
        "title_text",
        "caption",
        "nearby_heading",
        "declared_type",
        "srcset_descriptor",
    )
