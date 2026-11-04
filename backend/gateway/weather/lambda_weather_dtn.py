import json
import logging
import math
import os
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any
from uuid import UUID

import boto3
import httpx
from botocore.config import Config

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)
# httpx logs every request URL at INFO, and DTN URLs carry the searched coordinates.
logging.getLogger("httpx").setLevel(logging.WARNING)

# DTN API Configuration
DTN_API_BASE = os.environ.get(
    'DTN_API_BASE_URL', 'https://weather.api.dtn.com')
DTN_AUTH_URL = os.environ.get(
    'DTN_AUTH_URL', 'https://api.auth.dtn.com/v1/tokens/authorize')
# The backend API keys secret, which holds DTN_CLIENT_ID and DTN_CLIENT_SECRET (L2, D53): read by ARN, never from
# the environment.
CREDENTIALS_SECRET_ARN = os.environ.get("DTN_CREDENTIALS_SECRET_ARN", "")
OPERATION_DEADLINE = ContextVar("dtn_operation_deadline", default=None)
REQUEST_LIMITS = ContextVar("dtn_request_limits", default=None)
MAX_PROVIDER_BYTES = 2*1024*1024


class WeatherBudgetExpired(TimeoutError):
    """The enclosing operation has no time left for another provider attempt."""


def operation_limits():
    """Deployment settings declare elapsed time, per-request time and retry count together."""
    operation = float(os.environ["DTN_OPERATION_TIMEOUT_SECONDS"])
    request = float(os.environ["DTN_REQUEST_TIMEOUT_SECONDS"])
    attempts = int(os.environ["DTN_MAX_ATTEMPTS"])
    reserve = float(os.environ["DTN_SERIALIZATION_RESERVE_SECONDS"])
    if (not all(math.isfinite(value) for value in (operation, request, reserve))
            or not 0 < request <= operation <= 300 or not 1 <= attempts <= 3 or not 0 <= reserve <= 30):
        raise ValueError("Invalid DTN operation limits")
    return operation, request, attempts, reserve


def request_limits():
    configured = REQUEST_LIMITS.get()
    if configured is not None:
        return configured
    _, request, attempts, _ = operation_limits()
    return request, attempts


def request_timeout():
    maximum, _ = request_limits()
    deadline = OPERATION_DEADLINE.get()
    remaining = maximum if deadline is None else deadline-time.monotonic()
    if remaining <= 0:
        raise WeatherBudgetExpired()
    return min(maximum, remaining)


def retry_pause(response=None):
    delay = 1.0
    if response is not None:
        supplied = response.headers.get("Retry-After", "")
        if supplied.isdecimal() and len(supplied) <= 10:
            delay = min(5.0, max(1.0, float(supplied)))
    deadline = OPERATION_DEADLINE.get()
    if deadline is not None and deadline-time.monotonic() <= delay:
        raise WeatherBudgetExpired()
    time.sleep(delay)


class CredentialsUnavailable(RuntimeError):
    """The DTN credentials are not configured; the message is fixed text and never holds a value."""


@lru_cache(maxsize=1)
def secrets():
    """Secrets Manager client for the credentials read, bounded like the geocode adapter's client."""
    return boto3.client("secretsmanager", config=Config(
        connect_timeout=2, read_timeout=5, retries={"total_max_attempts": 2}))


@lru_cache(maxsize=1)
def dtn_credentials() -> tuple[str, str]:
    """Read the DTN client ID and secret once per execution environment from the secret named by ARN.

    lru_cache keeps only a returned pair: a failed read raises, so the next invocation reads again. Every
    deploy-all.sh --apply updates the function, so its new execution environments read the current secret.
    A ClientError or BotoCoreError propagates to the handler, which answers WEATHER_UNAVAILABLE.
    """
    if not CREDENTIALS_SECRET_ARN:
        raise CredentialsUnavailable("not configured")
    text = secrets().get_secret_value(SecretId=CREDENTIALS_SECRET_ARN).get("SecretString")
    try:
        values = json.loads(text) if isinstance(text, str) else None
    except json.JSONDecodeError:
        values = None
    pair = tuple(values.get(key) if isinstance(values, dict) else None
                 for key in ("DTN_CLIENT_ID", "DTN_CLIENT_SECRET"))
    if not all(isinstance(value, str) and value.strip() for value in pair):
        raise CredentialsUnavailable("not configured")
    return pair

