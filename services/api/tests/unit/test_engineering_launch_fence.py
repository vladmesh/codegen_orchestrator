from types import SimpleNamespace

from shared.contracts.dto.commit_publication import COMMIT_PUBLICATION_KEY
from src.routers.runs import _launch_fenced


def test_late_deploy_cannot_escape_the_committed_story_stop():
    story = SimpleNamespace(status="waiting_human_review", engineering_stop=None)
    project = SimpleNamespace(config={})
    assert _launch_fenced(story, project)


def test_preserved_checkout_fences_taskless_deploy():
    project = SimpleNamespace(config={COMMIT_PUBLICATION_KEY: {}})
    assert _launch_fenced(None, project)


def test_normal_pr_deploy_remains_available():
    story = SimpleNamespace(status="deploying", engineering_stop=None)
    assert not _launch_fenced(story, SimpleNamespace(config={}))
