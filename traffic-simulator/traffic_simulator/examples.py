"""Saved example study areas: the Traffic Lab examples and the demo areas a starting worker pre-builds.

Each name has saved Caltrans highway geometry in data/<name>-highways.json, which the Traffic Lab examples route
(/api/v1/research/examples/<name>) serves. cache.demo_areas_to_prebuild in deployment/settings.toml lists the areas
whose road networks a starting worker prepares (worker.prebuild); its allowed values are these names.
"""

# name -> (latitude, longitude, radius in km) of the study area.
EXAMPLES = {"paradise": (39.7596, -121.6219, 8), "malibu": (34.0259, -118.7798, 15)}
