#!/usr/bin/env python3
"""
download_brands.py

动态识别当前 GitHub 仓库与分支，生成完整的当前仓库 GitHub Raw Logo 链接：
如: https://raw.githubusercontent.com/<owner>/<repo>/<branch>/localbrand/logo/xxx.svg

优先级判断逻辑：
1. 命令行参数 `--github-base-url` (如指定参数)
2. 环境变量 `GITHUB_REPOSITORY` & `GITHUB_REF_NAME` (GitHub Actions 自动化环境下自动生效)
3. 本地 `git config remote.origin.url` & `git branch` 动态识别
4. 默认 fallback 模板

维护独立的 localbrand/logo_manifest.json 记录文件，包含 3 个月 (90天) 过期检查。
"""

import os
import sys
import json
import glob
import time
import datetime
import urllib.request
import urllib.parse
import urllib.error
import argparse
import subprocess
import re
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed

# 强制开启 Python 标准输出无缓冲，保证在 Colab 中实时逐行打印日志
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)

# Wikimedia 官方规定必须提供明确联系方式与项目名称的 User-Agent，避免通用浏览器 UA 触发防爬虫与 429 拦截
BROWSER_HEADERS = {
    'User-Agent': 'NameSuggestionIndexBot/1.0 (https://github.com/osmlab/name-suggestion-index; brand-logo-fetcher)',
    'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9'
}

LOGO_PROPERTIES = ['P8972', 'P154', 'P18', 'P158', 'P94']
NINETY_DAYS_SECONDS = 90 * 24 * 3600  # 90 天 (3 个月) 秒数


def detect_github_base_url(custom_base_url=None):
    """
    动态检测当前 GitHub 仓库的 Raw 文件根路径链接。
    例如: https://raw.githubusercontent.com/owner/repo/main/
    """
    if custom_base_url and custom_base_url.strip():
        base = custom_base_url.strip().rstrip('/') + '/'
        print(f"🔗 [仓库 Base URL] 使用自定义配置: {base}")
        return base

    # 1. 优先读取 GitHub Actions 环境变量
    gh_repo = os.environ.get('GITHUB_REPOSITORY')  # e.g., 'myowner/myrepo'
    gh_branch = os.environ.get('GITHUB_REF_NAME', 'main')
    if gh_repo:
        base = f"https://raw.githubusercontent.com/{gh_repo}/{gh_branch}/"
        print(f"🔗 [仓库 Base URL] 从 GitHub Actions 环境变量识别: {base}")
        return base

    # 2. 本地 git 命令自动解析当前远程仓库与分支
    try:
        url_bytes = subprocess.check_output(['git', 'config', '--get', 'remote.origin.url'], stderr=subprocess.DEVNULL)
        origin_url = url_bytes.decode('utf-8').strip()

        branch_bytes = subprocess.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], stderr=subprocess.DEVNULL)
        branch = branch_bytes.decode('utf-8').strip() or 'main'

        m = re.search(r'github\.com[:/]([^/]+)/([^/.]+)(?:\.git)?', origin_url)
        if m:
            owner, repo = m.group(1), m.group(2)
            base = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/"
            print(f"🔗 [仓库 Base URL] 从本地 Git 自动解析识别: {base}")
            return base
    except Exception:
        pass

    # 3. 默认 Fallback 占位链接
    base = "https://raw.githubusercontent.com/owner/repo/main/"
    print(f"🔗 [仓库 Base URL] 无法自动解析，使用默认模板: {base} (可传入 --github-base-url 参数覆盖)")
    return base


