// Routes anonymous Reddit requests through a residential scraping-proxy service
// (e.g. ScraperAPI) so they egress from a residential IP that Reddit does not
// block, instead of the datacenter IP of this Worker.
//
// Required env:
//   PROXY_SECRET     shared secret the bot sends in X-Proxy-Secret
//   SCRAPER_API_KEY  API key of the scraping-proxy service
// Optional env:
//   SCRAPER_BASE_URL endpoint of the service (default ScraperAPI)
//   USER_AGENT       User-Agent forwarded to Reddit

const DEFAULT_SCRAPER_BASE = "https://api.scraperapi.com/";

function buildScraperUrl(env, targetUrl) {
  const base = new URL(env.SCRAPER_BASE_URL || DEFAULT_SCRAPER_BASE);
  base.searchParams.set("api_key", env.SCRAPER_API_KEY);
  base.searchParams.set("url", targetUrl);
  return base.toString();
}

async function fetchViaProxy(env, targetUrl) {
  return fetch(buildScraperUrl(env, targetUrl), {
    headers: {
      "User-Agent": env.USER_AGENT || "reddit-scrapper/0.1",
      Accept: "application/json",
      "Accept-Language": "en-US,en;q=0.9",
    },
  });
}

export default {
  async fetch(request, env) {
    const secret = request.headers.get("X-Proxy-Secret");
    if (!secret || secret !== env.PROXY_SECRET) {
      return new Response("Unauthorized", { status: 401 });
    }

    if (!env.SCRAPER_API_KEY) {
      return new Response(JSON.stringify({ error: "SCRAPER_API_KEY not configured" }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      });
    }

    const url = new URL(request.url);
    const redditUrl = new URL("https://www.reddit.com" + url.pathname);
    url.searchParams.forEach((value, key) => redditUrl.searchParams.set(key, value));
    const target = redditUrl.toString();

    let response = await fetchViaProxy(env, target);

    // On 429/5xx do a single short backoff retry.
    if (response.status === 429 || response.status >= 500) {
      const retryAfter = parseInt(response.headers.get("Retry-After") || "2", 10);
      await new Promise((r) => setTimeout(r, Math.min(retryAfter, 5) * 1000));
      response = await fetchViaProxy(env, target);
    }

    const body = await response.text();
    return new Response(body, {
      status: response.status,
      headers: { "Content-Type": "application/json" },
    });
  },
};
