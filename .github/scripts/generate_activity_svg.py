#!/usr/bin/env python3
"""Generate the live GitHub activity panel as a self-contained SVG."""

from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


USERNAME = "red-angelo-panugaling"
GRAPHQL_URL = "https://api.github.com/graphql"
OUTPUT = Path(__file__).resolve().parents[2] / "assets" / "banners" / "activity-banner.svg"
PALETTE = {
    "NONE": "#211a35",
    "FIRST_QUARTILE": "#55245f",
    "SECOND_QUARTILE": "#8c327c",
    "THIRD_QUARTILE": "#c34eaa",
    "FOURTH_QUARTILE": "#72e4f2",
}
FALLBACK_LANGUAGE_COLORS = ["#72e4f2", "#d978df", "#c34eaa", "#a78bfa", "#f6a7cf", "#8fd3ff"]


def request_graphql(token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "red-angelo-panugaling-activity-panel",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        message = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub GraphQL returned HTTP {error.code}: {message}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not reach GitHub GraphQL: {error.reason}") from error

    if result.get("errors"):
        messages = "; ".join(item.get("message", "GraphQL error") for item in result["errors"])
        raise RuntimeError(
            f"GitHub GraphQL rejected the statistics query: {messages}. "
            "If this concerns user contribution access, configure the optional "
            "GH_STATS_TOKEN repository secret with the read:user scope."
        )
    return result["data"]


REPOSITORIES_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    createdAt
    repositories(
      first: 50,
      after: $cursor,
      privacy: PUBLIC,
      ownerAffiliations: OWNER,
      orderBy: {field: NAME, direction: ASC}
    ) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        languages(first: 20, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
  }
}
"""

CONTRIBUTIONS_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays {
            date
            contributionCount
            contributionLevel
          }
        }
      }
    }
  }
}
"""


def get_profile_and_languages(token: str) -> tuple[date, int, list[dict[str, Any]]]:
    cursor: str | None = None
    created_at: date | None = None
    repo_count = 0
    language_bytes: Counter[str] = Counter()
    language_colors: dict[str, str] = {}

    while True:
        data = request_graphql(token, REPOSITORIES_QUERY, {"login": USERNAME, "cursor": cursor})
        user = data.get("user")
        if not user:
            raise RuntimeError(f"GitHub user {USERNAME!r} was not found.")
        if created_at is None:
            created_at = date.fromisoformat(user["createdAt"][:10])

        repositories = user["repositories"]
        repo_count = repositories["totalCount"]
        for repository in repositories["nodes"]:
            for edge in repository["languages"]["edges"]:
                name = edge["node"]["name"]
                language_bytes[name] += edge["size"]
                color = edge["node"].get("color")
                if color and re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
                    language_colors.setdefault(name, color)

        page = repositories["pageInfo"]
        if not page["hasNextPage"]:
            break
        cursor = page["endCursor"]

    if created_at is None:
        raise RuntimeError("GitHub did not return the account creation date.")

    total_bytes = sum(language_bytes.values())
    languages = []
    for name, size in language_bytes.most_common(6):
        languages.append(
            {
                "name": name,
                "size": size,
                "percent": (size / total_bytes * 100) if total_bytes else 0,
                "color": language_colors.get(name),
            }
        )
    return created_at, repo_count, languages


def get_contributions(token: str, first_year: int, current_date: date) -> dict[date, dict[str, Any]]:
    days: dict[date, dict[str, Any]] = {}
    for year in range(first_year, current_date.year + 1):
        from_date = date(year, 1, 1)
        to_date = min(date(year, 12, 31), current_date)
        data = request_graphql(
            token,
            CONTRIBUTIONS_QUERY,
            {
                "login": USERNAME,
                "from": f"{from_date.isoformat()}T00:00:00Z",
                "to": f"{to_date.isoformat()}T23:59:59Z",
            },
        )
        user = data.get("user")
        if not user:
            raise RuntimeError(f"GitHub user {USERNAME!r} was not found while loading contributions.")
        calendar = user["contributionsCollection"]["contributionCalendar"]
        for week in calendar["weeks"]:
            for item in week["contributionDays"]:
                day = date.fromisoformat(item["date"])
                days[day] = {
                    "count": int(item["contributionCount"]),
                    "level": item["contributionLevel"],
                }
    return days


