"""Read worker operational-setting names from Python AST without importing its AWS configuration."""
import ast
import json
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[3] / "traffic-simulator" / "traffic_simulator"
READERS = {"limit", "names", "declared_setting"}


def literal_name(call: ast.Call) -> str:
    """A computed setting name needs an explicit scanner update, rather than silently escaping the gate."""
    if not call.args or not isinstance(call.args[0], ast.Constant) or not isinstance(call.args[0].value, str):
        raise ValueError(f"Operational setting name must be a string literal: {ast.unparse(call)}")
    return call.args[0].value


def inventory() -> dict[str, list[str]]:
    """Include direct worker reads, imported accessors and both startup-validation lists."""
    tree = ast.parse((PACKAGE / "settings.py").read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}

    def accessor_names(name: str, visiting: frozenset[str] = frozenset()) -> set[str]:
        if name not in functions:
            raise ValueError(f"Unknown settings accessor: {name}")
        if name in visiting:
            raise ValueError(f"Settings accessor cycle: {name}")
        result = set()
        for node in ast.walk(functions[name]):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id in READERS:
                result.add(literal_name(node))
            elif node.func.id in functions:
                result.update(accessor_names(node.func.id, visiting | {name}))
        return result

    startup = set()
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id in {"WORKER_SETTINGS", "STARTUP_LIMITS"}
                        for target in node.targets)):
            if not isinstance(node.value, ast.Tuple | ast.List):
                raise ValueError("Startup settings must remain a statically inspectable accessor list")
            for accessor in node.value.elts:
                if not isinstance(accessor, ast.Name):
                    raise TypeError("Startup settings must contain named accessors")
                startup.update(accessor_names(accessor.id))

    required = set(startup)
    # This package contains the worker engine; the application API lives under backend/.
    for path in PACKAGE.glob("*.py"):
        if path.name == "settings.py":
            continue
        module = ast.parse(path.read_text(encoding="utf-8"))
        modules, imported = set(), {}
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                modules.update(alias.asname or alias.name for alias in node.names
                               if alias.name == "traffic_simulator.settings")
            elif isinstance(node, ast.ImportFrom) and node.module == "traffic_simulator":
                modules.update(alias.asname or alias.name for alias in node.names if alias.name == "settings")
            elif isinstance(node, ast.ImportFrom) and node.module == "traffic_simulator.settings":
                imported.update((alias.asname or alias.name, alias.name) for alias in node.names)
        for node in ast.walk(module):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute) and ast.unparse(node.func.value) in modules:
                name = node.func.attr
            elif isinstance(node.func, ast.Name) and node.func.id in imported:
                name = imported[node.func.id]
            else:
                continue
            required.update({literal_name(node)} if name in READERS else accessor_names(name))
    return {"required": sorted(required), "startup": sorted(startup)}


if __name__ == "__main__":
    print(json.dumps(inventory()))
