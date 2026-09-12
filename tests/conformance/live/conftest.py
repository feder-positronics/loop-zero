"""CLI options for the explicitly selected live conformance suite."""


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--diagnose",
        action="store_true",
        dest="live_diagnose",
        help=(
            "seal the selected host credential and print sanitized runtime "
            "readiness without launching a paid scenario"
        ),
    )
