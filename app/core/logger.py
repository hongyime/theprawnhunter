import logging
import sys

from app.core.config import settings


class ContextLogger:
    """
    Centralized logging service with context awareness.
    Provides structured logging with consistent formatting.
    """

    def __init__(self, name: str):
        self.logger = logging.getLogger(name)
        self._setup_logger()

    def _setup_logger(self):
        """Configure logger based on environment"""
        if not self.logger.handlers:
            # Create handler
            handler = logging.StreamHandler(sys.stdout)

            # Set format based on environment
            if settings.ENV == "production":
                # JSON format for production (easier to parse)
                fmt = '{"time":"%(asctime)s","level":"%(levelname)s","name":"%(name)s","msg":"%(message)s"}'
            else:
                # Human-readable for development
                fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

            formatter = logging.Formatter(
                fmt,
                datefmt="%Y-%m-%d %H:%M:%S"
            )
            handler.setFormatter(formatter)

            self.logger.addHandler(handler)

            # Set level
            level = logging.DEBUG if settings.DEBUG else logging.INFO
            self.logger.setLevel(level)

    def with_context(self, **context):
        """
        Add context to log messages.
        Usage: logger.with_context(cred_id="abc123").info("Processing...")
        """
        return ContextualLoggerAdapter(self.logger, context)

    def debug(self, msg: str, **kwargs):
        self.logger.debug(msg, extra=kwargs)

    def info(self, msg: str, **kwargs):
        self.logger.info(msg, extra=kwargs)

    def warning(self, msg: str, **kwargs):
        self.logger.warning(msg, extra=kwargs)

    def error(self, msg: str, exc_info: bool = False, **kwargs):
        self.logger.error(msg, exc_info=exc_info, extra=kwargs)

    def critical(self, msg: str, exc_info: bool = True, **kwargs):
        self.logger.critical(msg, exc_info=exc_info, extra=kwargs)


class ContextualLoggerAdapter(logging.LoggerAdapter):
    """Adapter that adds context to log messages"""

    def process(self, msg, kwargs):
        # Add context to message
        context_str = " | ".join(f"{k}={v}" for k, v in self.extra.items())
        if context_str:
            msg = f"[{context_str}] {msg}"
        return msg, kwargs


def get_logger(name: str) -> ContextLogger:
    """
    Get a logger instance for the given name.

    Args:
        name: Logger name (typically __name__)

    Returns:
        ContextLogger instance

    Example:
        logger = get_logger(__name__)
        logger.info("Task started")
        logger.with_context(task_id="123").error("Task failed")
    """
    return ContextLogger(name)
