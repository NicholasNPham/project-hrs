"""Logging to a dated file and the console.

Ported from Project-Rap-Sheet's logger.py, with one deliberate difference:
Rap-Sheet logs defendant names, HRS does not. Every line names the
spreadsheet row instead, which is enough to find the person in the sheet and
keeps the log safe to paste.
"""

import logging
import sys
from pathlib import Path

LOGGER_NAME = "HRS"
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)-7s - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_path: Path) -> logging.Logger:
    """Attach a file handler and a console handler to the HRS logger.

    Parent folders of log_path are created. A second call returns the
    already configured logger, because adding handlers again would print
    every line twice.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return logger

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    to_file = logging.FileHandler(log_path, encoding="utf-8")
    to_file.setLevel(logging.DEBUG)
    to_file.setFormatter(formatter)
    logger.addHandler(to_file)

    to_console = logging.StreamHandler(sys.stdout)
    to_console.setLevel(logging.INFO)
    to_console.setFormatter(formatter)
    logger.addHandler(to_console)

    return logger


def get_logger(module: str = "") -> logging.Logger:
    """Return the HRS logger, or a child of it named for one module."""
    if not module:
        return logging.getLogger(LOGGER_NAME)
    return logging.getLogger(LOGGER_NAME).getChild(module)
