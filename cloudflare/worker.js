export default {
  async fetch(request, env) {
    const secret = request.headers.get("X-Proxy-Secret");
    if (!secret || secret !== env.PROXY_SECRET) {
      return new Response("Unauthorized", { status: 401 });
    }

    const url = new URL(request.url);
    const redditUrl = new URL("https://www.reddit.com" + url.pathname);
    url.searchParams.forEach((value, key) => redditUrl.searchParams.set(key, value));

    const response = await fetch(redditUrl.toString(), {
      headers: {
        "User-Agent": env.USER_AGENT || "reddit-scrapper/0.1",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
      },
    });

    const body = await response.text();
    return new Response(body, {
      status: response.status,
      headers: { "Content-Type": "application/json" },
    });
  },
};
