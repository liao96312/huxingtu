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


def test_coordinate_grid_projection_error_is_at_most_two_face_pixels() -> None:
    face_size, width = 128, 512
    y, x = np.indices((face_size, face_size), dtype=np.uint8)
    names = ("px", "nx", "py", "ny", "pz", "nz")
    faces = {name: np.dstack((x, y, np.full_like(x, index))) for index, name in enumerate(names)}
    erp = cubemap_to_erp(faces, width, nearest=True)

    checked = 0
    for row in range(8, width // 2, 19):
        latitude = np.pi / 2 - (row + 0.5) / (width // 2) * np.pi
        for column in range(7, width, 23):
            longitude = (column + 0.5) / width * 2 * np.pi - np.pi
            ray = np.array((np.sin(longitude) * np.cos(latitude), np.sin(latitude), -np.cos(longitude) * np.cos(latitude)))
            scores = [float(ray @ np.asarray(forward)) for forward, _ in (
                ((1, 0, 0), (0, 1, 0)), ((-1, 0, 0), (0, 1, 0)),
                ((0, 1, 0), (0, 0, -1)), ((0, -1, 0), (0, 0, 1)),
                ((0, 0, 1), (0, 1, 0)), ((0, 0, -1), (0, 1, 0)),
            )]
            ordered = sorted(scores, reverse=True)
            if ordered[0] - ordered[1] < 0.05:
                continue
            face_index = int(np.argmax(scores))
            forward, up = (
                ((1, 0, 0), (0, 1, 0)), ((-1, 0, 0), (0, 1, 0)),
                ((0, 1, 0), (0, 0, -1)), ((0, -1, 0), (0, 0, 1)),
                ((0, 0, 1), (0, 1, 0)), ((0, 0, -1), (0, 1, 0)),
            )[face_index]
            forward, up = np.asarray(forward), np.asarray(up)
            right = np.cross(forward, up)
            denominator = ray @ forward
            expected_x = ((ray @ right) / denominator + 1) * 0.5 * (face_size - 1)
            expected_y = (-(ray @ up) / denominator + 1) * 0.5 * (face_size - 1)
            actual_x, actual_y, actual_face = erp[row, column]
            assert int(actual_face) == face_index
            assert abs(int(actual_x) - expected_x) <= 2
            assert abs(int(actual_y) - expected_y) <= 2
            checked += 1
    assert checked > 150
