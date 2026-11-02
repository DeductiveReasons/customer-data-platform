"""Saves a read-only copy of the web app, with data from a deployed instance, for GitHub Pages.

Usage: CDP_WEBAPP_PASSWORD=... python scripts/export_demo.py <webapp_url> [output_dir]

The copy loads JSON files instead of calling the API. Hosts, account details and AWS links are
removed, and every person in it is simulated.
"""
import concurrent.futures
import json
import os
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VIEWS = ("overview", "performance", "model", "bandit", "insights", "journeys", "content", "usage", "monitoring", "daily", "assets")
ACCOUNT_RE = re.compile(r"\b\d{12}\b")
AWS_URL_RE = re.compile(r"https://[A-Za-z0-9.-]+\.(amazonaws\.com|aws\.amazon\.com)[^\s\"'<>]*")


class Exporter:
    def __init__(self, url, password):
        self.url = url.rstrip("/")
        self.host = self.url.split("://", 1)[1].split("/", 1)[0]
        self.password = password

    def _get(self, path):
        request = urllib.request.Request(self.url + path, headers={"Authorization": f"Bearer {self.password}"})
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()

    def api(self, path):
        return self.clean(json.loads(self._get(path)))

    def clean(self, value):
        """Replace this deployment's own address, AWS links and account numbers."""
        if isinstance(value, dict):
            return {k: self.clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        if not isinstance(value, str):
            return value
        value = value.replace(f"https://{self.host}/images/", "images/").replace(f"https://{self.host}", "https://<webapp>")
        value = value.replace('src="/images/', 'src="images/')
        value = AWS_URL_RE.sub("#", value)
        return ACCOUNT_RE.sub("<account>", value)


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")


def _shrink(images):
    """Full-size images are about 50 MB, which is too much for a Git repository."""
    try:
        from PIL import Image
    except ImportError:
        print("Pillow is not installed, so images were kept at full size")
        return
    for path in images.glob("*.jpg"):
        image = Image.open(path).convert("RGB")
        image.thumbnail((768, 768))
        image.save(path, "JPEG", quality=80, optimize=True, progressive=True)


def _page(snapshot_day):
    page = (ROOT / "src" / "cdp" / "webapp.html").read_text(encoding="utf-8")
    marker = "<script>\nconst VIEWS"
    if marker not in page:
        raise SystemExit("webapp.html has changed: update the marker in export_demo.py")
    stub = (ROOT / "scripts" / "demo_stub.js").read_text(encoding="utf-8")
    page = page.replace(marker, f"<script>const DEMO_DATE='{snapshot_day}';\n{stub}</script>\n{marker}", 1)
    # Pages serve from a sub-path, so image links must be relative.
    return page.replace('src="/images/', 'src="images/')


def main(url, output):
    password = os.environ.get("CDP_WEBAPP_PASSWORD")
    if not password:
        raise SystemExit("set CDP_WEBAPP_PASSWORD")
    exporter = Exporter(url, password)
    data = output / "data"

    for view in VIEWS:
        _write(data / f"{view}.json", exporter.api(f"/api/{view}"))
        print(f"saved {view}", flush=True)

    first = exporter.api("/api/variations?page=1")
    pages = -(-first["matching"] // first["page_size"])
    variations = list(first["variations"])
    for number in range(2, pages + 1):
        variations.extend(exporter.api(f"/api/variations?page={number}")["variations"])
    _write(data / "variations.json", {**first, "variations": variations, "page": 1})
    print(f"saved {len(variations)} variations", flush=True)

    people = exporter.api("/api/people")
    _write(data / "people.json", people)
    ids = [row["customer_id"] for row in people["people"]]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        profiles = dict(zip(ids, pool.map(lambda i: exporter.api(f"/api/people/{i}"), ids)))
    _write(data / "profiles.json", profiles)
    print(f"saved {len(profiles)} profiles", flush=True)

    images = output / "images"
    images.mkdir(parents=True, exist_ok=True)
    image_ids = [image["id"] for image in json.loads((data / "content.json").read_text())["images"]]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for image_id, body in zip(image_ids, pool.map(lambda i: exporter._get(f"/images/{i}.jpg"), image_ids)):
            (images / f"{image_id}.jpg").write_bytes(body)
    print(f"saved {len(image_ids)} images", flush=True)
    _shrink(images)

    (output / "index.html").write_text(_page(date.today().isoformat()), encoding="utf-8")
    print(f"wrote {output / 'index.html'}")


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        raise SystemExit(__doc__)
    main(sys.argv[1], Path(sys.argv[2]) if len(sys.argv) == 3 else ROOT / "docs" / "demo")
