"""Request recording and response matching shared by the two fake gh runners."""

import json
import os


def replay(
    argv: list[str], calls: str, scenario: str, cwd: str, token: str | None,
) -> tuple[int, str, str]:
    record = {"argv": argv, "token": token}
    input_files = {}
    for flag in ("--input", "--body-file"):
        if flag in argv:
            with open(os.path.join(cwd, argv[argv.index(flag) + 1])) as payload:
                content = payload.read()
            record[flag] = content
            input_files[flag] = content
    record["input_files"] = input_files
    log = []
    if os.path.exists(calls):
        with open(calls) as call_log:
            log = json.load(call_log)
    log.append(record)
    with open(calls, "w") as call_log:
        call_log.write(json.dumps(log))

    with open(scenario) as scenario_file:
        plan = json.load(scenario_file)
    strict = plan.get("strict")
    if strict is not None:
        index = len(log) - 1
        if index >= len(strict):
            return 97, "", f"fake gh: exhausted after {len(strict)} interactions; got {argv!r}\n"
        resp = strict[index]
        normalized = list(argv)
        for flag in ("--input", "--body-file"):
            if flag in normalized:
                normalized[normalized.index(flag) + 1] = "<temp-file>"
        if normalized != resp["argv"]:
            return 97, "", ("fake gh: unexpected argv\n"
                            f"expected: {resp['argv']!r}\nactual:   {normalized!r}\n")
        expected_files = resp.get("input_files", {})
        if expected_files != input_files:
            return 97, "", ("fake gh: unexpected input files\n"
                            f"expected: {expected_files!r}\nactual:   {input_files!r}\n")
    else:
        compat = plan.get("compat", {})

        def response_key(args):
            basic = " ".join(args[:2])
            if basic == "api graphql" and any("viewer { login }" in arg for arg in args):
                basic += " viewer"
            method = args[args.index("--method") + 1] if "--method" in args else ""
            specific = basic + " --method " + method
            return specific if specific in compat else basic

        key = response_key(argv)
        responses = compat.get(key)
        if responses is None:
            return 97, "", f"fake gh: no response for {key}\n"
        index = sum(1 for call in log[:-1] if response_key(call["argv"]) == key)
        resp = responses[min(index, len(responses) - 1)]
    return resp.get("exit_code", resp.get("exit", 0)), resp.get("stdout", ""), resp.get("stderr", "")
