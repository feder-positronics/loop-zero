#!/usr/bin/env python3
"""Measure delivery windows from GitHub REST and terminal loopzero check receipts.

JSON goes to stdout and the weekly table to stderr. Windows are UTC [since, until).
Issue metrics use current labels and GitHub's latest closure; a completed order's
closure is its acceptance timestamp. Use --base to scope merges to one branch.

--check-receipts takes a repository-scoped JSONL ledger (use a host-wide ledger
only for a host-wide comparison). Terminal job/exit records use command_class
"loopzero-check", end_utc, exit_code, and outcome "ok" or "fail", as in the
existing slot receipts. Cancelled, starved, and refused jobs are separate from
passes/failures. The ledger's first terminal timestamp bounds known coverage.

--host-command is an argv string (shell quoting, no shell). Its stdin is JSON
with repo and named windows, including their since/until dates. Return a JSON
object keyed by window name, with owner_messages (count), tokens_by_role (object
of counts), and spend (number, in the caller's chosen currency). For liveness
return an optional top-level owners object mapping names to booleans (true is
alive), as of window end. Pass --owner to check a particular name or
--dead-owner to supply independently confirmed downtime.

--record writes one private snapshot under --frozen-inputs. Replay with the
same repo, windows, labels, and alert options. Replay never executes commands.
"""

import argparse
import datetime as dt
import json
import math
import random
import re
import shlex
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode

UNKNOWN = "UNKNOWN"
DAY = dt.timedelta(days=1)
HOUR = dt.timedelta(hours=1)
HOST_KEYS = ("owner_messages", "tokens_by_role", "spend")
TABLE_KEYS = (
    "outcomes_closed", "order_to_acceptance_median_hours", "merges",
    "checks_passed", "checks_failed", "oldest_open_p0_hours",
)
NUMERIC_KEYS = set(TABLE_KEYS) | {
    "orders_accepted", "order_to_acceptance_p90_hours", "open_to_merge_median_hours",
    "open_to_merge_p90_hours", "prs_opened", "prs_closed_unmerged", "open_p0",
    "owner_messages", "spend",
}
SOURCE_ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError,
                 OverflowError, subprocess.SubprocessError)


def timestamp(value):
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return parsed.replace(tzinfo=dt.UTC)
        raise ValueError("timestamps require a timezone")
    return parsed.astimezone(dt.UTC)


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def run_json(argv, stdin=""):
    result = subprocess.run(argv, input=stdin, capture_output=True, text=True,
                            check=True, timeout=45)
    return json.loads(result.stdout)


class Inputs:
    def __init__(self, directory, record):
        self.path = directory / "inputs.json" if directory else None
        self.replay = bool(directory and not record)
        self.data = {}
        self.alerts = []
        if self.replay:
            self.data = self.read("snapshot", lambda: json.loads(self.path.read_text()), dict) or {}

    def alert(self, source, reason="unavailable"):
        finding = {"kind": "unknown", "source": source, "reason": reason}
        if finding not in self.alerts:
            self.alerts.append(finding)

    def read(self, name, collect, expected):
        try:
            value = collect()
            if not isinstance(value, expected):
                raise TypeError("unexpected data type")
            return value
        except SOURCE_ERRORS:
            self.alert(name)
            return None

    def get(self, name, collect, expected):
        if name not in self.data and not self.replay:
            self.data[name] = self.read(name, collect, expected)
        value = self.data.get(name)
        if not isinstance(value, expected):
            self.alert(name)
            return None
        return value

    def save(self):
        if self.path and not self.replay:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tempfile.NamedTemporaryFile(
                    mode="w", dir=self.path.parent, delete=False, encoding="utf-8"
                ) as stream:
                    json.dump(self.data, stream, sort_keys=True)
                Path(stream.name).replace(self.path)
            except OSError as error:
                print(f"Cannot record metrics: could not write snapshot at {self.path}: {error}", file=sys.stderr)
                return False
        return True


def pages(inputs, path, cutoff=None, **params):
    """Use the host collector's update-ordered pull pagination and cutoff."""
    rows = []
    for page in range(1, sys.maxsize):
        endpoint = f"{path}?{urlencode(dict(params, per_page=100, page=page))}"
        batch = inputs.get(endpoint, lambda url=endpoint: run_json(
            ["gh", "api", "--method", "GET", url]), list)
        if batch is None:
            return None
        rows.extend(batch)
        if len(batch) < 100 or (
            cutoff is not None and timestamp(batch[-1]["updated_at"]) < cutoff
        ):
            return rows
    return rows