# WMO Weather Code Descriptions
WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    4: "Haze", 5: "Smoke/Dust",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail"
}

# Current conditions parameters (point values)
CURRENT_PARAMS = [
    'hotDryWindyIndex', 'airTemp', 'dewPoint', 'relativeHumidity',
    'heatIndex', 'windSpeed', 'windDirection', 'windGust',
    'precipAmount', 'precipProb', 'weatherCode'
]

# Forecast parameters (verified to work with interval=6h via DTN API testing)
FORECAST_PARAMS = [
    'airTempMax', 'dewPointMax', 'relativeHumidityMin',
    'windSpeedMax', 'windDirectionAvg', 'windGustMax',
    'precipAmountSum', 'precipProbMax', 'weatherCode'
]
# Note: hotDryWindyIndex does NOT support aggregation - only available in CURRENT_PARAMS


class InvalidWeatherQuery(ValueError):
    """A weather query outside the adapter contract; its message is fixed text naming the broken rule."""


def error(code, message):
    """Answer with a stable error code and fixed message, as the geocode adapter does; details stay in the log."""
    return {"error": {"code": code, "message": message}}


class DTNTokenManager:
    """Manages OAuth2 token lifecycle for DTN API"""

    def __init__(self):
        self.token = None
        self.expiry = None

    def get_token(self) -> str:
        """Get a valid access token, refreshing if necessary.

        Declared retries share the current operation deadline. Throttle, selected
        5xx and network failures may recover; ordinary auth refusals are terminal.
        """
        if self.token and self.expiry and self.expiry > time.time() + 10:
            return self.token

        client_id, client_secret = dtn_credentials()
        last_exc: Exception | None = None
        _, attempts = request_limits()
        for attempt in range(attempts):
            try:
                response = httpx.post(
                    DTN_AUTH_URL,
                    json={
                        'grant_type': 'client_credentials',
                        'client_id': client_id,
                        'client_secret': client_secret,
                        'audience': 'https://weather.api.dtn.com/conditions'
                    },
                    timeout=request_timeout()
                )
                request_timeout()  # An over-budget response cannot start another stage.
                response.raise_for_status()
                data = response.json()['data']
                self.token = data['access_token']
                self.expiry = time.time() + data['expires_in']
                return self.token
            except httpx.HTTPStatusError as e:
                last_exc = e
                # Only retry on 5xx; auth errors (4xx) won't recover.
                if (e.response.status_code < 500 and e.response.status_code != 429) or attempt == attempts-1:
                    raise
                logger.warning(
                    "DTN auth returned %d, retrying once...", e.response.status_code)
                retry_pause(e.response)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_exc = e
                if attempt == attempts-1:
                    raise
                logger.warning(
                    "DTN auth network error: %s, retrying once...", type(e).__name__)
                retry_pause()

        # Defensive: should not reach here but keep type checker happy.
        if last_exc:
            raise last_exc
        raise RuntimeError("DTN token request failed without an exception")


token_manager = DTNTokenManager()


