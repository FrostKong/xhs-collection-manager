"""
小红书收藏夹抓取 - 正式分批版
==============================
每批处理 200 条：滚动抓列表 -> 逐条点开抓 tag -> 先落库 -> 再取消收藏 -> 退出。
下次运行自动从剩余收藏继续（已取消收藏的不会再出现）。

## ⚠️ 非常重要的风控警告！！！
**本脚本涉及登录态下的小红书自动化操作，官方检测到会弹出警告、限流甚至封号！**
- 我本人测试爬了300条左右的收藏，就收到了官方"不允许使用AI操作账号"的警告
- 目前小红书对Playwright/自动化工具的检测非常严格，包括浏览器指纹、行为节奏、批量操作频率
- 强烈不建议直接运行本脚本，本项目仅作为**爬虫/反风控学习的案例**，不要用于实际批量操作！
- 作者不对任何因使用本脚本导致的账号封禁/警告承担责任！

特性：
  - 过滤作者为 "xxx" 的笔记（自己发的）
  - 不下载封面，只存 cover_url
  - 详情页抓 #hash-tag 标签 + 发布时间
  - 安全顺序：标签成功落库（detail_done=1）后才取消收藏；
  - 反风控：每条间隔随机抖动 2~4 秒；检测到验证页自动暂停，
  - 每批 100 条后保存退出
  - 每次打开干净浏览器，手动登录后回车开抓（不存登录态）
  - 每批结束自动备份 db 到 backups/（只留最近 10 份）

用法：python crawl.py
"""
import asyncio
import json
import random
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

HERE = Path(__file__).parent
DB_PATH = HERE / "xhs.db"
BACKUP_DIR = HERE / "backups"

BATCH_SIZE = 100            # 每批处理多少条详情；触发风控就调小
LIST_BUFFER = 220         # 列表阶段多抓 20 条缓冲
MAX_SCROLLS = 50          # 列表阶段最多滚动次数
SELF_AUTHOR = ""          # 改成你自己的笔名，抓取时自动跳过自己发的笔记


# ──────────────────────── 数据库 ────────────────────────