def receipts(path, lower, end):
    """Keep raw terminal records in scope and retain the source's coverage bound."""
    if path is None:
        raise ValueError("receipt input absent")
    rows = []
    first = None
    # Bound a concurrently appended ledger to the bytes visible at open.
    with path.open("rb") as stream:
        remaining = stream.seek(0, 2)
        stream.seek(0)
        while remaining:
            line = stream.readline(remaining)
            remaining -= len(line)
            if not line.endswith(b"\n"):
                raise ValueError("incomplete receipt")
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("event") not in {"job", "exit"} or not row.get("end_utc"):
                continue
            finished = timestamp(row["end_utc"])
            first = finished if first is None else min(first, finished)
            if lower <= finished < end and row.get("command_class") == "loopzero-check":
                rows.append(row)
    if first is None:
        raise ValueError("no terminal receipts")
    return {"since": stamp(max(first, lower)), "until": stamp(end), "rows": rows}


def check_attempts(source):
    attempts = {}
    for row in source["rows"]:
        if row["outcome"] not in {"ok", "fail"}:
            continue
        code = row["exit_code"]
        if type(code) is not int or (code == 0) != (row["outcome"] == "ok"):
            raise ValueError("inconsistent check receipt")
        identity = tuple(row.get(key) for key in ("queued_utc", "pid", "ticket"))
        if None in identity:
            identity = (json.dumps(row, sort_keys=True),)
        attempt = (timestamp(row["end_utc"]), code)
        if attempts.setdefault(identity, attempt) != attempt:
            raise ValueError("conflicting check receipt")
    return timestamp(source["since"]), timestamp(source["until"]), list(attempts.values())


def distribution(values):
    values = sorted(values)
    if not values:
        return UNKNOWN, UNKNOWN
    return statistics.median(values), values[math.ceil(0.9 * len(values)) - 1]


def window_stats(start, end, pulls, outcomes, orders, p0, checks):
    def inside(value):
        return value is not None and start <= timestamp(value) < end

    def completed(rows):
        return [issue for issue in rows if issue.get("state_reason") == "completed"
                and inside(issue.get("closed_at"))]

    result = dict.fromkeys(NUMERIC_KEYS, UNKNOWN)
    result.update(since=stamp(start), until=stamp(end))
    if pulls is not None:
        merged = [p for p in pulls if inside(p.get("merged_at"))]
        median, p90 = distribution(
            (timestamp(p["merged_at"]) - timestamp(p["created_at"])) / HOUR for p in merged
        )
        result.update(merges=len(merged), open_to_merge_median_hours=median,
                      open_to_merge_p90_hours=p90,
                      prs_opened=sum(inside(p["created_at"]) for p in pulls),
                      prs_closed_unmerged=sum(inside(p.get("closed_at")) and not p.get("merged_at")
                                               for p in pulls))
    if outcomes is not None:
        result["outcomes_closed"] = len(completed(outcomes))
    if orders is not None:
        accepted = completed(orders)
        median, p90 = distribution(
            (timestamp(i["closed_at"]) - timestamp(i["created_at"])) / HOUR for i in accepted
        )
        result.update(orders_accepted=len(accepted), order_to_acceptance_median_hours=median,
                      order_to_acceptance_p90_hours=p90)
    if p0 is not None and histories_complete(p0, end):
        open_orders = open_at(p0, end)
        result.update(open_p0=len(open_orders), oldest_open_p0_hours=max(
            ((end - timestamp(i["created_at"])) / HOUR for i in open_orders), default=UNKNOWN))
    if checks is not None and checks[0] <= start and end <= checks[1]:
        codes = [code for finished, code in checks[2] if start <= finished < end]
        result.update(checks_passed=codes.count(0), checks_failed=len(codes) - codes.count(0))
    return result


def open_at(issues, end):
    def still_open(issue):
        if "state_changes" in issue:
            changes = issue["state_changes"]
            return changes is not None and next((kind == "reopened" for when, kind in changes if when < end), True)
        return not issue.get("closed_at") or timestamp(issue["closed_at"]) >= end

    return [issue for issue in issues if timestamp(issue["created_at"]) < end and still_open(issue)]


def histories_complete(issues, end):
    return all(issue["state_changes"] is not None or timestamp(issue["created_at"]) >= end for issue in issues)


