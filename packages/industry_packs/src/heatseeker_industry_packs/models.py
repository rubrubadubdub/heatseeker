"""Pack registration table: which pack versions this instance has loaded (spec §31.4)."""

from datetime import datetime

from heatseeker_common.db import Base, UTCDateTime
from heatseeker_common.timeutil import utc_now
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column


class PackRegistration(Base):
    __tablename__ = "industry_pack_registration"

    pack_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(20))
    content_hash: Mapped[str] = mapped_column(String(64))
    first_loaded_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    loaded_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
