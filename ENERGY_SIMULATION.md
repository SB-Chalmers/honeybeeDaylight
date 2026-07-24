# Energy Simulation — Backend Implementation Guide

This document describes what the frontend expects from the honeybeeDaylight backend to support EnergyPlus energy simulations via EPSM.

---

## Context

The frontend (threejsEditor) already runs **daylight simulations** through this backend (`POST /v1/studies`). The goal is to add a parallel **energy simulation** flow using the same building geometry.

Construction materials are sourced from the **EPSM database** at `https://epsm.chalmers.se`. The frontend lets users pick individual constructions per element type (wall / floor / roof / window) from EPSM's `GET /api/constructions/` endpoint (filtered by `element_type`). The backend receives those construction names and is responsible for generating the EnergyPlus IDF and submitting it to EPSM for simulation.

> **Embodied carbon is handled entirely by the frontend** — `calculateEmbodiedCarbon()` in `EPSMService.ts` reads `gwp_kgco2e_per_m2` from the construction record and multiplies by surface area. The backend does **not** need to compute or return GWP. Only operational energy (heating / cooling / total kWh) comes from the backend.

All EPSM read and simulation endpoints are **publicly accessible — no auth required**.

---

## EPSM `/api/constructions/` response schema

This is the endpoint the backend uses to fetch construction layer data for IDF generation.

```jsonc
// GET https://epsm.chalmers.se/api/constructions/  →  array of:
{
  "id": "uuid",
  "name": "ExteriorWall_Brick",          // EnergyPlus Construction object name
  "element_type": "wall",               // wall | roof | floor | ceiling | window
  "is_window": false,
  "u_value_w_m2k": 1.2,
  "gwp_kgco2e_per_m2": 85.2,            // pre-computed; used by frontend only
  "cost_sek_per_m2": 450.0,
  "layers": [
    {
      "layer_order": 1,
      "material_name": "Brick",           // EnergyPlus Material object name
      "thickness_m": 0.1016,
      "conductivity_w_mk": 0.89,
      "is_glazing_layer": false
    },
    // ... more layers ordered outside → inside
  ]
}
```

Filter by `element_type` to get only the constructions you need.
The `name` and `material_name` fields are the exact EnergyPlus object names — use them verbatim in the IDF.

---

## What the Frontend Sends

```jsonc
// POST /v1/energy/studies
{
  // Same room geometry shape as daylight studies
  "room": {
    "footprint_coordinates": [[0,0],[10,0],[10,8],[0,8]],  // metres, counter-clockwise
    "floor_to_floor_height": 3.2,
    "floors": 4,
    "orientation_offset": 0.0,
    "wwr": 0.4,
    "additional_horizontal_shading_depth": 0.0
  },

  // EPSM construction names — exactly as returned by GET /api/constructions/
  // These are the EnergyPlus Construction object names stored in EPSM
  "constructions": {
    "wall":   "ExteriorWall_Brick",
    "floor":  "GroundFloor_Concrete",
    "roof":   "FlatRoof_Insulated",
    "window": "DoubleGlazing_LowE"
  },

  // Building use type (maps to honeybee-energy ProgramType)
  "building_program": "Office",      // Office | Residential | Retail | School | Hospital

  // HVAC system identifier (maps to honeybee-energy HVAC template)
  "hvac_system": "VAV",              // VAV | CAV | Radiant | Split System | Default HVAC

  // Natural ventilation flag
  "natural_ventilation": false,

  // EPW location name (same server-side list as daylight: GET /v1/locations)
  "location": "Gothenburg"
}
```

---

## What the Frontend Expects Back

### `POST /v1/energy/studies` → 202

```json
{
  "study_id": "uuid-here",
  "status": "queued"
}
```

### `GET /v1/energy/studies/{study_id}` → 200

```json
{
  "study_id": "uuid-here",
  "status": "running",   // queued | running | complete | failed
  "stage": "idf_generation"
}
```

### `GET /v1/energy/studies/{study_id}/result` → 200

```json
{
  "study_id": "uuid-here",
  "status": "complete",
  "heating_energy_kwh": 8320.2,
  "cooling_energy_kwh": 4100.1,
  "total_energy_kwh": 15420.5,
  "heating_demand_kwh_m2": 42.3,   // normalised by floor area
  "cooling_demand_kwh_m2": 20.8
}
```

> Do **not** include `gwp_total` or embodied carbon values here — those are computed client-side from the EPSM construction data and never need to round-trip through this backend.

Return `{ "status": "failed", "error": "..." }` on failure.

---

## Suggested Backend Implementation

### 1. Validate inputs

Reuse the same `RoomInput` / `ContextBuildingInput` Pydantic models from `api/schemas.py`.
Add a new `EnergyRequest` schema in `api/schemas.py`:

