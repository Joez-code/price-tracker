#!/usr/bin/env python3
"""
读 products.json，逐个抓当前价格，追加到 data/prices.json。

设计原则：
  - 单个商品失败不影响其他商品，失败原因记在 status 里供排查。
  - 同一天重复运行会覆盖当天那一条，不会产生重复点。
  - 永远不猜价格。抓不到就记失败，宁可曲线断一天也不写假数据。
"""

import json
import os
import re
import sys
import time
import random
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRODUCTS = os.path.join(ROOT, "products.json")
DATA = os.path.join(ROOT, "data", "prices.json")

# 抓取代理。只有在 products.json 里标了 "proxy": true 的商品才会走它，
# 免得把每月 1000 次的免费额度浪费在本来就能直连的商品上。
# token 从 GitHub Secrets 注入，绝不写进代码或仓库。
SCRAPEDO_TOKEN = os.environ.get("SCRAPEDO_TOKEN", "").strip()
SCRAPEDO_ENDPOINT = "https://api.scrape.do/"


def redact(text):
    """这个仓库是公开的：token 绝不能出现在日志、报错或 prices.json 里。"""
    s = str(text)
    if SCRAPEDO_TOKEN:
        s = s.replace(SCRAPEDO_TOKEN, "<TOKEN>")
    # 以防 token 以其他形式混进 URL
    s = re.sub(r"(token=)[^&\s]+", r"\1<TOKEN>", s)
    return s


def request_url(p):
    """返回 (真正要请求的地址, 是否走代理, 出错原因)。"""
    target = p.get("url")
    if not p.get("proxy"):
        return target, False, None
    if not SCRAPEDO_TOKEN:
        return None, True, "这个商品需要走代理，但仓库里没配置 SCRAPEDO_TOKEN"
    params = {"token": SCRAPEDO_TOKEN, "url": target}
    if p.get("proxy_render"):        # 需要执行 JS 的页面才开，消耗额度更多
        params["render"] = "true"
    if p.get("proxy_geo"):           # 例如 "au"，让代理从澳洲 IP 访问
        params["geoCode"] = p["proxy_geo"]
    return SCRAPEDO_ENDPOINT + "?" + urlencode(params), True, None

# 用悉尼时间给数据点打日期，和看板显示保持一致
SYDNEY = timezone(timedelta(hours=11))

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    # 不要手写 Accept-Encoding。requests 会按自己实际能解压的格式去声明；
    # 手写上 br 而环境里没装 brotli，服务器就会发回解不开的压缩数据，
    # 程序会把二进制当文字去找价格，表现为「页面里找不到价格」。
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    # 真实 Chrome 访问网页时会带上这几个头。少了它们，有些站点的
    # 初级拦截规则会判定为机器人并返回 403。
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

PRICE_RE = re.compile(r"[-+]?[\d,]*\.?\d+")


def to_number(raw):
    """'$1,234.56' -> 1234.56；拿不出数字返回 None。"""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).replace(",", "")
    m = PRICE_RE.search(text)
    if not m:
        return None
    try:
        v = float(m.group(0))
    except ValueError:
        return None
    # 0 或负数几乎一定是解析错了，不是真价格
    return v if v > 0 else None


def walk_jsonld(node, found):
    """递归找 JSON-LD 里的 offers.price / lowPrice。"""
    if isinstance(node, dict):
        for key in ("price", "lowPrice", "highPrice"):
            if key in node:
                v = to_number(node[key])
                if v is not None:
                    found.append((key, v))
        for v in node.values():
            walk_jsonld(v, found)
    elif isinstance(node, list):
        for v in node:
            walk_jsonld(v, found)


