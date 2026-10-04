from filebrownie.evidence import readers
from filebrownie.evidence.cache import cache_key, reader_fingerprint


def test_cache_identity_tracks_bytes_format_and_configuration(monkeypatch):
    before = reader_fingerprint()
    monkeypatch.setattr(readers, "MAX_INPUT_BYTES", readers.MAX_INPUT_BYTES + 1)
    after = reader_fingerprint()
    assert before != after
    base = cache_key("a" * 64, "pdf", before)
    assert (
        len(
            {
                base,
                cache_key("b" * 64, "pdf", before),
                cache_key("a" * 64, "jpeg", before),
                cache_key("a" * 64, "pdf", after),
            }
        )
        == 4
    )
