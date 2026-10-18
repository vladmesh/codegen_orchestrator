"""The real scheduler API client, narrowed to one story for supervisors over the shared database."""

from __future__ import annotations


class OnlyStory:
    """The real API client, with every status scan narrowed to one story.

    The service database is shared by every test in the session, so a
    supervisor that scans a status must not reach another test's stories.
    Every other call is the real client's.
    """

    def __init__(self, api_client, story_id: str) -> None:
        self._api = api_client
        self._story_id = story_id

    async def get_stories_by_status(self, status) -> list:
        stories = await self._api.get_stories_by_status(status)
        return [story for story in stories if story.id == self._story_id]

    def __getattr__(self, name: str):
        return getattr(self._api, name)
