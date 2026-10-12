"""A pull request linked to a story by an operator or automation, not by the story's own runs."""

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class StoryLink(Base):
    __tablename__ = "story_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    story_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("stories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # A `StoryLinkKind` value, e.g. a reusable module proposed to the module library.
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
