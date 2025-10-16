"""
Standalone miner to fetch PR metadata and update the leaderboard immediately.

This script reuses the same logic and on-disk/HuggingFace formats as app.py, but
has no UI or scheduler. You can run it once, or run it in a loop for hours.

Datasets used:
- Agents: SWE-Arena/pr_agents
- PR metadata: SWE-Arena/pr_metadata
- Leaderboard: SWE-Arena/pr_leaderboard

Environment:
- Requires HF_TOKEN (for HuggingFace uploads)
- Optional GITHUB_TOKEN (highly recommended to avoid low rate limits)
- Reads .env if present

CLI flags:
- --debug / --no-debug: Same semantics as app.py (debug limits to 10 PRs/pattern
  and DOES NOT save to HF, mirroring app.py behavior).
- --loop: Keep running in a loop.
- --interval-seconds N: Sleep between loops (default 3600 seconds).

Note: In production mode (default), data will be saved to HuggingFace datasets.
"""

import argparse
import json
import os
import random
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests
from dotenv import load_dotenv
from huggingface_hub import HfApi, hf_hub_download


# =============================================================================
# Environment & CLI
# =============================================================================

load_dotenv()

parser = argparse.ArgumentParser(description="Immediate PR miner for SWE Arena")
parser.add_argument("--debug", "--DEBUG", action="store_true", help="Enable debug mode (limits PR retrieval to 10 per query; does NOT save to HF)")
parser.add_argument("--no-debug", "--production", action="store_true", help="Explicitly disable debug mode (force production mode)")
parser.add_argument("--loop", action="store_true", help="Run in a loop until interrupted")
parser.add_argument("--interval-seconds", type=int, default=3600, help="Sleep interval between loops in seconds (default: 3600)")
args = parser.parse_args()

# DEBUG MODE priority: 1) flags, 2) env var, 3) default False
if args.no_debug:
    DEBUG_MODE = False
elif args.debug:
    DEBUG_MODE = True
else:
    DEBUG_MODE = os.getenv("DEBUG_MODE", "False").lower() in ("true", "1", "yes")


# =============================================================================
# Constants (match app.py)
# =============================================================================

DEBUG_LEADERBOARD_CACHE = {}
DEBUG_PR_METADATA_CACHE = defaultdict(list)

AGENTS_REPO = "SWE-Arena/pr_agents"
LEADERBOARD_REPO = "SWE-Arena/pr_leaderboard"
PR_METADATA_REPO = "SWE-Arena/pr_metadata"


# =============================================================================
# Utilities & I/O (match app.py behavior exactly)
# =============================================================================