def calculate_streaks(days: dict[date, dict[str, Any]], today: date) -> tuple[int, int]:
    active_dates = sorted(day for day, item in days.items() if item["count"] > 0)
    longest = 0
    run = 0
    previous: date | None = None
    for day in active_dates:
        run = run + 1 if previous is not None and day == previous + timedelta(days=1) else 1
        longest = max(longest, run)
        previous = day

    anchor = today if days.get(today, {}).get("count", 0) > 0 else today - timedelta(days=1)
    current = 0
    day = anchor
    while days.get(day, {}).get("count", 0) > 0:
        current += 1
        day -= timedelta(days=1)
    return current, longest


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def text_element(
    x: float,
    y: float,
    value: Any,
    *,
    fill: str = "#e5e7eb",
    size: int = 14,
    weight: str = "400",
    spacing: int = 0,
    anchor: str | None = None,
) -> str:
    anchor_attr = f' text-anchor="{anchor}"' if anchor else ""
    return (
        f'<text x="{x}" y="{y}" fill="{fill}" font-family="Courier New,monospace" '
        f'font-size="{size}" font-weight="{weight}" letter-spacing="{spacing}"{anchor_attr}>'
        f"{esc(value)}</text>"
    )


def metric_card(x: int, label: str, value: int, accent: str) -> str:
    formatted_value = f"{value:,}"
    label_element = text_element(x + 14, 184, label, fill="#c4b5fd", size=10, spacing=1)
    value_element = text_element(x + 14, 229, formatted_value, fill="#f8fafc", size=31, weight="700")
    return f"""<g>
      <rect x="{x}" y="148" width="268" height="104" rx="8" fill="#100d20" stroke="{accent}" stroke-opacity=".7"/>
      <path d="M{x + 14} 164h32" stroke="{accent}" stroke-width="3"/>
      {label_element}
      {value_element}
    </g>"""


def render_contribution_grid(days: dict[date, dict[str, Any]], today: date) -> str:
    # 53 weeks keeps the current week visible while covering the last 365 days.
    start = today - timedelta(days=today.weekday() + 52 * 7)
    x0, y0, step, cell = 88, 393, 11.5, 8.5
    parts = []
    shown_from = today - timedelta(days=364)
    for offset in range(53 * 7):
        day = start + timedelta(days=offset)
        if day > today or day < shown_from:
            continue
        week = (day - start).days // 7
        row = day.weekday()
        item = days.get(day, {"count": 0, "level": "NONE"})
        color = PALETTE.get(item["level"], PALETTE["NONE"])
        title = f"{day.isoformat()}: {item['count']} contributions"
        parts.append(
            f'<rect x="{x0 + week * step:.1f}" y="{y0 + row * step:.1f}" '
            f'width="{cell}" height="{cell}" rx="2" fill="{color}"><title>{esc(title)}</title></rect>'
        )

    month_labels = []
    seen_months: set[tuple[int, int]] = set()
    for offset in range(53 * 7):
        day = start + timedelta(days=offset)
        if day > today or day < shown_from or day.day > 7:
            continue
        key = (day.year, day.month)
        if key in seen_months:
            continue
        seen_months.add(key)
        week = (day - start).days // 7
        month_labels.append(text_element(f"{x0 + week * step:.1f}", 377, day.strftime("%b"), fill="#9ca3af", size=11))
    parts.extend(month_labels)
    return "\n".join(parts)


def render_languages(languages: list[dict[str, Any]]) -> str:
    if not languages:
        return text_element(786, 411, "No public language data available")

    parts = []
    for index, language in enumerate(languages):
        y = 368 + index * 34
        color = language.get("color") or FALLBACK_LANGUAGE_COLORS[index % len(FALLBACK_LANGUAGE_COLORS)]
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            color = FALLBACK_LANGUAGE_COLORS[index % len(FALLBACK_LANGUAGE_COLORS)]
        percent = max(0.0, min(100.0, float(language["percent"])))
        bar_width = 340 * percent / 100
        parts.append(
            text_element(786, y, language["name"])
            + text_element(1116, y, f"{percent:.1f}%", fill="#9ca3af", size=11, anchor="end")
            + f'<rect x="786" y="{y + 9}" width="340" height="7" rx="3" fill="#27213b"/>'
            + f'<rect x="786" y="{y + 9}" width="{bar_width:.1f}" height="7" rx="3" fill="{color}"/>'
        )
    return "\n".join(parts)