```python
class ConstructionNames(BaseModel):
    wall: str
    floor: str
    roof: str
    window: str

class EnergyRequest(BaseModel):
    room: RoomInput
    constructions: ConstructionNames
    building_program: str = "Office"
    hvac_system: str = "Default HVAC"
    natural_ventilation: bool = False
    location: str  # must match a key in WEATHER_DIR
```

### 2. Fetch construction layer data from EPSM

The `/api/constructions/` endpoint returns each construction with its ordered layers
(material name, thickness, conductivity) — exactly what EnergyPlus IDF blocks need.

```python
import httpx

EPSM_BASE = "https://epsm.chalmers.se"

async def fetch_epsm_constructions(names: ConstructionNames) -> dict:
    """
    Returns a dict keyed by element type, each value is the full construction
    record including layers[] from GET /api/constructions/.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{EPSM_BASE}/api/constructions/")
        resp.raise_for_status()
        all_constructions = resp.json()

    # Index by name — names are the exact EnergyPlus Construction object names
    by_name = {c["name"]: c for c in all_constructions}

    missing = [n for n in [names.wall, names.floor, names.roof, names.window]
               if n not in by_name]
    if missing:
        raise ValueError(f"EPSM constructions not found: {missing}")

    return {
        "wall":   by_name[names.wall],
        "floor":  by_name[names.floor],
        "roof":   by_name[names.roof],
        "window": by_name[names.window],
    }
```

**Each construction record looks like:**
```python
{
    "name": "ExteriorWall_Brick",       # EnergyPlus Construction name
    "element_type": "wall",
    "u_value_w_m2k": 1.2,
    "gwp_kgco2e_per_m2": 85.2,         # ignore — frontend uses this, not backend
    "layers": [
        {
            "layer_order": 1,
            "material_name": "Brick",   # EnergyPlus Material name
            "thickness_m": 0.1016,
            "conductivity_w_mk": 0.89,
            "is_glazing_layer": False
        },
        # ... ordered outside → inside
    ]
}
```

### 3. Generate the EnergyPlus IDF

Since the EPSM layer data gives you exact EnergyPlus material names and thermal properties,
you have two options:

**Option A — Direct IDF text generation (simpler, no extra deps):**

```python
def construction_to_idf_blocks(c: dict) -> str:
    """
    Emit Material, and Construction, IDF blocks for one EPSM construction.
    material_name and construction name are already EnergyPlus names.
    """
    blocks = []
    for layer in c["layers"]:
        if not layer.get("is_glazing_layer"):
            # Use EPSM material properties — density/specific_heat not in the
            # layers endpoint; fetch from GET /api/materials/ if needed, or
            # use EnergyPlus defaults (density=2300, cp=900 are safe placeholders).
            blocks.append(f"""
Material,
  {layer['material_name']},
  MediumRough,
  {layer['thickness_m']},
  {layer['conductivity_w_mk']},
  2300.0,
  900.0;
""")

    layer_names = ", ".join(L["material_name"] for L in c["layers"])
    blocks.append(f"""
Construction,
  {c['name']},
  {layer_names};
""")
    return "\n".join(blocks)
```

> If you need accurate density and specific heat, call `GET /api/materials/?name=<name>` or
> include a second fetch of `GET /api/materials/` and join on `material_name`. For a first
> implementation, EnergyPlus defaults are acceptable for most opaque materials.

**Option B — honeybee-energy (more accurate, more setup):**

```python
from honeybee_energy.material.opaque import EnergyMaterial
from honeybee_energy.construction.opaque import OpaqueConstruction

def epsm_to_hb_construction(c: dict) -> OpaqueConstruction:
    materials = [
        EnergyMaterial(
            L["material_name"],
            L["thickness_m"],
            L["conductivity_w_mk"],
            density=2300,      # fetch from /api/materials/ for accuracy
            specific_heat=900
        )
        for L in c["layers"] if not L.get("is_glazing_layer")
    ]
    return OpaqueConstruction(c["name"], materials)
```

### 4. Submit IDF + EPW to EPSM

The EPSM simulation endpoint is at the **public hosted service**, not the local EPSM Django app.

```python
async def submit_to_epsm(idf_path: str, epw_path: str) -> str:
    """Returns the EPSM simulation_id UUID."""
    async with httpx.AsyncClient(timeout=120) as client:
        with open(idf_path, "rb") as idf, open(epw_path, "rb") as epw:
            resp = await client.post(
                "https://epsm.chalmers.se/api/simulation/run/",
                files={"idf_files": (os.path.basename(idf_path), idf),
                       "weather_file": (os.path.basename(epw_path), epw)}
            )
        resp.raise_for_status()
        return resp.json()["simulation_id"]
```

**EPSM simulation status values:** `pending → running → completed / failed`

