# -*- coding: utf-8 -*-
"""
twbook.cc 小说下载器
- 抓取章节 HTML
- 解码混淆字体（应用 font_decode_map.json）
- 清除广告/脚本，仅保留章节标题 + 正文
- 合并为单个 TXT
"""
import re, json, time, sys, io, html as ihtml, random
import requests
from bs4 import BeautifulSoup

BASE = "https://www.twbook.cc"
BOOK = "100786922"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": BASE + "/" + BOOK + "/",
}

with io.open("font_decode_map.json", encoding="utf-8") as f:
    DECODE_MAP = json.load(f)

def decode_text(s):
    return "".join(DECODE_MAP.get(ch, ch) for ch in s)

def fetch(url, retries=4):
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            r.encoding = "utf-8"
            if r.status_code == 200:
                return r.text
            print("  status %d, backing off" % r.status_code)
        except Exception as e:
            print("  retry %d: %s" % (i+1, e))
        # 失败退避：随机递增等待，降低被封概率
        time.sleep(random.uniform(5, 10) * (i + 1))
    return None

def parse_chapter(html):
    soup = BeautifulSoup(html, "html.parser")
    # 标题
    h1 = soup.select_one("h1.imgtext")
    title = decode_text(h1.get_text(strip=True)) if h1 else ""
    # 正文容器
    content = soup.select_one("div.content")
    if not content:
        return title, ""
    # 移除广告块与脚本
    for tag in content.select(".adBlock, script, style, ins"):
        tag.decompose()
    paras = []
    for p in content.find_all("p"):
        txt = p.get_text()
        txt = ihtml.unescape(txt)
        txt = decode_text(txt).strip()
        # 过滤温馨提示等干扰
        if not txt:
            continue
        if "網站即將改版" in txt or "网站即将改版" in txt:
            continue
        paras.append(txt)
    return title, "\n".join(paras)

def find_next(html):
    """从页面里找“下一章”链接，返回相对路径或None"""
    soup = BeautifulSoup(html, "html.parser")
    a = soup.find("a", string=re.compile("下一[章页]"))
    if a and a.get("href"):
        return a["href"]
    # 备用：id/class 命名
    a = soup.select_one("#next, a.next")
    if a and a.get("href"):
        return a["href"]
    return None

def main():
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 187
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    outfile = sys.argv[3] if len(sys.argv) > 3 else "林七夜_%d-%d.txt" % (start, start+count-1)

    url = "%s/%s/%d.html" % (BASE, BOOK, start)
    # 增量写入：每抓一章立即落盘，中途失败也不丢已下载内容
    f = io.open(outfile, "w", encoding="utf-8")
    done = 0
    for n in range(count):
        print("[%d/%d] fetching %s" % (n+1, count, url))
        html = fetch(url)
        if not html:
            print("  FAILED after retries, stop.")
            break
        title, body = parse_chapter(html)
        print("   -> %s (%d chars)" % (title, len(body)))
        f.write(title + "\n\n")
        f.write(body + "\n\n\n")
        f.flush()
        done += 1
        nxt = find_next(html)
        if not nxt:
            print("  no next link, stop.")
            break
        url = nxt if nxt.startswith("http") else BASE + nxt
        # 随机延时 2~6 秒，模拟人工阅读节奏，避免被封
        delay = random.uniform(2.0, 6.0)
        # 每抓一定数量章节，偶尔来个更长的停顿
        if (n + 1) % random.randint(15, 25) == 0:
            delay += random.uniform(8.0, 20.0)
            print("   (long pause %.1fs)" % delay)
        time.sleep(delay)

    f.close()
    print("DONE. %d chapters -> %s" % (done, outfile))

if __name__ == "__main__":
    main()
