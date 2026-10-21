"""
simulation.shared_walls
───────────────────────
Walls shared with neighbouring buildings: internal (adiabatic) and without windows.

The request's optional ``shared_floors_by_edge`` lists, per footprint edge
[p0->p1, p1->p2, ...], the floors (1-based) whose wall along that edge touches
a neighbouring building.
"""

from honeybee.boundarycondition import boundary_conditions


def shared_edges_on_floor(cfg: dict, floor: int) -> set[int]:
    """Indices of the footprint edges whose wall is shared with a neighbour on the given floor (1-based)."""
    shared_floors_by_edge = cfg.get('shared_floors_by_edge') or []
    return {index for index, floors in enumerate(shared_floors_by_edge) if floor in floors}


def make_adiabatic(wall) -> None:
    """Turn a wall into an internal wall with no heat flow through it. The wall must not have windows."""
    wall.boundary_condition = boundary_conditions.adiabatic
