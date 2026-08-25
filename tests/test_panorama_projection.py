import numpy as np
import pytest

from pano_platform.panorama_projection import cubemap_to_erp


COLORS = {
    "px": (0, 0, 255),
    "nx": (0, 255, 0),
    "py": (255, 0, 0),
    "ny": (0, 255, 255),
    "pz": (255, 0, 255),
    "nz": (255, 255, 0),
}


def solid_faces(size: int = 32) -> dict[str, np.ndarray]:
    return {name: np.full((size, size, 3), color, np.uint8) for name, color in COLORS.items()}


def test_six_color_cubemap_has_frozen_erp_directions() -> None:
    width = 256
    erp = cubemap_to_erp(solid_faces(), width, nearest=True)
    middle = erp.shape[0] // 2

    assert erp.shape == (128, 256, 3)
    assert tuple(erp[middle, 0]) == COLORS["pz"]
    assert tuple(erp[middle, width // 4]) == COLORS["nx"]
    assert tuple(erp[middle, width // 2]) == COLORS["nz"]
    assert tuple(erp[middle, width * 3 // 4]) == COLORS["px"]
    assert tuple(erp[0, width // 2]) == COLORS["py"]
    assert tuple(erp[-1, width // 2]) == COLORS["ny"]
    assert np.array_equal(erp[:, 0], erp[:, -1])


def test_cubemap_projection_rejects_incomplete_or_odd_inputs() -> None:
    faces = solid_faces()
    faces.pop("ny")
    with pytest.raises(ValueError, match="px/nx/py/ny/pz/nz"):
        cubemap_to_erp(faces, 256)
    with pytest.raises(ValueError, match="even"):
        cubemap_to_erp(solid_faces(), 255)