**EPSM result fields** (from `GET https://epsm.chalmers.se/api/simulation/{id}/results/`):
```jsonc
{
  "simulationId": "...",
  "total_energy_kwh": 15420.5,
  "heating_energy_kwh": 8320.2,
  "cooling_energy_kwh": 4100.1,
  "hourly_timeseries": { ... }   // optional, can be large
}
```

Note: EPSM does not return `gwp_total` from the simulation endpoint — that field exists on
the EPSM Django `SimulationResult` model but is populated internally after post-processing.
The frontend computes GWP independently from construction data, so this is not needed.

### 5. Poll EPSM and proxy results

Store the `epsm_simulation_id` alongside the study record in SQLite (extend `api/store.py`).
The `GET /v1/energy/studies/{study_id}` endpoint polls EPSM when status is not yet final:

```python
@router.get("/energy/studies/{study_id}")
async def get_energy_study(study_id: str):
    record = store.get(study_id)
    if record.status in ("queued", "running"):
        async with httpx.AsyncClient() as client:
            epsm_resp = await client.get(
                f"https://epsm.chalmers.se/api/simulation/{record.epsm_simulation_id}/status/"
            )
        epsm = epsm_resp.json()  # {"status": "running", "progress": 65}

        if epsm["status"] == "completed":
            async with httpx.AsyncClient() as client:
                results_resp = await client.get(
                    f"https://epsm.chalmers.se/api/simulation/{record.epsm_simulation_id}/results/"
                )
            raw = results_resp.json()
            floor_area = record.footprint_area * record.floors
            store.update(study_id, status="complete", result={
                "heating_energy_kwh":  raw.get("heating_energy_kwh"),
                "cooling_energy_kwh":  raw.get("cooling_energy_kwh"),
                "total_energy_kwh":    raw.get("total_energy_kwh"),
                "heating_demand_kwh_m2": raw.get("heating_energy_kwh", 0) / floor_area,
                "cooling_demand_kwh_m2": raw.get("cooling_energy_kwh", 0) / floor_area,
            })
        elif epsm["status"] == "failed":
            store.update(study_id, status="failed", error="EPSM simulation failed")
    return store.get(study_id)
```

### 6. Normalise results by floor area

The frontend expects `heating_demand_kwh_m2` and `cooling_demand_kwh_m2` (per m² of floor area).
Calculate: `floor_area = footprint_area × floors` and divide EPSM's `heating_energy_kwh` etc.

---

## HVAC name → honeybee-energy mapping

| Frontend value | honeybee-energy class |
|---|---|
| `Default HVAC` | `IdealAirSystem` |
| `VAV` | `VAVwithPFP` or `AllAirSystem` with VAV |
| `CAV` | `AllAirSystem` with CAV |
| `Radiant` | `RadiantConvective` |
| `Split System` | `PTAC` |

---

## Building Program → ProgramType

| Frontend value | HB-Energy identifier |
|---|---|
| `Office` | `Generic Office Program` |
| `Residential` | `Generic Apartment` |
| `Retail` | `Generic Retail` |
| `School` | `Generic Classroom` |
| `Hospital` | `Generic Hospital` |

Use `ProgramType.from_standards_dict(identifier)` to load from the honeybee-energy standards library.

---

## File / Module Layout (suggested)

```
api/
  routes.py          ← add /v1/energy/studies endpoints here
  schemas.py         ← add EnergyRequest, EnergyStudyResult schemas
  store.py           ← add epsm_simulation_id column to studies table
simulation/
  energy/
    __init__.py
    idf_builder.py   ← Room → IDF generation
    epsm_client.py   ← EPSM API calls (fetch constructions, submit, poll)
    hvac_mapper.py   ← frontend HVAC name → honeybee-energy class
    program_mapper.py← frontend program name → ProgramType identifier
```

---

## Environment / Dependencies

Add to `requirements.txt`:
```
honeybee-energy>=1.100
httpx>=0.27
```

`honeybee-energy` is a separate package from `honeybee-radiance` (daylight). Both can coexist.

---

## Questions for the frontend dev

- Should energy studies support context buildings (shading geometry) the same way daylight studies do? If yes, add `context_buildings` to `EnergyRequest`. Answer : Yes
- Should the `/v1/energy/studies` endpoint support a synchronous variant (`POST /v1/energy/studies:run-sync`) matching the existing daylight pattern? Answer : Yes
- The frontend currently stores `wall_construction`, `floor_construction`, `roof_construction`, `window_construction` as strings on each `BuildingData`. These are the EPSM construction names that will be sent in the `constructions` block above. This is fine.- Do you need density/specific_heat for each material layer (for more accurate IDF)? If so, the backend should also call `GET https://epsm.chalmers.se/api/materials/` and join on `material_name`. Otherwise, use EnergyPlus defaults (concrete: 2300 kg/m³, 900 J/kg·K) for a first pass. Answer : get from EPSM back end