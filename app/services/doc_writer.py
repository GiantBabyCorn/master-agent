from datetime import datetime
from pathlib import Path
import re


def build_doc_timestamp(date_value: datetime | None = None) -> str:
    date_value = date_value or datetime.now()
    return date_value.strftime("%Y%m%d-%H%M%S")


def build_doc_file_name(doc_name: str, date_value: datetime | None = None) -> str:
    clean_doc_name = re.sub(r"[^A-Z0-9_]+", "_", doc_name.strip().upper()).strip("_")
    return f"{clean_doc_name}.{build_doc_timestamp(date_value)}.md"


def write_agent_doc(doc_name: str, content: str) -> Path:
    docs_dir = Path.cwd() / ".agent"
    docs_dir.mkdir(parents=True, exist_ok=True)

    file_path = docs_dir / build_doc_file_name(doc_name)
    file_path.write_text(content, encoding="utf-8")
    return file_path
