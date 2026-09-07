#!/usr/bin/env python3
"""Read-only check-policy report; never runs checks, retries, or changes GitHub."""

import argparse
import json
from pathlib import Path
import sys
import tomllib


INFRASTRUCTURE = {'runner_loss', 'cancelled_concurrency', 'network_timeout'}
CONCLUSIONS = {'success', 'failure', 'pending', *INFRASTRUCTURE}


def classify(config, results):
    """Class 3 describes an execution failure, not a permanent check category."""
    if not isinstance(config, dict) or not isinstance(results, dict):
        raise ValueError('checks and results must be tables/objects')
    names = set()
    rows = []
    for group in ('required', 'advisory', 'scheduled'):
        checks = config.get(group)
        if not isinstance(checks, list):
            raise ValueError(f'checks.{group} must be a list of check names')
        for name in checks:
            if not isinstance(name, str) or not name.strip() or name in names:
                raise ValueError('check names must be nonempty, unique, and disjoint')
            names.add(name)
            result = results.get(name, {'conclusion': 'pending'})
            if not isinstance(result, dict):
                raise ValueError(f'{name}: result must be an object')
            conclusion = result.get('conclusion')
            attempts = result.get('attempts', 1)
            if conclusion not in CONCLUSIONS or type(attempts) is not int or attempts < 1:
                raise ValueError(f'{name}: invalid conclusion or attempts')
            check_class = 2 if group == 'scheduled' else 1
            if conclusion in INFRASTRUCTURE:
                check_class = 3
                action = 'retry once automatically' if attempts == 1 else 'report infrastructure; no code verdict'
                if group == 'required':
                    action += '; required evidence unavailable'
            elif group == 'required' and conclusion != 'success':
                action = 'would block PR'
            else:
                action = 'does not block PR'
            rows.append({'name': name, 'group': group, 'class': check_class,
                         'conclusion': conclusion, 'action': action})
    if results.keys() - names:
        raise ValueError('results contain checks absent from the policy')
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['checks'])
    parser.add_argument('--workflow', type=Path, default=Path('workflow.toml'))
    parser.add_argument('--results', type=Path, help='optional JSON object keyed by check name')
    args = parser.parse_args()
    try:
        config = tomllib.loads(args.workflow.read_text())['checks']
        results = json.loads(args.results.read_text()) if args.results else {}
        rows = classify(config, results)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'UNVERIFIED: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(rows, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
