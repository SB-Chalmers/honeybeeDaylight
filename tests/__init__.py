def simulation_room(**fields) -> dict:
    """A room as the jobs receive it: validated and filled in by the API schema."""
    from api.schemas import RoomInput

    return RoomInput(**fields).to_simulation_dict()


def full_room(fields: dict) -> dict:
    """simulation_room for a dict of fields."""
    return simulation_room(**fields)
