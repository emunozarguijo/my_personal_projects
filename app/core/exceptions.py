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


class AuthError(AppError):
    """Credenciales inválidas, registro rechazado o sesión inexistente."""


class RateLimitedError(AppError):
    """Demasiados intentos fallidos de inicio de sesión."""


class QuotaExceededError(AppError):
    """El usuario alcanzó un límite de uso (preguntas o cargas por día, estados de cuenta guardados)."""