def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            note_id       TEXT PRIMARY KEY,
            title         TEXT,
            author        TEXT,
            likes         INTEGER,
            cover_url     TEXT,
            url_token     TEXT,
            xsec_token    TEXT,
            tags          TEXT,
            publish_date  TEXT,
            my_tags       TEXT DEFAULT '[]',
            status        TEXT DEFAULT 'active',
            crawled_at    TEXT,
            detail_done   INTEGER DEFAULT 0,
            canceled      INTEGER DEFAULT 0,
            canceled_at   TEXT
        )
    """)
    conn.commit()
    return conn


def upsert_note(conn, note):
    conn.execute("""
        INSERT OR IGNORE INTO notes
            (note_id, title, author, likes, cover_url, url_token, xsec_token, crawled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
    """, (
        note["note_id"], note.get("title"), note.get("author"),
        note.get("likes"), note.get("cover_url"),
        note.get("url_token"), note.get("xsec_token"),
    ))
    conn.commit()


def update_detail(conn, note_id, tags, publish_date):
    """标签抓完落库：标记 detail_done=1，不碰取消收藏状态。"""
    conn.execute("""
        UPDATE notes SET tags=?, publish_date=?, detail_done=1
        WHERE note_id=?
    """, (json.dumps(tags, ensure_ascii=False), publish_date, note_id))
    conn.commit()


def mark_canceled(conn, note_id):
    """确认线上取消收藏成功后，单独打 canceled 标记。"""
    conn.execute("""
        UPDATE notes SET canceled=1, canceled_at=datetime('now')
        WHERE note_id=?
    """, (note_id,))
    conn.commit()


def is_done(conn, note_id):
    row = conn.execute("SELECT detail_done FROM notes WHERE note_id=?", (note_id,)).fetchone()
    return row is not None and row[0] == 1


def backup_db(keep=10):
    """每批结束后复制一份带时间戳的 db，只留最近 keep 份。"""
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"xhs_{stamp}.db"
    shutil.copy2(DB_PATH, dst)
    for old in sorted(BACKUP_DIR.glob("xhs_*.db"))[:-keep]:
        old.unlink()
    return dst


# ──────────────────────── 页面操作 ────────────────────────

async def scrape_visible_cards(page):
    """抓当前可见的收藏卡片，自动过滤作者为 xxx 的自己的笔记。"""
    return await page.evaluate(
        """
        () => {
            const covers = Array.from(document.querySelectorAll("a.cover.mask.ld"));
            const seen = new Set();
            const result = [];
            for (const a of covers) {
                if (a.offsetParent === null) continue;  // 跳过隐藏元素
                const href = a.getAttribute("href") || "";
                const m = href.match(/\\/([a-f0-9]{24})\\?/);
                if (!m || seen.has(m[1])) continue;
                seen.add(m[1]);

                let card = a;
                for (let i = 0; i < 6; i++) {
                    if (!card.parentElement) break;
                    card = card.parentElement;
                    const txt = (card.innerText || "").trim();
                    if (txt.length > 5 && txt.length < 200) break;
                }
                const lines = (card.innerText || "").trim().split("\\n").filter(s => s.trim());
                const author = (lines[1] || "").trim();
                if (author === "xxx") continue;  // 过滤自己的笔记

                const img = a.querySelector("img");
                const tokenMatch = href.match(/xsec_token=([^&]+)/);
                result.push({
                    note_id: m[1],
                    title: lines[0] || "",
                    author: author,
                    likes: parseInt(lines[2] || "0") || 0,
                    cover_url: img ? (img.src || img.dataset.src || "") : "",
                    url_token: href,
                    xsec_token: tokenMatch ? tokenMatch[1] : "",
                });
            }
            return result;
        }
        """
    )


async def check_verify(page):
    """检测当前是否跳转到了小红书风控验证页。"""
    try:
        if "verify" in page.url or "verification" in page.url:
            return True
        for sel in ("#captcha_container", ".verify-bar-area", ".geetest_btn",
                    "iframe[src*='captcha']", "iframe[src*='verify']"):
            if await page.query_selector(sel, timeout=500):
                return True
    except Exception:
        pass
    return False


async def pause_for_verify(page, note_desc=""):
    """若检测到验证页，提示用户手动过验证并等待回车。返回是否触发了验证。"""
    if not await check_verify(page):
        return False
    print("\n    ⚠️⚠️⚠️ 检测到小红书风控验证页面！请在浏览器里：")
    print("       1. 手动完成滑块/点选验证")
    print("       2. 回到「收藏」列表页面")
    tip = "（本条将跳过，下轮再抓）" if note_desc else ""
    print(f"       完成后回到这里按回车继续{tip}...")
    input()
    await page.wait_for_timeout(1000)
    return True


async def open_detail(page, note_id, url_token):
    """点开笔记详情。优先点击封面，失败则直接打开带 token 的 URL。
    返回 True 表示中途触发了风控验证（本条应跳过）。"""
    try:
        await page.click(f"a.cover.mask.ld[href*='{note_id}']", timeout=4000)
    except Exception:
        await page.goto("https://www.xiaohongshu.com" + url_token, wait_until="domcontentloaded")
    # 随机抖动等待，别让每条间隔都一样
    await page.wait_for_timeout(random.randint(2000, 4000))
    return await pause_for_verify(page, note_id)


async def scrape_tags_and_date(page):
    """从详情页抓 #hash-tag 标签和发布时间。"""
    return await page.evaluate(
        """
        () => {
            // 优先抓 id="hash-tag"
            const hashEls = document.querySelectorAll("#hash-tag");
            let tags = Array.from(hashEls).map(e =>
                (e.innerText || e.textContent || "").trim()
            ).filter(Boolean);

            // fallback: 正文里的 #xxx#
            if (tags.length === 0) {
                const desc = document.querySelector("#detail-desc, .desc, [class*='desc']");
                const text = desc ? (desc.innerText || "") : "";
                tags = (text.match(/#[^#\\s]+#/g) || []).map(t => t.replace(/#/g, ""));
            }

            // 发布时间
            const dateEl = document.querySelector(
                ".date, [class*='date'], .publish-date, span[class*='date'], .bottom-container span"
            );
            const publish_date = dateEl ? (dateEl.innerText || "").trim() : "";

            return { tags, publish_date };
        }
        """
    )


async def cancel_collect(page):
    """
    在详情页取消收藏。
    仅当收藏按钮显示为已收藏状态（svg use href 包含 #collected）时才点击。
    返回是否成功取消。
    """
    try:
        # 找收藏按钮（class 包含 collect-icon）
        btn = await page.query_selector("[class*='collect-icon']")
        if not btn:
            return False
        # 检查内部 svg use 的 href
        use_el = await btn.query_selector("use")
        href = ""
        if use_el:
            href = (await use_el.get_attribute("href")) or ""
        if "#collected" not in href:
            # 当前是未收藏状态，不操作（避免误点成收藏）
            return False
        # 点击取消收藏
        await btn.click()
        await page.wait_for_timeout(random.randint(600, 1500))
        return True
    except Exception as e:
        print(f"    [!] 取消收藏异常: {e}")
        return False


async def close_detail(page):
    """关闭详情抽屉。"""
    try:
        await page.click(".close-box", timeout=2000)
    except Exception:
        await page.keyboard.press("Escape")
    await page.wait_for_timeout(random.randint(500, 1200))


# ──────────────────────── 主流程 ────────────────────────

async def main():
    conn = init_db()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, channel="chrome")
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        await page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        print("\n" + "=" * 60)
        print("请在浏览器里手动操作：")
        print("  1. 用手机号 + 验证码登录小红书")
        print("  2. 点头像进入个人主页")
        print("  3. 切到「收藏」tab，确认看到的是别人的笔记")
        print("然后回到这里按回车开始抓取（本批将处理 100 条后自动退出）...")
        print("=" * 60)
        input()

        # === 阶段一：滚动抓列表 ===
        print(f"\n[阶段一] 滚动抓取收藏列表（目标 {LIST_BUFFER} 条，最多滚动 {MAX_SCROLLS} 次）...")
        all_notes = {}
        for i in range(MAX_SCROLLS):
            cards = await scrape_visible_cards(page)
            new_count = 0
            for c in cards:
                if c["note_id"] not in all_notes and not is_done(conn, c["note_id"]):
                    all_notes[c["note_id"]] = c
                    upsert_note(conn, c)
                    new_count += 1
            print(f"  滚动 {i+1:2d}/{MAX_SCROLLS}  待处理 {len(all_notes):3d} 条  本次 +{new_count}")
            if len(all_notes) >= LIST_BUFFER:
                print(f"  已达到缓冲量 {LIST_BUFFER}，停止滚动")
                break
            if new_count == 0 and i > 8:
                print("  连续无新增，可能到底了")
                break
            await page.evaluate("window.scrollBy(0, 1500)")
            await page.wait_for_timeout(random.randint(1400, 2600))
            await pause_for_verify(page)

        note_list = list(all_notes.values())
        print(f"\n[阶段一完成] 本轮待处理 {len(note_list)} 条")

        if not note_list:
            print("[i] 没有新笔记需要处理，退出")
            await browser.close()
            conn.close()
            return

        # === 阶段二：逐条抓详情 + 取消收藏 ===
        print(f"\n[阶段二] 逐条处理（抓标签 + 取消收藏），最多 {BATCH_SIZE} 条...")
        processed = 0
        canceled_count = 0
        for idx, note in enumerate(note_list):
            if processed >= BATCH_SIZE:
                print(f"\n[i] 已达到本批上限 {BATCH_SIZE}，停止处理")
                break

            note_id = note["note_id"]
            title_short = note["title"][:35].ljust(35)

            try:
                verify_hit = await open_detail(page, note_id, note["url_token"])
                if verify_hit:
                    print(f"  [{processed+1:3d}] {note_id} 触发验证，本条跳过，下轮再抓")
                    continue

                info = await scrape_tags_and_date(page)

                # 安全顺序：标签先落库确认存进 SQLite，再考虑取消收藏
                update_detail(conn, note_id, info["tags"], info["publish_date"])

                canceled = False
                canceled = await cancel_collect(page)
                if canceled:
                    mark_canceled(conn, note_id)
                    canceled_count += 1

                await close_detail(page)
                processed += 1

                tag_str = ", ".join(info["tags"][:4]) if info["tags"] else "(无标签)"
                cancel_mark = "✓已取消" if canceled else "✗未取消"
                print(f"  [{processed:3d}] {title_short} | {cancel_mark} | tags: {tag_str}")

            except Exception as e:
                print(f"  [{processed+1:3d}] {note_id} 处理失败: {e}")
                # 失败时尝试关闭详情，避免影响下一条
                try:
                    await close_detail(page)
                except Exception:
                    pass

        # 备份数据库（每批结束留一个时间戳副本，只留最近 10 份）
        backup_path = backup_db()
        print(f"[i] 已备份数据库: backups/{backup_path.name}")

        # === 统计 ===
        print("\n" + "=" * 60)
        total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        done = conn.execute("SELECT COUNT(*) FROM notes WHERE detail_done=1").fetchone()[0]
        canceled_total = conn.execute("SELECT COUNT(*) FROM notes WHERE canceled=1").fetchone()[0]
        print(f"本轮处理:     {processed} 条")
        print(f"本轮取消收藏: {canceled_count} 条")
        print(f"数据库总计:   {total} 条")
        print(f"已抓标签:     {done} 条")
        print(f"已取消收藏:   {canceled_total} 条")
        print(f"DB 文件:      {DB_PATH}")
        print("=" * 60)
        print("\n[i] 本批结束。下次运行 python crawl.py 将继续处理剩余收藏。")

        input("\n按回车关闭浏览器...")
        await browser.close()
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
