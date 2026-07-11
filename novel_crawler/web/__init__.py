"""Web UI 管理界面：基于标准库 http.server，无额外依赖。"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from novel_crawler.core.crawler import CrawlerService
from novel_crawler.runtime.env import RuntimeContext

HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>小说爬虫管理</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: system-ui, sans-serif; background: #f5f5f5; color: #333; }
.container { max-width: 960px; margin: 0 auto; padding: 20px; }
h1 { margin-bottom: 20px; color: #1a1a1a; }
.card { background: #fff; border-radius: 8px; padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #eee; }
th { background: #fafafa; font-weight: 600; }
.btn { display: inline-block; padding: 6px 16px; background: #4f46e5; color: #fff; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; }
.btn:hover { background: #4338ca; }
.btn-sm { padding: 4px 10px; font-size: 12px; }
.btn-danger { background: #dc2626; }
.btn-danger:hover { background: #b91c1c; }
.input { padding: 8px 12px; border: 1px solid #ddd; border-radius: 4px; width: 100%; margin-bottom: 10px; }
.progress { background: #e0e0e0; border-radius: 4px; height: 8px; overflow: hidden; }
.progress-bar { background: #4f46e5; height: 100%; transition: width 0.3s; }
.status-done { color: #16a34a; }
.status-failed { color: #dc2626; }
.status-pending { color: #d97706; }
.stats { display: flex; gap: 20px; margin-bottom: 16px; }
.stat { background: #fff; padding: 16px 24px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
.stat-num { font-size: 28px; font-weight: 700; }
.stat-label { font-size: 13px; color: #666; }
pre { background: #f8f8f8; padding: 12px; border-radius: 4px; overflow-x: auto; font-size: 13px; }
</style>
</head>
<body>
<div class="container">
<h1>小说爬虫管理</h1>
<div id="app"></div>
</div>
<script>
function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

async function api(path) {
  const res = await fetch('/api' + path);
  return res.json();
}

async function refresh() {
  const data = await api('/books');
  const app = document.getElementById('app');
  let html = '<div class="stats">';
  html += `<div class="stat"><div class="stat-num">${data.books.length}</div><div class="stat-label">书籍</div></div>`;
  const totalDone = data.books.reduce((s,b) => s + (b.done||0), 0);
  const totalChap = data.books.reduce((s,b) => s + (b.total||0), 0);
  html += `<div class="stat"><div class="stat-num">${totalDone}/${totalChap}</div><div class="stat-label">已下载章节</div></div>`;
  html += '</div>';

  html += '<div class="card"><h3>书籍列表</h3><table><thead><tr><th>ID</th><th>标题</th><th>站点</th><th>进度</th><th>操作</th></tr></thead><tbody>';
  for (const b of data.books) {
    const pct = b.total ? Math.round(b.done / b.total * 100) : 0;
    html += `<tr><td>${b.id}</td><td>${esc(b.title)}</td><td>${esc(b.site)}</td>`;
    html += `<td><div class="progress"><div class="progress-bar" style="width:${pct}%"></div></div><small>${b.done||0}/${b.total||0} (${pct}%)</small></td>`;
    html += `<td><button class="btn btn-sm" onclick="showDetail(${b.id})">详情</button> `;
    html += `<button class="btn btn-sm" onclick="exportBook(${b.id})">导出</button> `;
    html += `<button class="btn btn-sm btn-danger" onclick="deleteBook(${b.id})">删除</button></td></tr>`;
  }
  html += '</tbody></table></div>';

  html += `<div class="card"><h3>抓取新书</h3>`;
  html += `<input class="input" id="crawl-url" placeholder="小说首页URL">`;
  html += `<button class="btn" onclick="crawlNew()">开始抓取</button></div>`;
  app.innerHTML = html;
}

async function showDetail(id) {
  const data = await api('/books/' + id + '/report');
  alert(data.report);
}

async function exportBook(id) {
  const data = await api('/books/' + id + '/export?format=txt');
  alert('导出完成: ' + data.path);
}

async function deleteBook(id) {
  if (!confirm('确认删除?')) return;
  await api('/books/' + id + '/delete', { method: 'POST' });
  refresh();
}

async function crawlNew() {
  const url = document.getElementById('crawl-url').value;
  if (!url) return;
  alert('已在后台开始抓取，请等待几秒后刷新查看');
  const data = await api('/crawl?url=' + encodeURIComponent(url));
  alert('book_id: ' + data.book_id);
  refresh();
}

refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>"""


class WebHandler(BaseHTTPRequestHandler):
    service: CrawlerService = None

    def _json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text):
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/" or path == "":
            self._html(HTML_PAGE)
            return

        if path == "/api/books":
            books = self.service.list_books()
            self._json({"books": books})
            return

        if path.startswith("/api/books/") and path.endswith("/report"):
            book_id = int(path.split("/")[3])
            self._json({"report": self.service.report(book_id)})
            return

        if path.startswith("/api/books/") and path.endswith("/export"):
            book_id = int(path.split("/")[3])
            fmt = qs.get("format", ["txt"])[0]
            try:
                path_out = self.service.export(book_id, fmt)
                self._json({"path": str(path_out)})
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
            return

        if path == "/api/crawl":
            url = qs.get("url", [""])[0]
            if not url:
                self._json({"error": "missing url"}, 400)
                return
            try:
                book_id = self.service.crawl(url, export=False)
                self._json({"book_id": book_id})
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
            return

        self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/books/") and path.endswith("/delete"):
            book_id = int(path.split("/")[3])
            self.service.delete_book(book_id)
            self._json({"ok": True})
            return

        self._json({"error": "not found"}, 404)

    def log_message(self, *args):
        pass


def run_web_ui(ctx: RuntimeContext, host: str = "127.0.0.1", port: int = 8765) -> None:
    WebHandler.service = CrawlerService(ctx)
    server = ThreadingHTTPServer((host, port), WebHandler)
    print(f"Web UI 启动: http://{host}:{port}")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.shutdown()
