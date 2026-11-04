"""Gateway checks, run in the Runtime image: the shared IAM geocoder and the fire feed."""
import io
import unittest
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
from checks_support import load_fire_weather_tool, load_registered, sample

geo = load_registered("tested_geocoder", "backend/gateway/geocode/handler.py")
wildfire = load_registered("tested_fire_feed", "backend/gateway/wildfire/handler.py")


class GeocodingChecks(unittest.TestCase):
    def test_stored_results_and_coordinate_order(self):
        """Check that Amazon Location results are stored and returned as latitude then longitude."""
        client = MagicMock()
        client.geocode.return_value = sample()
        with patch.object(geo, "places", return_value=client):
            result = geo.handler({"address": "17000 Sunset Blvd, CA"}, None)
        args = client.geocode.call_args.kwargs
        self.assertEqual(args["IntendedUse"], "Storage")
        self.assertEqual(args["Filter"], {"IncludeCountries": ["USA"]})
        self.assertEqual(result["candidates"][0]["lat"], 34.04)
        self.assertEqual(result["candidates"][0]["lon"], -118.55)
        self.assertEqual(result["candidates"][0]["score"], 99)
        self.assertEqual(result["provider"], "Amazon Location")

    def test_provider_failure_is_not_an_empty_match(self):
        """Check that a geocoder error reports a provider failure, not zero matches."""
        client = MagicMock()
        client.geocode.side_effect = ClientError({"Error": {"Code": "AccessDeniedException"}}, "Geocode")
        with patch.object(geo, "places", return_value=client):
            result = geo.handler({"address": "Los Angeles, CA"}, None)
        self.assertEqual(result["error"]["code"], "GEOCODER_UNAVAILABLE")
        self.assertNotIn("candidates", result)

    def test_bounded_input_and_invalid_coordinates(self):
        """Check that too short, too long or single-use addresses and bad coordinates are refused."""
        for event in ({"address": "x"}, {"address": "x" * 501},
                      {"address": "Los Angeles", "IntendedUse": "SingleUse"}, {}):
            with patch.object(geo, "places") as client:
                self.assertEqual(geo.handler(event, None)["error"]["code"], "INVALID_ADDRESS")
                client.assert_not_called()
        for position in ([True, 34], [float("nan"), 34], [-118, 200]):
            data = sample()
            data["ResultItems"][0]["Position"] = position
            self.assertEqual(geo.normalize(data)["candidates"], [])

    def test_wildfire_adapter_rejects_unbounded_or_injected_queries(self):
        """Check that the ArcGIS fire adapter refuses URLs, SQL filters and oversized distances."""
        base = {"geometry": "-118.5,34.1", "distance": 20}
        for payload in ({**base, "url": "http://example.invalid"},
                        {**base, "where": "1=1 OR private=1"},
                        {**base, "distance": 10000},
                        {**base, "geometry": "nan,34"},
                        {**base, "outSR": 3857}):
            with patch.object(wildfire.OPENER, "open") as fetch:
                self.assertEqual(wildfire.handler(payload, None)["error"]["code"], "INVALID_FIRE_QUERY")
                fetch.assert_not_called()
        with patch.object(wildfire.OPENER, "open", return_value=io.BytesIO(b'{"features":[]}')) as fetch:
            self.assertEqual(wildfire.handler(base, None), {"features": []})
            url = fetch.call_args.args[0].full_url
            self.assertTrue(url.startswith(wildfire.URL + "?"))
            self.assertNotIn("token=", url)

    def test_failed_fire_feed_is_not_reported_as_zero_fires(self):
        """Check that an ArcGIS error or malformed body is a failure, never an empty fire list."""
        for body in (b'{"error":{"code":498}}', b'not-json', b'[]'):
            with patch.object(wildfire.OPENER, "open", return_value=io.BytesIO(body)):
                self.assertEqual(wildfire.handler({"geometry": "-118,34", "distance": 20}, None)
                                 ["error"]["code"], "WILDFIRE_UNAVAILABLE")
        # Exercise the real orchestration function, with stand-ins for its generated presentation
        # models: these failure paths must never construct empty MapData.
        fire, models = load_fire_weather_tool()
        session = MagicMock()
        lookup = fire.build_fire_weather_tool(session)
        with self.assertLogs(fire.logger.name, level="ERROR") as logs:
            for result in ({"isError": True}, {"content": [{"text": '{"error":{"code":"WILDFIRE_UNAVAILABLE"}}'}]},
                           {"content": [{"text": "not-json"}]}, None):
                session.call_tool_sync.return_value = result
                with self.assertRaisesRegex(RuntimeError, "do not infer"):
                    lookup(34, -118)
        self.assertEqual(sum("Fire weather tool failed" in line for line in logs.output), 4)
        models.MapData.assert_not_called()
        models.MapSummary.assert_not_called()
