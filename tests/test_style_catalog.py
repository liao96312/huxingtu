from pathlib import Path

from pano_platform.contracts import StyleCatalog


ROOT = Path(__file__).resolve().parents[1]


def test_style_catalog_separates_design_style_from_visual_mode() -> None:
    catalog = StyleCatalog.model_validate_json(
        (ROOT / "datasets/derived/fp-001/style-catalog.v1.json").read_text(encoding="utf-8")
    )
    assert [(style.label, style.designStyle) for style in catalog.styles] == [
        ("现代", "modern"), ("中式", "Chinese style"), ("北欧", "Scandinavian"),
        ("奶油风", "cream style"), ("法式", "French style"), ("混搭", "eclectic"),
        ("轻奢", "Light luxury style"), ("侘寂", "Wabi-sabi style"),
    ]
    assert all(style.visualMode == "realistic" for style in catalog.styles)
    assert all(style.promptSuffix for style in catalog.styles)
