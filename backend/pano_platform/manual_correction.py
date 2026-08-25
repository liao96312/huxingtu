from .opening_projection import add_projected_opening


def add_pixel_annotated_opening(layout, opening_id, opening_type, pixel_start, pixel_end, max_projection_distance_mm=250):
    return add_projected_opening(
        layout, opening_id, opening_type, pixel_start, pixel_end,
        confidence=1, max_projection_distance_mm=max_projection_distance_mm,
    )