def render_svg(stats: dict[str, Any]) -> str:
    today: date = stats["today"]
    contribution_total = int(stats["contribution_total"])
    repo_count = int(stats["public_repositories"])
    current_streak = int(stats["current_streak"])
    longest_streak = int(stats["longest_streak"])
    languages: list[dict[str, Any]] = stats["languages"]
    days: dict[date, dict[str, Any]] = stats["days"]
    updated_at: datetime = stats["updated_at"]

    skyline = []
    for x, y, width, height in [(825, 44, 58, 78), (891, 28, 71, 94), (970, 51, 60, 71), (1038, 20, 68, 102), (1114, 40, 61, 82)]:
        skyline.append(f'<rect x="{x}" y="{y}" width="{width}" height="{height}" fill="#0b0b19" stroke="#543366"/>')
        for wx in range(x + 10, x + width - 6, 18):
            for wy in range(y + 14, y + height - 7, 22):
                shade = "#f472b6" if (wx + wy) % 3 else "#67e8f9"
                skyline.append(f'<rect x="{wx}" y="{wy}" width="5" height="8" fill="{shade}" opacity=".65"/>')

    cards = "\n".join(
        [
            metric_card(40, "TOTAL CONTRIBUTIONS · LAST 365 DAYS", contribution_total, "#f472b6"),
            metric_card(328, "PUBLIC REPOSITORIES", repo_count, "#67e8f9"),
            metric_card(616, "CURRENT STREAK · DAYS", current_streak, "#c084fc"),
            metric_card(904, "LONGEST STREAK · DAYS", longest_streak, "#f472b6"),
        ]
    )

    grid = render_contribution_grid(days, today)
    language_rows = render_languages(languages)
    skyline_svg = "".join(skyline)
    updated_label = f"SOURCE: GITHUB GRAPHQL API · UPDATED {updated_at.strftime('%Y-%m-%d %H:%M UTC')}"
    legend_colors = list(PALETTE.values())
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="690" viewBox="0 0 1200 690" role="img" aria-labelledby="panel-title panel-desc">
  <title id="panel-title">GitHub Activity — {USERNAME}</title>
  <desc id="panel-desc">Daily updated contribution graph, past-year contribution total, public repository count, top languages, current contribution streak, and longest streak.</desc>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#080913"/><stop offset=".58" stop-color="#141027"/><stop offset="1" stop-color="#21102a"/></linearGradient>
    <pattern id="scanlines" width="4" height="4" patternUnits="userSpaceOnUse"><path d="M0 3.5h4" stroke="#fff" stroke-opacity=".025"/></pattern>
  </defs>
  <rect width="1200" height="690" rx="18" fill="url(#bg)"/>
  <rect width="1200" height="690" rx="18" fill="url(#scanlines)"/>
  <rect x="18" y="18" width="1164" height="654" rx="12" fill="none" stroke="#a855f7" stroke-opacity=".75" stroke-width="2"/>
  <path d="M18 61h18M18 61V43m1164 18h-18m18 0V43M18 629h18m-18 0v18m1164-18h-18m18 0v18" stroke="#f472b6" stroke-width="3"/>
  <circle cx="765" cy="56" r="25" fill="#c026d3" opacity=".32"/>
  {skyline_svg}
  {text_element(44, 46, "夜間データ端末  /  LIVE CONTRIBUTION FEED", fill="#f9a8d4", size=12, spacing=2)}
  {text_element(42, 91, "GITHUB ACTIVITY", fill="#f8fafc", size=32, weight="700", spacing=3)}
  {text_element(44, 116, "MY CODING JOURNEY, CONTRIBUTIONS AND PROGRESS", fill="#cbd5e1", size=13, spacing=1)}
  <path d="M44 130h1110" stroke="#7642a1" stroke-width="1"/>
  {cards}
  <g>
    <rect x="40" y="278" width="700" height="344" rx="8" fill="#0d0c19" stroke="#74429a"/>
    <path d="M40 320h700" stroke="#382448"/>
    {text_element(62, 306, "CONTRIBUTION GRAPH · LAST 365 DAYS", fill="#f9a8d4", size=12, spacing=2)}
    {text_element(64, 406, "MON", fill="#9ca3af", size=11)}{text_element(64, 432, "WED", fill="#9ca3af", size=11)}{text_element(64, 458, "FRI", fill="#9ca3af", size=11)}
    {grid}
    <g transform="translate(72 518)">
      {text_element(0, 45, "LESS", fill="#9ca3af", size=11)}
      <rect x="43" y="32" width="11" height="11" rx="2" fill="{legend_colors[0]}"/>
      <rect x="60" y="32" width="11" height="11" rx="2" fill="{legend_colors[1]}"/>
      <rect x="77" y="32" width="11" height="11" rx="2" fill="{legend_colors[2]}"/>
      <rect x="94" y="32" width="11" height="11" rx="2" fill="{legend_colors[3]}"/>
      <rect x="111" y="32" width="11" height="11" rx="2" fill="{legend_colors[4]}"/>
      {text_element(132, 45, "MORE", fill="#9ca3af", size=11)}
      {text_element(250, 45, "DAILY CONTRIBUTION COUNTS", fill="#9ca3af", size=11)}
    </g>
  </g>
  <g>
    <rect x="760" y="278" width="400" height="344" rx="8" fill="#0d0c19" stroke="#74429a"/>
    <path d="M760 320h400" stroke="#382448"/>
    {text_element(786, 306, "TOP PROGRAMMING LANGUAGES", fill="#f9a8d4", size=12, spacing=2)}
    {language_rows}
    {text_element(786, 601, "BASED ON PUBLIC REPOSITORY LANGUAGE BYTES", fill="#9ca3af", size=10)}
  </g>
  {text_element(44, 651, updated_label, fill="#9ca3af", size=11)}
  {text_element(1154, 651, USERNAME.upper(), fill="#9ca3af", size=11, anchor="end")}
