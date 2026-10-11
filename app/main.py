"""Punto de entrada local (GUI y CLI). Opera como el usuario LOCAL_USER (por defecto "local").

  python -m app.main                          → abre la interfaz gráfica
  python -m app.main --init-db                → aplica las migraciones (igual que python -m app.migrate)
  python -m app.main --ingest f.pdf           → carga un PDF sin GUI
  python -m app.main --ask "..."              → pregunta sin GUI (usa el último estado de cuenta)
  python -m app.main --stats                  → imprime métricas agregadas
  python -m app.main --user ana --ask "..."   → opera como otro usuario

Administración de cuentas (pide la contraseña sin mostrarla):
  python -m app.main --create-user ana [--admin]
  python -m app.main --reset-password ana     → también cierra todas sus sesiones
  python -m app.main --set-role ana admin
"""
from __future__ import annotations

import argparse
import getpass
import sys

from app.core.exceptions import AppError


def _ask_password() -> str:
    first = getpass.getpass("Contraseña nueva: ")
    if first != getpass.getpass("Repítela: "):
        raise AppError("Las contraseñas no coinciden.")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Asistente de estado de cuenta (RAG + LangGraph)")
    parser.add_argument("--init-db", action="store_true")
    parser.add_argument("--ingest", metavar="PDF")
    parser.add_argument("--ask", metavar="PREGUNTA")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--user", metavar="USUARIO", help="usuario con el que operar (por defecto LOCAL_USER)")
    parser.add_argument("--create-user", metavar="USUARIO")
    parser.add_argument("--admin", action="store_true", help="con --create-user: crea la cuenta como admin")
    parser.add_argument("--reset-password", metavar="USUARIO")
    parser.add_argument("--set-role", nargs=2, metavar=("USUARIO", "ROL"))
    args = parser.parse_args(argv)

    try:
        if args.init_db:
            from app.migrate import upgrade

            upgrade()
            return 0

        from app.container import build_services
        from app.core.config import get_settings

        services = build_services()
        auth = services.auth

        if args.create_user:
            user = auth.create_user(args.create_user, _ask_password(), role="admin" if args.admin else "user")
            print(f"Cuenta creada: {user.username} ({user.role}).")
            return 0
        if args.reset_password:
            user = auth.reset_password(args.reset_password, _ask_password())
            print(f"Contraseña actualizada para {user.username}; se cerraron sus sesiones.")
            return 0
        if args.set_role:
            user = auth.set_role(*args.set_role)
            print(f"{user.username} ahora es {user.role}.")
            return 0

        user = auth.ensure_user(args.user or get_settings().local_user)
        assistant = services.assistant.for_user(user.id)
    except AppError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

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
