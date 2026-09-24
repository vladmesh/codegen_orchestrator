"""A fake docker CLI that removes images by the daemon's rules, for the cleanup tests.

What the cleanups depend on, as `docker image rm` behaves (moby `ImageDelete`):

* by ID, an image named in more than one repository is refused ("referenced in multiple
  repositories"), and an image a container uses is refused ("being used");
* by name, only that name goes, and untagging the last tag of a repository also drops that
  repository's digest references; an image left with no name is deleted, unless a container
  uses it, which refuses the untag of its last name;
* anything not there any more is "No such image".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
import subprocess


def _repository(name: str) -> str:
    if "@" in name:
        return name.partition("@")[0]
    return name.rpartition(":")[0]


def _single_repository(names: list[str]) -> bool:
    tags = [name for name in names if "@" not in name]
    if len(tags) > 1:
        return False
    return len({_repository(name) for name in names}) <= 1


@dataclass
class FakeImage:
    names: list[str]
    labels: dict[str, str] = field(default_factory=dict)
    parent: str = ""


class FakeDockerDaemon:
    def __init__(self) -> None:
        self.images: dict[str, FakeImage] = {}
        self.containers: dict[str, str] = {}  # container ID -> image ID
        self.listed_but_gone: list[str] = []  # IDs `image ls` still reports
        self.failures: dict[str, str] = {}  # image ID -> stderr of a failing removal
        self.before_first_removal: Callable[[FakeDockerDaemon], None] | None = None
        self.removals: list[str] = []  # every `image rm` argument, in order

    def add(self, image_id: str, *names: str, labels: dict | None = None, parent: str = ""):
        self.images[image_id] = FakeImage(list(names), dict(labels or {}), parent)

    def untag(self, name: str) -> None:
        for image in self.images.values():
            if name in image.names:
                image.names.remove(name)

    def _resolve(self, argument: str) -> str | None:
        if argument in self.images:
            return argument
        return next((i for i, image in self.images.items() if argument in image.names), None)

    @staticmethod
    def _fail(command: list[str], stderr: str) -> subprocess.CalledProcessError:
        return subprocess.CalledProcessError(1, ["docker", *command], output="", stderr=stderr)

    def __call__(self, command: list[str]) -> str:
        if command == ["image", "ls", "-q", "--no-trunc"]:
            return "".join(f"{image_id}\n" for image_id in [*self.images, *self.listed_but_gone])
        if command[:2] == ["image", "inspect"] and len(command) == 3:
            image_id = self._resolve(command[2])
            if image_id is None:
                raise self._fail(command, f"Error: No such image: {command[2]}\n")
            image = self.images[image_id]
            return json.dumps(
                [
                    {
                        "Id": image_id,
                        "RepoTags": [name for name in image.names if "@" not in name],
                        "RepoDigests": [name for name in image.names if "@" in name],
                        "Parent": image.parent,
                        "Config": {"Labels": image.labels},
                    }
                ]
            )
        if command == ["ps", "-a", "-q", "--no-trunc"]:
            return "".join(f"{container}\n" for container in self.containers)
        if command[:4] == ["container", "inspect", "--format", "{{.Image}}"]:
            return self.containers[command[4]] + "\n"
        if command[:2] == ["image", "rm"] and len(command) == 3:
            return self._remove(command)
        raise AssertionError(f"unexpected docker call {command}")

    def _remove(self, command: list[str]) -> str:
        if self.before_first_removal is not None:
            hook, self.before_first_removal = self.before_first_removal, None
            hook(self)
        argument = command[2]
        self.removals.append(argument)
        image_id = self._resolve(argument)
        if image_id is None:
            raise self._fail(command, f"Error response from daemon: No such image: {argument}\n")
        if image_id in self.failures:
            raise self._fail(command, self.failures[image_id])
        image = self.images[image_id]
        in_use = image_id in self.containers.values()
        if argument == image_id:
            if not _single_repository(image.names):
                raise self._fail(
                    command,
                    f"Error response from daemon: conflict: unable to delete {image_id} "
                    "(must be forced) - image is referenced in multiple repositories\n",
                )
            if in_use:
                raise self._fail(
                    command,
                    f"Error response from daemon: conflict: unable to delete {image_id} "
                    "(cannot be forced) - image is being used by running container\n",
                )
            del self.images[image_id]
            return f"Deleted: {image_id}\n"
        remaining = [name for name in image.names if name != argument]
        if "@" not in argument and not any(
            "@" not in name and _repository(name) == _repository(argument) for name in remaining
        ):
            remaining = [name for name in remaining if _repository(name) != _repository(argument)]
        if not remaining and in_use:
            raise self._fail(
                command,
                f'Error response from daemon: conflict: unable to remove repository reference "'
                f'{argument}" (must force) - container is using its referenced image {image_id}\n',
            )
        image.names = remaining
        if not remaining:
            del self.images[image_id]
            return f"Untagged: {argument}\nDeleted: {image_id}\n"
        return f"Untagged: {argument}\n"
