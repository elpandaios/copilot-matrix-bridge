"""Auto-discover projects (git repos) under a root directory."""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class ProjectDiscovery:
    def __init__(self, projects_root: str):
        self.projects_root = Path(projects_root)

    def list_projects(self) -> list[str]:
        """Return sorted list of project directory names that contain a .git folder."""
        if not self.projects_root.exists():
            logger.warning("Projects root does not exist: %s", self.projects_root)
            return []

        projects = []
        for child in sorted(self.projects_root.iterdir()):
            if child.is_dir() and (child / ".git").exists():
                projects.append(child.name)
        return projects

    def resolve(self, name: str) -> Optional[str]:
        """Resolve a project name to its full path. Case-insensitive match."""
        name_lower = name.lower().strip()
        for child in self.projects_root.iterdir():
            if child.is_dir() and child.name.lower() == name_lower:
                if (child / ".git").exists():
                    return str(child)
        return None
