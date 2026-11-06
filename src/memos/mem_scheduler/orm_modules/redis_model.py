
from typing import TypeVar

from sqlalchemy.orm import declarative_base

from memos.log import get_logger


T = TypeVar("T")  # The model type (MemoryMonitorManager, QueryMonitorManager, etc.)
ORM = TypeVar("ORM")  # The ORM model type

logger = get_logger(__name__)

Base = declarative_base()
