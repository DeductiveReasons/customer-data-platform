"""Writes a local HTML gallery of the Eric Health dummy asset library."""
import html
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from cdp import assets  # noqa: E402


def main():
    cards = []
    for asset_id, asset in assets.ASSETS.items():
        for channel in ("email", "direct_mail"):
            content = assets.render(asset_id, channel, "Alex", "preview")
            cards.append(
                f"<section><h2>{html.escape(asset['name'])} ({channel})</h2>"
                f"<p>Stage: {html.escape(asset['stage'])}. Subject: {html.escape(asset['subject'])}</p>"
                f'<iframe title="{html.escape(asset_id)} {channel}" srcdoc="{html.escape(content, quote=True)}" '
                'style="width:100%;max-width:620px;height:420px;border:1px solid #ccc"></iframe></section>'
            )
    page = (
        "<!doctype html><html><head><meta charset='utf-8'><title>Eric Health asset library</title></head>"
        f"<body><h1>Eric Health asset library</h1><p>{html.escape(assets.BRAND['disclaimer'])}</p>"
        + "".join(cards)
        + "</body></html>"
    )
    path = os.path.join(HERE, "..", "asset-preview.html")
    with open(path, "w", encoding="utf-8") as output:
        output.write(page)
    print(os.path.abspath(path))


if __name__ == "__main__":
    main()