def format_iso(ts):
    """时间戳转 ISO 8601 格式字符串"""
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def fetch_url(url, max_retries=5, backoff=3.0):
    """带重试保护和退避机制的 HTTP 下载"""
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(url, headers=BROWSER_HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read(), resp.geturl(), resp.headers.get('Content-Type', '')
        except urllib.error.HTTPError as e:
            if e.code == 429:
                sleep_time = backoff * (1.5 ** (attempt - 1))
                time.sleep(sleep_time)
            elif e.code in [500, 502, 503, 504]:
                time.sleep(2.0)
            else:
                return None, url, ""
        except Exception:
            time.sleep(1.5)
    return None, url, ""


def load_json(path):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ 保存 JSON 失败 ({path}): {e}")


def get_wiki_logo_url(image_filename):
    """
    通过计算 Wikimedia MD5 哈希直接生成 upload.wikimedia.org 静态 CDN 直链，
    绕过 Special:FilePath / Special:Redirect 动态重定向服务，彻底避免触发 429 限流。
    """
    if not image_filename:
        return None
    fn = image_filename.strip().replace(' ', '_')
    m = hashlib.md5(fn.encode('utf-8')).hexdigest()
    a = m[0]
    ab = m[0:2]
    encoded_fn = urllib.parse.quote(fn)
    return f"https://upload.wikimedia.org/wikipedia/commons/{a}/{ab}/{encoded_fn}"


def extract_qid(item):
    tags = item.get('tags', {})
    for k in ['brand:wikidata', 'operator:wikidata', 'network:wikidata', 'flag:wikidata', 'subject:wikidata', 'wikidata']:
        v = tags.get(k)
        if v and isinstance(v, str) and v.startswith('Q'):
            return v
    return None


def is_manifest_entry_valid(entry):
    """检查独立清单中的 Logo 记录是否在 3 个月内且磁盘文件真实存在"""
    if not entry or not isinstance(entry, dict):
        return False

    expires_at_ts = entry.get('expires_at_ts', 0)
    now_ts = time.time()

    # 已过期
    if now_ts >= expires_at_ts:
        return False

    local_path = entry.get('local_logo_path')
    if not local_path:
        return False

    abs_path = os.path.abspath(local_path)
    return os.path.exists(abs_path) and os.path.getsize(abs_path) > 0


def fetch_wikidata_batch(qids, wd_cache):
    """批量从 Wikidata 获取 Logo URL 节点（优先使用外部直链如 Facebook，Wiki 走 CDN 直链）"""
    missing = [q for q in qids if q not in wd_cache]
    if not missing:
        return

    chunks = [missing[i:i + 50] for i in range(0, len(missing), 50)]

    def process_chunk(chunk):
        q_str = '|'.join(chunk)
        api_url = f"https://www.wikidata.org/w/api.php?action=wbgetentities&ids={q_str}&props=claims&format=json"
        content, _, _ = fetch_url(api_url)

        res = {}
        if content:
            try:
                entities = json.loads(content.decode('utf-8')).get('entities', {})
                for q in chunk:
                    claims = entities.get(q, {}).get('claims', {})

                    # 1. 优先提取 Facebook ID 直链，避免走 Wiki
                    fb_logo_url = None
                    if 'P2013' in claims:
                        try:
                            fb = claims['P2013'][0]['mainsnak']['datavalue']['value']
                            if fb:
                                fb_logo_url = f"https://graph.facebook.com/{fb}/picture?type=large"
                        except Exception:
                            pass

                    # 2. 提取 Wiki Logo 文件名并计算 CDN 直链
                    image_filename = None
                    for prop in LOGO_PROPERTIES:
                        if prop in claims:
                            try:
                                fn = claims[prop][0]['mainsnak']['datavalue']['value']
                                if fn:
                                    image_filename = fn
                                    break
                            except Exception:
                                pass

                    wiki_logo_url = get_wiki_logo_url(image_filename)

                    # 优先级：若有 Facebook 等外部直链优先使用，否则使用计算出的 Wiki CDN 直链
                    logo_url = fb_logo_url or wiki_logo_url
                    res[q] = logo_url
            except Exception:
                for q in chunk:
                    res[q] = None
        else:
            for q in chunk:
                res[q] = None
        time.sleep(0.1)  # 温和间隔，尊重 API 限制
        return res

    completed_qids = 0
    total_qids = len(missing)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(process_chunk, c) for c in chunks]
        for f in as_completed(futures):
            res = f.result()
            if res:
                wd_cache.update(res)
                completed_qids += len(res)
                print(f"  └─ 🌐 [Wikidata 提取中] 已完成 {completed_qids}/{total_qids} 个 QID 解析...", flush=True)


