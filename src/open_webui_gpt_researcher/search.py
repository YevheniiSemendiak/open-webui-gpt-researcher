from __future__ import annotations

from dataclasses import dataclass

import httpx


class PublicSearchError(RuntimeError):
    pass


@dataclass
class SearxClient:
    """Small adapter for SearXNG's JSON search API."""

    base_url: str
    timeout: float = 30.0

    async def search(
        self, *, query: str, max_results: int = 5, domains: list[str] | None = None
    ) -> list[dict[str, str]]:
        if domains:
            domain_filter = " OR ".join(f"site:{domain}" for domain in domains)
            query = f"{query} ({domain_filter})"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.base_url.rstrip('/')}/search",
                    params={"q": query, "format": "json"},
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise PublicSearchError(f"SearXNG search failed: {error}") from error
        raw_results = payload.get("results", []) if isinstance(payload, dict) else []
        results: list[dict[str, str]] = []
        for item in raw_results if isinstance(raw_results, list) else []:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url:
                continue
            results.append(
                {
                    "url": url,
                    "href": url,
                    "title": str(item.get("title") or url)[:1_000],
                    "body": str(item.get("content") or "")[:2_000],
                }
            )
            if len(results) >= max_results:
                break
        return results
