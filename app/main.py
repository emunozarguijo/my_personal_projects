"""Punto de entrada.

  python -m app.main                 → abre la interfaz gráfica
  python -m app.main --init-db       → crea extensión pgvector y tablas
  python -m app.main --ingest f.pdf  → carga un PDF sin GUI
  python -m app.main --ask "..."     → pregunta sin GUI (usa el último estado de cuenta)
  python -m app.main --stats         → imprime métricas agregadas
"""
from __future__ import annotations

import argparse
import sys

from app.core.exceptions import AppError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Asistente de estado de cuenta (RAG + LangGraph)")
    parser.add_argument("--init-db", action="store_true")
    parser.add_argument("--ingest", metavar="PDF")
    parser.add_argument("--ask", metavar="PREGUNTA")
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args(argv)

    from app.container import build_assistant

    try:
        assistant = build_assistant()
    except AppError as exc:
        print(f"Error de configuración: {exc}", file=sys.stderr)
        return 2

    if args.init_db:
        print("Base de datos lista.")
        return 0
    if args.ingest:
        r = assistant.ingest_pdf(args.ingest)
        print(r.model_dump_json(indent=2))
        return 0 if r.ok else 1
    if args.ask:
        r = assistant.ask(args.ask)
        print(r.answer if r.ok else f"Error: {r.error}")
        if r.llm:
            print(f"\n[{r.llm.model} · {r.llm.latency_ms:.0f} ms · {r.llm.usage.input_tokens}→{r.llm.usage.output_tokens} tokens]")
        return 0 if r.ok else 1
    if args.stats:
        for s in assistant.metrics_summary():
            print(s.model_dump_json())
        return 0

    from app.ui.gui import run_gui

    run_gui(assistant)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
