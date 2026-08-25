from collections.abc import Mapping

import cv2
import numpy as np


FACE_BASES = {
    "px": ((1, 0, 0), (0, 1, 0)),
    "nx": ((-1, 0, 0), (0, 1, 0)),
    "py": ((0, 1, 0), (0, 0, -1)),
    "ny": ((0, -1, 0), (0, 0, 1)),
    "pz": ((0, 0, 1), (0, 1, 0)),
    "nz": ((0, 0, -1), (0, 1, 0)),
}


def cubemap_to_erp(faces: Mapping[str, np.ndarray], width: int, *, nearest: bool = False) -> np.ndarray:
    if width < 2 or width % 2:
        raise ValueError("ERP width must be an even integer")
    if set(faces) != set(FACE_BASES):
        raise ValueError("cubemap requires px/nx/py/ny/pz/nz faces")
    face_size = next(iter(faces.values())).shape[0]
    if any(image.shape[:2] != (face_size, face_size) for image in faces.values()):
        raise ValueError("cubemap faces must be square and aligned")

    height = width // 2
    longitude = (np.arange(width, dtype=np.float32) + 0.5) / width * (2 * np.pi) - np.pi
    latitude = np.pi / 2 - (np.arange(height, dtype=np.float32) + 0.5) / height * np.pi
    lon, lat = np.meshgrid(longitude, latitude)
    rays = np.stack((np.sin(lon) * np.cos(lat), np.sin(lat), -np.cos(lon) * np.cos(lat)), axis=-1)
    scores = np.stack([rays @ np.asarray(forward, np.float32) for forward, _ in FACE_BASES.values()], axis=-1)
    selected = scores.argmax(axis=-1)
    erp = np.zeros((height, width, 3), np.uint8)
    interpolation = cv2.INTER_NEAREST if nearest else cv2.INTER_LINEAR
    for face_index, (name, (forward, up)) in enumerate(FACE_BASES.items()):
        forward = np.asarray(forward, np.float32)
        up = np.asarray(up, np.float32)
        right = np.cross(forward, up)
        denominator = rays @ forward
        map_x = ((rays @ right) / denominator + 1) * 0.5 * (face_size - 1)
        map_y = (-(rays @ up) / denominator + 1) * 0.5 * (face_size - 1)
        sampled = cv2.remap(faces[name], map_x, map_y, interpolation, borderMode=cv2.BORDER_REPLICATE)
        erp[selected == face_index] = sampled[selected == face_index]
    return erp
