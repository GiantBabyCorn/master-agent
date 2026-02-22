from dataclasses import dataclass
import subprocess

from app.core.config import get_settings


@dataclass
class CursorRunInput:
    agent_name: str
    prompt: str
    project_path: str | None = None


@dataclass
class CursorRunResult:
    success: bool
    stdout: str
    stderr: str
    command: str


def run_cursor_agent(data: CursorRunInput) -> CursorRunResult:
    settings = get_settings()
    args = [settings.cursor_cli_command, "agent", "run", "--name", data.agent_name, "--prompt", data.prompt]
    if data.project_path:
        args.extend(["--project", data.project_path])

    command = " ".join(args)
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=settings.cursor_cli_timeout_ms / 1000.0,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return CursorRunResult(success=False, stdout="", stderr=str(exc), command=command)

    return CursorRunResult(
        success=result.returncode == 0,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
        command=command,
    )
