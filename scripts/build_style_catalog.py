import json
from pathlib import Path

from pano_platform.contracts import StyleCatalog, canonical_sha256


ROOT = Path(__file__).resolve().parents[1]
styles = [
    ("modern", "现代", "modern", ["warm white", "soft gray", "light oak"], ["oak", "stone", "matte metal"], "Contemporary modern interior, clean architectural lines, warm white and light oak, restrained built-ins, realistic natural light."),
    ("chinese", "中式", "Chinese style", ["warm white", "walnut", "ink black"], ["walnut", "stone", "linen"], "Contemporary Chinese interior, balanced symmetry, warm walnut, pale stone, subtle ink-art accents, elegant and restrained rather than ornate."),
    ("scandinavian", "北欧", "Scandinavian", ["white", "pale oak", "sage"], ["pale oak", "wool", "linen"], "Scandinavian interior, pale oak, soft white walls, wool and linen textures, functional furniture, generous natural daylight."),
    ("cream", "奶油风", "cream style", ["ivory", "cream", "light beige"], ["microcement", "boucle", "light oak"], "Realistic cream-style interior, soft ivory and light beige palette, rounded but structurally faithful furniture, boucle and light oak, diffuse natural light, no excessive ornament."),
    ("french", "法式", "French style", ["ivory", "taupe", "brass"], ["plaster", "oak", "brass"], "Contemporary French interior, ivory plaster, restrained wall moulding, warm oak and subtle brass, elegant proportions, realistic daylight."),
    ("eclectic", "混搭", "eclectic", ["warm neutral", "terracotta", "deep green"], ["wood", "textile", "stone"], "Controlled eclectic interior, coherent warm-neutral base with limited terracotta and deep-green accents, mixed textures without clutter."),
    ("light-luxury", "轻奢", "Light luxury style", ["warm white", "greige", "champagne metal"], ["stone", "wood veneer", "champagne metal"], "Light-luxury interior, premium stone and wood veneer, restrained champagne-metal details, refined lighting, no gaudy decoration."),
    ("wabi-sabi", "侘寂", "Wabi-sabi style", ["clay", "sand", "warm gray"], ["limewash", "aged wood", "travertine"], "Wabi-sabi interior, warm clay and sand tones, limewash, aged wood and travertine, quiet imperfect textures, minimal objects, soft natural light."),
]
payload = {
    "schemaVersion": "0.1.0", "source": "independently defined style catalog v1",
    "styles": [{
        "id": style_id, "label": label, "designStyle": provider_value, "visualMode": "realistic",
        "inspirations": ["natural light", "texture"], "palette": palette, "materials": materials, "promptSuffix": prompt,
    } for style_id, label, provider_value, palette, materials, prompt in styles],
}
payload["contentHash"] = canonical_sha256(payload)
catalog = StyleCatalog.model_validate(payload)
output = ROOT / "datasets/style-catalog.v1.json"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(catalog.model_dump_json(indent=2), encoding="utf-8")
print(f"{len(catalog.styles)} styles -> {output.name}")