def make_dtn_request(endpoint: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """Make authenticated request to DTN API.

    Declared bounded retries on transient 5xx, throttle and network errors.
    Returns None on failure; callers produce explicit unavailable source receipts.
    """
    _, attempts = request_limits()
    for attempt in range(attempts):
        try:
            token = token_manager.get_token()
            headers = {'Authorization': f'Bearer {token}',
                       'Accept': 'application/json'}

            with httpx.Client(timeout=request_timeout()) as client, client.stream(
                    "GET", f"{DTN_API_BASE}{endpoint}", headers=headers, params=params) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    request_timeout()
                    if len(body)+len(chunk) > MAX_PROVIDER_BYTES:
                        raise ValueError("Weather provider response exceeded its size limit")
                    body.extend(chunk)
                request_timeout()
                return json.loads(body)
        except httpx.HTTPStatusError as e:
            # Never the exception text: it quotes the request URL with the searched coordinates.
            if (e.response.status_code < 500 and e.response.status_code != 429) or attempt == attempts-1:
                logger.error("DTN API error (%d)", e.response.status_code)
                return None
            logger.warning(
                "DTN API returned %d, retrying once...", e.response.status_code)
            retry_pause(e.response)
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            if attempt == attempts-1:
                logger.error("DTN API network error after retry: %s", type(e).__name__)
                return None
            logger.warning("DTN API network error: %s, retrying once...", type(e).__name__)
            retry_pause()
        except WeatherBudgetExpired:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("DTN API unexpected error: %s", type(e).__name__)
            return None
    return None


# Field mappings: DTN param → (output field, unit)
# Handles both point values (airTemp) and aggregated values (airTempMax, airTempMin)
FIELD_MAP = {
    'airTemp': ('temperature', 'degF'),
    'airTempMax': ('temperature', 'degF'),
    'dewPoint': ('dewpoint', 'degF'),
    'dewPointMax': ('dewpoint', 'degF'),
    'heatIndex': ('heatIndex', 'degF'),
    'relativeHumidity': ('relativeHumidity', 'percent'),
    'relativeHumidityMin': ('relativeHumidity', 'percent'),
    'precipAmount': ('precipAmount', 'in'),
    'precipAmountSum': ('precipAmount', 'in'),
    'precipProb': ('precipProb', 'percent'),
    'precipProbMax': ('precipProb', 'percent')
}

WIND_MAP = {
    'windSpeed': ('speed', 'mph'),
    'windSpeedMax': ('speed', 'mph'),
    'windDirection': ('direction', 'degrees'),
    'windDirectionAvg': ('direction', 'degrees'),
    'windGust': ('gust', 'mph'),
    'windGustMax': ('gust', 'mph')
}

HDW_MAP = {
    'hotDryWindyIndex': 'hotDryWindyIndex',
    'hotDryWindyIndexMax': 'hotDryWindyIndex'
}


def remove_nulls(obj):
    """Recursively remove None values from dictionaries and lists"""
    if isinstance(obj, dict):
        return {k: remove_nulls(v) for k, v in obj.items() if v is not None}
    elif isinstance(obj, list):
        return [remove_nulls(item) for item in obj if item is not None]
    return obj


def build_weather_obj(data: dict[str, Any]) -> dict[str, Any]:
    """Build weather object from DTN data (handles both point and aggregated values).

    Only finite numbers are read. Any other DTN value (text, an object, a list, a boolean, NaN, infinity)
    counts as missing, so the values add no key or text beyond this adapter's names, units and descriptions.
    DTN's own text in a receipt is the period key, which map_dtn_response copies as the timestamp.
    """
    data = {param: value for param, value in (data.items() if isinstance(data, dict) else ())
            if type(value) in (int, float) and math.isfinite(value)}
    weather = {}

    # Scalar fields - try each possible name
    for dtn_param, (field, unit) in FIELD_MAP.items():
        if (val := data.get(dtn_param)) is not None:
            weather[field] = {"value": val, "unit": unit}

    # Wind composite
    wind = {}
    for dtn_param, (field, unit) in WIND_MAP.items():
        if (val := data.get(dtn_param)) is not None:
            wind[field] = {"value": val, "unit": unit}
    if wind:
        weather['wind'] = wind

    # Precipitation composite
    precip = {}
    amount = data.get('precipAmount')
    if amount is None:
        amount = data.get('precipAmountSum')
    if amount is not None:
        precip['amount'] = {"value": amount, "unit": "in"}
    prob = data.get('precipProb')
    if prob is None:
        prob = data.get('precipProbMax')
    if prob is not None:
        precip['probability'] = {"value": prob, "unit": "percent"}
    if precip:
        weather['precipitation'] = precip

    # Weather description
    if (code := data.get('weatherCode')) is not None:
        weather['weatherDescription'] = WEATHER_CODES.get(
            code, f'Weather code {code}')

    # Fire weather index
    for dtn_param, field in HDW_MAP.items():
        if (val := data.get(dtn_param)) is not None:
            weather['dtnSpecific'] = {field: {"value": val, "unit": ''}}
            break

    return weather


def is_timestamp(key: str) -> bool:
    """Accept an explicit observation instant, never a date-only or timezone-ambiguous period."""
    try:
        parsed = datetime.fromisoformat(key)
    except (TypeError, ValueError):
        return False
    return parsed.utcoffset() is not None


def map_dtn_response(lat: float, lon: float, dtn_data: dict, is_forecast: bool = False) -> dict[str, Any]:
    """Map DTN response to clean schema with only critical fields.

    DTN keys each period by its time. That key is DTN text, and it reaches the receipt as the period's
    timestamp and, for a forecast, as the day's date and its dayName fallback. A period whose key does not
    parse as an ISO 8601 timestamp is skipped, so a key cannot carry free provider text.
    """
    if dtn_data is None:
        return {"latitude": lat, "longitude": lon, "error": "Weather provider unavailable"}
    if (not isinstance(dtn_data, dict) or not isinstance(dtn_data.get('features'), list)
            or not dtn_data['features'] or not isinstance(dtn_data['features'][0], dict)):
        return {"latitude": lat, "longitude": lon, "error": "No weather data available"}

    properties = dtn_data['features'][0].get('properties', {})
    if not isinstance(properties, dict):
        return {"latitude": lat, "longitude": lon, "error": "No timestamp data"}
    timestamps = sorted((key for key, value in properties.items()
                         if is_timestamp(key) and isinstance(value, dict)),
                        key=lambda key: datetime.fromisoformat(key).astimezone(UTC))

    if not timestamps:
        return {"latitude": lat, "longitude": lon, "error": "No timestamp data"}

    if is_forecast:
        # Build periods (DTN returns intervals based on interval parameter)
        periods = []
        for ts in timestamps:
            values = build_weather_obj(properties[ts])
            if not values:
                continue
            period = {"timestamp": ts}
            period.update(values)
            # Add human-readable hour
            try:
                hour_obj = datetime.fromisoformat(ts)
                period['hour'] = hour_obj.strftime('%I:%M %p')
            except (ValueError, TypeError):
                period['hour'] = ''
            periods.append(period)
        if not periods:
            return {"latitude": lat, "longitude": lon, "error": "No weather measurements"}

        # Group by date (DTN already returns 6-hour intervals, no filtering needed)
        from collections import defaultdict
        days_map = defaultdict(list)
        for period in periods:
            date_str = period['timestamp'][:10]
            days_map[date_str].append(period)

        # Build days array with summaries
        days = []
        for date_str in sorted(days_map.keys()):
            day_periods = days_map[date_str]

            temps = [p['temperature']['value']
                     for p in day_periods if 'temperature' in p]
            humidities = [p['relativeHumidity']['value']
                          for p in day_periods if 'relativeHumidity' in p]
            wind_speeds = [p['wind']['speed']['value']
                           for p in day_periods if 'wind' in p and 'speed' in p['wind']]
            precip_probs = [p['precipitation']['probability']['value']
                            for p in day_periods if 'precipitation' in p and 'probability' in p['precipitation']]
            # Note: HDW not available in forecasts - DTN doesn't support aggregation for hotDryWindyIndex

            try:
                date_obj = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=UTC)
                day_name = date_obj.strftime('%A')
            except (ValueError, TypeError):
                day_name = date_str

            days.append({
                'date': date_str,
                'dayName': day_name,
                'periods': day_periods,
                'summary': {
                    'highTemp': {'value': max(temps), 'unit': 'degF'} if temps else None,
                    'lowTemp': {'value': min(temps), 'unit': 'degF'} if temps else None,
                    'maxWindSpeed': {'value': max(wind_speeds), 'unit': 'mph'} if wind_speeds else None,
                    'avgHumidity': sum(humidities) / len(humidities) if humidities else None,
                    'precipProb': max(precip_probs) if precip_probs else None
                }
            })

        return {
            "latitude": lat,
            "longitude": lon,
            "forecastData": {"provider": "DTN", "totalDays": len(days), "days": days}
        }
    else:
        # Current conditions - DTN returns the requested current interval first.
        for ts in timestamps:
            weather = build_weather_obj(properties[ts])
            if weather:
                return {"latitude": lat, "longitude": lon, "currentWeather": {**weather, "timestamp": ts}}
        return {"latitude": lat, "longitude": lon, "error": "No weather measurements"}


