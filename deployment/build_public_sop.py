"""Rebuild the reviewed public guidance collection from pinned official snapshots."""
import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path

# Repository-relative collection folder; deployers resolve it under their root. This stdlib script also runs
# as a plain file, so its default stays relative to this checkout.
COLLECTION_PATH = "backend/knowledge/public-sop"
COLLECTION = Path(__file__).resolve().parents[1] / COLLECTION_PATH
COLLECTION_ID = "wildfire-public-guidance-v1"
OFFICIAL_HOSTS = {"www.ready.gov", "www.fs.usda.gov", "www.nwcg.gov", "www.nps.gov"}


class Text(HTMLParser):
    """Keep text and ordered-list numbers; omit script/style and decorative images."""
    def __init__(self):
        super().__init__()
        self.parts, self.lists, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        """Track skipped elements and emit line breaks and list markers for opening HTML tags."""
        if tag in {"script", "style", "figure"}:
            self.skip += 1
        if self.skip:
            return
        if tag == "ol":
            self.lists.append(int(dict(attrs).get("start", "1"))-1)
        if tag in {"p", "h1", "h2", "h3", "h4", "br", "li", "div"}:
            self.parts.append("\n")
        if tag == "li":
            if self.lists:
                self.lists[-1] += 1
                self.parts.append(f"{self.lists[-1]}. ")
            else:
                self.parts.append("- ")

    def handle_endtag(self, tag):
        """Close skipped elements, end ordered-list numbering and add line breaks when an HTML tag ends."""
        if tag in {"script", "style", "figure"}:
            self.skip -= 1
        if self.skip:
            return
        if tag == "ol":
            self.lists.pop()
        if tag in {"p", "h1", "h2", "h3", "h4", "li", "div", "ol", "ul"}:
            self.parts.append("\n")

    def handle_data(self, data):
        """Collect visible page text with whitespace collapsed, outside skipped elements."""
        if not self.skip:
            self.parts.append(re.sub(r"\s+", " ", data))

    def text(self):
        return "\n".join(line.strip() for line in "".join(self.parts).splitlines() if line.strip())


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def build(root=COLLECTION):
    """Build the public SOP collection's text artifacts from the reviewed HTML snapshots."""
    from urllib.parse import urlsplit
    manifest = json.loads((root / "sources.json").read_text())
    outputs = {}
    for source in manifest["sources"]:
        url = urlsplit(source["url"])
        if url.scheme != "https" or url.hostname not in OFFICIAL_HOSTS:
            raise ValueError("Only explicitly reviewed official HTTPS sources may be ingested")
        snapshot = (root / "snapshots" / source["snapshot"]).read_bytes()
        if sha256(snapshot) != source["sha256"]:
            raise ValueError(f"Source snapshot checksum mismatch: {source['id']}")
        html = snapshot.decode()
        for section in source["sections"]:
            if html.count(section["start"]) != 1:
                raise ValueError(f"Section start is not unique: {section['id']}")
            selected = html.split(section["start"], 1)[1]
            if section["end"] not in selected:
                raise ValueError(f"Section end is missing: {section['id']}")
            selected = selected.split(section["end"], 1)[0]
            parser = Text()
            parser.feed(selected)
            text = parser.text()
            if len(text) < 80 or len(text.encode()) > 16000:
                raise ValueError(f"Invalid or oversized section: {section['id']}")
            title = section["title"]
            content = f"# {title}\n\n{text}\n"
            metadata = {
                "collection": COLLECTION_ID, "classification": "public",
                "title": title, "publisher": source["publisher"], "source_url": source["url"],
                "retrieved_at": source["retrieved_at"], "source_id": source["id"],
                **({"source_date": source["source_date"]} if source.get("source_date") else {}),
            }
            if len(json.dumps(metadata).encode()) > 1000:
                raise ValueError("S3 Vectors custom metadata budget exceeded")
            name = section["id"]+".txt"
            outputs["documents/"+name] = content.encode()
            outputs["documents/"+name+".metadata.json"] = json.dumps(
                {"metadataAttributes": metadata}, indent=2, ensure_ascii=False).encode()
    return outputs


def validate(root=COLLECTION):
    """Rebuild the public SOP collection and fail unless its digests match the reviewed manifest."""
    outputs = build(root)
    expected = json.loads((root / "collection.json").read_text())
    actual = {name: sha256(data) for name, data in sorted(outputs.items())}
    if expected["artifacts"] != actual:
        raise ValueError("Collection differs from the reviewed artifact manifest; rebuild and review before deployment")
    for name, data in outputs.items():
        if (root / name).read_bytes() != data:
            raise ValueError(f"Collection artifact drift: {name}")
    actual_files = {p.relative_to(root).as_posix() for p in (root / "documents").rglob("*") if p.is_file()}
    if actual_files != set(outputs):
        raise ValueError("Unreviewed or missing collection files")
    return outputs


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    outputs = validate() if args.check else build()
    if not args.check:
        for name, data in outputs.items():
            path = COLLECTION / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (COLLECTION / "collection.json").write_text(json.dumps({
            "collection": COLLECTION_ID,
            "artifacts": {name: sha256(data) for name, data in sorted(outputs.items())},
        }, indent=2)+"\n")
    print(f"Verified {len(outputs)//2} reviewed public guidance sections")