def extract_price(html, selector=None):
    """返回 (价格, 用的哪种方法)；都失败返回 (None, 原因)。"""
    soup = BeautifulSoup(html, "html.parser")

    # 1) 商品自己指定的 CSS 选择器优先级最高
    if selector:
        el = soup.select_one(selector)
        if el is not None:
            v = to_number(el.get("content") or el.get_text(" ", strip=True))
            if v is not None:
                return v, "selector"
        return None, "选择器没匹配到元素或取不到数字: %s" % selector

    # 2) JSON-LD 结构化数据，最可靠
    candidates = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        walk_jsonld(data, candidates)
    prices = [v for k, v in candidates if k in ("price", "lowPrice")]
    if prices:
        # 同页多个报价时取最低，通常是实际售价而非套装价
        return min(prices), "json-ld"

    # 3) meta 标签
    for attrs in (
        {"property": "product:price:amount"},
        {"property": "og:price:amount"},
        {"itemprop": "price"},
        {"name": "price"},
    ):
        el = soup.find("meta", attrs=attrs)
        if el is not None:
            v = to_number(el.get("content"))
            if v is not None:
                return v, "meta:%s" % list(attrs.values())[0]

    # 4) 带 itemprop 的可见元素
    el = soup.find(attrs={"itemprop": "price"})
    if el is not None:
        v = to_number(el.get("content") or el.get_text(" ", strip=True))
        if v is not None:
            return v, "itemprop"

    return None, "页面里找不到结构化价格；给这个商品加个 selector 字段试试"


MONEY_RE = re.compile(r"(?:AU\$|NZ\$|US\$|\$|AUD|USD)\s?\d[\d,]*(?:\.\d{1,2})?")


def diagnose(html, limit=12):
    """抓不到价格时，把页面上长得像价格的元素记下来，方便事后写 selector。"""
    soup = BeautifulSoup(html, "html.parser")

    body_text = " ".join(soup.get_text(" ", strip=True).split())
    low = html.lower()
    flags = [w for w in (
        "enable javascript", "captcha", "are you a robot", "access denied",
        "blocked", "cloudflare", "incapsula", "akamai", "bot detection",
        "unusual traffic", "please verify",
    ) if w in low]

    out = {
        "ld_json_blocks": len(soup.find_all("script", attrs={"type": "application/ld+json"})),
        "html_bytes": len(html),
        "title": soup.title.get_text(strip=True)[:120] if soup.title else "",
        "text_head": body_text[:400],
        "flags": flags,
        "candidates": [],
    }
    seen = set()
    for el in soup.find_all(True):
        if el.find(True):                 # 只看最内层元素，跳过容器
            continue
        text = el.get_text(" ", strip=True)
        if not text or len(text) > 40 or not MONEY_RE.search(text):
            continue

        desc = el.name
        if el.get("id"):
            desc += "#" + el["id"]
        if el.get("class"):
            desc += "." + ".".join(el["class"][:3])

        key = (desc, text)
        if key in seen:
            continue
        seen.add(key)

        chain, node = [], el.parent
        for _ in range(3):
            if node is None or node.name in ("html", "[document]"):
                break
            seg = node.name
            if node.get("class"):
                seg += "." + ".".join(node["class"][:2])
            chain.append(seg)
            node = node.parent

        out["candidates"].append({"text": text, "el": desc, "parents": " < ".join(chain)})
        if len(out["candidates"]) >= limit:
            break
    return out


def fetch(url, tries=3, via_proxy=False):
    last = None
    # 走代理时不要带浏览器请求头：伪装交给代理做，我们自己带反而可能干扰它。
    # 代理那头也更慢，超时放宽。
    headers = {"Accept": "*/*"} if via_proxy else HEADERS
    timeout = 90 if via_proxy else 30
    for i in range(tries):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                text = r.text
                # 解压失败的话，拿到的是二进制噪声。早点识破，别让它伪装成
                # 「页面里找不到价格」把人引向错误的方向。
                sample = text[:2000]
                if sample:
                    junk = sum(1 for c in sample
                               if c == "�" or (ord(c) < 32 and c not in "\t\r\n"))
                    if junk / len(sample) > 0.15:
                        return None, ("页面内容解码失败（压缩格式解不开，"
                                      "Content-Encoding: %s）" % r.headers.get("Content-Encoding", "未知"))
                return text, None
            last = "HTTP %d" % r.status_code
            if via_proxy:
                # 这些码来自代理服务本身，不是目标网站
                if r.status_code == 401:
                    last += "（代理拒绝：SCRAPEDO_TOKEN 不对或已失效）"
                    break
                if r.status_code in (402, 429):
                    last += "（代理额度用完了，或请求太频繁）"
                    break
                last += "（经代理访问时目标网站返回的状态码）"
            else:
                if r.status_code in (403, 429):
                    last += "（网站把请求当成机器人挡掉了，给这个商品加 \"proxy\": true 试试）"
                    break          # 反爬拦截，直连重试只会更可疑
                if r.status_code == 404:
                    last += "（页面不存在，商品可能已下架或链接变了）"
                    break
        except requests.RequestException as e:
            last = type(e).__name__ + ": " + redact(str(e))[:140]
        if i < tries - 1:
            time.sleep(2 + random.random() * 3)
    return None, redact(last or "未知错误")


