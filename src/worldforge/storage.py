"""Bounded self-contained run artifacts with atomic, non-overwriting publication."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Protocol

from worldforge.models import Run

MAX_RUN_BYTES = 32 * 1024 * 1024
_RUN_ID = re.compile(r"WF-[0-9a-f]{32}\Z")


class RunStore(Protocol):
    def save(self, run: Run) -> Path: ...

    def load(self, run_id: str) -> Run: ...


class JsonRunStore:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def _path(self, run_id: str) -> Path:
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("run_id must have the form WF- followed by 32 lowercase hex digits")
        return self.directory / f"{run_id}.json"

    def save(self, run: Run) -> Path:
        destination = self._path(run.run_id)
        payload = run.model_dump_json(by_alias=True, indent=2).encode("utf-8")
        if len(payload) > MAX_RUN_BYTES:
            raise ValueError("run artifact exceeds the 32 MiB limit")
        self.directory.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".run-", suffix=".tmp", dir=self.directory)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # link publishes a complete file atomically and fails if the name already exists.
            # rename/replace would silently overwrite another process's completed run.
            os.link(temporary, destination)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def load(self, run_id: str) -> Run:
        source = self._path(run_id)
        # Refuse symlinks so run identifiers cannot redirect reads outside this store.
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            file_stat = os.fstat(stream.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise ValueError("run artifact must be a regular file")
            if file_stat.st_size > MAX_RUN_BYTES:
                raise ValueError("run artifact exceeds the 32 MiB limit")
            payload = stream.read(MAX_RUN_BYTES + 1)
        if len(payload) > MAX_RUN_BYTES:
            raise ValueError("run artifact exceeds the 32 MiB limit")
        run = Run.model_validate_json(payload)
        if run.run_id != run_id:
            raise ValueError("stored run identifier does not match its filename")
        return run