def point_status(result):
    """Classify actual fields; missing speed is never a measured calm observation."""
    weather = result.get("currentWeather")
    if not isinstance(weather, dict):
        return {"status": "unavailable" if "error" in result else "available"}
    wind = weather.get("wind") or {}
    speed = (wind.get("speed") or {}).get("value")
    direction = (wind.get("direction") or {}).get("value")
    if speed is None:
        wind_status = "missing"
    elif speed < 0:
        wind_status = "invalid"
    elif speed == 0:
        wind_status = "calm"
    elif direction is None:
        wind_status = "direction_missing"
    elif not 0 <= direction <= 360:
        wind_status = "invalid"
    else:
        wind_status = "measured"
    fields = len(weather.keys() - {"timestamp"})
    return {"status": "available" if wind_status in ("measured", "calm") else "partial",
            "windStatus": wind_status, "fieldCount": fields}


def query_point(lat, lon, params, is_forecast, request_id, operation_id, index):
    """One point's malformed response or failure never aborts another point in the batch."""
    started = time.monotonic()
    try:
        request_timeout()
        result = map_dtn_response(lat, lon, make_dtn_request('/v2/conditions', params), is_forecast)
    except WeatherBudgetExpired:
        result = {"latitude": lat, "longitude": lon, "error": "Weather operation budget exhausted",
                  "errorCode": "WEATHER_BUDGET_EXHAUSTED"}
    except Exception as exc:  # noqa: BLE001 — isolate a point; details and coordinates stay off the wire
        logger.warning("DTN point failed: request_id=%s operation_id=%s point=%d reason=%s",
                       request_id, operation_id, index, type(exc).__name__)
        result = {"latitude": lat, "longitude": lon, "error": "Weather provider unavailable"}
    result.update(point_status(result), durationMs=round((time.monotonic()-started)*1000))
    if "error" in result:
        result.setdefault("errorCode", "WEATHER_UNAVAILABLE" if result["error"] == "Weather provider unavailable"
                          else "WEATHER_DATA_INVALID")
    logger.info("DTN point completed: request_id=%s operation_id=%s point=%d status=%s "
                "wind_status=%s duration_ms=%d",
                request_id, operation_id, index, result["status"], result.get("windStatus", "not_applicable"),
                result["durationMs"])
    return result


