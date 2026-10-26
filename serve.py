"""
小红书收藏夹本地查看器 v2
=========================
用法：python serve.py  →  浏览器打开 http://127.0.0.1:5000

功能：
  - 搜索框一键搜（标题 / 正文 tag / 自己打的 my_tags / 作者）
  - 勾选「只搜标签」后只在 tag 里搜
  - 卡片墙展示封面（懒下载，第一次打开时缓存到 covers/ 目录）
  - 点卡片打开编辑弹窗；弹窗里可跳转小红书原链接
  - 弹窗内编辑「我的标签」、软删除记录
  - 「回收站」勾选框查看/恢复已删除记录
"""
import json
import sqlite3
import urllib.request
from pathlib import Path

from flask import Flask, Response, g, jsonify, request

HERE = Path(__file__).parent
DB_PATH = HERE / "xhs.db"
COVERS_DIR = HERE / "covers"
COVERS_DIR.mkdir(exist_ok=True)

app = Flask(__name__)


# ──────────────────────── 数据库 ────────────────────────

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(str(DB_PATH))
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def parse_tags(raw):
    """库里 tags / my_tags 是 JSON 字符串如 '["#钩织", "#手工"]'，剥掉 # 前缀。"""
    if not raw:
        return []
    try:
        return [t.lstrip("#").strip() for t in json.loads(raw) if t.strip()]
    except Exception:
        return []


# ──────────────────────── 接口 ────────────────────────