def weekly_sample(repo, start, end, pulls, orders, open_orders, stalls, *, stalls_complete=True):
    oldest = sorted(open_at(open_orders or [], end), key=lambda row: (
        timestamp(row["created_at"]), row["number"]))
    accepted = sorted(
        (row for row in orders or [] if row.get("state_reason") == "completed"
         and row.get("closed_at") and start <= timestamp(row["closed_at"]) < end),
        key=lambda row: (timestamp(row["created_at"]) - timestamp(row["closed_at"]),
                         row["number"]),
    )
    stalled = sorted(stalls or [],
                     key=lambda alert: (-alert["hours"], alert["issue"]))
    merged = sorted({row["number"] for row in pulls or []
                     if row.get("merged_at") and start <= timestamp(row["merged_at"]) < end})
    seed = f"{repo}@{stamp(end)}"
    sample = [{"kind": "merged_control", "number": number}
              for number in random.Random(seed).sample(merged, min(2, len(merged)))]
    seen = {item["number"] for item in sample}

    for kind, numbers, limit in (
        ("slowest_accepted", (row["number"] for row in accepted), 2),
        ("stalled_p0", (alert["issue"] for alert in stalled), 1),
        ("oldest_open_order", (row["number"] for row in oldest), 4),
    ):
        picked = 0
        for number in numbers:
            if picked == limit or len(sample) == 8:
                break
            if number not in seen:
                sample.append({"kind": kind, "number": number})
                seen.add(number)
                picked += 1
    return {
        "seed": seed, "items": sample, "shortfall": max(0, 6 - len(sample)),
        "pool_sizes": {
            "merged_control": len(merged) if pulls is not None else UNKNOWN,
            "slowest_accepted": len(accepted) if orders is not None else UNKNOWN,
            "stalled_p0": len(stalled) if stalls_complete and stalls is not None else UNKNOWN,
            "oldest_open_order": len(oldest) if open_orders is not None else UNKNOWN,
        },
    }


def host_fields(data, name):
    values = data.get(name)
    if not isinstance(values, dict):
        return dict.fromkeys(HOST_KEYS, UNKNOWN)

    def count(value):
        return type(value) is int and value >= 0

    tokens = values.get("tokens_by_role")
    spend = values.get("spend")
    messages = values.get("owner_messages")
    valid_spend = count(spend) or (type(spend) is float and math.isfinite(spend) and spend >= 0)
    return {
        "owner_messages": messages if count(messages) else UNKNOWN,
        "tokens_by_role": tokens if isinstance(tokens, dict) and
        all(count(value) for value in tokens.values()) else UNKNOWN,
        "spend": spend if valid_spend else UNKNOWN,
    }


def _complete_pr_commits(inputs, path, commits):
    if commits is None:
        return False
    if len(commits) < 250:
        return True
    detail = inputs.get(path, lambda: run_json(["gh", "api", "--method", "GET", path]), dict)
    count = (detail or {}).get("commits")
    return type(count) is int and count == len(commits)


def issue_progress(inputs, repo, issue, end):
    timeline = pages(inputs, f"repos/{repo}/issues/{issue['number']}/timeline")
    if timeline is None:
        raise ValueError("issue timeline unavailable")
    dates = [timestamp(issue["created_at"])]
    for event in timeline:
        kind = event.get("event")
        if kind in {"commented", "committed", "referenced"}:
            date = event.get("created_at") or (event.get("committer") or {}).get("date")
            if date and (kind != "referenced" or event.get("commit_id")):
                dates.append(timestamp(date))
        if kind == "cross-referenced":
            source = (event.get("source") or {}).get("issue") or {}
            pr = source.get("pull_request") or {}
            match = re.fullmatch(r"https://api\.github\.com/repos/([\w.-]+/[\w.-]+)/pulls/(\d+)",
                                 pr.get("url", ""))
            if not match or timestamp(event["created_at"]) >= end:
                continue
            dates += [timestamp(source["created_at"]), timestamp(event["created_at"])]
            pull = f"repos/{match[1]}/pulls/{match[2]}"
            commits = pages(inputs, f"{pull}/commits")
            comments = pages(inputs, f"repos/{match[1]}/issues/{match[2]}/comments")
            if comments is None or not _complete_pr_commits(inputs, pull, commits):
                raise ValueError("linked PR activity unavailable or truncated")
            dates += [timestamp(item["commit"]["committer"]["date"]) for item in commits]
            dates += [timestamp(item["created_at"]) for item in comments]
    return max(date for date in dates if date < end)


