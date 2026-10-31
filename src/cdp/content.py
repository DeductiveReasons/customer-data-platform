"""Eric Health image library and the model that picks an image theme for each person."""
import hashlib
import json
import math
import os

MODEL_PATH = os.path.join(os.path.dirname(__file__), "content_model.json")
VARIANTS = 10
EXPLORATION_RATE = 0.1
IMAGE_PREFIX = "content-library/"
# Age is left out on purpose so imagery is chosen from behavior, not from assumptions about age.
FEATURES = [
    "tenure_months",
    "app_sessions_30d",
    "days_since_active",
    "email_open_rate",
    "wellness_member",
    "touches",
    "opens",
    "clicks",
    "is_direct_mail",
]

THEMES = {
    "outdoor_walk": {"label": "Outdoor walks", "tone": "calm", "sky": ("#bfe3ff", "#eaf7ff"), "ground": "#6fb36a", "accent": "#f4b740"},
    "home_workout": {"label": "Home workouts", "tone": "practical", "sky": ("#ffe7cf", "#fff6ea"), "ground": "#c98f5a", "accent": "#e4572e"},
    "strength": {"label": "Strength training", "tone": "energetic", "sky": ("#2e3a59", "#56688f"), "ground": "#28303f", "accent": "#ff7a33"},
    "yoga": {"label": "Yoga and mobility", "tone": "calm", "sky": ("#e7d9ff", "#f8f1ff"), "ground": "#9c86c9", "accent": "#65b891"},
    "group_class": {"label": "Group classes", "tone": "social", "sky": ("#ffd6e4", "#fff0f5"), "ground": "#d76a8d", "accent": "#3d7edb"},
    "coaching": {"label": "Coaching", "tone": "supportive", "sky": ("#cfeee6", "#effaf6"), "ground": "#3d9a85", "accent": "#f28f3b"},
    "cycling": {"label": "Cycling", "tone": "energetic", "sky": ("#c9f0ff", "#f0fbff"), "ground": "#7c8b99", "accent": "#d7263d"},
    "swimming": {"label": "Swimming", "tone": "calm", "sky": ("#b8ecf2", "#e8fbfd"), "ground": "#2b95c3", "accent": "#ffffff"},
    "running": {"label": "Running", "tone": "energetic", "sky": ("#ffd9a8", "#fff1dc"), "ground": "#b36b45", "accent": "#23395b"},
    "daily_routine": {"label": "Daily routines", "tone": "practical", "sky": ("#f3edc8", "#fffbe6"), "ground": "#b3a85f", "accent": "#4d9de0"},
}


def _uniform():
    zeros = [0.0] * len(FEATURES)
    return {
        "version": "untrained",
        "features": FEATURES,
        "means": zeros,
        "stds": [1.0] * len(FEATURES),
        "actions": {theme: {"intercept": 0.0, "weights": list(zeros)} for theme in THEMES},
    }


def uniform_model():
    return _uniform()


if os.path.exists(MODEL_PATH):
    with open(MODEL_PATH) as model_file:
        BUNDLED_MODEL = json.load(model_file)
else:
    BUNDLED_MODEL = _uniform()


def all_images():
    return [
        {"id": f"{theme}-{variant:02d}", "theme": theme, "label": config["label"], "tone": config["tone"]}
        for theme, config in THEMES.items()
        for variant in range(1, VARIANTS + 1)
    ]


def image_theme(image_id):
    theme, _, variant = image_id.rpartition("-")
    if theme in THEMES and variant.isdigit() and 1 <= int(variant) <= VARIANTS and len(variant) == 2:
        return theme
    return None


def image_url(image_id):
    base = os.environ.get("CONTENT_BASE_URL", "").rstrip("/")
    return f"{base}/images/{image_id}.jpg"


def score(model, theme, features):
    weights = model["actions"].get(theme)
    if weights is None:
        return None
    means = weights.get("means", model["means"])
    stds = weights.get("stds", model["stds"])
    total = weights["intercept"]
    for name, weight, mean, std in zip(model["features"], weights["weights"], means, stds):
        value = features.get(name)
        if value is not None:
            total += weight * (value - mean) / std
    return 1 / (1 + math.exp(-max(min(total, 30), -30)))


