# Asistente de estado de cuenta (RAG + LangGraph)

Aplicación de escritorio en Python: cargas el PDF del estado de cuenta de tu tarjeta (formato **Nu México**), se
extraen y guardan los datos en **PostgreSQL + pgvector**, y puedes hacerle preguntas a un LLM que responde usando como
contexto lo recuperado de la base de datos (**RAG**). Registra **logs, latencia, tokens, costo y errores** de cada llamada.

## Arquitectura

```
app/
├── main.py            # CLI + arranque de la GUI
├── container.py       # Composition root: crea y conecta todas las dependencias
├── schemas.py         # Contratos pydantic (ParsedStatement, Chunk, LLMResponse, ...)
├── core/
│   ├── config.py      # Settings + secretos (.env / variables de entorno, SecretStr)
│   ├── logging.py     # Logs JSON (logs/app.jsonl) + request_id por operación
│   ├── database.py    # Engine / sesiones / CREATE EXTENSION vector
│   ├── models.py      # ORM: statements, transactions, installments, document_chunks (vector), api_metrics
│   ├── prompts.py     # System prompt y armado del contexto
│   └── exceptions.py
├── services/          # Lógica de negocio, sin LangGraph (reutilizable)
│   ├── pdf_parser.py  # PdfTextExtractor + NuStatementParser (+ validaciones de consistencia)
│   ├── chunking.py    # ParsedStatement → fragmentos (resumen, escenarios, a meses, movimientos, análisis)
│   ├── embeddings.py  # Proveedores: local (sentence-transformers) | openai | fake
│   ├── retrieval.py   # Retriever: embedding de la pregunta + búsqueda pgvector (coseno) + resumen siempre
│   ├── llm.py         # Proveedores: anthropic | openai, con métricas
│   ├── metrics.py     # MetricsService + Stopwatch (latencia, tokens, costo)
│   ├── repository.py  # Única capa que conoce SQLAlchemy
│   └── assistant.py   # Fachada para UI/CLI
├── pipeline/          # LangGraph
│   ├── state.py       # IngestionState, QAState
│   ├── nodes.py       # IngestionNodes, QANodes (clases con dependencias inyectadas)
│   └── graph.py       # build_ingestion_graph, build_qa_graph
├── api.py             # FastAPI: subir PDF, preguntar, métricas (+ página HTML mínima)
└── ui/gui.py          # Tkinter: cargar PDF, chat, resumen, métricas
```

**Grafo de ingesta:** `validate_file → extract_text → parse_statement → check_duplicate → persist_statement → build_chunks → embed_and_store`
(un error o un PDF duplicado cortan el grafo).
**Grafo de preguntas:** `retrieve → build_prompt → generate`.

Los nodos son métodos de clases con servicios inyectados: puedes agregar nodos (p. ej. un clasificador de intención,
un reranker, un guardrail, un nodo de reintento) sin tocar los servicios.

## Puesta en marcha

Requiere Python 3.10+ (con Tkinter) y Docker (o un PostgreSQL con pgvector).

```bash
python -m venv .venv && source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-local-embeddings.txt            # solo si usarás EMBEDDING_PROVIDER=local

cp .env.example .env                                        # y edítalo (ver "Secretos")
docker compose up -d                                        # PostgreSQL 16 + pgvector
python -m app.main --init-db                                # crea extensión y tablas
python -m app.main                                          # abre la interfaz gráfica
```

Hay tres formas de usarlo (todas comparten el mismo código y base de datos). Ejecuta los comandos desde la carpeta raíz del proyecto
(donde está `README.md`); el punto de entrada es `app/main.py`, por eso se lanza como módulo (`-m app.main`):

| Modo | Comando |
|---|---|
| Escritorio (Tkinter) | `python -m app.main` |
| Web (FastAPI) | `uvicorn app.api:app --reload` → http://127.0.0.1:8000 (Swagger en `/docs`) |
| Terminal | `python -m app.main --ingest estado.pdf` · `--ask "¿Cuánto debo pagar?"` · `--stats` |

**API web:** `POST /upload-pdf/` (igual que tu ejemplo, pero procesa el PDF), `GET /statements`, `POST /ask`,
`GET /metrics`, `GET /metrics/recent`, `DELETE /statements/{id}`. Los PDFs se guardan en `UPLOAD_DIR` (`archivos_subidos/`).

