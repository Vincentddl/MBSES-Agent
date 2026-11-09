"""Resolve private case data without publishing a machine-specific file path."""
import os
from pathlib import Path
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def default_case_source():
    value = os.getenv("MBSES_CASE_SOURCE") or dotenv_values(ROOT / ".env.real").get("MBSES_CASE_SOURCE")
    path = Path(value) if value else Path("private/toothbrush_case.docx")
    return path if path.is_absolute() else ROOT / path
