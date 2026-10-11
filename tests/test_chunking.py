from pathlib import Path

from app.services.chunking import StatementChunker
from app.services.pdf_parser import parse_statement_text

FIXTURE = Path(__file__).parent / "fixtures" / "nu_statement_sample.txt"


def _chunks(redact=True):
    parsed = parse_statement_text(FIXTURE.read_text(encoding="utf-8"))
    return parsed, StatementChunker(tx_per_chunk=10, redact_pii=redact).chunk(parsed)


def test_chunk_kinds_and_positions():
    _, chunks = _chunks()
    kinds = [c.kind for c in chunks]
    assert kinds[0] == "summary"
    assert kinds.count("transactions") == 2  # 17 movimientos / 10 por chunk
    assert {"scenarios", "installments", "analytics"} <= set(kinds)
    assert [c.position for c in chunks] == list(range(len(chunks)))


def test_summary_has_key_numbers():
    _, chunks = _chunks()
    text = chunks[0].content
    assert "$17,510.97" in text and "21/09/2026" in text and "57.0" in text


def test_analytics_precomputes_totals():
    _, chunks = _chunks()
    analytics = next(c for c in chunks if c.kind == "analytics").content
    assert "$14,984.20" in analytics          # total de compras
    assert "Apple.Com/Bill: $214.00" in analytics  # 165 + 49 agrupado


def test_redaction():
    parsed, redacted = _chunks(True)
    assert "Ana Pérez" not in redacted[0].content
    _, full = _chunks(False)
    assert parsed.holder_name in full[0].content
