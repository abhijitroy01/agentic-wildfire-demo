"""The radio .env.example lists exactly the settings the radio code reads (BCFY-12)."""
import ast
import re
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
# A KEY=value line, or a commented "# KEY=value" line for an optional setting, documents one setting.
LISTED = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=", re.MULTILINE)
# Calls that read one environment variable named by their first argument.
READERS = re.compile(r"os\.getenv|os\.environ\.get|Config\.get_[a-z_]+")


def listed(example: Path) -> set[str]:
    """Return the setting names an .env.example documents, whether set or commented out."""
    return set(LISTED.findall(example.read_text(encoding="utf-8")))


def read_by(folder: Path) -> set[str]:
    """Return the environment variable names a component's code reads by literal name, tests excluded."""
    names = set()
    for path in folder.rglob("*.py"):
        if path.name.startswith("test_"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and READERS.fullmatch(ast.unparse(node.func)) and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                names.add(node.args[0].value)
            elif (isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ"
                    and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
                names.add(node.slice.value)
    return names


def test_radio_env_example_lists_exactly_the_settings_the_radio_code_reads():
    documented = listed(REPOSITORY / "broadcastify/.env.example")
    read = read_by(REPOSITORY / "broadcastify")
    assert {"MONITOR_BEDROCK_MODEL_ID", "API_KEY", "S3_BUCKET"} <= read, "the scan finds radio settings"
    assert sorted(documented - read) == [], "listed in broadcastify/.env.example, but no radio code reads it"
    assert sorted(read - documented) == [], "read by the radio code, but missing from broadcastify/.env.example"
