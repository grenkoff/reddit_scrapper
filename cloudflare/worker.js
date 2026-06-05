// Cached OAuth token, shared across requests within the same isolate.
let tokenCache = { value: null, expiresAt: 0 };

async function getToken(env) {
  const now = Date.now();
  if (tokenCache.value && now < tokenCache.expiresAt - 60_000) {
    return tokenCache.value;
  }

  const auth = btoa(`${env.REDDIT_CLIENT_ID}:${env.REDDIT_CLIENT_SECRET}`);
  const response = await fetch("https://www.reddit.com/api/v1/access_token", {
    method: "POST",
    headers: {
      Authorization: `Basic ${auth}`,
      "Content-Type": "application/x-www-form-urlencoded",
      "User-Agent": env.USER_AGENT || "reddit-scrapper/0.1",
    },
    body: "grant_type=client_credentials",
  });

  if (!response.ok) {
    throw new Error(`OAuth token request failed: ${response.status}`);
  }

  const data = await response.json();
  tokenCache = {
    value: data.access_token,
    expiresAt: now + data.expires_in * 1000,
  };
  return tokenCache.value;
}

async function fetchReddit(redditUrl, token, env) {
  return fetch(redditUrl, {
    headers: {
      Authorization: `Bearer ${token}`,
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

    const url = new URL(request.url);
    // Authenticated requests go to oauth.reddit.com instead of www.reddit.com.
    const redditUrl = new URL("https://oauth.reddit.com" + url.pathname);
    url.searchParams.forEach((value, key) => redditUrl.searchParams.set(key, value));

    let token;
    try {
      token = await getToken(env);
    } catch (err) {
      return new Response(JSON.stringify({ error: String(err) }), {
        status: 502,
        headers: { "Content-Type": "application/json" },
      });
    }

    let response = await fetchReddit(redditUrl.toString(), token, env);

    // On 401 the token may be stale — refresh once and retry.
    if (response.status === 401) {
      tokenCache = { value: null, expiresAt: 0 };
      token = await getToken(env);
      response = await fetchReddit(redditUrl.toString(), token, env);
    }

    // On 429/5xx do a single short backoff retry.
    if (response.status === 429 || response.status >= 500) {
      const retryAfter = parseInt(response.headers.get("Retry-After") || "2", 10);
      await new Promise((r) => setTimeout(r, Math.min(retryAfter, 5) * 1000));
      response = await fetchReddit(redditUrl.toString(), token, env);
    }

    const body = await response.text();
    return new Response(body, {
      status: response.status,
      headers: { "Content-Type": "application/json" },
    });
  },
};
