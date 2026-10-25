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

import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRODUCTS = os.path.join(ROOT, "products.json")
DATA = os.path.join(ROOT, "data", "prices.json")

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


def fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
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
            # 403/429 基本是反爬，重试没用，早点放弃
            if r.status_code in (403, 404, 429):
                break
        except requests.RequestException as e:
            last = type(e).__name__ + ": " + str(e)[:140]
        if i < tries - 1:
            time.sleep(2 + random.random() * 3)
    return None, last or "未知错误"


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
    for idx, p in enumerate(products):
        pid = p.get("id")
        url = p.get("url")
        if not pid or not url:
            print("跳过第 %d 个：缺 id 或 url" % idx)
            continue

        if idx:
            time.sleep(3 + random.random() * 4)   # 别把人家站点打疼

        html, err = fetch(url)
        if html is None:
            status[pid] = {"ok": False, "error": err, "at": now_iso}
            print("✗ %-28s %s" % (pid, err))
            continue

        price, how = extract_price(html, p.get("selector"))
        if price is None:
            status[pid] = {"ok": False, "error": how, "at": now_iso}
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

        status[pid] = {"ok": True, "method": how, "at": now_iso}
        ok_count += 1
        print("✓ %-28s %8.2f  (%s)" % (pid, price, how))

    store["series"] = series
    store["status"] = status
    store["updated"] = now_iso
    if debug:
        store["debug"] = debug
    else:
        store.pop("debug", None)      # 全部成功时清掉上一轮的诊断信息

    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")

    print("\n成功 %d / %d 个商品。" % (ok_count, len(products)))
    # 即使全失败也返回 0：一次抓取失败不该让 Actions 报红，
    # 失败详情已经写进 status，看板上会标出来。
    return 0


if __name__ == "__main__":
    sys.exit(main())