def process_single_logo(item, logo_url, localbrand_logo_dir, manifest, base_raw_url):
    """处理并维护独立 manifest 中的 Logo 项"""
    item_id = item.get('id')
    displayName = item.get('displayName') or item.get('tags', {}).get('brand', '')
    qid = extract_qid(item)

    if not item_id or not logo_url:
        return None

    # 1. 如果独立 manifest 中已有记录且 3 个月内未过期、文件健全，直接复用（可更新 github_logo_url 基础域名）
    existing_entry = manifest.get(item_id)
    if is_manifest_entry_valid(existing_entry):
        rel_path = existing_entry.get('local_logo_path')
        existing_entry['github_logo_url'] = f"{base_raw_url}{rel_path}"
        return existing_entry

    # 2. 下载 Logo 图片
    data, _, ctype = fetch_url(logo_url)
    now_ts = time.time()
    expires_ts = now_ts + NINETY_DAYS_SECONDS

    if data and len(data) > 0:
        ext = ".png"
        if "svg" in ctype or logo_url.lower().endswith(".svg"): ext = ".svg"
        elif "jpeg" in ctype or "jpg" in ctype: ext = ".jpg"
        elif "webp" in ctype: ext = ".webp"

        fname = f"{item_id}{ext}"
        dest_path = os.path.join(localbrand_logo_dir, fname)

        with open(dest_path, 'wb') as f:
            f.write(data)

        rel_path = f"localbrand/logo/{fname}"
        github_full_url = f"{base_raw_url}{rel_path}"

        manifest_entry = {
            "id": item_id,
            "brand": displayName,
            "qid": qid,
            "original_logo_url": logo_url,
            "local_logo_path": rel_path,
            "github_logo_url": github_full_url,
            "created_at": format_iso(now_ts),
            "created_at_ts": int(now_ts),
            "expires_at": format_iso(expires_ts),
            "expires_at_ts": int(expires_ts)
        }
        manifest[item_id] = manifest_entry
        return manifest_entry

    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data/brands")
    parser.add_argument("--output-dir", default="localbrand")
    parser.add_argument("--github-base-url", default=None, help="当前仓库 GitHub Raw Base URL (如: https://raw.githubusercontent.com/user/repo/main/)")
    parser.add_argument("--workers", type=int, default=4, help="并发下载线程数")
    args, _ = parser.parse_known_args()

    localbrand_dir = args.output_dir
    localbrand_logo_dir = os.path.join(localbrand_dir, "logo")
    manifest_file = os.path.join(localbrand_dir, "logo_manifest.json")

    # 动态确认当前仓库 GitHub 基础 Raw 链接
    base_raw_url = detect_github_base_url(args.github_base_url)

    os.makedirs(localbrand_dir, exist_ok=True)
    os.makedirs(localbrand_logo_dir, exist_ok=True)

    manifest = load_json(manifest_file)
    wd_cache = {}

    files = glob.glob(os.path.join(args.data_dir, "**", "*.json"), recursive=True)
    total_files = len(files)
    print(f"📋 [初始化] 扫描到 {total_files} 个品牌文件，独立维护清单: {manifest_file} (已有 {len(manifest)} 项)")

    all_items = []
    missing_wd_qids = set()

    for fpath in files:
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                content = json.load(f)
            rel_path = os.path.relpath(fpath, args.data_dir)
            target_path = os.path.join(localbrand_dir, rel_path)
            all_items.append({'fpath': fpath, 'target_path': target_path, 'content': content})

            for item in content.get('items', []):
                item_id = item.get('id')
                if not is_manifest_entry_valid(manifest.get(item_id)):
                    q = extract_qid(item)
                    if q:
                        missing_wd_qids.add(q)
        except Exception:
            pass

    print(f"📊 [3个月缓存分析] 待处理 QID: {len(missing_wd_qids)} 个")

    if missing_wd_qids:
        print("🌐 正在并发获取必要 Wikidata Logo URL...")
        fetch_wikidata_batch(list(missing_wd_qids), wd_cache)

    print("🚀 逐文件生成镜像 JSON 并更新独立清单 logo_manifest.json...")

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        for idx, file_rec in enumerate(all_items, 1):
            target_path = file_rec['target_path']
            content = file_rec['content']
            items = content.get('items', [])

            futures = []
            for item in items:
                item_id = item.get('id')
                qid = extract_qid(item)

                if is_manifest_entry_valid(manifest.get(item_id)):
                    continue

                logo_url = wd_cache.get(qid) if qid else None
                if logo_url:
                    futures.append(executor.submit(process_single_logo, item, logo_url, localbrand_logo_dir, manifest, base_raw_url))

            for fut in futures:
                fut.result()

            # 注入完整当前仓库 GitHub 链接到当前品牌 JSON
            attached = 0
            for item in items:
                item_id = item.get('id')
                m_entry = manifest.get(item_id)
                if m_entry and m_entry.get('local_logo_path'):
                    loc_path = m_entry['local_logo_path']
                    orig_url = m_entry['original_logo_url']
                    gh_url = f"{base_raw_url}{loc_path}"

                    item['original_logo_url'] = orig_url
                    item['github_logo_url'] = gh_url
                    item['local_logo_path'] = loc_path
                    item['logos'] = {
                        'original': orig_url,
                        'github_local': gh_url
                    }
                    attached += 1

            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            with open(target_path, 'w', encoding='utf-8') as f:
                json.dump(content, f, ensure_ascii=False, indent=2)

            print(f"[{idx}/{total_files}] ✅ 生成: {target_path} (成功关联 {attached} 个 Logo)")

            if idx % 10 == 0:
                save_json(manifest_file, manifest)

    save_json(manifest_file, manifest)

    print("\n" + "=" * 60)
    print(f"🎉 [完成] 已生成独立的 Logo 维护文件: {manifest_file}")
    print(f"🎉 [完成] 动态关联当前仓库 GitHub URL 根路径: {base_raw_url}")
    print("=" * 60)


if __name__ == "__main__":
    main()
