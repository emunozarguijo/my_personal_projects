"""Excepciones propias de la aplicación."""


class AppError(Exception):
    """Base de todos los errores controlados."""


class ConfigurationError(AppError):
    """Falta o es inválida una configuración / secreto."""


class PdfExtractionError(AppError):
    """No se pudo leer texto del PDF (corrupto, escaneado, protegido...)."""


class StatementParseError(AppError):
    """El texto no corresponde a un estado de cuenta soportado."""


class EmbeddingError(AppError):
    """Falló la generación de embeddings."""


class LLMError(AppError):
    """Falló la llamada al proveedor de LLM."""
