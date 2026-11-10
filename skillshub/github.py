"""Client GitHub (httpx) : REST, ETag, gestion du quota et retries."""

from __future__ import annotations

import time
from typing import Any, Callable
from urllib.parse import quote

import httpx

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"


class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub {status} : {message}")
        self.status = status


class NotFound(GitHubError):
    pass


class GitHubClient:
    """Petit client REST.

    - `get(..., etag=...)` envoie `If-None-Match` ; une réponse 304 ne consomme pas de quota.
    - Quota épuisé (403/429 avec `X-RateLimit-Remaining: 0` ou `Retry-After`) : pause
      automatique jusqu'à la remise à zéro, puis nouvel essai.
    - Erreurs réseau et 5xx : retries avec délai exponentiel.
    """

    def __init__(self, token: str = "", *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, max_retries: int = 4,
                 max_wait: float = 3700.0, log: Callable[[str], None] | None = None):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "skills-hub-crawler",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.token = token
        self.http = httpx.Client(
            headers=headers, timeout=30.0, follow_redirects=True, transport=transport
        )
        self.sleep = sleep
        self.max_retries = max_retries
        self.max_wait = max_wait
        self.log = log or (lambda _msg: None)
        self.remaining: int | None = None  # quota « core » restant
        self.reset_at: float | None = None
        self.requests = 0

    def close(self) -> None:
        self.http.close()

    # ------------------------------------------------------------------
    # Requête bas niveau
    # ------------------------------------------------------------------

    def _track(self, resp: httpx.Response) -> None:
        if resp.headers.get("x-ratelimit-resource", "core") != "core":
            return
        try:
            self.remaining = int(resp.headers["x-ratelimit-remaining"])
            self.reset_at = float(resp.headers["x-ratelimit-reset"])
        except (KeyError, ValueError):
            pass

    def _rate_limit_wait(self, resp: httpx.Response) -> float | None:
        """Durée d'attente si la réponse signale un quota épuisé, sinon None."""
        if resp.status_code not in (403, 429):
            return None
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                return float(retry_after) + 1
            except ValueError:
                return 60.0
        if resp.headers.get("x-ratelimit-remaining") == "0":
            try:
                reset = float(resp.headers.get("x-ratelimit-reset", "0"))
            except ValueError:
                reset = 0.0
            return max(1.0, reset - time.time() + 2)
        if "rate limit" in resp.text.lower():
            return 60.0
        return None

    def request(self, url: str, *, params: dict[str, Any] | None = None,
                etag: str | None = None, accept: str | None = None) -> httpx.Response:
        if not url.startswith("http"):
            url = API + url
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if accept:
            headers["Accept"] = accept
        attempt = 0
        waits = 0
        while True:
            try:
                self.requests += 1
                resp = self.http.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                attempt += 1
                if attempt > self.max_retries:
                    raise GitHubError(0, f"erreur réseau : {exc}") from exc
                self.sleep(min(60.0, 2.0 ** attempt))
                continue
            self._track(resp)
            if resp.status_code in (200, 304):
                return resp
            wait = self._rate_limit_wait(resp)
            if wait is not None:
                waits += 1
                if wait > self.max_wait or waits > 3:
                    raise GitHubError(resp.status_code, "quota GitHub épuisé")
                self.log(f"  quota GitHub atteint, pause de {int(wait)} s…")
                self.sleep(wait)
                continue
            if resp.status_code >= 500:
                attempt += 1
                if attempt > self.max_retries:
                    raise GitHubError(resp.status_code, "erreur serveur persistante")
                self.sleep(min(60.0, 2.0 ** attempt))
                continue
            if resp.status_code in (404, 410, 451):
                raise NotFound(resp.status_code, url)
            raise GitHubError(resp.status_code, resp.text[:200])

    # ------------------------------------------------------------------
    # Points d'accès utilisés par le crawler
    # ------------------------------------------------------------------

    def get_repo(self, full_name: str, etag: str | None = None
                 ) -> tuple[dict[str, Any] | None, str | None]:
        """Retourne (données, etag). `données` vaut None si la réponse est 304."""
        resp = self.request(f"/repos/{full_name}", etag=etag)
        if resp.status_code == 304:
            return None, etag
        return resp.json(), resp.headers.get("etag")

    def get_tree(self, full_name: str, ref: str) -> dict[str, Any]:
        resp = self.request(
            f"/repos/{full_name}/git/trees/{quote(ref, safe='')}", params={"recursive": "1"}
        )
        return resp.json()

    def get_file(self, full_name: str, ref: str, path: str, sha: str | None = None) -> str:
        """Contenu texte d'un fichier. raw.githubusercontent.com ne consomme pas de quota API ;
        en cas d'échec, repli sur l'API des blobs."""
        try:
            resp = self.request(f"{RAW}/{full_name}/{quote(ref, safe='/')}/{quote(path, safe='/')}")
            return resp.text
        except GitHubError:
            if not sha:
                raise
        resp = self.request(
            f"/repos/{full_name}/git/blobs/{sha}", accept="application/vnd.github.raw+json"
        )
        return resp.text

    def get_readme(self, full_name: str) -> str:
        resp = self.request(
            f"/repos/{full_name}/readme", accept="application/vnd.github.raw+json"
        )
        return resp.text

    def search(self, kind: str, query: str, page: int = 1, per_page: int = 100,
               sort: str | None = None) -> dict[str, Any]:
        """Recherche `code` ou `repositories`."""
        params: dict[str, Any] = {"q": query, "page": page, "per_page": per_page}
        if sort:
            params["sort"] = sort
        return self.request(f"/search/{kind}", params=params).json()
