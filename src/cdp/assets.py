"""Eric Health dummy asset library: renders campaign messages as email or direct mail HTML."""
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
INK = "#17212f"
MUTED = "#5d6b7e"
PAGE = "#f1f3f6"
FONT = "Helvetica,Arial,sans-serif"


def _hero(asset):
    # Inline SVG keeps a preview self-contained when no library image is supplied.
    title = html.escape(asset["headline"])
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 160" role="img" aria-label="{title}" '
        'style="display:block;width:100%">'
        f'<rect width="600" height="160" fill="{asset["accent"]}"/>'
        '<circle cx="520" cy="80" r="46" fill="#ffffff" opacity="0.2"/>'
        '<text x="32" y="92" font-family="Arial, sans-serif" font-size="30" font-weight="700" fill="#ffffff">'
        f'{html.escape(BRAND["name"])}</text></svg>'
    )


def cta_url(asset_id, message_id):
    query = urlencode({"asset": asset_id, "m": message_id})
    path = "start" if asset_id == ONBOARDING else "register"
    return f"https://{BRAND['domain']}/{path}?{query}"


def _hero_html(asset, image_url, image_alt):
    if not image_url:
        return _hero(asset)
    return (
        f'<img src="{html.escape(image_url, quote=True)}" alt="{html.escape(image_alt, quote=True)}" width="600" '
        'style="display:block;width:100%;max-width:600px;height:auto;border:0">'
    )


def _email(asset, name, url, hero):
    accent = asset["accent"]
    brand = html.escape(BRAND["name"])
    preheader = html.escape(asset["body"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(asset['subject'])}</title></head>
<body style="margin:0;padding:0;background:{PAGE};font-family:{FONT};color:{INK}">
<div style="display:none;max-height:0;overflow:hidden">{preheader}</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:{PAGE}"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:600px;background:#ffffff;border-radius:10px;overflow:hidden">
<tr><td style="padding:20px 32px;font-size:20px;font-weight:700;letter-spacing:-0.2px"><span style="color:{accent}">&#9679;</span> {brand}</td></tr>
<tr><td>{hero}</td></tr>
<tr><td style="padding:32px 32px 8px">
<p style="margin:0 0 8px;font-size:13px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:{accent}">{html.escape(BRAND['program'])}</p>
<h1 style="margin:0 0 16px;font-size:28px;line-height:1.2;font-weight:700">{html.escape(asset['headline'])}</h1>
<p style="margin:0 0 12px;font-size:16px;line-height:1.55">Hi {name},</p>
<p style="margin:0 0 24px;font-size:16px;line-height:1.55">{html.escape(asset['body'])}</p>
<table role="presentation" cellspacing="0" cellpadding="0"><tr><td style="border-radius:7px;background:{accent}"><a href="{url}" style="display:inline-block;padding:14px 26px;font-size:16px;font-weight:700;color:#ffffff;text-decoration:none">{html.escape(asset['cta'])}</a></td></tr></table>
</td></tr>
<tr><td style="padding:24px 32px 32px"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:{PAGE};border-radius:8px"><tr><td style="padding:16px 18px;font-size:14px;line-height:1.5;color:{INK}"><strong>{html.escape(asset['highlight_title'])}</strong><br>{html.escape(asset['highlight'])}</td></tr></table></td></tr>
</table>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:600px"><tr><td style="padding:20px 32px;font-size:12px;line-height:1.55;color:{MUTED};text-align:center">
{brand} &middot; {html.escape(BRAND['address'])}<br>
You're receiving this because you opted in to {brand} emails.<br>
<a href="https://{BRAND['domain']}/unsubscribe" style="color:{MUTED}">Unsubscribe</a> &middot; <a href="https://{BRAND['domain']}/preferences" style="color:{MUTED}">Email preferences</a> &middot; <a href="https://{BRAND['domain']}/privacy" style="color:{MUTED}">Privacy</a><br><br>
{html.escape(BRAND['disclaimer'])}
</td></tr></table>
</td></tr></table></body></html>"""


def _direct_mail(asset, name, url, hero):
    accent = asset["accent"]
    brand = html.escape(BRAND["name"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(asset['subject'])}</title></head>
<body style="margin:0;padding:18px 10px;background:{PAGE};font-family:{FONT};color:{INK}">
<div style="max-width:600px;margin:0 auto;background:#ffffff;border:1px solid #d8dde4">
<div style="padding:18px 28px;font-size:20px;font-weight:700"><span style="color:{accent}">&#9679;</span> {brand}</div>
{hero}
<div style="padding:28px">
<h1 style="margin:0 0 14px;font-size:28px;line-height:1.2">{html.escape(asset['headline'])}</h1>
<p style="margin:0 0 10px;font-size:16px;line-height:1.55">Dear {name},</p>
<p style="margin:0 0 20px;font-size:16px;line-height:1.55">{html.escape(asset['body'])}</p>
<div style="border:2px solid {accent};border-radius:8px;padding:16px 18px;font-size:16px;line-height:1.5"><strong style="color:{accent}">{html.escape(asset['cta'])}</strong><br>Visit <strong style="word-break:break-all">{url}</strong></div>
<p style="margin:20px 0 0;font-size:14px;line-height:1.5"><strong>{html.escape(asset['highlight_title'])}</strong><br>{html.escape(asset['highlight'])}</p>
</div>
<div style="padding:16px 28px;background:{PAGE};font-size:12px;line-height:1.55;color:{MUTED}">{brand} &middot; {html.escape(BRAND['address'])}<br>To stop receiving mail, visit {html.escape(BRAND['domain'])}/mail-preferences.<br>{html.escape(BRAND['disclaimer'])}</div>
</div></body></html>"""


def render(asset_id, channel, first_name, message_id, image_url=None, image_alt=""):
    asset = ASSETS[asset_id]
    name = html.escape(str(first_name or "there"))
    url = html.escape(cta_url(asset_id, message_id), quote=True)
    hero = _hero_html(asset, image_url, image_alt)
    builder = _email if channel == "email" else _direct_mail
    return builder(asset, name, url, hero)
