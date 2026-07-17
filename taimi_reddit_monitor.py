#!/usr/bin/env python3
"""
Taimi Reddit Monitor
Fetches Reddit posts AND comments mentioning "Taimi" from the past 24 hours,
excludes specified subreddits, and posts a digest to Slack.
"""

import urllib.request
import xml.etree.ElementTree as ET
import json
import time
import re
from datetime import datetime, timezone, timedelta

SEARCH_QUERY = "Taimi"
EXCLUDED_SUBREDDITS = {"Guildwars2", "dreamcast", "pokemongo", "rdcworld"}
SLACK_CHANNEL = "#taimi-reddit-mentions"
LOOKBACK_HOURS = 24
MAX_POSTS = 100
MAX_COMMENTS_PER_SUB = 100
USER_AGENT = "TaimiMonitor/1.0 (appflame marketing; daily digest)"
MAX_RETRIES = 3
RETRY_DELAY = 15

FULL_SUBREDDITS = ["taimi_lgbtq_platform"]

COMMENT_SUBREDDITS = [
    "lgbt", "gaybros", "LesbianActually", "AskLesbians", "actuallesbians", "bisexual", "asexual",
    "nonbinary", "trans", "ainbow", "queer", "QueerWomenOfColor",
    "FTMStraight", "FemboysDating", "feminineboys",
    "dating_advice", "OnlineDating", "datingapps", "Tinder",
    "relationships", "relationship_advice", "relationships_advice",
    "Sissy", "PNW_Sissies", "sissyology", "bisexualafterdark",
    "countttt", "BDSMsapphic",
]