### Usar Neon (PostgreSQL en la nube) en lugar de Docker

No necesitas `docker compose`. En tu `.env`:

```
DB_HOST=<host de Neon>          # el que termina en neon.tech
DB_PORT=5432
DB_NAME=<base>
DB_USER=<usuario>
DB_PASSWORD='<contraseña>'      # entre comillas simples si tiene # o !
DB_SCHEMA=<tu esquema privado>
DB_SSLMODE=require
```

Las tablas se crean dentro de `DB_SCHEMA`. Neon incluye pgvector; si `--init-db` dice que la extensión no existe, ejecuta `CREATE EXTENSION vector;` en la consola SQL de Neon.

### Secretos

- Todo se lee de `.env` o de variables de entorno (`app/core/config.py`); las claves son `SecretStr` y no se imprimen.
- `.env` está en `.gitignore`. **Nunca** subas `.env` ni PDFs de estados de cuenta al repositorio.
- Necesitas: `DB_PASSWORD`, y `ANTHROPIC_API_KEY` (o `OPENAI_API_KEY` si `LLM_PROVIDER=openai`).
- Embeddings: Anthropic no ofrece API de embeddings, por eso hay dos opciones: **local** (gratis, sin key) u **openai**.
  Si usas `openai` con `text-embedding-3-small` pon `EMBEDDING_DIM=1536`. Cambiar de modelo/dimensión exige recrear `document_chunks`.
- Para estimar costo, llena `PRICE_INPUT_PER_MTOK` / `PRICE_OUTPUT_PER_MTOK` con los precios de tu proveedor (por defecto 0).

## Observabilidad

| Qué | Dónde |
|---|---|
| Logs JSON con `request_id` | `logs/app.jsonl` (rotativo) y consola |
| Métrica por llamada (`chat`, `embedding`, `retrieval`, `ingestion`): latencia ms, tokens in/out, costo, estado, error | tabla `api_metrics` |
| Agregados: llamadas, errores, latencia promedio y p95, tokens, costo | pestaña **Métricas** / `--stats` |
| Tiempo por nodo del grafo | `timings_ms` en `IngestResult` / `AskResult` y en logs (`node.<nombre>`) |

Consulta de ejemplo: `SELECT operation, avg(latency_ms), sum(input_tokens), sum(output_tokens) FROM api_metrics GROUP BY 1;`

## Decisiones de diseño

- **Parser determinista, no LLM:** las cifras del estado de cuenta se extraen con regex y se validan entre sí
  (el resumen debe cuadrar, compras parseadas = cargos regulares, etc.). Los avisos quedan en `Statement.extra.warnings`.
- **Los agregados se calculan en código** (`chunking._analytics`) y se escriben en el texto; el LLM no suma de memoria.
- **RAG:** cada estado de cuenta se divide en fragmentos autocontenidos con cabecera (periodo y tarjeta). El resumen
  siempre se incluye en el contexto y el resto sale de una búsqueda por similitud coseno (índice HNSW).
- **Privacidad:** con `REDACT_PII=true` no se envía dirección, CLABE, RFC ni nombre completo al LLM / proveedor de embeddings.
  Aun así, los importes y comercios sí se envían al proveedor que elijas: revisa sus políticas de datos.
- **Prompt injection:** el contexto va delimitado en `<contexto>` y el system prompt indica tratarlo como datos.
- **Idempotencia:** el hash SHA-256 del PDF evita cargarlo dos veces.

## Pruebas

```bash
pip install -r requirements-dev.txt
pytest
```

`tests/test_parser.py` y `tests/test_chunking.py` usan un texto anonimizado (`tests/fixtures/nu_statement_sample.txt`).
`tests/test_pipeline.py` ejecuta ambos grafos completos con dobles (`tests/fakes.py`): sin PostgreSQL ni APIs.

## Limitaciones conocidas

- Solo soporta el formato de Nu México. Para otro banco, crea una subclase de `BaseStatementParser` y agrégala a `PARSERS`.
- PDFs escaneados (imagen) no funcionan: haría falta OCR.
- Las tarjetas adicionales / varias tarjetas en un mismo PDF no se distinguen.
- Tkinter es austero a propósito; la GUI está desacoplada por la fachada `StatementAssistant` si prefieres Streamlit/Gradio.
