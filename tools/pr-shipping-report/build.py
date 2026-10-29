#!/usr/bin/env python3
"""Build the weekly shipping report artifact.

Pulls every merged pull request across the owner's GitHub repos, aggregates
PR counts and diff sizes, and renders template.html with the data baked in.

    python build.py [--owner jfreal] [--out report.html]

Requires the `gh` CLI, authenticated. On Windows, `pip install tzdata` if Python
has no time-zone database (`ZoneInfoNotFoundError` on start).
"""
import argparse, json, os, subprocess, sys, time, statistics as st, datetime as dt
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
BS = chr(92)  # kept out of a literal so the source carries no escape sequence
TZ = ZoneInfo("America/New_York")
# PRs at or under this many changed lines are treated as hand-written code.
# Anything bigger is almost always a data drop, a vendored tree or a lockfile.
CODE_MAX = 5000
WEEKS = 26
# Fetch ceilings. Hitting one is reported as a failure rather than silently truncating.
REPO_LIMIT = 1000
PR_LIMIT = 10000
# Months of history the naive trend line is fitted on, and months projected past today.
FIT_MONTHS = 6
FORECAST_MONTHS = 3

# Real-world events that changed how much time was available to ship. Marked on the
# timelines, and the forecast is fitted only on the stretch since the latest one -
# extrapolating across a step change reads the step as a rate of improvement.
EVENTS = [
    {"date": "2026-07-27", "label": "Laid off", "note": "full-time on these repos from here"},
]


def linfit(ys):
    """Least-squares line through ys at x = 0..n-1. Returns slope, intercept, r2."""
    n = len(ys)
    if n < 3:
        return None
    mx, my = (n - 1) / 2, sum(ys) / n
    sxx = sum((i - mx) ** 2 for i in range(n))
    sxy = sum((i - mx) * (y - my) for i, y in enumerate(ys))
    if sxx == 0:
        return None
    slope = sxy / sxx
    intercept = my - slope * mx
    sst = sum((y - my) ** 2 for y in ys)
    ssr = sum((y - (slope * i + intercept)) ** 2 for i, y in enumerate(ys))
    return {"slope": round(slope, 3), "intercept": round(intercept, 2),
            "r2": round(1 - ssr / sst, 3) if sst else None, "n": n}


def add_months(month, k):
    y, m = map(int, month.split("-"))
    m += k
    return f"{y + (m - 1) // 12:04d}-{(m - 1) % 12 + 1:02d}"


