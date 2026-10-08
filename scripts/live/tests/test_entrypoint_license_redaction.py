"""The Striim entrypoint must never print the license into `docker logs`.

The primary and node branches used to `cat` the rendered conf/startUp.properties after the
four license values had been substituted into it, so `docker logs slt-striim` carried
ProductKey and LicenceKey in clear (found on a Mac run). The entrypoint runs inside
the image, so these tests run its printing helper with bash against a temp file, and check
the shipped source for any other path that prints the file or the values.
"""
import re
import subprocess
from pathlib import Path

_ENTRYPOINT = (Path(__file__).resolve().parents[1]
               / "services" / "striim" / "images" / "striim" / "files" / "entrypoint.sh")

_PROPS = """\
WAClusterName=cluster-x
CompanyName=company-x
# ProductKey=
ProductKey=PK-SECRET-1234
LicenceKey=LK-SECRET-5678
  LicenceKey = LK-SPACED-9999
MEM_MAX=4096m
"""


def _code_lines() -> list:
    return [ln for ln in _ENTRYPOINT.read_text().splitlines()
            if not ln.lstrip().startswith("#")]


def _show_props_function() -> str:
    text = _ENTRYPOINT.read_text()
    m = re.search(r"^show_props\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert m, "entrypoint.sh has no show_props() helper"
    return m.group(0)


def test_show_props_prints_the_file_with_the_license_values_redacted(tmp_path):
    props = tmp_path / "startUp.properties"
    props.write_text(_PROPS)
    out = subprocess.run(["bash", "-c", _show_props_function() + 'show_props "$1"', "_",
                          str(props)], capture_output=True, text=True, check=True).stdout
    for secret in ("PK-SECRET-1234", "LK-SECRET-5678", "LK-SPACED-9999"):
        assert secret not in out
    assert "ProductKey=<redacted>" in out
    assert "LicenceKey=<redacted>" in out
    # Everything else is still shown: the print exists to debug the rendered config.
    assert "WAClusterName=cluster-x" in out
    assert "MEM_MAX=4096m" in out


def test_no_code_path_cats_the_rendered_startup_properties():
    offenders = [ln for ln in _code_lines()
                 if re.search(r"\bcat\b[^|]*startUp\.properties", ln)]
    assert offenders == [], f"prints the license in clear: {offenders}"


def test_the_license_values_are_only_ever_substituted_never_echoed():
    for ln in _code_lines():
        if "${PRODUCT_KEY}" in ln or "${LICENCE_KEY}" in ln:
            assert ln.lstrip().startswith("sed -i"), f"license value used outside sed -i: {ln}"


# The server itself logs the license at boot ("ProductKey: …", "License Key: …", seen in
# `docker logs slt-striim` on a live run), and the entrypoint's last command tails that
# log to stdout. So the tail is filtered too.

_SERVER_LOG = """\
2026-09-25T02:20:09.338339628Z[GMT] Registered to: company-x
2026-09-25T02:20:09.338385587Z[GMT] ProductKey: PK-SECRET-1234
2026-09-25T02:20:09.348857753Z[GMT] License Key: LK-SECRET-5678
2026-09-25T02:20:09.434058712Z[GMT] License expires in 97 days
"""


def _redact_stream_function() -> str:
    m = re.search(r"^redact_stream\(\) \{\n.*?^\}\n", _ENTRYPOINT.read_text(), re.S | re.M)
    assert m, "entrypoint.sh has no redact_stream() helper"
    return m.group(0)


def test_redact_stream_blanks_the_servers_own_license_lines():
    out = subprocess.run(["bash", "-c", _redact_stream_function() + "redact_stream"],
                         input=_SERVER_LOG, capture_output=True, text=True, check=True).stdout
    assert "PK-SECRET-1234" not in out and "LK-SECRET-5678" not in out
    assert "ProductKey: <redacted>" in out and "License Key: <redacted>" in out
    assert "License expires in 97 days" in out, "only the key lines are touched"


def test_every_server_log_tail_goes_through_redact_stream():
    # The tails run in the background under follow() so SIGTERM reaches its trap.
    # They write to the script's stdout, which the redirect below sends through redact_stream
    # (test_the_whole_script_output_goes_through_redact_stream); nothing redirects them past it.
    tails = [ln.strip() for ln in _code_lines() if ln.strip().startswith("follow ")]
    assert len(tails) == 3, tails          # the primary, the node, the agent
    assert sum("striim-node.log" in ln for ln in tails) == 2
    assert [ln.strip() for ln in _code_lines() if "tail -f" in ln] == ['tail -f "$1" &']


# Review R3: redacting at each call site left stderr of the nohup'd starts, the keystore tools
# and the agent's tail unfiltered. The whole script's stdout and stderr now go through
# redact_stream, which also replaces the literal values from the container's environment.

def _stream(text, **env):
    import os
    return subprocess.run(["bash", "-c", _redact_stream_function() + "redact_stream"],
                          input=text, capture_output=True, text=True, check=True,
                          env={**os.environ, **env}).stdout


def test_redact_stream_replaces_the_raw_values_wherever_they_appear():
    out = _stream("license PK-RAW-1234 was rejected (LK-RAW-5678)\nplain line\n",
                  PRODUCT_KEY="PK-RAW-1234", LICENCE_KEY="LK-RAW-5678")
    assert "PK-RAW-1234" not in out and "LK-RAW-5678" not in out
    assert "plain line" in out


def test_redact_stream_values_with_regex_characters_are_literal():
    out = _stream("key a.b*c[d]$ here\nabxbc untouched\n", PRODUCT_KEY="a.b*c[d]$")
    assert "a.b*c[d]$" not in out and "abxbc untouched" in out


def test_redact_stream_catches_every_key_spelling():
    out = _stream("PRODUCT_KEY=aa11\nLICENCE_KEY=bb22\nLicenseKey: cc33\nproduct key = dd44\n",
                  PRODUCT_KEY="", LICENCE_KEY="")
    for v in ("aa11", "bb22", "cc33", "dd44"):
        assert v not in out


def test_the_whole_script_output_goes_through_redact_stream():
    lines = _code_lines()
    redirect = [i for i, ln in enumerate(lines) if ln.strip() == "exec > >(redact_stream) 2>&1"]
    assert redirect, "stdout and stderr are not redirected through redact_stream"
    first_role = next(i for i, ln in enumerate(lines) if 'if [ "${ROLE}" == "primary" ]' in ln)
    defined = next(i for i, ln in enumerate(lines) if ln.startswith("redact_stream() {"))
    assert defined < redirect[0] < first_role


def test_redact_stream_terminates_when_a_value_is_inside_the_placeholder():
    out = subprocess.run(["bash", "-c", _redact_stream_function() + "redact_stream"],
                         input="x edac y\n", capture_output=True, text=True, timeout=5,
                         env={"PRODUCT_KEY": "edac", "PATH": "/usr/bin:/bin"}).stdout
    assert "x <redacted> y" in out


# Linux and Mac live runs: with mawk, redact_stream held its input until its read
# buffer filled, so a running container's `docker logs` lost everything after the first few
# kilobytes (the agent showed 1 line). Every test above closes stdin, which flushed it anyway.
# A line must come out while the writer is still open.

def test_redact_stream_passes_each_line_through_while_the_writer_stays_open():
    import os
    import select
    p = subprocess.Popen(["bash", "-c", _redact_stream_function() + "redact_stream"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         env={**os.environ, "PRODUCT_KEY": "PK-LIVE-1234"})
    try:
        p.stdin.write(b"Starting agent PK-LIVE-1234\n")
        p.stdin.flush()
        ready, _, _ = select.select([p.stdout], [], [], 3)
        assert ready, "no output while the writer is open: the filter is buffering its input"
        line = p.stdout.readline().decode()
        assert line == "Starting agent <redacted>\n"
    finally:
        p.kill()
        p.wait()


def test_the_entrypoint_parses():
    # The function-extracting tests above pass on a file bash cannot run.
    subprocess.run(["bash", "-n", str(_ENTRYPOINT)], check=True)