</svg>
'''


def collect_stats(token: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    today = now.date()
    created_at, repo_count, languages = get_profile_and_languages(token)
    days = get_contributions(token, max(2008, created_at.year), today)
    first_day = today - timedelta(days=364)
    total = sum(item["count"] for day, item in days.items() if first_day <= day <= today)
    current_streak, longest_streak = calculate_streaks(days, today)
    return {
        "today": today,
        "contribution_total": total,
        "public_repositories": repo_count,
        "current_streak": current_streak,
        "longest_streak": longest_streak,
        "languages": languages,
        "days": days,
        "updated_at": now,
    }


def validate_svg(svg: str, stats: dict[str, Any]) -> None:
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as error:
        raise RuntimeError(f"Generated activity SVG is invalid XML: {error}") from error
    if not root.tag.endswith("svg"):
        raise RuntimeError("Generated activity document is not an SVG.")
    required = [
        "GITHUB ACTIVITY",
        "CONTRIBUTION GRAPH · LAST 365 DAYS",
        "TOP PROGRAMMING LANGUAGES",
        "TOTAL CONTRIBUTIONS · LAST 365 DAYS",
        f"{int(stats['contribution_total']):,}",
        f"{int(stats['public_repositories']):,}",
        f"{int(stats['current_streak']):,}",
        f"{int(stats['longest_streak']):,}",
    ]
    for text in required:
        if text not in svg:
            raise RuntimeError(f"Generated activity SVG is missing expected content: {text}")
    if "{stats" in svg or "None" in svg:
        raise RuntimeError("Generated activity SVG contains an unresolved value.")
    if "<script" in svg or "<style" in svg or "<image" in svg:
        raise RuntimeError("Generated SVG must use self-contained native SVG elements only.")
    if svg.count(" contributions</title>") < 360:
        raise RuntimeError("Generated contribution graph is missing daily contribution cells.")


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_STATS_TOKEN")
    if not token:
        print("Set GITHUB_TOKEN (or GH_STATS_TOKEN) to read the GitHub GraphQL API.", file=sys.stderr)
        return 2
    stats = collect_stats(token)
    svg = render_svg(stats)
    validate_svg(svg, stats)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(svg, encoding="utf-8", newline="\n")
    print(f"Validated and generated {OUTPUT.relative_to(OUTPUT.parents[2])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
