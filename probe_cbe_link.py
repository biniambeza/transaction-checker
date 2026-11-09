"""One-time helper: discovers how CBE's new receipt link (mbreciept.cbe.com.et) loads its data.

    .venv\\Scripts\\python.exe probe_cbe_link.py https://mbreciept.cbe.com.et/v2-xxxxxxxx

Writes everything it finds to probe_cbe.txt (send that file's contents to Claude).
"""
import asyncio
import re
import sys
from urllib.parse import urljoin

import aiohttp

OUT = "probe_cbe.txt"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130 Safari/537.36"}


async def fetch(session, url):
    try:
        async with session.get(url, ssl=False, allow_redirects=True) as r:
            body = await r.read()
            return r.status, r.headers.get("Content-Type", ""), str(r.url), body
    except Exception as e:  # noqa: BLE001
        return None, "", url, repr(e).encode()


async def main(link: str) -> None:
    lines = []
    log = lines.append
    token = link.rstrip("/").rsplit("/", 1)[-1]
    async with aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as s:
        status, ctype, final, body = await fetch(s, link)
        log(f"== PAGE {link}\nstatus={status} type={ctype} final_url={final} size={len(body)}")
        page = body.decode("utf-8", "ignore")
        log(page[:4000])

        scripts = re.findall(r"<script[^>]+src=[\"']([^\"']+)", page, re.I)
        for src in scripts:
            js_url = urljoin(final, src)
            st, ct, _, js = await fetch(s, js_url)
            text = js.decode("utf-8", "ignore")
            log(f"\n== SCRIPT {js_url} status={st} size={len(text)}")
            found = set()
            for pat in (r"https?://[A-Za-z0-9._:/\-?=&%]+", r"[\"'`](/[A-Za-z0-9_\-/]*(?:api|receipt|reciept|v\d)[A-Za-z0-9_\-/]*)[\"'`]",
                        r"fetch\([^)]{0,200}\)", r"axios\.[a-z]+\([^)]{0,200}\)", r"baseURL[^,;]{0,200}"):
                for m in re.finditer(pat, text, re.I):
                    found.add(m.group(0)[:300])
            for f in sorted(found):
                log("   " + f)
            for kw in ("receipt", "reciept", "api", "fetch"):
                for m in list(re.finditer(kw, text, re.I))[:15]:
                    log(f"   ...{text[max(0, m.start() - 150): m.end() + 150]!r}")

        # Common guesses for where the data lives
        log("\n== GUESSES")
        base = final.split("/v2-")[0] if "/v2-" in final else final.rsplit("/", 1)[0]
        for guess in (f"{base}/api/{token}", f"{base}/api/receipt/{token}", f"{base}/api/v1/receipt/{token}",
                      f"{base}/receipt/{token}", f"{base}/{token}.pdf", f"{base}/api/receipts/{token}"):
            st, ct, _, b = await fetch(s, guess)
            log(f"{guess} -> {st} {ct} {b[:300]!r}")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Done. Results saved to {OUT}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1]))
