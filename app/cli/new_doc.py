import sys

from app.services.doc_writer import write_agent_doc


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python -m app.cli.new_doc <DOC_NAME>")
        return 1

    doc_name = sys.argv[1]
    content = "\n".join(
        [
            f"# {doc_name}",
            "",
            "## Context",
            "Describe why this document was generated.",
            "",
            "## Notes",
            "- Add implementation notes here",
            "- Keep content in English as project convention",
            "",
            "## Next Steps",
            "- Define actionable follow-up tasks",
        ]
    )

    file_path = write_agent_doc(doc_name, content)
    print(f"Created: {file_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