def load_json(path, fallback):
    if not os.path.exists(path):
        return fallback
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError) as e:
        print("读取 %s 失败（当成空的继续）: %s" % (path, e), file=sys.stderr)
        return fallback


def main():
    products = load_json(PRODUCTS, [])
    if not products:
        print("products.json 是空的，没事可做。")
        return 0

    store = load_json(DATA, {})
    series = store.get("series") or {}
    status = {}
    debug = {}

    today = datetime.now(SYDNEY).strftime("%Y-%m-%d")
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    ok_count = 0
    proxy_calls = 0
    for idx, p in enumerate(products):
        pid = p.get("id")
        url = p.get("url")
        if not pid or not url:
            print("跳过第 %d 个：缺 id 或 url" % idx)
            continue

        if idx:
            time.sleep(3 + random.random() * 4)   # 别把人家站点打疼

        req_url, via_proxy, cfg_err = request_url(p)
        if cfg_err:
            status[pid] = {"ok": False, "error": cfg_err, "at": now_iso, "proxy": True}
            print("✗ %-28s %s" % (pid, cfg_err))
            continue

        html, err = fetch(req_url, via_proxy=via_proxy)
        if html is None:
            status[pid] = {"ok": False, "error": err, "at": now_iso, "proxy": via_proxy}
            print("✗ %-28s %s%s" % (pid, "[代理] " if via_proxy else "", err))
            continue

        price, how = extract_price(html, p.get("selector"))

        # 合理性检查：抓到的数字落在设定区间外，多半是认错了元素
        # （抓到了套装价、分期总额、或者页面上别的商品）。
        # 宁可当作失败并记下诊断，也不要往曲线里塞一个假价格。
        if price is not None:
            lo, hi = p.get("price_min"), p.get("price_max")
            if (lo is not None and price < lo) or (hi is not None and price > hi):
                how = ("抓到 %.2f，超出设定的合理区间 %s–%s，判定为认错元素"
                       % (price, lo if lo is not None else "不限",
                          hi if hi is not None else "不限"))
                price = None

        if price is None:
            status[pid] = {"ok": False, "error": how, "at": now_iso, "proxy": via_proxy}
            try:
                debug[pid] = diagnose(html)
            except Exception as e:                      # 诊断失败也不能影响主流程
                debug[pid] = {"diagnose_error": str(e)[:200]}
            print("✗ %-28s %s" % (pid, how))
            print("   页面里像价格的元素：%s" % json.dumps(
                debug[pid].get("candidates", [])[:6], ensure_ascii=False))
            continue

        points = [pt for pt in series.get(pid, []) if pt.get("ts") != today]
        points.append({"ts": today, "price": round(price, 2)})
        points.sort(key=lambda x: x["ts"])
        series[pid] = points[-400:]          # 只留最近 400 天

        status[pid] = {"ok": True, "method": how, "at": now_iso, "proxy": via_proxy}
        ok_count += 1
        proxy_calls += 1 if via_proxy else 0
        print("✓ %-28s %8.2f  (%s%s)" % (pid, price, how, "，经代理" if via_proxy else ""))

    store["series"] = series
    store["status"] = status
    store["updated"] = now_iso
    if debug:
        store["debug"] = debug
    else:
        store.pop("debug", None)      # 全部成功时清掉上一轮的诊断信息

    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    blob = json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True)
    # 最后一道防线：这个仓库是公开的，token 绝不能写进去。
    if SCRAPEDO_TOKEN and SCRAPEDO_TOKEN in blob:
        print("中止：输出里含有代理 token，拒绝写入文件。", file=sys.stderr)
        return 1
    with open(DATA, "w", encoding="utf-8") as f:
        f.write(blob + "\n")

    print("\n成功 %d / %d 个商品。本次用掉代理额度 %d 次。" % (ok_count, len(products), proxy_calls))
    # 即使全失败也返回 0：一次抓取失败不该让 Actions 报红，
    # 失败详情已经写进 status，看板上会标出来。
    return 0


if __name__ == "__main__":
    sys.exit(main())
