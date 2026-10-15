#!/usr/bin/env python3
"""新文章上線後，自動發文到 Facebook 粉專、Instagram、Threads。

由 .github/workflows/social-publish.yml 呼叫，參數是用 | 分隔的文章路徑。
憑證都放在 GitHub Secrets，沒設定的平台會略過。

文章 front matter 可用的欄位：
  social_post    社群貼文內容（留空時自動用「【標題】＋簡介」）
  social_images  Instagram／Threads 輪播要加的圖片路徑清單（封面固定是第一張）
  social: false  這篇不要自動發文
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

SITE_URL = os.environ.get("SITE_URL", "https://yenlabaero.com").rstrip("/")
GRAPH = "https://graph.facebook.com/v23.0"
THREADS = "https://graph.threads.net/v1.0"
THREADS_LIMIT = 500


def call(method, url, params):
    """呼叫 API；錯誤訊息只印回應內容，不印含權杖的網址。"""
    data = urllib.parse.urlencode(params).encode()
    if method == "GET":
        url, data = f"{url}?{data.decode()}", None
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            body = json.loads(body).get("error", {}).get("message", body)
        except ValueError:
            pass
        raise RuntimeError(f"HTTP {e.code}：{body[:400]}") from None


def absolute(path):
    if path.startswith("http"):
        return path
    return SITE_URL + urllib.parse.quote(path)


def is_live(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30) as resp:
            return resp.status == 200
    except urllib.error.URLError:
        return False


def wait_until_live(urls, minutes=15):
    """GitHub Pages 建置約需 1～2 分鐘，等文章與圖片都能開再發文。"""
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        if all(is_live(u) for u in urls):
            return
        time.sleep(20)
    raise RuntimeError(f"等了 {minutes} 分鐘，網站上還打不開：{', '.join(urls)}")


def load_post(path):
    text = open(path, encoding="utf-8").read()
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    return yaml.safe_load(match.group(1)) if match else {}


def build_texts(fm, url):
    base = (fm.get("social_post") or "").strip()
    if not base:
        base = f"【{fm.get('title', '')}】\n\n{fm.get('description', '')}".strip()
    with_link = base if url in base else f"{base}\n\n完整文章：{url}"
    # Instagram 文字裡的連結點不了，網址改成網站名稱
    if re.search(r"https?://", base):
        ig = re.sub(r"https?://\S+", "yenlabaero.com", base)
    else:
        ig = f"{base}\n\n完整文章請上 yenlabaero.com"
    threads = with_link
    if len(threads) > THREADS_LIMIT:
        room = THREADS_LIMIT - len(url) - 4
        threads = f"{base[:room].rstrip()}…\n\n{url}"
    return with_link, ig, threads


def post_facebook(text, url):
    page_id, token = os.environ["FB_PAGE_ID"], os.environ["FB_PAGE_TOKEN"]
    res = call("POST", f"{GRAPH}/{page_id}/feed", {"message": text, "link": url, "access_token": token})
    return res.get("id")


def ig_wait(container_id, token):
    for _ in range(30):
        status = call("GET", f"{GRAPH}/{container_id}", {"fields": "status_code", "access_token": token}).get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram 處理圖片失敗（{status}）")
        time.sleep(10)
    raise RuntimeError("Instagram 處理圖片逾時")


def post_instagram(images, caption):
    token = os.environ["FB_PAGE_TOKEN"]
    ig_id = os.environ.get("IG_USER_ID")
    if not ig_id:
        page = call("GET", f"{GRAPH}/{os.environ['FB_PAGE_ID']}", {"fields": "instagram_business_account", "access_token": token})
        ig_id = (page.get("instagram_business_account") or {}).get("id")
        if not ig_id:
            raise RuntimeError("粉專沒有連結 Instagram 專業帳號")
    if len(images) == 1:
        container = call("POST", f"{GRAPH}/{ig_id}/media", {"image_url": images[0], "caption": caption, "access_token": token})["id"]
    else:
        children = []
        for image in images[:10]:
            child = call("POST", f"{GRAPH}/{ig_id}/media", {"image_url": image, "is_carousel_item": "true", "access_token": token})["id"]
            ig_wait(child, token)
            children.append(child)
        container = call("POST", f"{GRAPH}/{ig_id}/media", {
            "media_type": "CAROUSEL", "children": ",".join(children), "caption": caption, "access_token": token})["id"]
    ig_wait(container, token)
    return call("POST", f"{GRAPH}/{ig_id}/media_publish", {"creation_id": container, "access_token": token}).get("id")


def threads_wait(container_id, token):
    for _ in range(30):
        status = call("GET", f"{THREADS}/{container_id}", {"fields": "status", "access_token": token}).get("status")
        if status in ("FINISHED", "PUBLISHED"):
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Threads 處理圖片失敗（{status}）")
        time.sleep(10)
    raise RuntimeError("Threads 處理圖片逾時")


def post_threads(text, images, url):
    token = os.environ["THREADS_TOKEN"]
    user = os.environ.get("THREADS_USER_ID") or "me"
    if len(images) > 1:
        children = []
        for image in images[:20]:
            child = call("POST", f"{THREADS}/{user}/threads", {
                "media_type": "IMAGE", "image_url": image, "is_carousel_item": "true", "access_token": token})["id"]
            children.append(child)
        for child in children:
            threads_wait(child, token)
        params = {"media_type": "CAROUSEL", "children": ",".join(children), "text": text, "access_token": token}
    elif images:
        params = {"media_type": "IMAGE", "image_url": images[0], "text": text, "access_token": token}
    else:
        params = {"media_type": "TEXT", "text": text, "link_attachment": url, "access_token": token}
    container = call("POST", f"{THREADS}/{user}/threads", params)["id"]
    time.sleep(30)  # 官方建議建立後等約 30 秒再發布
    threads_wait(container, token)
    return call("POST", f"{THREADS}/{user}/threads_publish", {"creation_id": container, "access_token": token}).get("id")


def configured(platform):
    need = {
        "facebook": ["FB_PAGE_ID", "FB_PAGE_TOKEN"],
        "instagram": ["FB_PAGE_ID", "FB_PAGE_TOKEN"],
        "threads": ["THREADS_TOKEN"],
    }[platform]
    return all(os.environ.get(k) for k in need)


def publish(path, platforms, report):
    fm = load_post(path)
    name = os.path.basename(path)
    if fm.get("social") is False:
        report.append(f"| {name} | 全部 | 略過（social: false） |")
        return True
    slug = fm.get("slug")
    if not slug:
        report.append(f"| {name} | 全部 | ❌ 沒有 slug，無法組出文章網址 |")
        return False
    url = f"{SITE_URL}/{slug}"
    thumb = absolute(fm["thumbnail"]) if fm.get("thumbnail") else None
    images = [u for u in [thumb] + [absolute(p) for p in fm.get("social_images") or []] if u]
    wait_until_live([url] + images)

    fb_text, ig_text, threads_text = build_texts(fm, url)
    jpgs = [u for u in images if u.lower().split("?")[0].endswith((".jpg", ".jpeg"))]

    ok = True
    for platform in platforms:
        if not configured(platform):
            report.append(f"| {name} | {platform} | 略過（尚未設定憑證） |")
            continue
        try:
            if platform == "facebook":
                pid = post_facebook(fb_text, url)
            elif platform == "instagram":
                if not jpgs:
                    report.append(f"| {name} | instagram | 略過（Instagram 只接受 JPG 圖片） |")
                    continue
                pid = post_instagram(jpgs, ig_text)
            else:
                pid = post_threads(threads_text, jpgs, url)
            report.append(f"| {name} | {platform} | ✅ 已發文（{pid}） |")
        except Exception as e:  # 單一平台失敗不影響其他平台
            ok = False
            report.append(f"| {name} | {platform} | ❌ {e} |")
    return ok


def main():
    posts = [p for p in sys.argv[1].split("|") if p.strip()]
    platforms = [p.strip() for p in os.environ.get("PLATFORMS", "facebook,instagram,threads").split(",") if p.strip()]
    report = ["| 文章 | 平台 | 結果 |", "| --- | --- | --- |"]
    ok = True
    for path in posts:
        try:
            ok = publish(path, platforms, report) and ok
        except Exception as e:
            ok = False
            report.append(f"| {os.path.basename(path)} | 全部 | ❌ {e} |")
    summary = "\n".join(report)
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("## 社群自動發文結果\n\n" + summary + "\n")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
