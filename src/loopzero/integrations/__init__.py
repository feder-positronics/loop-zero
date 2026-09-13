"""External service adapters used by portable mechanisms."""

from .github import GitHub, GitHubError, GitHubSettings, Repository

__all__ = ["GitHub", "GitHubError", "GitHubSettings", "Repository"]