@app.route("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    tag_only = request.args.get("tag_only") == "1"
    include_deleted = request.args.get("include_deleted") == "1"
    kws = q.split()

    where = []
    if not include_deleted:
        where.append("(status IS NULL OR status != 'deleted')")
    params = []
    if kws:
        conds = []
        n = 2 if tag_only else 4
        for _ in kws:
            if tag_only:
                conds.append("(tags LIKE ? OR my_tags LIKE ?)")
            else:
                conds.append("(title LIKE ? OR tags LIKE ? OR my_tags LIKE ? OR author LIKE ?)")
        for kw in kws:
            if tag_only:
                params += [f"%{kw}%", f"%{kw}%"]
            else:
                params += [f"%{kw}%", f"%{kw}%", f"%{kw}%", f"%{kw}%"]
        where.append("(" + " OR ".join(conds) + ")")

    sql = (
        "SELECT note_id, title, author, likes, tags, my_tags, "
        "publish_date, url_token, cover_url, detail_done, status "
        "FROM notes WHERE " + " AND ".join(where) +
        " ORDER BY detail_done DESC, likes DESC, crawled_at DESC LIMIT 200"
    )
    rows = get_db().execute(sql, params).fetchall()

    items = []
    for r in rows:
        items.append({
            "note_id": r["note_id"],
            "title": r["title"] or "(无标题)",
            "author": r["author"] or "",
            "likes": r["likes"] or 0,
            "tags": parse_tags(r["tags"]),
            "my_tags": parse_tags(r["my_tags"]),
            "date": (r["publish_date"] or "").strip(),
            "url": "https://www.xiaohongshu.com" + (r["url_token"] or ""),
            "has_cover": bool(r["cover_url"]),
            "deleted": r["status"] == "deleted",
        })
    return jsonify({"total": len(items), "items": items})


@app.route("/api/note/<note_id>/my_tags", methods=["PUT"])
def set_my_tags(note_id):
    data = request.get_json(force=True)
    tags = [t.strip() for t in data.get("my_tags", []) if t.strip()]
    db = get_db()
    row = db.execute("SELECT 1 FROM notes WHERE note_id=?", (note_id,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404
    db.execute("UPDATE notes SET my_tags=? WHERE note_id=?",
               (json.dumps(tags, ensure_ascii=False), note_id))
    db.commit()
    return jsonify({"ok": True, "my_tags": tags})


@app.route("/api/note/<note_id>/status", methods=["POST"])
def set_status(note_id):
    data = request.get_json(force=True)
    status = data.get("status", "active")
    if status not in ("active", "deleted"):
        return jsonify({"ok": False, "error": "bad status"}), 400
    db = get_db()
    db.execute("UPDATE notes SET status=? WHERE note_id=?", (status, note_id))
    db.commit()
    return jsonify({"ok": True, "status": status})


@app.route("/api/notes/batch_status", methods=["POST"])
def batch_status():
    data = request.get_json(force=True)
    ids = data.get("ids", [])
    status = data.get("status", "deleted")
    if status not in ("active", "deleted"):
        return jsonify({"ok": False, "error": "bad status"}), 400
    db = get_db()
    db.executemany("UPDATE notes SET status=? WHERE note_id=?",
                   [(status, i) for i in ids])
    db.commit()
    return jsonify({"ok": True, "count": len(ids)})


@app.route("/api/tags")
def api_tags():
    """统计所有未删除笔记的 tag 词频（小红书自带 + 我的），返回前 40。"""
    rows = get_db().execute(
        "SELECT tags, my_tags FROM notes WHERE (status IS NULL OR status != 'deleted')"
    ).fetchall()
    counter = {}
    for r in rows:
        for raw in (r["tags"], r["my_tags"]):
            for t in parse_tags(raw):
                t = t.strip()
                if t:
                    counter[t] = counter.get(t, 0) + 1
    top = sorted(counter.items(), key=lambda x: -x[1])[:40]
    return jsonify([{"tag": t, "count": c} for t, c in top])


@app.route("/covers/<note_id>")
def cover(note_id):
    row = get_db().execute(
        "SELECT cover_url FROM notes WHERE note_id=?", (note_id,)
    ).fetchone()
    if not row or not row["cover_url"]:
        return Response(status=404)

    for ext in (".webp", ".jpg", ".png"):
        p = COVERS_DIR / f"{note_id}{ext}"
        if p.exists():
            return Response(p.read_bytes(), mimetype=f"image/{ext[1:]}")

    url = row["cover_url"]
    if url.startswith("//"):
        url = "https:" + url
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://www.xiaohongshu.com/",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read()
            ctype = r.headers.get("Content-Type", "image/jpeg")
        ext = ".webp" if "webp" in ctype else (".png" if "png" in ctype else ".jpg")
        (COVERS_DIR / f"{note_id}{ext}").write_bytes(data)
        return Response(data, mimetype=ctype)
    except Exception:
        return Response(status=404)


# ──────────────────────── 页面 ────────────────────────

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>我的小红书收藏</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
    background: #f6f4ef; color: #2b2b2b; padding: 24px 16px 60px;
  }
  .wrap { max-width: 1200px; margin: 0 auto; }
  header h1 { font-size: 22px; margin-bottom: 14px; }
  .searchbar {
    display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
    background: #fff; padding: 12px 14px; border-radius: 12px;
    box-shadow: 0 1px 4px rgba(0,0,0,.06);
  }
  .searchbar input[type=text] {
    flex: 1; min-width: 220px; font-size: 16px; padding: 9px 12px;
    border: 1px solid #ddd; border-radius: 8px; outline: none;
  }
  .searchbar input[type=text]:focus { border-color: #ff2442; }
  .searchbar label { font-size: 14px; display: flex; align-items: center; gap: 6px; cursor: pointer; }
  .searchbar button {
    font-size: 15px; padding: 9px 22px; border: none; border-radius: 8px;
    background: #ff2442; color: #fff; cursor: pointer;
  }
  .meta { margin: 14px 2px; font-size: 13px; color: #888; }
  .grid {
    display: grid; gap: 14px;
    grid-template-columns: repeat(2, 1fr);
  }
  @media (min-width: 768px) { .grid { grid-template-columns: repeat(4, 1fr); } }
  .card {
    background: #fff; border-radius: 12px; overflow: hidden; cursor: pointer;
    box-shadow: 0 1px 4px rgba(0,0,0,.06); transition: transform .12s;
    display: flex; flex-direction: column; position: relative;
  }
  .card:hover { transform: translateY(-2px); }
  .cover { width: 100%; aspect-ratio: 3/4; background: #eee; object-fit: cover; }
  .cover.placeholder {
    display: flex; align-items: center; justify-content: center;
    color: #aaa; font-size: 13px; padding: 10px; text-align: center;
  }
  .body { padding: 10px 12px 12px; }
  .title {
    font-size: 14px; line-height: 1.4; margin-bottom: 8px;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }
  .tags { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 8px; }
  .tag { font-size: 11px; padding: 2px 7px; border-radius: 10px; }
  .tag.builtin { color: #ff2442; background: #ffedf0; }
  .tag.mine { color: #2563eb; background: #dbeafe; }
  .foot { font-size: 12px; color: #999; display: flex; justify-content: space-between; }

  /* ── 多选模式 ── */
  .batchbar {
    display: none; gap: 14px; align-items: center;
    background: #fff; padding: 10px 14px; border-radius: 12px;
    margin-top: 10px; box-shadow: 0 1px 4px rgba(0,0,0,.06); font-size: 14px;
  }
  .batchbar .btn { padding: 7px 16px; font-size: 13px; }
  .sel-cb {
    position: absolute; top: 10px; left: 10px; width: 20px; height: 20px;
    z-index: 2; cursor: pointer;
  }
  .card.selected { outline: 3px solid #ff2442; }
  .history, .tagcloud { margin: 10px 2px 0; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .hist-label { font-size: 12px; color: #999; margin-right: 2px; }
  .history .tag, .tagcloud .tag { cursor: pointer; }

  /* ── 弹窗 ── */
  .overlay {
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,.45);
    z-index: 10; align-items: center; justify-content: center; padding: 20px;
  }
  .dialog {
    background: #fff; border-radius: 14px; max-width: 760px; width: 100%;
    display: flex; gap: 0; position: relative; max-height: 90vh; overflow: hidden;
  }
  .close-x {
    position: absolute; top: 10px; right: 12px; border: none; background: none;
    font-size: 24px; cursor: pointer; color: #999; z-index: 2;
  }
  .d-left { width: 280px; flex-shrink: 0; background: #eee; }
  .d-left img { width: 100%; height: 100%; object-fit: cover; }
  .d-left .ph {
    height: 100%; display: flex; align-items: center; justify-content: center;
    color: #aaa; font-size: 14px;
  }
  .d-right { flex: 1; padding: 20px; overflow-y: auto; min-width: 0; }
  .d-title { font-size: 17px; font-weight: 600; margin-bottom: 6px; line-height: 1.4; }
  .d-meta { font-size: 12px; color: #999; margin-bottom: 14px; }
  .d-label { font-size: 12px; color: #888; margin: 12px 0 6px; }
  .d-tags { display: flex; flex-wrap: wrap; gap: 4px; }
  .mytag-list { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 6px; }
  .mytag-chip {
    font-size: 12px; color: #2563eb; background: #dbeafe;
    padding: 3px 8px; border-radius: 10px; cursor: pointer;
  }
  .mytag-chip:hover { background: #fecaca; color: #dc2626; }
  .mytag-editor input {
    width: 100%; font-size: 14px; padding: 7px 10px;
    border: 1px dashed #bbb; border-radius: 8px; outline: none;
  }
  .d-actions { margin-top: 18px; display: flex; gap: 10px; align-items: center; }
  .d-actions a {
    font-size: 13px; color: #ff2442; text-decoration: none; margin-right: auto;
  }
  .btn {
    font-size: 14px; padding: 8px 18px; border-radius: 8px; cursor: pointer; border: none;
  }
  .btn.primary { background: #ff2442; color: #fff; }
  .btn.danger { background: #fff; color: #dc2626; border: 1px solid #dc2626; }
  .btn.danger.armed { background: #dc2626; color: #fff; }
  @media (max-width: 640px) {
    .dialog { flex-direction: column; overflow-y: auto; }
    .d-left { width: 100%; height: 200px; }
  }
</style>
</head>
<body>
<div class="wrap">
  <header><h1>我的小红书收藏</h1></header>
  <div class="searchbar">
    <input type="text" id="q" placeholder="搜标题 / tag / 作者，多个词用空格隔开">
    <label><input type="checkbox" id="tag_only"> 只搜标签</label>
    <label><input type="checkbox" id="include_deleted"> 回收站</label>
    <button id="multi_btn">多选</button>
    <button id="btn">搜索</button>
  </div>
  <div class="batchbar" id="batchbar">
    <label><input type="checkbox" id="select_all"> 全选</label>
    <span id="sel_count">已选 0 项</span>
    <button class="btn danger" id="batch_del">删除所选</button>
    <button class="btn" id="batch_exit">退出多选</button>
  </div>
  <div class="history" id="history"></div>
  <div class="tagcloud" id="tagcloud"></div>
  <div class="meta" id="meta"></div>
  <div class="grid" id="grid"></div>
</div>

<div class="overlay" id="overlay">
  <div class="dialog">
    <button class="close-x" id="modal_close">&times;</button>
    <div class="d-left" id="d_cover"></div>
    <div class="d-right">
      <div class="d-title" id="d_title"></div>
      <div class="d-meta" id="d_meta"></div>
      <div class="d-label">小红书标签</div>
      <div class="d-tags" id="d_builtin"></div>
      <div class="d-label">我的标签（回车添加，点标签移除）</div>
      <div class="mytag-list" id="d_mytags"></div>
      <div class="mytag-editor">
        <input id="d_mytag_input" placeholder="输入后回车添加">
      </div>
      <div class="d-actions">
        <a id="d_open" target="_blank" rel="noopener">在小红书打开 ↗</a>
        <button class="btn danger" id="d_delete">删除记录</button>
        <button class="btn primary" id="d_save">完成</button>
      </div>
    </div>
  </div>
</div>

<script>
const grid = document.getElementById("grid");
const meta = document.getElementById("meta");
const overlay = document.getElementById("overlay");
let editTags = [];
let selecting = false;
let selected = new Set();
let currentItems = [];

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => (
    {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]
  ));
}
function fmtLikes(n) {
  if (n >= 10000) return (n / 10000).toFixed(1) + "万";
  return String(n);
}
function chipEl(t, cls) {
  const s = document.createElement("span");
  s.className = "tag " + cls;
  s.textContent = "#" + t;
  s.title = "点此搜索";
  s.onclick = (e) => {
    e.stopPropagation();
    document.getElementById("q").value = t;
    search();
  };
  return s;
}
function cleanDate(s) {
  // 只在前端显示时清洗：剥掉"编辑于"和地点尾巴（"09-28 广东" -> "09-28"），不改数据库
  if (!s) return "";
  return s.replace("编辑于", "").trim().split(" ")[0];
}

async function search() {
  const q = document.getElementById("q").value.trim();
  const tagOnly = document.getElementById("tag_only").checked;
  const incDel = document.getElementById("include_deleted").checked;
  const url = "/api/search?q=" + encodeURIComponent(q)
    + "&tag_only=" + (tagOnly ? 1 : 0)
    + "&include_deleted=" + (incDel ? 1 : 0);
  meta.textContent = "加载中...";
  const data = await (await fetch(url)).json();
  meta.textContent = "共 " + data.total + " 条" + (q ? "（关键词：" + q + "）" : "")
    + (incDel ? "（回收站）" : "");
  currentItems = data.items;
  grid.innerHTML = "";
  for (const it of data.items) {
    grid.appendChild(buildCard(it));
  }
  saveHistory(q);
}

// ── 搜索历史（localStorage，重启不丢）──
function loadHistory() {
  try { return JSON.parse(localStorage.getItem("xhs_history") || "[]"); }
  catch (e) { return []; }
}
function saveHistory(q) {
  if (!q) return;
  let h = loadHistory().filter(x => x !== q);
  h.unshift(q);
  h = h.slice(0, 10);
  localStorage.setItem("xhs_history", JSON.stringify(h));
  renderHistory();
}
function renderHistory() {
  const box = document.getElementById("history");
  box.innerHTML = "";
  const h = loadHistory();
  if (!h.length) return;
  const lab = document.createElement("span");
  lab.className = "hist-label";
  lab.textContent = "历史:";
  box.appendChild(lab);
  h.forEach(q => {
    const c = document.createElement("span");
    c.className = "tag builtin";
    c.textContent = q;
    c.onclick = () => { document.getElementById("q").value = q; search(); };
    box.appendChild(c);
  });
}

// ── 热门 tag 云 ──
async function loadTagCloud() {
  const data = await (await fetch("/api/tags")).json();
  const box = document.getElementById("tagcloud");
  box.innerHTML = '<span class="hist-label">热门tag:</span>';
  data.forEach(t => {
    const s = document.createElement("span");
    s.className = "tag mine";
    s.textContent = "#" + t.tag + " " + t.count;
    s.onclick = () => { document.getElementById("q").value = t.tag; search(); };
    box.appendChild(s);
  });
}

function buildCard(it) {
  const card = document.createElement("div");
  card.className = "card" + (selected.has(it.note_id) ? " selected" : "");
  card.onclick = () => {
    if (selecting) toggleSelect(it.note_id);
    else openEdit(it);
  };
  if (selecting) {
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "sel-cb";
    cb.checked = selected.has(it.note_id);
    cb.onclick = (e) => { e.stopPropagation(); toggleSelect(it.note_id); };
    card.appendChild(cb);
  }

  // 封面
  if (it.has_cover) {
    const img = document.createElement("img");
    img.className = "cover";
    img.loading = "lazy";
    img.src = "/covers/" + it.note_id;
    img.onerror = () => {
      img.outerHTML = '<div class="cover placeholder">封面加载失败</div>';
    };
    card.appendChild(img);
  } else {
    const ph = document.createElement("div");
    ph.className = "cover placeholder";
    ph.textContent = "无封面";
    card.appendChild(ph);
  }

  // 内容
  const body = document.createElement("div");
  body.className = "body";
  const title = document.createElement("div");
  title.className = "title";
  title.textContent = it.title;
  const tagsBox = document.createElement("div");
  tagsBox.className = "tags";
  it.tags.slice(0, 5).forEach(t => tagsBox.appendChild(chipEl(t, "builtin")));
  it.my_tags.slice(0, 5).forEach(t => tagsBox.appendChild(chipEl(t, "mine")));
  const foot = document.createElement("div");
  foot.className = "foot";
  const fspan1 = document.createElement("span");
  fspan1.textContent = it.author;
  const fspan2 = document.createElement("span");
  const d = cleanDate(it.date);
  fspan2.textContent = (d ? d + " · " : "") + "♥ " + fmtLikes(it.likes);
  foot.appendChild(fspan1);
  foot.appendChild(fspan2);
  body.appendChild(title);
  body.appendChild(tagsBox);
  body.appendChild(foot);
  card.appendChild(body);
  return card;
}

// ── 弹窗 ──
let current = null;
let deleteArmed = false;

function openEdit(it) {
  current = it;
  deleteArmed = false;
  const delBtn = document.getElementById("d_delete");
  delBtn.textContent = it.deleted ? "恢复记录" : "删除记录";
  delBtn.classList.remove("armed");

  document.getElementById("d_title").textContent = it.title;
  document.getElementById("d_meta").textContent =
    it.author + " · " + (cleanDate(it.date) || "") + " · ♥ " + fmtLikes(it.likes);
  const bb = document.getElementById("d_builtin");
  bb.innerHTML = "";
  if (it.tags.length) it.tags.forEach(t => bb.appendChild(chipEl(t, "builtin")));
  else bb.innerHTML = '<span style="color:#bbb;font-size:12px">（无）</span>';
  document.getElementById("d_open").href = it.url;

  // 封面
  const dc = document.getElementById("d_cover");
  dc.innerHTML = "";
  if (it.has_cover) {
    const img = document.createElement("img");
    img.src = "/covers/" + it.note_id;
    img.onerror = () => { dc.innerHTML = '<div class="ph">封面加载失败</div>'; };
    dc.appendChild(img);
  } else {
    dc.innerHTML = '<div class="ph">无封面</div>';
  }

  editTags = it.my_tags.slice();
  renderMyTags();
  document.getElementById("d_mytag_input").value = "";
  overlay.style.display = "flex";
}

function renderMyTags() {
  const box = document.getElementById("d_mytags");
  box.innerHTML = "";
  editTags.forEach((t, i) => {
    const chip = document.createElement("span");
    chip.className = "mytag-chip";
    chip.textContent = "#" + t;
    chip.title = "点击移除";
    chip.onclick = () => { editTags.splice(i, 1); renderMyTags(); };
    box.appendChild(chip);
  });
}

document.getElementById("d_mytag_input").addEventListener("keydown", e => {
  if (e.key === "Enter") {
    const v = e.target.value.trim();
    if (v && !editTags.includes(v)) { editTags.push(v); renderMyTags(); }
    e.target.value = "";
  }
});

document.getElementById("modal_close").onclick = () => overlay.style.display = "none";
overlay.onclick = (e) => { if (e.target === overlay) overlay.style.display = "none"; };

document.getElementById("d_save").onclick = async () => {
  await fetch("/api/note/" + current.note_id + "/my_tags", {
    method: "PUT", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({my_tags: editTags})
  });
  overlay.style.display = "none";
  search();
};

document.getElementById("d_delete").onclick = async () => {
  const delBtn = document.getElementById("d_delete");
  if (current.deleted) {
    await fetch("/api/note/" + current.note_id + "/status", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({status: "active"})
    });
    overlay.style.display = "none";
    search();
    return;
  }
  if (!deleteArmed) {
    deleteArmed = true;
    delBtn.textContent = "再点一次确认删除";
    delBtn.classList.add("armed");
    return;
  }
  await fetch("/api/note/" + current.note_id + "/status", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({status: "deleted"})
  });
  overlay.style.display = "none";
  search();
};

// ── 多选模式 ──
function toggleSelect(id) {
  if (selected.has(id)) selected.delete(id); else selected.add(id);
  document.getElementById("sel_count").textContent = "已选 " + selected.size + " 项";
  search();
}

document.getElementById("multi_btn").onclick = () => {
  selecting = true;
  document.getElementById("batchbar").style.display = "flex";
  search();
};
document.getElementById("batch_exit").onclick = () => {
  selecting = false;
  selected.clear();
  document.getElementById("select_all").checked = false;
  document.getElementById("batchbar").style.display = "none";
  search();
};
document.getElementById("select_all").onchange = (e) => {
  selected.clear();
  if (e.target.checked) currentItems.forEach(i => selected.add(i.note_id));
  document.getElementById("sel_count").textContent = "已选 " + selected.size + " 项";
  search();
};
let batchArmed = false;
document.getElementById("batch_del").onclick = async () => {
  const btn = document.getElementById("batch_del");
  if (selected.size === 0) return;
  if (!batchArmed) {
    batchArmed = true;
    btn.textContent = "再点一次确认删除 " + selected.size + " 项";
    return;
  }
  await fetch("/api/notes/batch_status", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ids: [...selected], status: "deleted"})
  });
  batchArmed = false;
  btn.textContent = "删除所选";
  selected.clear();
  document.getElementById("select_all").checked = false;
  document.getElementById("sel_count").textContent = "已选 0 项";
  search();
};

document.getElementById("btn").onclick = search;
document.getElementById("q").addEventListener("keydown", e => { if (e.key === "Enter") search(); });
document.getElementById("tag_only").onchange = search;
document.getElementById("include_deleted").onchange = search;
renderHistory();
loadTagCloud();
search();
</script>
</body>
</html>"""


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html; charset=utf-8")


if __name__ == "__main__":
    print(f"数据库: {DB_PATH}")
    print(f"封面缓存: {COVERS_DIR}")
    print("打开浏览器访问: http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