def stalled_p0s(inputs, args, p0, end):
    if args.stall_hours is None or p0 is None:
        return [], False
    stalls, complete = [], histories_complete(p0, end)
    for issue in sorted(open_at(p0, end), key=lambda row: row["number"]):
        name = f"p0/#{issue['number']}/activity"
        latest = inputs.read(name, lambda order=issue: issue_progress(
            inputs, args.repo, order, end), dt.datetime)
        if latest is None:
            complete = False
        elif (end - latest) / HOUR >= args.stall_hours:
            stalls.append({"kind": "stalled_p0", "issue": issue["number"],
                           "last_activity": stamp(latest), "hours": (end - latest) / HOUR})
    return stalls, complete


def find_alerts(inputs, args, metrics, p0, host, end):
    for key, minimum, maximum in args.band:
        value = metrics[key]
        if value != UNKNOWN and not minimum <= value <= maximum:
            inputs.alerts.append({"kind": "outside_band", "metric": key, "value": value,
                                  "min": minimum, "max": maximum})
    stalls, _ = stalled_p0s(inputs, args, p0, end)
    inputs.alerts.extend(stalls)
    owners = host.get("owners", {})
    if not isinstance(owners, dict):
        owners = {}
    names = args.owner or sorted(owners)
    dead = set(args.dead_owner)
    for owner in names:
        if owners.get(owner) is False:
            dead.add(owner)
        elif owners.get(owner) is not True and owner not in dead:
            inputs.alert(f"owner/{owner}")
    inputs.alerts.extend({"kind": "dead_owner", "owner": owner} for owner in sorted(dead))


def load_github(inputs, args, lower):
    root = f"repos/{args.repo}"

    def pulls():
        rows = pages(inputs, f"{root}/pulls", cutoff=lower, state="all", sort="updated",
                     direction="desc", **({"base": args.base} if args.base else {}))
        return validate_rows(rows, ("closed_at", "merged_at"))

    return (
        inputs.read("pulls", pulls, list),
        inputs.read("outcome_issues", lambda: load_issues(
            inputs, args.repo, args.label_outcome, since=stamp(lower)), list),
        inputs.read("order_issues", lambda: load_issues(
            inputs, args.repo, args.label_order, since=stamp(lower)), list),
        inputs.read("p0_issues", lambda: list(load_p0(inputs, args)), list),
    )


def with_histories(inputs, repo, issues, end, source):
    def state_changes(issue):
        timeline = pages(inputs, f"repos/{repo}/issues/{issue['number']}/timeline")
        if timeline is None:
            raise ValueError("issue timeline unavailable")
        changes = [(timestamp(event["created_at"]), event["event"]) for event in timeline
                   if event.get("event") in {"closed", "reopened"}]
        # Reverse chronological order, with the last event first for tied timestamps.
        return sorted(reversed(changes), key=lambda change: change[0], reverse=True)

    for issue in issues:
        if timestamp(issue["created_at"]) < end:
            changes = inputs.read(f"{source}/#{issue['number']}/state", lambda row=issue: state_changes(row), list)
            yield dict(issue, state_changes=changes)


def load_p0(inputs, args):
    return with_histories(inputs, args.repo, load_issues(inputs, args.repo, args.label_p0), args.end, "p0")


def load_issues(inputs, repo, label, state="all", **params):
    if not label:
        raise ValueError("label absent")
    rows = pages(inputs, f"repos/{repo}/issues", state=state, labels=label, **params)
    return validate_rows([row for row in rows if "pull_request" not in row], ("closed_at",))


def validate_rows(rows, date_fields):
    unique = {}
    for row in rows:
        timestamp(row["created_at"])
        for key in date_fields:
            if row[key] is not None:
                timestamp(row[key])
        unique[row["number"]] = row
    return list(unique.values())


def number(value):
    return f"{value:.2f}" if type(value) is float else str(value)


