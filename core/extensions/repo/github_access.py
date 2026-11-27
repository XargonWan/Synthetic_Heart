"""The GitHub access token of the Core: one setting for every GitHub repository.

A token lets the Core read a private repository and lifts GitHub's anonymous download
limit. It is configured once (Settings > Extensions) and handed only to
``GithubReleaseTransport``, never to an extension and never to a non-GitHub host.
"""

from __future__ import annotations

TOKEN_KEY = "GITHUB_TOKEN"

#: Where to enter the token, as shown to the user when GitHub refuses a download.
WHERE = "Settings > Extensions > GitHub access token"

HINT = (
    "GitHub refused the request (its download limit was probably reached, or the "
    f"repository is private). Enter a GitHub access token in {WHERE} to raise the "
    "limit and to read private repositories."
)

HINT_WITH_TOKEN = (
    "GitHub refused the request even with the configured token (limit reached, expired "
    f"token, or no access to this repository). Check the token in {WHERE}."
)


def register() -> None:
    """Declare the setting so it appears in Settings. Never raises."""
    github_token()


def github_token() -> str:
    """The configured token, or an empty string. Never raises (the CLI has no database)."""
    try:
        from core.config_manager import config_registry

        value = config_registry.get_value(
            TOKEN_KEY,
            "",
            label="GitHub access token",
            description=(
                "A GitHub personal access token (read-only is enough: 'Contents: read' "
                "or the 'repo' scope for private repositories). Used to download "
                "extensions from GitHub repositories: it lets SyntH read private "
                "repositories and raises GitHub's download limit. Stored masked; "
                "sent only to github.com and api.github.com."
            ),
            value_type=str,
            group="extensions",
            component="extensions",
            sensitive=True,
        )
        return str(value or "").strip()
    except Exception:  # noqa: BLE001 - a missing token is not an error
        return ""
