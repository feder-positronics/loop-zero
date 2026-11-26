"""Request recording and response matching shared by the two fake gh runners."""

import json
import os


def replay(
    argv: list[str], calls: str, scenario: str, cwd: str, token: str | None,
) -> tuple[int, str, str]:
    record = dict(argv=argv, token=token, config=[os.getenv(k) for k in ("GH_CONFIG_DIR", "XDG_CONFIG_HOME")])
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

    def response_key(args):
        basic = " ".join(args[:2])
        if basic == "api graphql" and any("viewer { login }" in arg for arg in args):
            basic += " viewer"
        method = args[args.index("--method") + 1] if "--method" in args else ""
        page = next((arg for arg in args if arg.startswith("page=") and arg != "page=1"), "")
        return next((key for key in (f"{basic} {page}", f"{basic} --method {method}") if key in plan), basic)

    key = response_key(argv)
    responses = plan.get(key)
    if responses is None:
        # Legacy scenarios exercise the list fallback for an absent remote branch.
        if argv[0] == "api" and "/branches/" in key:
            return 1, "", "gh: Branch not found (HTTP 404)\n"
        if argv[0] == "api" and "/commits/" in key and "/pulls?" in key:
            sha = key.split("/commits/", 1)[1].split("/", 1)[0]
            return 1, "", f"gh: No commit found for SHA: {sha} (HTTP 422)\n"
        return 97, "", f"fake gh: no response for {key}\n"
    index = sum(1 for call in log[:-1] if response_key(call["argv"]) == key)
    resp = responses[min(index, len(responses) - 1)]
    return resp.get("exit_code", resp.get("exit", 0)), resp.get("stdout", ""), resp.get("stderr", "")
