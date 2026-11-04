"""Generate checkout-only Runtime models with the same arguments as the image build."""
import sys
import tempfile
from pathlib import Path


def generate_models(schema):
    """Keep the returned temporary folder alive for the caller's check; images already have models."""
    if (Path(__file__).resolve().parent / "schemas/generated_models.py").is_file():
        return None
    from datamodel_code_generator.__main__ import Exit, main
    folder = tempfile.TemporaryDirectory(prefix="runtime-models-")
    package = Path(folder.name) / "schemas"
    package.mkdir()
    (package / "__init__.py").touch()
    result = main(["--input", str(schema), "--output", str(package / "generated_models.py"),
                   "--output-model-type", "pydantic_v2.BaseModel", "--field-constraints",
                   "--use-standard-collections", "--ignore-pyproject"])
    if result != Exit.OK:
        folder.cleanup()
        raise RuntimeError(f"Generating Runtime schema models failed: {result!r}")
    sys.path.insert(0, folder.name)
    return folder
