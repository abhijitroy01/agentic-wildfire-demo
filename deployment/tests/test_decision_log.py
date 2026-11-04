"""Every decision ID cited in a tracked file is defined in architecture/decisions.md (W3.4, DEPL-5, DOCS-12)."""
import re
from pathlib import PurePosixPath

import pytest

from deployment import ROOT

LOG = "architecture/decisions.md"
# A decision ID is D or G, digits and an optional lower-case letter: D24, G17, G00d. A tracked file cites one
# wherever it stands as a token of its own: "(D24)", "(G17)", "Accepted (D38, G45)", "decisions D4, D7" or a
# "D9: " reason prefix. The pattern leaves out look-alikes that cite nothing:
#   - part of a longer word or of a hyphenated token: the GUID 6003FACD-D976-4668 in a public SOP snapshot, or
#     review-finding labels such as D-13 (UIWS-13);
#   - a quoted string of its own: the firetruck unit names "D1", "D2" and "D3";
#   - Caltrans district 3, written "D3 travel-time" (traffic-simulator/scripts).
CITATION = re.compile(r"""
    (?<![\w-])                                      # not the end of a longer word or of a hyphenated token
    (?!(?<=")[DG]\d+[a-z]?"|(?<=')[DG]\d+[a-z]?')    # not a quoted string of its own
    ([DG]\d+[a-z]?)
    (?![\w-])                                       # not the start of a longer word or of a hyphenated token
    (?!\ travel-time)                               # not Caltrans district 3
""", re.VERBOSE)
# A complete row of the log's tables defines its ID: | ID | decision | reason | YYYY-MM-DD | status |, where the
# status starts with "Current" or "Superseded by <ID>".
DEFINITION = re.compile(r"^\| ([DG]\d+[a-z]?) \|[^|\n]+\|[^|\n]+\| \d{4}-\d{2}-\d{2} "
                        r"\| (?:Current|Superseded by [DG]\d+[a-z]?)[^|\n]* \|$", re.MULTILINE)
# npm writes these, and their base64 integrity hashes can hold D or G tokens; nobody cites a decision there.
GENERATED = {"package-lock.json"}


def citations(tracked_files):
    """Find every decision-ID citation in the tracked text files, as (path, line number, ID)."""
    found = []
    for name in tracked_files:
        path = ROOT / name
        if PurePosixPath(name).name in GENERATED or not path.is_file():  # or deleted, not yet staged
            continue
        with path.open("rb") as handle:
            head = handle.read(8000)
            if b"\0" in head:  # binary, by git's own test
                continue
            text = (head + handle.read()).decode("utf-8", "replace")
        found += [(name, number, identifier) for number, line in enumerate(text.splitlines(), 1)
                  for identifier in CITATION.findall(line)]
    return found


@pytest.mark.parametrize("text,cited", [
    ("Accepted (D38, G45): access logs would need a log bucket", ["D38", "G45"]),
    ("that hop stays inside the VPC (decisions D4, D7).", ["D4", "D7"]),
    ('comment: "D9: geocode Gateway target"', ["D9"]),
    ('"""G00d: no function URL"""', ["G00d"]),
    ("D23's settings table", ["D23"]),
    ('"D1", "D2", "D3",', []),
    ("searchListing-6003FACD-D976-4668", []),
    ("Demo framing (D-13)", []),
    ("found all 14 D3 travel-time observations", []),
    ("AD24, 0xD3, #D3D3D3", []),
])
def test_the_citation_pattern_finds_decision_ids_and_skips_look_alikes(text, cited):
    assert CITATION.findall(text) == cited


def test_every_decision_id_cited_in_a_tracked_file_is_defined_in_the_decision_log(tracked_files):
    defined = set(DEFINITION.findall((ROOT / LOG).read_text()))
    found = citations(tracked_files)
    assert defined and found, "the log defines no ID, or the scan found no citation"
    undefined = [f"{name}:{number}: {identifier}" for name, number, identifier in found if identifier not in defined]
    assert not undefined, (f"Define each of these decision IDs in {LOG} with a complete row (ID, decision, reason, "
                           "date, status), or state the decision in plain words:\n" + "\n".join(undefined))
