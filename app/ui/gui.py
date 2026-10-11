"""Interfaz gráfica (Tkinter, incluida con Python). Las operaciones largas corren en hilos
y devuelven el resultado a la UI mediante una cola, para no congelar la ventana."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable

from app.schemas import StatementSummary
from app.services.assistant import UserAssistant

QUICK_QUESTIONS = [
    "¿Cuánto debo pagar para no generar intereses y hasta cuándo?",
    "¿En qué gasté más este periodo?",
    "¿Cuánto me costaría pagar solo el mínimo?",
    "¿Cuánto me queda de mensualidades a meses?",
]


class App(tk.Tk):
    def __init__(self, assistant: UserAssistant):
        super().__init__()
        self.assistant = assistant
        self.title("Asistente de estado de cuenta")
        self.geometry("1100x700")
        self.minsize(900, 560)

        self._results: queue.Queue = queue.Queue()
        self._statements: list[StatementSummary] = []
        self._history: list[dict[str, str]] = []
        self._busy = False

        self._build_layout()
        self.after(100, self._poll_results)
        self._run_async(self.assistant.list_statements, self._on_statements)

    # ------------------------------------------------------------------ layout
    def _build_layout(self) -> None:
        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(paned, padding=10)
        paned.add(left, weight=1)
        ttk.Button(left, text="Cargar estado de cuenta (PDF)…", command=self._choose_pdf).pack(fill=tk.X)
        ttk.Label(left, text="Estados de cuenta").pack(anchor=tk.W, pady=(12, 2))
        self.listbox = tk.Listbox(left, exportselection=False, height=12)
        self.listbox.pack(fill=tk.BOTH, expand=True)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        ttk.Button(left, text="Eliminar seleccionado", command=self._delete_selected).pack(fill=tk.X, pady=(8, 0))
        self.status = tk.StringVar(value="Listo")
        ttk.Label(left, textvariable=self.status, wraplength=240, foreground="#555").pack(anchor=tk.W, pady=(12, 0))

        right = ttk.Notebook(paned)
        paned.add(right, weight=4)
        self._build_chat_tab(right)
        self._build_overview_tab(right)
        self._build_metrics_tab(right)

    def _build_chat_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="Preguntas")
        self.chat = scrolledtext.ScrolledText(tab, wrap=tk.WORD, state=tk.DISABLED, font=("Segoe UI", 10))
        self.chat.pack(fill=tk.BOTH, expand=True)
        self.chat.tag_config("user", foreground="#1a4fa0", font=("Segoe UI", 10, "bold"))
        self.chat.tag_config("bot", foreground="#111")
        self.chat.tag_config("meta", foreground="#888", font=("Segoe UI", 8))
        self.chat.tag_config("err", foreground="#b00020")

        quick = ttk.Frame(tab)
        quick.pack(fill=tk.X, pady=(6, 0))
        for q in QUICK_QUESTIONS:
            ttk.Button(quick, text=q, command=lambda q=q: self._ask(q)).pack(side=tk.LEFT, padx=2)

        row = ttk.Frame(tab)
        row.pack(fill=tk.X, pady=(6, 0))
        self.entry = ttk.Entry(row)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.entry.bind("<Return>", lambda _e: self._ask(self.entry.get()))
        ttk.Button(row, text="Enviar", command=lambda: self._ask(self.entry.get())).pack(side=tk.LEFT, padx=(6, 0))

    def _build_overview_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="Resumen")
        self.overview = scrolledtext.ScrolledText(tab, wrap=tk.WORD, state=tk.DISABLED, font=("Consolas", 10))
        self.overview.pack(fill=tk.BOTH, expand=True)

    def _build_metrics_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="Métricas")
        ttk.Button(tab, text="Actualizar", command=self._refresh_metrics).pack(anchor=tk.E)

        cols = ("op", "calls", "errors", "avg", "p95", "tin", "tout", "cost")
        heads = ("Operación", "Llamadas", "Errores", "Latencia prom. (ms)", "Latencia p95 (ms)", "Tokens entrada", "Tokens salida", "Costo USD")
        self.stats_tree = ttk.Treeview(tab, columns=cols, show="headings", height=6)
        for c, h in zip(cols, heads):
            self.stats_tree.heading(c, text=h)
            self.stats_tree.column(c, width=110, anchor=tk.CENTER)
        self.stats_tree.pack(fill=tk.X, pady=(6, 10))

        ttk.Label(tab, text="Últimas llamadas").pack(anchor=tk.W)
        cols2 = ("when", "op", "model", "status", "ms", "tin", "tout", "req")
        heads2 = ("Fecha", "Operación", "Modelo", "Estado", "ms", "Tokens in", "Tokens out", "request_id")
        self.calls_tree = ttk.Treeview(tab, columns=cols2, show="headings")
        for c, h in zip(cols2, heads2):
            self.calls_tree.heading(c, text=h)
            self.calls_tree.column(c, width=100, anchor=tk.CENTER)
        self.calls_tree.pack(fill=tk.BOTH, expand=True)

    # ------------------------------------------------------------------ async helpers
    def _run_async(self, fn: Callable, on_done: Callable, busy_msg: str | None = None) -> None:
        if busy_msg:
            self.status.set(busy_msg)

        def worker() -> None:
            try:
                self._results.put((on_done, fn(), None))
            except Exception as exc:  # noqa: BLE001 - se muestra en la UI
                self._results.put((on_done, None, exc))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_results(self) -> None:
        try:
            while True:
                callback, result, error = self._results.get_nowait()
                callback(result, error)
        except queue.Empty:
            pass
        self.after(100, self._poll_results)

    # ------------------------------------------------------------------ estados de cuenta
    def _selected_id(self) -> int | None:
        sel = self.listbox.curselection()
        return self._statements[sel[0]].id if sel else None

    def _on_statements(self, result, error, select_id: int | None = None) -> None:
        if error:
            self.status.set(f"No se pudo leer la base de datos: {error}")
            return
        self._statements = result
        self.listbox.delete(0, tk.END)
        for s in self._statements:
            self.listbox.insert(tk.END, s.label)
        if self._statements:
            idx = next((i for i, s in enumerate(self._statements) if s.id == select_id), 0)
            self.listbox.selection_set(idx)
            self._on_select()
        self.status.set("Listo")

    def _on_select(self, _event=None) -> None:
        sid = self._selected_id()
        self._history.clear()
        if sid is None:
            return
        self._run_async(lambda: self.assistant.overview(sid), self._on_overview)

    def _on_overview(self, text, error) -> None:
        self._set_text(self.overview, text if not error else f"Error: {error}")

    def _choose_pdf(self) -> None:
        path = filedialog.askopenfilename(title="Selecciona el estado de cuenta", filetypes=[("PDF", "*.pdf")])
        if path:
            self._run_async(lambda: self.assistant.ingest_pdf(path), self._on_ingested, "Procesando PDF…")

    def _on_ingested(self, result, error) -> None:
        if error or not result.ok:
            self.status.set("Falló la carga")
            messagebox.showerror("No se pudo procesar el PDF", str(error or result.error))
            return
        if result.duplicate:
            messagebox.showinfo("Ya cargado", "Este PDF ya estaba en la base de datos.")
        else:
            detail = f"{result.n_transactions} movimientos, {result.n_chunks} fragmentos indexados en {result.total_ms / 1000:.1f} s."
            if result.warnings:
                detail += "\n\nAvisos:\n- " + "\n- ".join(result.warnings)
            messagebox.showinfo("Estado de cuenta cargado", detail)
        sid = result.statement_id
        self._run_async(self.assistant.list_statements, lambda r, e: self._on_statements(r, e, sid))
        self._refresh_metrics()

    def _delete_selected(self) -> None:
        sid = self._selected_id()
        if sid is None or not messagebox.askyesno("Eliminar", "¿Eliminar este estado de cuenta y sus datos?"):
            return
        self._run_async(
            lambda: self.assistant.delete_statement(sid) or self.assistant.list_statements(), self._on_statements
        )

    # ------------------------------------------------------------------ chat
    def _ask(self, question: str) -> None:
        question = question.strip()
        if not question or self._busy:
            return
        self.entry.delete(0, tk.END)
        self._append(f"Tú: {question}\n", "user")
        self._busy = True
        sid = self._selected_id()
        history = list(self._history)
        self._run_async(
            lambda: self.assistant.ask(question, sid, history),
            lambda r, e: self._on_answer(question, r, e),
            "Consultando…",
        )

    def _on_answer(self, question: str, result, error) -> None:
        self._busy = False
        self.status.set("Listo")
        if error or not result.ok:
            self._append(f"Error: {error or result.error}\n\n", "err")
            return
        self._history += [{"role": "user", "content": question}, {"role": "assistant", "content": result.answer}]
        self._append(f"{result.answer}\n", "bot")
        llm = result.llm
        meta = (
            f"⏱ {result.total_ms:.0f} ms (LLM {llm.latency_ms:.0f} ms) · tokens {llm.usage.input_tokens}→{llm.usage.output_tokens}"
            f" · {llm.model} · {len(result.sources)} fuentes\n\n"
        )
        self._append(meta, "meta")
        self._refresh_metrics()

    def _append(self, text: str, tag: str) -> None:
        self.chat.configure(state=tk.NORMAL)
        self.chat.insert(tk.END, text, tag)
        self.chat.configure(state=tk.DISABLED)
        self.chat.see(tk.END)

    @staticmethod
    def _set_text(widget: scrolledtext.ScrolledText, text: str) -> None:
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert(tk.END, text)
        widget.configure(state=tk.DISABLED)

    # ------------------------------------------------------------------ métricas
    def _refresh_metrics(self) -> None:
        self._run_async(
            lambda: (self.assistant.metrics_summary(), self.assistant.recent_calls(30)), self._on_metrics
        )

    def _on_metrics(self, result, error) -> None:
        if error:
            return
        stats, calls = result
        self.stats_tree.delete(*self.stats_tree.get_children())
        for s in stats:
            self.stats_tree.insert("", tk.END, values=(
                s.operation, s.calls, s.errors, f"{s.avg_latency_ms:.0f}", f"{s.p95_latency_ms:.0f}",
                s.input_tokens, s.output_tokens, f"{s.cost_usd:.4f}",
            ))  # fmt: skip
        self.calls_tree.delete(*self.calls_tree.get_children())
        for c in calls:
            self.calls_tree.insert("", tk.END, values=(
                c.created_at, c.operation, c.model, c.status, f"{c.latency_ms:.0f}",
                c.input_tokens, c.output_tokens, c.request_id,
            ))  # fmt: skip


def run_gui(assistant: UserAssistant) -> None:
    app = App(assistant)
    app.mainloop()
