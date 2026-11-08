"""Fresh path imports for tests that cannot share a service's package name."""

import importlib.util
from os import PathLike
import sys
from types import ModuleType


def load_module(name: str, path: str | PathLike[str]) -> ModuleType:
    """Execute a fresh module, registered while decorators resolve annotations.

    Every call replaces the previous successful import so per-test patches cannot
    leak through a module cache. A failed import restores the previous registration.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module {name!r} from {path!s}")
    module = importlib.util.module_from_spec(spec)
    missing = object()
    previous = sys.modules.get(name, missing)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if previous is missing:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        raise
    return module