def lambda_handler(event, context):
    """Lambda handler for DTN Weather API"""
    if not isinstance(event, dict):
        return error("INVALID_WEATHER_QUERY", "Expected a weather query object.")
    started = time.monotonic()
    request_id = getattr(context, "aws_request_id", None)
    request_id = request_id if isinstance(request_id, str) and len(request_id) <= 80 \
        and all(char.isascii() and (char.isalnum() or char == "-") for char in request_id) else "unavailable"
    logger.info("Lambda invoked: request_id=%s", request_id)
    # Sizes only: the searched coordinates are a user's location.
    logger.info("📍 Event summary: %s location(s), forecast=%s",
                len(event["locations"]) if isinstance(event.get("locations"), list) else 1, "forecastDays" in event)

    deadline_token = limits_token = None
    try:
        operation, request, attempts, reserve = operation_limits()
    except (KeyError, ValueError) as exc:
        logger.error("DTN operation limits not configured: %s", type(exc).__name__)
        return error("WEATHER_NOT_CONFIGURED", "The weather adapter has no valid operation limits.")
    try:
        remaining = getattr(context, "get_remaining_time_in_millis", None)
        if callable(remaining):
            operation = min(operation, max(0.0, remaining()/1000-reserve))
        deadline_token = OPERATION_DEADLINE.set(started+operation)
        limits_token = REQUEST_LIMITS.set((request, attempts))
        operation_id = event.get("operationId")
        if operation_id is not None:
            try:
                operation_id = str(UUID(operation_id)) if isinstance(operation_id, str) else None
            except ValueError:
                operation_id = None
            if operation_id is None:
                raise InvalidWeatherQuery("operationId must be a UUID")
        metadata = {"requestId": request_id, "operationId": operation_id}
        logger.info("Weather operation: request_id=%s operation_id=%s", request_id, operation_id)
        # AgentCore Gateway provides parameters directly - no complex parsing needed
        # This adapter labels temperatures degF, wind mph, and precipitation inches.
        # Never request SI values and label them as U.S. units. Accept the common
        # model alias at this boundary, then send DTN's canonical spelling.
        if event.get('units', 'us-std') not in ('us-std', 'us'):
            raise InvalidWeatherQuery("units must be us-std")
        units = 'us-std'
        is_forecast = 'forecastDays' in event
        if is_forecast and (type(event['forecastDays']) is not int or not 1 <= event['forecastDays'] <= 15):
            raise InvalidWeatherQuery("forecastDays must be an integer from 1 to 15")
        if is_forecast and 'locations' in event:
            raise InvalidWeatherQuery("Forecast accepts a single latitude and longitude")

        def valid_point(point):
            return (isinstance(point, dict)
                    and type(point.get('latitude')) in (int, float)
                    and type(point.get('longitude')) in (int, float)
                    and math.isfinite(point['latitude']) and -90 <= point['latitude'] <= 90
                    and math.isfinite(point['longitude']) and -180 <= point['longitude'] <= 180)

        points = event.get('locations', [event])
        if not isinstance(points, list) or not 1 <= len(points) <= 50 or not all(map(valid_point, points)):
            raise InvalidWeatherQuery("Provide 1–50 valid latitude/longitude pairs")
        if OPERATION_DEADLINE.get() > time.monotonic():
            # Invalid inputs or an exhausted deadline never start a credential/provider read.
            try:
                dtn_credentials()
            except CredentialsUnavailable:
                logger.error("DTN credentials not configured")
                return error("WEATHER_NOT_CONFIGURED", "The weather adapter has no provider credentials.")

        logger.info(
            f"🔧 Request type: {'FORECAST' if is_forecast else 'CURRENT'}, units: {units}")

        # Handle multiple locations (batch) - always current conditions
        if 'locations' in event:
            locations = event['locations']
            results = []
            for index, loc in enumerate(locations):
                lat, lon = loc['latitude'], loc['longitude']
                params = {'lat': lat, 'lon': lon, 'parameters': ','.join(
                    CURRENT_PARAMS), 'units': units}
                results.append(query_point(lat, lon, params, False, request_id, operation_id, index))

            # Remove nulls and return plain JSON, like the other Gateway Lambda targets
            return remove_nulls({"locations": results, "metadata": {
                **metadata, "locationCount": len(results),
                "durationMs": round((time.monotonic()-started)*1000)}})

        # Handle single location
        lat, lon = event['latitude'], event['longitude']

        # Use appropriate parameters based on request type
        param_list = FORECAST_PARAMS if is_forecast else CURRENT_PARAMS
        params = {
            'lat': lat,
            'lon': lon,
            'parameters': ','.join(param_list),
            'units': units
        }

        # Add time parameters and interval for forecast requests
        if is_forecast:
            # Calculate dates: tomorrow + forecastDays
            from datetime import UTC, datetime, timedelta
            forecast_days = event.get('forecastDays', 1)
            tomorrow = (datetime.now(UTC) + timedelta(days=1)
                        ).replace(hour=0, minute=0, second=0, microsecond=0)
            start_time = tomorrow.strftime('%Y-%m-%dT%H:%M:%SZ')
            end_time = (tomorrow + timedelta(days=forecast_days)
                        ).strftime('%Y-%m-%dT%H:%M:%SZ')

            params['startTime'] = start_time
            params['endTime'] = end_time
            params['interval'] = '6h'  # Request 6-hour aggregated data
            logger.info(
                f"📅 Forecast request: {forecast_days} days, {start_time} to {end_time}, interval=6h")

        logger.info("📡 Calling DTN API: %d parameters, units %s", len(param_list), units)
        result = query_point(lat, lon, params, is_forecast, request_id, operation_id, 0)
        logger.info("🔄 Mapped response, has_error=%s", 'error' in result)

        # Wrap single location in array for consistency and remove nulls
        cleaned = remove_nulls({"locations": [result], "metadata": {
            **metadata, "locationCount": 1,
            "durationMs": round((time.monotonic()-started)*1000)}})
        logger.info(f"📤 Returning response size: {len(json.dumps(cleaned))} chars")
        return cleaned

    except InvalidWeatherQuery as e:
        logger.warning("Invalid weather input: %s", e)
        return error("INVALID_WEATHER_QUERY", str(e))
    except Exception as e:  # noqa: BLE001 -- the adapter boundary answers every failure with a stable code
        logger.error("Weather request failed: %s", type(e).__name__)
        return error("WEATHER_UNAVAILABLE", "The weather provider could not answer this request.")
    finally:
        if deadline_token is not None:
            OPERATION_DEADLINE.reset(deadline_token)
        if limits_token is not None:
            REQUEST_LIMITS.reset(limits_token)