def choose(customer_id, touch, features, state, model, rng, is_blocked=lambda image_id: False):
    """Pick a theme with the model, then a variant that differs from the last image shown."""
    scored = [(score(model, theme, features), theme) for theme in THEMES]
    scored = [item for item in scored if item[0] is not None]
    if rng.random() < EXPLORATION_RATE or not scored:
        theme = rng.choice(list(THEMES))
        selection = "explore"
    else:
        theme = max(scored)[1]
        selection = "best"
    digest = hashlib.sha256(f"{customer_id}|{touch}".encode()).digest()
    variant = digest[0] % VARIANTS + 1
    image_id = f"{theme}-{variant:02d}"
    if image_id == state.get("last_image"):
        variant = variant % VARIANTS + 1
    for _ in range(VARIANTS):
        image_id = f"{theme}-{variant:02d}"
        if not is_blocked(image_id):
            break
        variant = variant % VARIANTS + 1
    else:
        return None  # Every image in this theme is pending approval for this message.
    probability = score(model, theme, features)
    return {
        "image_id": image_id,
        "image_theme": theme,
        "content_selection": selection,
        "content_score": round(probability, 4) if probability is not None else None,
    }


def _scene(theme, r, accent):
    """Theme-specific shapes. Variation comes from values derived from the image's hash."""
    x = 120 + r[0] % 420
    y = 100 + r[1] % 60
    size = 26 + r[2] % 22
    white = "#ffffff"
    dark = "#22303f"
    if theme == "outdoor_walk":
        trees = "".join(
            f'<rect x="{60 + i * 150 + r[i] % 50}" y="170" width="12" height="50" fill="#7a5a3a"/>'
            f'<circle cx="{66 + i * 150 + r[i] % 50}" cy="{150 + r[i + 3] % 15}" r="{32 + r[i + 5] % 18}" fill="#3f8f55"/>'
            for i in range(5)
        )
        return trees + f'<path d="M0 300 C200 {235 + r[3] % 30},400 {290 - r[4] % 30},768 235 L768 262 C420 {315 - r[4] % 20},220 {285 + r[3] % 20},0 340Z" fill="#f2e4c4"/>'
    if theme == "home_workout":
        return (
            f'<rect x="{70 + r[0] % 50}" y="80" width="150" height="110" rx="6" fill="{white}" stroke="{dark}" stroke-width="6"/>'
            f'<rect x="{300 + r[1] % 120}" y="255" width="290" height="24" rx="12" fill="{accent}"/>'
            f'<rect x="{350 + r[2] % 120}" y="228" width="70" height="14" rx="4" fill="{dark}"/>'
        )
    if theme == "strength":
        return (
            f'<rect x="{x - 130}" y="{y + 70}" width="260" height="14" rx="7" fill="#d9dee7"/>'
            + "".join(
                f'<rect x="{x + offset}" y="{y + 25 - abs(offset) % 9}" width="26" height="104" rx="6" fill="{accent if abs(offset) > 110 else "#d9dee7"}"/>'
                for offset in (-145, -112, 86, 119)
            )
        )
    if theme == "yoga":
        return (
            f'<ellipse cx="{x}" cy="285" rx="190" ry="20" fill="{accent}"/>'
            f'<circle cx="{x}" cy="{y + 40}" r="{size}" fill="{white}" opacity="0.92"/>'
            f'<path d="M{x - 95} 278 C{x - 50} {y + 120},{x + 50} {y + 120},{x + 95} 278Z" fill="{white}" opacity="0.92"/>'
        )
    if theme == "group_class":
        return "".join(
            f'<circle cx="{110 + i * 138}" cy="{165 + (r[i] % 22)}" r="{23 + r[i + 4] % 8}" fill="{accent if i % 2 else white}"/>'
            f'<rect x="{84 + i * 138}" y="{194 + (r[i] % 22)}" width="52" height="86" rx="24" fill="{accent if i % 2 else white}"/>'
            for i in range(5)
        )
    if theme == "coaching":
        return (
            f'<rect x="{x - 110}" y="90" width="230" height="130" rx="18" fill="{white}"/>'
            f'<path d="M{x - 60} 218 l-20 44 l62 -44Z" fill="{white}"/>'
            f'<path d="M{x - 65} {165 - r[3] % 22} L{x - 15} {180 - r[4] % 22} L{x + 30} {130 - r[5] % 12} L{x + 75} {120 - r[6] % 12}" fill="none" stroke="{accent}" stroke-width="10" stroke-linecap="round" stroke-linejoin="round"/>'
        )
    if theme == "cycling":
        spread = 150 + r[3] % 30
        return (
            f'<circle cx="{x - spread // 2}" cy="240" r="58" fill="none" stroke="{dark}" stroke-width="9"/>'
            f'<circle cx="{x + spread // 2}" cy="240" r="58" fill="none" stroke="{dark}" stroke-width="9"/>'
            f'<path d="M{x - spread // 2} 240 L{x - 20} 165 L{x + 55} 165 L{x + spread // 2} 240 M{x - 20} 165 L{x + 8} 240 L{x + 55} 165" fill="none" stroke="{accent}" stroke-width="10" stroke-linejoin="round"/>'
        )
    if theme == "swimming":
        return "".join(
            f'<path d="M0 {205 + i * 38} q60 -{18 + r[i] % 12} 120 0 t120 0 t120 0 t120 0 t120 0 t120 0 t120 0" fill="none" stroke="{white}" stroke-width="9" opacity="{0.9 - i * 0.14:.2f}"/>'
            for i in range(5)
        )
    if theme == "running":
        return (
            f'<path d="M0 {225 + r[3] % 20} L768 {185 + r[4] % 30}" stroke="{white}" stroke-width="12" stroke-dasharray="46 30"/>'
            f'<circle cx="{x}" cy="{y + 18}" r="{size - 4}" fill="{accent}"/>'
            f'<path d="M{x} {y + 44} L{x - 18} {y + 112} L{x - 70} {y + 150} M{x - 18} {y + 112} L{x + 50} {y + 158} M{x - 4} {y + 66} L{x + 55} {y + 88} M{x - 4} {y + 66} L{x - 58} {y + 72}" fill="none" stroke="{accent}" stroke-width="16" stroke-linecap="round" stroke-linejoin="round"/>'
        )
    return (
        f'<circle cx="{x}" cy="{y + 70}" r="82" fill="{white}" stroke="{dark}" stroke-width="7"/>'
        f'<path d="M{x} {y + 70} V{y + 22} M{x} {y + 70} L{x + 38} {y + 92}" stroke="{accent}" stroke-width="9" stroke-linecap="round"/>'
        f'<rect x="{60 + r[3] % 40}" y="245" width="{90 + r[4] % 60}" height="36" rx="10" fill="{accent}"/>'
    )


def render_svg(image_id):
    """Return an illustration for this ID, or None if it is not in the library."""
    theme = image_theme(image_id)
    if theme is None:
        return None
    config = THEMES[theme]
    r = hashlib.sha256(image_id.encode()).digest()
    top, bottom = config["sky"]
    label = f"{config['label']} illustration"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 768 384" role="img" aria-label="{label}">'
        f'<defs><linearGradient id="s" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{top}"/>'
        f'<stop offset="1" stop-color="{bottom}"/></linearGradient></defs>'
        '<rect width="768" height="384" fill="url(#s)"/>'
        f'<circle cx="{560 + r[10] % 150}" cy="{55 + r[11] % 40}" r="{24 + r[12] % 24}" fill="#ffffff" opacity="0.55"/>'
        f'<path d="M0 {282 + r[13] % 24} C240 {252 + r[14] % 34},500 {312 - r[15] % 28},768 {270 + r[16] % 24} V384 H0Z" fill="{config["ground"]}"/>'
        f"{_scene(theme, r, config['accent'])}</svg>"
    )