def gh(args, attempts=4):
    """Run gh, retrying the transient 5xx that the GraphQL API throws now and then."""
    for i in range(attempts):
        p = subprocess.run(["gh"] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if p.returncode == 0:
            return p.stdout
        err = p.stderr.strip() or f"gh {' '.join(args)} failed"
        # "unexpected end of JSON input" is gh reporting a truncated response body,
        # which the big repos hit often enough that one attempt is not enough.
        transient = any(c in err for c in ("HTTP 5", "timeout", "TLS handshake",
                                           "connection reset", "unexpected end of JSON input",
                                           "EOF", "502", "503"))
        if not transient or i == attempts - 1:
            raise RuntimeError(err)
        time.sleep(2 * (i + 1))


def fetch(owner):
    repos = [r["name"] for r in json.loads(
        gh(["repo", "list", owner, "--limit", str(REPO_LIMIT), "--json", "name"]))]
    rows, failed = [], []
    if len(repos) >= REPO_LIMIT:
        failed.append({"repo": owner, "error": f"hit the {REPO_LIMIT}-repo fetch limit; some repos are missing"})
    for name in sorted(repos):
        try:
            data = json.loads(gh(["pr", "list", "--repo", f"{owner}/{name}",
                                  "--state", "merged", "--limit", str(PR_LIMIT), "--json",
                                  "number,title,mergedAt,createdAt,additions,deletions,changedFiles,author"]))
        except RuntimeError as e:
            failed.append({"repo": name, "error": str(e)[:200]})
            continue
        if len(data) >= PR_LIMIT:
            failed.append({"repo": name, "error": f"hit the {PR_LIMIT}-PR fetch limit; older PRs are missing"})
        for p in data:
            if not p.get("mergedAt"):
                continue
            merged = dt.datetime.fromisoformat(p["mergedAt"].replace("Z", "+00:00"))
            created = dt.datetime.fromisoformat(p["createdAt"].replace("Z", "+00:00"))
            rows.append({
                "repo": name, "num": p["number"], "title": p["title"],
                "merged": merged, "created": created,
                "add": p["additions"], "del": p["deletions"], "files": p["changedFiles"],
                "author": (p.get("author") or {}).get("login", "unknown"),
                "hours": (merged - created).total_seconds() / 3600.0,
            })
    return rows, failed, len(repos)


def window(rows, now, days):
    rs = [r for r in rows if (now - r["merged"]).days < days]
    if not rs:
        return {"days": days, "prs": 0, "per_day": 0, "median_size": 0, "mean_size": 0,
                "p90_size": 0, "median_merge_hours": 0, "add": 0, "del": 0, "repos": 0}
    sizes = sorted(r["add"] + r["del"] for r in rs)
    hours = sorted(r["hours"] for r in rs)
    small = [r for r in rs if r["add"] + r["del"] <= CODE_MAX]
    return {
        "days": days, "prs": len(rs), "per_day": round(len(rs) / days, 2),
        "median_size": round(st.median(sizes)), "mean_size": round(st.mean(sizes)),
        "p90_size": sizes[max(0, int(0.9 * len(sizes)) - 1)],
        "median_merge_hours": round(st.median(hours), 1),
        "add": sum(r["add"] for r in small), "del": sum(r["del"] for r in small),
        "repos": len({r["repo"] for r in rs}),
    }


def build(rows, failed, repo_count):
    rows.sort(key=lambda r: r["merged"])
    # Rolling windows count back from the run, not from the last merge, so a quiet
    # week reads as quiet instead of repeating the last busy one.
    now = dt.datetime.now(dt.timezone.utc)
    last = max(r["merged"] for r in rows)
    small = [r for r in rows if r["add"] + r["del"] <= CODE_MAX]
    top10 = sorted(rows, key=lambda r: -r["add"])[:10]
    total_add = sum(r["add"] for r in rows)

    weekly = []
    byw = defaultdict(list)
    for r in rows:
        local = r["merged"].astimezone(TZ)
        byw[(local - dt.timedelta(days=local.weekday())).date()].append(r)
    today_l = now.astimezone(TZ).date()
    this_week = today_l - dt.timedelta(days=today_l.weekday())
    # Every calendar week, zero-PR weeks included, so quiet weeks pull the means down.
    for wk in (this_week - dt.timedelta(weeks=k) for k in range(WEEKS - 1, -1, -1)):
        rs = byw.get(wk, [])
        sm = [r for r in rs if r["add"] + r["del"] <= CODE_MAX]
        weekly.append({"week": wk.isoformat(), "prs": len(rs),
                       "add": sum(r["add"] for r in sm), "del": sum(r["del"] for r in sm),
                       "repos": len({r["repo"] for r in rs})})

    monthly = []
    bym = defaultdict(list)
    for r in rows:
        bym[r["merged"].astimezone(TZ).strftime("%Y-%m")].append(r)
    today = dt.datetime.now(TZ).date()
    this_month = today.strftime("%Y-%m")
    months_all, m = [], min(bym)
    while m <= this_month:
        months_all.append(m)
        m = add_months(m, 1)
    for m in months_all:
        rs = bym.get(m, [])
        sm = [r for r in rs if r["add"] + r["del"] <= CODE_MAX]
        y, mm = map(int, m.split("-"))
        dim = (dt.date(y + (mm == 12), mm % 12 + 1, 1) - dt.date(y, mm, 1)).days
        row = {"month": m, "prs": len(rs), "complete": m < this_month, "days": dim,
               "add": sum(r["add"] for r in sm), "del": sum(r["del"] for r in sm)}
        if m == this_month:
            row["elapsed"] = today.day
            row["pace"] = round(len(rs) / today.day * dim)
        monthly.append(row)

    # Trend and forecast, fitted only on finished months - a month still in progress
    # would drag the line down for no reason other than the calendar.
    done = [m for m in monthly if m["complete"]][-FIT_MONTHS:]
    naive = linfit([m["prs"] for m in done])
    naive_fit = None
    if naive:
        base = len(done) - 1
        naive_fit = {"fit": naive, "fit_from": done[0]["month"], "fit_to": done[-1]["month"],
                     "per_month": round(naive["slope"]),
                     "months": [{"month": add_months(done[-1]["month"], k),
                                 "prs": max(0, round(naive["slope"] * (base + k) + naive["intercept"]))}
                                for k in range(1, FORECAST_MONTHS + 2)]}

    repos = []
    byr = defaultdict(list)
    for r in rows:
        byr[r["repo"]].append(r)
    for name, rs in byr.items():
        sm = [r for r in rs if r["add"] + r["del"] <= CODE_MAX]
        last90 = [r for r in rs if (now - r["merged"]).days < 90]
        repos.append({"repo": name, "prs": len(rs), "prs90": len(last90),
                      "add": sum(r["add"] for r in sm), "del": sum(r["del"] for r in sm),
                      "median": round(st.median(sorted(r["add"] + r["del"] for r in rs)))})
    repos.sort(key=lambda r: -r["prs"])

    # --- the step change -------------------------------------------------
    ev = dt.date.fromisoformat(EVENTS[-1]["date"]) if EVENTS else None
    eras = None
    post_weeks = []
    last_day = now.astimezone(TZ).date()
    span = (last_day - ev).days + 1 if ev else 0
    if ev and span > 0:
        prev_from = ev - dt.timedelta(days=span)
        def count(a, b):  # [a, b)
            rs = [r for r in rows if a <= r["merged"].astimezone(TZ).date() < b]
            sm = [r for r in rs if r["add"] + r["del"] <= CODE_MAX]
            return {"from": a.isoformat(), "to": (b - dt.timedelta(days=1)).isoformat(),
                    "days": (b - a).days, "prs": len(rs),
                    "per_day": round(len(rs) / (b - a).days, 2),
                    "add": sum(r["add"] for r in sm),
                    "repos": len({r["repo"] for r in rs})}
        before = count(prev_from, ev)
        after = count(ev, last_day + dt.timedelta(days=1))
        eras = {"event": EVENTS[-1], "before": before, "after": after,
                "multiple": round(after["per_day"] / before["per_day"], 2) if before["per_day"] else None}
        # closed weeks that start on or after the event
        post_weeks = [w for w in weekly[:-1] if dt.date.fromisoformat(w["week"]) >= ev]

    # --- forecast, fitted on the stretch since the event only ------------
    forecast = None
    if post_weeks:
        vals = [w["prs"] for w in post_weeks]
        mean = st.mean(vals)
        sd = st.pstdev(vals) if len(vals) > 1 else 0
        trend = linfit(vals)
        rising = bool(trend and trend["r2"] is not None and trend["r2"] >= 0.3 and trend["slope"] > 0)
        today_d = dt.datetime.now(TZ).date()
        months = []
        cur = monthly[-1] if monthly[-1]["month"] == this_month else None
        if cur:
            left = cur["days"] - cur["elapsed"]
            months.append({"month": cur["month"], "prs": round(cur["prs"] + mean * left / 7),
                           "low": round(cur["prs"] + max(0, mean - sd) * left / 7),
                           "high": round(cur["prs"] + (mean + sd) * left / 7), "partial": True})
        nxt = cur["month"] if cur else monthly[-1]["month"]
        for k in range(1, FORECAST_MONTHS + 1):
            m = add_months(nxt, k)
            y, mm = map(int, m.split("-"))
            dim = (dt.date(y + (mm == 12), mm % 12 + 1, 1) - dt.date(y, mm, 1)).days
            months.append({"month": m, "prs": round(mean * dim / 7),
                           "low": round(max(0, mean - sd) * dim / 7),
                           "high": round((mean + sd) * dim / 7)})
        forecast = {"basis": "since " + EVENTS[-1]["date"], "weeks": len(vals),
                    "weekly_mean": round(mean, 1), "weekly_sd": round(sd, 1),
                    "weekly_low": min(vals), "weekly_high": max(vals),
                    "trend": trend, "rising": rising, "months": months,
                    "current_month": cur["month"] if cur else None}

    byday = Counter(r["merged"].astimezone(TZ).date() for r in rows)
    dow = Counter(r["merged"].astimezone(TZ).weekday() for r in rows)

    return {
        "generated_at": dt.datetime.now(TZ).isoformat(timespec="minutes"),
        "code_max": CODE_MAX,
        "totals": {
            "prs": len(rows), "add": total_add, "del": sum(r["del"] for r in rows),
            "files": sum(r["files"] for r in rows),
            "code_add": sum(r["add"] for r in small), "code_del": sum(r["del"] for r in small),
            "code_prs": len(small),
            "first": min(r["merged"] for r in rows).astimezone(TZ).date().isoformat(),
            "last": last.astimezone(TZ).date().isoformat(),
            "repos_with_prs": len(byr), "repos_scanned": repo_count,
            "top10_share": round(100 * sum(r["add"] for r in top10) / total_add, 1) if total_add else 0,
            "active_days": len(byday),
            "per_active_day": round(len(rows) / len(byday), 1),
        },
        # the seven days before the last seven, so the page can show a week-over-week move
        "prev7": len([r for r in rows if 7 <= (now - r["merged"]).days < 14]),
        "authors": [{"login": a, "prs": n} for a, n in Counter(r["author"] for r in rows).most_common()],
        "windows": [window(rows, now, d) for d in (7, 30, 90)],
        "weekly": weekly, "monthly": monthly, "repos": repos,
        "events": EVENTS,
        "eras": eras,
        "forecast": forecast,
        "naive_fit": naive_fit,
        # weekly trend is fitted on the closed weeks only, for the same reason
        "weekly_trend": linfit([w["prs"] for w in weekly[:-1]]),
        "adds_trend": linfit([w["add"] for w in weekly[-16:-1]]),
        "dow": [dow.get(i, 0) for i in range(7)],
        "busiest": [{"date": d.isoformat(), "prs": n} for d, n in byday.most_common(5)],
        "biggest": [{"date": r["merged"].astimezone(TZ).date().isoformat(), "repo": r["repo"],
                     "num": r["num"], "add": r["add"], "del": r["del"],
                     "title": r["title"][:80]} for r in top10[:6]],
        "failed": failed,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", default="jfreal")
    ap.add_argument("--out", default=os.path.join(HERE, "report.html"))
    a = ap.parse_args()

    rows, failed, repo_count = fetch(a.owner)
    if not rows:
        sys.exit("no merged pull requests found - is gh authenticated?")
    data = build(rows, failed, repo_count)

    tpl = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<" + BS + "/")
    open(a.out, "w", encoding="utf-8").write(tpl.replace('"__DATA__"', payload))

    json.dump(data, open(os.path.join(HERE, "report.json"), "w"), indent=1)
    t = data["totals"]
    print(f"{t['prs']} merged PRs across {t['repos_with_prs']} repos "
          f"({t['repos_scanned']} scanned), {t['first']} to {t['last']}")
    print(f"wrote {a.out}")
    e = data.get("eras")
    if e:
        print(f"{e['event']['label']} {e['event']['date']}: "
              f"{e['before']['per_day']}/day before -> {e['after']['per_day']}/day after "
              f"(x{e['multiple']}, equal {e['after']['days']}-day windows)")
    f = data.get("forecast")
    if f:
        print(f"since event: {f['weekly_mean']} PRs/week +/- {f['weekly_sd']} over {f['weeks']} weeks, "
              f"{'still rising' if f['rising'] else 'flat'} (r2 {f['trend']['r2'] if f['trend'] else 'n/a'}); next: "
              + ", ".join(f"{m['month']} {m['prs']}" for m in f["months"]))
    n = data.get("naive_fit")
    if n:
        print(f"  (naive whole-history fit would say {n['per_month']:+d}/month, "
              + ", ".join(f"{m['month']} {m['prs']}" for m in n["months"][:3]) + ")")
    if failed:
        print("failed repos:", ", ".join(f["repo"] for f in failed))


if __name__ == "__main__":
    main()
