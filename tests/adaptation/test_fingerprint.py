from novel_crawler.adaptation.fingerprint import StructureFingerprint, fingerprint_html

SALT = b"s" * 32


def test_fingerprint_is_deterministic_and_content_independent() -> None:
    first = "<html><body><main><h1 id='book-123'>Alice</h1><article><p>Secret prose A</p></article></main></body></html>"
    second = "<html><body><main><h1 id='book-999'>Bob</h1><article><p>Entirely different private prose</p></article></main></body></html>"
    a = fingerprint_html(first, "chapter", {"title": "h1", "content": "article"}, SALT)
    b = fingerprint_html(second, "chapter", {"title": "h1", "content": "article"}, SALT)

    assert a == b
    assert isinstance(a, StructureFingerprint)
    assert len(a.digest) == 64
    assert "Alice" not in repr(a)


def test_layout_change_changes_digest_but_noise_is_ignored() -> None:
    base = "<main><h1>Title</h1><article><p>Text</p></article></main>"
    layout = "<main><header><h1>Title</h1></header><article><p>Text</p></article></main>"
    noisy = "<main><h1>Title</h1><aside class='advert'>Buy</aside><article><p>Text</p></article><script>x()</script></main>"
    selectors = {"title": "h1", "content": "article"}

    assert fingerprint_html(base, "chapter", selectors, SALT).digest != fingerprint_html(layout, "chapter", selectors, SALT).digest
    assert fingerprint_html(base, "chapter", selectors, SALT).digest == fingerprint_html(noisy, "chapter", selectors, SALT).digest


def test_semantic_attributes_and_selector_counts_affect_structure_without_pii() -> None:
    one = "<main><a role='navigation' href='/private/1'>Next</a></main>"
    two = "<main><a role='button' href='https://secret.example/token'>Prev</a></main>"
    assert fingerprint_html(one, "chapter", {"nav": "a"}, SALT).digest != fingerprint_html(two, "chapter", {"nav": "a"}, SALT).digest


def test_fingerprint_serialization_is_strict() -> None:
    fp = fingerprint_html("<article><p>x</p></article>", "chapter", {}, SALT)
    assert StructureFingerprint.from_dict(fp.to_dict()) == fp


def test_fingerprint_rejects_invalid_inputs() -> None:
    import pytest

    for args in [(2, "chapter", "a" * 64), (1, "other", "a" * 64), (1, "chapter", "bad")]:
        with pytest.raises(ValueError):
            StructureFingerprint(*args)
    with pytest.raises(ValueError):
        StructureFingerprint.from_dict({"version": 1})
    with pytest.raises(TypeError):
        StructureFingerprint.from_dict({"version": True, "page_kind": "chapter", "digest": "a" * 64})
    with pytest.raises(ValueError):
        fingerprint_html("<p>x</p>", "other", {}, SALT)
    with pytest.raises(TypeError):
        fingerprint_html("<p>x</p>", "chapter", {"x": 1}, SALT)  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        fingerprint_html("<p>x</p>", "chapter", {"x": "div["}, SALT)


def test_salt_makes_identical_layouts_unlinkable() -> None:
    html = "<main><article><p>private</p></article></main>"
    assert fingerprint_html(html, "chapter", {}, SALT).digest != fingerprint_html(html, "chapter", {}, b"x" * 32).digest
    assert fingerprint_html(html, "chapter", {}, SALT).digest != fingerprint_html(html, "book", {}, SALT).digest


def test_rejects_unsafe_or_excessive_candidate_selectors() -> None:
    import pytest

    for selector in ("a:has(span)", "div:contains(private-text)", "p:-soup-contains(private-text)", "p:-SOUP-CONTAINS-OWN(private-text)", "[data-user='alice@example.com']", "body div p a span"):
        with pytest.raises(ValueError):
            fingerprint_html("<p>x</p>", "chapter", {"private": selector}, SALT)
    with pytest.raises(ValueError):
        fingerprint_html("<p>x</p>", "chapter", {f"s{i}": "p" for i in range(21)}, SALT)
    with pytest.raises(ValueError):
        fingerprint_html("<p>x</p>", "chapter", {}, b"short")
