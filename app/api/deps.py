from collections.abc import Generator

from sqlalchemy.orm import Session

from app.db.session import get_db_session


def db_dep() -> Generator[Session, None, None]:
    yield from get_db_session()