def load_jsonl(filename):
    """Load JSONL file and return list of dictionaries."""
    if not os.path.exists(filename):
        return []

    data = []
    with open(filename, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entry = json.loads(line)
                    # Migration: rename 'developer' to 'organization' if needed
                    if 'developer' in entry and 'organization' not in entry:
                        entry['organization'] = entry.pop('developer')
                    data.append(entry)
                except json.JSONDecodeError as e:
                    print(f"Warning: Skipping invalid JSON line: {e}")
    return data


def save_jsonl(filename, data):
    """Save list of dictionaries to JSONL file."""
    with open(filename, 'w', encoding='utf-8') as f:
        for item in data:
            f.write(json.dumps(item) + '\n')


def cache_to_dict(cache_list):
    return {entry['github_identifier']: entry for entry in cache_list}


def dict_to_cache(cache_dict):
    return list(cache_dict.values())


def get_github_token():
    token = os.getenv('GITHUB_TOKEN')
    if not token:
        print("Warning: GITHUB_TOKEN not found. API rate limits: 60/hour (authenticated: 5000/hour)")
    return token


def get_hf_token():
    token = os.getenv('HF_TOKEN')
    if not token:
        print("Warning: HF_TOKEN not found in environment variables")
    return token


# =============================================================================
# GitHub API with backoff (same as app.py)
# =============================================================================

def request_with_backoff(method, url, *, headers=None, params=None, json_body=None, data=None, max_retries=10, timeout=30):
    delay = 1.0
    for attempt in range(max_retries):
        try:
            resp = requests.request(
                method,
                url,
                headers=headers or {},
                params=params,
                json=json_body,
                data=data,
                timeout=timeout
            )

            status = resp.status_code

            if 200 <= status < 300:
                return resp

            if status in (403, 429) or 500 <= status < 600:
                wait = None
                retry_after = resp.headers.get('Retry-After') or resp.headers.get('retry-after')
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except Exception:
                        wait = None
                if wait is None and status in (403, 429):
                    reset_hdr = resp.headers.get('X-RateLimit-Reset') or resp.headers.get('x-ratelimit-reset')
                    if reset_hdr:
                        try:
                            reset_ts = int(float(reset_hdr))
                            wait = max(reset_ts - time.time() + 2, 1)
                        except Exception:
                            wait = None
                if wait is None:
                    wait = delay + random.uniform(0, 0.5)
                wait = max(1.0, min(wait, 120.0))
                print(f"GitHub API {status}. Backing off {wait:.1f}s (attempt {attempt + 1}/{max_retries})...")
                time.sleep(wait)
                delay = min(delay * 2, 60.0)
                continue

            return resp

        except requests.RequestException as e:
            wait = delay + random.uniform(0, 0.5)
            wait = max(1.0, min(wait, 60.0))
            print(f"Request error: {e}. Retrying in {wait:.1f}s (attempt {attempt + 1}/{max_retries})...")
            time.sleep(wait)
            delay = min(delay * 2, 60.0)

    print(f"Exceeded max retries for {url}")
    return None


def fetch_prs_with_time_partition(base_query, start_date, end_date, headers, prs_by_id, debug_limit=None):
    start_str = start_date.strftime('%Y-%m-%d')
    end_str = end_date.strftime('%Y-%m-%d')
    query = f'{base_query} created:{start_str}..{end_str}'
    print(f"  Searching range {start_str} to {end_str}...")
    page = 1
    per_page = 100
    total_in_partition = 0
    while True:
        if debug_limit is not None and total_in_partition >= debug_limit:
            print(f"    🐛 DEBUG MODE: Reached limit of {debug_limit} PRs, stopping...")
            return total_in_partition
        url = 'https://api.github.com/search/issues'
        params = {
            'q': query,
            'per_page': per_page,
            'page': page,
            'sort': 'created',
            'order': 'asc'
        }
        try:
            response = request_with_backoff('GET', url, headers=headers, params=params)
            if response is None:
                print(f"    Error: retries exhausted for range {start_str} to {end_str}")
                return total_in_partition
            if response.status_code != 200:
                print(f"    Error: HTTP {response.status_code} for range {start_str} to {end_str}")
                return total_in_partition
            data = response.json()
            total_count = data.get('total_count', 0)
            items = data.get('items', [])
            if not items:
                break
            for pr in items:
                pr_id = pr.get('id')
                if pr_id and pr_id not in prs_by_id:
                    prs_by_id[pr_id] = pr
                    total_in_partition += 1
            if total_count > 1000 and page == 10:
                print(f"    ⚠️ Hit 1000-result limit ({total_count} total). Splitting time range...")
                time_diff = end_date - start_date
                mid_date = start_date + time_diff / 2
                count1 = fetch_prs_with_time_partition(base_query, start_date, mid_date, headers, prs_by_id, debug_limit)
                count2 = fetch_prs_with_time_partition(base_query, mid_date + timedelta(days=1), end_date, headers, prs_by_id, debug_limit)
                return count1 + count2
            if len(items) < per_page or page >= 10:
                break
            page += 1
            time.sleep(0.5)
        except Exception as e:
            print(f"    Error fetching range {start_str} to {end_str}: {str(e)}")
            return total_in_partition
    if total_in_partition > 0:
        print(f"    ✓ Found {total_in_partition} PRs in range {start_str} to {end_str}")
    return total_in_partition


def extract_pr_metadata(pr):
    pull_request = pr.get('pull_request', {})
    created_at = pr.get('created_at')
    merged_at = pull_request.get('merged_at')
    closed_at = pr.get('closed_at')
    if merged_at:
        closed_at = None
    return {
        'html_url': pr.get('html_url'),
        'created_at': created_at,
        'merged_at': merged_at,
        'closed_at': closed_at
    }


def fetch_all_prs_metadata(identifier, agent_name, token=None, start_from_date=None, year=None):
    headers = {'Authorization': f'token {token}'} if token else {}
    debug_limit_per_pattern = 10 if DEBUG_MODE else None
    if DEBUG_MODE:
        print(f"\n🐛 DEBUG MODE ENABLED: Limiting to {debug_limit_per_pattern} PRs per query pattern")
    query_patterns = [
        f'is:pr author:{identifier}',
        f'is:pr "co-authored-by: {identifier}"',
        f'is:pr head:{identifier}/',
    ]
    prs_by_id = {}
    current_time = datetime.now(timezone.utc)
    six_months_ago = current_time - timedelta(days=180)
    if start_from_date:
        start_date = max(start_from_date, six_months_ago)
    else:
        start_date = six_months_ago
    end_date = current_time
    for query_pattern in query_patterns:
        print(f"\n🔍 Searching with query: {query_pattern}")
        print(f"   Time range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
        pattern_start_time = time.time()
        initial_count = len(prs_by_id)
        prs_found = fetch_prs_with_time_partition(
            query_pattern,
            start_date,
            end_date,
            headers,
            prs_by_id,
            debug_limit_per_pattern
        )
        pattern_duration = time.time() - pattern_start_time
        new_prs = len(prs_by_id) - initial_count
        print(f"   ✓ Pattern complete: {new_prs} new PRs found ({prs_found} total fetched, {len(prs_by_id) - initial_count - (prs_found - new_prs)} duplicates)")
        print(f"   ⏱️ Time taken: {pattern_duration:.1f} seconds")
        time.sleep(0.2 if DEBUG_MODE else 1.0)
    all_prs = list(prs_by_id.values())
    if DEBUG_MODE:
        print(f"\n✅ COMPLETE (DEBUG MODE): Found {len(all_prs)} unique PRs for {identifier}")
        print(f"   Note: In production mode, this would fetch ALL PRs")
    else:
        print(f"\n✅ COMPLETE: Found {len(all_prs)} unique PRs for {identifier}")
    print("📦 Extracting minimal metadata...")
    metadata_list = [extract_pr_metadata(pr) for pr in all_prs]
    original_size = sys.getsizeof(str(all_prs))
    metadata_size = sys.getsizeof(str(metadata_list))
    savings_pct = ((original_size - metadata_size) / original_size * 100) if original_size > 0 else 0
    print(f"💾 Memory efficiency: {original_size // 1024}KB → {metadata_size // 1024}KB (saved {savings_pct:.1f}%)")
    return metadata_list


def group_metadata_by_date(metadata_list):
    grouped = defaultdict(list)
    for pr_meta in metadata_list:
        created_at = pr_meta.get('created_at')
        if not created_at:
            continue
        try:
            dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            key = (dt.year, dt.month, dt.day)
            grouped[key].append(pr_meta)
        except Exception as e:
            print(f"Warning: Could not parse date '{created_at}': {e}")
    return dict(grouped)


def save_pr_metadata_to_hf(metadata_list, agent_identifier):
    if DEBUG_MODE:
        global DEBUG_PR_METADATA_CACHE
        existing = {pr['html_url']: pr for pr in DEBUG_PR_METADATA_CACHE[agent_identifier] if pr.get('html_url')}
        new = {pr['html_url']: pr for pr in metadata_list if pr.get('html_url')}
        existing.update(new)
        DEBUG_PR_METADATA_CACHE[agent_identifier] = list(existing.values())
        print(f"🐛 DEBUG MODE: Saved to in-memory cache only ({len(metadata_list)} PRs) - NOT saved to HuggingFace")
        return True
    try:
        token = get_hf_token()
        if not token:
            raise Exception("No HuggingFace token found")
        api = HfApi()
        grouped = group_metadata_by_date(metadata_list)
        for (pr_year, month, day), day_metadata in grouped.items():
            # New structure: [agent_identifier]/YYYY.MM.DD.jsonl
            filename = f"{agent_identifier}/{pr_year}.{month:02d}.{day:02d}.jsonl"
            local_filename = f"{pr_year}.{month:02d}.{day:02d}.jsonl"
            print(f"📤 Uploading {len(day_metadata)} PRs to {filename}...")
            existing_metadata = []
            try:
                file_path = hf_hub_download(
                    repo_id=PR_METADATA_REPO,
                    filename=filename,
                    repo_type="dataset",
                    token=token
                )
                existing_metadata = load_jsonl(file_path)
                print(f"   Found {len(existing_metadata)} existing PRs in {filename}")
            except Exception:
                print(f"   No existing file found for {filename}, creating new")
            existing_by_url = {meta['html_url']: meta for meta in existing_metadata if meta.get('html_url')}
            new_by_url = {meta['html_url']: meta for meta in day_metadata if meta.get('html_url')}
            existing_by_url.update(new_by_url)
            merged_metadata = list(existing_by_url.values())
            save_jsonl(local_filename, merged_metadata)
            api.upload_file(
                path_or_fileobj=local_filename,
                path_in_repo=filename,
                repo_id=PR_METADATA_REPO,
                repo_type="dataset",
                token=token
            )
            os.remove(local_filename)
            print(f"   ✓ Saved {len(merged_metadata)} total PRs to {filename}")
        return True
    except Exception as e:
        print(f"✗ Error saving PR metadata: {str(e)}")
        return False


def load_agents_from_hf():
    try:
        api = HfApi()
        agents = []
        files = api.list_repo_files(repo_id=AGENTS_REPO, repo_type="dataset")
        json_files = [f for f in files if f.endswith('.json')]
        print(f"Found {len(json_files)} agent files in {AGENTS_REPO}")
        for json_file in json_files:
            try:
                file_path = hf_hub_download(
                    repo_id=AGENTS_REPO,
                    filename=json_file,
                    repo_type="dataset"
                )
                with open(file_path, 'r') as f:
                    agent_data = json.load(f)
                    agents.append(agent_data)
            except Exception as e:
                print(f"Warning: Could not load {json_file}: {str(e)}")
                continue
        print(f"✓ Loaded {len(agents)} agents from HuggingFace")
        return agents
    except Exception as e:
        print(f"Could not load agents from HuggingFace: {str(e)}")
        return None


def load_pr_metadata_for_year(year):
    if DEBUG_MODE and DEBUG_PR_METADATA_CACHE:
        all_metadata = []
        for agent_identifier, metadata_list in DEBUG_PR_METADATA_CACHE.items():
            for pr_meta in metadata_list:
                pr_with_agent = pr_meta.copy()
                pr_with_agent['agent_identifier'] = agent_identifier
                all_metadata.append(pr_with_agent)
        if all_metadata:
            print(f"🐛 DEBUG MODE: Loading PR metadata from in-memory cache ({len(all_metadata)} PRs)")
            return all_metadata
    try:
        api = HfApi()
        token = get_hf_token()
        files = api.list_repo_files(repo_id=PR_METADATA_REPO, repo_type="dataset")
        # Filter for files matching the year pattern: [agent_identifier]/YYYY.MM.DD.jsonl
        year_str = str(year)
        year_files = []
        for f in files:
            if f.endswith('.jsonl'):
                parts = f.split('/')
                if len(parts) == 2:  # [agent_identifier]/YYYY.MM.DD.jsonl
                    filename = parts[1]
                    if filename.startswith(year_str + '.'):
                        year_files.append(f)
        print(f"📥 Loading PR metadata for {year} ({len(year_files)} daily files across all agents)...")
        all_metadata = []
        for filename in year_files:
            try:
                parts = filename.split('/')
                if len(parts) != 2:
                    print(f"   Warning: Unexpected filename format: {filename}")
                    continue
                agent_identifier = parts[0]
                file_path = hf_hub_download(
                    repo_id=PR_METADATA_REPO,
                    filename=filename,
                    repo_type="dataset",
                    token=token
                )
                day_metadata = load_jsonl(file_path)
                for pr_meta in day_metadata:
                    pr_meta['agent_identifier'] = agent_identifier
                all_metadata.extend(day_metadata)
                print(f"   ✓ Loaded {len(day_metadata)} PRs from {filename}")
            except Exception as e:
                print(f"   Warning: Could not load {filename}: {str(e)}")
        print(f"✓ Loaded {len(all_metadata)} total PRs for {year}")
        return all_metadata
    except Exception as e:
        print(f"✗ Error loading PR metadata for {year}: {str(e)}")
        return []


def get_latest_pr_date_for_agent(agent_identifier):
    try:
        api = HfApi()
        token = get_hf_token()
        files = api.list_repo_files(repo_id=PR_METADATA_REPO, repo_type="dataset")
        # New structure: [agent_identifier]/YYYY.MM.DD.jsonl
        agent_pattern = f"{agent_identifier}/"
        agent_files = [f for f in files if f.startswith(agent_pattern) and f.endswith('.jsonl')]
        if not agent_files:
            return None
        latest_date = None
        for filename in agent_files:
            try:
                file_path = hf_hub_download(
                    repo_id=PR_METADATA_REPO,
                    filename=filename,
                    repo_type="dataset",
                    token=token
                )
                metadata = load_jsonl(file_path)
                for pr in metadata:
                    created_at = pr.get('created_at')
                    if created_at:
                        try:
                            dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                            if latest_date is None or dt > latest_date:
                                latest_date = dt
                        except Exception:
                            continue
            except Exception:
                continue
        return latest_date
    except Exception:
        return None


def save_leaderboard_to_hf(cache_dict):
    if DEBUG_MODE:
        global DEBUG_LEADERBOARD_CACHE
        DEBUG_LEADERBOARD_CACHE = cache_dict.copy()
        data_list = dict_to_cache(cache_dict)
        print(f"🐛 DEBUG MODE: Saved to in-memory cache only ({len(data_list)} entries) - NOT saved to HuggingFace")
        return True
    try:
        token = get_hf_token()
        if not token:
            raise Exception("No HuggingFace token found. Please set HF_TOKEN in your environment.")
        data_list = dict_to_cache(cache_dict)
        df = pd.DataFrame(data_list)
        year = datetime.now().year
        filename = f"{year}.csv"
        df.to_csv(filename, index=False)
        api = HfApi()
        api.upload_file(
            path_or_fileobj=filename,
            path_in_repo=filename,
            repo_id=LEADERBOARD_REPO,
            repo_type="dataset",
            token=token
        )
        os.remove(filename)
        print(f"✓ Saved leaderboard to HuggingFace as {filename} ({len(data_list)} entries)")
        return True
    except Exception as e:
        print(f"✗ Error saving leaderboard: {str(e)}")
        return False


def calculate_pr_stats_from_metadata(metadata_list):
    total_prs = len(metadata_list)
    merged = sum(1 for pr_meta in metadata_list if pr_meta.get('merged_at'))
    closed_not_merged = sum(1 for pr_meta in metadata_list if pr_meta.get('closed_at') and not pr_meta.get('merged_at'))
    total_decisions = merged + closed_not_merged
    acceptance_rate = (merged / total_decisions * 100) if total_decisions > 0 else 0
    return {
        'total_prs': total_prs,
        'merged': merged,
        'acceptance_rate': round(acceptance_rate, 2),
    }


def update_all_agents_incremental():
    token = get_github_token()
    current_year = datetime.now().year
    agents = load_agents_from_hf()
    if not agents:
        print("No agents found in HuggingFace dataset")
        return {}
    cache_dict = {}
    for agent in agents:
        identifier = agent.get('github_identifier')
        agent_name = agent.get('agent_name', 'Unknown')
        if not identifier:
            print(f"Warning: Skipping agent without identifier: {agent}")
            continue
        try:
            print(f"\n{'='*80}")
            print(f"Processing: {agent_name} ({identifier})")
            print(f"{'='*80}")
            latest_pr_date = get_latest_pr_date_for_agent(identifier)
            if latest_pr_date:
                print(f"📅 Latest PR found: {latest_pr_date.strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"   Fetching only PRs created after this date...")
                start_from = latest_pr_date + timedelta(seconds=1)
            else:
                print(f"📅 No existing PRs found. Fetching all PR metadata...")
                start_from = None
            new_metadata = fetch_all_prs_metadata(
                identifier,
                agent_name,
                token,
                start_from_date=start_from
            )
            if new_metadata:
                print(f"💾 Saving {len(new_metadata)} new PR records...")
                save_pr_metadata_to_hf(new_metadata, identifier)
            print(f"📊 Calculating statistics from stored metadata...")
            all_year_metadata = load_pr_metadata_for_year(current_year)
            agent_metadata = [pr for pr in all_year_metadata if pr.get('agent_identifier') == identifier]
            stats = calculate_pr_stats_from_metadata(agent_metadata)
            cache_dict[identifier] = {
                'agent_name': agent_name,
                'organization': agent.get('organization', 'Unknown'),
                'github_identifier': identifier,
                **stats
            }
            print(f"✓ Updated {identifier}: {stats['total_prs']} PRs, {stats['acceptance_rate']}% acceptance")
        except Exception as e:
            print(f"✗ Error updating {identifier}: {str(e)}")
            import traceback
            traceback.print_exc()
            continue
    return cache_dict


def run_once():
    print("\n🚀 Immediate mining run started")
    cache_dict = update_all_agents_incremental()
    if cache_dict:
        save_leaderboard_to_hf(cache_dict)
    print("✅ Immediate mining run completed\n")


def main():
    if DEBUG_MODE:
        print("\n" + "="*80)
        print("🐛 DEBUG MODE ENABLED 🐛")
        print("="*80)
        print("PR retrieval is limited to 10 PRs per query pattern per agent")
        print("Data will NOT be saved to HuggingFace in debug mode.")
        print("="*80 + "\n")
    else:
        print("\n🚀 Starting in PRODUCTION MODE - full PR retrieval enabled")
        print()

    if not args.loop:
        run_once()
        return

    print(f"🔁 Loop mode enabled. Interval: {args.interval_seconds} seconds")
    try:
        while True:
            start = time.time()
            run_once()
            elapsed = time.time() - start
            sleep_for = max(0, args.interval_seconds - int(elapsed))
            if sleep_for > 0:
                print(f"😴 Sleeping {sleep_for} seconds before next run...")
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        print("\n👋 Loop interrupted by user. Exiting...")


if __name__ == "__main__":
    main()
