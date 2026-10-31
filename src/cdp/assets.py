"""Eric Health dummy asset library: renders a campaign asset as HTML for a channel."""
import html
import json
import os
from urllib.parse import urlencode

with open(os.path.join(os.path.dirname(__file__), "asset_library.json")) as library_file:
    LIBRARY = json.load(library_file)

BRAND = LIBRARY["brand"]
ASSETS = LIBRARY["assets"]
ONBOARDING = "onboarding_welcome"
SIGNUP_ASSETS = [asset_id for asset_id in ASSETS if asset_id != ONBOARDING]


def _hero(asset):
    # Inline SVG keeps the library self-contained, with no image hosting.
    title = html.escape(asset["headline"])
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 160" role="img" aria-label="{title}">'
        f'<rect width="600" height="160" fill="{asset["accent"]}"/>'
        '<circle cx="520" cy="80" r="46" fill="#ffffff" opacity="0.2"/>'
        '<text x="32" y="92" font-family="Arial, sans-serif" font-size="30" font-weight="700" fill="#ffffff">'
        f'{html.escape(BRAND["name"])}</text></svg>'
    )


def cta_url(asset_id, message_id):
    query = urlencode({"asset": asset_id, "m": message_id})
    return f"https://{BRAND['domain']}/join?{query}"


def render(asset_id, channel, first_name, message_id):
    asset = ASSETS[asset_id]
    name = html.escape(str(first_name or "there"))
    url = html.escape(cta_url(asset_id, message_id), quote=True)
    if channel == "email":
        action = f'<p><a href="{url}">{html.escape(asset["cta"])}</a></p>'
        opt_out = f'<a href="https://{BRAND["domain"]}/unsubscribe">Unsubscribe</a>'
    else:
        action = f"<p><strong>{html.escape(asset['cta'])}:</strong> scan the code or visit {url}</p>"
        opt_out = f"To stop receiving mail, visit {BRAND['domain']}/mail-preferences."
    return (
        f"<html><body>{_hero(asset)}"
        f"<h1>{html.escape(asset['headline'])}</h1>"
        f"<p>Hi {name},</p><p>{html.escape(asset['body'])}</p>{action}"
        f"<p><small>{opt_out} {html.escape(BRAND['disclaimer'])}</small></p>"
        "</body></html>"
    )
