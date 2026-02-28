import logging

from app.core.request_context import get_correlation_id


class CorrelationIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "correlation_id"):
            record.correlation_id = get_correlation_id() or "-"
        return True


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not root.handlers:
        handler = logging.StreamHandler()
        handler.addFilter(CorrelationIdFilter())
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] [cid=%(correlation_id)s] %(message)s")
        )
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            handler.addFilter(CorrelationIdFilter())
