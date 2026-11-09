# Heath in Grasshopper

`heath.ghx` rebuilds a Heath request with stock Honeybee, Dragonfly and Foton
components, so students can continue a Heath study in Grasshopper. It builds
the same model as this backend and gives the same daylight and energy results.

## For students

1. **Install** Rhino 8, [Ladybug Tools](https://www.food4rhino.com/en/app/ladybug-tools)
   1.8 or newer, OpenStudio 3.9, and **foton** 0.3.6 from Rhino's
   `PackageManager` (set `install_` on Foton Settings to True once).
2. **Get the file.** In Heath, choose *Open in Grasshopper*. You get `heath.ghx`
   with your building already filled in. (Or open the `heath.ghx` from this folder and paste
   the Heath request JSON into the `request` panel.)
3. **Open it** in Grasshopper. The sliders show your Heath settings.
4. **Run the red groups in order:** 1 daylight factor, 2 annual daylight (sDA),
   3 energy. Each takes a few seconds.
5. **Explore.** The notes on each group say what happens there and what to try.

## What is not stock

Three small GhPython components:

1. **Heath Request** (`heath_request.py`) reads the JSON: footprint, context,
   energy zones, EPSM construction layers and the weather file.
2. **Heath Sensors** (`heath_sensors.py`) lays out the sensor grid. Heath's
   grid turns with the building, whereas *HB Sensor Grid from Rooms* follows the world axes.
3. Canvas notes mark any other workaround.

The template also differs from the backend in two ways, neither of which changes
the results:

1. The ideal air limits are 10 MW instead of *NoLimit*, because *HB Model to OSM*
   cannot translate *NoLimit* (honeybee-openstudio 0.3.11).
2. Zone sizing is on, which OpenStudio needs for the ideal air system.

Measured on the example request, energy is within 0.004 kWh/m² a year of the backend.

Not modelled in the template yet: party walls (`shared_floors_by_edge`) and
per-edge window ratios (`wwr_by_edge`).

## For developers

1. `POST /v1/grasshopper` takes a `/v1/simulate` body (`energy` optional) and
   returns `heath.ghx` (see `api/grasshopper.py`). The request goes into the
   `request` panel, the sliders are set to its values, and the two GhPython
   components get the code in this folder.
2. To change the canvas, edit `heath.ghx` in Grasshopper and save it as `.ghx`.
   Keep these nicknames: `request`, the slider names in `api/grasshopper.py`,
   `all_floors_`, `Heath Request` and `Heath Sensors`.
3. `tests/test_grasshopper.py` checks that the components match the backend.
