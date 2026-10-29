import logging
import sys


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Konfiguriert ein einheitliches Logging für die projekt7 Akquise-Pipeline."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    logger = logging.getLogger("p7_akquise")
    if not logger.handlers:
        logger.setLevel(numeric_level)
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(numeric_level)
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger
