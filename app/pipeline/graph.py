"""Construcción de los grafos de LangGraph.

  Ingesta:  validate_file → extract_text → parse_statement → check_duplicate → categorize → persist_statement
            → build_chunks → embed_and_store → END       (cualquier error o duplicado → END)
  Q&A:      retrieve → build_prompt → generate → END      (cualquier error → END)
"""
from __future__ import annotations

from langgraph.graph import END, StateGraph

from app.pipeline.nodes import IngestionNodes, QANodes
from app.pipeline.state import IngestionState, QAState


def _continue_or_end(next_node: str):
    def route(state) -> str:
        return END if state.get("error") else next_node

    return route


def _after_duplicate_check(state: IngestionState) -> str:
    if state.get("error") or state.get("duplicate"):
        return END
    return "categorize"


def _link(graph: StateGraph, current: str, nxt: str) -> None:
    """current → nxt, salvo que el estado tenga error (entonces END)."""
    graph.add_conditional_edges(current, _continue_or_end(nxt), {nxt: nxt, END: END})


def build_ingestion_graph(nodes: IngestionNodes):
    g = StateGraph(IngestionState)
    g.add_node("validate_file", nodes.validate_file)
    g.add_node("extract_text", nodes.extract_text)
    g.add_node("parse_statement", nodes.parse_statement)
    g.add_node("check_duplicate", nodes.check_duplicate)
    g.add_node("categorize", nodes.categorize)
    g.add_node("persist_statement", nodes.persist_statement)
    g.add_node("build_chunks", nodes.build_chunks)
    g.add_node("embed_and_store", nodes.embed_and_store)

    g.set_entry_point("validate_file")
    _link(g, "validate_file", "extract_text")
    _link(g, "extract_text", "parse_statement")
    _link(g, "parse_statement", "check_duplicate")
    g.add_conditional_edges(
        "check_duplicate", _after_duplicate_check, {"categorize": "categorize", END: END}
    )
    _link(g, "categorize", "persist_statement")
    _link(g, "persist_statement", "build_chunks")
    _link(g, "build_chunks", "embed_and_store")
    g.add_edge("embed_and_store", END)
    return g.compile()


def build_qa_graph(nodes: QANodes):
    g = StateGraph(QAState)
    g.add_node("retrieve", nodes.retrieve)
    g.add_node("build_prompt", nodes.build_prompt)
    g.add_node("generate", nodes.generate)

    g.set_entry_point("retrieve")
    _link(g, "retrieve", "build_prompt")
    _link(g, "build_prompt", "generate")
    g.add_edge("generate", END)
    return g.compile()