def print_table(trend):
    names = ("Week -3", "Week -2", "Previous 7d", "Last 7d")
    print(f"{'Metric':38} " + " ".join(f"{name:>12}" for name in names), file=sys.stderr)
    for key in TABLE_KEYS:
        print(f"{key:38} " + " ".join(f"{number(w[key]):>12}" for w in trend), file=sys.stderr)


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True, help="GitHub OWNER/REPO")
    parser.add_argument("--base", help="base branch for PR metrics")
    parser.add_argument("--since", type=timestamp, help="start of an additional custom window")
    parser.add_argument("--until", dest="end", type=timestamp, required=True)
    parser.add_argument("--label-outcome")
    parser.add_argument("--label-order")
    parser.add_argument("--label-p0")
    parser.add_argument("--check-receipts", type=Path)
    parser.add_argument("--host-command")
    parser.add_argument("--frozen-inputs", type=Path)
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--alerts", action="store_true")
    parser.add_argument("--band", nargs=3, metavar=("METRIC", "MIN", "MAX"), action="append", default=[])
    parser.add_argument("--stall-hours", type=float)
    parser.add_argument("--owner", action="append", default=[])
    parser.add_argument("--dead-owner", action="append", default=[])
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo) or any(
        part in {".", ".."} for part in args.repo.split("/")
    ):
        parser.error("--repo must be OWNER/REPO")
    if args.since is not None and args.since >= args.end:
        parser.error("--since must precede the window end")
    if args.record and args.frozen_inputs is None:
        parser.error("--record requires --frozen-inputs")
    if args.stall_hours is not None and (not math.isfinite(args.stall_hours) or args.stall_hours <= 0):
        parser.error("--stall-hours must be positive and finite")
    try:
        args.host_command = shlex.split(args.host_command) if args.host_command is not None else None
        if args.host_command == []:
            raise ValueError("empty --host-command")
        args.band = [(key, float(lo), float(hi)) for key, lo, hi in args.band]
        for key, lo, hi in args.band:
            if key not in NUMERIC_KEYS or not (math.isfinite(lo) and lo <= hi and math.isfinite(hi)):
                raise ValueError("each --band requires a numeric metric and finite, ordered limits")
        args.end - 28 * DAY
    except (ValueError, OverflowError) as error:
        parser.error(str(error))
    return args


def main(argv=None):
    args = parse_args(argv)
    inputs = Inputs(args.frozen_inputs, args.record)
    if inputs.replay and inputs.alerts:
        print(f"Cannot replay metrics: no readable snapshot at {inputs.path}. Run with --record first.", file=sys.stderr)
        return 1
    end = args.end
    weekly = [(end - day * DAY, end - (day - 7) * DAY) for day in (28, 21, 14, 7)]
    names = ("week_minus_3", "week_minus_2", "previous_7_days", "last_7_days")
    windows = dict(zip(names, weekly, strict=True))
    if args.since is not None:
        windows["custom_window"] = (args.since, end)
    lower = min(start for start, _ in windows.values())
    pulls, outcomes, orders, p0 = load_github(inputs, args, lower)
    source = inputs.get(f"checks/{args.repo}", lambda: receipts(args.check_receipts, lower, end), dict)
    checks = inputs.read("check_receipts", lambda: check_attempts(source), tuple) if source else None
    host_request = {"repo": args.repo, "windows": {
        key: {"since": stamp(start), "until": stamp(stop)} for key, (start, stop) in windows.items()
    }}
    host_key = "host/" + json.dumps(host_request, sort_keys=True)
    host = inputs.get(host_key, lambda: run_json(args.host_command, json.dumps(host_request))
                      if args.host_command else {}, dict) or {}
    metrics = {}
    for name, (start, stop) in windows.items():
        row = window_stats(start, stop, pulls, outcomes, orders, p0, checks)
        row.update(host_fields(host, name))
        for key in sorted(row):
            if row[key] == UNKNOWN:
                inputs.alert(f"{name}/{key}", "no data or no observations")
        metrics[name] = row
    selected = metrics.get("custom_window", metrics["last_7_days"])
    if args.alerts:
        find_alerts(inputs, args, selected, p0, host, end)
        if not inputs.save():
            return 1
        print(json.dumps({"alerts": inputs.alerts}, indent=2, sort_keys=True))
        return 0
    open_orders = inputs.read("open_order_issues", lambda: load_issues(
        inputs, args.repo, args.label_order, state="open"), list)
    if orders is None:
        open_orders = None
    elif open_orders is not None:
        # Include recent orders that closed after a historical report's end.
        open_orders = list({row["number"]: row for row in [*orders, *open_orders]}.values())
        open_orders = list(with_histories(inputs, args.repo, open_at(open_orders, end), end, "order"))
        if not histories_complete(open_orders, end):
            open_orders = None
    stalls, stalls_complete = stalled_p0s(inputs, args, p0, end)
    if not inputs.save():
        return 1
    trend = [metrics[name] for name in names]
    report = {
        "repo": args.repo, "last_7_days": trend[-1], "previous_7_days": trend[-2],
        "trend_4_weeks": trend, "custom_window": metrics.get("custom_window"),
        "alerts": inputs.alerts,
        "weekly_sample": weekly_sample(args.repo, weekly[-1][0], end, pulls, orders,
                                       open_orders, stalls, stalls_complete=stalls_complete),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    print_table(trend)
    return 0


if __name__ == "__main__":
    sys.exit(main())