def _fetch_url(url):
    for attempt in range(1, MAX_RETRIES + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < MAX_RETRIES:
                print(f"    429 rate-limit, waiting {RETRY_DELAY}s (attempt {attempt})...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"    HTTP {e.code} fetching {url} — skipping")
                return None
        except Exception as exc:
            print(f"    Error fetching {url}: {exc} — skipping")
            return None
    return None

def _parse_atom(rss_bytes, kind):
    root = ET.fromstring(rss_bytes)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entries = []
    for entry in root.findall("atom:entry", ns):
        title_el   = entry.find("atom:title", ns)
        link_el    = entry.find("atom:link", ns)
        updated_el = entry.find("atom:updated", ns)
        author_el  = entry.find("atom:author/atom:name", ns)
        content_el = entry.find("atom:content", ns)
        id_el      = entry.find("atom:id", ns)
        link = link_el.get("href") if link_el is not None else ""
        sub_match = re.search(r"/r/([^/]+)/", link or "")
        subreddit = sub_match.group(1) if sub_match else "unknown"
        updated_str = updated_el.text if updated_el is not None else ""
        try:
            updated_dt = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
        except ValueError:
            updated_dt = datetime.now(timezone.utc)
        raw_content = (content_el.text or "") if content_el is not None else ""
        clean = re.sub(r"<[^>]+>", " ", raw_content)
        clean = re.sub(r"\s+", " ", clean).strip()
        preview = clean[:200] + ("…" if len(clean) > 200 else "")
        entry_id = id_el.text if id_el is not None else ""
        if entry_id.startswith("t1_"):
            detected_kind = "comment"
        elif entry_id.startswith("t3_"):
            detected_kind = "post"
        else:
            detected_kind = kind
        entries.append({
            "kind": detected_kind,
            "title": (title_el.text or "(no title)") if title_el is not None else "(no title)",
            "url": link,
            "subreddit": subreddit,
            "author": (author_el.text or "unknown") if author_el is not None else "unknown",
            "updated": updated_dt,
            "preview": preview,
            "score": None,
        })
    return entries

def fetch_posts(query):
    encoded = urllib.request.quote(query)
    url = f"https://www.reddit.com/search.rss?q={encoded}&sort=new&t=day&limit={MAX_POSTS}"
    print("  Fetching posts from global search RSS...")
    data = _fetch_url(url)
    if data is None:
        return []
    entries = _parse_atom(data, "post")
    return [e for e in entries if e["kind"] == "post"]

def fetch_all_posts_from_subreddits(subreddits):
    all_posts = []
    for sub in subreddits:
        url = f"https://www.reddit.com/r/{sub}/new.rss?limit={MAX_POSTS}"
        print(f"  Fetching all posts from r/{sub}...")
        data = _fetch_url(url)
        time.sleep(1)
        if data is None:
            continue
        entries = _parse_atom(data, "post")
        all_posts.extend(e for e in entries if e["kind"] == "post")
    return all_posts

def fetch_comments_from_subreddits(query, subreddits):
    query_lower = query.lower()
    all_comments = []
    for sub in subreddits:
        url = f"https://www.reddit.com/r/{sub}/comments.rss?limit={MAX_COMMENTS_PER_SUB}"
        print(f"  Scanning r/{sub} comments...")
        data = _fetch_url(url)
        time.sleep(1)
        if data is None:
            continue
        entries = _parse_atom(data, "comment")
        for e in entries:
            if query_lower in e["preview"].lower() or query_lower in e["title"].lower():
                all_comments.append(e)
    return all_comments

def _fetch_score(url):
    json_url = url.rstrip("/") + "/.json?limit=1"
    data = _fetch_url(json_url)
    if data is None:
        return None
    try:
        parsed = json.loads(data)
        return parsed[0]["data"]["children"][0]["data"].get("score")
    except Exception:
        return None

def enrich_with_scores(entries):
    for entry in entries:
        url = entry.get("url", "")
        if "/comments/" in url:
            entry["score"] = _fetch_score(url)
            time.sleep(1)
    return entries

def filter_entries(entries, excluded, hours):
    excluded_lower = {s.lower() for s in excluded}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    seen_urls = set()
    result = []
    for e in entries:
        if e["subreddit"].lower() in excluded_lower:
            continue
        if e["updated"] < cutoff:
            continue
        if e["url"] in seen_urls:
            continue
        seen_urls.add(e["url"])
        result.append(e)
    return result

def build_slack_message(entries, query):
    today = datetime.now(timezone.utc).strftime("%B %d, %Y")
    posts    = [e for e in entries if e["kind"] == "post"]
    comments = [e for e in entries if e["kind"] == "comment"]
    if not entries:
        return f'*Reddit Mentions of "{query}" — {today}*\n\nNo new mentions found in the past 24 hours.'
    lines = [
        f'*Reddit Mentions of "{query}" — {today}*',
        f"_{len(posts)} post(s) · {len(comments)} comment(s) in the past 24h_",
    ]
    def render_section(label, items):
        if not items:
            return
        lines.append(f"\n*{label}*")
        by_sub = {}
        for e in items:
            by_sub.setdefault(e["subreddit"], []).append(e)
        for sub, group in sorted(by_sub.items(), key=lambda x: -len(x[1])):
            lines.append(f"\n*r/{sub}* ({len(group)})")
            for p in group:
                ts = p["updated"].strftime("%H:%M UTC")
                score = p.get("score")
                score_str = f" · ▲ {score}" if score is not None else ""
                lines.append(f"• <{p['url']}|{p['title']}> by u/{p['author']} at {ts}{score_str}")
                preview = p["preview"]
                if preview and preview not in p["title"]:
                    lines.append(f"  _{preview[:150]}_")
    render_section("📝 Posts", posts)
    render_section("💬 Comments", comments)
    return "\n".join(lines)

def post_to_slack(message, channel):
    import os
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if webhook_url:
        payload = json.dumps({"text": message}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                status = resp.status
            print(f"  Slack webhook response: {status}")
        except Exception as exc:
            print(f"  ERROR posting to Slack: {exc}")
            raise
    else:
        print("SLACK_CHANNEL:", channel)
        print("SLACK_MESSAGE_START")
        print(message)
        print("SLACK_MESSAGE_END")

def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Taimi Reddit Monitor starting...")
    posts = fetch_posts(SEARCH_QUERY)
    print(f"  Posts found (keyword search): {len(posts)}")
    full_sub_posts = fetch_all_posts_from_subreddits(FULL_SUBREDDITS)
    print(f"  Posts found (full subreddits): {len(full_sub_posts)}")
    comments = fetch_comments_from_subreddits(SEARCH_QUERY, COMMENT_SUBREDDITS)
    print(f"  Comments found: {len(comments)}")
    all_entries = posts + full_sub_posts + comments
    filtered = filter_entries(all_entries, EXCLUDED_SUBREDDITS, LOOKBACK_HOURS)
    print(f"  After dedup/filter: {len(filtered)}")
    if filtered:
        print("  Fetching upvote scores...")
        filtered = enrich_with_scores(filtered)
    message = build_slack_message(filtered, SEARCH_QUERY)
    post_to_slack(message, SLACK_CHANNEL)
    print("Done.")

if __name__ == "__main__":
    main()
